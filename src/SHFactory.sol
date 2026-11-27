// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SessionHandler} from "./SessionHandler.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {SHRegistry} from "./SHRegistry.sol";

contract SHFactory is Ownable, Pausable {
    error SHFactory_FundTransferFailed();
    /// @dev Thrown by deployWallet when no SpendingLimitModule has been configured yet.
    error SHFactory_SpendingLimitModuleNotSet();
    /// @dev Thrown by deployWallet when no RecurringPaymentModule has been configured yet.
    error SHFactory_RecurringPaymentModuleNotSet();
    /// @dev Thrown by deployWallet when no AgentModule has been configured yet.
    error SHFactory_AgentModuleNotSet();

    /// @notice The SHRegistry this factory's wallets are wired to. Its EntryPoint is baked into
    ///         {IMPLEMENTATION} at construction; its modules are read on every {deployWallet}.
    /// @dev Immutable: a factory is bound to one registry for life. Point wallets at a different
    ///      registry by deploying a new factory and recording it with SHRegistry.setFactory.
    SHRegistry public immutable REGISTRY;

    /// @notice The SessionHandler every wallet is an EIP-1167 clone of.
    address public immutable IMPLEMENTATION;

    /// @notice How many wallets an address has deployed. Doubles as that owner's next CREATE2 salt
    ///         nonce, and as a "does this user already have a wallet?" view a front end can read
    ///         BEFORE asking anyone to sign — cheaper than a reverted transaction.
    /// @dev The factory keeps no global wallet count or id list: nothing on chain needs one, and
    ///      both can be rebuilt from {WalletDeployed} logs.
    /// @dev Per-OWNER rather than global on purpose. A global counter would make a predicted address
    ///      depend on how many wallets everyone else had deployed, so any other user's deploy
    ///      landing first would move it — and {predictWalletAddress} exists
    ///      precisely so the address can be shown, and the user's funding sent to it, before the
    ///      deploy is signed. Keyed by owner, only that owner's own deploys move it.
    mapping(address owner => uint256) public deployCount;

    event WalletDeployed(address indexed walletAddress, address indexed owner);

    /**
     * @notice Deploys the factory and the SessionHandler implementation it clones from.
     * @dev The EntryPoint is read from the registry ONCE, here, and becomes an immutable of the
     *      implementation (see {SessionHandler-ProtocolAddresses}). It is immutable on the registry
     *      too, so there is no later change for wallets to miss. The modules are the exception: the
     *      operator can change them, so {deployWallet} reads them fresh for every wallet. The registry
     *      must therefore already be deployed.
     * @param owner       Address that will own this factory — the SHTreasury, the protocol's
     *                    single admin root. Supplied rather than taken from msg.sender so no
     *                    follow-up transferOwnership is needed; the operator reaches
     *                    {pause}/{unpause} through SHTreasury's owner-only passthroughs.
     * @param _registry   The SHRegistry this factory and its wallets read configuration from.
     * @param gasCeilings This chain's starting gas ceilings for every wallet: see
     *                    {SessionHandler-GasCeilings}. Both must be > 0.
     */
    constructor(address owner, address _registry, SessionHandler.GasCeilings memory gasCeilings) Ownable(owner) {
        REGISTRY = SHRegistry(_registry);
        IMPLEMENTATION = address(
            new SessionHandler(
                SessionHandler.ProtocolAddresses({
                    entryPoint: SHRegistry(_registry).ENTRY_POINT(),
                    registry: _registry
                }),
                gasCeilings
            )
        );
    }

    /// @notice Pauses the factory, blocking {deployWallet}. Existing wallets are unaffected. Only
    ///         callable by the owner.
    function pause() public onlyOwner {
        _pause();
    }

    /// @notice Unpauses the factory, re-enabling {deployWallet}. Only callable by the owner.
    function unpause() public onlyOwner {
        _unpause();
    }

    /**
     * @notice Derives the CREATE2 salt for a wallet. Binding the OWNER into the salt is what makes
     *         {predictWalletAddress} safe to publish: `nonce` is only ever that owner's own deploy
     *         count, so no other caller can reach the same salt and squat a predicted address.
     * @param owner The address that will own the wallet (deployWallet's msg.sender).
     * @param nonce That owner's {deployCount} at the time of the deploy.
     */
    function _salt(address owner, uint256 nonce) private pure returns (bytes32) {
        return keccak256(abi.encode(owner, nonce));
    }

    /**
     * @notice Returns the address `owner`'s NEXT {deployWallet} call will produce, before it is
     *         deployed. Lets a front end show the user their wallet address in the same screen that
     *         asks them to sign. (It no longer feeds a session key: a passkey is made on the user's
     *         device before the deploy and does not depend on the wallet's address.)
     * @dev Two things move this value, and nothing else can: `owner` deploying again (their
     *      {deployCount} advances), and SHFactory being redeployed (the deployer is baked into a
     *      CREATE2 address, so a new factory invalidates every previously shown address).
     */
    function predictWalletAddress(address owner) external view returns (address) {
        return Clones.predictDeterministicAddress(IMPLEMENTATION, _salt(owner, deployCount[owner]));
    }

    /// @notice Deploys a SessionHandler (ERC-7579 account) with spendingLimitModule installed as a
    ///         hook, recurringPaymentModule as an executor and agentModule as a validator, seeded with
    ///         the caller's spending-cap config and first passkey. Reverts if any module isn't set.
    /// @dev Deployed with CREATE2 at a salt derived from (msg.sender, that owner's {deployCount}),
    ///      so the address is known before the transaction is signed -- see {predictWalletAddress}.
    ///      The counter is consumed here, so calling this twice yields two distinct wallets and a
    ///      salt can never repeat; there is no collision case to guard against, and no external
    ///      party can occupy the address first because a CREATE2 address is bound to its deployer.
    /// @dev The passkey and `trustedSpenders` are applied inside {SessionHandler-initialize}, so a
    ///      wallet is usable after this ONE transaction. Otherwise they would be two follow-up
    ///      owner-signed calls to addSession/addTrustedSpender. The key goes through the same grant as
    ///      {SessionHandler-addSession}: it must be a point on the P-256 curve and its deadline must
    ///      be in the future and within {SessionHandler-MAX_SESSION_TTL}, or the whole deploy reverts.
    ///      A user can hold up to {SessionHandler-MAX_SESSION_KEYS} passkeys, one per device; this
    ///      seeds the first and the owner adds the rest with {SessionHandler-addSession}.
    /// @dev Deliberately does NOT refuse an owner who already has a wallet -- one address may own
    ///      several. A caller wanting one-wallet-per-user enforces it by reading {deployCount}
    ///      before asking the user to sign, rather than by spending gas on a revert.
    /// @dev That check only means "does this USER have a wallet" when the user is `msg.sender` --
    ///      i.e. when the end user signs their own deploy, which is the flow this is built for. A
    ///      caller that deploys on users' behalf from one shared EOA (as app/deploy_wallet.py does)
    ///      sees a counter covering ALL its users, and must track per-user ownership itself.
    /// @param dailyLimitUsd   Max USD (18 decimals) the wallet may spend per window. Must be >= 0.
    /// @param windowDuration  Spending-window length in seconds. Must be > 0.
    /// @param watchedTokens   Tokens to meter; each must already be priced by the oracle.
    /// @param nativeMetered   Whether native ETH/BNB counts toward the USD cap. False leaves native
    ///        outside it; with no watched tokens either, the cap meters nothing until the owner adds some.
    /// @param sessionKeyX     X coordinate of the first passkey's P-256 public key. With
    ///        `sessionKeyY`, both zero, the wallet is owner-only and no passkey is authorized.
    /// @param sessionKeyY     Y coordinate of that public key.
    /// @param sessionKeyValidUntil Unix timestamp the passkey expires at. Must be in the future and
    ///        within {SessionHandler-MAX_SESSION_TTL}; ignored when the key is (0, 0).
    /// @param trustedSpenders Spenders trusted for unpriced-token approvals. May be empty.
    function deployWallet(
        int256 dailyLimitUsd,
        uint256 windowDuration,
        address[] calldata watchedTokens,
        bool nativeMetered,
        bytes32 sessionKeyX,
        bytes32 sessionKeyY,
        uint48 sessionKeyValidUntil,
        address[] calldata trustedSpenders
    ) external payable whenNotPaused returns (address) {
        address module = REGISTRY.spendingLimitModule();
        if (module == address(0)) revert SHFactory_SpendingLimitModuleNotSet();
        address recurringModule = REGISTRY.recurringPaymentModule();
        if (recurringModule == address(0)) revert SHFactory_RecurringPaymentModuleNotSet();
        address agentModule = REGISTRY.agentModule();
        if (agentModule == address(0)) revert SHFactory_AgentModuleNotSet();

        // The counter is bumped BEFORE the external initialize() call (checks-effects-interactions).
        bytes32 salt = _salt(msg.sender, deployCount[msg.sender]++);
        address walletInstance = Clones.cloneDeterministic(IMPLEMENTATION, salt);

        SessionHandler(payable(walletInstance))
            .initialize(
                SessionHandler.InitConfig({
                    owner: msg.sender,
                    spendingLimitModule: module,
                    recurringPaymentModule: recurringModule,
                    agentModule: agentModule,
                    dailyLimitUsd: dailyLimitUsd,
                    windowDuration: windowDuration,
                    watchedTokens: watchedTokens,
                    nativeMetered: nativeMetered,
                    sessionKeyX: sessionKeyX,
                    sessionKeyY: sessionKeyY,
                    sessionKeyValidUntil: sessionKeyValidUntil,
                    trustedSpenders: trustedSpenders
                })
            );

        (bool success,) = payable(walletInstance).call{value: msg.value}("");
        if (!success) {
            revert SHFactory_FundTransferFailed();
        }

        emit WalletDeployed(walletInstance, msg.sender);
        return walletInstance;
    }
}

