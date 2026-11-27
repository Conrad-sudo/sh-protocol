// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

import {IAppCheck} from "../../src/interfaces/IAppCheck.sol";

/// @notice An app check that allows every call to its app and reports nothing: for tests whose subject
///         is something other than the PayeeGuard (an allowlist, a fee), so calls to a mock contract
///         get past the guard.
contract OpenAppCheck is IAppCheck {
    address public immutable app;

    constructor(address app_) {
        app = app_;
    }

    function inspect(address, uint256, bytes calldata)
        external
        pure
        returns (address[] memory, Trade[] memory, PoolPrice[] memory)
    {
        return (new address[](0), new Trade[](0), new PoolPrice[](0));
    }
}

/// @notice An app check whose report the test sets: to drive the PayeeGuard's decisions on payees,
///         trades and pool prices without a real router.
contract ScriptedAppCheck is IAppCheck {
    address public immutable app;
    address[] private _payees;
    Trade[] private _trades;
    PoolPrice[] private _prices;

    constructor(address app_) {
        app = app_;
    }

    function setPayee(address payee) external {
        delete _payees;
        _payees.push(payee);
    }

    function setTrade(Trade calldata trade) external {
        delete _trades;
        _trades.push(trade);
    }

    function setPrice(PoolPrice calldata price) external {
        delete _prices;
        _prices.push(price);
    }

    function clear() external {
        delete _payees;
        delete _trades;
        delete _prices;
    }

    function inspect(address, uint256, bytes calldata)
        external
        view
        returns (address[] memory, Trade[] memory, PoolPrice[] memory)
    {
        return (_payees, _trades, _prices);
    }
}
