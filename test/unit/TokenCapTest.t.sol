// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Execution, MODULE_TYPE_HOOK} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {IERC20Extended} from "../../src/interfaces/IERC20Extended.sol";

/**
 * @title TokenCapTest
 * @notice A token capped in its own units instead of in USD: the setters, the charging, the calls a
 *         capped token may receive, and what a broken token can and can't do. `noFeed` has no price
 *         feed and `link` has one, which are the two kinds of token the feature is for.
 * @dev The hook can't tell the owner's execute from a session key's, so these tests drive the owner's.
 *      The same rules through the real EntryPoint are in SHForkTestBase and SessionGuardTest.
 */
contract TokenCapTest is Test {
    uint256 constant NO_FEED_CAP = 150e18;
    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;

    SHOracle oracle;
    HelperConfig.NetworkConfig config;
    SessionHandler wallet;
    SpendingLimitModule module;
    ERC20Mock usdc;
    ERC20Mock dai;
    ERC20Mock link;
    ERC20Mock noFeed;
    CapTestVenue venue;

    address owner;
    address sink = makeAddr("sink");
    address rando = makeAddr("rando");

    function setUp() public {
        SHFactory factory;
        SHTreasury treasury;
        (factory, treasury, config, oracle) = new DeploySHProtocol().run();
        module = SpendingLimitModule(treasury.REGISTRY().spendingLimitModule());
        owner = config.account;

        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);
        link = ERC20Mock(config.link);
        noFeed = new ERC20Mock("No Feed", "NOF", 18);
        venue = new CapTestVenue();
        vm.deal(address(venue), 100 ether);

        address[] memory watched = new address[](2);
        watched[0] = address(usdc);
        watched[1] = address(dai);
        vm.prank(owner);
        wallet = SessionHandler(
            payable(factory.deployWallet(DAILY_LIMIT, WINDOW, watched, true, bytes32(0), bytes32(0), 0, new address[](0)))
        );

        vm.deal(address(wallet), 10 ether);
        usdc.mint(address(wallet), 10_000e6);
        link.mint(address(wallet), 10_000e18);
        noFeed.mint(address(wallet), 10_000e18);

        vm.prank(owner);
        wallet.setTokenCap(address(noFeed), NO_FEED_CAP);
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    function _setCap(address token, uint256 limit) internal {
        vm.prank(owner);
        wallet.setTokenCap(token, limit);
    }

    function _execute(address target, bytes memory data) internal {
        vm.prank(owner);
        wallet.execute(bytes32(0), abi.encodePacked(target, uint256(0), data));
    }

    function _executeBatch(Execution[] memory execs) internal {
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));
    }

    function _send(ERC20Mock token, uint256 amount) internal {
        _execute(address(token), abi.encodeCall(IERC20.transfer, (sink, amount)));
    }

    function _expectOverCap(address token, uint256 spent, uint256 limit) internal {
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_TokenCapExceeded.selector, token, spent, limit
            )
        );
    }

    function _remaining(address token) internal view returns (uint256 remaining) {
        (,, remaining) = wallet.getTokenCap(token);
    }

    function _spentUsd() internal view returns (int256) {
        return wallet.getConfig().spentInWindow;
    }

    /*//////////////////////////////////////////////////////////////
                                SETTERS
    //////////////////////////////////////////////////////////////*/

    function test_setTokenCap_recordsTheCap() public view {
        (bool capped, uint256 limit, uint256 remaining) = wallet.getTokenCap(address(noFeed));
        assertTrue(capped);
        assertEq(limit, NO_FEED_CAP);
        assertEq(remaining, NO_FEED_CAP);

        SpendingLimitModule.Config memory cfg = wallet.getConfig();
        assertEq(cfg.cappedTokenCount, 1);
        assertEq(cfg.cappedTokens.length, 1);
        assertEq(cfg.cappedTokens[0], address(noFeed));
    }

    function test_getTokenCap_ofATokenWithNoCap() public view {
        (bool capped, uint256 limit, uint256 remaining) = wallet.getTokenCap(address(link));
        assertFalse(capped);
        assertEq(limit, 0);
        assertEq(remaining, 0);
    }

    function test_setTokenCap_emitsItsEvent() public {
        vm.expectEmit(true, true, false, true, address(module));
        emit SpendingLimitModule.TokenCapSet(address(wallet), address(link), 5e18);
        _setCap(address(link), 5e18);
    }

    /// @notice A token with a feed may be capped in its own units; that's the owner's choice.
    function test_setTokenCap_acceptsAPricedToken() public {
        assertTrue(oracle.isPriced(address(link)));
        _setCap(address(link), 100e18);
        (bool capped,,) = wallet.getTokenCap(address(link));
        assertTrue(capped);
    }

    /// @notice A token counts in USD or in its own units, never both, whichever came first.
    function test_aTokenHasOneMeter() public {
        bytes memory alreadyMetered =
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenAlreadyMetered.selector, address(usdc));
        vm.prank(owner);
        vm.expectRevert(alreadyMetered);
        wallet.setTokenCap(address(usdc), 1e6);

        _setCap(address(link), 100e18);
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenAlreadyMetered.selector, address(link))
        );
        wallet.addWatchedToken(address(link));
    }

    /// @notice The owner can move a token from one meter to the other by leaving the first.
    function test_aTokenCanSwitchMeters() public {
        vm.startPrank(owner);
        wallet.removeWatchedToken(address(usdc));
        wallet.setTokenCap(address(usdc), 1e6);
        assertFalse(wallet.isWatched(address(usdc)));

        wallet.removeTokenCap(address(usdc));
        wallet.addWatchedToken(address(usdc));
        assertTrue(wallet.isWatched(address(usdc)));
        vm.stopPrank();
    }

    /// @notice A token with no feed still can't be watched, so its only meter is its own units.
    function test_aTokenWithNoFeedCanOnlyBeCappedInItsOwnUnits() public {
        ERC20Mock another = new ERC20Mock("Another", "ANO", 18);
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(another))
        );
        wallet.addWatchedToken(address(another));

        _setCap(address(another), 1e18);
        (bool capped,,) = wallet.getTokenCap(address(another));
        assertTrue(capped);
    }

    function test_setTokenCap_rejectsBadInput() public {
        vm.startPrank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidTokenCap.selector);
        wallet.setTokenCap(address(0), 1);

        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidTokenCap.selector);
        wallet.setTokenCap(address(link), uint256(type(uint104).max) + 1);

        wallet.setTokenCap(address(link), type(uint104).max); // the largest that fits

        // A cap on an address that isn't a token would revert every later read of it.
        vm.expectRevert();
        wallet.setTokenCap(makeAddr("not a token"), 1);
        vm.stopPrank();
    }

    function test_setTokenCap_stopsAtEight() public {
        for (uint256 i = 1; i < 8; i++) {
            _setCap(address(new ERC20Mock("T", "T", 18)), 1);
        }
        assertEq(wallet.getConfig().cappedTokenCount, 8);

        address ninth = address(new ERC20Mock("T", "T", 18));
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_TooManyCappedTokens.selector);
        wallet.setTokenCap(ninth, 1);

        // Changing a token already on a full list is not adding one.
        _setCap(address(noFeed), 1);
    }

    function test_setTokenCap_changingIt_keepsWhatWasSpent() public {
        _send(noFeed, 100e18);
        _setCap(address(noFeed), 120e18);
        assertEq(_remaining(address(noFeed)), 20e18);
        assertEq(wallet.getConfig().cappedTokenCount, 1, "changing a cap must not add it twice");

        _setCap(address(noFeed), 50e18); // now below what was spent
        assertEq(_remaining(address(noFeed)), 0);
        _expectOverCap(address(noFeed), 101e18, 50e18);
        wallet.execute(
            bytes32(0), abi.encodePacked(address(noFeed), uint256(0), abi.encodeCall(IERC20.transfer, (sink, 1e18)))
        );
    }

    function test_removeTokenCap_keepsTheListConsistent() public {
        ERC20Mock second = new ERC20Mock("Second", "SEC", 18);
        ERC20Mock third = new ERC20Mock("Third", "THI", 18);
        _setCap(address(second), 2);
        _setCap(address(third), 3);

        vm.expectEmit(true, true, false, false, address(module));
        emit SpendingLimitModule.TokenCapRemoved(address(wallet), address(noFeed));
        vm.prank(owner);
        wallet.removeTokenCap(address(noFeed)); // the first: third moves into its place

        SpendingLimitModule.Config memory cfg = wallet.getConfig();
        assertEq(cfg.cappedTokenCount, 2);
        assertEq(cfg.cappedTokens.length, 2);
        assertEq(cfg.cappedTokens[0], address(third));
        assertEq(cfg.cappedTokens[1], address(second));

        vm.prank(owner);
        wallet.removeTokenCap(address(third)); // the moved one must still be found at its new position
        cfg = wallet.getConfig();
        assertEq(cfg.cappedTokens.length, 1);
        assertEq(cfg.cappedTokens[0], address(second));
        (bool capped,,) = wallet.getTokenCap(address(third));
        assertFalse(capped);
    }

    function test_removeTokenCap_withNoCap_doesNothing() public {
        vm.prank(owner);
        wallet.removeTokenCap(address(link));
        assertEq(wallet.getConfig().cappedTokenCount, 1);
    }

    function test_removeTokenCap_forgetsWhatWasSpent() public {
        _send(noFeed, 100e18);
        vm.prank(owner);
        wallet.removeTokenCap(address(noFeed));
        _setCap(address(noFeed), NO_FEED_CAP);
        assertEq(_remaining(address(noFeed)), NO_FEED_CAP);
    }

    function test_setters_areOwnerOnly() public {
        bytes memory notOwner = abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando);
        vm.prank(rando);
        vm.expectRevert(notOwner);
        wallet.setTokenCap(address(link), 1);
        vm.prank(rando);
        vm.expectRevert(notOwner);
        wallet.removeTokenCap(address(noFeed));

        vm.prank(rando);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_NotInstalled.selector);
        module.setTokenCap(address(link), 1);
    }

    /// @notice The setters can't be driven through execute by anyone, or a session key could raise a cap.
    function test_setters_areRefusedThroughExecute() public {
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_AdminExecution.selector);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(
                address(module), uint256(0), abi.encodeCall(SpendingLimitModule.setTokenCap, (address(noFeed), 1e30))
            )
        );

        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_AdminExecution.selector);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(
                address(module), uint256(0), abi.encodeCall(SpendingLimitModule.removeTokenCap, (address(noFeed)))
            )
        );
    }

    function test_uninstall_wipesCaps() public {
        _send(noFeed, 100e18);
        vm.startPrank(owner);
        wallet.uninstallModule(MODULE_TYPE_HOOK, address(module), "");

        assertEq(module.getConfig(address(wallet)).cappedTokens.length, 0);
        assertEq(module.getConfig(address(wallet)).cappedTokenCount, 0);
        (bool capped,,) = module.getTokenCap(address(wallet), address(noFeed));
        assertFalse(capped);

        wallet.installModule(MODULE_TYPE_HOOK, address(module), abi.encode(DAILY_LIMIT, WINDOW, new address[](0), true));
        wallet.setTokenCap(address(noFeed), NO_FEED_CAP);
        vm.stopPrank();
        assertEq(_remaining(address(noFeed)), NO_FEED_CAP, "old spend survived a reinstall");
    }

    /*//////////////////////////////////////////////////////////////
                                CHARGING
    //////////////////////////////////////////////////////////////*/

    function test_send_chargesTheCapAndNotTheUsdLimit() public {
        vm.expectEmit(true, true, false, true, address(module));
        emit SpendingLimitModule.TokenCapSpendMetered(address(wallet), address(noFeed), 100e18, 100e18);
        _send(noFeed, 100e18);

        assertEq(_remaining(address(noFeed)), 50e18);
        assertEq(_spentUsd(), 0);
    }

    function test_send_upToTheCapPasses_andOneUnitMoreReverts() public {
        _send(noFeed, NO_FEED_CAP);
        assertEq(_remaining(address(noFeed)), 0);

        uint256 balanceBefore = noFeed.balanceOf(address(wallet));
        _expectOverCap(address(noFeed), NO_FEED_CAP + 1, NO_FEED_CAP);
        wallet.execute(
            bytes32(0), abi.encodePacked(address(noFeed), uint256(0), abi.encodeCall(IERC20.transfer, (sink, 1)))
        );
        assertEq(noFeed.balanceOf(address(wallet)), balanceBefore, "revert was not atomic");
    }

    function test_send_overTheCapInOneGoReverts() public {
        _expectOverCap(address(noFeed), NO_FEED_CAP + 1, NO_FEED_CAP);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(noFeed), uint256(0), abi.encodeCall(IERC20.transfer, (sink, NO_FEED_CAP + 1)))
        );
    }

    function test_aCapOfZero_stopsTheTokenLeaving() public {
        _setCap(address(noFeed), 0);
        _expectOverCap(address(noFeed), 1, 0);
        wallet.execute(
            bytes32(0), abi.encodePacked(address(noFeed), uint256(0), abi.encodeCall(IERC20.transfer, (sink, 1)))
        );
    }

    function test_twoSendsInABatch_chargeTheirTotal() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(noFeed), 0, abi.encodeCall(IERC20.transfer, (sink, 60e18)));
        execs[1] = Execution(address(noFeed), 0, abi.encodeCall(IERC20.transfer, (sink, 70e18)));
        _executeBatch(execs);
        assertEq(_remaining(address(noFeed)), 20e18);
    }

    /// @notice A cap nets against itself within one transaction: 100 out and 40 back charges 60.
    function test_outAndBackInOneTransaction_chargesTheNet() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(noFeed), 0, abi.encodeCall(IERC20.transfer, (sink, 100e18)));
        execs[1] = Execution(address(venue), 0, abi.encodeCall(CapTestVenue.give, (noFeed, 40e18)));
        _executeBatch(execs);
        assertEq(_remaining(address(noFeed)), NO_FEED_CAP - 60e18);
    }

    /// @notice An inflow charges nothing and banks nothing for later.
    function test_inflow_chargesNothingAndBanksNothing() public {
        _send(noFeed, 100e18);

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(noFeed), 0, abi.encodeCall(IERC20.transfer, (sink, 10e18)));
        execs[1] = Execution(address(venue), 0, abi.encodeCall(CapTestVenue.give, (noFeed, 500e18)));
        _executeBatch(execs);

        assertEq(_remaining(address(noFeed)), 50e18, "an inflow changed the cap");
    }

    /// @notice A token can leave without being a call's target, as an L2 bridge's burn does. Anything but
    ///         a plain send reads every capped token, so this is still charged.
    function test_aBalanceDropWithNoCallToTheToken_isCharged() public {
        _execute(address(venue), abi.encodeCall(CapTestVenue.burnFromCaller, (noFeed, 100e18)));
        assertEq(_remaining(address(noFeed)), 50e18);

        _expectOverCap(address(noFeed), 200e18, NO_FEED_CAP);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(venue), uint256(0), abi.encodeCall(CapTestVenue.burnFromCaller, (noFeed, 100e18)))
        );
    }

    function test_theCapResetsWithTheWindow() public {
        _send(noFeed, NO_FEED_CAP);
        assertEq(_remaining(address(noFeed)), 0);

        vm.warp(block.timestamp + WINDOW);
        assertEq(_remaining(address(noFeed)), NO_FEED_CAP, "the view must show an expired window as a full cap");

        _send(noFeed, NO_FEED_CAP); // would revert if the old spend still counted
        assertEq(_remaining(address(noFeed)), 0);
    }

    /// @notice A window that rolled while the token wasn't used must not carry its old spend over.
    function test_theCapResetsEvenIfTheRollCameFromAnotherTransaction() public {
        _send(noFeed, 100e18);
        vm.warp(block.timestamp + WINDOW);
        _execute(address(venue), abi.encodeCall(CapTestVenue.noop, ())); // rolls the window, moves nothing

        assertEq(_remaining(address(noFeed)), NO_FEED_CAP);
        _send(noFeed, NO_FEED_CAP);
    }

    function test_justBeforeTheWindowEnds_theSpendStillCounts() public {
        _send(noFeed, NO_FEED_CAP);
        vm.warp(block.timestamp + WINDOW - 1);
        assertEq(_remaining(address(noFeed)), 0);
    }

    /// @notice Caps never read the oracle, so a dead feed can't block a token capped in its own units.
    function test_aStaleFeedDoesntBlockACappedToken() public {
        _setCap(address(link), 100e18);
        vm.warp(block.timestamp + 30 days); // every feed in the mock config is stale by now
        _send(link, 100e18);
        assertEq(_remaining(address(link)), 0);
    }

    /*//////////////////////////////////////////////////////////////
                               SCENARIOS
    //////////////////////////////////////////////////////////////*/

    /// @notice Trading ETH for a capped token: whatever ETH was paid counts in full against the USD cap,
    ///         since the token coming in has no USD value here, and the token's own cap isn't touched.
    function test_buyingACappedTokenWithEth_chargesTheUsdLimitInFull() public {
        _setCap(address(link), 150e18);
        uint256 paid = 1 ether;

        vm.prank(owner);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(venue), paid, abi.encodeCall(CapTestVenue.buyWithNative, (link, 100e18)))
        );

        assertEq(_spentUsd(), oracle.getPrice(address(0), paid));
        assertEq(_remaining(address(link)), 150e18, "buying used the token's own cap");
    }

    /// @notice Selling a capped token: the amount sold counts against its cap, however fair the price,
    ///         and the ETH coming in neither charges nor adds room to the USD cap.
    function test_sellingACappedTokenForEth_chargesItsOwnCapOnly() public {
        _setCap(address(link), 150e18);

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(link), 0, abi.encodeCall(IERC20.approve, (address(venue), 100e18)));
        execs[1] = Execution(address(venue), 0, abi.encodeCall(CapTestVenue.sellForNative, (link, 100e18, 1 ether)));
        _executeBatch(execs);

        assertEq(_remaining(address(link)), 50e18);
        assertEq(_spentUsd(), 0);
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT, "the ETH received added room or charged the cap");
    }

    function test_sellingPastTheCap_revertsTheWholeTrade() public {
        _setCap(address(link), 150e18);
        uint256 ethBefore = address(wallet).balance;

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(link), 0, abi.encodeCall(IERC20.approve, (address(venue), 200e18)));
        execs[1] = Execution(address(venue), 0, abi.encodeCall(CapTestVenue.sellForNative, (link, 200e18, 1 ether)));

        _expectOverCap(address(link), 200e18, 150e18);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        assertEq(address(wallet).balance, ethBefore);
    }

    /*//////////////////////////////////////////////////////////////
                      WHAT A CAPPED TOKEN MAY RECEIVE
    //////////////////////////////////////////////////////////////*/

    /// @notice Anything else aimed at a capped token is refused, alone or hidden in a batch: it could be
    ///         a grant function under any name, leaving an allowance the hook never sees.
    function test_callsOtherThanTransferOrAGrant_areRefused() public {
        bytes[] memory calls = new bytes[](4);
        calls[0] = abi.encodeWithSignature("someGrant(address,uint256)", sink, 1e18);
        calls[1] = abi.encodeCall(IERC20.transferFrom, (address(wallet), sink, 1));
        calls[2] = abi.encodeCall(ERC20Mock.mint, (address(wallet), 1));
        calls[3] = hex"01"; // too short to hold a selector, so it would reach the fallback

        for (uint256 i = 0; i < calls.length; i++) {
            bytes4 selector = calls[i].length >= 4 ? bytes4(calls[i]) : bytes4(0);
            bytes memory refusal = abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_CallNotAllowedOnCappedToken.selector, address(noFeed), selector
            );

            vm.prank(owner);
            vm.expectRevert(refusal);
            wallet.execute(bytes32(0), abi.encodePacked(address(noFeed), uint256(0), calls[i]));

            Execution[] memory execs = new Execution[](2);
            execs[0] = Execution(address(venue), 0, abi.encodeCall(CapTestVenue.noop, ()));
            execs[1] = Execution(address(noFeed), 0, calls[i]);
            vm.prank(owner);
            vm.expectRevert(refusal);
            wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));
        }
    }

    /// @notice Empty calldata reaches a token's fallback too, so sending it native value is refused.
    function test_emptyCalldataToACappedToken_isRefused() public {
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_CallNotAllowedOnCappedToken.selector, address(noFeed), bytes4(0)
            )
        );
        wallet.execute(bytes32(0), abi.encodePacked(address(noFeed), uint256(1)));
    }

    /// @notice The rule is only about capped tokens: the same call on another contract still runs.
    function test_theRule_leavesOtherTargetsAlone() public {
        _execute(address(usdc), abi.encodeCall(ERC20Mock.mint, (address(wallet), 1)));
    }

    /// @notice A grant on a capped token still has to be used up within its transaction.
    function test_grants_onACappedToken_stillCantStand() public {
        CapTestLegacyToken legacy = new CapTestLegacyToken();
        legacy.mint(address(wallet), 1000e18);
        _setCap(address(legacy), 500e18);
        vm.prank(owner);
        wallet.addTrustedSpender(address(venue)); // it has no feed, so only a trusted spender may be approved

        bytes4[3] memory grants = [
            IERC20.approve.selector, IERC20Extended.increaseAllowance.selector, IERC20Extended.increaseApproval.selector
        ];
        for (uint256 i = 0; i < grants.length; i++) {
            vm.prank(owner);
            vm.expectRevert(
                abi.encodeWithSelector(
                    SpendingLimitModule.SpendingLimitModule_StandingApprovalNotAllowed.selector,
                    address(legacy),
                    address(venue),
                    uint256(1e18)
                )
            );
            wallet.execute(
                bytes32(0),
                abi.encodePacked(
                    address(legacy), uint256(0), abi.encodeWithSelector(grants[i], address(venue), uint256(1e18))
                )
            );
        }
    }

    /// @notice A grant used up in its transaction is charged as the balance drop it caused.
    function test_grantThenPull_isCharged() public {
        vm.prank(owner);
        wallet.addTrustedSpender(address(venue));

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(noFeed), 0, abi.encodeCall(IERC20.approve, (address(venue), 100e18)));
        execs[1] = Execution(address(venue), 0, abi.encodeCall(CapTestVenue.pull, (noFeed, 100e18)));
        _executeBatch(execs);

        assertEq(_remaining(address(noFeed)), 50e18);
        assertEq(noFeed.allowance(address(wallet), address(venue)), 0);
    }

    /// @notice A token with no feed may still only be approved to a trusted spender.
    function test_approvingAnUntrustedSpender_onATokenWithNoFeed_isRefused() public {
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(noFeed))
        );
        wallet.execute(
            bytes32(0), abi.encodePacked(address(noFeed), uint256(0), abi.encodeCall(IERC20.approve, (sink, 1e18)))
        );
    }

    /*//////////////////////////////////////////////////////////////
                             A BROKEN TOKEN
    //////////////////////////////////////////////////////////////*/

    /// @notice A token whose balanceOf fails blocks anything that reads it, rather than dodging the cap,
    ///         and the owner can always get out by removing the cap: that call never reaches the token.
    function test_aBrokenToken_blocksExecutionsUntilTheCapIsRemoved() public {
        CapTestBrokenToken broken = new CapTestBrokenToken();
        _setCap(address(broken), 1e18);
        broken.setBroken(true);

        bytes memory swapLike = abi.encodePacked(address(venue), uint256(0), abi.encodeCall(CapTestVenue.noop, ()));
        vm.prank(owner);
        vm.expectRevert(CapTestBrokenToken.Broken.selector);
        wallet.execute(bytes32(0), swapLike);

        vm.prank(owner);
        wallet.removeTokenCap(address(broken));

        vm.prank(owner);
        wallet.execute(bytes32(0), swapLike);
    }

    /// @notice A token that breaks during the transaction can't skip being charged: the read after the
    ///         calls fails too, and the whole thing reverts. This is also what running a read out of
    ///         gas would do, so a leaked key can't dodge the cap by setting a tight gas limit.
    function test_aTokenThatBreaksMidTransaction_revertsRatherThanSkippingTheCap() public {
        CapTestBrokenToken broken = new CapTestBrokenToken();
        broken.mint(address(wallet), 100e18);
        _setCap(address(broken), 1e18);

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(broken), 0, abi.encodeCall(IERC20.transfer, (sink, 100e18)));
        execs[1] = Execution(address(venue), 0, abi.encodeCall(CapTestVenue.breakToken, (broken)));

        vm.prank(owner);
        vm.expectRevert(CapTestBrokenToken.Broken.selector);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        assertEq(broken.balanceOf(address(wallet)), 100e18);
    }

    /// @notice A plain send can't move a capped token, so it doesn't read one, and a broken capped token
    ///         doesn't stop it. Sending the broken token itself is not plain, and reads it.
    function test_aPlainSend_skipsTheCappedTokens() public {
        CapTestBrokenToken broken = new CapTestBrokenToken();
        _setCap(address(broken), 1e18);
        broken.setBroken(true);

        _send(usdc, 1e6);
        assertEq(usdc.balanceOf(sink), 1e6);

        vm.prank(owner);
        wallet.execute(bytes32(0), abi.encodePacked(sink, uint256(1 ether)));
        assertEq(sink.balance, 1 ether);

        vm.prank(owner);
        vm.expectRevert(CapTestBrokenToken.Broken.selector);
        wallet.execute(
            bytes32(0), abi.encodePacked(address(broken), uint256(0), abi.encodeCall(IERC20.transfer, (sink, 1)))
        );
    }

    /// @notice A wallet with no caps doesn't change: a token with no meter still moves unmetered.
    function test_anUncappedTokenWithNoFeed_stillMovesFreely() public {
        ERC20Mock free = new ERC20Mock("Free", "FRE", 18);
        free.mint(address(wallet), 1000e18);
        _send(free, 1000e18);
        assertEq(free.balanceOf(sink), 1000e18);
        assertEq(_spentUsd(), 0);
    }

    /// @notice Whatever is sent, and however it's split up, the total charged is what left the wallet
    ///         and never more than the cap.
    function testFuzz_spendNeverPassesTheCap(uint104 limit, uint96[3] memory amounts) public {
        _setCap(address(noFeed), limit);
        noFeed.mint(address(wallet), uint256(type(uint96).max) * 3);
        uint256 left = noFeed.balanceOf(address(wallet));
        uint256 total;

        for (uint256 i = 0; i < amounts.length; i++) {
            vm.prank(owner);
            try wallet.execute(
                bytes32(0),
                abi.encodePacked(
                    address(noFeed), uint256(0), abi.encodeCall(IERC20.transfer, (sink, uint256(amounts[i])))
                )
            ) {
                total += amounts[i];
            } catch {
                assertGt(total + amounts[i], limit, "reverted while still under the cap");
            }
            assertEq(noFeed.balanceOf(address(wallet)), left - total);
            assertEq(_remaining(address(noFeed)), limit - total);
        }
        assertLe(total, limit);
    }
}

