// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

/**
 * @title IAppCheck
 * @author Conrad Japhet
 * @notice Reads what one call to one app (a router, a position manager) would do with a wallet's
 *         money, so the PayeeGuard can hold it to the wallet's contacts and its trade floors.
 * @dev A check REPORTS and the PayeeGuard DECIDES. A check names every address that may receive value
 *      from the wallet because of the call, every trade in it and the price of every pool it deposits
 *      into or withdraws from; the guard then requires each payee to be the wallet or a signed contact
 *      and each trade and pool price to sit within its floors. So a check can't loosen the contact
 *      rule or the floors. What it can get wrong is the report: a payee or a trade it leaves out goes
 *      unchecked. That is what reviewing a new check comes down to.
 * @dev A check must REVERT for any call it doesn't fully understand: an unknown function, a shape it
 *      can't read, funds the call would leave sitting in the app for anyone to take. Reverting refuses
 *      the wallet's whole execution.
 * @dev Checks are registered per app in SHRegistry, behind a two-day delay ({SHRegistry-proposeAppCheck}).
 */
interface IAppCheck {
    /// @notice One trade the call makes: the wallet gives at most `maxIn` of `tokenIn` and gets at
    ///         least `minOut` of `tokenOut`. Wrapped native counts as itself (WETH, WBNB).
    struct Trade {
        address tokenIn;
        uint256 maxIn;
        address tokenOut;
        uint256 minOut;
    }

    /// @notice The current price of a pool the call deposits into or withdraws from, as two amounts
    ///         worth the same in that pool: `amount0` of `token0` trades for `amount1` of `token1`.
    struct PoolPrice {
        address token0;
        uint256 amount0;
        address token1;
        uint256 amount1;
    }

    /// @notice The one contract this check reads calls to. SHRegistry refuses to register a check
    ///         under any other address.
    function app() external view returns (address);

    /**
     * @notice What a call from `account` to {app}, carrying `value` wei, would do.
     * @dev Reverts for any call it doesn't fully understand (see the interface NatSpec).
     * @param account The wallet making the call.
     * @param value   The native value the call carries.
     * @param data    The call's calldata, selector included.
     * @return payees Every address that may receive value from the wallet because of the call. The
     *                wallet itself may be listed; the guard lets it through.
     * @return trades Every trade the call makes.
     * @return prices The price of every pool the call deposits into or withdraws from.
     */
    function inspect(address account, uint256 value, bytes calldata data)
        external
        view
        returns (address[] memory payees, Trade[] memory trades, PoolPrice[] memory prices);
}
