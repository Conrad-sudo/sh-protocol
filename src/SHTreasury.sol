// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {SHRegistry} from "./SHRegistry.sol";
import {SHOracle} from "./SHOracle.sol";
import {SHFactory} from "./SHFactory.sol";

/**
 * @title SHTreasury
 * @author Conrad Japhet
 * @notice Receives protocol fees from SessionHandler wallets and is the protocol's single admin
 *         root: it owns the SHRegistry, the SHOracle, and the SHFactory, and is the only address
 *         that can drive their owner-only functions.
 * @dev The registry is deployed separately and wired in once via {setRegistry}, rather than being
 *      constructed here. That ordering is what lets every other contract be deployed already owned
 *      by this treasury instead of needing a follow-up transferOwnership: this contract is deployed
 *      FIRST, so its address is available as the `owner` argument to all of them.
 *
 *      Ownership graph:
 *        operator EOA → SHTreasury → { SHRegistry, SHOracle, SHFactory }
 *
 *      Fee flow:
 *        SessionHandler.execute() → payable(REGISTRY.treasury()).call{value: fee}()
 *                                 → SHTreasury.receive()
 *
 *      The fee is a flat wei amount set on the registry and read per execution through
 *      {SHRegistry-getFee}, so every execution pays the same wei until the operator changes it.
 *
 *      Admin flow:
 *        Protocol operator → SHTreasury.<passthrough>() → SHRegistry / SHOracle / SHFactory
 *
 *      The oracle and factory passthroughs take their target's address as an argument rather than
 *      reading a stored one. For the oracle that is what allows a REPLACEMENT oracle to be seeded
 *      with feeds while it sits pending in {SHRegistry-proposePriceOracle}'s timelock, before it
 *      becomes the live one; for the factory it means the protocol can run more than one factory
 *      without this contract needing to be redeployed to learn about each.
 */
