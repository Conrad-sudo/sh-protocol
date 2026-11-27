// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {IAppCheck} from "../interfaces/IAppCheck.sol";

/**
 * @title AppCheckBase
 * @author Conrad Japhet
 * @notice What every app check shares: the app it reads, the report it builds, and "parking" — funds a
 *         call sends to the app itself, which must leave again within the same call.
 * @dev Parking is how routers pay out native coin: a swap pays the router, then `unwrapWETH9` in the
 *      same multicall sends the coin on. Anything still parked when the call ends stays in the app,
 *      where anyone may sweep it, so {_finish} refuses the call.
 */
abstract contract AppCheckBase is IAppCheck {
    /// @notice The check doesn't understand this call to `app`, so it can't be allowed.
    error AppCheck_UnsupportedCall(address app, bytes4 selector);
    /// @notice The call would leave `token` sitting in `app` for anyone to take.
    error AppCheck_FundsLeftInApp(address app, address token);
    /// @notice The call would leave native coin sitting in `app` for anyone to take.
    error AppCheck_NativeLeftInApp(address app);
    /// @notice A swap path that can't be read.
    error AppCheck_BadPath(address app);
    /// @notice The call deposits into or withdraws from a pool that doesn't exist or has no price.
    error AppCheck_NoPool(address app);

    /// @inheritdoc IAppCheck
    address public immutable app;

    /// @dev What {inspect} gathers, in arrays sized up front and cut to length by {_finish}.
    struct Report {
        address[] payees;
        uint256 payeeCount;
        Trade[] trades;
        uint256 tradeCount;
        PoolPrice[] prices;
        uint256 priceCount;
        address[] parked;
        uint256 parkedCount;
    }

    /// @param app_ The contract this check reads calls to.
    constructor(address app_) {
        app = app_;
    }

    /// @dev An empty report with room for `calls` calls: each names at most one payee, one trade and
    ///      one pool, and parks at most two tokens.
    function _newReport(uint256 calls) internal pure returns (Report memory report) {
        report.payees = new address[](calls);
        report.trades = new Trade[](calls);
        report.prices = new PoolPrice[](calls);
        report.parked = new address[](calls * 2);
    }

    function _payee(Report memory report, address payee) internal pure {
        report.payees[report.payeeCount++] = payee;
    }

    function _trade(Report memory report, address tokenIn, uint256 maxIn, address tokenOut, uint256 minOut)
        internal
        pure
    {
        report.trades[report.tradeCount++] = Trade({tokenIn: tokenIn, maxIn: maxIn, tokenOut: tokenOut, minOut: minOut});
    }

    function _price(Report memory report, PoolPrice memory price) internal pure {
        report.prices[report.priceCount++] = price;
    }

    /// @dev Records that `token` is now held by the app on the wallet's behalf.
    function _park(Report memory report, address token) internal pure {
        for (uint256 i = 0; i < report.parkedCount; i++) {
            if (report.parked[i] == token) return;
        }
        report.parked[report.parkedCount++] = token;
    }

    /// @dev Records that the app pays out ALL it holds of `token` (unwrapWETH9 and sweepToken take the
    ///      whole balance), so nothing of it stays parked.
    function _drain(Report memory report, address token) internal pure {
        for (uint256 i = 0; i < report.parkedCount; i++) {
            if (report.parked[i] == token) {
                report.parked[i] = report.parked[--report.parkedCount];
                return;
            }
        }
    }

    /// @dev The report as {inspect} returns it. Reverts if anything is still parked.
    function _finish(Report memory report)
        internal
        view
        returns (address[] memory payees, Trade[] memory trades, PoolPrice[] memory prices)
    {
        if (report.parkedCount != 0) revert AppCheck_FundsLeftInApp(app, report.parked[0]);
        payees = report.payees;
        trades = report.trades;
        prices = report.prices;
        uint256 payeeCount = report.payeeCount;
        uint256 tradeCount = report.tradeCount;
        uint256 priceCount = report.priceCount;
        // Shortening a memory array in place: each count is at most the length it was allocated with.
        assembly ("memory-safe") {
            mstore(payees, payeeCount)
            mstore(trades, tradeCount)
            mstore(prices, priceCount)
        }
    }

    /// @dev The ABI-encoded arguments of `inner`, without its selector, for abi.decode. Reuses `call`'s
    ///      memory: its length word moves four bytes up, over the selector, so `inner` must not be read
    ///      again afterwards.
    function _args(bytes memory inner) internal view returns (bytes memory args) {
        if (inner.length < 4) revert AppCheck_UnsupportedCall(app, bytes4(0));
        assembly ("memory-safe") {
            args := add(inner, 4)
            mstore(args, sub(mload(inner), 4))
        }
    }

    /// @dev The first and last token of a Uniswap V3 path (token | fee | token | ...).
    function _pathEnds(bytes memory path) internal view returns (address first, address last) {
        uint256 length = path.length;
        if (length < 43 || (length - 20) % 23 != 0) revert AppCheck_BadPath(app);
        assembly ("memory-safe") {
            first := shr(96, mload(add(path, 32)))
            last := shr(96, mload(add(add(path, 32), sub(length, 20))))
        }
    }
}
