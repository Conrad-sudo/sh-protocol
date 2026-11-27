// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

import {Test} from "forge-std/Test.sol";
import {IAppCheck} from "../../src/interfaces/IAppCheck.sol";
import {AppCheckBase} from "../../src/appchecks/AppCheckBase.sol";
import {SwapRouter02Check} from "../../src/appchecks/SwapRouter02Check.sol";
import {V3PositionManagerCheck} from "../../src/appchecks/V3PositionManagerCheck.sol";
import {V2RouterCheck} from "../../src/appchecks/V2RouterCheck.sol";
import {WrappedNativeCheck} from "../../src/appchecks/WrappedNativeCheck.sol";
import {ReputationCheck} from "../../src/appchecks/ReputationCheck.sol";
import {IReputationRegistry} from "../../src/interfaces/IReputationRegistry.sol";

/*//////////////////////////////////////////////////////////////
       MOCKS: just the reads the checks make of the real apps
//////////////////////////////////////////////////////////////*/

contract MockSwapRouter {
    address public immutable WETH9;

    constructor(address weth) {
        WETH9 = weth;
    }
}

contract MockV3Pool {
    uint160 public sqrtPriceX96;

    constructor(uint160 sqrtPrice) {
        sqrtPriceX96 = sqrtPrice;
    }

    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool) {
        return (sqrtPriceX96, 0, 0, 0, 0, 0, true);
    }
}

contract MockV3Factory {
    mapping(bytes32 => address) public pools;

    function setPool(address a, address b, uint24 fee, address pool) external {
        pools[keccak256(abi.encode(a, b, fee))] = pool;
        pools[keccak256(abi.encode(b, a, fee))] = pool;
    }

    function getPool(address a, address b, uint24 fee) external view returns (address) {
        return pools[keccak256(abi.encode(a, b, fee))];
    }
}

contract MockPositionManager {
    address public immutable WETH9;
    address public immutable factory;
    mapping(uint256 => address) public ownerOf;
    mapping(uint256 => address[2]) private _tokens;
    mapping(uint256 => uint24) private _fee;

    constructor(address weth, address factory_) {
        WETH9 = weth;
        factory = factory_;
    }

    function setPosition(uint256 id, address owner, address token0, address token1, uint24 fee) external {
        ownerOf[id] = owner;
        _tokens[id] = [token0, token1];
        _fee[id] = fee;
    }

    function positions(uint256 id)
        external
        view
        returns (
            uint96, address, address, address, uint24, int24, int24, uint128, uint256, uint256, uint128, uint128
        )
    {
        return (0, address(0), _tokens[id][0], _tokens[id][1], _fee[id], 0, 0, 0, 0, 0, 0, 0);
    }
}

contract MockV2Pair {
    address public immutable token0;
    uint112 private _r0;
    uint112 private _r1;

    constructor(address t0, uint112 r0, uint112 r1) {
        token0 = t0;
        _r0 = r0;
        _r1 = r1;
    }

    function getReserves() external view returns (uint112, uint112, uint32) {
        return (_r0, _r1, 0);
    }
}

contract MockV2Factory {
    mapping(bytes32 => address) private _pairs;

    function setPair(address a, address b, address pair) external {
        _pairs[keccak256(abi.encode(a, b))] = pair;
        _pairs[keccak256(abi.encode(b, a))] = pair;
    }

    function getPair(address a, address b) external view returns (address) {
        return _pairs[keccak256(abi.encode(a, b))];
    }
}

contract MockV2Router {
    address public immutable WETH;
    address public immutable factory;

    constructor(address weth, address factory_) {
        WETH = weth;
        factory = factory_;
    }
}

/**
 * @title AppChecksTest
 * @notice Each app check's report for the calls Mitfah makes, and its refusals, against mocks of just
 *         the reads it makes. The fork suites run the same checks against the real routers.
 */
