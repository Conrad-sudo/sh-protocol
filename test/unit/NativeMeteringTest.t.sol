// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Execution, MODULE_TYPE_HOOK} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
//Local imports
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockWeth} from "../../src/mocks/MockWeth.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";

/**
 * @title NativeMeteringTest
 * @notice Native ETH/BNB counts toward the USD cap only if the wallet chose that
 *         (`Config.nativeMetered`): set at deploy, changed by the owner with `setNativeMetering`.
 * @dev `wallet` is deployed with native metering OFF and USDC watched. Every execution here is the
 *      owner's own `execute`: the hook sees the account either way and cannot tell an owner from a
 *      session key, so what it meters for one it meters for the other. SessionGuardTest sends the
 *      same shapes as a passkey through the real EntryPoint.
 *      SHProtocolTest keeps the tests of a wallet with native metering ON, which is how every wallet
 *      behaved before the flag existed.
 */
contract NativeMeteringTest is Test {
    ERC20Mock usdc;
    ERC20Mock dai;
    MockWeth weth;
    SHOracle oracle;
    SHRegistry registry;
    SHTreasury treasury;
    SHFactory factory;
    HelperConfig.NetworkConfig config;
    SessionHandler wallet;
    SpendingLimitModule module;

    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;

    address owner;
    address kani = makeAddr("kani");
    address rando = makeAddr("rando");

    function setUp() public {
        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        registry = treasury.REGISTRY();
        module = SpendingLimitModule(registry.spendingLimitModule());
        owner = config.account;

        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);
        weth = MockWeth(payable(config.weth));

        address[] memory watched = new address[](1);
        watched[0] = address(usdc);
        wallet = _deploy(DAILY_LIMIT, watched, false);

        // Backs the mock so an unwrap can pay the wallet.
        vm.deal(address(weth), 100 ether);
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    /// @dev A funded wallet owned by `owner`, with no passkey.
    function _deploy(int256 limit, address[] memory watched, bool nativeMetered)
        internal
        returns (SessionHandler deployed)
    {
        vm.prank(owner);
        deployed = SessionHandler(
            payable(factory.deployWallet(limit, WINDOW, watched, nativeMetered, bytes32(0), bytes32(0), 0, new address[](0)))
        );
        vm.deal(address(deployed), 10 ether);
        usdc.mint(address(deployed), 10_000e6);
        dai.mint(address(deployed), 10_000e18);
        weth.mint(address(deployed), 100e18);
    }

    /// @dev Owner-direct single-call execute (the hook applies).
    function _execute(SessionHandler from, address dest, uint256 value, bytes memory data) internal {
        vm.prank(owner);
        from.execute(bytes32(0), abi.encodePacked(dest, value, data));
    }

    function _executeBatch(SessionHandler from, Execution[] memory execs) internal {
        vm.prank(owner);
        from.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));
    }

    function _spent(SessionHandler from) internal view returns (int256) {
        return from.getConfig().spentInWindow;
    }

    /// @dev How many SpendMetered events the recorded logs hold.
    function _spendEvents() internal returns (uint256 count) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == SpendingLimitModule.SpendMetered.selector) count++;
        }
    }

    /*//////////////////////////////////////////////////////////////
                         THE CHOICE AT DEPLOY
    //////////////////////////////////////////////////////////////*/

    function test_deploy_storesTheChoice() public {
        assertFalse(wallet.getConfig().nativeMetered, "deployed with native metering off");

        SessionHandler metered = _deploy(DAILY_LIMIT, new address[](0), true);
        assertTrue(metered.getConfig().nativeMetered, "deployed with native metering on");
    }

    /// @notice The flag is the second byte of Config's one slot, beside the install flag.
    function test_flag_sitsInTheConfigSlot() public {
        // _configs is the module's first storage variable.
        bytes32 slot = keccak256(abi.encode(address(wallet), uint256(0)));
        assertEq(uint256(vm.load(address(module), slot)) & 0xffff, 0x0001, "installed, native off");

        vm.prank(owner);
        wallet.setNativeMetering(true, address(0));
        assertEq(uint256(vm.load(address(module), slot)) & 0xffff, 0x0101, "installed, native on");
    }

    /*//////////////////////////////////////////////////////////////
                    OFF: NATIVE IS OUTSIDE THE CAP
    //////////////////////////////////////////////////////////////*/

    /// @notice Native worth more than the whole limit leaves in one execution, and nothing is charged.
    function test_off_nativeSend_isNotCharged() public {
        uint256 amount = 9 ether;
        assertGt(oracle.getPrice(address(0), amount), DAILY_LIMIT, "test mis-sized: must exceed the limit");

        vm.recordLogs();
        _execute(wallet, kani, amount, "");

        assertEq(kani.balance, amount);
        assertEq(_spent(wallet), 0, "native was charged while unmetered");
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT);
        assertEq(_spendEvents(), 0, "nothing was metered, so nothing is emitted");
    }

    /// @notice No price is read for it, so the oracle is never looked up and no token is read.
    function test_off_nativeSend_readsNoPriceAndNoToken() public {
        vm.expectCall(address(registry), abi.encodeWithSignature("priceOracle()"), 0);
        vm.expectCall(address(usdc), abi.encodeCall(IERC20.balanceOf, (address(wallet))), 0);

        _execute(wallet, kani, 1 ether, "");
    }

    /// @notice A stale native feed blocks a native send only on a wallet that meters native.
    function test_off_nativeSend_survivesAStaleNativeFeed() public {
        SessionHandler metered = _deploy(DAILY_LIMIT, new address[](0), true);
        vm.warp(block.timestamp + 365 days); // every feed is stale now

        _execute(wallet, kani, 1 ether, "");
        assertEq(kani.balance, 1 ether);

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SHOracle.PriceOracle_StalePrice.selector, config.ethUsdPriceFeed));
        metered.execute(bytes32(0), abi.encodePacked(kani, uint256(1 ether), ""));
    }

    /// @notice The half that keeps the cap whole: native coming IN earns no credit. Selling a watched
    ///         token for native is charged the full amount sold. If the inflow offset it, the token
    ///         could be turned into native at no charge and the native then sent out unmetered.
    function test_off_nativeInflow_offsetsNothing() public {
        uint256 usdcOut = 3_000e6;
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, usdcOut)));
        execs[1] = Execution(address(weth), 0, abi.encodeCall(MockWeth.withdraw, (1 ether)));

        uint256 nativeBefore = address(wallet).balance;
        _executeBatch(wallet, execs);

        assertEq(address(wallet).balance, nativeBefore + 1 ether, "the native did come in");
        assertEq(_spent(wallet), oracle.getPrice(address(usdc), usdcOut), "a native inflow earned credit");
    }

    /// @notice The same batch on a wallet that meters native nets the two, as it always did.
    function test_on_nativeInflow_stillOffsets() public {
        address[] memory watched = new address[](1);
        watched[0] = address(usdc);
        SessionHandler metered = _deploy(DAILY_LIMIT, watched, true);

        uint256 usdcOut = 3_000e6;
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, usdcOut)));
        execs[1] = Execution(address(weth), 0, abi.encodeCall(MockWeth.withdraw, (1 ether)));
        _executeBatch(metered, execs);

        int256 net = oracle.getPrice(address(usdc), usdcOut) - oracle.getPrice(address(0), 1 ether);
        assertGt(net, 0, "test mis-sized: expected a positive net");
        assertEq(_spent(metered), net);
    }

    /// @notice Paying native for a watched token charges nothing, and banks nothing for later.
    function test_off_buyingAWatchedTokenWithNative_chargesNothingAndBanksNoCredit() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(kani, 1 ether, "");
        execs[1] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.mint, (address(wallet), 2_000e6)));
        _executeBatch(wallet, execs);
        assertEq(_spent(wallet), 0, "a net inflow of the watched token adds nothing");

        _execute(wallet, address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1_000e6)));
        assertEq(_spent(wallet), oracle.getPrice(address(usdc), 1_000e6), "the earlier inflow bought no headroom");
    }

    /// @notice Watched tokens are bound exactly as before: the limit still refuses them.
    function test_off_watchedTokens_areStillCapped() public {
        uint256 amount = 6_000e6;
        int256 usd = oracle.getPrice(address(usdc), amount);
        assertGt(usd, DAILY_LIMIT);

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, usd, DAILY_LIMIT)
        );
        wallet.execute(
            bytes32(0), abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.transfer, (kani, amount)))
        );
    }

    /// @notice Why the app moves the wrapped token with the flag. With native off and the wrapped
    ///         token still watched, a wrap is free but an unwrap is charged as if the token were spent.
    function test_off_withTheWrappedTokenStillWatched_anUnwrapIsCharged() public {
        // Neither counted: wrapping and unwrapping cost nothing.
        _execute(wallet, address(weth), 1 ether, "");
        _execute(wallet, address(weth), 0, abi.encodeCall(MockWeth.withdraw, (1 ether)));
        assertEq(_spent(wallet), 0);

        vm.prank(owner);
        wallet.addWatchedToken(address(weth));

        _execute(wallet, address(weth), 1 ether, "");
        assertEq(_spent(wallet), 0, "a wrap is an inflow of the watched token");

        _execute(wallet, address(weth), 0, abi.encodeCall(MockWeth.withdraw, (1 ether)));
        assertEq(_spent(wallet), oracle.getPrice(address(weth), 1 ether), "an unwrap is charged in full");
    }

    /*//////////////////////////////////////////////////////////////
                  NOTHING COUNTED: A WALLET WITH NO LIMIT
    //////////////////////////////////////////////////////////////*/

    /// @notice Native off, no watched tokens and a limit of 0 is a wallet with no USD cap: nothing is
    ///         ever charged, so the zero limit refuses nothing.
    function test_nothingCounted_aZeroLimitRefusesNothing() public {
        SessionHandler free = _deploy(0, new address[](0), false);
        assertEq(free.getConfig().dailyLimitUsd, 0);

        _execute(free, kani, 9 ether, "");
        _execute(free, address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 10_000e6)));

        assertEq(kani.balance, 9 ether);
        assertEq(usdc.balanceOf(kani), 10_000e6);
        assertEq(_spent(free), 0);
        assertEq(free.getRemainingBudget(), 0);
    }

    /// @notice Counting something on such a wallet freezes it until the owner sets a limit: the safe
    ///         direction. The app asks for the limit first.
    function test_nothingCounted_countingSomethingFreezesItUntilALimitIsSet() public {
        SessionHandler free = _deploy(0, new address[](0), false);

        vm.startPrank(owner);
        free.addWatchedToken(address(usdc));
        int256 usd = oracle.getPrice(address(usdc), 1e6);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, usd, int256(0))
        );
        free.execute(
            bytes32(0), abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)))
        );

        free.setNativeMetering(true, address(0));
        int256 nativeUsd = oracle.getPrice(address(0), 1 gwei);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, nativeUsd, int256(0)
            )
        );
        free.execute(bytes32(0), abi.encodePacked(kani, uint256(1 gwei), ""));

        free.setDailyLimit(DAILY_LIMIT);
        free.execute(
            bytes32(0), abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)))
        );
        vm.stopPrank();
        assertEq(_spent(free), usd);
    }

    /*//////////////////////////////////////////////////////////////
                         THE OWNER'S SWITCH
    //////////////////////////////////////////////////////////////*/

    /// @notice One owner call turns metering on and puts the wrapped token on the watched list, so a
    ///         send is charged and a wrap nets out.
    function test_switchOn_chargesNativeAndWatchesTheWrappedToken() public {
        vm.expectEmit(true, false, false, false, address(module));
        emit SpendingLimitModule.ConfigUpdated(address(wallet));
        vm.expectEmit(true, true, false, false, address(module));
        emit SpendingLimitModule.WatchedTokenAdded(address(wallet), address(weth));
        vm.prank(owner);
        wallet.setNativeMetering(true, address(weth));

        assertTrue(wallet.getConfig().nativeMetered);
        assertTrue(wallet.isWatched(address(weth)));

        _execute(wallet, kani, 1 ether, "");
        int256 sendUsd = oracle.getPrice(address(0), 1 ether);
        assertEq(_spent(wallet), sendUsd, "native is charged once metering is on");

        _execute(wallet, address(weth), 1 ether, "");
        assertEq(_spent(wallet), sendUsd, "a wrap nets to nothing");
    }

    /// @notice And off again: the flag clears, the wrapped token leaves the list, native is free.
    function test_switchOff_stopsChargingAndUnwatchesTheWrappedToken() public {
        vm.startPrank(owner);
        wallet.setNativeMetering(true, address(weth));

        vm.expectEmit(true, false, false, false, address(module));
        emit SpendingLimitModule.ConfigUpdated(address(wallet));
        vm.expectEmit(true, true, false, false, address(module));
        emit SpendingLimitModule.WatchedTokenRemoved(address(wallet), address(weth));
        wallet.setNativeMetering(false, address(weth));
        vm.stopPrank();

        assertFalse(wallet.getConfig().nativeMetered);
        assertFalse(wallet.isWatched(address(weth)));
        assertTrue(wallet.isWatched(address(usdc)), "the other watched token is untouched");

        _execute(wallet, kani, 1 ether, "");
        assertEq(_spent(wallet), 0);
    }

    /// @notice What was spent before the switch stays on the tally.
    function test_switchOff_keepsWhatWasAlreadySpent() public {
        vm.prank(owner);
        wallet.setNativeMetering(true, address(0));
        _execute(wallet, kani, 1 ether, "");
        int256 spent = _spent(wallet);
        assertGt(spent, 0);

        vm.prank(owner);
        wallet.setNativeMetering(false, address(0));

        assertEq(_spent(wallet), spent);
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT - spent);
    }

    /// @notice address(0) changes the flag and leaves the watched list as it was.
    function test_switch_withNoWrappedToken_leavesTheListAlone() public {
        vm.startPrank(owner);
        wallet.setNativeMetering(true, address(0));
        assertTrue(wallet.getConfig().nativeMetered);
        assertEq(wallet.getConfig().watchedTokens.length, 1);

        wallet.addWatchedToken(address(weth));
        wallet.setNativeMetering(false, address(0));
        vm.stopPrank();

        assertFalse(wallet.getConfig().nativeMetered);
        assertTrue(wallet.isWatched(address(weth)), "the wrapped token was not named, so it stays");
    }

    /// @notice Both halves or neither: if the wrapped token can't join the list, the flag doesn't move.
    function test_switch_isAllOrNothing() public {
        ERC20Mock unpriced = new ERC20Mock("Unpriced", "UNP", 18);
        vm.startPrank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(unpriced))
        );
        wallet.setNativeMetering(true, address(unpriced));
        assertFalse(wallet.getConfig().nativeMetered, "the flag moved though the list change failed");

        // Capped in its own units, so it can't also be watched.
        wallet.setTokenCap(address(weth), 1e18);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenAlreadyMetered.selector, address(weth))
        );
        wallet.setNativeMetering(true, address(weth));
        vm.stopPrank();
        assertFalse(wallet.getConfig().nativeMetered);
    }

    /*//////////////////////////////////////////////////////////////
                    WHO MAY FLIP IT: THE OWNER ONLY
    //////////////////////////////////////////////////////////////*/

    function test_switch_isOwnerOnly() public {
        vm.prank(rando);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, rando));
        wallet.setNativeMetering(true, address(0));

        // The module keys its config by caller, so a stranger reaches only its own (empty) entry.
        vm.prank(rando);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_NotInstalled.selector);
        module.setNativeMetering(true);

        assertFalse(wallet.getConfig().nativeMetered);
    }

    /// @notice The setter is on the module's admin list, so no execution reaches it: a session key
    ///         that could switch metering off would lift the cap on native by itself.
    function test_switch_isRefusedThroughExecute() public {
        vm.prank(owner);
        wallet.setNativeMetering(true, address(0));
        bytes memory off = abi.encodeCall(SpendingLimitModule.setNativeMetering, (false));

        // Single call, on the owner's own execute path (the hook can't tell it from a key's).
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_AdminExecution.selector);
        wallet.execute(bytes32(0), abi.encodePacked(address(module), uint256(0), off));

        // Buried in a batch.
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        execs[1] = Execution(address(module), 0, off);
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_AdminExecution.selector);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        // A non-owner execution (a passkey's op arrives from the EntryPoint) is stopped a layer
        // earlier, by the wallet's own guard, whether it aims at the module or at the wallet.
        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(module))
        );
        wallet.execute(bytes32(0), abi.encodePacked(address(module), uint256(0), off));

        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(wallet))
        );
        wallet.execute(
            bytes32(0),
            abi.encodePacked(
                address(wallet), uint256(0), abi.encodeCall(SessionHandler.setNativeMetering, (false, address(0)))
            )
        );

        assertTrue(wallet.getConfig().nativeMetered, "metering was switched off through execute");
        assertEq(usdc.balanceOf(kani), 0, "the batch was not reverted whole");
    }

    /*//////////////////////////////////////////////////////////////
               ONE EXECUTION IS METERED ONE WAY THROUGHOUT
    //////////////////////////////////////////////////////////////*/

    /// @notice preCheck makes the decision and postCheck follows it. Here native is unmetered at
    ///         preCheck and switched on before postCheck: the native that left is still not charged.
    function test_decisionMadeInPreCheck_offStaysOff() public {
        vm.prank(address(wallet));
        bytes memory hookData = module.preCheck(address(0), 0, "");
        (bool metered, uint256 nativeBefore,,,,) =
            abi.decode(hookData, (bool, uint256, address[], uint256[], address[], address[]));
        assertFalse(metered);
        assertEq(nativeBefore, 0, "an unmetered balance is not read");

        vm.prank(address(wallet));
        module.setNativeMetering(true);
        // A watched token leaves in between. Were postCheck to read the flag afresh, it would take the
        // wallet's whole native balance for an inflow (the snapshot holds 0) and offset this with it.
        vm.prank(address(wallet));
        usdc.transfer(kani, 1_000e6);

        vm.prank(address(wallet));
        module.postCheck(hookData);
        assertEq(_spent(wallet), oracle.getPrice(address(usdc), 1_000e6));
    }

    /// @notice The other way round: metered at preCheck, switched off before postCheck, still charged.
    function test_decisionMadeInPreCheck_onStaysOn() public {
        vm.prank(owner);
        wallet.setNativeMetering(true, address(0));

        vm.prank(address(wallet));
        bytes memory hookData = module.preCheck(address(0), 0, "");
        (bool metered, uint256 nativeBefore,,,,) =
            abi.decode(hookData, (bool, uint256, address[], uint256[], address[], address[]));
        assertTrue(metered);
        assertEq(nativeBefore, address(wallet).balance);

        vm.prank(address(wallet));
        module.setNativeMetering(false);
        vm.deal(address(wallet), address(wallet).balance - 1 ether);

        vm.prank(address(wallet));
        module.postCheck(hookData);
        assertEq(_spent(wallet), oracle.getPrice(address(0), 1 ether));
    }

    /*//////////////////////////////////////////////////////////////
                              REINSTALL
    //////////////////////////////////////////////////////////////*/

    /// @notice Uninstalling wipes the flag with the rest of the config; a reinstall sets it afresh.
    function test_reinstall_takesTheChoiceGivenThen() public {
        vm.startPrank(owner);
        wallet.setNativeMetering(true, address(0));
        wallet.uninstallModule(MODULE_TYPE_HOOK, address(module), "");
        assertFalse(module.getConfig(address(wallet)).nativeMetered, "uninstall left the flag behind");

        wallet.installModule(
            MODULE_TYPE_HOOK, address(module), abi.encode(DAILY_LIMIT, WINDOW, new address[](0), true)
        );
        vm.stopPrank();
        assertTrue(wallet.getConfig().nativeMetered);
    }
}
