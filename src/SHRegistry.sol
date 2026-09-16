// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {SHOracle} from "./SHOracle.sol";

/**
 * @title SHRegistry
 * @author Conrad Japhet
 * @notice Central configuration registry for the SessionHandler Protocol. Stores the
 *         protocol fee, treasury address, price oracle, and agent identity used across all
 *         deployed SessionHandler wallets.
 * @dev SessionHandler wallets read all protocol parameters from this contract at
 *      execution time rather than storing them as immutables, so any update here
 *      propagates instantly to every deployed wallet without redeployment.
 *
 *      The protocol fee is denominated in USD (18 decimals), not in wei. SessionHandler converts it
 *      to native at execution time via {getFee}, which prices it through the registry's own
 *      priceOracle. That keeps what a user actually pays per execution stable in dollar terms
 *      across ETH price moves, where a stored wei amount would silently re-price itself.
 *
 *      Owned by the treasury operator. protocolFee is bounded to
 *      [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE]: the ceiling bounds the worst-case impact of a
 *      compromised owner key, and the floor means SessionHandler never sends a zero-value fee
 *      transfer. Note the floor also means fees cannot be switched off protocol-wide.
 */
contract SHRegistry is Ownable {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when a proposed protocolFee falls outside [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE].
    error SHRegistry_FeeNotInRange();
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
    /// @dev Thrown when address(0) is passed as the ERC-8004 Identity Registry address.
    error SHRegistry_InvalidIdentityRegistry();
    /// @dev Thrown when address(0) is passed as the Reputation Registry address.
    error SHRegistry_InvalidReputationRegistry();
    /// @dev Thrown when address(0) is passed as the ERC-4337 EntryPoint address.
    error SHRegistry_InvalidEntryPoint();

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Maximum protocol fee that can ever be set, protecting wallet owners from runaway fees.
    /// @dev USD with 18 decimals: 15e16 is $0.15 per execution. A ceiling in USD is the meaningful
    ///      one — a wei ceiling would tighten or loosen on its own every time ETH moved.
    uint256 public constant MAX_PROTOCOL_FEE = 15e16;

    /// @notice Minimum protocol fee that can ever be set. Guarantees SessionHandler's fee transfer is
    ///         never a zero-value call, so the fee path always records value.
    /// @dev USD with 18 decimals: 15e15 is $0.015 per execution. Converted through {getFee}, this
    ///      floor only truncates to zero wei at an ETH price around 1e16 USD, so the guarantee holds
    ///      for any price this protocol will ever see.
    uint256 public constant MIN_PROTOCOL_FEE = 15e15;

    /// @notice USD-denominated fee (18 decimals) charged on every session-key execution across all
    ///         wallets. Read {getFee} for the native amount a wallet actually transfers.
    uint256 public protocolFee;

    /// @notice Id of the SessionHandler ERC-4337 AI agent registered on ERC-8004 Identity Registery
    uint256 public agentId;

    /// @notice Delay between proposing an oracle change and being able to commit it.
    /// @dev The oracle governs every wallet's spending cap, so a repoint is the single highest-impact
    ///      action the operator key can take (THREAT_MODEL §3.8). The delay makes it publicly
    ///      observable before it binds. The cost: a genuinely broken live oracle cannot be replaced
    ///      for this long — wallet owners can {SessionHandler-pause} in the meantime.
    uint256 public constant ORACLE_TIMELOCK = 2 days;

    
    /// @notice Timestamp from which {pendingPriceOracle} may be committed. Meaningless when there is
    ///         no pending proposal.
    uint256 public pendingPriceOracleEta;

    /// @notice SpendingLimitModule installed as a hook on every SessionHandler deployed from here on.
    /// @dev Settable rather than immutable, and deliberately NOT a constructor argument: the module's
    ///      own constructor reads `priceOracle()` off this registry, so the registry must exist first.
    ///      Taking the module here would make the two mutually undeployable. The deploy script sets it
    ///      immediately after constructing the module; SHFactory.deployWallet reverts while it is unset.
    ///      Changing it only affects wallets deployed afterwards — existing wallets keep the module
    ///      they were initialized with, since SessionHandler copies it into its own storage.
    address public spendingLimitModule;

    /// @notice Oracle awaiting commit, or address(0) when no proposal is outstanding.
    address public pendingPriceOracle;

    /// @notice Address that receives protocol fees collected by SessionHandler wallets.
    address public treasury;

    /// @notice Canonical SHOracle used by all SessionHandler wallets for USD spending limit enforcement.
    address public priceOracle;

    /// @notice The SHFactory that deploys SessionHandler wallets against this registry.
    /// @dev Recorded for off-chain discoverability — no contract here reads it. Set after deployment
    ///      because the factory takes this registry's address in its own constructor.
    address public factory;

    /// @notice The canonical ERC-4337 EntryPoint that deployed wallets validate UserOps against.
    /// @dev Immutable, like the two ERC-8004 registries below: SessionHandler.initialize copies this
    ///      into the wallet's own storage at deploy time, so a later change here could never reach an
    ///      existing wallet anyway. Making that permanence explicit is more honest than a setter that
    ///      silently applies only to future wallets.
    address public immutable ENTRY_POINT;

    /// @notice Reputation Registry baked into every SessionHandler deployed from this registry.
    address public immutable REPUTATION_REGISTRY;
    /// @notice ERC-8004 Identity Registry baked into every SessionHandler deployed from this registry.
    address public immutable IDENTITY_REGISTRY;

    
    /*//////////////////////////////////////////////////////////////
                                  EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when the protocol fee is updated.
    /// @param oldFee The previous fee, in USD with 18 decimals.
    /// @param newFee The new fee, in USD with 18 decimals.
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
     *      until it is set.
     * @dev Rejects an initial oracle that cannot price native, the same check {proposePriceOracle}
     *      makes on a replacement. Two separate paths depend on the native feed: SpendingLimitModule
     *      meters the account's native balance delta, and {getFee} prices the protocol fee. An oracle
     *      without it would leave every deployed wallet unable to execute at all, so it is caught at
     *      construction rather than at the first user transaction. It also rejects any address with
     *      no isPriced() to call, catching a wrong-network or mistyped oracle address.
     * @param initialOwner       Address that will own this registry — the SHTreasury.
     * @param initialFee         Starting protocol fee, in USD with 18 decimals. Must be within
     *        [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE].
     * @param initialTreasury    Address that will receive protocol fees. Must not be address(0).
     * @param initialOracle      Address of the deployed SHOracle. Must not be address(0), and must be
     *        able to price native — see the native-feed check below.
     * @param reputationRegistry Reputation Registry baked into every wallet. Must not be address(0).
     * @param identityRegistry   ERC-8004 Identity Registry baked into every wallet. Must not be address(0).
     * @param entryPointAddress  Canonical ERC-4337 EntryPoint. Must not be address(0).
     * @param initialAgentId     Id of the SessionHandler agent on the ERC-8004 Identity Registry. Zero is
     *        a VALID id — ERC-8004 registries mint from 0, so the first agent registered holds id 0.
     */
    constructor(
        address initialOwner,
        uint256 initialFee,
        address initialTreasury,
        address initialOracle,
        address reputationRegistry,
        address identityRegistry,
        address entryPointAddress,
        uint256 initialAgentId
    ) Ownable(initialOwner) {
        if (initialFee > MAX_PROTOCOL_FEE || initialFee < MIN_PROTOCOL_FEE) {
            revert SHRegistry_FeeNotInRange();
        }
        if (initialTreasury == address(0)) revert SHRegistry_InvalidTreasury();
        if (initialOracle == address(0)) revert SHRegistry_InvalidPriceOracle();
        if (!SHOracle(initialOracle).isPriced(address(0))) revert SHRegistry_InvalidPriceOracle();
        if (entryPointAddress == address(0)) revert SHRegistry_InvalidEntryPoint();
        if (reputationRegistry == address(0)) revert SHRegistry_InvalidReputationRegistry();
        if (identityRegistry == address(0)) revert SHRegistry_InvalidIdentityRegistry();
        IDENTITY_REGISTRY = identityRegistry;
        REPUTATION_REGISTRY = reputationRegistry;
        ENTRY_POINT = entryPointAddress;
        protocolFee = initialFee;
        treasury = initialTreasury;
        priceOracle = initialOracle;
        agentId = initialAgentId;
    }

    /*//////////////////////////////////////////////////////////////
                            EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Updates the protocol fee charged on every session-key execution. Only callable by the owner.
     * @dev Takes effect protocol-wide on the next execution — wallets read the fee from here rather
     *      than storing it. The value is USD, so it does not need revisiting when ETH moves; {getFee}
     *      re-derives the native amount on every call.
     * @param newFee The new fee, in USD with 18 decimals. Must be within
     *        [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE].
     */
    function setProtocolFee(uint256 newFee) external onlyOwner {
        if (newFee > MAX_PROTOCOL_FEE || newFee < MIN_PROTOCOL_FEE) revert SHRegistry_FeeNotInRange();
        uint256 oldFee = protocolFee;
        protocolFee = newFee;
        emit ProtocolFeeUpdated(oldFee, newFee);
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
        pendingPriceOracleEta = block.timestamp + ORACLE_TIMELOCK;
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
     * @notice The protocol fee for one execution, converted to native at the current oracle price.
     * @dev What SessionHandler actually transfers to the treasury. Resolved live on every call — off
     *      the registry's CURRENT priceOracle, so a committed oracle swap changes it immediately, and
     *      off the CURRENT protocolFee, so a {setProtocolFee} lands without touching any wallet.
     * @dev Reverts if the native feed is stale or unregistered (see {SHOracle-getNativeFee}). Because
     *      every session-key execution charges the fee, that makes the ETH/USD feed's freshness a
     *      liveness dependency for session execution as a whole, not only for native-moving calls as
     *      it was when the fee was a flat wei amount. Wallet owners can still {SessionHandler-pause}
     *      and the owner path never charges a fee, so an owner keeps full access to a wallet
     *      throughout. See THREAT_MODEL.md §3.7.
     * @return The fee in wei at the oracle's current price.
     */
    function getFee() external view returns (uint256) {
        return SHOracle(priceOracle).getNativeFee(protocolFee);
    }
}
