// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {SHOracle} from "./SHOracle.sol";
import {IAppCheck} from "./interfaces/IAppCheck.sol";

/**
 * @title SHRegistry
 * @author Conrad Japhet
 * @notice Central configuration registry for the SessionHandler Protocol. Stores the
 *         protocol fee, keeper fee, treasury address, price oracle, and agent identity used across
 *         all deployed SessionHandler wallets.
 * @dev SessionHandler wallets read the MUTABLE parameters here (fees, treasury, price oracle, agent
 *      id) at execution time rather than storing them, so an update propagates instantly to every
 *      deployed wallet without redeployment. Two kinds of address do not work that way: the
 *      EntryPoint and both ERC-8004 registries are immutable here and baked into the SessionHandler
 *      implementation by SHFactory, and the two modules (SpendingLimitModule, RecurringPaymentModule)
 *      are installed into each wallet when it is deployed, so changing either only affects wallets
 *      deployed afterwards.
 *
 *      The protocol fee is a flat amount of native token, in wei, and is charged as stored — no
 *      oracle read on the fee path. That keeps a session execution from paying for a Chainlink read
 *      (and, on an L2, the sequencer check) just to price a fee, and keeps the fee collectable
 *      while the native feed is stale. The trade: its dollar value moves with the native price, so
 *      the operator re-sets it with {setProtocolFee} when that drifts too far.
 *
 *      Owned by the treasury operator. protocolFee is bounded to
 *      [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE]: the ceiling bounds the worst-case impact of a
 *      compromised owner key, and the floor means SessionHandler never sends a zero-value fee
 *      transfer. Note the floor also means fees cannot be switched off protocol-wide.
 *
 *      The bounds are wei, so they differ per chain: the deploy script converts one pair of dollar
 *      targets through each chain's native price at deploy time. They are immutable, and so drift
 *      in dollar terms as the native price moves; the gap between them is what leaves the operator
 *      room to keep the fee where it wants within them.
 *
 *      The KEEPER fee is a second flat amount, charged on top of the protocol fee only when an
 *      executor module (RecurringPaymentModule) runs a payment. The protocol's own backend sends that
 *      transaction and pays its gas, and the keeper fee pays it back through the treasury. It has its
 *      own immutable bounds, [MIN_KEEPER_FEE, MAX_KEEPER_FEE], and unlike the protocol fee its floor
 *      may be zero: it is never transferred on its own.
 *
 *      Both fees are stored in GWEI, as uint48s, so they share one slot with {treasury}: the wallet's
 *      fee path reads all three in one storage read via {feeConfig}. Everything outside the contract
 *      stays in wei -- the setters take wei, the getters return wei -- and a fee must be a whole number
 *      of gwei. That costs no useful precision: a $0.01 fee is thousands of gwei on every chain.
 */
