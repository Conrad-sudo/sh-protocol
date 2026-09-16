//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {DECIMALS, ETH_USD_PRICE} from "../../script/Constants.s.sol";

/**
 * @title SHAdminTest
 * @author Conrad Japhet
 * @notice Covers the protocol's admin surface: the SHTreasury-rooted ownership graph, the
 *         two-phase timelock on oracle changes, and SHOracle's feed admin.
 * @dev Shape notes worth knowing up front:
 *      - Ownership is a chain, not a star: config.account owns SHTreasury, and SHTreasury owns the
 *        SHRegistry, SHOracle, and SHFactory. So the operator calling a registry/oracle/factory
 *        function DIRECTLY reverts with OwnableUnauthorizedAccount — every admin action goes
 *        through a treasury passthrough. Several tests below assert exactly that.
 *      - MockV3Aggregator stamps updatedAt at write time, so any test that warps past
 *        ORACLE_TIMELOCK (2 days) leaves every mock feed stale and getPrice reverting for reasons
 *        unrelated to what is under test. _warpPastTimelock re-stamps the feeds it is given.
 *      - proposePriceOracle rejects an oracle that cannot price native, so replacement oracles
 *        built here always register address(0).
 */
contract SHAdminTest is Test {
    SHFactory factory;
    SHTreasury treasury;
    SHRegistry registry;
    SHOracle oracle;
    HelperConfig.NetworkConfig config;
    SessionHandler wallet;

    address owner;
    address rando = makeAddr("rando");

    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;

    /// @dev Deliberately different from ETH_USD_PRICE so a committed oracle swap is observable in
    ///      what a live wallet prices native at.
    int256 constant NEW_ETH_USD_PRICE = 4000e8;

    /// @dev The two answers an L2 Sequencer Uptime Feed reports. Not prices — a status flag.
    int256 constant SEQ_UP = 0;
    int256 constant SEQ_DOWN = 1;

    /// @dev Timestamps for the sequencer tests. Foundry starts block.timestamp at 1, so dating an
    ///      uptime round in the past needs room to subtract into: the tests warp to SEQ_NOW and
    ///      place the sequencer's recovery at SEQ_UP_SINCE, i.e. up for 9 days — comfortably past
    ///      SHOracle.SEQUENCER_GRACE_PERIOD, so only the branch under test can revert.
    uint256 constant SEQ_NOW = 10 days;
    uint256 constant SEQ_UP_SINCE = 1 days;

    function setUp() public {
        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        registry = treasury.REGISTRY();
        owner = config.account;

        address[] memory watched = new address[](1);
        watched[0] = config.usdc;
        vm.prank(owner);
        wallet =
            SessionHandler(payable(factory.deployWallet(DAILY_LIMIT, WINDOW, watched, address(0), new address[](0))));
    }

    /*//////////////////////////////////////////////////////////////
                            OWNERSHIP GRAPH
    //////////////////////////////////////////////////////////////*/

    function test_treasuryOwnsRegistryOracleAndFactory() public view {
        assertEq(treasury.owner(), owner, "operator owns the treasury");
        assertEq(registry.owner(), address(treasury), "treasury owns the registry");
        assertEq(oracle.owner(), address(treasury), "treasury owns the oracle");
        assertEq(factory.owner(), address(treasury), "treasury owns the factory");
    }

    function test_registryPaysFeesToTheTreasury() public view {
        assertEq(registry.treasury(), address(treasury));
    }

    function test_registrySetOnceAtDeploy_cannotBeReset() public {
        vm.prank(owner);
        vm.expectRevert(SHTreasury.SHTreasury_RegistryAlreadySet.selector);
        treasury.setRegistry(makeAddr("otherRegistry"));
    }

    /// @dev The whole point of the chain: the operator's key is not itself an admin of anything
    ///      below the treasury, so a direct call is rejected even though it is the human in charge.
    function test_operatorCannotCallOwnedContractsDirectly() public {
        // Read before expectRevert: an argument evaluated after it would be the call expectRevert
        // latches onto, and MIN_PROTOCOL_FEE() succeeds.
        uint256 minFee = registry.MIN_PROTOCOL_FEE();
        vm.startPrank(owner);

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, owner));
        registry.setProtocolFee(minFee);

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, owner));
        oracle.removeFeed(config.usdc);

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, owner));
        factory.pause();

        vm.stopPrank();
    }

    function test_treasuryPassthroughsReachOwnedContracts() public {
        vm.startPrank(owner);

        treasury.setProtocolFee(registry.MAX_PROTOCOL_FEE());
        assertEq(registry.protocolFee(), registry.MAX_PROTOCOL_FEE());

        treasury.pauseFactory(address(factory));
        assertTrue(factory.paused());
        treasury.unpauseFactory(address(factory));
        assertFalse(factory.paused());

        address newModule = makeAddr("newModule");
        treasury.setSpendingLimitModule(newModule);
        assertEq(registry.spendingLimitModule(), newModule);

        vm.stopPrank();
    }

    function test_treasuryPassthroughs_revertForNonOwner() public {
        vm.startPrank(rando);
        bytes memory err = abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando);

        vm.expectRevert(err);
        treasury.proposePriceOracle(address(oracle));
        vm.expectRevert(err);
        treasury.commitPriceOracle();
        vm.expectRevert(err);
        treasury.cancelPriceOracle();
        vm.expectRevert(err);
        treasury.setFeed(address(oracle), config.usdc, config.usdcUsdPriceFeed, config.usdcHeartbeat);
        vm.expectRevert(err);
        treasury.removeOracleFeed(address(oracle), config.usdc);
        vm.expectRevert(err);
        treasury.pauseFactory(address(factory));
        vm.expectRevert(err);
        treasury.setSpendingLimitModule(makeAddr("m"));
        vm.expectRevert(err);
        treasury.setFactory(makeAddr("f"));
        vm.expectRevert(err);
        treasury.transferProtocol(rando);

        vm.stopPrank();
    }

    /*//////////////////////////////////////////////////////////////
                       REGISTRY-SOURCED WALLET CONFIG
    //////////////////////////////////////////////////////////////*/

    /// @dev The factory stores none of these itself — it reads them off the registry at deploy time,
    ///      so a wallet's baked-in addresses must match what the registry held.
    function test_walletInheritsAddressesFromRegistry() public view {
        assertEq(wallet.ENTRY_POINT(), registry.ENTRY_POINT());
        assertEq(wallet.REPUTATION_REGISTRY(), registry.REPUTATION_REGISTRY());
        assertEq(wallet.IDENTITY_REGISTRY(), registry.IDENTITY_REGISTRY());
        assertEq(address(wallet.REGISTRY()), address(registry));
        assertEq(address(wallet.SH_MODULE()), registry.spendingLimitModule());
    }

    function test_factoryIsBoundToTheRegistry() public view {
        assertEq(address(factory.REGISTRY()), address(registry));
        assertEq(registry.factory(), address(factory), "deploy script records the factory");
    }

    /// @dev The module cannot be a registry constructor argument — SpendingLimitModule's own
    ///      constructor reads priceOracle() off the registry, so the two would be mutually
    ///      undeployable. It is set immediately after instead, which this asserts actually happened.
    function test_spendingLimitModuleIsSetAfterDeployment() public view {
        assertTrue(registry.spendingLimitModule() != address(0));
    }

    function test_setSpendingLimitModule_rejectsZero() public {
        vm.prank(owner);
        vm.expectRevert(SHRegistry.SHRegistry_InvalidSpendingLimitModule.selector);
        treasury.setSpendingLimitModule(address(0));
    }

    function test_setFactory_rejectsZero() public {
        vm.prank(owner);
        vm.expectRevert(SHRegistry.SHRegistry_InvalidFactory.selector);
        treasury.setFactory(address(0));
    }

    /// @dev Swapping the module only affects wallets deployed afterwards: SessionHandler copies it
    ///      into its own storage at initialize, so an existing wallet keeps what it installed.
    function test_moduleSwapDoesNotTouchExistingWallets() public {
        address oldModule = address(wallet.SH_MODULE());
        SpendingLimitModule fresh = new SpendingLimitModule(address(registry));

        vm.prank(owner);
        treasury.setSpendingLimitModule(address(fresh));

        assertEq(address(wallet.SH_MODULE()), oldModule, "existing wallet unchanged");
        assertEq(registry.spendingLimitModule(), address(fresh), "future wallets get the new one");
    }

    /*//////////////////////////////////////////////////////////////
                            PROTOCOL MIGRATION
    //////////////////////////////////////////////////////////////*/

    function test_transferProtocol_movesAllThreeOwnerships() public {
        address successor = makeAddr("successor");
        vm.prank(owner);
        treasury.transferProtocol(successor);

        assertEq(registry.owner(), successor);
        assertEq(oracle.owner(), successor);
        assertEq(factory.owner(), successor);
    }

    /// @dev One-way by design: the treasury can no longer administer anything afterwards.
    function test_transferProtocol_leavesTreasuryUnableToAdminister() public {
        address successor = makeAddr("successor");
        uint256 maxFee = registry.MAX_PROTOCOL_FEE(); // read before expectRevert, or it latches here
        vm.startPrank(owner);
        treasury.transferProtocol(successor);

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, address(treasury)));
        treasury.setProtocolFee(maxFee);
        vm.stopPrank();
    }

    /// @dev The fee stream is deliberately NOT moved — the registry still names this treasury.
    function test_transferProtocol_leavesFeeStreamIntact() public {
        address successor = makeAddr("successor");
        vm.prank(owner);
        treasury.transferProtocol(successor);

        assertEq(registry.treasury(), address(treasury));
    }

    function test_transferProtocol_rejectsZeroOwner() public {
        vm.prank(owner);
        vm.expectRevert(SHTreasury.SHTreasury_InvalidNewOwner.selector);
        treasury.transferProtocol(address(0));
    }

    /*//////////////////////////////////////////////////////////////
                         ORACLE TIMELOCK: PROPOSE
    //////////////////////////////////////////////////////////////*/

    function test_propose_recordsPendingWithoutChangingLiveOracle() public {
        (SHOracle candidate,) = _replacementOracle(NEW_ETH_USD_PRICE);

        vm.expectEmit(true, false, false, true, address(registry));
        emit SHRegistry.PriceOracleProposed(address(candidate), block.timestamp + registry.ORACLE_TIMELOCK());
        vm.prank(owner);
        treasury.proposePriceOracle(address(candidate));

        assertEq(registry.pendingPriceOracle(), address(candidate));
        assertEq(registry.pendingPriceOracleEta(), block.timestamp + registry.ORACLE_TIMELOCK());
        assertEq(registry.priceOracle(), address(oracle), "live oracle must not move on propose");
    }

    function test_propose_revertsOnZeroAddress() public {
        vm.prank(owner);
        vm.expectRevert(SHRegistry.SHRegistry_InvalidPriceOracle.selector);
        treasury.proposePriceOracle(address(0));
    }

    /// @dev The check that matters most: SpendingLimitModule prices the native delta on EVERY
    ///      metered transaction, so committing an oracle without a native feed would revert every
    ///      native-moving execution on every deployed wallet.
    function test_propose_revertsWhenOracleCannotPriceNative() public {
        address[] memory tokens = new address[](1);
        address[] memory feeds = new address[](1);
        uint256[] memory beats = new uint256[](1);
        tokens[0] = config.usdc;
        feeds[0] = config.usdcUsdPriceFeed;
        beats[0] = config.usdcHeartbeat;
        SHOracle noNative = new SHOracle(address(treasury), address(0), tokens, feeds, beats);

        assertFalse(noNative.isPriced(address(0)));
        vm.prank(owner);
        vm.expectRevert(SHRegistry.SHRegistry_InvalidPriceOracle.selector);
        treasury.proposePriceOracle(address(noNative));
    }

    /// @dev A wrong-network or mistyped address has no isPriced() to call, so the proposal reverts
    ///      rather than being accepted and bricking every wallet two days later.
    function test_propose_revertsOnAddressWithNoCode() public {
        vm.prank(owner);
        vm.expectRevert();
        treasury.proposePriceOracle(makeAddr("notAnOracle"));
    }

    function test_propose_replacesOutstandingProposalAndRestartsDelay() public {
        (SHOracle first,) = _replacementOracle(NEW_ETH_USD_PRICE);
        (SHOracle second,) = _replacementOracle(NEW_ETH_USD_PRICE);

        vm.prank(owner);
        treasury.proposePriceOracle(address(first));

        skip(1 days);
        vm.prank(owner);
        treasury.proposePriceOracle(address(second));

        assertEq(registry.pendingPriceOracle(), address(second));
        assertEq(registry.pendingPriceOracleEta(), block.timestamp + registry.ORACLE_TIMELOCK());
    }

    /*//////////////////////////////////////////////////////////////
                          ORACLE TIMELOCK: COMMIT
    //////////////////////////////////////////////////////////////*/

    function test_commit_revertsBeforeEta() public {
        (SHOracle candidate,) = _replacementOracle(NEW_ETH_USD_PRICE);
        vm.prank(owner);
        treasury.proposePriceOracle(address(candidate));
        uint256 eta = registry.pendingPriceOracleEta();

        skip(registry.ORACLE_TIMELOCK() - 1);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_TimelockNotElapsed.selector, eta));
        treasury.commitPriceOracle();

        assertEq(registry.priceOracle(), address(oracle));
    }

    /// @dev Boundary: the guard is `block.timestamp < eta`, so exactly at the ETA must succeed.
    function test_commit_succeedsExactlyAtEta() public {
        (SHOracle candidate,) = _replacementOracle(NEW_ETH_USD_PRICE);
        vm.prank(owner);
        treasury.proposePriceOracle(address(candidate));

        vm.warp(registry.pendingPriceOracleEta());
        vm.prank(owner);
        treasury.commitPriceOracle();

        assertEq(registry.priceOracle(), address(candidate));
    }

    function test_commit_clearsPendingStateAndEmits() public {
        (SHOracle candidate,) = _replacementOracle(NEW_ETH_USD_PRICE);
        vm.prank(owner);
        treasury.proposePriceOracle(address(candidate));
        skip(registry.ORACLE_TIMELOCK());

        vm.expectEmit(true, true, false, false, address(registry));
        emit SHRegistry.PriceOracleUpdated(address(oracle), address(candidate));
        vm.prank(owner);
        treasury.commitPriceOracle();

        assertEq(registry.pendingPriceOracle(), address(0));
        assertEq(registry.pendingPriceOracleEta(), 0);
    }

    function test_commit_revertsWithNoPendingProposal() public {
        vm.prank(owner);
        vm.expectRevert(SHRegistry.SHRegistry_NoPendingOracle.selector);
        treasury.commitPriceOracle();
    }

    function test_commit_twiceReverts() public {
        (SHOracle candidate,) = _replacementOracle(NEW_ETH_USD_PRICE);
        vm.prank(owner);
        treasury.proposePriceOracle(address(candidate));
        skip(registry.ORACLE_TIMELOCK());

        vm.startPrank(owner);
        treasury.commitPriceOracle();
        vm.expectRevert(SHRegistry.SHRegistry_NoPendingOracle.selector);
        treasury.commitPriceOracle();
        vm.stopPrank();
    }

    /*//////////////////////////////////////////////////////////////
                          ORACLE TIMELOCK: CANCEL
    //////////////////////////////////////////////////////////////*/

    function test_cancel_clearsPendingAndLeavesLiveOracleUntouched() public {
        (SHOracle candidate,) = _replacementOracle(NEW_ETH_USD_PRICE);
        vm.prank(owner);
        treasury.proposePriceOracle(address(candidate));

        vm.expectEmit(true, false, false, false, address(registry));
        emit SHRegistry.PriceOracleProposalCancelled(address(candidate));
        vm.prank(owner);
        treasury.cancelPriceOracle();

        assertEq(registry.pendingPriceOracle(), address(0));
        assertEq(registry.pendingPriceOracleEta(), 0);
        assertEq(registry.priceOracle(), address(oracle));
    }

    function test_cancel_revertsWithNoPendingProposal() public {
        vm.prank(owner);
        vm.expectRevert(SHRegistry.SHRegistry_NoPendingOracle.selector);
        treasury.cancelPriceOracle();
    }

    function test_cancelThenCommit_reverts() public {
        (SHOracle candidate,) = _replacementOracle(NEW_ETH_USD_PRICE);
        vm.startPrank(owner);
        treasury.proposePriceOracle(address(candidate));
        treasury.cancelPriceOracle();
        skip(registry.ORACLE_TIMELOCK());

        vm.expectRevert(SHRegistry.SHRegistry_NoPendingOracle.selector);
        treasury.commitPriceOracle();
        vm.stopPrank();
    }

    /*//////////////////////////////////////////////////////////////
                      END-TO-END: WHAT A WALLET READS
    //////////////////////////////////////////////////////////////*/

    /// @dev The property the timelock exists to provide: a live wallet keeps pricing against the
    ///      old oracle for the whole delay, and switches only once the change is committed.
    function test_walletReadsNewOracleOnlyAfterCommit() public {
        (SHOracle candidate, MockV3Aggregator newEthFeed) = _replacementOracle(NEW_ETH_USD_PRICE);

        vm.prank(owner);
        treasury.proposePriceOracle(address(candidate));

        MockV3Aggregator[] memory feeds = new MockV3Aggregator[](2);
        feeds[0] = MockV3Aggregator(config.ethUsdPriceFeed);
        feeds[1] = newEthFeed;
        _warpPastTimelock(feeds);

        assertEq(wallet.getUsdValue(address(0), 1 ether), int256(ETH_USD_PRICE) * 1e10, "still on the old oracle");

        vm.prank(owner);
        treasury.commitPriceOracle();

        assertEq(wallet.getUsdValue(address(0), 1 ether), NEW_ETH_USD_PRICE * 1e10, "switched on commit");
    }

    /*//////////////////////////////////////////////////////////////
                              PROTOCOL FEE
    //////////////////////////////////////////////////////////////*/

    /// @dev The conversion itself: protocolFee is USD (18 dec), getFee returns wei at the native
    ///      feed's current price. On anvil ETH is $1000, so the deploy script's $0.02 fee is 2e13 wei.
    function test_getFee_convertsUsdFeeToNativeAtOraclePrice() public view {
        uint256 usdFee = registry.protocolFee();
        uint256 usdPerEth = uint256(oracle.getPrice(address(0), 1 ether));

        assertEq(usdPerEth, uint256(ETH_USD_PRICE) * 1e10, "fixture ETH price moved");
        assertEq(registry.getFee(), (usdFee * 1e18) / usdPerEth, "getFee is not the USD/price quotient");
        // Pinned literal, so a silent decimals change in either direction fails here rather than
        // agreeing with a recomputed expectation: $0.02 at $1000/ETH is 0.00002 ETH.
        assertEq(registry.getFee(), 2e13, "$0.02 at $1000/ETH should be 2e13 wei");
    }

    /// @dev The property USD denomination exists to provide: the wei charged moves inversely with
    ///      the ETH price, so the DOLLAR cost of an execution stays put. A flat wei fee would do the
    ///      opposite — unchanged wei, doubled dollar cost.
    function test_getFee_movesInverselyWithEthPriceSoUsdCostIsConstant() public {
        uint256 feeBefore = registry.getFee();

        // Repoint native at 2x the price. setFeed is immediate and not timelocked (THREAT_MODEL 3.8).
        MockV3Aggregator doubled = new MockV3Aggregator(DECIMALS, ETH_USD_PRICE * 2);
        vm.prank(owner);
        treasury.setFeed(address(oracle), address(0), address(doubled), config.ethHeartbeat);

        uint256 feeAfter = registry.getFee();
        assertEq(feeAfter, feeBefore / 2, "wei charged did not halve when ETH doubled");

        // The invariant that actually matters: same dollars, both times.
        assertEq(
            uint256(oracle.getPrice(address(0), feeAfter)),
            registry.protocolFee(),
            "USD value of the fee drifted from the configured fee"
        );
    }

    /// @dev A fee change lands protocol-wide with no wallet redeployment, because wallets read
    ///      getFee() per execution rather than storing anything.
    function test_getFee_tracksSetProtocolFeeImmediately() public {
        uint256 maxFee = registry.MAX_PROTOCOL_FEE();
        vm.prank(owner);
        treasury.setProtocolFee(maxFee);

        assertEq(registry.getFee(), (maxFee * 1e18) / uint256(oracle.getPrice(address(0), 1 ether)));
    }

    /// @dev The liveness surface the USD fee introduced: getFee prices native, so a stale ETH/USD
    ///      feed makes the fee uncollectable rather than collectable at a wrong price. See the
    ///      end-to-end consequence in SessionGuardTest (every session execution reverts).
    function test_getFee_revertsWhenNativeFeedIsStale() public {
        skip(config.ethHeartbeat + 1);

        vm.expectRevert(SHOracle.PriceOracle_StalePrice.selector);
        registry.getFee();
    }

    /// @dev A non-positive answer is a feed malfunction, and must be rejected before the cast in
    ///      getNativeFee would turn it into an enormous divisor.
    function test_getFee_revertsOnNonPositiveNativePrice() public {
        MockV3Aggregator(config.ethUsdPriceFeed).updateAnswer(0);

        vm.expectRevert(SHOracle.PriceOracle_InvalidPrice.selector);
        registry.getFee();
    }

    /// @dev The bounds are USD figures now: $0.015 to $0.15 per execution.
    function test_setProtocolFee_enforcesUsdBounds() public {
        uint256 minFee = registry.MIN_PROTOCOL_FEE();
        uint256 maxFee = registry.MAX_PROTOCOL_FEE();
        assertEq(minFee, 15e15, "MIN is $0.015 at 18 decimals");
        assertEq(maxFee, 15e16, "MAX is $0.15 at 18 decimals");

        vm.startPrank(owner);
        vm.expectRevert(SHRegistry.SHRegistry_FeeNotInRange.selector);
        treasury.setProtocolFee(minFee - 1);

        vm.expectRevert(SHRegistry.SHRegistry_FeeNotInRange.selector);
        treasury.setProtocolFee(maxFee + 1);

        // Both bounds are inclusive.
        treasury.setProtocolFee(minFee);
        assertEq(registry.protocolFee(), minFee);
        treasury.setProtocolFee(maxFee);
        assertEq(registry.protocolFee(), maxFee);
        vm.stopPrank();
    }

    /// @dev The floor exists so the fee transfer is never zero-value. Confirm it survives the USD
    ///      conversion at a price far above anything real — $1M ETH still yields non-zero wei.
    function test_minFee_staysNonZeroInNativeAtExtremeEthPrices() public {
        MockV3Aggregator expensive = new MockV3Aggregator(DECIMALS, 1_000_000e8);
        vm.startPrank(owner);
        treasury.setFeed(address(oracle), address(0), address(expensive), config.ethHeartbeat);
        treasury.setProtocolFee(registry.MIN_PROTOCOL_FEE());
        vm.stopPrank();

        assertGt(registry.getFee(), 0, "floor truncated to a zero-value transfer");
    }

    /// @dev Same check proposePriceOracle makes on a replacement, now made at construction too: two
    ///      paths need the native feed (the module's balance metering and getFee), so an oracle
    ///      without it would leave every wallet deployed against this registry unable to execute.
    function test_constructor_revertsWhenOracleCannotPriceNative() public {
        address[] memory tokens = new address[](1);
        address[] memory feeds = new address[](1);
        uint256[] memory beats = new uint256[](1);
        tokens[0] = config.usdc;
        feeds[0] = config.usdcUsdPriceFeed;
        beats[0] = config.usdcHeartbeat;
        SHOracle noNative = new SHOracle(address(treasury), address(0), tokens, feeds, beats);
        uint256 minFee = registry.MIN_PROTOCOL_FEE(); // read before expectRevert, or it latches here

        vm.expectRevert(SHRegistry.SHRegistry_InvalidPriceOracle.selector);
        new SHRegistry(
            address(treasury),
            minFee,
            address(treasury),
            address(noNative),
            config.reputationRegistry,
            config.identityRegistry,
            config.entryPoint,
            0
        );
    }

    /// @dev The same check catches the likelier mistake: an address with no isPriced() to call —
    ///      a mistyped oracle, a wrong-network address, or the registry address passed to itself.
    function test_constructor_revertsOnOracleWithNoCode() public {
        uint256 minFee = registry.MIN_PROTOCOL_FEE(); // read before expectRevert, or it latches here
        address notAnOracle = makeAddr("notAnOracle");

        vm.expectRevert();
        new SHRegistry(
            address(treasury),
            minFee,
            address(treasury),
            notAnOracle,
            config.reputationRegistry,
            config.identityRegistry,
            config.entryPoint,
            0
        );
    }

    /*//////////////////////////////////////////////////////////////
                              FEED ADMIN
    //////////////////////////////////////////////////////////////*/

    function test_addFeed_registersAndPricesANewToken() public {
        ERC20Mock token = new ERC20Mock("Six", "SIX", 6);
        MockV3Aggregator feed = new MockV3Aggregator(DECIMALS, 2e8); // $2.00

        assertFalse(oracle.isPriced(address(token)));
        vm.prank(owner);
        treasury.setFeed(address(oracle), address(token), address(feed), 1 days);

        assertTrue(oracle.isPriced(address(token)));
        // 1000 tokens at 6 decimals, $2.00 each => $2000 with 18 decimals.
        assertEq(oracle.getPrice(address(token), 1000e6), 2000e18);
    }

    /// @dev Repointing in place is the supported way to correct a heartbeat or migrate aggregators;
    ///      a remove-then-add would leave the token unpriced in between.
    function test_addFeed_repointsAnExistingFeed() public {
        MockV3Aggregator replacement = new MockV3Aggregator(DECIMALS, 3000e8);

        vm.prank(owner);
        treasury.setFeed(address(oracle), address(0), address(replacement), 1 days);

        assertEq(oracle.getPrice(address(0), 1 ether), 3000e18);
    }

    function test_addFeed_revertsOnZeroFeed() public {
        vm.prank(owner);
        vm.expectRevert(SHOracle.PriceOracle_InvalidFeed.selector);
        treasury.setFeed(address(oracle), config.usdc, address(0), 1 days);
    }

    function test_addFeed_revertsOnZeroHeartbeat() public {
        vm.prank(owner);
        vm.expectRevert(SHOracle.PriceOracle_InvalidHeartbeat.selector);
        treasury.setFeed(address(oracle), config.usdc, config.usdcUsdPriceFeed, 0);
    }

    function test_addFeed_revertsOnHeartbeatPastUint48() public {
        vm.prank(owner);
        vm.expectRevert(SHOracle.PriceOracle_InvalidHeartbeat.selector);
        treasury.setFeed(address(oracle), config.usdc, config.usdcUsdPriceFeed, uint256(type(uint48).max) + 1);
    }

    function test_removeFeed_deregistersToken() public {
        assertTrue(oracle.isPriced(config.usdc));

        vm.prank(owner);
        treasury.removeOracleFeed(address(oracle), config.usdc);

        assertFalse(oracle.isPriced(config.usdc));
        vm.expectRevert(SHOracle.PriceOracle_UnsupportedToken.selector);
        oracle.getPrice(config.usdc, 1e6);
    }

    /// @dev Removing native would revert every native-moving execution on every wallet installed
    ///      against this oracle, so the oracle refuses outright.
    function test_removeFeed_revertsForNative() public {
        vm.prank(owner);
        vm.expectRevert(SHOracle.PriceOracle_CannotRemoveNativeFeed.selector);
        treasury.removeOracleFeed(address(oracle), address(0));
    }

    /*//////////////////////////////////////////////////////////////
                    L2 SEQUENCER UPTIME (SHOracle)
    //////////////////////////////////////////////////////////////*/

    /// @notice The happy path: a sequencer up for longer than the grace period prices exactly as
    ///         an L1 would, so the check is invisible in normal operation.
    function test_sequencerUp_pricesNormally() public {
        vm.warp(SEQ_NOW);
        SHOracle gated = _sequencerGatedOracle(address(_uptimeFeed(SEQ_UP, SEQ_UP_SINCE)));

        assertEq(gated.getPrice(address(0), 1 ether), int256(ETH_USD_PRICE) * 1e10);
    }

    /// @notice While the sequencer is DOWN every feed on the chain is frozen at its pre-outage
    ///         answer, so nothing may be priced — however fresh those answers still look to the
    ///         per-feed heartbeat check. This is the gap the heartbeat alone cannot see.
    function test_sequencerDown_blocksValuation() public {
        vm.warp(SEQ_NOW);
        SHOracle gated = _sequencerGatedOracle(address(_uptimeFeed(SEQ_DOWN, SEQ_UP_SINCE)));

        vm.expectRevert(SHOracle.PriceOracle_SequencerDown.selector);
        gated.getPrice(address(0), 1 ether);
    }

    /// @notice A round with startedAt == 0 was never initialised and reports no status at all.
    ///         "Cannot tell" must not read as "up".
    function test_sequencerUninitialisedRound_readsAsDown() public {
        vm.warp(SEQ_NOW);
        SHOracle gated = _sequencerGatedOracle(address(_uptimeFeed(SEQ_UP, 0)));

        vm.expectRevert(SHOracle.PriceOracle_SequencerDown.selector);
        gated.getPrice(address(0), 1 ether);
    }

    /// @notice The grace period at its exact boundary: blocked the instant the sequencer returns,
    ///         still blocked ON the boundary (the check is <=), clear one second later. This window
    ///         is what gives the feeds time to publish a post-outage price before anything is
    ///         valued against them.
    function test_sequencerJustRecovered_blockedUntilGraceElapses() public {
        vm.warp(SEQ_NOW);
        SHOracle gated = _sequencerGatedOracle(address(_uptimeFeed(SEQ_UP, block.timestamp)));
        uint256 grace = gated.SEQUENCER_GRACE_PERIOD();

        vm.expectRevert(SHOracle.PriceOracle_SequencerGracePeriod.selector);
        gated.getPrice(address(0), 1 ether);

        vm.warp(SEQ_NOW + grace);
        vm.expectRevert(SHOracle.PriceOracle_SequencerGracePeriod.selector);
        gated.getPrice(address(0), 1 ether);

        vm.warp(SEQ_NOW + grace + 1);
        assertEq(gated.getPrice(address(0), 1 ether), int256(ETH_USD_PRICE) * 1e10, "grace period never cleared");
    }

    /// @notice The fee path is gated too — SHRegistry.getFee converts through getNativeFee — so an
    ///         outage stops fee collection rather than striking the fee at a frozen price.
    function test_getNativeFee_blockedWhileSequencerIsDown() public {
        vm.warp(SEQ_NOW);
        SHOracle gated = _sequencerGatedOracle(address(_uptimeFeed(SEQ_DOWN, SEQ_UP_SINCE)));

        vm.expectRevert(SHOracle.PriceOracle_SequencerDown.selector);
        gated.getNativeFee(0.02e18);
    }

    /// @notice isPriced is a plain storage read and must stay answerable through an outage:
    ///         proposePriceOracle's native-pricing check calls it, and a candidate oracle should
    ///         not be unproposable just because the sequencer happens to be down.
    function test_isPriced_unaffectedBySequencerOutage() public {
        vm.warp(SEQ_NOW);
        SHOracle gated = _sequencerGatedOracle(address(_uptimeFeed(SEQ_DOWN, SEQ_UP_SINCE)));

        assertTrue(gated.isPriced(address(0)), "isPriced should not depend on the sequencer");
    }

    /// @notice On a chain with no sequencer the check is skipped outright — which is what keeps the
    ///         mainnet, BSC and Anvil configurations on their existing behaviour.
    function test_noUptimeFeed_skipsTheCheckEntirely() public view {
        assertEq(oracle.SEQUENCER_UPTIME_FEED(), address(0), "anvil config should configure no sequencer");
        assertEq(oracle.getPrice(address(0), 1 ether), int256(ETH_USD_PRICE) * 1e10);
    }

    /// @notice A mistyped or wrong-network uptime feed would otherwise pass construction and then
    ///         revert every valuation protocol-wide, fixable only through the two-day oracle swap.
    ///         The constructor probes it once so that becomes a failed deploy instead.
    function test_constructor_revertsOnUptimeFeedWithNoCode() public {
        // Everything expectRevert must NOT latch onto is built first: it applies to the next call
        // or CREATE, and the feed mock plus makeAddr would otherwise get there before the oracle.
        address notAFeed = makeAddr("notAnUptimeFeed");
        (address[] memory tokens, address[] memory feeds, uint256[] memory beats) = _nativeOnlyFeedArrays();

        vm.expectRevert();
        new SHOracle(address(treasury), notAFeed, tokens, feeds, beats);
    }

    /// @notice The likelier version of the same mistake: a PRICE feed passed where the uptime feed
    ///         belongs. Its answer is a price, not a 0/1 flag, so it would read as "sequencer down"
    ///         forever. Caught at construction.
    function test_constructor_revertsWhenGivenAPriceFeedAsUptimeFeed() public {
        address priceFeedByMistake = address(new MockV3Aggregator(DECIMALS, ETH_USD_PRICE));
        (address[] memory tokens, address[] memory feeds, uint256[] memory beats) = _nativeOnlyFeedArrays();

        vm.expectRevert(SHOracle.PriceOracle_InvalidSequencerFeed.selector);
        new SHOracle(address(treasury), priceFeedByMistake, tokens, feeds, beats);
    }

    /// @notice A deploy that lands during an outage must still succeed — the status is checked at
    ///         valuation time, not construction time. Bricking deploys on a transient chain
    ///         condition would be its own failure mode.
    function test_constructor_acceptsAnUptimeFeedReadingDown() public {
        vm.warp(SEQ_NOW);
        SHOracle gated = _sequencerGatedOracle(address(_uptimeFeed(SEQ_DOWN, SEQ_UP_SINCE)));

        assertTrue(gated.SEQUENCER_UPTIME_FEED() != address(0), "uptime feed should be recorded");
    }

    /// @notice End-to-end blast radius on a live wallet: with a sequencer-gated oracle committed,
    ///         an owner transfer of a WATCHED token is refused the moment the sequencer goes down,
    ///         because postCheck has to price the balance change to meter it. Stricter than the L1
    ///         behaviour by design — see THREAT_MODEL §3.14.
    function test_liveWallet_haltsWhenSequencerGoesDown() public {
        vm.warp(SEQ_NOW);
        MockV3Aggregator uptime = _uptimeFeed(SEQ_UP, SEQ_UP_SINCE);
        (SHOracle gated, MockV3Aggregator gatedEthFeed) = _replacementOracle(ETH_USD_PRICE, address(uptime));

        vm.prank(owner);
        treasury.proposePriceOracle(address(gated));

        // Re-stamps the PRICE feeds only. Handing the uptime feed to this would reset its startedAt
        // to now and put the sequencer back inside its grace period.
        MockV3Aggregator[] memory feeds = new MockV3Aggregator[](2);
        feeds[0] = gatedEthFeed;
        feeds[1] = MockV3Aggregator(config.usdcUsdPriceFeed);
        _warpPastTimelock(feeds);

        vm.prank(owner);
        treasury.commitPriceOracle();

        // Sequencer up and long past grace: the wallet meters and transfers as normal.
        ERC20Mock(config.usdc).mint(address(wallet), 100e6);
        vm.prank(owner);
        wallet.execute(
            bytes32(0), abi.encodePacked(config.usdc, uint256(0), abi.encodeCall(ERC20Mock.transfer, (rando, 1e6)))
        );
        assertEq(ERC20Mock(config.usdc).balanceOf(rando), 1e6, "baseline transfer should succeed");

        // Sequencer goes down. postCheck can no longer price the watched-token outflow, so the
        // whole execution reverts rather than metering against a frozen price.
        uptime.updateRoundData(2, SEQ_DOWN, block.timestamp, block.timestamp);
        vm.prank(owner);
        vm.expectRevert(SHOracle.PriceOracle_SequencerDown.selector);
        wallet.execute(
            bytes32(0), abi.encodePacked(config.usdc, uint256(0), abi.encodeCall(ERC20Mock.transfer, (rando, 1e6)))
        );
        assertEq(ERC20Mock(config.usdc).balanceOf(rando), 1e6, "revert was not atomic");
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    /// @dev An L2 Sequencer Uptime Feed reporting `answer` (SEQ_UP / SEQ_DOWN) with the status
    ///      having begun at `startedAt`. Zero decimals, matching the real feed — it carries a flag,
    ///      not a price.
    function _uptimeFeed(int256 answer, uint256 startedAt) internal returns (MockV3Aggregator f) {
        f = new MockV3Aggregator(0, answer);
        f.updateRoundData(1, answer, block.timestamp, startedAt);
    }

    /// @dev A self-contained oracle gated by `uptimeFeed`, pricing native at ETH_USD_PRICE. Its
    ///      price feed is given a deliberately wide heartbeat: the sequencer tests warp, and a
    ///      staleness revert would mask the branch actually under test.
    function _sequencerGatedOracle(address uptimeFeed) internal returns (SHOracle) {
        (address[] memory tokens, address[] memory feeds, uint256[] memory beats) = _nativeOnlyFeedArrays();
        return new SHOracle(address(treasury), uptimeFeed, tokens, feeds, beats);
    }

    /// @dev Native-only constructor arrays backed by a fresh mock feed. The heartbeat is
    ///      deliberately wide: the sequencer tests warp, and a staleness revert would mask the
    ///      branch actually under test. Split out from {_sequencerGatedOracle} so a test can build
    ///      these BEFORE arming vm.expectRevert, which would otherwise latch onto the feed's CREATE.
    function _nativeOnlyFeedArrays()
        internal
        returns (address[] memory tokens, address[] memory feeds, uint256[] memory beats)
    {
        MockV3Aggregator ethFeed = new MockV3Aggregator(DECIMALS, ETH_USD_PRICE);
        tokens = new address[](1);
        feeds = new address[](1);
        beats = new uint256[](1);
        tokens[0] = address(0);
        feeds[0] = address(ethFeed);
        beats[0] = 365 days;
    }

    /// @dev A replacement oracle in the shape proposePriceOracle accepts: it prices native (at
    ///      `ethPrice`) plus USDC, so a wallet watching USDC keeps working across the swap.
    function _replacementOracle(int256 ethPrice) internal returns (SHOracle, MockV3Aggregator ethFeed) {
        return _replacementOracle(ethPrice, address(0));
    }

    /// @dev As above, but gated by `uptimeFeed` — for exercising the sequencer check against a
    ///      wallet that is actually live on the oracle.
    function _replacementOracle(int256 ethPrice, address uptimeFeed)
        internal
        returns (SHOracle, MockV3Aggregator ethFeed)
    {
        ethFeed = new MockV3Aggregator(DECIMALS, ethPrice);
        address[] memory tokens = new address[](2);
        address[] memory feeds = new address[](2);
        uint256[] memory beats = new uint256[](2);
        tokens[0] = address(0);
        feeds[0] = address(ethFeed);
        beats[0] = config.ethHeartbeat;
        tokens[1] = config.usdc;
        feeds[1] = config.usdcUsdPriceFeed;
        beats[1] = config.usdcHeartbeat;
        return (new SHOracle(address(treasury), uptimeFeed, tokens, feeds, beats), ethFeed);
    }

    /// @dev Warps past ORACLE_TIMELOCK and re-stamps the given mock feeds. MockV3Aggregator records
    ///      updatedAt at write time, so a 2-day warp would otherwise leave every feed stale and make
    ///      getPrice revert for reasons unrelated to the timelock under test.
    function _warpPastTimelock(MockV3Aggregator[] memory feeds) internal {
        vm.warp(block.timestamp + registry.ORACLE_TIMELOCK());
        for (uint256 i = 0; i < feeds.length; i++) {
            feeds[i].updateAnswer(feeds[i].latestAnswer());
        }
    }
}
