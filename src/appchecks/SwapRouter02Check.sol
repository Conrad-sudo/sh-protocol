// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {AppCheckBase} from "./AppCheckBase.sol";
import {IAppCheck} from "../interfaces/IAppCheck.sol";

/// @dev The one read the check makes of the router, at construction.
interface ISwapRouter02Immutables {
    function WETH9() external view returns (address);
}

/**
 * @title SwapRouter02Check
 * @author Conrad Japhet
 * @notice The app check for Uniswap's SwapRouter02: the V3 swaps Mitfah makes, alone or in a multicall,
 *         with native coin paid in as the call's value and paid out by `unwrapWETH9`.
 * @dev Understood, and nothing else:
 *        - `exactInputSingle`, `exactInput`, `exactOutputSingle`, `exactOutput` (SwapRouter02's
 *          deadline-free params). Each is a trade; its recipient is a payee, except the router itself
 *          (by address or as ADDRESS_THIS), which PARKS the output for a later call to pay out.
 *          MSG_SENDER means the wallet. An exact-input amount of 0 (the router's CONTRACT_BALANCE,
 *          "whatever I hold") is refused: what it trades can't be read from the call.
 *        - `unwrapWETH9` and `sweepToken`, both forms: they pay out the router's WHOLE balance of the
 *          token, so they drain what was parked; the recipient form names a payee.
 *        - `refundETH`, and `multicall` in its three forms around any of the above (one level deep:
 *          the router runs each inner call as a delegatecall to itself, so an inner call to anything
 *          else would be just as reachable).
 *      Native value: the router keeps whatever the call doesn't spend, where anyone's `refundETH` can
 *      take it. So a call carrying value must either end with `refundETH` or spend exactly that value
 *      in exact-input swaps from WETH9 (which the router wraps from the call's value).
 */
