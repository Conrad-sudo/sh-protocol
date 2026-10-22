// SPDX-License-Identifier: BUSL-1.1
// Copyright (C) 2026 Conrad Japhet
// Use of this software is governed by the Business Source License included in the LICENSE file.
// Change Date: 2029-06-12. Change License: MIT.
pragma solidity 0.8.33;

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
import {P256} from "@openzeppelin/contracts/utils/cryptography/P256.sol";
import {EnumerableSet} from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";
import {ERC4337Utils} from "@openzeppelin/contracts/account/utils/draft-ERC4337Utils.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {SHOracle} from "./SHOracle.sol";
import {SHRegistry} from "./SHRegistry.sol";
import {SpendingLimitModule} from "./SpendingLimitModule.sol";
import {WebAuthnNative} from "./utils/WebAuthnNative.sol";
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
 * @dev Session-key auth is built into the account itself: {_authenticate} accepts a UserOp signed by
 *      the owner (a 65-byte ECDSA signature) OR by a PASSKEY the owner has authorized (any other
 *      length: a WebAuthn assertion carrying the passkey's public key). A session key IS a passkey: a
 *      P-256 key held in a phone's or laptop's secure hardware, released only after Face ID, a
 *      fingerprint or a PIN, so the server never holds a key that can sign for the wallet. The owner
 *      authorizes up to {MAX_SESSION_KEYS} at once, one per device, with {addSession} /
 *      {removeSession} / {removeAllSessions}, and {getSessions} answers "which keys does this wallet
 *      trust?". No separate validator module is installed -- SpendingLimitModule is a hook (module
 *      type 4) ONLY, enforcing the USD spending cap on every execution. Raw-signature validation is
 *      left disabled at the base's default, so {_authenticate} is the single authentication path;
 *      see its NatSpec for why it does not delegate to {AccountERC7579-_validateUserOp}.
 * @dev A session key EXPIRES: its deadline in {sessionValidUntil} bounds the op's ERC-4337 validity
 *      window, so the EntryPoint refuses an expired key with `AA22 expired or not due`. Each passkey
 *      op also carries a deadline of its OWN, in its nonce key, and the window is the earlier of the
 *      two -- so an op that was signed but never sent dies within minutes, not when the key does
 *      (see {_authenticatePasskey}). A key is still a bare signer within its window -- no per-key selector scope -- bounded by the
 *      spending cap (see {addSession}) and narrowed optionally by the owner-managed target allowlist
 *      (see {enableAllowList}). Every one of those limits belongs to the WALLET, not to a key: a
 *      second or third key adds a device, never spending power.
 * @dev The USD cap cannot see ETH spent as GAS (the prefund leaves before the hook's preCheck,
 *      refunds land in the EntryPoint deposit after postCheck). Two ceilings bound it instead:
 *      {maxOpGasCost} per UserOp, and {sessionGasBudget} across every UserOp the owner did not sign
 *      in a {SESSION_GAS_WINDOW}. The EntryPoint is a restricted target, so a key cannot withdraw
 *      the deposit either. THREAT_MODEL §3.12.
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
    using EnumerableSet for EnumerableSet.AddressSet;
    using EnumerableSet for EnumerableSet.Bytes32Set;

    /*//////////////////////////////////////////////////////////////
                                    ERRORS
    //////////////////////////////////////////////////////////////*/
    error SessionHandler_InvalidRecipient();
    error SessionHandler_NotEnoughBalance();
    error SessionHandler_ExecutionFailed();
    /// @dev Thrown when addSession is given a public key that is not a point on the P-256 curve: a
    ///      coordinate outside the field, or an (x, y) that does not satisfy the curve equation.
    ///      (0, 0) is one such pair, so it can never be granted here; it is only {initialize}'s way of
    ///      saying "no key". No private key exists for a pair off the curve, so a grant would
    ///      authorize nobody and still take up one of the {MAX_SESSION_KEYS} slots.
    error SessionHandler_InvalidSessionKey();
    /// @dev Thrown when addSession is given a deadline that has already passed (or is zero, which the
    ///      EntryPoint would read as "no expiry").
    error SessionHandler_SessionExpiryInPast(uint48 validUntil);
    /// @dev Thrown when addSession is given a deadline further out than {MAX_SESSION_TTL}.
    error SessionHandler_SessionTtlTooLong(uint48 validUntil, uint48 maxTtl);
    /// @dev Thrown when addSession would authorize a NEW key while {MAX_SESSION_KEYS} keys are held
    ///      and none of them has expired. The owner removes one first ({removeSession}).
    error SessionHandler_TooManySessionKeys(uint256 max);
    /// @dev Thrown when removeSession names a key this wallet does not hold. A typo or a stale id
    ///      must not read as a successful revoke: the caller would believe a lost device was cut off
    ///      when it never was.
    error SessionHandler_UnknownSessionKey(bytes32 keyId);
    /// @dev Thrown when a session-key (non-owner) execution targets the account's own admin surface
    ///      (address(this) or the spending-limit module) or the EntryPoint, which would let a key
    ///      escape the cap -- or, while the allowlist is on, a contract that is not on it.
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
    /// @dev Thrown when the UserOps the owner did not sign would have paid more native coin as gas,
    ///      within one {SESSION_GAS_WINDOW}, than {sessionGasBudget} allows.
    error SessionHandler_SessionGasBudgetExceeded(uint256 spent, uint256 budget);
    /// @dev Thrown on setSessionGasBudget(0), which would stop every UserOp the owner did not sign.
    error SessionHandler_InvalidSessionGasBudget();
    /// @dev Thrown when setSessionGasBudget is given more than {sessionGasBudget} can hold
    ///      (type(uint80).max).
    error SessionHandler_SessionGasBudgetTooHigh(uint256 newBudget, uint256 limit);
    /// @dev Thrown by the constructor when either default gas ceiling is zero.
    error SessionHandler_InvalidGasCeilings();
    /// @dev Thrown when a UserOp's callData calls this account's own {validateUserOp}: the one call
    ///      that would make the account pay a prefund outside validation (THREAT_MODEL §3.12).
    error SessionHandler_SelfValidationCall();
    /// @dev Thrown on enabling an empty allowlist, which would reject every session-key execution.
    error SessionHandler_EmptyAllowlist();
    /// @dev Thrown when address(0) is passed as a session-key allowlist target.
    error SessionHandler_InvalidAllowedTarget();

    /*//////////////////////////////////////////////////////////////
                                    EVENTS
    //////////////////////////////////////////////////////////////*/
    /// @notice Emitted when the owner authorizes a passkey, or renews the deadline of one already
    ///         authorized.
    /// @dev Carries the whole public key, not just its id: the id is a hash, and this event is the one
    ///      place an indexer can learn which (qx, qy) an id stands for without asking the owner.
    /// @param keyId      {sessionKeyId} of the key: what {sessionValidUntil} and {getSessions} use.
    /// @param qx         X coordinate of the passkey's P-256 public key.
    /// @param qy         Y coordinate of the passkey's P-256 public key.
    /// @param validUntil Unix timestamp this key stops being accepted, inclusive of that second.
    event SessionAdded(bytes32 indexed keyId, bytes32 qx, bytes32 qy, uint48 validUntil);
    /// @notice Emitted when a passkey stops being authorized -- revoked by the owner, or swept out by
    ///         {addSession} because it had already expired and its slot was needed. NOT emitted when a
    ///         key merely expires: nothing runs at that moment, the key just stops validating.
    event SessionRemoved(bytes32 indexed keyId);

    /// @notice Emitted when the owner changes the per-UserOp gas-cost ceiling.
    event MaxOpGasCostUpdated(uint256 oldMax, uint256 newMax);
    /// @notice Emitted when the owner changes the per-window gas budget for UserOps it did not sign.
    event SessionGasBudgetUpdated(uint256 oldBudget, uint256 newBudget);
    /// @notice Emitted when the owner enables or disables the session-key target allowlist.
    event SessionAllowlistToggled(bool enabled);
    /// @notice Emitted when a target is added to the session-key allowlist.
    event AllowedTargetAdded(address indexed target);
    /// @notice Emitted when a target is removed from the session-key allowlist.
    event AllowedTargetRemoved(address indexed target);

    /// @notice Emitted when a session-key execution pays the protocol fee. Owner-initiated executions
    ///         pay no fee and emit nothing.
    /// @param treasury The registry-configured recipient at the time of payment.
    /// @param fee      The amount paid in wei: the registry's flat fee at the time of payment.
    event ProtocolFeePaid(address indexed treasury, uint256 fee);

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Default {maxOpGasCost} written by {initialize}, in wei. Set per chain when the
    ///         implementation is built; see {GasCeilings}.
    /// @dev Sized to clear a ~600k-gas swap at 2x a spiking base fee on its chain, so a fee spike
    ///      never rejects a legitimate op. A bound on one op's abuse, not a budget.
    uint256 public immutable DEFAULT_MAX_OP_GAS_COST;

    /// @notice Default {sessionGasBudget} written by {initialize}, in wei. Set per chain when the
    ///         implementation is built; see {GasCeilings}.
    uint256 public immutable DEFAULT_SESSION_GAS_BUDGET;

    /// @notice How long one {sessionGasBudget} lasts before it refills.
    uint256 public constant SESSION_GAS_WINDOW = 1 days;

    /// @notice Longest a session key may be authorized for, from the moment it is granted.
    /// @dev A hard ceiling rather than an owner setting: a settable one would be raised to "forever"
    ///      by the first UI that defaults it, which is the behaviour this expiry exists to remove.
    ///      Renewal is one owner transaction ({addSession} again), so the cost of a short life is
    ///      friction, not lockout.
    uint48 public constant MAX_SESSION_TTL = 90 days;

    /// @notice Most passkeys this wallet may authorize at once.
    /// @dev The user's choice: one passkey per device, up to five devices (a phone, a laptop, a
    ///      tablet, with room for a replacement while the old one is still listed). A hard constant
    ///      for the same reason as {MAX_SESSION_TTL}, and because it bounds the two loops that walk
    ///      the list ({_pruneExpiredSessions}, {removeAllSessions}) and the {getSessions} read, so none
    ///      of them can be made expensive.
    uint256 public constant MAX_SESSION_KEYS = 5;

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
    // variables fill that slot's remaining 11 bytes, and the next two share one slot. Every UserOp
    // reads `_paused` and {maxOpGasCost}; a session-key execution also reads {SH_MODULE} and
    // {sessionAllowlistEnabled} -- two slots in all. `forge inspect SessionHandler storageLayout` shows
    // the assignment. Wallets are non-upgradeable clones, so a layout change only ever applies to a
    // new implementation, never to a wallet already deployed.
    //
    // ONE SLOT NUMBER IS PART OF THE INTERFACE: {sessionValidUntil} sits on slot 9, and app/bundler.py
    // writes a throwaway key's deadline at keccak256(abi.encode(keyId, uint256(9))) in a state
    // override when it simulates a quote. Declare nothing above it, and move it only together with
    // that constant. The order is: slots 7-8 the target allowlist, 9 {sessionValidUntil}, 10-11
    // {_sessionKeys}, 12 the three gas-budget variables.

    /// @notice Whether the session-key target allowlist is being enforced. See {enableAllowList}.
    bool public sessionAllowlistEnabled;
    /// @notice Maximum total ETH (wei) one UserOp may cost this account, however it is paid.
    /// @dev Owner-settable because gas prices differ per chain and over time; a compile-time constant
    ///      would be too tight somewhere (legitimate ops fail in a fee spike) and too loose elsewhere.
    ///      Enforced on the op's own gas fields in {_authenticate} and on the prefund transfer in
    ///      {validateUserOp}. It bounds ONE op; {sessionGasBudget} bounds all of them per window.
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

    /// @dev Targets a session key may call while {sessionAllowlistEnabled} is true. OFF by default.
    ///      Confines a key to a fixed set of venues — mainly to keep it away from protocols where the
    ///      account can take on a LIABILITY, which the balance-diff meter never charges to the cap
    ///      (THREAT_MODEL §3.13). Address-granular, never selector-granular, so the account needs no
    ///      ABI knowledge of what it calls. An address with no code passes whether listed or not; see
    ///      {_requireUnrestrictedTarget}.
    /// @dev A set rather than a bare mapping so {getAllowedTargets} can return the whole list: a
    ///      mapping cannot be enumerated, and rebuilding one from events needs log ranges some RPC
    ///      providers refuse. Membership is still one storage read, so the guard pays nothing extra.
    ///      Two slots where a mapping would take one, which is why {sessionValidUntil} follows on slot 9.
    EnumerableSet.AddressSet private _sessionTargets;

    /// @notice When each authorized passkey stops being accepted, keyed by {sessionKeyId}; 0 for a key
    ///         this wallet does not trust. The owner is always authorized separately, in
    ///         {_authenticate}.
    /// @dev This mapping is the ONLY thing validation reads about a passkey. The UserOp signature
    ///      carries the public key itself (`qx || qy || WebAuthn assertion`, see
    ///      {_authenticatePasskey}), so the account hashes it to an id, makes ONE storage read for
    ///      the deadline, and hands the supplied key straight to the P-256 check: a signature from any
    ///      other key finds a 0 here and stops. Storing the key instead would cost three reads (qx, qy
    ///      and the deadline) and still need a lookup handle in the signature.
    /// @dev Carrying the key also keeps the bundler's job small. app/bundler.py simulates a quote for
    ///      a user who has not signed yet by planting a throwaway key's deadline here with ONE storage
    ///      override, at keccak256(abi.encode(keyId, uint256(9))). With no stored key material there
    ///      is nothing else to fake. See the note on slot numbers above.
    /// @dev Written only together with {_sessionKeys}, so a key is in the list iff its deadline here
    ///      is non-zero. A zero deadline must never describe a live key: the EntryPoint reads zero in
    ///      validation data as "valid forever", which is why {_grantSession} refuses any deadline that
    ///      is not in the future and why {_authenticatePasskey} treats 0 as "not authorized" before
    ///      it builds any validation data. Inclusive: a key is still accepted in the second that
    ///      equals its deadline, matching the EntryPoint's own comparison.
    /// @dev Within its window a key is still a BARE signer -- it may drive ANY execute() call, bounded
    ///      by the SpendingLimitModule spending cap, {_guardSessionExecution}, and the two gas
    ///      ceilings ({maxOpGasCost}, {sessionGasBudget}) -- all of which belong to the wallet, not
    ///      to a key, so five keys spend no more than one.
    mapping(bytes32 keyId => uint48 validUntil) public sessionValidUntil;

    /// @dev The keys this wallet trusts, so {getSessions} can list them. NOT read during validation.
    ///      The owner chose up to {MAX_SESSION_KEYS} keys, one per device, and a mapping alone cannot
    ///      be enumerated: nothing on chain could answer "which keys does this wallet trust?", and
    ///      rebuilding the answer from events needs log ranges some RPC providers refuse. Holds at
    ///      most {MAX_SESSION_KEYS} ids. It may hold keys whose deadline has passed: those are swept
    ///      only when a new key needs their slot ({_pruneExpiredSessions}), and until then
    ///      {getSessions} reports them with their past deadline, which is how a UI shows "expired".
    EnumerableSet.Bytes32Set private _sessionKeys;

    /// @notice Native coin (wei) that UserOps NOT signed by the owner have paid as gas in the current
    ///         {SESSION_GAS_WINDOW}. See {sessionGasBudget}.
    /// @dev These three share slot 12. They come after the session-key storage so that
    ///      {sessionValidUntil} keeps slot 9, which app/bundler.py writes by number. Never above
    ///      {sessionGasBudget}, so a uint128 holds it with room to spare.
    uint128 public sessionGasSpent;
    /// @notice When the current {SESSION_GAS_WINDOW} began, or 0 before the first charge.
    uint48 public sessionGasWindowStart;
    /// @notice The most native coin (wei) UserOps NOT signed by the owner may pay as gas per
    ///         {SESSION_GAS_WINDOW}, all together. Owner-settable; see {setSessionGasBudget}.
    /// @dev The cumulative bound {maxOpGasCost} cannot give: the EntryPoint takes many ops from one
    ///      account in a single bundle, so a per-op ceiling alone lets a stolen key that runs its own
    ///      bundler collect the ceiling once per op until the balance is gone (THREAT_MODEL §3.12).
    uint80 public sessionGasBudget;

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

    /**
     * @notice The starting values of the two gas ceilings for every wallet of this implementation,
     *         in the chain's native coin. Chosen per chain at deploy (HelperConfig), because one
     *         figure cannot suit them all: 0.1 ETH of gas is a fee spike on Ethereum and a thousand
     *         ops on an L2. The owner can change either afterwards.
     * @param maxOpGasCost     Default {maxOpGasCost}: the most one UserOp may cost. Must be > 0.
     * @param sessionGasBudget Default {sessionGasBudget}: the most all UserOps the owner did not sign
     *                         may cost per {SESSION_GAS_WINDOW}. Must be > 0.
     */
    struct GasCeilings {
        uint80 maxOpGasCost;
        uint80 sessionGasBudget;
    }

    /// @param protocol    See {ProtocolAddresses}.
    /// @param gasCeilings See {GasCeilings}.
    constructor(ProtocolAddresses memory protocol, GasCeilings memory gasCeilings) {
        if (gasCeilings.maxOpGasCost == 0 || gasCeilings.sessionGasBudget == 0) {
            revert SessionHandler_InvalidGasCeilings();
        }
        ENTRY_POINT = protocol.entryPoint;
        REPUTATION_REGISTRY = protocol.reputationRegistry;
        IDENTITY_REGISTRY = protocol.identityRegistry;
        REGISTRY = SHRegistry(protocol.registry);
        DEFAULT_MAX_OP_GAS_COST = gasCeilings.maxOpGasCost;
        DEFAULT_SESSION_GAS_BUDGET = gasCeilings.sessionGasBudget;
        _disableInitializers();
    }

    /*//////////////////////////////////////////////////////////////
                                Initialization
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The per-wallet settings {initialize} needs, bundled so the wallet is fully usable after
     *         ONE transaction: ownership, the spending-cap config, and the two owner-only grants that
     *         would otherwise need a follow-up transaction each ({addSession}, {addTrustedSpender}).
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
     * @param sessionKeyX         X coordinate of the first passkey to authorize. With `sessionKeyY`
     *                            both zero, the wallet starts owner-only and no key is granted.
     * @param sessionKeyY         Y coordinate of that passkey's P-256 public key.
     * @param sessionKeyValidUntil Unix timestamp the passkey expires at. Subject to the same rules as
     *                            {addSession}, so it must be in the future and no further out than
     *                            {MAX_SESSION_TTL}. Ignored when the key is (0, 0).
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
        bytes32 sessionKeyX;
        bytes32 sessionKeyY;
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
        // Safe: both defaults arrived in the constructor as uint80s.
        // forge-lint: disable-next-line(unsafe-typecast)
        maxOpGasCost = uint80(DEFAULT_MAX_OP_GAS_COST);
        // forge-lint: disable-next-line(unsafe-typecast)
        sessionGasBudget = uint80(DEFAULT_SESSION_GAS_BUDGET);

        // Install the spending-limit hook. Its onInstall
        // decodes exactly this (dailyLimitUsd, windowDuration, watchedTokens) tuple, so the config
        // must be non-empty and valid: windowDuration > 0, dailyLimitUsd >= 0, and every watched
        // token already priced by the oracle.
        _installModule(
            MODULE_TYPE_HOOK,
            cfg.spendingLimitModule,
            abi.encode(cfg.dailyLimitUsd, cfg.windowDuration, cfg.watchedTokens)
        );

        // Authorize the wallet's first passkey, if one was supplied. Routed through the SAME internal
        // grant as {addSession} -- including the curve and expiry rules -- so a deploy can never seed
        // a key on terms an owner transaction would have refused. (0, 0) is not an error here (it
        // fails the curve check in {addSession}): it means an owner-only wallet, and its deadline is
        // ignored. Any other pair, including one with a single zero coordinate, goes to the grant and
        // is judged there.
        if (cfg.sessionKeyX != bytes32(0) || cfg.sessionKeyY != bytes32(0)) {
            _grantSession(cfg.sessionKeyX, cfg.sessionKeyY, cfg.sessionKeyValidUntil);
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

            _applyFee();
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
                  SESSION TARGET ALLOWLIST (owner-managed)
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Adds `targets` to the allowlist and turns it on, in one transaction.
     * @dev The one-signature way to start enforcing it. Without it an owner needs one transaction to
     *      fill the list and a second to switch it on, since {toggleAllowList} refuses an empty list.
     *      Which venues to pass is the caller's choice: the account never lists one by itself, for the
     *      same reason {initialize} never trusts a router on its own (see the comment there).
     *      Each entry follows {addAllowedTarget}'s rules. `targets` may be empty when the list already
     *      holds entries, which makes this a plain "turn on".
     * @param targets The contracts session keys may call. Each must not be address(0).
     */
    function enableAllowList(address[] calldata targets) external onlyOwner {
        _addAllowedTargets(targets);
        _setAllowlistEnabled(true);
    }

    /**
     * @notice Turns the session-key target allowlist on or off.
     * @dev Refuses to enable an empty allowlist (it would reject every call to a contract). Disabling
     *      is always allowed, so the owner can never be locked out of restoring service.
     * @param enabled True to enforce the allowlist.
     */
    function toggleAllowList(bool enabled) external onlyOwner {
        _setAllowlistEnabled(enabled);
    }

    /**
     * @notice Permits session keys to call `target` while the allowlist is enforced. No-op if
     *         already allowed.
     * @param target The contract a session key may call. Must not be address(0).
     */
    function addAllowedTarget(address target) external onlyOwner {
        _addAllowedTarget(target);
    }

    /**
     * @notice Adds several targets in one transaction (a real allowlist needs a router plus tokens).
     * @param targets The contracts session keys may call. Each must not be address(0).
     */
    function addAllowedTargets(address[] calldata targets) external onlyOwner {
        _addAllowedTargets(targets);
    }

    /**
     * @notice Stops session keys from calling `target`. No-op if it was not allowed.
     * @dev Emptying the list does NOT auto-disable enforcement: it fails closed rather than silently
     *      reopening every target. Disable deliberately with {toggleAllowList}.
     * @param target The contract to remove.
     */
    function removeAllowedTarget(address target) external onlyOwner {
        if (_sessionTargets.remove(target)) emit AllowedTargetRemoved(target);
    }

    /// @notice Whether `target` is on the allowlist.
    /// @dev Membership only. While the list is on, an address with no code is callable without being
    ///      listed, so `false` here does not by itself mean a call would be refused.
    function sessionTargetAllowlist(address target) external view returns (bool) {
        return _sessionTargets.contains(target);
    }

    /// @notice How many targets are on the allowlist.
    function allowedTargetCount() external view returns (uint256) {
        return _sessionTargets.length();
    }

    /// @notice Every target on the allowlist, in no particular order.
    /// @dev Copies the whole list into memory, so it is meant for off-chain reads; a transaction calling
    ///      it pays for every entry. A removal moves the last entry into the freed position, so the order
    ///      is not stable either.
    function getAllowedTargets() external view returns (address[] memory) {
        return _sessionTargets.values();
    }

    /// @dev Shared by every path that lists a target, so all of them refuse address(0) and treat a
    ///      target already listed as a silent no-op.
    function _addAllowedTarget(address target) internal {
        if (target == address(0)) revert SessionHandler_InvalidAllowedTarget();
        if (_sessionTargets.add(target)) emit AllowedTargetAdded(target);
    }

    function _addAllowedTargets(address[] calldata targets) internal {
        for (uint256 i = 0; i < targets.length; i++) {
            _addAllowedTarget(targets[i]);
        }
    }

    /// @dev Shared by {toggleAllowList} and {enableAllowList}, so neither can enforce an empty list.
    function _setAllowlistEnabled(bool enabled) internal {
        if (enabled && _sessionTargets.length() == 0) revert SessionHandler_EmptyAllowlist();
        sessionAllowlistEnabled = enabled;
        emit SessionAllowlistToggled(enabled);
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

    /**
     * @notice Sets the most native coin UserOps not signed by the owner may pay as gas per
     *         {SESSION_GAS_WINDOW}, all together. See {sessionGasBudget}.
     * @dev Takes effect at once, and does not reset what the current window has already spent:
     *      lowering it below that stops the keys' ops until the window refills, raising it lets them
     *      continue. Reverts on 0 (use {removeAllSessions} or {pause} to stop the keys) and above
     *      type(uint80).max, which the storage slot cannot hold.
     * @param newBudget New budget in wei. Must be > 0 and <= type(uint80).max.
     */
    function setSessionGasBudget(uint256 newBudget) external onlyOwner {
        if (newBudget == 0) revert SessionHandler_InvalidSessionGasBudget();
        if (newBudget > type(uint80).max) {
            revert SessionHandler_SessionGasBudgetTooHigh(newBudget, type(uint80).max);
        }
        uint256 oldBudget = sessionGasBudget;
        // Safe: bounded by type(uint80).max above.
        // forge-lint: disable-next-line(unsafe-typecast)
        sessionGasBudget = uint80(newBudget);
        emit SessionGasBudgetUpdated(oldBudget, newBudget);
    }

    /// @notice What UserOps not signed by the owner may still pay as gas before the budget refills:
    ///         the whole {sessionGasBudget} once the current window has ended.
    function sessionGasRemaining() external view returns (uint256) {
        if (block.timestamp >= uint256(sessionGasWindowStart) + SESSION_GAS_WINDOW) return sessionGasBudget;
        uint256 spent = sessionGasSpent;
        return spent >= sessionGasBudget ? 0 : sessionGasBudget - spent;
    }

    /*//////////////////////////////////////////////////////////////
                       SESSION-KEY AUTH (owner-managed)
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The id this wallet files a passkey under: the hash of its public key.
     * @dev What {sessionValidUntil}, {getSessions}, {removeSession} and the events all use. Derived
     *      from the key, never assigned, so the app, the contract and an indexer compute the same
     *      id for the same key without asking each other.
     * @param qx X coordinate of the passkey's P-256 public key.
     * @param qy Y coordinate of the passkey's P-256 public key.
     */
    function sessionKeyId(bytes32 qx, bytes32 qy) public pure returns (bytes32) {
        return keccak256(abi.encode(qx, qy));
    }

    /**
     * @notice Authorizes the passkey with public key (`qx`, `qy`) until `validUntil`. A key this
     *         wallet already holds is RENEWED instead: its deadline is overwritten (later or
     *         earlier) and no extra slot is used.
     * @dev SECURITY: a session key is a BARE signer -- until it expires it can drive ANY execute()
     *      call to external targets, gated by the SpendingLimitModule USD spending cap and by
     *      {_guardSessionExecution} (which blocks it from the account's own admin surface, so a key
     *      can never authorize another key). It is NOT scoped to particular external targets or
     *      selectors; scoped keys (Smart Sessions / ERC-7715) remain a deliberate future step. Grant
     *      a key only to a device the owner holds, and prefer a short `validUntil`.
     * @dev Up to {MAX_SESSION_KEYS} keys at once, one per device. The user chose several keys over
     *      one so a phone and a laptop can each confirm without sharing a secret, and so a lost
     *      device is revoked on its own ({removeSession}) without disturbing the others. They are
     *      listed by {getSessions}, which is what answers "which keys does this wallet trust?".
     *      The spending cap, the gas budget and the allowlist are the WALLET's: a second key adds a
     *      device, never spending power.
     * @dev A NEW key when {MAX_SESSION_KEYS} are held first sweeps every key whose deadline has
     *      passed, emitting {SessionRemoved} for each: an expired key already stopped working, so
     *      making the owner revoke it by hand would be a transaction spent on nothing. Only if the
     *      list is still full does this revert with {SessionHandler_TooManySessionKeys}.
     * @param qx         X coordinate of the passkey's P-256 public key.
     * @param qy         Y coordinate. (qx, qy) must be a point on the curve, or the call reverts with
     *                   {SessionHandler_InvalidSessionKey}.
     * @param validUntil Unix timestamp the key expires at. Must be in the future and no further out
     *                   than {MAX_SESSION_TTL}.
     */
    function addSession(bytes32 qx, bytes32 qy, uint48 validUntil) external onlyOwner {
        _grantSession(qx, qy, validUntil);
    }

    /**
     * @notice Revokes one passkey, by the id {sessionKeyId} gives it. Reverts if this wallet does
     *         not hold that key.
     * @dev Takes the id rather than the public key because the id is what {getSessions} and the
     *      events report. Reverting on an unknown id (instead of returning quietly) is deliberate: a
     *      typo or a stale id must not read as a successful revoke when the lost device is still
     *      trusted. An expired key that is still listed is removable like any other.
     * @param keyId {sessionKeyId} of the key to remove.
     */
    function removeSession(bytes32 keyId) external onlyOwner {
        if (!_sessionKeys.remove(keyId)) revert SessionHandler_UnknownSessionKey(keyId);
        delete sessionValidUntil[keyId];
        emit SessionRemoved(keyId);
    }

    /**
     * @notice Revokes every passkey in one transaction -- the panic button for "I do not trust any
     *         device right now". A no-op, emitting nothing, when the wallet holds none.
     * @dev Bounded by {MAX_SESSION_KEYS}. Walks from the end of the list so every removal pops the
     *      last entry and no other entry is moved.
     */
    function removeAllSessions() external onlyOwner {
        for (uint256 i = _sessionKeys.length(); i > 0; --i) {
            bytes32 keyId = _sessionKeys.at(i - 1);
            _sessionKeys.remove(keyId);
            delete sessionValidUntil[keyId];
            emit SessionRemoved(keyId);
        }
    }

    /**
     * @notice Every passkey this wallet holds, with its deadline: the answer to "which keys does
     *         this wallet trust?".
     * @dev Includes keys whose deadline has passed but that have not been swept yet (see
     *      {addSession}); compare each deadline with the clock, or ask {isSessionActive}, to tell
     *      them apart. At most {MAX_SESSION_KEYS} entries, in no particular order (a removal moves
     *      the last entry into the freed position). Meant for off-chain reads.
     * @return keyIds      {sessionKeyId} of each held key.
     * @return validUntils Each key's deadline, in the same order as `keyIds`.
     */
    function getSessions() external view returns (bytes32[] memory keyIds, uint48[] memory validUntils) {
        keyIds = _sessionKeys.values();
        validUntils = new uint48[](keyIds.length);
        for (uint256 i = 0; i < keyIds.length; i++) {
            validUntils[i] = sessionValidUntil[keyIds[i]];
        }
    }

    /**
     * @notice Whether this wallet holds the key `keyId` AND it has not expired.
     * @dev The comparison is `<=`, matching the EntryPoint's own: an op is still valid in the second
     *      that equals the deadline (EntryPoint treats a UserOp as out of range only once
     *      `block.timestamp > validUntil`). A view that disagreed with the chain, even by a second
     *      and even conservatively, would be a debugging trap. Callers wanting a safety margin
     *      before starting a transaction should compare {sessionValidUntil} themselves. A key that
     *      is not held reads 0 there, so it is never active.
     * @dev Reading block.timestamp is fine here: this is an ordinary view, never reached during
     *      ERC-4337 validation.
     * @param keyId {sessionKeyId} of the key.
     */
    function isSessionActive(bytes32 keyId) public view returns (bool) {
        uint256 validUntil = sessionValidUntil[keyId];
        return validUntil != 0 && block.timestamp <= validUntil;
    }

    /**
     * @dev The single path that authorizes a session key, shared by {addSession} and {initialize} so
     *      a deploy cannot seed a key on terms an owner transaction would refuse.
     * @dev The curve check is not cosmetic. A pair that is not a point on P-256 has no private key,
     *      so granting it would authorize nobody while using up one of the five slots; the same
     *      check also stops a typo'd or truncated key from being stored as if it were the user's.
     * @dev A deadline of zero is caught by the past-deadline check, which matters more than it looks:
     *      the EntryPoint reads a zero deadline in validation data as "valid forever".
     * @dev The key joins {_sessionKeys} and gets its deadline in the same call, so a key is listed
     *      iff its deadline is non-zero. A key already listed is renewed in place and never needs a
     *      free slot, so an owner with five devices can still extend any of them.
     */
    function _grantSession(bytes32 qx, bytes32 qy, uint48 validUntil) internal {
        if (!P256.isValidPublicKey(qx, qy)) revert SessionHandler_InvalidSessionKey();
        if (validUntil <= block.timestamp) revert SessionHandler_SessionExpiryInPast(validUntil);
        if (validUntil > block.timestamp + MAX_SESSION_TTL) {
            revert SessionHandler_SessionTtlTooLong(validUntil, MAX_SESSION_TTL);
        }

        bytes32 keyId = sessionKeyId(qx, qy);
        if (!_sessionKeys.contains(keyId)) {
            // A new key needs a free slot. Sweep the dead ones before refusing: they are the owner's
            // clutter, not trusted keys.
            if (_sessionKeys.length() >= MAX_SESSION_KEYS) {
                _pruneExpiredSessions();
                if (_sessionKeys.length() >= MAX_SESSION_KEYS) {
                    revert SessionHandler_TooManySessionKeys(MAX_SESSION_KEYS);
                }
            }
            _sessionKeys.add(keyId);
        }

        sessionValidUntil[keyId] = validUntil;
        emit SessionAdded(keyId, qx, qy, validUntil);
    }

    /**
     * @dev Removes every listed key whose deadline has passed, emitting {SessionRemoved} for each.
     *      A key is expired once `block.timestamp > validUntil`, the same comparison the EntryPoint
     *      uses, so a key still accepted in its deadline second is never swept.
     * @dev At most {MAX_SESSION_KEYS} iterations. Walks from the end: removing an entry moves the
     *      LAST entry into its place, and every entry after the current one has already been
     *      judged, so none is skipped or judged twice.
     */
    function _pruneExpiredSessions() internal {
        for (uint256 i = _sessionKeys.length(); i > 0; --i) {
            bytes32 keyId = _sessionKeys.at(i - 1);
            if (block.timestamp > sessionValidUntil[keyId]) {
                _sessionKeys.remove(keyId);
                delete sessionValidUntil[keyId];
                emit SessionRemoved(keyId);
            }
        }
    }

    /**
     * @notice ERC-4337 entry point for validating a UserOp against this account.
     * @dev Written out rather than calling {Account-validateUserOp} so the signer is recovered ONCE
     *      and both answers -- the validation data and whether the owner signed -- come from that
     *      one recovery. In order:
     *        1. {_authenticate}: the op's own gas price against {maxOpGasCost}, and who signed it.
     *        2. The prefund itself against {maxOpGasCost}. In validation it can't exceed the op's
     *           price, which step 1 bounds; kept so the transfer has a bound of its own whatever the
     *           EntryPoint asks for.
     *        3. Unless the owner signed, the prefund is charged to {sessionGasBudget}. That is the
     *           bound across ops: the EntryPoint takes many from one account in a single bundle,
     *           and a stolen key that runs its own bundler is paid every one of them.
     *        4. The prefund is paid -- after the charge, so the state change comes before the call.
     *      {whenNotPaused} makes a paused wallet fail here and pay nothing, rather than pay a prefund
     *      and then revert in `execute`.
     * @dev Step 3 charges the TOP-UP (`missingAccountFunds`), not the op's maximum price, so an
     *      ordinary op uses up about what it really cost: refunds of unused gas stay in the deposit
     *      and shrink the next op's top-up. The deposit can be spent without a charge, but it only
     *      ever holds what charged top-ups (or the owner, depositing directly) put there -- which is
     *      why {_authenticate} refuses an op whose callData calls this function, the one way a key
     *      could otherwise have the account pay a prefund outside validation.
     */
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash, uint256 missingAccountFunds)
        public
        override
        onlyEntryPoint
        whenNotPaused
        returns (uint256 validationData)
    {
        bool byOwner;
        (validationData, byOwner) = _authenticate(userOp, userOpHash, userOp.signature);
        if (missingAccountFunds > maxOpGasCost) {
            revert SessionHandler_PrefundTooHigh(missingAccountFunds, maxOpGasCost);
        }
        if (!byOwner) _chargeSessionGas(missingAccountFunds);
        _payPrefund(missingAccountFunds);
    }

    /**
     * @dev Adds `amount` to what this {SESSION_GAS_WINDOW} has spent, starting a new window when the
     *      last one has ended, and reverts past {sessionGasBudget}. A zero amount (the deposit covered
     *      the op) writes nothing.
     * @dev Reads block.timestamp during validation, which ERC-7562's bundler rules ban: a public
     *      bundler would refuse this account's session-key ops. Every op here goes through the
     *      protocol's own bundler (app/bundler.py), which doesn't apply those rules, so it holds;
     *      moving to public bundlers would need the window carried another way.
     */
    function _chargeSessionGas(uint256 amount) internal {
        if (amount == 0) return;
        uint256 windowStart = sessionGasWindowStart;
        uint256 spent = sessionGasSpent;
        if (block.timestamp >= windowStart + SESSION_GAS_WINDOW) {
            windowStart = block.timestamp;
            spent = 0;
        }
        spent += amount;
        uint256 budget = sessionGasBudget;
        if (spent > budget) revert SessionHandler_SessionGasBudgetExceeded(spent, budget);
        // Safe: spent <= budget <= type(uint80).max, and the timestamp fits a uint48 for millions of
        // years.
        // forge-lint: disable-next-line(unsafe-typecast)
        sessionGasSpent = uint128(spent);
        // forge-lint: disable-next-line(unsafe-typecast)
        sessionGasWindowStart = uint48(windowStart);
    }

    /// @dev Kept for anything in the base contracts that asks: the same answer {validateUserOp} gets.
    function _validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash, bytes calldata signature)
        internal
        view
        override
        returns (uint256 validationData)
    {
        (validationData,) = _authenticate(userOp, userOpHash, signature);
    }

    /**
     * @dev Who signed the op, and its ERC-4337 validation data.
     * @dev Refuses any op whose callData calls {validateUserOp} itself. The EntryPoint forwards an
     *      op's callData to the account in the EXECUTION phase with msg.sender == EntryPoint, so such
     *      an op would reach validateUserOp a second time, with a struct and a `missingAccountFunds`
     *      of its own choosing -- and could replay an old owner signature to skip the gas budget.
     *      No legitimate op does this.
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
     *      (SpendingLimitModule is a hook ONLY), so that branch is dead, and taking it would add a
     *      hop just to learn who signed. Deciding here is also what lets an expired session key be
     *      reported as `AA22 expired or not due` rather than the misleading `AA24 signature error`.
     *      DELIBERATE CONSEQUENCE: a validator module installed by the owner is ignored for UserOp
     *      validation, though {AccountERC7579-isValidSignature} would still consult it. Installing one
     *      is an owner-key action (THREAT_MODEL §3.9), and ignoring it here fails towards this
     *      account's own auth rather than towards a module's.
     * @dev The two kinds of signer are told apart by the signature's LENGTH. Exactly 65 bytes is the
     *      owner: a plain ECDSA signature, unchanged. Anything else is a passkey (see
     *      {_authenticatePasskey}). The encodings cannot be confused -- the shortest passkey
     *      signature is 256 bytes -- so no type byte is needed, and every tool that already signs for
     *      the owner keeps working untouched. It also means a 64-byte "compact" ECDSA signature, or
     *      any truncated owner signature, is judged as a passkey and fails there: the owner has
     *      exactly one encoding.
     * @dev The owner signs the EIP-191 envelope of {Account-_signableUserOpHash} (toEthSignedMessageHash /
     *      eth_account.encode_defunct), so it is re-wrapped before recovery. A passkey signs the raw
     *      32 bytes as its WebAuthn challenge: WebAuthn already wraps the challenge in its own signed
     *      envelope (a clientDataJSON carrying a type and an origin), so another would add nothing.
     * @dev A BAD SIGNATURE NEVER REVERTS HERE, for either kind: a signature that is malformed, from
     *      the wrong key or simply wrong returns SIG_VALIDATION_FAILED, which is what the EntryPoint
     *      requires of an account. A revert would fail the whole bundle (FailedOp) instead of just
     *      this op.
     * @dev A passkey op's deadline -- the earlier of the key's and the op's own -- is RETURNED as the
     *      op's validity window, not compared here: the EntryPoint does that comparison inside
     *      handleOps. A zero deadline would read as "valid forever" there, which is why
     *      {_grantSession} refuses one and why {_authenticatePasskey} treats zero, from either
     *      source, as "not authorized".
     * @return validationData The ERC-4337 validation data for the EntryPoint.
     * @return byOwner        Whether the owner signed: true only for a valid owner signature.
     */
    function _authenticate(PackedUserOperation calldata userOp, bytes32 userOpHash, bytes calldata signature)
        internal
        view
        returns (uint256 validationData, bool byOwner)
    {
        if (bytes4(userOp.callData) == this.validateUserOp.selector) {
            revert SessionHandler_SelfValidationCall();
        }

        uint256 verificationGasLimit = uint256(userOp.accountGasLimits) >> 128;
        uint256 callGasLimit = uint128(uint256(userOp.accountGasLimits));
        uint256 maxFeePerGas = uint128(uint256(userOp.gasFees));
        // Any op extreme enough to overflow this is rejected by the checked-arithmetic revert, which
        // is the same outcome as failing the comparison below.
        uint256 cost = (verificationGasLimit + callGasLimit + userOp.preVerificationGas) * maxFeePerGas;
        if (cost > maxOpGasCost) revert SessionHandler_OpGasCostTooHigh(cost, maxOpGasCost);

        bytes32 hash = _signableUserOpHash(userOp, userOpHash);

        if (signature.length == 65) {
            (address signer, ECDSA.RecoverError err,) =
                ECDSA.tryRecoverCalldata(MessageHashUtils.toEthSignedMessageHash(hash), signature);
            // The owner is authorized unconditionally and carries no validity window. Any other
            // 65-byte signer is nobody: a session key is a passkey now, never an ECDSA address.
            if (err == ECDSA.RecoverError.NoError && signer == owner()) {
                return (ERC4337Utils.SIG_VALIDATION_SUCCESS, true);
            }
            return (ERC4337Utils.SIG_VALIDATION_FAILED, false);
        }

        return (_authenticatePasskey(hash, userOp.nonce, signature), false);
    }

    /**
     * @dev Validation data for a passkey-signed op. `signature` is `qx || qy || assertion`:
     *        bytes 0..31   qx, X coordinate of the passkey's public key
     *        bytes 32..63  qy, its Y coordinate
     *        bytes 64..    the WebAuthn assertion, ABI-encoded as six values in a row --
     *                      abi.encode(r, s, challengeIndex, typeIndex, authenticatorData,
     *                      clientDataJSON) -- which is the layout {WebAuthnNative-tryDecodeAuth}
     *                      reads. It is NOT `abi.encode(auth)` of a struct value, which puts one
     *                      extra offset word in front and fails to decode.
     *      In order: the key must be one this wallet holds ({sessionValidUntil} non-zero); the
     *      assertion must decode; and {WebAuthnNative-verify} must accept it with `hash` as the
     *      challenge and user verification REQUIRED -- a fingerprint, Face ID or PIN, not just a
     *      touch. The earlier of the key's deadline and the op's own then rides back as the op's
     *      validity window.
     * @dev The op's own deadline is the top 48 bits of its nonce (the top of the EntryPoint nonce
     *      KEY): `nonce = (opDeadline << 208) | (lane << 64) | sequence`. The app sends every passkey
     *      op on a fresh nonce key with a random 144-bit lane, so ops never queue behind one another
     *      -- but nothing else ever uses such a lane, so without a deadline of its own an op that
     *      was signed and then abandoned would stay sendable until the KEY expired, up to
     *      {MAX_SESSION_TTL} later. With it, the op dies minutes after its confirmation link does.
     *      The nonce is inside the op hash the passkey signs, so nobody who relays the op can move
     *      its deadline. A zero deadline is refused, never read as "no deadline": every passkey op
     *      must carry one. It is checked first because it costs nothing. The owner's ops ignore it.
     * @dev Why the signature carries the public key: the account can then derive the key's id by
     *      hashing and make ONE storage read (the deadline), where storing the key would take three
     *      (qx, qy and the deadline) plus a lookup handle in the signature. It is also what lets the
     *      bundler plant a throwaway key with a single storage override (see {sessionValidUntil}).
     *      Carrying the key cannot be abused: a key the wallet does not hold finds a zero deadline
     *      and stops, and a held key is only accepted with a signature that key really made.
     * @dev What is NOT checked here is the origin and relying-party id inside the assertion. A
     *      contract cannot know which website the user meant, and the browser and authenticator
     *      already refuse to use a credential for any other origin, so those checks belong to the
     *      server that relays the assertion. On chain, three things bind a signature to THIS op: the
     *      key, user verification, and the challenge -- which is the op hash, so it covers the wallet,
     *      the chain, the EntryPoint, the nonce and the calldata. An assertion cannot be replayed on
     *      another op, wallet or chain.
     * @dev OpenZeppelin's {WebAuthn-verify} can say "no", but two of its steps can still revert or
     *      run out of gas on a hostile signature: it adds `challengeIndex` to a length with checked
     *      arithmetic, and it reads 32 bytes at `typeIndex` into memory before comparing. An index
     *      near 2^256 would panic, or ask for memory no gas limit could pay for, inside validation.
     *      Both indices are therefore bounds-checked against the JSON first, which changes no
     *      verdict: an index past the end could never have matched.
     * @dev The shortest blob that can hold a signature is 64 bytes of key plus the 192 bytes of head
     *      that {WebAuthnNative-tryDecodeAuth} needs. The explicit length check also keeps the
     *      slices below from reverting on a short signature.
     */
    function _authenticatePasskey(bytes32 hash, uint256 nonce, bytes calldata signature)
        internal
        view
        returns (uint256)
    {
        uint48 opDeadline = uint48(nonce >> 208);
        if (opDeadline == 0 || signature.length < 64 + 0xC0) return ERC4337Utils.SIG_VALIDATION_FAILED;

        bytes32 qx = bytes32(signature[0x00:0x20]);
        bytes32 qy = bytes32(signature[0x20:0x40]);
        uint48 validUntil = sessionValidUntil[sessionKeyId(qx, qy)];
        if (validUntil == 0) return ERC4337Utils.SIG_VALIDATION_FAILED;

        (bool decoded, WebAuthnNative.WebAuthnAuth calldata auth) = WebAuthnNative.tryDecodeAuth(signature[0x40:]);
        if (!decoded) return ERC4337Utils.SIG_VALIDATION_FAILED;

        uint256 clientDataLength = bytes(auth.clientDataJSON).length;
        if (auth.challengeIndex >= clientDataLength || auth.typeIndex >= clientDataLength) {
            return ERC4337Utils.SIG_VALIDATION_FAILED;
        }

        if (!WebAuthnNative.verify(abi.encodePacked(hash), auth, qx, qy, true)) {
            return ERC4337Utils.SIG_VALIDATION_FAILED;
        }
        return ERC4337Utils.packValidationData(true, 0, opDeadline < validUntil ? opDeadline : validUntil);
    }

    /**
     * @dev Blocks a non-owner execution — a session key, an installed executor, or a self-call — from
     *      reaching the account's own admin surface. Such callers may act on external protocols under
     *      the USD cap, but must never be able to reconfigure or remove the cap itself, nor reach
     *      value the cap cannot see:
     *        - single / batch: reverts if any sub-call targets a restricted address or, while the
     *          allowlist is on, a contract not on it (see {_requireUnrestrictedTarget});
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
     *      module, the EntryPoint), plus, while the allowlist is on, any CONTRACT not on it. The
     *      EntryPoint is on the denylist because `withdrawTo` moves the account's 4337 deposit without
     *      changing `account.balance`, so the hook would meter a $0 spend. See {_guardSessionExecution}.
     * @dev An address with no code passes the allowlist even when it is not listed. A call to it only
     *      moves native value, which the hook meters like any other outflow; no code runs, so nothing
     *      can record a debt against the account -- the thing the allowlist exists to stop
     *      (THREAT_MODEL §3.13). Without this, every contact sent ETH would need an entry of its own.
     *      Any code at all counts as a contract: an EIP-7702 delegated EOA (its 23-byte designator) and
     *      Arbitrum's system contracts (a one-byte `0xfe` placeholder) are both checked against the
     *      list. Plain precompiles hold no code and pass, but they only compute.
     * @dev The code is read BEFORE the list. EXTCODESIZE warms the address, so the call that follows
     *      costs ~2,500 gas less and the read nets ~100 gas; a plain address then skips the list's
     *      cold storage read entirely.
     * @dev KNOWN EDGE: {_guardSessionExecution} checks every target of a batch before any of them runs,
     *      so an address that is empty at the check but gets code from an EARLIER call in the same
     *      batch (a listed factory deploying to it) is called without being listed. Harmless for what
     *      the list guards: the new code acts as itself, so nothing it does is recorded against this
     *      account. Closing it would mean re-checking inside OpenZeppelin's execution loop.
     */
    function _requireUnrestrictedTarget(address target) internal view {
        if (target == address(this) || target == address(SH_MODULE) || target == ENTRY_POINT) {
            revert SessionHandler_SessionRestrictedTarget(target);
        }
        if (sessionAllowlistEnabled && target.code.length != 0 && !_sessionTargets.contains(target)) {
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
    function _applyFee() internal {
        address treasury = REGISTRY.treasury();
        uint256 fee = REGISTRY.getFee();
        if (address(this).balance < fee) revert SessionHandler_NotEnoughBalance();
        // The recipient is the registry's treasury, set by the protocol's admin, never by the caller.
        // slither-disable-next-line arbitrary-send-eth
        (bool success,) = treasury.call{value: fee}("");
        if (!success) revert SessionHandler_TransferFailed();
        emit ProtocolFeePaid(treasury, fee);
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
     *      {_applyFee} is unconditional here. Guard runs BEFORE the fee so a rejected call is never
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

        _applyFee();

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

    /// @notice Returns the agent's ERC-8004 on-chain   identity.
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

    function _msgData() internal pure override(Context, ContextUpgradeable) returns (bytes calldata) {
        return msg.data;
    }

    function _contextSuffixLength() internal pure override(Context, ContextUpgradeable) returns (uint256) {
        return 0;
    }
}
