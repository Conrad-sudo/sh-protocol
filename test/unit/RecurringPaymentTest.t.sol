//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {MODULE_TYPE_EXECUTOR} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {RecurringPaymentModule} from "../../src/RecurringPaymentModule.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {PasskeyHelper} from "../utils/PasskeyHelper.sol";

/// @dev A token that reports failure by returning false instead of reverting.
contract FalseReturningToken {
    function transfer(address, uint256) external pure returns (bool) {
        return false;
    }
}

/// @dev A native recipient that tries to collect the same instalment twice by re-entering run().
contract ReenteringRecipient {
    RecurringPaymentModule public immutable MODULE;
    address public account;
    uint256 public id;
    bool public reentryFailed;
    uint256 public received;

    constructor(RecurringPaymentModule module) {
        MODULE = module;
    }

    function arm(address account_, uint256 id_) external {
        account = account_;
        id = id_;
    }

    receive() external payable {
        received += msg.value;
        if (account == address(0)) return;
        try MODULE.run(account, id) {}
        catch {
            reentryFailed = true;
        }
    }
}

/**
 * @title RecurringPaymentTest
 * @notice RecurringPaymentModule on a real SessionHandler wallet: who may create and cancel
 *         schedules, when {run} pays, that each payment goes through the wallet's guard, fee and
 *         spending cap, that missed periods do not pile up, and that an uninstall wipes everything.
 * @dev Also measures the gas of one {run}, which sizes the keeper fee per chain. Those numbers come
 *      from local mocks (ERC20Mock, MockV3Aggregator), which are cheaper than real tokens and
 *      Chainlink proxies, so they are a floor, not the real cost on a live chain.
 */