contract SwapRouter02Check is AppCheckBase {
    /// @notice The router's wrapped native token, read from it at construction.
    address public immutable WETH9;

    // SwapRouter02's Constants: recipient placeholders the router replaces.
    address private constant MSG_SENDER = address(1);
    address private constant ADDRESS_THIS = address(2);

    bytes4 private constant EXACT_INPUT_SINGLE = 0x04e45aaf;
    bytes4 private constant EXACT_INPUT = 0xb858183f;
    bytes4 private constant EXACT_OUTPUT_SINGLE = 0x5023b4df;
    bytes4 private constant EXACT_OUTPUT = 0x09b81346;
    bytes4 private constant MULTICALL = 0xac9650d8; // multicall(bytes[])
    bytes4 private constant MULTICALL_DEADLINE = 0x5ae401dc; // multicall(uint256,bytes[])
    bytes4 private constant MULTICALL_BLOCKHASH = 0x1f0464d1; // multicall(bytes32,bytes[])
    bytes4 private constant UNWRAP_WETH9 = 0x49404b7c; // unwrapWETH9(uint256,address)
    bytes4 private constant UNWRAP_WETH9_TO_SENDER = 0x49616997; // unwrapWETH9(uint256)
    bytes4 private constant SWEEP_TOKEN = 0xdf2ab5bb; // sweepToken(address,uint256,address)
    bytes4 private constant SWEEP_TOKEN_TO_SENDER = 0xe90a182f; // sweepToken(address,uint256)
    bytes4 private constant REFUND_ETH = 0x12210e8a; // refundETH()

    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    struct ExactOutputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountOut;
        uint256 amountInMaximum;
        uint160 sqrtPriceLimitX96;
    }

    struct ExactOutputParams {
        bytes path;
        address recipient;
        uint256 amountOut;
        uint256 amountInMaximum;
    }

    /// @param router This chain's SwapRouter02.
    constructor(address router) AppCheckBase(router) {
        WETH9 = ISwapRouter02Immutables(router).WETH9();
    }

    /// @inheritdoc IAppCheck
    function inspect(address account, uint256 value, bytes calldata data)
        external
        view
        returns (address[] memory, Trade[] memory, PoolPrice[] memory)
    {
        bytes[] memory calls = _calls(data);
        Report memory report = _newReport(calls.length);
        uint256 nativeSpent;
        bool nativeOpen; // an exact-output swap from WETH9 may leave part of the value unspent
        bytes4 last;
        for (uint256 i = 0; i < calls.length; i++) {
            (bytes4 selector, uint256 spent, bool open) = _readCall(report, account, value, calls[i]);
            nativeSpent += spent;
            nativeOpen = nativeOpen || open;
            last = selector;
        }
        if (value != 0 && last != REFUND_ETH && (nativeOpen || nativeSpent != value)) {
            revert AppCheck_NativeLeftInApp(app);
        }
        return _finish(report);
    }

    /// @dev The calls a top-level call makes: a multicall's inner calls, or the call itself.
    function _calls(bytes calldata data) internal pure returns (bytes[] memory calls) {
        bytes4 selector = data.length >= 4 ? bytes4(data[:4]) : bytes4(0);
        if (selector == MULTICALL) return abi.decode(data[4:], (bytes[]));
        if (selector == MULTICALL_DEADLINE) {
            (, calls) = abi.decode(data[4:], (uint256, bytes[]));
            return calls;
        }
        if (selector == MULTICALL_BLOCKHASH) {
            (, calls) = abi.decode(data[4:], (bytes32, bytes[]));
            return calls;
        }
        calls = new bytes[](1);
        calls[0] = data;
    }

    /**
     * @dev Adds one (inner) call to the report.
     * @return selector    The call's selector.
     * @return nativeSpent Native value an exact-input swap from WETH9 spends, when the call carries value.
     * @return nativeOpen  Whether an exact-output swap from WETH9 may leave value unspent.
     */
    function _readCall(Report memory report, address account, uint256 value, bytes memory inner)
        internal
        view
        returns (bytes4 selector, uint256 nativeSpent, bool nativeOpen)
    {
        // The first four bytes: shorter calldata pads with zeros, which no selector here matches.
        // forge-lint: disable-next-line(unsafe-typecast)
        selector = bytes4(inner);
        if (selector == EXACT_INPUT_SINGLE) {
            ExactInputSingleParams memory p = abi.decode(_args(inner), (ExactInputSingleParams));
            if (p.amountIn == 0) revert AppCheck_UnsupportedCall(app, selector);
            _swapRecipient(report, account, p.recipient, p.tokenOut);
            _trade(report, p.tokenIn, p.amountIn, p.tokenOut, p.amountOutMinimum);
            if (value != 0 && p.tokenIn == WETH9) nativeSpent = p.amountIn;
        } else if (selector == EXACT_INPUT) {
            ExactInputParams memory p = abi.decode(_args(inner), (ExactInputParams));
            if (p.amountIn == 0) revert AppCheck_UnsupportedCall(app, selector);
            (address tokenIn, address tokenOut) = _pathEnds(p.path);
            _swapRecipient(report, account, p.recipient, tokenOut);
            _trade(report, tokenIn, p.amountIn, tokenOut, p.amountOutMinimum);
            if (value != 0 && tokenIn == WETH9) nativeSpent = p.amountIn;
        } else if (selector == EXACT_OUTPUT_SINGLE) {
            ExactOutputSingleParams memory p = abi.decode(_args(inner), (ExactOutputSingleParams));
            _swapRecipient(report, account, p.recipient, p.tokenOut);
            _trade(report, p.tokenIn, p.amountInMaximum, p.tokenOut, p.amountOut);
            nativeOpen = value != 0 && p.tokenIn == WETH9;
        } else if (selector == EXACT_OUTPUT) {
            ExactOutputParams memory p = abi.decode(_args(inner), (ExactOutputParams));
            // An exact-output path runs backwards: tokenOut first.
            (address tokenOut, address tokenIn) = _pathEnds(p.path);
            _swapRecipient(report, account, p.recipient, tokenOut);
            _trade(report, tokenIn, p.amountInMaximum, tokenOut, p.amountOut);
            nativeOpen = value != 0 && tokenIn == WETH9;
        } else if (selector == UNWRAP_WETH9) {
            (, address recipient) = abi.decode(_args(inner), (uint256, address));
            _drain(report, WETH9);
            _payee(report, recipient);
        } else if (selector == UNWRAP_WETH9_TO_SENDER) {
            _drain(report, WETH9);
        } else if (selector == SWEEP_TOKEN) {
            (address token,, address recipient) = abi.decode(_args(inner), (address, uint256, address));
            _drain(report, token);
            _payee(report, recipient);
        } else if (selector == SWEEP_TOKEN_TO_SENDER) {
            (address token,) = abi.decode(_args(inner), (address, uint256));
            _drain(report, token);
        } else if (selector != REFUND_ETH) {
            revert AppCheck_UnsupportedCall(app, selector);
        }
    }

    /// @dev A swap's recipient: the wallet (MSG_SENDER), the router itself (parks `tokenOut`), or a payee.
    function _swapRecipient(Report memory report, address account, address recipient, address tokenOut)
        internal
        view
    {
        if (recipient == MSG_SENDER) {
            _payee(report, account);
        } else if (recipient == ADDRESS_THIS || recipient == app) {
            _park(report, tokenOut);
        } else {
            _payee(report, recipient);
        }
    }
}