contract AppChecksTest is Test {
    address account = makeAddr("wallet");
    address bob = makeAddr("bob");
    address weth = makeAddr("weth");
    address usdc = makeAddr("usdc");
    address dai = makeAddr("dai");

    SwapRouter02Check routerCheck;
    MockSwapRouter router;
    V3PositionManagerCheck pmCheck;
    MockPositionManager pm;
    MockV3Factory v3Factory;
    V2RouterCheck v2Check;
    MockV2Router v2Router;
    MockV2Factory v2Factory;

    bytes4 constant MULTICALL_DEADLINE = 0x5ae401dc;
    bytes4 constant MULTICALL = 0xac9650d8;

    function setUp() public {
        router = new MockSwapRouter(weth);
        routerCheck = new SwapRouter02Check(address(router));
        v3Factory = new MockV3Factory();
        pm = new MockPositionManager(weth, address(v3Factory));
        pmCheck = new V3PositionManagerCheck(address(pm));
        v2Factory = new MockV2Factory();
        v2Router = new MockV2Router(weth, address(v2Factory));
        v2Check = new V2RouterCheck(address(v2Router));
    }

    function _unsupported(address app, bytes4 selector) internal pure returns (bytes memory) {
        return abi.encodeWithSelector(AppCheckBase.AppCheck_UnsupportedCall.selector, app, selector);
    }

    /*//////////////////////////////////////////////////////////////
                              SWAPROUTER02
    //////////////////////////////////////////////////////////////*/

    function _exactInputSingle(address tokenIn, address tokenOut, address recipient, uint256 amountIn, uint256 minOut)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodeWithSelector(
            0x04e45aaf,
            SwapRouter02Check.ExactInputSingleParams(tokenIn, tokenOut, 500, recipient, amountIn, minOut, 0)
        );
    }

    function _multicall(bytes[] memory calls) internal pure returns (bytes memory) {
        return abi.encodeWithSelector(MULTICALL_DEADLINE, uint256(1e12), calls);
    }

    function _two(bytes memory a, bytes memory b) internal pure returns (bytes[] memory calls) {
        calls = new bytes[](2);
        calls[0] = a;
        calls[1] = b;
    }

    function test_router_exactInputSingle_reportsTradeAndRecipient() public view {
        (address[] memory payees, IAppCheck.Trade[] memory trades,) =
            routerCheck.inspect(account, 0, _exactInputSingle(usdc, dai, bob, 100e6, 90e18));
        assertEq(payees.length, 1);
        assertEq(payees[0], bob);
        assertEq(trades.length, 1);
        assertEq(trades[0].tokenIn, usdc);
        assertEq(trades[0].maxIn, 100e6);
        assertEq(trades[0].tokenOut, dai);
        assertEq(trades[0].minOut, 90e18);
    }

    /// @notice MSG_SENDER (address(1)) is the wallet itself.
    function test_router_msgSenderRecipient_isTheWallet() public view {
        (address[] memory payees,,) =
            routerCheck.inspect(account, 0, _exactInputSingle(usdc, dai, address(1), 100e6, 90e18));
        assertEq(payees[0], account);
    }

    /// @notice Native out as the app builds it: the swap pays the router, unwrapWETH9 pays the recipient.
    function test_router_nativeOut_parksThenUnwraps() public view {
        bytes memory data = _multicall(
            _two(
                _exactInputSingle(usdc, weth, address(router), 100e6, 1e16),
                abi.encodeWithSelector(0x49404b7c, uint256(1e16), bob)
            )
        );
        (address[] memory payees, IAppCheck.Trade[] memory trades,) = routerCheck.inspect(account, 0, data);
        assertEq(payees.length, 1);
        assertEq(payees[0], bob);
        assertEq(trades[0].tokenOut, weth);
    }

    /// @notice Output left in the router (parked, never paid out) is refused: anyone could sweep it.
    function test_router_outputLeftInTheRouter_isRefused() public {
        bytes[] memory calls = new bytes[](1);
        calls[0] = _exactInputSingle(usdc, dai, address(2), 100e6, 90e18);
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_FundsLeftInApp.selector, address(router), dai));
        routerCheck.inspect(account, 0, _multicall(calls));
    }

    /// @notice Unwrapping when the parked token isn't WETH leaves it parked: refused.
    function test_router_unwrapDoesNotDrainAnotherToken() public {
        bytes memory data = _multicall(
            _two(
                _exactInputSingle(usdc, dai, address(router), 100e6, 90e18),
                abi.encodeWithSelector(0x49404b7c, uint256(0), bob)
            )
        );
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_FundsLeftInApp.selector, address(router), dai));
        routerCheck.inspect(account, 0, data);
    }

    function test_router_sweepDrainsAndNamesItsRecipient() public view {
        bytes memory data = _multicall(
            _two(
                _exactInputSingle(usdc, dai, address(2), 100e6, 90e18),
                abi.encodeWithSelector(0xdf2ab5bb, dai, uint256(0), bob)
            )
        );
        (address[] memory payees,,) = routerCheck.inspect(account, 0, data);
        assertEq(payees.length, 1);
        assertEq(payees[0], bob);
    }

    /// @notice Native in, exact input: the value is spent exactly, so no refund is needed.
    function test_router_nativeIn_exactValue_isAllowed() public view {
        bytes[] memory calls = new bytes[](1);
        calls[0] = _exactInputSingle(weth, usdc, account, 1 ether, 900e6);
        routerCheck.inspect(account, 1 ether, _multicall(calls));
    }

    /// @notice Native in that the swaps don't fully spend must end with refundETH.
    function test_router_nativeLeftOver_needsRefundEth() public {
        bytes[] memory calls = new bytes[](1);
        calls[0] = _exactInputSingle(weth, usdc, account, 0.5 ether, 450e6);
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_NativeLeftInApp.selector, address(router)));
        routerCheck.inspect(account, 1 ether, _multicall(calls));

        routerCheck.inspect(
            account, 1 ether, _multicall(_two(calls[0], abi.encodeWithSelector(bytes4(0x12210e8a))))
        );
    }

    /// @notice An exact-output swap from WETH may not use all the value: it needs refundETH last.
    function test_router_nativeExactOutput_needsRefundEth() public {
        bytes memory swap = abi.encodeWithSelector(
            0x5023b4df, SwapRouter02Check.ExactOutputSingleParams(weth, usdc, 500, account, 900e6, 1 ether, 0)
        );
        bytes[] memory calls = new bytes[](1);
        calls[0] = swap;
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_NativeLeftInApp.selector, address(router)));
        routerCheck.inspect(account, 1 ether, _multicall(calls));
        routerCheck.inspect(account, 1 ether, _multicall(_two(swap, abi.encodeWithSelector(bytes4(0x12210e8a)))));
    }

    /// @notice Multi-hop paths: exact input reads first→last, exact output last→first.
    function test_router_paths() public view {
        bytes memory path = abi.encodePacked(usdc, uint24(500), weth, uint24(3000), dai);
        bytes memory exactIn = abi.encodeWithSelector(
            0xb858183f, SwapRouter02Check.ExactInputParams(path, account, 100e6, 90e18)
        );
        (, IAppCheck.Trade[] memory trades,) = routerCheck.inspect(account, 0, exactIn);
        assertEq(trades[0].tokenIn, usdc);
        assertEq(trades[0].tokenOut, dai);

        bytes memory exactOut = abi.encodeWithSelector(
            0x09b81346, SwapRouter02Check.ExactOutputParams(path, account, 90e18, 100e6)
        );
        (, trades,) = routerCheck.inspect(account, 0, exactOut);
        assertEq(trades[0].tokenOut, usdc, "an exact-output path starts with the token bought");
        assertEq(trades[0].tokenIn, dai);
        assertEq(trades[0].maxIn, 100e6);
        assertEq(trades[0].minOut, 90e18);
    }

    function test_router_badPath_isRefused() public {
        bytes memory exactIn = abi.encodeWithSelector(
            0xb858183f, SwapRouter02Check.ExactInputParams(abi.encodePacked(usdc, uint24(500)), account, 1, 1)
        );
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_BadPath.selector, address(router)));
        routerCheck.inspect(account, 0, exactIn);
    }

    /// @notice CONTRACT_BALANCE (amountIn 0) trades whatever the router holds: unreadable, refused.
    function test_router_contractBalanceAmount_isRefused() public {
        vm.expectRevert(_unsupported(address(router), 0x04e45aaf));
        routerCheck.inspect(account, 0, _exactInputSingle(usdc, dai, account, 0, 0));
    }

    /// @notice Nothing but the listed functions, and no multicall inside a multicall.
    function test_router_unknownAndNestedCalls_areRefused() public {
        vm.expectRevert(_unsupported(address(router), bytes4(keccak256("wrapETH(uint256)"))));
        routerCheck.inspect(account, 0, abi.encodeWithSignature("wrapETH(uint256)", 1));

        bytes[] memory inner = new bytes[](1);
        inner[0] = _exactInputSingle(usdc, dai, account, 1, 1);
        bytes[] memory outer = new bytes[](1);
        outer[0] = abi.encodeWithSelector(MULTICALL, inner);
        vm.expectRevert(_unsupported(address(router), MULTICALL));
        routerCheck.inspect(account, 0, _multicall(outer));
    }

    /*//////////////////////////////////////////////////////////////
                       V3 POSITION MANAGER
    //////////////////////////////////////////////////////////////*/

    function _mint(address recipient) internal view returns (bytes memory) {
        return abi.encodeWithSelector(
            0x88316456,
            V3PositionManagerCheck.MintParams(usdc, weth, 500, -10, 10, 100e6, 1e17, 0, 0, recipient, 1e12)
        );
    }

    function _withPool(uint160 sqrtPrice) internal {
        v3Factory.setPool(usdc, weth, 500, address(new MockV3Pool(sqrtPrice)));
    }

    function test_pm_mint_reportsRecipientAndPoolPrice() public {
        _withPool(uint160(2 ** 96)); // price 1
        (address[] memory payees,, IAppCheck.PoolPrice[] memory prices) = pmCheck.inspect(account, 0, _mint(bob));
        assertEq(payees[0], bob);
        assertEq(prices.length, 1);
        assertEq(prices[0].token0, usdc);
        assertEq(prices[0].token1, weth);
        assertEq(prices[0].amount0, 1e18);
        assertEq(prices[0].amount1, 1e18, "price 1 means equal amounts");
    }

    /// @notice Price above and below one: amounts in the ratio the pool trades at.
    function test_pm_poolPrice_bothSidesOfOne() public {
        _withPool(uint160(2 * 2 ** 96)); // sqrt 2 -> price 4
        (,, IAppCheck.PoolPrice[] memory prices) = pmCheck.inspect(account, 0, _mint(account));
        assertEq(prices[0].amount0, 1e18);
        assertEq(prices[0].amount1, 4e18);

        _withPool(uint160(2 ** 95)); // sqrt 1/2 -> price 1/4
        (,, prices) = pmCheck.inspect(account, 0, _mint(account));
        assertEq(prices[0].amount0, 4e18);
        assertEq(prices[0].amount1, 1e18);
    }

    function test_pm_mintIntoAMissingPool_isRefused() public {
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_NoPool.selector, address(pm)));
        pmCheck.inspect(account, 0, _mint(account));
    }

    /// @notice Adding to someone else's position hands them the deposit: refused.
    function test_pm_increaseLiquidity_onlyOnTheWalletsOwnPosition() public {
        _withPool(uint160(2 ** 96));
        pm.setPosition(7, bob, usdc, weth, 500);
        bytes memory increase = abi.encodeWithSelector(
            0x219f5d17, V3PositionManagerCheck.IncreaseLiquidityParams(7, 1, 1, 0, 0, 1e12)
        );
        vm.expectRevert(abi.encodeWithSelector(V3PositionManagerCheck.AppCheck_NotYourPosition.selector, 7));
        pmCheck.inspect(account, 0, increase);

        pm.setPosition(7, account, usdc, weth, 500);
        (,, IAppCheck.PoolPrice[] memory prices) = pmCheck.inspect(account, 0, increase);
        assertEq(prices.length, 1);
    }

    /// @notice Native out of a position as the app builds it: collect to the manager, then unwrap and
    ///         sweep both tokens to the recipient.
    function test_pm_collectNative_parksBothThenPaysOut() public {
        _withPool(uint160(2 ** 96));
        pm.setPosition(7, account, usdc, weth, 500);
        bytes[] memory calls = new bytes[](4);
        calls[0] = abi.encodeWithSelector(
            0x0c49ccbe, V3PositionManagerCheck.DecreaseLiquidityParams(7, 1000, 0, 0, 1e12)
        );
        calls[1] = abi.encodeWithSelector(
            0xfc6f7865, V3PositionManagerCheck.CollectParams(7, address(0), type(uint128).max, type(uint128).max)
        );
        calls[2] = abi.encodeWithSelector(0x49404b7c, uint256(0), bob);
        calls[3] = abi.encodeWithSelector(0xdf2ab5bb, usdc, uint256(0), bob);
        (address[] memory payees,, IAppCheck.PoolPrice[] memory prices) =
            pmCheck.inspect(account, 0, abi.encodeWithSelector(MULTICALL, calls));
        assertEq(payees.length, 2);
        assertEq(prices.length, 1, "the withdrawal's pool price");

        // Without the sweep, the other token stays in the manager: refused.
        bytes[] memory short = new bytes[](3);
        short[0] = calls[0];
        short[1] = calls[1];
        short[2] = calls[2];
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_FundsLeftInApp.selector, address(pm), usdc));
        pmCheck.inspect(account, 0, abi.encodeWithSelector(MULTICALL, short));
    }

    function test_pm_collectToARecipient_namesThem() public view {
        bytes memory collect = abi.encodeWithSelector(
            0xfc6f7865, V3PositionManagerCheck.CollectParams(7, bob, type(uint128).max, type(uint128).max)
        );
        (address[] memory payees,,) = pmCheck.inspect(account, 0, collect);
        assertEq(payees[0], bob);
    }

    /// @notice The manager's ERC-721 functions can't hide in its multicall.
    function test_pm_nftTransferInsideAMulticall_isRefused() public {
        bytes[] memory calls = new bytes[](1);
        calls[0] = abi.encodeWithSignature("safeTransferFrom(address,address,uint256)", account, bob, 7);
        vm.expectRevert(_unsupported(address(pm), bytes4(calls[0])));
        pmCheck.inspect(account, 0, abi.encodeWithSelector(MULTICALL, calls));
    }

    function test_pm_valueNeedsRefundEthLast() public {
        _withPool(uint160(2 ** 96));
        bytes[] memory calls = new bytes[](1);
        calls[0] = _mint(account);
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_NativeLeftInApp.selector, address(pm)));
        pmCheck.inspect(account, 1 ether, abi.encodeWithSelector(MULTICALL, calls));

        bytes[] memory withRefund = new bytes[](2);
        withRefund[0] = calls[0];
        withRefund[1] = abi.encodeWithSelector(bytes4(0x12210e8a));
        pmCheck.inspect(account, 1 ether, abi.encodeWithSelector(MULTICALL, withRefund));
    }

    function test_pm_poolCreation_isRefused() public {
        bytes memory create = abi.encodeWithSignature(
            "createAndInitializePoolIfNecessary(address,address,uint24,uint160)", usdc, weth, uint24(500), uint160(1)
        );
        vm.expectRevert(_unsupported(address(pm), bytes4(create)));
        pmCheck.inspect(account, 0, create);
    }

    /*//////////////////////////////////////////////////////////////
                              V2 ROUTER
    //////////////////////////////////////////////////////////////*/

    function _path(address a, address b) internal pure returns (address[] memory path) {
        path = new address[](2);
        path[0] = a;
        path[1] = b;
    }

    function test_v2_swaps_reportTradeAndTo() public view {
        bytes memory exactIn = abi.encodeWithSelector(0x38ed1739, 100e6, 90e18, _path(usdc, dai), bob, 1e12);
        (address[] memory payees, IAppCheck.Trade[] memory trades,) = v2Check.inspect(account, 0, exactIn);
        assertEq(payees[0], bob);
        assertEq(trades[0].tokenIn, usdc);
        assertEq(trades[0].maxIn, 100e6);
        assertEq(trades[0].minOut, 90e18);

        bytes memory exactOut = abi.encodeWithSelector(0x8803dbee, 90e18, 100e6, _path(usdc, dai), account, 1e12);
        (, trades,) = v2Check.inspect(account, 0, exactOut);
        assertEq(trades[0].maxIn, 100e6);
        assertEq(trades[0].minOut, 90e18);
    }

    /// @notice A payable swap's input is the call's value.
    function test_v2_ethSwaps_tradeTheValue() public view {
        bytes memory ethIn = abi.encodeWithSelector(0x7ff36ab5, 900e6, _path(weth, usdc), account, 1e12);
        (, IAppCheck.Trade[] memory trades,) = v2Check.inspect(account, 1 ether, ethIn);
        assertEq(trades[0].tokenIn, weth);
        assertEq(trades[0].maxIn, 1 ether);

        bytes memory ethForExact = abi.encodeWithSelector(0xfb3bdb41, 900e6, _path(weth, usdc), account, 1e12);
        (, trades,) = v2Check.inspect(account, 2 ether, ethForExact);
        assertEq(trades[0].maxIn, 2 ether);
        assertEq(trades[0].minOut, 900e6);
    }

    function test_v2_liquidity_reportsToAndThePairsPrice() public {
        v2Factory.setPair(usdc, weth, address(new MockV2Pair(weth, 10e18, 30_000e6)));
        bytes memory add = abi.encodeWithSelector(0xe8e33700, usdc, weth, 100e6, 1e17, 0, 0, bob, 1e12);
        (address[] memory payees,, IAppCheck.PoolPrice[] memory prices) = v2Check.inspect(account, 0, add);
        assertEq(payees[0], bob);
        assertEq(prices[0].token0, weth);
        assertEq(prices[0].amount0, 10e18);
        assertEq(prices[0].token1, usdc);
        assertEq(prices[0].amount1, 30_000e6);

        bytes memory removeEth = abi.encodeWithSelector(0x02751cec, usdc, 1, 0, 0, account, 1e12);
        (,, prices) = v2Check.inspect(account, 0, removeEth);
        assertEq(prices.length, 1);
    }

    /// @notice A first deposit into a pair that doesn't exist would set its price: refused.
    function test_v2_depositIntoAMissingPair_isRefused() public {
        bytes memory add = abi.encodeWithSelector(0xe8e33700, usdc, dai, 1, 1, 0, 0, account, 1e12);
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_NoPool.selector, address(v2Router)));
        v2Check.inspect(account, 0, add);
    }

    function test_v2_permitsAndUnknowns_areRefused() public {
        bytes memory withPermit = abi.encodeWithSignature(
            "removeLiquidityWithPermit(address,address,uint256,uint256,uint256,address,uint256,bool,uint8,bytes32,bytes32)",
            usdc,
            dai,
            1,
            0,
            0,
            account,
            1e12,
            false,
            uint8(27),
            bytes32(0),
            bytes32(0)
        );
        vm.expectRevert(_unsupported(address(v2Router), bytes4(withPermit)));
        v2Check.inspect(account, 0, withPermit);

        bytes memory shortPath = abi.encodeWithSelector(0x38ed1739, 1, 1, new address[](1), account, 1e12);
        vm.expectRevert(abi.encodeWithSelector(AppCheckBase.AppCheck_BadPath.selector, address(v2Router)));
        v2Check.inspect(account, 0, shortPath);
    }

    /*//////////////////////////////////////////////////////////////
                    WRAPPED NATIVE AND REPUTATION
    //////////////////////////////////////////////////////////////*/

    function test_wrappedNative_depositAndWithdrawOnly() public {
        WrappedNativeCheck check = new WrappedNativeCheck(weth);
        (address[] memory payees,,) = check.inspect(account, 1 ether, abi.encodeWithSignature("deposit()"));
        assertEq(payees.length, 0);
        check.inspect(account, 0, abi.encodeWithSignature("withdraw(uint256)", 1 ether));

        vm.expectRevert(_unsupported(weth, bytes4(keccak256("withdraw(uint256)"))));
        check.inspect(account, 1, abi.encodeWithSignature("withdraw(uint256)", 1 ether));
        vm.expectRevert(_unsupported(weth, bytes4(keccak256("flashLoan(uint256)"))));
        check.inspect(account, 0, abi.encodeWithSignature("flashLoan(uint256)", 1));
    }

    function test_reputation_reviewsOnly_withNoValue() public {
        address registry = makeAddr("reputation");
        ReputationCheck check = new ReputationCheck(registry);
        bytes memory revoke = abi.encodeCall(IReputationRegistry.revokeFeedback, (1, uint64(0)));
        check.inspect(account, 0, revoke);
        vm.expectRevert(_unsupported(registry, IReputationRegistry.revokeFeedback.selector));
        check.inspect(account, 1, revoke);
        vm.expectRevert(_unsupported(registry, bytes4(keccak256("transferOwnership(address)"))));
        check.inspect(account, 0, abi.encodeWithSignature("transferOwnership(address)", bob));
    }
}