contract SHRegistry is Ownable {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when a proposed fee falls outside its bounds: [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE]
    ///      for the protocol fee, [MIN_KEEPER_FEE, MAX_KEEPER_FEE] for the keeper fee.
    error SHRegistry_FeeNotInRange();
    /// @dev Thrown at construction when a pair of fee bounds is unusable: a floor above the ceiling, a
    ///      ceiling past {MAX_STORABLE_FEE}, a bound that is not a whole number of gwei, or -- for the
    ///      protocol fee only -- a zero floor, which would allow a zero-value fee transfer.
    error SHRegistry_InvalidFeeBounds();
    /// @dev Thrown when a fee is not a whole number of gwei, the unit fees are stored in.
    error SHRegistry_FeeNotWholeGwei(uint256 fee);
    /// @dev Thrown when address(0) is passed as the treasury address.
    error SHRegistry_InvalidTreasury();
    /// @dev Thrown when address(0) is passed as the price oracle address.
    error SHRegistry_InvalidPriceOracle();
    /// @dev Thrown by commitPriceOracle/cancelPriceOracle when no proposal is outstanding.
    error SHRegistry_NoPendingOracle();
    /// @dev Thrown when commitPriceOracle is called before the proposal's ETA.
    /// @param eta The timestamp at which the pending proposal becomes committable.
    error SHRegistry_TimelockNotElapsed(uint256 eta);
    /// @dev Thrown when address(0) is passed as the factory address.
    error SHRegistry_InvalidFactory();
    /// @dev Thrown when address(0) is passed as the SpendingLimitModule address.
    error SHRegistry_InvalidSpendingLimitModule();
    /// @dev Thrown when address(0) is passed as the RecurringPaymentModule address.
    error SHRegistry_InvalidRecurringPaymentModule();
    /// @dev Thrown when address(0) is passed as the AgentModule address.
    error SHRegistry_InvalidAgentModule();
    /// @dev Thrown when address(0) is passed as the ERC-8004 Identity Registry address.
    error SHRegistry_InvalidIdentityRegistry();
    /// @dev Thrown when address(0) is passed as the Reputation Registry address.
    error SHRegistry_InvalidReputationRegistry();
    /// @dev Thrown when address(0) is passed as the ERC-4337 EntryPoint address.
    error SHRegistry_InvalidEntryPoint();
    /// @dev Thrown when an app check is registered for address(0), names no code, or reports a
    ///      different {IAppCheck-app} from the one it is registered under.
    /// @param app   The app named.
    /// @param check The check named.
    error SHRegistry_InvalidAppCheck(address app, address check);
    /// @dev Thrown when an app check change is committed or cancelled for an app with none pending.
    /// @param app The app named.
    error SHRegistry_NoPendingAppCheck(address app);
    /// @dev Thrown when an app check is removed from an app that has none.
    /// @param app The app named.
    error SHRegistry_NoAppCheck(address app);

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Maximum protocol fee that can ever be set, in wei, protecting wallet owners from
    ///         runaway fees.
    /// @dev Immutable, so no later owner action — including a compromised owner key — can raise it.
    ///      It deliberately does not consult the oracle: the owner also controls the oracle's feeds,
    ///      so a ceiling checked in dollars could be lifted by faking a low native price first.
    uint256 public immutable MAX_PROTOCOL_FEE;

    /// @notice Minimum protocol fee that can ever be set, in wei. Guarantees SessionHandler's fee
    ///         transfer is never a zero-value call, so the fee path always records value.
    uint256 public immutable MIN_PROTOCOL_FEE;

    /// @notice Maximum keeper fee that can ever be set, in wei. Immutable for the same reason as
    ///         {MAX_PROTOCOL_FEE}: no owner action, including a compromised key, can raise it.
    uint256 public immutable MAX_KEEPER_FEE;

    /// @notice Minimum keeper fee that can ever be set, in wei. May be zero: the keeper fee is only
    ///         ever added to the protocol fee, which is never zero, so no zero-value transfer results.
    uint256 public immutable MIN_KEEPER_FEE;

    /// @notice Largest fee the gwei storage can hold, in wei: type(uint48).max gwei, about 281,000
    ///         ETH. Both fee ceilings must fit under it.
    uint256 public constant MAX_STORABLE_FEE = uint256(type(uint48).max) * 1 gwei;

    /// @notice Delay between proposing an oracle change and being able to commit it.
    /// @dev The oracle governs every wallet's spending cap, so a repoint is the single highest-impact
    ///      action the operator key can take (THREAT_MODEL §3.8). The delay makes it publicly
    ///      observable before it binds. The cost: a genuinely broken live oracle cannot be replaced
    ///      for this long — wallet owners can {SessionHandler-pause} in the meantime.
    uint256 public constant ORACLE_TIMELOCK = 2 days;

    /// @notice Delay between proposing an app check and being able to commit it.
    /// @dev An app check decides which calls passkeys and agents may make to an app on EVERY wallet,
    ///      so adding or replacing one waits as long as an oracle change does, in public. Removing one
    ///      ({removeAppCheck}) is instant: it only takes ability away.
    uint256 public constant APP_CHECK_TIMELOCK = 2 days;

    // Storage is ordered to pack: each uint below is sized to share a slot with the address it is
    // read alongside. Slot 0 is Ownable's `_owner`; `forge inspect SHRegistry storageLayout` shows
    // the rest. The registry is not upgradeable, so this layout only matters for a fresh deploy.

    /// @notice Canonical SHOracle used by all SessionHandler wallets for USD spending limit enforcement.
    address public priceOracle;

    /// @notice Oracle awaiting commit, or address(0) when no proposal is outstanding.
    address public pendingPriceOracle;
    /// @notice Timestamp from which {pendingPriceOracle} may be committed. Meaningless when there is
    ///         no pending proposal.
    /// @dev uint48 (enough seconds for millions of years) so it shares a slot with
    ///      {pendingPriceOracle}; the two are always written, read and cleared together.
    uint48 public pendingPriceOracleEta;

    /// @notice Id of the SessionHandler ERC-4337 AI agent registered on the ERC-8004 Identity Registry.
    /// @dev Left uint256: the id is minted by the external identity registry, so its range is not
    ///      this contract's to narrow.
    uint256 public agentId;

    /// @notice SpendingLimitModule installed as a hook on every SessionHandler deployed from here on.
    /// @dev Settable rather than immutable, and deliberately NOT a constructor argument: the module's
    ///      own constructor reads `priceOracle()` off this registry, so the registry must exist first.
    ///      Taking the module here would make the two mutually undeployable. The deploy script sets it
    ///      immediately after constructing the module; SHFactory.deployWallet reverts while it is unset.
    ///      Changing it only affects wallets deployed afterwards — existing wallets keep the module
    ///      they were initialized with, since SessionHandler copies it into its own storage.
    address public spendingLimitModule;

    /// @notice RecurringPaymentModule installed as an EXECUTOR on every SessionHandler deployed from
    ///         here on.
    /// @dev Settable, like {spendingLimitModule}, and with the same reach: changing it only affects
    ///      wallets deployed afterwards, since each wallet installs the module current at its deploy.
    ///      SHFactory.deployWallet reverts while it is unset. An executor can MOVE the wallet's funds
    ///      (within the guard and the spending cap), where a hook can only refuse, so this setter is
    ///      the more sensitive of the two: whoever controls it chooses the executor new wallets get.
    address public recurringPaymentModule;

    /// @notice AgentModule installed as a VALIDATOR on every SessionHandler deployed from here on.
    /// @dev Settable, with the same reach as the two above: changing it only affects wallets deployed
    ///      afterwards. SHFactory.deployWallet reverts while it is unset. As sensitive as
    ///      {recurringPaymentModule}: a validator approves UserOps for the wallet (AgentModule only an
    ///      op signed by an agent the wallet's OWNER added), so whoever controls this setter chooses
    ///      what new wallets trust to sign for them.
    address public agentModule;

    /// @notice Address that receives protocol fees collected by SessionHandler wallets.
    address public treasury;
    /// @dev The protocol fee, in GWEI. Read through {protocolFee}, {getFee} or {feeConfig}, which all
    ///      return wei. uint48 so it and {_keeperFeeGwei} share a slot with {treasury}: SessionHandler's
    ///      fee path reads all three on every execution that pays a fee, in one storage read.
    uint48 private _protocolFeeGwei;
    /// @dev The keeper fee, in GWEI, charged on top of the protocol fee when an executor module runs a
    ///      payment. Read through {keeperFee} or {feeConfig}, which return wei.
    uint48 private _keeperFeeGwei;

    /// @notice The SHFactory that deploys SessionHandler wallets against this registry.
    /// @dev Recorded for off-chain discoverability — no contract here reads it. Set after deployment
    ///      because the factory takes this registry's address in its own constructor.
    address public factory;

    /// @notice The canonical ERC-4337 EntryPoint that deployed wallets validate UserOps against.
    /// @dev Immutable, like the two ERC-8004 registries below. SHFactory's constructor bakes all three
    ///      into its SessionHandler implementation as immutables, so a later change here could never
    ///      reach a wallet anyway. Making that permanence explicit is more honest than a setter that
    ///      would do nothing for any wallet.
    address public immutable ENTRY_POINT;

    /// @notice app => the IAppCheck the PayeeGuard asks about every call a passkey or an agent makes to
    ///         it. address(0) for an app with none, which the guard refuses outright.
    /// @dev Read live by the guard on each call, so a change here reaches every deployed wallet at once
    ///      — which is why adding or replacing an entry waits {APP_CHECK_TIMELOCK}.
    mapping(address app => address check) public appCheck;

    /// @notice A proposed app check, waiting out {APP_CHECK_TIMELOCK}. `eta == 0` means none.
    struct PendingAppCheck {
        address check; // the IAppCheck to register for the app
        uint48 eta; // when it may be committed
    }

    /// @notice app => its pending app check, if any.
    mapping(address app => PendingAppCheck) public pendingAppCheck;

    /// @notice Reputation Registry baked into every SessionHandler deployed from this registry.
    address public immutable REPUTATION_REGISTRY;
    /// @notice ERC-8004 Identity Registry baked into every SessionHandler deployed from this registry.
    address public immutable IDENTITY_REGISTRY;

    /*//////////////////////////////////////////////////////////////
                                  EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when the protocol fee is updated.
    /// @param oldFee The previous fee, in wei.
    /// @param newFee The new fee, in wei.
    event ProtocolFeeUpdated(uint256 oldFee, uint256 newFee);

    /// @notice Emitted when the treasury address is updated.
    /// @param oldTreasury The previous treasury address.
    /// @param newTreasury The new treasury address.
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);

    /// @notice Emitted when the price oracle address is updated.
    /// @param oldOracle The previous price oracle address.
    /// @param newOracle The new price oracle address.
    event PriceOracleUpdated(address indexed oldOracle, address indexed newOracle);

    /// @notice Emitted when the registered agentId is updated.
    /// @param oldId The previous agentId.
    /// @param newId The new agentId.
    event AgentIdUpdated(uint256 indexed oldId, uint256 indexed newId);

    /// @notice Emitted when an oracle change is proposed, starting the timelock.
    /// @param newOracle The proposed SHOracle.
    /// @param eta       The timestamp from which it may be committed.
    event PriceOracleProposed(address indexed newOracle, uint256 eta);

    /// @notice Emitted when a pending oracle proposal is withdrawn before commit.
    /// @param cancelledOracle The proposal that was withdrawn.
    event PriceOracleProposalCancelled(address indexed cancelledOracle);

    /// @notice Emitted when the recorded factory address changes.
    /// @param oldFactory The previous factory.
    /// @param newFactory The new factory.
    event FactoryUpdated(address indexed oldFactory, address indexed newFactory);

    /// @notice Emitted when the SpendingLimitModule for future wallet deployments changes.
    /// @param oldModule The previous module.
    /// @param newModule The new module.
    event SpendingLimitModuleUpdated(address indexed oldModule, address indexed newModule);

    /// @notice Emitted when the keeper fee is updated.
    /// @param oldFee The previous fee, in wei.
    /// @param newFee The new fee, in wei.
    event KeeperFeeUpdated(uint256 oldFee, uint256 newFee);

    /// @notice Emitted when the RecurringPaymentModule for future wallet deployments changes.
    /// @param oldModule The previous module.
    /// @param newModule The new module.
    event RecurringPaymentModuleUpdated(address indexed oldModule, address indexed newModule);

    /// @notice Emitted when the AgentModule for future wallet deployments changes.
    /// @param oldModule The previous module.
    /// @param newModule The new module.
    event AgentModuleUpdated(address indexed oldModule, address indexed newModule);

    /// @notice Emitted when an app check is proposed, starting its {APP_CHECK_TIMELOCK}.
    /// @param app   The app the check is for.
    /// @param check The check proposed.
    /// @param eta   When it may be committed.
    event AppCheckProposed(address indexed app, address indexed check, uint256 eta);

    /// @notice Emitted when a pending app check is withdrawn before it was committed.
    /// @param app   The app the check was for.
    /// @param check The check withdrawn.
    event AppCheckProposalCancelled(address indexed app, address indexed check);

    /// @notice Emitted when an app's live check changes: registered at deploy, committed, or removed.
    /// @param app      The app.
    /// @param oldCheck Its check before, or address(0).
    /// @param newCheck Its check now, or address(0) once removed.
    event AppCheckUpdated(address indexed app, address indexed oldCheck, address indexed newCheck);

    /*//////////////////////////////////////////////////////////////
                                  TYPES
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice A fee's starting value and the bounds it may ever be set within, all in wei and each a
     *         whole number of gwei. A struct so the constructor names every figure and stays clear of
     *         the stack limit.
     * @param initial Starting fee. Must be within [min, max].
     * @param min     Lowest fee the owner may ever set.
     * @param max     Highest fee the owner may ever set. Must be at least `min` and at most
     *                {MAX_STORABLE_FEE}.
     */
    struct FeeBounds {
        uint256 initial;
        uint256 min;
        uint256 max;
    }

    /// @notice An app and its check, as the constructor takes them.
    struct AppCheckEntry {
        address app;
        address check;
    }

    /*//////////////////////////////////////////////////////////////
                               CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Deploys the SHRegistry with an initial fee and protocol addresses.
     * @dev The owner is supplied rather than taken from msg.sender so the registry can be deployed
     *      directly under SHTreasury's ownership, without a follow-up transferOwnership. SHTreasury
     *      is the protocol's single admin root: it owns this registry, the SHOracle, and the
     *      SHFactory, and reaches each through its own owner-only passthroughs.
     * @dev The SpendingLimitModule is deliberately NOT taken here — see {spendingLimitModule} for
     *      why it cannot be (the module's own constructor reads this registry, so the registry must
     *      exist first). The deploy script sets it right after; SHFactory refuses to deploy wallets
     *      until it is set. The RecurringPaymentModule is set the same way, for symmetry: both modules
     *      a wallet installs come from the same kind of setter.
     * @dev Rejects an initial oracle that cannot price native, the same check {proposePriceOracle}
     *      makes on a replacement. SpendingLimitModule meters the account's native balance delta, so
     *      an oracle without it would revert every native-moving execution on every deployed wallet;
     *      it is caught at construction rather than at the first user transaction. It also rejects
     *      any address with no isPriced() to call, catching a wrong-network or mistyped oracle address.
     * @param initialOwner       Address that will own this registry — the SHTreasury.
     * @param protocolFeeBounds  Starting protocol fee and its bounds. See {FeeBounds}; `min` must be
     *        non-zero.
     * @param keeperFeeBounds    Starting keeper fee and its bounds. See {FeeBounds}; `min` may be zero.
     * @param initialTreasury    Address that will receive protocol fees. Must not be address(0).
     * @param initialOracle      Address of the deployed SHOracle. Must not be address(0), and must be
     *        able to price native — see the native-feed check below.
     * @param reputationRegistry Reputation Registry baked into every wallet. Must not be address(0).
     * @param identityRegistry   ERC-8004 Identity Registry baked into every wallet. Must not be address(0).
     * @param entryPointAddress  Canonical ERC-4337 EntryPoint. Must not be address(0).
     * @param initialAgentId     Id of the SessionHandler agent on the ERC-8004 Identity Registry. Zero is
     *        a VALID id — ERC-8004 registries mint from 0, so the first agent registered holds id 0.
     * @param initialAppChecks   The app checks wallets start with, registered at once (see
     *        {proposeAppCheck} for every later one). Each must pass the same checks a proposal does.
     */
    constructor(
        address initialOwner,
        FeeBounds memory protocolFeeBounds,
        FeeBounds memory keeperFeeBounds,
        address initialTreasury,
        address initialOracle,
        address reputationRegistry,
        address identityRegistry,
        address entryPointAddress,
        uint256 initialAgentId,
        AppCheckEntry[] memory initialAppChecks
    ) Ownable(initialOwner) {
        if (protocolFeeBounds.min == 0) revert SHRegistry_InvalidFeeBounds();
        _checkFeeBounds(protocolFeeBounds);
        _checkFeeBounds(keeperFeeBounds);
        if (initialTreasury == address(0)) revert SHRegistry_InvalidTreasury();
        if (initialOracle == address(0)) revert SHRegistry_InvalidPriceOracle();
        if (!SHOracle(initialOracle).isPriced(address(0))) revert SHRegistry_InvalidPriceOracle();
        if (entryPointAddress == address(0)) revert SHRegistry_InvalidEntryPoint();
        if (reputationRegistry == address(0)) revert SHRegistry_InvalidReputationRegistry();
        if (identityRegistry == address(0)) revert SHRegistry_InvalidIdentityRegistry();
        IDENTITY_REGISTRY = identityRegistry;
        REPUTATION_REGISTRY = reputationRegistry;
        ENTRY_POINT = entryPointAddress;
        MIN_PROTOCOL_FEE = protocolFeeBounds.min;
        MAX_PROTOCOL_FEE = protocolFeeBounds.max;
        MIN_KEEPER_FEE = keeperFeeBounds.min;
        MAX_KEEPER_FEE = keeperFeeBounds.max;
        _protocolFeeGwei = _toGwei(protocolFeeBounds.initial);
        _keeperFeeGwei = _toGwei(keeperFeeBounds.initial);
        treasury = initialTreasury;
        priceOracle = initialOracle;
        agentId = initialAgentId;
        // No delay here: before the factory exists there is no wallet for a check to bind.
        for (uint256 i = 0; i < initialAppChecks.length; i++) {
            AppCheckEntry memory entry = initialAppChecks[i];
            _requireValidAppCheck(entry.app, entry.check);
            appCheck[entry.app] = entry.check;
            emit AppCheckUpdated(entry.app, address(0), entry.check);
        }
    }

    /*//////////////////////////////////////////////////////////////
                            EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Updates the protocol fee charged on every session-key execution. Only callable by the owner.
     * @dev Takes effect protocol-wide on the next execution — wallets read the fee from here rather
     *      than storing it. The value is wei, so its dollar value moves with the native price; this is
     *      how the operator brings it back in line.
     * @param newFee The new fee, in wei. Must be within [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE] and a
     *        whole number of gwei.
     */
    function setProtocolFee(uint256 newFee) external onlyOwner {
        if (newFee > MAX_PROTOCOL_FEE || newFee < MIN_PROTOCOL_FEE) revert SHRegistry_FeeNotInRange();
        uint256 oldFee = protocolFee();
        _protocolFeeGwei = _toGwei(newFee);
        emit ProtocolFeeUpdated(oldFee, newFee);
    }

    /**
     * @notice Updates the keeper fee charged, on top of the protocol fee, each time an executor module
     *         runs a payment. Only callable by the owner.
     * @dev Takes effect protocol-wide on the next executor run, like {setProtocolFee}. The keeper's
     *      cost is gas, which differs per chain and over time; this is how the operator follows it.
     * @param newFee The new fee, in wei. Must be within [MIN_KEEPER_FEE, MAX_KEEPER_FEE] and a whole
     *        number of gwei.
     */
    function setKeeperFee(uint256 newFee) external onlyOwner {
        if (newFee > MAX_KEEPER_FEE || newFee < MIN_KEEPER_FEE) revert SHRegistry_FeeNotInRange();
        uint256 oldFee = keeperFee();
        _keeperFeeGwei = _toGwei(newFee);
        emit KeeperFeeUpdated(oldFee, newFee);
    }

    /**
     * @notice Updates the treasury address that receives protocol fees. Only callable by the owner.
     * @param newTreasury The new treasury address. Must not be address(0).
     */
    function setTreasury(address newTreasury) external onlyOwner {
        if (newTreasury == address(0)) revert SHRegistry_InvalidTreasury();
        address oldTreasury = treasury;
        treasury = newTreasury;
        emit TreasuryUpdated(oldTreasury, newTreasury);
    }

    /**
     * @notice Records the SHFactory deploying wallets against this registry. Only callable by the owner.
     * @dev Set after deployment because the factory takes this registry's address in its own
     *      constructor. Nothing on-chain reads it — it exists for off-chain discoverability.
     * @param newFactory The SHFactory address. Must not be address(0).
     */
    function setFactory(address newFactory) external onlyOwner {
        if (newFactory == address(0)) revert SHRegistry_InvalidFactory();
        address oldFactory = factory;
        factory = newFactory;
        emit FactoryUpdated(oldFactory, newFactory);
    }

    /**
     * @notice Sets the SpendingLimitModule installed on wallets deployed from this point on. Only
     *         callable by the owner.
     * @dev Does NOT touch wallets already deployed: SessionHandler.initialize copies the module into
     *      the wallet's own storage, so an existing wallet keeps the module it installed at deploy.
     *      Set by the deploy script immediately after the module is constructed — it cannot be a
     *      constructor argument, since the module's constructor reads this registry's oracle.
     * @param newModule The deployed SpendingLimitModule address. Must not be address(0); to halt
     *        deployments, pause the factory instead.
     */
    function setSpendingLimitModule(address newModule) external onlyOwner {
        if (newModule == address(0)) revert SHRegistry_InvalidSpendingLimitModule();
        address oldModule = spendingLimitModule;
        spendingLimitModule = newModule;
        emit SpendingLimitModuleUpdated(oldModule, newModule);
    }

    /**
     * @notice Sets the RecurringPaymentModule installed as an executor on wallets deployed from this
     *         point on. Only callable by the owner.
     * @dev Does NOT touch wallets already deployed: each keeps the executor it installed at deploy,
     *      and only its owner can uninstall it. See {recurringPaymentModule} for why this is the more
     *      sensitive of the two module setters.
     * @param newModule The deployed RecurringPaymentModule address. Must not be address(0); to halt
     *        deployments, pause the factory instead.
     */
    function setRecurringPaymentModule(address newModule) external onlyOwner {
        if (newModule == address(0)) revert SHRegistry_InvalidRecurringPaymentModule();
        address oldModule = recurringPaymentModule;
        recurringPaymentModule = newModule;
        emit RecurringPaymentModuleUpdated(oldModule, newModule);
    }

    /**
     * @notice Sets the AgentModule installed as a validator on wallets deployed from this point on.
     *         Only callable by the owner.
     * @dev Does NOT touch wallets already deployed: each keeps the validator it installed at deploy,
     *      and only its owner can uninstall it. See {agentModule} for why this setter is sensitive.
     * @param newModule The deployed AgentModule address. Must not be address(0); to halt deployments,
     *        pause the factory instead.
     */
    function setAgentModule(address newModule) external onlyOwner {
        if (newModule == address(0)) revert SHRegistry_InvalidAgentModule();
        address oldModule = agentModule;
        agentModule = newModule;
        emit AgentModuleUpdated(oldModule, newModule);
    }

    /**
     * @notice Proposes a new canonical SHOracle, starting the {ORACLE_TIMELOCK} delay. Only callable
     *         by the owner. Replaces any proposal already outstanding, restarting the delay.
     * @dev Rejects an oracle that cannot price native (address(0)). SpendingLimitModule meters the
     *      account's native balance delta on EVERY transaction, so committing such an oracle would
     *      revert every native-moving execution on every deployed wallet. Checking it here fails the
     *      proposal loudly rather than two days later on commit. This also rejects any address with
     *      no isPriced() to call, catching a wrong-network or mistyped oracle address.
     * @param newOracle The SHOracle to propose. Must not be address(0) and must price address(0).
     */
    function proposePriceOracle(address newOracle) external onlyOwner {
        if (newOracle == address(0)) revert SHRegistry_InvalidPriceOracle();
        if (!SHOracle(newOracle).isPriced(address(0))) revert SHRegistry_InvalidPriceOracle();

        pendingPriceOracle = newOracle;
        pendingPriceOracleEta = SafeCast.toUint48(block.timestamp + ORACLE_TIMELOCK);
        emit PriceOracleProposed(newOracle, pendingPriceOracleEta);
    }

    /**
     * @notice Commits the pending oracle proposal once its ETA has passed. Only callable by the owner.
     * @dev Takes effect immediately for every deployed wallet — they resolve the oracle from here on
     *      each valuation, so no redeployment is needed. Clears the proposal, so committing twice
     *      reverts with {SHRegistry_NoPendingOracle}.
     */
    function commitPriceOracle() external onlyOwner {
        address newOracle = pendingPriceOracle;
        if (newOracle == address(0)) revert SHRegistry_NoPendingOracle();
        if (block.timestamp < pendingPriceOracleEta) revert SHRegistry_TimelockNotElapsed(pendingPriceOracleEta);

        address oldOracle = priceOracle;
        priceOracle = newOracle;
        delete pendingPriceOracle;
        delete pendingPriceOracleEta;
        emit PriceOracleUpdated(oldOracle, newOracle);
    }

    /**
     * @notice Withdraws the pending oracle proposal before it is committed. Only callable by the owner.
     * @dev The escape hatch for a proposal made in error: without it a mistaken proposal could only
     *      be superseded by another, never cleared.
     */
    function cancelPriceOracle() external onlyOwner {
        address cancelled = pendingPriceOracle;
        if (cancelled == address(0)) revert SHRegistry_NoPendingOracle();
        delete pendingPriceOracle;
        delete pendingPriceOracleEta;
        emit PriceOracleProposalCancelled(cancelled);
    }

    /**
     * @notice Proposes `check` as the IAppCheck for calls passkeys and agents make to `app`, starting
     *         the {APP_CHECK_TIMELOCK}. Only callable by the owner. Replaces any proposal already
     *         pending for `app`, restarting the delay.
     * @dev Validated now ({_requireValidAppCheck}) and again on commit, so a mis-wired check fails here
     *      rather than two days later.
     * @param app   The contract the check is for. Must not be address(0).
     * @param check The IAppCheck. Must have code, and its {IAppCheck-app} must be `app`.
     */
    function proposeAppCheck(address app, address check) external onlyOwner {
        _requireValidAppCheck(app, check);
        uint48 eta = SafeCast.toUint48(block.timestamp + APP_CHECK_TIMELOCK);
        pendingAppCheck[app] = PendingAppCheck({check: check, eta: eta});
        emit AppCheckProposed(app, check, eta);
    }

    /**
     * @notice Makes `app`'s pending check its live one, once the delay has passed. Only callable by the
     *         owner.
     * @dev Takes effect on every deployed wallet's next call to `app`. Clears the proposal, so
     *      committing twice reverts with {SHRegistry_NoPendingAppCheck}.
     * @param app The app whose pending check to commit.
     */
    function commitAppCheck(address app) external onlyOwner {
        PendingAppCheck memory pending = pendingAppCheck[app];
        if (pending.eta == 0) revert SHRegistry_NoPendingAppCheck(app);
        if (block.timestamp < pending.eta) revert SHRegistry_TimelockNotElapsed(pending.eta);
        _requireValidAppCheck(app, pending.check);
        address oldCheck = appCheck[app];
        appCheck[app] = pending.check;
        delete pendingAppCheck[app];
        emit AppCheckUpdated(app, oldCheck, pending.check);
    }

    /**
     * @notice Withdraws `app`'s pending check before it is committed. Only callable by the owner.
     * @param app The app whose pending check to withdraw.
     */
    function cancelAppCheck(address app) external onlyOwner {
        address cancelled = pendingAppCheck[app].check;
        if (pendingAppCheck[app].eta == 0) revert SHRegistry_NoPendingAppCheck(app);
        delete pendingAppCheck[app];
        emit AppCheckProposalCancelled(app, cancelled);
    }

    /**
     * @notice Removes `app`'s live check at once: from the next call, passkeys and agents can't call
     *         `app` at all. Only callable by the owner.
     * @dev No delay, because removing only takes ability away. It is the switch for a check found to
     *      have a bug. A check still pending for `app` is left as it is: cancel it too.
     * @param app The app whose check to remove.
     */
    function removeAppCheck(address app) external onlyOwner {
        address oldCheck = appCheck[app];
        if (oldCheck == address(0)) revert SHRegistry_NoAppCheck(app);
        delete appCheck[app];
        emit AppCheckUpdated(app, oldCheck, address(0));
    }

    /**
     * @notice Updates the registered agentId for the SessionHandler Protocol on the ERC-8004 Identity Registry. Only callable by the owner.
     * @dev Unvalidated on purpose: 0 is a legitimate agent id (ERC-8004 registries mint from 0), so
     *      there is no sentinel to reject. An id with no corresponding token makes
     *      {SessionHandler-getAgentIdentity} report `registered == false` rather than revert.
     * @param newId The new agentId.
     */
    function setAgentId(uint256 newId) external onlyOwner {
        uint256 oldId = agentId;
        agentId = newId;
        emit AgentIdUpdated(oldId, newId);
    }

    /*//////////////////////////////////////////////////////////////
                                  VIEWS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The protocol fee for one session-key execution, in wei.
     * @dev Same value as {protocolFee}; kept because the app's quotes read it by this name. Makes no
     *      oracle call, so it cannot revert on a stale feed.
     * @return The fee in wei.
     */
    function getFee() external view returns (uint256) {
        return protocolFee();
    }

    /// @notice The protocol fee for one session-key execution, in wei.
    function protocolFee() public view returns (uint256) {
        return uint256(_protocolFeeGwei) * 1 gwei;
    }

    /// @notice The keeper fee added to the protocol fee when an executor module runs a payment, in wei.
    function keeperFee() public view returns (uint256) {
        return uint256(_keeperFeeGwei) * 1 gwei;
    }

    /**
     * @notice Everything SessionHandler's fee path needs, in one call and one storage read.
     * @dev The three values share a slot. Read per call, so a fee or treasury change lands on every
     *      wallet at once.
     * @return feeRecipient The treasury the fees are sent to.
     * @return protocolFee_  The protocol fee, in wei.
     * @return keeperFee_    The keeper fee, in wei. A wallet adds it only on the executor path.
     */
    function feeConfig() external view returns (address feeRecipient, uint256 protocolFee_, uint256 keeperFee_) {
        return (treasury, uint256(_protocolFeeGwei) * 1 gwei, uint256(_keeperFeeGwei) * 1 gwei);
    }

    /*//////////////////////////////////////////////////////////////
                                INTERNAL
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts unless `bounds` is usable: min <= initial <= max <= {MAX_STORABLE_FEE}, and both
    ///      bounds whole gwei (`initial` is checked for that when it is stored). The protocol fee's
    ///      non-zero floor is checked by the caller.
    function _checkFeeBounds(FeeBounds memory bounds) private pure {
        if (
            bounds.min > bounds.max || bounds.max > MAX_STORABLE_FEE || bounds.min % 1 gwei != 0
                || bounds.max % 1 gwei != 0
        ) {
            revert SHRegistry_InvalidFeeBounds();
        }
        if (bounds.initial > bounds.max || bounds.initial < bounds.min) revert SHRegistry_FeeNotInRange();
    }

    /// @dev Converts a wei fee to the gwei it is stored in. Callers range-check first, against a
    ///      ceiling no higher than {MAX_STORABLE_FEE}, so the result always fits a uint48.
    function _toGwei(uint256 fee) private pure returns (uint48) {
        if (fee % 1 gwei != 0) revert SHRegistry_FeeNotWholeGwei(fee);
        // Safe: fee <= MAX_STORABLE_FEE = type(uint48).max gwei, checked by every caller's range check.
        // forge-lint: disable-next-line(unsafe-typecast)
        return uint48(fee / 1 gwei);
    }

    /**
     * @dev Reverts unless `check` can be registered for `app`: `app` is not address(0), `check` has
     *      code, and `check` says it reads calls to `app`. The last catches the likely wiring mistake
     *      of registering a router's check under the position manager's address.
     */
    function _requireValidAppCheck(address app, address check) internal view {
        if (app == address(0) || check.code.length == 0 || IAppCheck(check).app() != app) {
            revert SHRegistry_InvalidAppCheck(app, check);
        }
    }
}