contract SHTreasury is Ownable, ReentrancyGuard {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when a withdrawal ETH transfer fails.
    error SHTreasury_WithdrawalFailed();
    /// @dev Thrown when the requested withdrawal amount exceeds the contract balance.
    error SHTreasury_InsufficientBalance();
    /// @dev Thrown when a zero withdrawal amount is requested.
    error SHTreasury_InvalidAmount();
    /// @dev Thrown when address(0) is passed as the withdrawal recipient.
    error SHTreasury_InvalidRecipient();
    /// @dev Thrown when address(0) is passed to {setRegistry}.
    error SHTreasury_InvalidRegistry();
    /// @dev Thrown when {setRegistry} is called after the registry has already been wired in. The
    ///      registry is set exactly once, at deployment, so it is effectively immutable afterwards.
    error SHTreasury_RegistryAlreadySet();
    /// @dev Thrown when a passthrough is called before {setRegistry} has wired in the registry.
    error SHTreasury_RegistryNotSet();
    /// @dev Thrown when address(0) is passed as the new owner to {transferProtocol}, which would
    ///      renounce protocol admin outright rather than migrate it.
    error SHTreasury_InvalidNewOwner();
    /// @dev Thrown by {transferProtocol} when the registry has no factory recorded yet.
    error SHTreasury_InvalidFactory();

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice The SHRegistry owned by this treasury.
    /// @dev Typed rather than a bare address so every passthrough below reads as a plain call.
    ///      Not `immutable`, and NOT a constructor argument, only because this contract must be
    ///      deployed BEFORE the registry — the registry takes this address as both its owner and its
    ///      fee recipient, so it cannot exist first. {setRegistry} writes this exactly once and it
    ///      can never change afterwards, so it carries the same guarantee an immutable would.
    SHRegistry public REGISTRY;

    /*//////////////////////////////////////////////////////////////
                                  EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when accumulated fees are withdrawn to a recipient.
    /// @param recipient The address that received the ETH.
    /// @param amount    The amount withdrawn in wei.
    event FeesWithdrawn(address indexed recipient, uint256 amount);

    /// @notice Emitted once, at deployment, when the registry is wired in.
    /// @param registry The SHRegistry this treasury administers.
    event RegistrySet(address indexed registry);

    /*//////////////////////////////////////////////////////////////
                               CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Deploys the treasury, owned by the protocol operator.
     * @dev Deliberately takes no dependencies: this contract is deployed FIRST so that its address
     *      can be passed as the `owner` argument to the SHOracle, SHRegistry, and SHFactory
     *      constructors. The registry is wired back in afterwards with {setRegistry}.
     */
    constructor() Ownable(msg.sender) {}

    /// @dev Reverts if the registry has not been wired in yet. Guards every passthrough, so an
    ///      admin call made before {setRegistry} fails loudly instead of calling into address(0).
    modifier registrySet() {
        if (address(REGISTRY) == address(0)) revert SHTreasury_RegistryNotSet();
        _;
    }

    /**
     * @notice Wires in the SHRegistry this treasury administers. Callable once, by the owner.
     * @dev Called by the deploy script immediately after the registry is constructed. Permanent:
     *      a second call reverts, so the registry cannot be swapped out from under the wallets
     *      that read it.
     * @param registry The deployed SHRegistry, which must already have this contract as its owner.
     */
    function setRegistry(address registry) external onlyOwner {
        if (registry == address(0)) revert SHTreasury_InvalidRegistry();
        if (address(REGISTRY) != address(0)) revert SHTreasury_RegistryAlreadySet();
        REGISTRY = SHRegistry(registry);
        emit RegistrySet(registry);
    }

    /*//////////////////////////////////////////////////////////////
                            RECEIVE FUNCTION
    //////////////////////////////////////////////////////////////*/

    /// @notice Accepts ETH fee payments from SessionHandler wallets.
    /// @dev Keeps no running total: one would cost every session-key execution ~5,000 gas, and each
    ///      wallet's {SessionHandler-ProtocolFeePaid} event already records the fee it paid.
    receive() external payable {}

    /*//////////////////////////////////////////////////////////////
                           EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Withdraws ETH fees to a specified recipient. Only callable by the owner.
     * @param recipient Address to send ETH to. Must not be address(0).
     * @param amount    Amount in wei to withdraw. Must not exceed contract balance.
     */
    function withdraw(address recipient, uint256 amount) external onlyOwner nonReentrant {
        if (recipient == address(0)) revert SHTreasury_InvalidRecipient();
        if (amount == 0) revert SHTreasury_InvalidAmount();
        if (address(this).balance < amount) revert SHTreasury_InsufficientBalance();
        (bool success,) = payable(recipient).call{value: amount}("");
        if (!success) revert SHTreasury_WithdrawalFailed();
        emit FeesWithdrawn(recipient, amount);
    }

    /**
     * @notice Withdraws the entire ETH balance to a recipient. Only callable by the owner.
     * @param recipient Address to send ETH to. Must not be address(0).
     */
    function withdrawAll(address recipient) external onlyOwner nonReentrant {
        if (recipient == address(0)) revert SHTreasury_InvalidRecipient();
        uint256 balance = address(this).balance;
        if (balance == 0) revert SHTreasury_InsufficientBalance();
        (bool success,) = payable(recipient).call{value: balance}("");
        if (!success) revert SHTreasury_WithdrawalFailed();
        emit FeesWithdrawn(recipient, balance);
    }

    /*//////////////////////////////////////////////////////////////
                         REGISTRY ADMIN
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Hands ownership of the registry, the live oracle, and the recorded factory to
     *         `newOwner` in one transaction. Only callable by the owner.
     * @dev The migration path to a successor treasury or a multi-sig. ONE-WAY: this contract loses
     *      the ability to drive any of the three the moment it returns, so every passthrough below
     *      starts reverting and only `newOwner` can undo it. Pass an address you control and have
     *      tested — a wrong one strands protocol admin permanently.
     * @dev Deliberately does NOT move this contract's own ownership, nor redirect the fee stream:
     *      the registry's `treasury` still points here, so fees keep arriving and {withdraw} keeps
     *      working for the current owner. Call {setTreasury} FIRST if the intent is to move the fee
     *      stream too — afterwards this contract can no longer reach it.
     * @dev Reverts if the registry has no factory recorded, since {SHRegistry-setFactory} is a
     *      post-deployment step and calling into address(0) would revert unhelpfully.
     * @param newOwner The address to receive ownership of all three. Must not be address(0).
     */
    function transferProtocol(address newOwner) external onlyOwner registrySet {
        if (newOwner == address(0)) revert SHTreasury_InvalidNewOwner();
        address factory = REGISTRY.factory();
        if (factory == address(0)) revert SHTreasury_InvalidFactory();

        SHOracle(REGISTRY.priceOracle()).transferOwnership(newOwner);
        SHFactory(factory).transferOwnership(newOwner);
        // Last: once the registry moves, priceOracle()/factory() are still readable but this
        // contract can no longer administer anything, so nothing above could be retried.
        REGISTRY.transferOwnership(newOwner);
    }

    /**
     * @notice Updates the protocol fee charged on every session-key execution. Only callable by the owner.
     * @dev The fee is a flat wei amount, so its dollar value moves with the native price; calling
     *      this again is how the operator brings it back in line.
     * @param newFee The new fee, in wei. Must be within
     *               [SHRegistry.MIN_PROTOCOL_FEE, SHRegistry.MAX_PROTOCOL_FEE].
     */
    function setProtocolFee(uint256 newFee) external onlyOwner registrySet {
        REGISTRY.setProtocolFee(newFee);
    }

    /**
     * @notice Updates the keeper fee, charged on top of the protocol fee each time an executor module
     *         runs a payment. Only callable by the owner.
     * @dev Pays back the gas the protocol's backend spends sending those payments. Like the protocol
     *      fee it is a flat wei amount, re-set here when gas or the native price drifts.
     * @param newFee The new fee, in wei. Must be within
     *               [SHRegistry.MIN_KEEPER_FEE, SHRegistry.MAX_KEEPER_FEE] and a whole number of gwei.
     */
    function setKeeperFee(uint256 newFee) external onlyOwner registrySet {
        REGISTRY.setKeeperFee(newFee);
    }

    /**
     * @notice Proposes a new canonical SHOracle for all SessionHandler wallets, starting the
     *         registry's {SHRegistry-ORACLE_TIMELOCK} delay. Only callable by the owner.
     * @dev Changing the oracle is a two-step, delayed operation: propose here, then
     *      {commitPriceOracle} once the delay elapses, or {cancelPriceOracle} to withdraw. The
     *      oracle governs every wallet's spending cap, so the delay makes the change publicly
     *      observable before it binds (THREAT_MODEL §3.8).
     * @param newOracle The new SHOracle address. Must not be address(0) and must price native.
     */
    function proposePriceOracle(address newOracle) external onlyOwner registrySet {
        REGISTRY.proposePriceOracle(newOracle);
    }

    /**
     * @notice Commits the pending oracle proposal once its delay has elapsed. Only callable by the owner.
     * @dev Reverts if no proposal is outstanding or the ETA has not passed. Takes effect on every
     *      deployed wallet's next valuation.
     */
    function commitPriceOracle() external onlyOwner registrySet {
        REGISTRY.commitPriceOracle();
    }

    /**
     * @notice Withdraws the pending oracle proposal before it is committed. Only callable by the owner.
     */
    function cancelPriceOracle() external onlyOwner registrySet {
        REGISTRY.cancelPriceOracle();
    }

    /**
     * @notice Redirects future fee payments to a new treasury address. Only callable by the owner.
     * @dev Use this when migrating to a new treasury contract. After calling this, fees will no
     *      longer flow to this contract — ensure the new treasury is ready before calling.
     * @param newTreasury The new treasury address. Must not be address(0).
     */
    function setTreasury(address newTreasury) external onlyOwner registrySet {
        REGISTRY.setTreasury(newTreasury);
    }

    /**
     * @notice Updates the registered agentId for the SessionHandler Protocol. Only callable by the owner.
     * @dev Any id is accepted, 0 and the current one included: see {SHRegistry-setAgentId}.
     * @param newId The new agentId.
     */
    function setAgentId(uint256 newId) external onlyOwner registrySet {
        REGISTRY.setAgentId(newId);
    }

    /*//////////////////////////////////////////////////////////////
                              ORACLE ADMIN
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Proposes registering or repointing a price feed on an SHOracle this treasury owns,
     *         starting its {SHOracle-FEED_TIMELOCK}. Only callable by the owner.
     * @dev `oracle` is an argument rather than the registry's current oracle so a feed can be
     *      corrected on any oracle this treasury owns. No feed change binds before its delay: prices
     *      decide what the spending cap charges and what the PayeeGuard's trade floors allow
     *      (THREAT_MODEL §3.8).
     * @param oracle    The SHOracle to write to. Must be owned by this treasury.
     * @param token     Token to price. Use address(0) for native.
     * @param priceFeed Chainlink USD aggregator for it. Must not be address(0).
     * @param heartbeat That feed's Chainlink-published heartbeat, in seconds. Must be > 0.
     */
    function proposeOracleFeed(address oracle, address token, address priceFeed, uint256 heartbeat)
        external
        onlyOwner
    {
        SHOracle(oracle).proposeFeed(token, priceFeed, heartbeat);
    }

    /**
     * @notice Proposes deregistering a token from an SHOracle this treasury owns, starting its
     *         {SHOracle-FEED_TIMELOCK}. Only callable by the owner.
     * @dev The oracle refuses to remove the native feed. Accounts watching `token` will revert on
     *      their next transaction that moves it once this is committed — have them unwatch it first.
     * @param oracle The SHOracle to write to. Must be owned by this treasury.
     * @param token  The token to stop pricing. Must not be address(0).
     */
    function proposeOracleFeedRemoval(address oracle, address token) external onlyOwner {
        SHOracle(oracle).proposeFeedRemoval(token);
    }

    /**
     * @notice Applies a pending feed change on an SHOracle this treasury owns, once its delay has
     *         passed. Only callable by the owner.
     * @param oracle The SHOracle to write to.
     * @param token  The token whose pending change to apply.
     */
    function commitOracleFeed(address oracle, address token) external onlyOwner {
        SHOracle(oracle).commitFeed(token);
    }

    /**
     * @notice Withdraws a pending feed change on an SHOracle this treasury owns. Only callable by
     *         the owner.
     * @param oracle The SHOracle to write to.
     * @param token  The token whose pending change to withdraw.
     */
    function cancelOracleFeed(address oracle, address token) external onlyOwner {
        SHOracle(oracle).cancelFeed(token);
    }

    /*//////////////////////////////////////////////////////////////
                             APP CHECKS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Proposes the check the PayeeGuard uses for calls a passkey or agent makes to `app`,
     *         starting the {SHRegistry-APP_CHECK_TIMELOCK}. Only callable by the owner.
     * @dev Adding or replacing a check can only widen what wallets may do, so it waits two days, in
     *      public, before it binds on every wallet.
     * @param app   The contract the check is for (a router, say).
     * @param check The IAppCheck contract whose {IAppCheck-app} is `app`.
     */
    function proposeAppCheck(address app, address check) external onlyOwner registrySet {
        REGISTRY.proposeAppCheck(app, check);
    }

    /**
     * @notice Applies the pending check for `app` once its delay has passed. Only callable by the owner.
     * @param app The contract whose pending check to apply.
     */
    function commitAppCheck(address app) external onlyOwner registrySet {
        REGISTRY.commitAppCheck(app);
    }

    /**
     * @notice Withdraws the pending check for `app` before it is committed. Only callable by the owner.
     * @param app The contract whose pending check to withdraw.
     */
    function cancelAppCheck(address app) external onlyOwner registrySet {
        REGISTRY.cancelAppCheck(app);
    }

    /**
     * @notice Removes the live check for `app` at once, so passkeys and agents can no longer call it.
     *         Only callable by the owner.
     * @dev Instant on purpose: removing only ever takes ability away, so it is the emergency switch for
     *      a check found to have a bug. A check still pending for `app` is left alone; cancel it too.
     * @param app The contract whose check to remove.
     */
    function removeAppCheck(address app) external onlyOwner registrySet {
        REGISTRY.removeAppCheck(app);
    }

    /*//////////////////////////////////////////////////////////////
                             FACTORY ADMIN
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Halts new wallet deployments on a factory this treasury owns. Only callable by the owner.
     * @dev `factory` is an argument so the protocol can run more than one factory (e.g. alongside a
     *      successor) without redeploying this contract to learn about each.
     * @param factory The SHFactory to pause. Must be owned by this treasury.
     */
    function pauseFactory(address factory) external onlyOwner {
        SHFactory(factory).pause();
    }

    /**
     * @notice Resumes wallet deployments on a factory this treasury owns. Only callable by the owner.
     * @param factory The SHFactory to unpause. Must be owned by this treasury.
     */
    function unpauseFactory(address factory) external onlyOwner {
        SHFactory(factory).unpause();
    }

    /**
     * @notice Sets the SpendingLimitModule installed on wallets deployed from this point on. Only
     *         callable by the owner.
     * @dev Lives on the REGISTRY, not the factory: every factory reads the module from the registry,
     *      so one call covers them all. Does not touch wallets already deployed — they keep the
     *      module they were initialized with.
     * @param newModule The deployed SpendingLimitModule address. Must not be address(0).
     */
    function setSpendingLimitModule(address newModule) external onlyOwner registrySet {
        REGISTRY.setSpendingLimitModule(newModule);
    }

    /**
     * @notice Sets the RecurringPaymentModule installed as an executor on wallets deployed from this
     *         point on. Only callable by the owner.
     * @dev Lives on the registry for the same reason as {setSpendingLimitModule}. Does not touch
     *      wallets already deployed — they keep the executor they were initialized with.
     * @param newModule The deployed RecurringPaymentModule address. Must not be address(0).
     */
    function setRecurringPaymentModule(address newModule) external onlyOwner registrySet {
        REGISTRY.setRecurringPaymentModule(newModule);
    }

    /**
     * @notice Sets the AgentModule installed as a validator on wallets deployed from this point on.
     *         Only callable by the owner.
     * @dev Lives on the registry for the same reason as {setSpendingLimitModule}. Does not touch
     *      wallets already deployed — they keep the validator they were initialized with.
     * @param newModule The deployed AgentModule address. Must not be address(0).
     */
    function setAgentModule(address newModule) external onlyOwner registrySet {
        REGISTRY.setAgentModule(newModule);
    }

    /**
     * @notice Records the SHFactory deploying wallets against the registry. Only callable by the owner.
     * @dev Called by the deploy script once the factory exists. Off-chain discoverability only.
     * @param newFactory The SHFactory address. Must not be address(0).
     */
    function setFactory(address newFactory) external onlyOwner registrySet {
        REGISTRY.setFactory(newFactory);
    }
}
