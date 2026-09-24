// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IEntryPoint, PackedUserOperation} from "@openzeppelin/contracts/interfaces/draft-IERC4337.sol";
import {AccountERC7579Hooked} from "@openzeppelin/contracts/account/extensions/draft-AccountERC7579Hooked.sol";
import {MODULE_TYPE_HOOK, MODULE_TYPE_EXECUTOR, Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {Calldata} from "@openzeppelin/contracts/utils/Calldata.sol";
import {ERC7579Utils, Mode, CallType} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {ERC4337Utils} from "@openzeppelin/contracts/account/utils/draft-ERC4337Utils.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {SHOracle} from "./SHOracle.sol";
import {SHRegistry} from "./SHRegistry.sol";
import {SpendingLimitModule} from "./SpendingLimitModule.sol";
import {IReputationRegistry} from "./interfaces/IReputationRegistry.sol";
import {IIdentityRegistry} from "./interfaces/IIdentityRegistry.sol";
import {Context} from "@openzeppelin/contracts/utils/Context.sol";
import {ContextUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/ContextUpgradeable.sol";

/**
 * @title SessionHandler
 * @author Conrad Japhet
 * @notice ERC-7579 smart account guarded by a USD-denominated spending cap. The cap and all
 *         USD-value enforcement live entirely in the installed SpendingLimitModule (installed as
 *         a hook); this contract keeps only the account-level concerns that aren't cap-specific
 *         (ownership, ETH/ERC20 withdrawal, pausing, spending-cap configuration passthroughs, and
 *         the ERC-8004 identity/reputation lookups).
 * @dev Session-key auth is built into the account itself: {_validateUserOp} recovers the signer and
 *      accepts a UserOp signed by the owner OR by {currentSession}, the ONE session key this wallet
 *      authorizes at a time (managed with {addSession}/{removeSession}). No separate validator module
 *      is installed -- SpendingLimitModule is a hook (module type 4) ONLY, enforcing the USD spending
 *      cap on every execution. Raw-signature validation is left disabled at the base's default, so
 *      {_validateUserOp} is the single authentication path; see its NatSpec for why it does not
 *      delegate to {AccountERC7579-_validateUserOp}.
 * @dev A session key EXPIRES: {currentSessionValidUntil} is returned as the op's ERC-4337 validity
 *      window, so the EntryPoint refuses an expired key with `AA22 expired or not due`. It is still a
 *      bare signer within that window -- no per-key selector scope -- bounded by the spending cap
 *      (see {addSession}) and narrowed optionally by the owner-managed {sessionTargetAllowlist}.
 * @dev The USD cap cannot see ETH spent as GAS (the prefund leaves before the hook's preCheck,
 *      refunds land in the EntryPoint deposit after postCheck). {maxOpGasCost} bounds it instead,
 *      and the EntryPoint is a restricted target so a key cannot withdraw the deposit.
 *      THREAT_MODEL §3.12.
 * @dev Deliberate deviation from stock AccountERC7579Hooked, which makes `execute`, `installModule`,
 *      and `uninstallModule` `onlyEntryPointOrSelf` -- an EOA owner cannot call them directly. The
 *      three overrides below reopen a direct-owner path so the owner never has to submit a UserOp for
 *      their own admin actions, and needs no second "owner validator" module. They do NOT all reopen
 *      it the same way:
 *        - {execute} becomes onlyEntryPointOrSelfOrOwner: still reachable through the EntryPoint (that
 *          is how session keys act at all), with {_guardSessionExecution} restraining non-owner callers.
 *        - {installModule} / {uninstallModule} become onlyOwner, which is strictly TIGHTER than stock.
 *          An owner-signed UserOp arrives as msg.sender == EntryPoint, not the owner, so these are
 *          reachable only by a direct owner call and by no UserOp at all -- closing the path where a
 *          session key submits a UserOp aimed straight at them, bypassing {execute}'s guard entirely.
 *      Module reconfiguration has a second, independent line of defence in the hook itself:
 *      SpendingLimitModule's preCheck refuses any execute-routed call to its own admin surface, for
 *      every caller including the owner. The owner's supported route to the cap settings is the
 *      passthroughs below, which call the module directly with the account as msg.sender.
 */
contract SessionHandler is AccountERC7579Hooked, OwnableUpgradeable, Pausable {
    using SafeERC20 for IERC20;

    /*//////////////////////////////////////////////////////////////
                                    ERRORS
    //////////////////////////////////////////////////////////////*/
    error SessionHandler_InvalidRecipient();
    error SessionHandler_NotEnoughBalance();
    error SessionHandler_ExecutionFailed();
    /// @dev Thrown when address(0) is passed as a session key to addSession.
    error SessionHandler_InvalidSessionKey();
    /// @dev Thrown when the owner's own address is passed to addSession. The owner can already sign
    ///      UserOps unconditionally, so the grant would buy nothing and would evict the live key.
    error SessionHandler_SessionKeyIsOwner();
    /// @dev Thrown when addSession is given a deadline that has already passed (or is zero, which the
    ///      EntryPoint would read as "no expiry").
    error SessionHandler_SessionExpiryInPast(uint48 validUntil);
    /// @dev Thrown when addSession is given a deadline further out than {MAX_SESSION_TTL}.
    error SessionHandler_SessionTtlTooLong(uint48 validUntil, uint48 maxTtl);
    /// @dev Thrown when a session-key (non-owner) execution targets the account's own admin surface
    ///      (address(this) or the spending-limit module), which would let a key escape the cap.
    error SessionHandler_SessionRestrictedTarget(address target);
    /// @dev Thrown when a session-key (non-owner) execution uses delegatecall, which runs arbitrary
    ///      code in the account's context and so could reach the admin surface regardless of target.
    error SessionHandler_SessionDelegateCallForbidden();

    error SessionHandler_TransferFailed();

    /// @dev Thrown when a UserOp's prefund request exceeds {maxOpGasCost}.
    error SessionHandler_PrefundTooHigh(uint256 requested, uint256 max);
    /// @dev Thrown when a UserOp's own gas parameters price it above {maxOpGasCost}.
    error SessionHandler_OpGasCostTooHigh(uint256 cost, uint256 max);
    /// @dev Thrown on setMaxOpGasCost(0), which would reject every UserOp.
    error SessionHandler_InvalidMaxOpGasCost();
    /// @dev Thrown when setMaxOpGasCost is given more than {maxOpGasCost} can hold (type(uint80).max,
    ///      ~1.2 million ETH).
    error SessionHandler_MaxOpGasCostTooHigh(uint256 newMax, uint256 limit);
    /// @dev Thrown on enabling an empty allowlist, which would reject every session-key execution.
    error SessionHandler_EmptyAllowlist();
    /// @dev Thrown when address(0) is passed as a session-key allowlist target.
    error SessionHandler_InvalidAllowedTarget();

    /*//////////////////////////////////////////////////////////////
                                    EVENTS
    //////////////////////////////////////////////////////////////*/
    /// @notice Emitted when the owner authorizes a session key, or extends the deadline of the one
    ///         already authorized.
    /// @param sessionKey The authorized signer.
    /// @param validUntil Unix timestamp this key stops being accepted, inclusive of that second.
    event SessionAdded(address indexed sessionKey, uint48 validUntil);
    /// @notice Emitted when a session key stops being authorized -- revoked by the owner, or evicted
    ///         by {addSession} granting a different key.
    event SessionRemoved(address indexed sessionKey);

    /// @notice Emitted when the owner changes the per-UserOp gas-cost ceiling.
    event MaxOpGasCostUpdated(uint256 oldMax, uint256 newMax);
    /// @notice Emitted when the owner enables or disables the session-key target allowlist.
    event SessionAllowlistToggled(bool enabled);
    /// @notice Emitted when a target is added to the session-key allowlist.
    event AllowedTargetAdded(address indexed target);
    /// @notice Emitted when a target is removed from the session-key allowlist.
    event AllowedTargetRemoved(address indexed target);

    /// @notice Emitted when a session-key execution pays the protocol fee. Owner-initiated executions
    ///         pay no fee and emit nothing.
    /// @param treasury The registry-configured recipient at the time of payment.
    /// @param fee      The amount paid in wei. The fee is configured in USD, so this is the converted
    ///        amount at the oracle price that applied to this execution, not the configured figure.
    event ProtocolFeePaid(address indexed treasury, uint256 fee);

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Default {maxOpGasCost} written by {initialize}, in wei.
    /// @dev Deliberately generous — it clears a ~600k-gas swap at 2x a spiking base fee, so a fee
    ///      spike never rejects a legitimate op. A bound on abuse, not a gas budget.
    uint256 public constant DEFAULT_MAX_OP_GAS_COST = 0.1 ether;

    /// @notice Longest a session key may be authorized for, from the moment it is granted.
    /// @dev A hard ceiling rather than an owner setting: a settable one would be raised to "forever"
    ///      by the first UI that defaults it, which is the behaviour this expiry exists to remove.
    ///      Renewal is one owner transaction ({addSession} again), so the cost of a short life is
    ///      friction, not lockout.
    uint48 public constant MAX_SESSION_TTL = 90 days;

    /// @notice This deployment's ERC-4337 EntryPoint. Overrides Account's default (OZ's canonical v0.8
    ///         singleton), since this project uses a v0.7 EntryPoint (see HelperConfig.s.sol).
    /// @dev This and the next three are immutables of the IMPLEMENTATION, set once when SHFactory
    ///      constructs it (see {ProtocolAddresses}). A wallet is an EIP-1167 clone that runs the
    ///      implementation's code, so every wallet reads these same values out of that code: no wallet
    ///      stores them, and no two wallets from one factory can differ. That is only correct because
    ///      all four are the same for every wallet — anything per-wallet stays in storage below.
    address public immutable ENTRY_POINT;
    /// @notice ERC-8004 reputation registry, read by {getAgentReputation}.
    address public immutable REPUTATION_REGISTRY;
    /// @notice ERC-8004 identity registry, read by {getAgentIdentity}.
    address public immutable IDENTITY_REGISTRY;
    /// @notice SHRegistry this wallet resolves the fee, treasury, oracle and agent id from, per call.
    SHRegistry public immutable REGISTRY;

    // Storage below is ordered to pack. The inherited layout ends with a slot holding
    // AccountERC7579Hooked's `_hook` (20 bytes) and Pausable's `_paused` (1 byte); the first two
    // variables fill that slot's remaining 11 bytes, and the next three share one slot. Every UserOp
    // reads `_paused` and {maxOpGasCost}; a session-key execution also reads {SH_MODULE} and
    // {sessionAllowlistEnabled} -- two slots in all. `forge inspect SessionHandler storageLayout` shows
    // the assignment. Wallets are non-upgradeable clones, so a layout change only ever applies to a
    // new implementation, never to a wallet already deployed.

    /// @notice Whether {sessionTargetAllowlist} is being enforced. See {toggleAllowList}.
    bool public sessionAllowlistEnabled;
    /// @notice Maximum total ETH (wei) one UserOp may cost this account, however it is paid.
    /// @dev Owner-settable because gas prices differ per chain and over time; a compile-time constant
    ///      would be too tight somewhere (legitimate ops fail in a fee spike) and too loose elsewhere.
    ///      Enforced in both {_validateUserOp} and {_payPrefund} — see each for why one is not enough.
    /// @dev uint80 holds up to ~1.2 million ETH per op, against a 0.1 ETH default; {setMaxOpGasCost}
    ///      rejects anything larger.
    uint80 public maxOpGasCost;

    /// @notice SpendingLimitModule installed as this wallet's hook in {initialize}; enforces the
    ///         account's USD spending cap.
    /// @dev Per-wallet storage, NOT an immutable like the four above: the operator can change the
    ///      registry's module, and each wallet keeps the one that was current when it was deployed.
    SpendingLimitModule public SH_MODULE;
    /// @notice Sequential id assigned by the factory. Bookkeeping only.
    /// @dev uint64 (~1.8e19 wallets), the same type as SHFactory's {SHFactory-totalWallets}.
    uint64 public WALLET_ID;
    /// @dev Entry count for {sessionTargetAllowlist}; lets {toggleAllowList} refuse an empty one.
    ///      uint32 (~4.3 billion) is far more targets than any owner could pay gas to add.
    uint32 public allowedTargetCount;

    /// @notice Targets a session key may call, when {sessionAllowlistEnabled} is true. OFF by default.
    /// @dev Confines a key to a fixed set of venues — mainly to keep it away from protocols where the
    ///      account can take on a LIABILITY, which the balance-diff meter never charges to the cap
    ///      (THREAT_MODEL §3.13). Address-granular, never selector-granular, so the account needs no
    ///      ABI knowledge of what it calls.
    mapping(address target => bool allowed) public sessionTargetAllowlist;

    /// @notice The ONE session key this wallet authorizes, or address(0) for none. The owner is
    ///         always authorized separately, in {_validateUserOp}.
    /// @dev One key at a time, not an allowlist: a wallet that can authorize several keys can drift
    ///      out of step with whatever off-chain system holds them, and a mapping cannot be
    ///      enumerated, so nothing on chain could answer "which keys does this wallet trust?".
    ///      {addSession} evicts whatever was here before. Within its window the key is still a BARE
    ///      signer -- it may drive ANY execute() call, bounded by the SpendingLimitModule spending
    ///      cap, {_guardSessionExecution}, and {maxOpGasCost}.
    /// @dev Packs with {currentSessionValidUntil} into one slot; the two are always written together
    ///      so `currentSession != address(0)` iff `currentSessionValidUntil != 0`. {_validateUserOp}
    ///      depends on that: the EntryPoint reads a zero deadline as "valid forever".
    address public currentSession;
    /// @notice Unix timestamp at which {currentSession} stops being accepted. Inclusive: an op is
    ///         still valid in the second that equals it, matching the EntryPoint's own comparison.
    uint48 public currentSessionValidUntil;

    /*//////////////////////////////////////////////////////////////
                                Constructor
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The four protocol addresses every wallet shares. SHFactory passes them once, when it
     *         constructs the implementation that all of its wallets are cloned from.
     * @dev Kept out of {InitConfig} because they are identical for every wallet a factory deploys, so
     *      they become immutables of the implementation instead of per-wallet storage. Nothing is lost
     *      by fixing them: SHRegistry holds the EntryPoint and both ERC-8004 registries as immutables
     *      too, and a factory is bound to one registry for life.
     * @param entryPoint         This deployment's ERC-4337 EntryPoint (v0.7).
     * @param reputationRegistry ERC-8004 reputation registry.
     * @param identityRegistry   ERC-8004 identity registry.
     * @param registry           SHRegistry every wallet reads protocol config from.
     */
    struct ProtocolAddresses {
        address entryPoint;
        address reputationRegistry;
        address identityRegistry;
        address registry;
    }

    /// @param protocol See {ProtocolAddresses}.
    constructor(ProtocolAddresses memory protocol) {
        ENTRY_POINT = protocol.entryPoint;
        REPUTATION_REGISTRY = protocol.reputationRegistry;
        IDENTITY_REGISTRY = protocol.identityRegistry;
        REGISTRY = SHRegistry(protocol.registry);
        _disableInitializers();
    }

    /*//////////////////////////////////////////////////////////////
                                Initialization
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The per-wallet settings {initialize} needs, bundled so the wallet is fully usable after
     *         ONE transaction: ownership, the spending-cap config, and the two owner-only grants that
     *         used to require a follow-up transaction each ({addSession}, {addTrustedSpender}).
     * @dev The protocol addresses every wallet shares are not here; the constructor fixes them in the
     *      implementation (see {ProtocolAddresses}). A struct rather than a flat parameter list so the
     *      factory's call names every field.
     * @param owner               Account owner. SHFactory passes its own msg.sender, so the user
     *                            who signs the deploy owns the wallet.
     * @param walletId            Sequential id assigned by the factory. Bookkeeping only.
     * @param spendingLimitModule SpendingLimitModule to install as MODULE_TYPE_HOOK.
     * @param dailyLimitUsd       Max USD (18 decimals) spendable per window. Must be >= 0.
     * @param windowDuration      Spending-window length in seconds. Must be > 0.
     * @param watchedTokens       Tokens to meter. Each must already be priced by the oracle.
     * @param sessionKey          Session key to authorize, or address(0) for an owner-only wallet.
     * @param sessionKeyValidUntil Unix timestamp the session key expires at. Subject to the same
     *                            rules as {addSession}, so it must be in the future and no further
     *                            out than {MAX_SESSION_TTL}. Ignored when `sessionKey` is address(0).
     * @param trustedSpenders     Spenders trusted for unpriced-token approvals (typically the DEX
     *                            router). May be empty.
     */
    struct InitConfig {
        address owner;
        uint64 walletId;
        address spendingLimitModule;
        int256 dailyLimitUsd;
        uint256 windowDuration;
        address[] watchedTokens;
        address sessionKey;
        uint48 sessionKeyValidUntil;
        address[] trustedSpenders;
    }

    /**
     * @notice Initializes a freshly cloned wallet. Callable once, by the factory that cloned it.
     * @dev The two grants at the end are what make a deployed wallet immediately usable. Both are
     *      normally `onlyOwner` and are applied here directly, which is safe for the same reason the
     *      owner's own passthroughs are: this runs inside `initializer`, before any key exists that
     *      could reach it, and the values come from the caller who is about to become the owner.
     * @param cfg See {InitConfig}.
     */
    function initialize(InitConfig calldata cfg) external initializer {
        __Ownable_init(cfg.owner);
        WALLET_ID = cfg.walletId;
        SH_MODULE = SpendingLimitModule(cfg.spendingLimitModule);
        // Safe: the default is 0.1 ETH, far inside uint80.
        // forge-lint: disable-next-line(unsafe-typecast)
        maxOpGasCost = uint80(DEFAULT_MAX_OP_GAS_COST);

        // Install the spending-limit hook. Its onInstall
        // decodes exactly this (dailyLimitUsd, windowDuration, watchedTokens) tuple, so the config
        // must be non-empty and valid: windowDuration > 0, dailyLimitUsd >= 0, and every watched
        // token already priced by the oracle.
        _installModule(
            MODULE_TYPE_HOOK,
            cfg.spendingLimitModule,
            abi.encode(cfg.dailyLimitUsd, cfg.windowDuration, cfg.watchedTokens)
        );

        // Authorize the wallet's session key, if one was supplied. Routed through the SAME internal
        // grant as {addSession} -- including the expiry rules -- so a deploy can never seed a key on
        // terms an owner transaction would have refused. address(0) is not an error here (unlike in
        // {addSession}): it means an owner-only wallet, and its deadline is ignored.
        // Must come after __Ownable_init: _grantSession rejects the owner's own address.
        if (cfg.sessionKey != address(0)) {
            _grantSession(cfg.sessionKey, cfg.sessionKeyValidUntil);
        }

        // Which venue a wallet trades on remains the OWNER'S choice, not protocol config -- this list
        // comes from the deploying caller, not from the registry. (The old deploy-time auto-trust of
        // SHRegistry.router() was removed deliberately and is NOT being reinstated here.) An empty
        // array keeps the previous behaviour exactly: nothing trusted, so unpriced-token approvals
        // (e.g. an LP token in removeLiquidity) are refused until the owner grants a spender.
        //
        // MUST come after _installModule: SpendingLimitModule.addTrustedSpender is `onlyInstalled`.
        // These are DIRECT calls to the module (msg.sender == this account), not wrapped in
        // execute(), so no hook runs and the module's admin-selector guard never fires -- the same
        // path {addTrustedSpender}'s owner passthrough uses. The module validates each entry
        // (rejects address(0), caps at MAX_TRUSTED_SPENDERS, ignores duplicates).
        for (uint256 i = 0; i < cfg.trustedSpenders.length; i++) {
            SH_MODULE.addTrustedSpender(cfg.trustedSpenders[i]);
        }
    }

    function entryPoint() public view override returns (IEntryPoint) {
        return IEntryPoint(ENTRY_POINT);
    }

    /*//////////////////////////////////////////////////////////////
                     OWNER DIRECT-CALL ESCAPE HATCH
    //////////////////////////////////////////////////////////////*/

    /// @dev Allows the EntryPoint, the account itself (self-call), or the owner directly.
    modifier onlyEntryPointOrSelfOrOwner() {
        _onlyEntryPointOrSelfOrOwner();
        _;
    }

    function _onlyEntryPointOrSelfOrOwner() internal view {
        if (msg.sender != owner()) _checkEntryPointOrSelf();
    }

    /**
     * @notice Executes on behalf of the account. Callable by the EntryPoint, the account itself
     *         (via an installed executor), or directly by the owner without a UserOp.
     * @dev The installed SpendingLimitModule hook wraps this call: its preCheck/postCheck enforce
     *      the account's USD spending cap around whatever runs here. For non-owner (session-key)
     *      executions, {_guardSessionExecution} additionally blocks any sub-call to the account's own
     *      admin surface, so a session key cannot uninstall the hook or raise the cap to escape it.
     */
    function execute(bytes32 mode, bytes calldata executionCalldata)
        public
        payable
        override
        whenNotPaused
        onlyEntryPointOrSelfOrOwner
    {
        Mode execMode = Mode.wrap(mode);
        // Owner-initiated calls are unrestricted; any other path (a session-key UserOp via the
        // EntryPoint, or a self-call) must not be able to reach the account's own admin surface.
        if (msg.sender != owner()) {
            _guardSessionExecution(execMode, executionCalldata);

            _extractFee();
        }
        _execute(execMode, executionCalldata);
    }

    /// @notice Installs an ERC-7579 module.
    /// @dev Owner-only, via a direct call: an owner-signed UserOp arrives as msg.sender == EntryPoint
    ///      (not the owner), so onlyOwner rejects it. This is deliberate — a session key could
    ///      otherwise submit a UserOp whose callData targets this function directly (bypassing
    ///      {execute}'s {_guardSessionExecution}) to install a malicious validator/executor and
    ///      escape the spending cap. Deployment is unaffected: {initialize} uses the internal
    ///      {_installModule} rather than this external entrypoint.
    function installModule(uint256 moduleTypeId, address module, bytes calldata initData) public override onlyOwner {
        _installModule(moduleTypeId, module, initData);
    }

    /// @notice Uninstalls an ERC-7579 module.
    /// @dev Owner-only, via a direct call (same rationale as {installModule}): a session key must
    ///      never be able to uninstall the SpendingLimitModule hook to lift its own cap. Because an
    ///      owner-signed UserOp is seen as msg.sender == EntryPoint, this is reachable only by the
    ///      owner calling the account directly, not through any UserOp.
    function uninstallModule(uint256 moduleTypeId, address module, bytes calldata deInitData)
        public
        override
        onlyOwner
    {
        _uninstallModule(moduleTypeId, module, deInitData);
    }

    /*//////////////////////////////////////////////////////////////
           SPENDING-LIMIT CONFIG (owner-only passthrough to module)
    //////////////////////////////////////////////////////////////*/

    /// @dev Each setter below calls the module AS this account, so the module keys the config under
    ///      this account's address. Kept owner-only on purpose: a session key must never be able to
    ///      raise its own cap or reshape the watched list (see SpendingLimitModule's NatSpec).
    /// @dev These passthroughs are also the ONLY working route to the module's setters, for the owner
    ///      included. Reaching them via execute(address(SH_MODULE), ...) reverts with
    ///      SpendingLimitModule_AdminExecution -- the hook cannot tell an owner-driven execute from a
    ///      session-key one, so it refuses both. A direct call here is not wrapped in execute, so no
    ///      hook runs and the account reaches the module as itself.

    /// @notice Sets the account's max USD spend per window (18 decimals). Forwards to SH_MODULE.setDailyLimit.
    function setDailyLimit(int256 dailyLimitUsd) external onlyOwner {
        SH_MODULE.setDailyLimit(dailyLimitUsd);
    }

    /// @notice Sets the account's spending-window length in seconds. Forwards to SH_MODULE.setWindowDuration.
    function setWindowDuration(uint256 windowDuration) external onlyOwner {
        SH_MODULE.setWindowDuration(windowDuration);
    }

    /// @notice Adds a token to the account's watched (value-metered) list. Forwards to SH_MODULE.addWatchedToken.
    function addWatchedToken(address token) external onlyOwner {
        SH_MODULE.addWatchedToken(token);
    }

    /// @notice Removes a token from the account's watched list. Forwards to SH_MODULE.removeWatchedToken.
    function removeWatchedToken(address token) external onlyOwner {
        SH_MODULE.removeWatchedToken(token);
    }

    /// @notice Returns the account's full spending-limit config from the module.
    function getConfig() external view returns (SpendingLimitModule.Config memory) {
        return SH_MODULE.getConfig(address(this));
    }

    /// @notice Returns whether a token is on the account's watched list.
    function isWatched(address token) external view returns (bool) {
        return SH_MODULE.isWatched(address(this), token);
    }

    /// @notice Returns the account's remaining USD budget for the current window (18 decimals).
    function getRemainingBudget() external view returns (int256) {
        return SH_MODULE.getRemainingBudget(address(this));
    }

    /// @notice Trusts a spender so it may be approved even for unpriced tokens (e.g. the DEX router,
    ///         for removeLiquidity's LP-token approval). Forwards to SH_MODULE.addTrustedSpender.
    /// @dev Owner-only: a trusted spender can pull an unpriced token within a single transaction.
    ///      The list holds whatever {InitConfig-trustedSpenders} seeded at deploy, which is EMPTY
    ///      unless the deploying caller passed one -- it is never protocol config. Use this to grant
    ///      a spender afterwards, or on a wallet deployed with an empty list.
    function addTrustedSpender(address spender) external onlyOwner {
        SH_MODULE.addTrustedSpender(spender);
    }

    /// @notice Stops trusting a spender. Forwards to SH_MODULE.removeTrustedSpender.
    function removeTrustedSpender(address spender) external onlyOwner {
        SH_MODULE.removeTrustedSpender(spender);
    }

    /// @notice Returns whether a spender is trusted for this account.
    function isTrustedSpender(address spender) external view returns (bool) {
        return SH_MODULE.isTrustedSpender(address(this), spender);
    }

    /*//////////////////////////////////////////////////////////////
                    SESSION TARGET ALLOWLIST (owner-only)
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Turns the session-key target allowlist on or off.
     * @dev Refuses to enable an empty allowlist (it would reject every session execution). Disabling
     *      is always allowed, so the owner can never be locked out of restoring service.
     * @param enabled True to enforce {sessionTargetAllowlist}.
     */
    function toggleAllowList(bool enabled) external onlyOwner {
        if (enabled && allowedTargetCount == 0) revert SessionHandler_EmptyAllowlist();
        sessionAllowlistEnabled = enabled;
        emit SessionAllowlistToggled(enabled);
    }

    /**
     * @notice Permits session keys to call `target` while the allowlist is enforced. No-op if
     *         already allowed.
     * @param target The contract a session key may call. Must not be address(0).
     */
    function addAllowedTarget(address target) external onlyOwner {
        if (target == address(0)) revert SessionHandler_InvalidAllowedTarget();
        if (sessionTargetAllowlist[target]) return;
        sessionTargetAllowlist[target] = true;
        allowedTargetCount++;
        emit AllowedTargetAdded(target);
    }

    /**
     * @notice Adds several targets in one transaction (a real allowlist needs a router plus tokens).
     * @param targets The contracts session keys may call. Each must not be address(0).
     */
    function addAllowedTargets(address[] calldata targets) external onlyOwner {
        for (uint256 i = 0; i < targets.length; i++) {
            address target = targets[i];
            if (target == address(0)) revert SessionHandler_InvalidAllowedTarget();
            if (sessionTargetAllowlist[target]) continue;
            sessionTargetAllowlist[target] = true;
            allowedTargetCount++;
            emit AllowedTargetAdded(target);
        }
    }

    /**
     * @notice Stops session keys from calling `target`. No-op if it was not allowed.
     * @dev Emptying the list does NOT auto-disable enforcement: it fails closed rather than silently
     *      reopening every target. Disable deliberately with {toggleAllowList}.
     * @param target The contract to remove.
     */
    function removeAllowedTarget(address target) external onlyOwner {
        if (!sessionTargetAllowlist[target]) return;
        delete sessionTargetAllowlist[target];
        allowedTargetCount--;
        emit AllowedTargetRemoved(target);
    }

    /**
     * @notice Sets the maximum total ETH one UserOp may cost this account. See {maxOpGasCost}.
     * @dev Raise it on an expensive chain, lower it to tighten the bound on a compromised key.
     *      Reverts on 0, which would reject every UserOp, and above type(uint80).max, which the
     *      storage slot cannot hold. Takes a uint256 so the ABI is unchanged for callers.
     * @param newMax New ceiling in wei. Must be > 0 and <= type(uint80).max.
     */
    function setMaxOpGasCost(uint256 newMax) external onlyOwner {
        if (newMax == 0) revert SessionHandler_InvalidMaxOpGasCost();
        if (newMax > type(uint80).max) revert SessionHandler_MaxOpGasCostTooHigh(newMax, type(uint80).max);
        // Cached before the write: emitting after assigning would report the NEW value as `oldMax`,
        // losing the previous ceiling for anything indexing this event.
        uint256 oldMax = maxOpGasCost;
        // Safe: bounded by type(uint80).max above.
        // forge-lint: disable-next-line(unsafe-typecast)
        maxOpGasCost = uint80(newMax);
        emit MaxOpGasCostUpdated(oldMax, newMax);
    }

    /*//////////////////////////////////////////////////////////////
                       SESSION-KEY AUTH (owner-managed)
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Authorizes `sessionKey` until `validUntil`, replacing whatever key was authorized
     *         before. Called again with the same key, it extends that key's deadline.
     * @dev SECURITY: a session key is a BARE signer -- until it expires it can drive ANY execute()
     *      call to external targets, gated by the SpendingLimitModule USD spending cap and by
     *      {_guardSessionExecution} (which blocks it from the account's own admin surface). It is
     *      NOT scoped to particular external targets or selectors; scoped keys (Smart Sessions /
     *      ERC-7715) remain a deliberate future step. Grant keys only to agents trusted to stay
     *      within the cap, and prefer a short `validUntil`.
     * @dev Granting a DIFFERENT key revokes the current one in the same transaction. That is the
     *      point: one key at a time is what keeps the wallet and whoever holds the key in step.
     * @param sessionKey The signer address to authorize. Must not be address(0) or the owner.
     * @param validUntil Unix timestamp the key expires at. Must be in the future and no further out
     *                   than {MAX_SESSION_TTL}.
     */
    function addSession(address sessionKey, uint48 validUntil) external onlyOwner {
        _grantSession(sessionKey, validUntil);
    }

    /**
     * @notice Revokes this wallet's session key. No-op when none is authorized.
     * @dev Takes no argument because only one key can be authorized at a time -- an address
     *      parameter would let a caller "revoke" a key that was never live and read the silent
     *      no-op as success.
     */
    function removeSession() external onlyOwner {
        address previous = currentSession;
        if (previous == address(0)) return;
        currentSession = address(0);
        currentSessionValidUntil = 0;
        emit SessionRemoved(previous);
    }

    /**
     * @notice Whether `key` is this wallet's session key AND has not expired.
     * @dev The comparison is `<=`, matching the EntryPoint's own: an op is still valid in the second
     *      that equals the deadline (EntryPoint treats a UserOp as out of range only once
     *      `block.timestamp > validUntil`). A view that disagreed with the chain, even by a second
     *      and even conservatively, would be a debugging trap. Callers wanting a safety margin
     *      before starting a transaction should compare {currentSessionValidUntil} themselves.
     * @dev Reading block.timestamp is fine here: this is an ordinary view, never reached during
     *      ERC-4337 validation.
     */
    function isSessionActive(address key) public view returns (bool) {
        return key != address(0) && key == currentSession && block.timestamp <= currentSessionValidUntil;
    }

    /**
     * @dev The single path that authorizes a session key, shared by {addSession} and {initialize} so
     *      a deploy cannot seed a key on terms an owner transaction would refuse.
     * @dev Rejects the owner's own address: the owner is already accepted unconditionally by
     *      {_validateUserOp}, so granting it would authorize nothing new while evicting the key the
     *      wallet actually relies on.
     * @dev `validUntil == 0` is caught by the past-deadline check, which matters more than it looks:
     *      the EntryPoint reads a zero deadline in validation data as "valid forever".
     */
    function _grantSession(address sessionKey, uint48 validUntil) internal {
        if (sessionKey == address(0)) revert SessionHandler_InvalidSessionKey();
        if (sessionKey == owner()) revert SessionHandler_SessionKeyIsOwner();
        if (validUntil <= block.timestamp) revert SessionHandler_SessionExpiryInPast(validUntil);
        if (validUntil > block.timestamp + MAX_SESSION_TTL) {
            revert SessionHandler_SessionTtlTooLong(validUntil, MAX_SESSION_TTL);
        }

        address previous = currentSession;
        // A renewal of the same key is an extension, not a revoke-and-regrant: emitting SessionRemoved
        // for it would tell anything watching events that the wallet lost its key.
        if (previous != address(0) && previous != sessionKey) emit SessionRemoved(previous);

        currentSession = sessionKey;
        currentSessionValidUntil = validUntil;
        emit SessionAdded(sessionKey, validUntil);
    }

    /**
     * @notice ERC-4337 entry point for validating a UserOp against this account.
     * @dev Overridden only to add {whenNotPaused}, so a paused wallet fails in validation and pays no
     *      prefund, rather than paying one and then reverting in `execute`. `onlyEntryPoint` comes
     *      from {Account-validateUserOp} via `super`.
     */
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash, uint256 missingAccountFunds)
        public
        override
        whenNotPaused
        returns (uint256)
    {
        return super.validateUserOp(userOp, userOpHash, missingAccountFunds);
    }

    /**
     * @dev Rejects any UserOp whose own gas parameters price it above {maxOpGasCost}. Stops a
     *      compromised key signing an op with absurd gas fields and bundling it itself: the inflated
     *      `preVerificationGas` is charged as if consumed and paid to the bundler beneficiary.
     * @dev Must price the op rather than `missingAccountFunds`: the EntryPoint debits the FULL
     *      requiredPrefund from the account's deposit and only asks for the shortfall, so a wallet
     *      carrying a deposit (every wallet accrues one from refunds) would see a top-up of 0 however
     *      extravagant the op. Pricing the op is deposit-independent.
     * @dev Packing per ERC-4337 v0.7+: `accountGasLimits` = verificationGasLimit | callGasLimit;
     *      `gasFees` = maxPriorityFeePerGas | maxFeePerGas (high | LOW 128 each). Paymaster limits are
     *      excluded — a sponsored op costs the account nothing.
     * @dev Authenticates the op HERE rather than delegating to {AccountERC7579-_validateUserOp}. The
     *      base routes to a validator module when the nonce key names one and otherwise falls back to
     *      {AbstractSigner-_rawSignatureValidation}; this account installs no validator module
     *      (SpendingLimitModule is a hook ONLY), so that branch is dead, and taking it would cost a
     *      SECOND ecrecover to learn which key signed. Recovering once here is what lets an expired
     *      session key be reported as `AA22 expired or not due` rather than the misleading
     *      `AA24 signature error`.
     *      DELIBERATE CONSEQUENCE: a validator module installed by the owner is ignored for UserOp
     *      validation, though {AccountERC7579-isValidSignature} would still consult it. Installing one
     *      is an owner-key action (THREAT_MODEL §3.9), and ignoring it here fails towards this
     *      account's own auth rather than towards a module's.
     * @dev The bot and the Foundry helper sign the EIP-191 envelope of {Account-_signableUserOpHash}
     *      (toEthSignedMessageHash / eth_account.encode_defunct), so it is re-wrapped before recovery.
     *      tryRecover keeps a malformed signature to SIG_VALIDATION_FAILED instead of reverting
     *      validation, which the EntryPoint requires.
     * @dev The session key's deadline is RETURNED as the op's validity window, not compared here: the
     *      EntryPoint does that comparison inside handleOps. A zero deadline would read as "valid
     *      forever" there, which is why {_grantSession} refuses one and why the key and its deadline
     *      are always written together.
     */
    function _validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash, bytes calldata signature)
        internal
        override
        returns (uint256)
    {
        uint256 verificationGasLimit = uint256(userOp.accountGasLimits) >> 128;
        uint256 callGasLimit = uint128(uint256(userOp.accountGasLimits));
        uint256 maxFeePerGas = uint128(uint256(userOp.gasFees));
        // Any op extreme enough to overflow this is rejected by the checked-arithmetic revert, which
        // is the same outcome as failing the comparison below.
        uint256 cost = (verificationGasLimit + callGasLimit + userOp.preVerificationGas) * maxFeePerGas;
        if (cost > maxOpGasCost) revert SessionHandler_OpGasCostTooHigh(cost, maxOpGasCost);

        (address signer, ECDSA.RecoverError err,) = ECDSA.tryRecoverCalldata(
            MessageHashUtils.toEthSignedMessageHash(_signableUserOpHash(userOp, userOpHash)), signature
        );
        if (err != ECDSA.RecoverError.NoError) return ERC4337Utils.SIG_VALIDATION_FAILED;

        // The owner is authorized unconditionally and carries no validity window.
        if (signer == owner()) return ERC4337Utils.SIG_VALIDATION_SUCCESS;

        // Also covers "no session key at all": currentSession is then address(0), which no
        // successfully recovered signer can equal.
        if (signer != currentSession) return ERC4337Utils.SIG_VALIDATION_FAILED;

        return ERC4337Utils.packValidationData(true, 0, currentSessionValidUntil);
    }

    /**
     * @dev Blocks a non-owner execution — a session key, an installed executor, or a self-call — from
     *      reaching the account's own admin surface. Such callers may act on external protocols under
     *      the USD cap, but must never be able to reconfigure or remove the cap itself, nor reach
     *      value the cap cannot see:
     *        - single / batch: reverts if any sub-call targets a restricted address (see
     *          {_requireUnrestrictedTarget}) or, when enabled, one outside {sessionTargetAllowlist};
     *        - delegatecall: reverts outright, since delegated code runs in this account's context
     *          and could reach the admin surface regardless of the encoded target.
     *
     *      What each restricted entry is actually doing, stated honestly because the three are NOT
     *      equally load-bearing:
     *        - ENTRY_POINT — the only one closing a hole nothing else closes. `withdrawTo` moves the
     *          account's 4337 deposit without changing `account.balance`, so the hook meters $0.
     *        - the delegatecall ban — likewise unique: delegated code runs as this account and would
     *          reach the admin surface whatever target was encoded.
     *        - address(this) and SH_MODULE — defence in depth TODAY, not a lone barrier. Every account
     *          function reachable at address(this) is now `onlyOwner` or `onlyEntryPoint`, so a
     *          self-call arrives with msg.sender == the account and fails `_checkOwner` on its own.
     *          (An older comment here claimed a session key could self-call installModule/
     *          uninstallModule because "those functions accept msg.sender == the account". That was
     *          true when they were `onlyEntryPointOrSelf`; they are `onlyOwner` now. The restriction
     *          is still worth keeping — it holds even if that access control is ever loosened — but
     *          do not repeat the dead justification.)
     *          SH_MODULE similarly backstops the module's own admin guard, whose setters key by
     *          msg.sender.
     */
    function _guardSessionExecution(Mode mode, bytes calldata executionCalldata) internal view {
        (CallType callType,,,) = ERC7579Utils.decodeMode(mode);

        if (callType == ERC7579Utils.CALLTYPE_SINGLE) {
            (address target,,) = ERC7579Utils.decodeSingle(executionCalldata);
            _requireUnrestrictedTarget(target);
        } else if (callType == ERC7579Utils.CALLTYPE_BATCH) {
            Execution[] calldata batch = ERC7579Utils.decodeBatch(executionCalldata);
            for (uint256 i = 0; i < batch.length; i++) {
                _requireUnrestrictedTarget(batch[i].target);
            }
        } else if (callType == ERC7579Utils.CALLTYPE_DELEGATECALL) {
            revert SessionHandler_SessionDelegateCallForbidden();
        }
    }

    /**
     * @dev Reverts if a session key may not call `target`: a permanent denylist (the account, the
     *      module, the EntryPoint), plus {sessionTargetAllowlist} when enabled. The EntryPoint is on
     *      the denylist because `withdrawTo` moves the account's 4337 deposit without changing
     *      `account.balance`, so the hook would meter a $0 spend. See {_guardSessionExecution}.
     */
    function _requireUnrestrictedTarget(address target) internal view {
        if (target == address(this) || target == address(SH_MODULE) || target == ENTRY_POINT) {
            revert SessionHandler_SessionRestrictedTarget(target);
        }
        if (sessionAllowlistEnabled && !sessionTargetAllowlist[target]) {
            revert SessionHandler_SessionRestrictedTarget(target);
        }
    }

    /**
     * @dev Pays the protocol fee for one session-key execution. Both the recipient and the amount are
     *      resolved from the registry per call, never stored here, so a fee or treasury change lands
     *      on every deployed wallet at once.
     * @dev The amount is a flat wei figure read straight off the registry, with no oracle call, so
     *      an ERC-20-only execution never touches the native feed and a stale ETH/USD feed does not
     *      block fee collection. {SHRegistry} packs the fee beside the treasury address, so the two
     *      calls below cost one storage read between them.
     * @dev Called BEFORE {_execute}, so the transfer lands outside the hook's preCheck→postCheck
     *      window and is NOT charged against the account's USD spending cap. That is intentional —
     *      the cap meters what the user spends, and a protocol fee is not the user's spend. It does
     *      mean the fee is native outflow the cap never sees, bounded by {SHRegistry-MAX_PROTOCOL_FEE}
     *      per execution rather than by the cap.
     */
    function _extractFee() internal {
        address treasury = REGISTRY.treasury();
        uint256 fee = REGISTRY.getFee();
        if (address(this).balance < fee) revert SessionHandler_NotEnoughBalance();
        (bool success,) = treasury.call{value: fee}("");
        if (!success) revert SessionHandler_TransferFailed();
        emit ProtocolFeePaid(treasury, fee);
    }

    /**
     * @dev Bounds the prefund this account will pay for one UserOp. Not redundant with the ceiling in
     *      {_validateUserOp}: the EntryPoint also forwards a UserOp's callData to the account in the
     *      EXECUTION phase, where a key can call `validateUserOp` with a hand-crafted op (harmless gas
     *      fields) and any `missingAccountFunds`. Only this check stops that transfer.
     */
    function _payPrefund(uint256 missingAccountFunds) internal override {
        if (missingAccountFunds > maxOpGasCost) {
            revert SessionHandler_PrefundTooHigh(missingAccountFunds, maxOpGasCost);
        }
        super._payPrefund(missingAccountFunds);
    }

    /**
     * @notice Executes on behalf of the account at the request of an installed executor module.
     * @dev Runs {_guardSessionExecution}, exactly as {execute} does for a session key. An executor is
     *      automated spending authority the owner delegated, so it is held to the same boundary:
     *      no reaching the account's own admin surface, no `SH_MODULE`, no `ENTRY_POINT`, no
     *      `delegatecall`, and bound by {sessionTargetAllowlist} when the owner has enabled it.
     *
     *      This guard was MISSING until 2026-09-14, and the gap was real: an executor could call
     *      `ENTRY_POINT.withdrawTo` and drain the account's 4337 deposit, which never touches
     *      `account.balance` and so metered as a $0 spend against the USD cap. Installing an executor
     *      is `onlyOwner` and none is installed at deploy, so it was a trust-the-executor assumption
     *      rather than a session-key hole — but it contradicted the guarantee THREAT_MODEL §3.5 makes
     *      for "every non-owner path", and an owner installing a capability executor (swaps, payroll,
     *      intents) had no reason to expect a weaker boundary than a session key gets.
     *
     *      No owner branch, unlike {execute}: an executor module is never the owner, which is also why
     *      {_extractFee} is unconditional here. Guard runs BEFORE the fee so a rejected call is never
     *      charged.
     * @param mode              ERC-7579 execution mode (single / batch / delegatecall).
     * @param executionCalldata The encoded execution, shaped by the mode's CallType.
     * @return returnData       Per-sub-call return data from {_execute}.
     */
    function executeFromExecutor(bytes32 mode, bytes calldata executionCalldata)
        public
        payable
        override
        onlyModule(MODULE_TYPE_EXECUTOR, Calldata.emptyBytes())
        whenNotPaused
        returns (bytes[] memory returnData)
    {
        Mode execMode = Mode.wrap(mode);
        _guardSessionExecution(execMode, executionCalldata);

        _extractFee();

        return _execute(execMode, executionCalldata);
    }

    /*//////////////////////////////////////////////////////////////
                           OWNER-ONLY FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    /**
     * @notice Withdraws ERC20 tokens or native ETH from the wallet to a recipient chosen by the owner.
     * @param token  ERC20 token address to withdraw, or address(0) for native ETH.
     * @param amount Amount to withdraw in the token's base units.
     * @param to     Recipient address for the withdrawn funds.
     */
    function withdraw(address token, uint256 amount, address to) external onlyOwner {
        if (to == address(0)) revert SessionHandler_InvalidRecipient();
        if (token != address(0)) {
            if (IERC20(token).balanceOf(address(this)) < amount) revert SessionHandler_NotEnoughBalance();
            SafeERC20.safeTransfer(IERC20(token), to, amount);
        } else {
            if (address(this).balance < amount) revert SessionHandler_NotEnoughBalance();
            (bool success,) = payable(to).call{value: amount}("");
            if (!success) revert SessionHandler_ExecutionFailed();
        }
    }

    /*//////////////////////////////////////////////////////////////
                             VIEW FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /// @notice Returns the USD value (18 decimals) of `amount` of `token`, via the registered SHOracle.
    function getUsdValue(address token, uint256 amount) public view returns (int256) {
        return SHOracle(REGISTRY.priceOracle()).getPrice(token, amount);
    }

    function getAgentId() public view returns (uint256) {
        return REGISTRY.agentId();
    }

    /// @notice Returns the agent's ERC-8004 on-chain identity.
    function getAgentIdentity() public view returns (bool registered, uint256 agentId, string memory agentUri) {
        agentId = getAgentId();
        try IIdentityRegistry(IDENTITY_REGISTRY).ownerOf(agentId) returns (address) {
            agentUri = IIdentityRegistry(IDENTITY_REGISTRY).tokenURI(agentId);
            registered = true;
        } catch {
            registered = false;
        }
    }

    /// @notice Returns the agent's on-chain reputation from the Reputation Registry, scoped to this wallet.
    function getAgentReputation()
        public
        view
        returns (uint256 agentId, uint64 feedbackCount, int128 summaryValue, uint8 summaryValueDecimals)
    {
        agentId = getAgentId();
        address[] memory clients = new address[](1);
        clients[0] = address(this);
        (feedbackCount, summaryValue, summaryValueDecimals) =
            IReputationRegistry(REPUTATION_REGISTRY).getSummary(agentId, clients, "", "");
    }

    /*//////////////////////////////////////////////////////////////
                      CONTEXT OVERRIDE RESOLUTION
    //////////////////////////////////////////////////////////////*/

    /// @dev Resolves the diamond between the non-upgradeable Context (pulled in by the ERC-7579
    ///      account stack) and ContextUpgradeable (pulled in by OwnableUpgradeable). Both are
    ///      stateless and identical for a non-ERC-2771 account, so these return the plain msg.* values.
    function _msgSender() internal view override(Context, ContextUpgradeable) returns (address) {
        return msg.sender;
    }

    function _msgData() internal view override(Context, ContextUpgradeable) returns (bytes calldata) {
        return msg.data;
    }

    function _contextSuffixLength() internal view override(Context, ContextUpgradeable) returns (uint256) {
        return 0;
    }
}
