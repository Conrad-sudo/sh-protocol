// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {AppCheckBase} from "./AppCheckBase.sol";
import {IAppCheck} from "../interfaces/IAppCheck.sol";

/// @dev What the check reads of Uniswap V3's NonfungiblePositionManager.
interface IPositionManagerReads {
    function WETH9() external view returns (address);
    function factory() external view returns (address);
    function ownerOf(uint256 tokenId) external view returns (address);
    function positions(uint256 tokenId)
        external
        view
        returns (
            uint96 nonce,
            address operator,
            address token0,
            address token1,
            uint24 fee,
            int24 tickLower,
            int24 tickUpper,
            uint128 liquidity,
            uint256 feeGrowthInside0LastX128,
            uint256 feeGrowthInside1LastX128,
            uint128 tokensOwed0,
            uint128 tokensOwed1
        );
}

/// @dev What the check reads of the V3 factory.
interface IV3FactoryReads {
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address);
}

/**
 * @title V3PositionManagerCheck
 * @author Conrad Japhet
 * @notice The app check for Uniswap V3's NonfungiblePositionManager: opening, adding to, taking out of
 *         and closing liquidity positions, as Mitfah does them, alone or in a multicall.
 * @dev Understood, and nothing else:
 *        - `mint`: the new position's recipient is a payee (a position is worth what it holds), and the
 *          pool's price is reported.
 *        - `increaseLiquidity`: only on a position the wallet OWNS — anyone can mint a position to any
 *          address, and adding to someone else's hands them the deposit. The pool's price is reported.
 *        - `decreaseLiquidity`: the pool's price is reported (a withdrawal at a pushed price comes out
 *          skewed). The tokens stay owed to the position until `collect`.
 *        - `collect`: its recipient is a payee, except address(0) or the manager itself, which PARK
 *          both of the position's tokens for `unwrapWETH9` / `sweepToken` to pay out.
 *        - `burn`, `unwrapWETH9(uint256,address)`, `sweepToken(address,uint256,address)` (draining what
 *          was parked; the recipient is a payee), `refundETH`, and `multicall(bytes[])` around any of
 *          these, one level deep. The manager's ERC-721 functions are NOT accepted inside a multicall
 *          (it runs inner calls as delegatecalls to itself, so `safeTransferFrom` could hide there);
 *          called directly, the PayeeGuard reads them as token functions.
 *      Native value: a call that carries any must end with `refundETH`, since the manager keeps what a
 *      deposit doesn't use, where anyone's `refundETH` could take it.
 */