contract RecurringPaymentTest is Test {
    using PasskeyHelper for PasskeyHelper.Passkey;

    SessionHandler wallet;
    RecurringPaymentModule recurring;
    SpendingLimitModule module;
    SHOracle oracle;
    SHTreasury treasury;
    SHRegistry registry;
    SHFactory factory;
    HelperConfig.NetworkConfig config;
    SendPackedUserOp sendPackedUserOp;
    ERC20Mock usdc;
    ERC20Mock dai;

    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;
    uint48 constant INTERVAL = 30 days;
    uint256 constant AMOUNT = 100e6; // 100 USDC
    /// @dev Fixed start for tests that move time. Never `uint256 start = block.timestamp` before a
    ///      warp: the optimizer treats TIMESTAMP as constant within a call and may re-read it AFTER
    ///      the warp, silently shifting every expected date.
    uint256 constant START = 1_000_000;

    PasskeyHelper.Passkey sessionKey;
    address owner;
    address payee = makeAddr("payee");
    address keeper = makeAddr("keeper");
    address attacker = makeAddr("attacker");
    address bundler = makeAddr("bundler");

    function setUp() public {
        sessionKey = PasskeyHelper.create("sessionKey");

        (factory, treasury, config, oracle) = new DeploySHProtocol().run();
        registry = factory.REGISTRY();
        module = SpendingLimitModule(registry.spendingLimitModule());
        recurring = RecurringPaymentModule(registry.recurringPaymentModule());
        owner = config.account;
        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);

        address[] memory watched = new address[](2);
        watched[0] = address(usdc);
        watched[1] = address(dai);
        vm.prank(owner);
        wallet = SessionHandler(
            payable(factory.deployWallet(
                    DAILY_LIMIT, WINDOW, watched, true, sessionKey.qx, sessionKey.qy, _deadline(), new address[](0)
                ))
        );

        sendPackedUserOp = new SendPackedUserOp();
        vm.deal(address(wallet), 10 ether);
        usdc.mint(address(wallet), 100_000e6);
    }

    /*//////////////////////////////////////////////////////////////
                           CREATE / CANCEL
    //////////////////////////////////////////////////////////////*/

    function test_create_ownerCreatesALiveSchedule() public {
        vm.expectEmit(address(recurring));
        emit RecurringPaymentModule.ScheduleCreated(
            address(wallet), 1, address(usdc), payee, AMOUNT, INTERVAL, block.timestamp, 0
        );
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);

        assertEq(id, 1);
        assertEq(recurring.scheduleCount(address(wallet)), 1);
        (RecurringPaymentModule.Schedule memory s, bool live) = recurring.getSchedule(address(wallet), id);
        assertTrue(live);
        assertEq(s.token, address(usdc));
        assertEq(s.recipient, payee);
        assertEq(s.amount, AMOUNT);
        assertEq(s.interval, INTERVAL);
        assertEq(s.nextDue, block.timestamp);
        assertTrue(recurring.isDue(address(wallet), id));
    }

    function test_create_strangerCannot() public {
        vm.expectRevert(
            abi.encodeWithSelector(RecurringPaymentModule.RecurringPayment_NotOwner.selector, address(wallet), attacker)
        );
        vm.prank(attacker);
        recurring.createSchedule(address(wallet), address(usdc), attacker, AMOUNT, INTERVAL, 0, 0);
    }

    /// @notice The property the module rests on: a session key acts AS the wallet, and the wallet is
    ///         not its owner, so a key cannot create a schedule that would outlive it. Direct form:
    ///         the exact error, with the EntryPoint as the caller of execute().
    function test_create_sessionKeyCannot_exactError() public {
        bytes memory call = abi.encodeCall(
            RecurringPaymentModule.createSchedule,
            (address(wallet), address(usdc), attacker, AMOUNT, INTERVAL, uint48(0), uint48(0))
        );
        vm.expectRevert(
            abi.encodeWithSelector(
                RecurringPaymentModule.RecurringPayment_NotOwner.selector, address(wallet), address(wallet)
            )
        );
        vm.prank(config.entryPoint);
        wallet.execute(bytes32(0), abi.encodePacked(address(recurring), uint256(0), call));
    }

    /// @notice The same through a real passkey-signed UserOp: handleOps absorbs the inner revert, so
    ///         the proof is that no schedule exists afterwards.
    function test_create_sessionKeyCannot_fullUserOp() public {
        bytes memory call = abi.encodeCall(
            RecurringPaymentModule.createSchedule,
            (address(wallet), address(usdc), attacker, AMOUNT, INTERVAL, uint48(0), uint48(0))
        );
        (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOp(
            address(wallet),
            config,
            address(recurring),
            0,
            call,
            PasskeyHelper.nonceKey(PasskeyHelper.LATEST_OP_DEADLINE, 0)
        );
        op.signature = sessionKey.sign(hash);
        _handleOps(op);

        assertEq(recurring.scheduleCount(address(wallet)), 0, "a session key created a schedule");
    }

    /// @notice A wallet that owns itself would let a session key pass the owner check, so it is refused.
    function test_create_selfOwnedWalletRefused() public {
        vm.prank(owner);
        wallet.transferOwnership(address(wallet));

        vm.expectRevert(
            abi.encodeWithSelector(
                RecurringPaymentModule.RecurringPayment_NotOwner.selector, address(wallet), address(wallet)
            )
        );
        vm.prank(address(wallet));
        recurring.createSchedule(address(wallet), address(usdc), payee, AMOUNT, INTERVAL, 0, 0);
    }

    function test_create_rejectsBadInputs() public {
        vm.startPrank(owner);
        vm.expectRevert(RecurringPaymentModule.RecurringPayment_InvalidRecipient.selector);
        recurring.createSchedule(address(wallet), address(usdc), address(0), AMOUNT, INTERVAL, 0, 0);

        vm.expectRevert(RecurringPaymentModule.RecurringPayment_InvalidRecipient.selector);
        recurring.createSchedule(address(wallet), address(usdc), address(wallet), AMOUNT, INTERVAL, 0, 0);

        vm.expectRevert(RecurringPaymentModule.RecurringPayment_InvalidToken.selector);
        recurring.createSchedule(address(wallet), makeAddr("noCode"), payee, AMOUNT, INTERVAL, 0, 0);

        vm.expectRevert(RecurringPaymentModule.RecurringPayment_InvalidAmount.selector);
        recurring.createSchedule(address(wallet), address(usdc), payee, 0, INTERVAL, 0, 0);

        uint48 minInterval = recurring.MIN_INTERVAL();
        vm.expectRevert(
            abi.encodeWithSelector(
                RecurringPaymentModule.RecurringPayment_InvalidInterval.selector, minInterval - 1, minInterval
            )
        );
        recurring.createSchedule(address(wallet), address(usdc), payee, AMOUNT, minInterval - 1, 0, 0);

        uint48 first = uint48(block.timestamp + 10 days);
        vm.expectRevert(
            abi.encodeWithSelector(RecurringPaymentModule.RecurringPayment_InvalidEnd.selector, first - 1, first)
        );
        recurring.createSchedule(address(wallet), address(usdc), payee, AMOUNT, INTERVAL, first, first - 1);
        vm.stopPrank();
    }

    /// @notice A start already in the past means now: the first payment is due immediately.
    function test_create_pastStartMeansNow() public {
        _warp(1_000_000);
        uint256 id = _create(address(usdc), AMOUNT, 1, 0);
        (RecurringPaymentModule.Schedule memory s,) = recurring.getSchedule(address(wallet), id);
        assertEq(s.nextDue, block.timestamp);
    }

    function test_create_refusedWhenModuleUninstalled() public {
        vm.prank(owner);
        wallet.uninstallModule(MODULE_TYPE_EXECUTOR, address(recurring), "");

        vm.expectRevert(
            abi.encodeWithSelector(RecurringPaymentModule.RecurringPayment_NotInstalled.selector, address(wallet))
        );
        vm.prank(owner);
        recurring.createSchedule(address(wallet), address(usdc), payee, AMOUNT, INTERVAL, 0, 0);
    }

    function test_cancel_ownerCancels_thenRunRefused() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);

        vm.expectEmit(address(recurring));
        emit RecurringPaymentModule.ScheduleCancelled(address(wallet), id);
        vm.prank(owner);
        recurring.cancelSchedule(address(wallet), id);

        (, bool live) = recurring.getSchedule(address(wallet), id);
        assertFalse(live);
        vm.expectRevert(
            abi.encodeWithSelector(
                RecurringPaymentModule.RecurringPayment_UnknownSchedule.selector, address(wallet), id
            )
        );
        recurring.run(address(wallet), id);
    }

    function test_cancel_strangerCannot() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        vm.expectRevert(
            abi.encodeWithSelector(RecurringPaymentModule.RecurringPayment_NotOwner.selector, address(wallet), attacker)
        );
        vm.prank(attacker);
        recurring.cancelSchedule(address(wallet), id);
    }

    /*//////////////////////////////////////////////////////////////
                                 RUN
    //////////////////////////////////////////////////////////////*/

    function test_run_notDueReverts() public {
        uint48 first = uint48(block.timestamp + 1 days);
        uint256 id = _create(address(usdc), AMOUNT, first, 0);
        vm.expectRevert(abi.encodeWithSelector(RecurringPaymentModule.RecurringPayment_NotDue.selector, first));
        recurring.run(address(wallet), id);
    }

    /// @notice A due token payment: the payee is paid, the due date moves one interval, the treasury
    ///         gets the protocol fee PLUS the keeper fee, and the cap meters the payment.
    function test_run_paysTokenChargesBothFeesAndIsMetered() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        uint256 treasuryBefore = address(treasury).balance;
        uint256 fees = registry.getFee() + registry.keeperFee();
        int256 expectedUsd = oracle.getPrice(address(usdc), AMOUNT);
        uint256 dueAt = block.timestamp;

        vm.expectEmit(address(recurring));
        emit RecurringPaymentModule.RecurringPaymentExecuted(address(wallet), id, dueAt, dueAt + INTERVAL);
        vm.expectEmit(address(wallet));
        emit SessionHandler.ProtocolFeePaid(registry.getFee(), registry.keeperFee());
        vm.prank(keeper);
        recurring.run(address(wallet), id);

        assertEq(usdc.balanceOf(payee), AMOUNT, "payee not paid");
        assertEq(address(treasury).balance, treasuryBefore + fees, "treasury did not get both fees");
        assertEq(wallet.getConfig().spentInWindow, expectedUsd, "payment not metered by the cap");
        (RecurringPaymentModule.Schedule memory s,) = recurring.getSchedule(address(wallet), id);
        assertEq(s.nextDue, dueAt + INTERVAL);
        assertFalse(recurring.isDue(address(wallet), id));
    }

    function test_run_paysNative() public {
        uint256 id = _create(address(0), 0.1 ether, uint48(block.timestamp), 0);
        recurring.run(address(wallet), id);
        assertEq(payee.balance, 0.1 ether);
        assertGt(wallet.getConfig().spentInWindow, 0, "native payment not metered");
    }

    /// @notice Anyone may call run; the schedule decides what is paid. A second call in the same
    ///         period pays nothing.
    function test_run_anyoneCanCall_butOnlyOncePerPeriod() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        vm.prank(attacker);
        recurring.run(address(wallet), id);
        assertEq(usdc.balanceOf(payee), AMOUNT);

        vm.expectRevert(
            abi.encodeWithSelector(RecurringPaymentModule.RecurringPayment_NotDue.selector, block.timestamp + INTERVAL)
        );
        vm.prank(attacker);
        recurring.run(address(wallet), id);
    }

    /// @notice Missed periods do not pile up: after 3.5 missed intervals, ONE payment, and the next
    ///         due date is the next point on the schedule's grid still in the future.
    function test_run_missedPeriodsPayOnce() public {
        uint256 start = START;
        _warp(START);
        uint256 id = _create(address(usdc), AMOUNT, uint48(start), 0);

        _warp(start + 3 * INTERVAL + INTERVAL / 2);
        recurring.run(address(wallet), id);

        assertEq(usdc.balanceOf(payee), AMOUNT, "paid more than one instalment");
        (RecurringPaymentModule.Schedule memory s,) = recurring.getSchedule(address(wallet), id);
        assertEq(s.nextDue, start + 4 * INTERVAL, "next due date off the grid");
    }

    /// @notice Run exactly on a later grid point: that point is the one being paid, so the next is
    ///         one interval after it.
    function test_run_onAGridPoint() public {
        uint256 start = START;
        _warp(START);
        uint256 id = _create(address(usdc), AMOUNT, uint48(start), 0);
        _warp(start + 2 * INTERVAL);
        recurring.run(address(wallet), id);
        (RecurringPaymentModule.Schedule memory s,) = recurring.getSchedule(address(wallet), id);
        assertEq(s.nextDue, start + 3 * INTERVAL);
    }

    /// @notice The last payment deletes the schedule.
    function test_run_lastPaymentEndsTheSchedule() public {
        uint256 start = START;
        _warp(START);
        uint48 end = uint48(start + INTERVAL); // two payments: start and start + INTERVAL
        uint256 id = _create(address(usdc), AMOUNT, uint48(start), end);

        recurring.run(address(wallet), id);
        _warp(start + INTERVAL);
        vm.expectEmit(address(recurring));
        emit RecurringPaymentModule.RecurringPaymentExecuted(address(wallet), id, start + INTERVAL, 0);
        recurring.run(address(wallet), id);

        assertEq(usdc.balanceOf(payee), 2 * AMOUNT);
        (, bool live) = recurring.getSchedule(address(wallet), id);
        assertFalse(live, "finished schedule still live");
        _warp(start + 2 * INTERVAL);
        vm.expectRevert(
            abi.encodeWithSelector(
                RecurringPaymentModule.RecurringPayment_UnknownSchedule.selector, address(wallet), id
            )
        );
        recurring.run(address(wallet), id);
    }

    /// @notice A payment over the USD cap reverts the whole run: nothing paid, no fee, still due.
    function test_run_overCapRevertsAndStaysDue() public {
        uint256 id = _create(address(usdc), 6_000e6, uint48(block.timestamp), 0); // cap is $5,000
        uint256 treasuryBefore = address(treasury).balance;

        vm.expectRevert();
        recurring.run(address(wallet), id);

        assertEq(usdc.balanceOf(payee), 0);
        assertEq(address(treasury).balance, treasuryBefore, "a failed run was charged");
        assertTrue(recurring.isDue(address(wallet), id), "a failed run moved the due date");
    }

    function test_run_pausedWalletReverts() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        vm.prank(owner);
        wallet.pause();
        vm.expectRevert(Pausable.EnforcedPause.selector);
        recurring.run(address(wallet), id);
        assertTrue(recurring.isDue(address(wallet), id));
    }

    /// @notice The wallet's allowlist binds the executor like a session key: an unlisted token
    ///         contract is refused.
    function test_run_allowlistBindsTheExecutor() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        address[] memory targets = new address[](1);
        targets[0] = address(dai);
        vm.prank(owner);
        wallet.enableAllowList(targets);

        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(usdc))
        );
        recurring.run(address(wallet), id);

        vm.prank(owner);
        wallet.addAllowedTarget(address(usdc));
        recurring.run(address(wallet), id);
        assertEq(usdc.balanceOf(payee), AMOUNT);
    }

    function test_run_notEnoughForTheFeeReverts() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        vm.deal(address(wallet), 0);
        vm.expectRevert(SessionHandler.SessionHandler_NotEnoughBalance.selector);
        recurring.run(address(wallet), id);
    }

    /// @notice A token that returns false must not count as paid.
    function test_run_falseReturningTokenReverts() public {
        FalseReturningToken bad = new FalseReturningToken();
        uint256 id = _create(address(bad), AMOUNT, uint48(block.timestamp), 0);
        vm.expectRevert(RecurringPaymentModule.RecurringPayment_TransferFailed.selector);
        recurring.run(address(wallet), id);
        assertTrue(recurring.isDue(address(wallet), id));
    }

    /// @notice A native recipient that re-enters run() during its payment finds nothing due: the due
    ///         date moved before the wallet was called.
    function test_run_reentryFindsNothingDue() public {
        ReenteringRecipient thief = new ReenteringRecipient(recurring);
        vm.prank(owner);
        uint256 id = recurring.createSchedule(address(wallet), address(0), address(thief), 0.1 ether, INTERVAL, 0, 0);
        thief.arm(address(wallet), id);

        recurring.run(address(wallet), id);

        assertEq(thief.received(), 0.1 ether, "paid other than exactly once");
        assertTrue(thief.reentryFailed(), "re-entry was not refused");
    }

    /*//////////////////////////////////////////////////////////////
                              UNINSTALL
    //////////////////////////////////////////////////////////////*/

    /// @notice Uninstalling wipes every schedule; reinstalling starts clean and ids keep counting.
    function test_uninstall_wipesSchedules_reinstallStartsClean() public {
        uint256 first = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);

        vm.startPrank(owner);
        wallet.uninstallModule(MODULE_TYPE_EXECUTOR, address(recurring), "");
        wallet.installModule(MODULE_TYPE_EXECUTOR, address(recurring), "");
        vm.stopPrank();

        (, bool live) = recurring.getSchedule(address(wallet), first);
        assertFalse(live, "schedule survived an uninstall");
        vm.expectRevert(
            abi.encodeWithSelector(
                RecurringPaymentModule.RecurringPayment_UnknownSchedule.selector, address(wallet), first
            )
        );
        recurring.run(address(wallet), first);

        uint256 fresh = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        assertEq(fresh, 3, "ids restarted after a reinstall");
        recurring.run(address(wallet), fresh);
        assertEq(usdc.balanceOf(payee), AMOUNT);
    }

    /*//////////////////////////////////////////////////////////////
                                 GAS
    //////////////////////////////////////////////////////////////*/

    /// @notice Gas of one run, the figure the keeper fee is sized from. Two cases: the payee's first
    ///         payment (a new token balance slot, the worst case) and a later one. Both are the whole
    ///         transaction: execution + 21,000 base + calldata. Read with -vv.
    function test_gas_runTokenPayment() public {
        uint256 id = _create(address(usdc), AMOUNT, uint48(block.timestamp), 0);
        uint256 first = _measureRun(id);

        _warp(block.timestamp + INTERVAL);
        uint256 later = _measureRun(id);

        console2.log("run, ERC-20, payee's first payment (gas):", first);
        console2.log("run, ERC-20, later payment (gas):        ", later);
    }

    function test_gas_runNativePayment() public {
        uint256 id = _create(address(0), 0.01 ether, uint48(block.timestamp), 0);
        console2.log("run, native, first payment (gas):        ", _measureRun(id));
    }

    /*//////////////////////////////////////////////////////////////
                               HELPERS
    //////////////////////////////////////////////////////////////*/

    /// @dev Moves time and refreshes the three feeds the wallet prices with, which would otherwise go
    ///      stale and fail the hook on any jump past their heartbeat.
    function _warp(uint256 to) internal {
        vm.warp(to);
        MockV3Aggregator(config.ethUsdPriceFeed).updateAnswer(MockV3Aggregator(config.ethUsdPriceFeed).latestAnswer());
        MockV3Aggregator(config.usdcUsdPriceFeed).updateAnswer(MockV3Aggregator(config.usdcUsdPriceFeed).latestAnswer());
        MockV3Aggregator(config.daiUsdPriceFeed).updateAnswer(MockV3Aggregator(config.daiUsdPriceFeed).latestAnswer());
    }

    function _create(address token, uint256 amount, uint48 firstDue, uint48 end) internal returns (uint256 id) {
        vm.prank(owner);
        id = recurring.createSchedule(address(wallet), token, payee, amount, INTERVAL, firstDue, end);
    }

    /// @dev Whole-transaction gas of one run: execution, plus the 21,000 base and the calldata cost.
    ///      Storage the test already touched is cooled first, so reads are priced as in a fresh tx.
    function _measureRun(uint256 id) internal returns (uint256) {
        vm.cool(address(wallet));
        vm.cool(address(recurring));
        vm.cool(address(module));
        vm.cool(address(registry));
        vm.cool(address(oracle));
        vm.cool(address(usdc));
        vm.cool(address(dai));
        vm.cool(address(treasury));
        vm.cool(payee);
        vm.cool(config.ethUsdPriceFeed);
        vm.cool(config.usdcUsdPriceFeed);
        vm.cool(config.daiUsdPriceFeed);
        bytes memory data = abi.encodeCall(RecurringPaymentModule.run, (address(wallet), id));
        vm.prank(keeper);
        uint256 before = gasleft();
        recurring.run(address(wallet), id);
        uint256 execution = before - gasleft();
        return execution + 21_000 + _calldataGas(data);
    }

    function _calldataGas(bytes memory data) internal pure returns (uint256 gas) {
        for (uint256 i = 0; i < data.length; i++) {
            gas += data[i] == 0 ? 4 : 16;
        }
    }

    function _handleOps(PackedUserOperation memory userOp) internal {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    function _deadline() internal view returns (uint48) {
        return uint48(block.timestamp + 30 days);
    }
}