/// @dev A stand-in for a swap venue, a bridge and a funding source, all in one.
contract CapTestVenue {
    function noop() external {}

    /// @dev Pays the caller tokens it never asked for.
    function give(ERC20Mock token, uint256 amount) external {
        token.mint(msg.sender, amount);
    }

    /// @dev Burns the caller's tokens with no allowance, as an L2 standard bridge does on a withdrawal.
    function burnFromCaller(ERC20Mock token, uint256 amount) external {
        token.burn(msg.sender, amount);
    }

    function pull(ERC20Mock token, uint256 amount) external {
        token.transferFrom(msg.sender, address(this), amount);
    }

    function buyWithNative(ERC20Mock token, uint256 amountOut) external payable {
        token.mint(msg.sender, amountOut);
    }

    function sellForNative(ERC20Mock token, uint256 amountIn, uint256 nativeOut) external {
        token.transferFrom(msg.sender, address(this), amountIn);
        (bool ok,) = msg.sender.call{value: nativeOut}("");
        require(ok, "native payment failed");
    }

    function breakToken(CapTestBrokenToken token) external {
        token.setBroken(true);
    }
}

/// @dev A token whose balanceOf can be made to fail, as a malicious or abandoned token's might.
contract CapTestBrokenToken is ERC20 {
    error Broken();

    bool private _broken;

    constructor() ERC20("Broken", "BRK") {}

    function mint(address account, uint256 amount) external {
        _mint(account, amount);
    }

    function setBroken(bool broken) external {
        _broken = broken;
    }

    function balanceOf(address account) public view override returns (uint256) {
        if (_broken) revert Broken();
        return super.balanceOf(account);
    }
}

/// @dev A token with the two legacy allowance functions, each adding to the current allowance.
contract CapTestLegacyToken is ERC20Mock {
    constructor() ERC20Mock("Legacy", "LGC", 18) {}

    function increaseAllowance(address spender, uint256 added) external returns (bool) {
        _approve(msg.sender, spender, allowance(msg.sender, spender) + added);
        return true;
    }

    function increaseApproval(address spender, uint256 added) external returns (bool) {
        _approve(msg.sender, spender, allowance(msg.sender, spender) + added);
        return true;
    }
}
