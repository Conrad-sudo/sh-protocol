// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {AppCheckBase} from "./AppCheckBase.sol";
import {IAppCheck} from "../interfaces/IAppCheck.sol";

/**
 * @title WrappedNativeCheck
 * @author Conrad Japhet
 * @notice The app check for a chain's wrapped native token (WETH9, WBNB): wrapping and unwrapping.
 * @dev `deposit` credits the caller with exactly the value sent, and `withdraw` pays the caller, so
 *      neither names anyone else. The token's ERC-20 functions never reach this check: the PayeeGuard
 *      reads `transfer` and `approve` itself, as on any token. Anything else is refused.
 */
contract WrappedNativeCheck is AppCheckBase {
    bytes4 private constant DEPOSIT = 0xd0e30db0; // deposit()
    bytes4 private constant WITHDRAW = 0x2e1a7d4d; // withdraw(uint256)

    /// @param wrappedNative This chain's wrapped native token.
    constructor(address wrappedNative) AppCheckBase(wrappedNative) {}

    /// @inheritdoc IAppCheck
    function inspect(address, uint256 value, bytes calldata data)
        external
        view
        returns (address[] memory, Trade[] memory, PoolPrice[] memory)
    {
        bytes4 selector = data.length >= 4 ? bytes4(data[:4]) : bytes4(0);
        if (selector != DEPOSIT && (selector != WITHDRAW || value != 0)) {
            revert AppCheck_UnsupportedCall(app, selector);
        }
        return _finish(_newReport(0));
    }
}
