// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {AppCheckBase} from "./AppCheckBase.sol";
import {IAppCheck} from "../interfaces/IAppCheck.sol";

/// @dev What the check reads of a Uniswap-V2-style router (PancakeSwap's on BSC).
interface IV2RouterReads {
    function WETH() external view returns (address);
    function factory() external view returns (address);
}

/// @dev What the check reads of the V2 factory and its pairs.
interface IV2FactoryReads {
    function getPair(address tokenA, address tokenB) external view returns (address);
}

interface IV2PairReads {
    function token0() external view returns (address);
    function getReserves() external view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);
}

/**
 * @title V2RouterCheck
 * @author Conrad Japhet
 * @notice The app check for a Uniswap-V2-style router: swaps and liquidity, as Mitfah makes them on BSC
 *         through PancakeSwap's.
 * @dev Understood, and nothing else: the six swap functions and the three that support fee-on-transfer
 *      tokens (each a trade from the path's first token to its last; `to` is a payee), `addLiquidity`,
 *      `addLiquidityETH`, `removeLiquidity`, `removeLiquidityETH` and
 *      `removeLiquidityETHSupportingFeeOnTransferTokens` (`to` is a payee, and the pair's price is
 *      reported). The router spends a payable call's whole value or pays the rest back to the caller,
 *      so value needs no rule of its own. The `...WithPermit` functions are refused: the wallet signs
 *      no permits. So is a deposit into a pair that doesn't exist yet, whose price the deposit itself
 *      would set.
 */