contract V3PositionManagerCheck is AppCheckBase {
    /// @notice The manager's wrapped native token.
    address public immutable WETH9;
    /// @notice The V3 factory its pools come from.
    address public immutable FACTORY;

    bytes4 private constant MINT = 0x88316456;
    bytes4 private constant INCREASE_LIQUIDITY = 0x219f5d17;
    bytes4 private constant DECREASE_LIQUIDITY = 0x0c49ccbe;
    bytes4 private constant COLLECT = 0xfc6f7865;
    bytes4 private constant BURN = 0x42966c68;
    bytes4 private constant MULTICALL = 0xac9650d8; // multicall(bytes[])
    bytes4 private constant UNWRAP_WETH9 = 0x49404b7c; // unwrapWETH9(uint256,address)
    bytes4 private constant SWEEP_TOKEN = 0xdf2ab5bb; // sweepToken(address,uint256,address)
    bytes4 private constant REFUND_ETH = 0x12210e8a; // refundETH()
    bytes4 private constant SLOT0 = 0x3850c7bd; // slot0()

    uint256 private constant Q96 = 2 ** 96;
    uint256 private constant PRICE_UNIT = 1e18;

    /// @notice The wallet tried to add liquidity to a position it doesn't own.
    error AppCheck_NotYourPosition(uint256 tokenId);

    struct MintParams {
        address token0;
        address token1;
        uint24 fee;
        int24 tickLower;
        int24 tickUpper;
        uint256 amount0Desired;
        uint256 amount1Desired;
        uint256 amount0Min;
        uint256 amount1Min;
        address recipient;
        uint256 deadline;
    }

    struct IncreaseLiquidityParams {
        uint256 tokenId;
        uint256 amount0Desired;
        uint256 amount1Desired;
        uint256 amount0Min;
        uint256 amount1Min;
        uint256 deadline;
    }

    struct DecreaseLiquidityParams {
        uint256 tokenId;
        uint128 liquidity;
        uint256 amount0Min;
        uint256 amount1Min;
        uint256 deadline;
    }

    struct CollectParams {
        uint256 tokenId;
        address recipient;
        uint128 amount0Max;
        uint128 amount1Max;
    }

    /// @param positionManager This chain's NonfungiblePositionManager.
    constructor(address positionManager) AppCheckBase(positionManager) {
        WETH9 = IPositionManagerReads(positionManager).WETH9();
        FACTORY = IPositionManagerReads(positionManager).factory();
    }

    /// @inheritdoc IAppCheck
    function inspect(address account, uint256 value, bytes calldata data)
        external
        view
        returns (address[] memory, Trade[] memory, PoolPrice[] memory)
    {
        bytes[] memory calls;
        if (data.length >= 4 && bytes4(data[:4]) == MULTICALL) {
            calls = abi.decode(data[4:], (bytes[]));
        } else {
            calls = new bytes[](1);
            calls[0] = data;
        }
        Report memory report = _newReport(calls.length);
        bytes4 last;
        for (uint256 i = 0; i < calls.length; i++) {
            last = _readCall(report, account, calls[i]);
        }
        if (value != 0 && last != REFUND_ETH) revert AppCheck_NativeLeftInApp(app);
        return _finish(report);
    }

    /// @dev Adds one (inner) call to the report and returns its selector.
    function _readCall(Report memory report, address account, bytes memory inner) internal view returns (bytes4 selector) {
        // The first four bytes: shorter calldata pads with zeros, which no selector here matches.
        // forge-lint: disable-next-line(unsafe-typecast)
        selector = bytes4(inner);
        if (selector == MINT) {
            MintParams memory p = abi.decode(_args(inner), (MintParams));
            _payee(report, p.recipient);
            _price(report, _poolPrice(p.token0, p.token1, p.fee));
        } else if (selector == INCREASE_LIQUIDITY) {
            IncreaseLiquidityParams memory p = abi.decode(_args(inner), (IncreaseLiquidityParams));
            if (IPositionManagerReads(app).ownerOf(p.tokenId) != account) revert AppCheck_NotYourPosition(p.tokenId);
            _price(report, _positionPoolPrice(p.tokenId));
        } else if (selector == DECREASE_LIQUIDITY) {
            DecreaseLiquidityParams memory p = abi.decode(_args(inner), (DecreaseLiquidityParams));
            _price(report, _positionPoolPrice(p.tokenId));
        } else if (selector == COLLECT) {
            CollectParams memory p = abi.decode(_args(inner), (CollectParams));
            if (p.recipient == address(0) || p.recipient == app) {
                (,, address token0, address token1,,,,,,,,) = IPositionManagerReads(app).positions(p.tokenId);
                _park(report, token0);
                _park(report, token1);
            } else {
                _payee(report, p.recipient);
            }
        } else if (selector == UNWRAP_WETH9) {
            (, address recipient) = abi.decode(_args(inner), (uint256, address));
            _drain(report, WETH9);
            _payee(report, recipient);
        } else if (selector == SWEEP_TOKEN) {
            (address token,, address recipient) = abi.decode(_args(inner), (address, uint256, address));
            _drain(report, token);
            _payee(report, recipient);
        } else if (selector != BURN && selector != REFUND_ETH) {
            revert AppCheck_UnsupportedCall(app, selector);
        }
    }

    /// @dev The price of the pool position `tokenId` sits in.
    function _positionPoolPrice(uint256 tokenId) internal view returns (PoolPrice memory) {
        (,, address token0, address token1, uint24 fee,,,,,,,) = IPositionManagerReads(app).positions(tokenId);
        return _poolPrice(token0, token1, fee);
    }

    /**
     * @dev The current price of the (token0, token1, fee) pool, as two amounts worth the same in it:
     *      1e18 base units of the token its price is above one for, and that many times the price of the
     *      other. Reverts if the pool doesn't exist or has no price yet.
     */
    function _poolPrice(address token0, address token1, uint24 fee) internal view returns (PoolPrice memory price) {
        address pool = IV3FactoryReads(FACTORY).getPool(token0, token1, fee);
        if (pool == address(0)) revert AppCheck_NoPool(app);
        (bool ok, bytes memory slot0) = pool.staticcall(abi.encodeWithSelector(SLOT0));
        if (!ok || slot0.length < 32) revert AppCheck_NoPool(app);
        // slot0's first word is the uint160 sqrtPriceX96.
        // forge-lint: disable-next-line(unsafe-typecast)
        uint256 sqrtPriceX96 = uint160(uint256(bytes32(slot0)));
        if (sqrtPriceX96 == 0) revert AppCheck_NoPool(app);
        // price = token1 per token0, in base units, = sqrtPriceX96^2 / 2^192.
        price.token0 = token0;
        price.token1 = token1;
        if (sqrtPriceX96 >= Q96) {
            price.amount0 = PRICE_UNIT;
            price.amount1 = Math.mulDiv(Math.mulDiv(PRICE_UNIT, sqrtPriceX96, Q96), sqrtPriceX96, Q96);
        } else {
            price.amount1 = PRICE_UNIT;
            price.amount0 = Math.mulDiv(Math.mulDiv(PRICE_UNIT, Q96, sqrtPriceX96), Q96, sqrtPriceX96);
        }
    }
}
