// SPDX-License-Identifier: BUSL-1.1
// Copyright (C) 2026 Conrad Japhet
// Use of this software is governed by the Business Source License included in the LICENSE file.
// Change Date: 2029-06-12. Change License: MIT.
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {SHOracle} from "./SHOracle.sol";

/**
 * @title SHRegistry
 * @author Conrad Japhet
 * @notice Central configuration registry for the SessionHandler Protocol. Stores the
 *         protocol fee, treasury address, price oracle, and agent identity used across all
 *         deployed SessionHandler wallets.
 * @dev SessionHandler wallets read the MUTABLE parameters here (fee, treasury, price oracle, agent
 *      id) at execution time rather than storing them, so an update propagates instantly to every
 *      deployed wallet without redeployment. Two kinds of address do not work that way: the
 *      EntryPoint and both ERC-8004 registries are immutable here and baked into the SessionHandler
 *      implementation by SHFactory, and the SpendingLimitModule is copied into each wallet when it is
 *      deployed, so changing it only affects wallets deployed afterwards.
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
 */
contract SHRegistry is Ownable {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when a proposed protocolFee falls outside [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE].
    error SHRegistry_FeeNotInRange();
    /// @dev Thrown at construction when the fee bounds are unusable: a zero floor (which would allow
    ///      a zero-value fee transfer), a floor above the ceiling, or a ceiling past uint96, the
    ///      width {protocolFee} is stored in.
    error SHRegistry_InvalidFeeBounds();
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

    /// @notice Maximum protocol fee that can ever be set, in wei, protecting wallet owners from
    ///         runaway fees.
    /// @dev Immutable, so no later owner action — including a compromised owner key — can raise it.
    ///      It deliberately does not consult the oracle: the owner also controls the oracle's feeds,
    ///      so a ceiling checked in dollars could be lifted by faking a low native price first.
    uint256 public immutable MAX_PROTOCOL_FEE;

    /// @notice Minimum protocol fee that can ever be set, in wei. Guarantees SessionHandler's fee
    ///         transfer is never a zero-value call, so the fee path always records value.
    uint256 public immutable MIN_PROTOCOL_FEE;

    /// @notice Delay between proposing an oracle change and being able to commit it.
    /// @dev The oracle governs every wallet's spending cap, so a repoint is the single highest-impact
    ///      action the operator key can take (THREAT_MODEL §3.8). The delay makes it publicly
    ///      observable before it binds. The cost: a genuinely broken live oracle cannot be replaced
    ///      for this long — wallet owners can {SessionHandler-pause} in the meantime.
    uint256 public constant ORACLE_TIMELOCK = 2 days;

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

    /// @notice Address that receives protocol fees collected by SessionHandler wallets.
    address public treasury;
    /// @notice Fee in wei charged on every session-key execution across all wallets.
    /// @dev uint96 so it shares a slot with {treasury}: SessionHandler's fee path reads both on every
    ///      session-key execution, and packed they cost one storage read instead of two. The value is
    ///      bounded by {MAX_PROTOCOL_FEE}, which the constructor caps at uint96's maximum.
    uint96 public protocolFee;

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
     *      makes on a replacement. SpendingLimitModule meters the account's native balance delta, so
     *      an oracle without it would revert every native-moving execution on every deployed wallet;
     *      it is caught at construction rather than at the first user transaction. It also rejects
     *      any address with no isPriced() to call, catching a wrong-network or mistyped oracle address.
     * @param initialOwner       Address that will own this registry — the SHTreasury.
     * @param initialFee         Starting protocol fee, in wei. Must be within
     *        [minProtocolFee, maxProtocolFee].
     * @param minProtocolFee     Lowest fee the owner may ever set, in wei. Must be non-zero.
     * @param maxProtocolFee     Highest fee the owner may ever set, in wei. Must be at least
     *        `minProtocolFee` and fit in a uint96.
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
        uint256 minProtocolFee,
        uint256 maxProtocolFee,
        address initialTreasury,
        address initialOracle,
        address reputationRegistry,
        address identityRegistry,
        address entryPointAddress,
        uint256 initialAgentId
    ) Ownable(initialOwner) {
        if (minProtocolFee == 0 || minProtocolFee > maxProtocolFee || maxProtocolFee > type(uint96).max) {
            revert SHRegistry_InvalidFeeBounds();
        }
        if (initialFee > maxProtocolFee || initialFee < minProtocolFee) {
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
        MIN_PROTOCOL_FEE = minProtocolFee;
        MAX_PROTOCOL_FEE = maxProtocolFee;
        // Safe: range-checked above, and the ceiling was checked to fit uint96.
        // forge-lint: disable-next-line(unsafe-typecast)
        protocolFee = uint96(initialFee);
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
     *      than storing it. The value is wei, so its dollar value moves with the native price; this is
     *      how the operator brings it back in line.
     * @param newFee The new fee, in wei. Must be within
     *        [MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE].
     */
    function setProtocolFee(uint256 newFee) external onlyOwner {
        if (newFee > MAX_PROTOCOL_FEE || newFee < MIN_PROTOCOL_FEE) revert SHRegistry_FeeNotInRange();
        uint256 oldFee = protocolFee;
        // Safe: range-checked above, and the constructor capped MAX_PROTOCOL_FEE at uint96.
        // forge-lint: disable-next-line(unsafe-typecast)
        protocolFee = uint96(newFee);
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
     * @dev What SessionHandler transfers to the treasury. Read per call, so a {setProtocolFee} lands on
     *      every wallet at once. Makes no oracle call, so it cannot revert on a stale feed.
     * @return The fee in wei.
     */
    function getFee() external view returns (uint256) {
        return protocolFee;
    }
}