contract V2RouterCheck is AppCheckBase {
    /// @notice The router's wrapped native token.
    address public immutable WETH;
    /// @notice The factory its pairs come from.
    address public immutable FACTORY;

    // amountIn, amountOutMin, path, to, deadline
    bytes4 private constant SWAP_EXACT_TOKENS_FOR_TOKENS = 0x38ed1739;
    bytes4 private constant SWAP_EXACT_TOKENS_FOR_ETH = 0x18cbafe5;
    bytes4 private constant SWAP_EXACT_TOKENS_FOR_TOKENS_FOT = 0x5c11d795;
    bytes4 private constant SWAP_EXACT_TOKENS_FOR_ETH_FOT = 0x791ac947;
    // amountOut, amountInMax, path, to, deadline
    bytes4 private constant SWAP_TOKENS_FOR_EXACT_TOKENS = 0x8803dbee;
    bytes4 private constant SWAP_TOKENS_FOR_EXACT_ETH = 0x4a25d94a;
    // amountOutMin, path, to, deadline (payable: amountIn is the value)
    bytes4 private constant SWAP_EXACT_ETH_FOR_TOKENS = 0x7ff36ab5;
    bytes4 private constant SWAP_EXACT_ETH_FOR_TOKENS_FOT = 0xb6f9de95;
    // amountOut, path, to, deadline (payable: at most the value goes in)
    bytes4 private constant SWAP_ETH_FOR_EXACT_TOKENS = 0xfb3bdb41;
    bytes4 private constant ADD_LIQUIDITY = 0xe8e33700;
    bytes4 private constant ADD_LIQUIDITY_ETH = 0xf305d719;
    bytes4 private constant REMOVE_LIQUIDITY = 0xbaa2abde;
    bytes4 private constant REMOVE_LIQUIDITY_ETH = 0x02751cec;
    bytes4 private constant REMOVE_LIQUIDITY_ETH_FOT = 0xaf2979eb;

    /// @param router This chain's V2 router.
    constructor(address router) AppCheckBase(router) {
        WETH = IV2RouterReads(router).WETH();
        FACTORY = IV2RouterReads(router).factory();
    }

    /// @inheritdoc IAppCheck
    function inspect(address, uint256 value, bytes calldata data)
        external
        view
        returns (address[] memory, Trade[] memory, PoolPrice[] memory)
    {
        Report memory report = _newReport(1);
        bytes4 selector = data.length >= 4 ? bytes4(data[:4]) : bytes4(0);
        bytes calldata args = data.length >= 4 ? data[4:] : data[0:0];

        if (
            selector == SWAP_EXACT_TOKENS_FOR_TOKENS || selector == SWAP_EXACT_TOKENS_FOR_ETH
                || selector == SWAP_EXACT_TOKENS_FOR_TOKENS_FOT || selector == SWAP_EXACT_TOKENS_FOR_ETH_FOT
        ) {
            (uint256 amountIn, uint256 amountOutMin, address[] memory path, address to,) =
                abi.decode(args, (uint256, uint256, address[], address, uint256));
            _swap(report, path, amountIn, amountOutMin, to);
        } else if (selector == SWAP_TOKENS_FOR_EXACT_TOKENS || selector == SWAP_TOKENS_FOR_EXACT_ETH) {
            (uint256 amountOut, uint256 amountInMax, address[] memory path, address to,) =
                abi.decode(args, (uint256, uint256, address[], address, uint256));
            _swap(report, path, amountInMax, amountOut, to);
        } else if (selector == SWAP_EXACT_ETH_FOR_TOKENS || selector == SWAP_EXACT_ETH_FOR_TOKENS_FOT) {
            (uint256 amountOutMin, address[] memory path, address to,) =
                abi.decode(args, (uint256, address[], address, uint256));
            _swap(report, path, value, amountOutMin, to);
        } else if (selector == SWAP_ETH_FOR_EXACT_TOKENS) {
            (uint256 amountOut, address[] memory path, address to,) =
                abi.decode(args, (uint256, address[], address, uint256));
            _swap(report, path, value, amountOut, to);
        } else if (selector == ADD_LIQUIDITY) {
            (address tokenA, address tokenB,,,,, address to,) =
                abi.decode(args, (address, address, uint256, uint256, uint256, uint256, address, uint256));
            _liquidity(report, tokenA, tokenB, to);
        } else if (selector == ADD_LIQUIDITY_ETH) {
            (address token,,,, address to,) =
                abi.decode(args, (address, uint256, uint256, uint256, address, uint256));
            _liquidity(report, token, WETH, to);
        } else if (selector == REMOVE_LIQUIDITY) {
            (address tokenA, address tokenB,,,, address to,) =
                abi.decode(args, (address, address, uint256, uint256, uint256, address, uint256));
            _liquidity(report, tokenA, tokenB, to);
        } else if (selector == REMOVE_LIQUIDITY_ETH || selector == REMOVE_LIQUIDITY_ETH_FOT) {
            (address token,,,, address to,) =
                abi.decode(args, (address, uint256, uint256, uint256, address, uint256));
            _liquidity(report, token, WETH, to);
        } else {
            revert AppCheck_UnsupportedCall(app, selector);
        }
        return _finish(report);
    }

    /// @dev A swap along `path`: a trade from its first token to its last, paid to `to`.
    function _swap(Report memory report, address[] memory path, uint256 maxIn, uint256 minOut, address to)
        internal
        view
    {
        if (path.length < 2) revert AppCheck_BadPath(app);
        _payee(report, to);
        _trade(report, path[0], maxIn, path[path.length - 1], minOut);
    }

    /// @dev A deposit into or withdrawal from the (tokenA, tokenB) pair: its LP tokens or its two tokens
    ///      go to `to`, at the pair's current price. Reverts for a pair that doesn't exist.
    function _liquidity(Report memory report, address tokenA, address tokenB, address to) internal view {
        _payee(report, to);
        address pair = IV2FactoryReads(FACTORY).getPair(tokenA, tokenB);
        if (pair == address(0)) revert AppCheck_NoPool(app);
        (uint112 reserve0, uint112 reserve1,) = IV2PairReads(pair).getReserves();
        address token0 = IV2PairReads(pair).token0();
        _price(
            report,
            PoolPrice({
                token0: token0, amount0: reserve0, token1: token0 == tokenA ? tokenB : tokenA, amount1: reserve1
            })
        );
    }
}
