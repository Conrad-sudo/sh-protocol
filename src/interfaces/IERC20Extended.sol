// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

import {IERC20} from "@openzeppelin/contracts/interfaces/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

/**
 * @title IERC20Extended
 * @notice ERC-20 plus the non-standard allowance functions some tokens expose, so
 *         SpendingLimitModule can recognize them.
 * @dev None of these is part of ERC-20. They exist here for exactly one purpose: giving
 *      SpendingLimitModule a `.selector` to compare calldata against, alongside
 *      `IERC20.approve.selector`. A token that grants an allowance this way would otherwise slip past
 *      the hook's no-standing-approval rule unseen. Nothing in this repo ever CALLS them.
 *
 *      Declared without return values, whatever the real tokens return. That mismatch is harmless
 *      because a selector is derived from the signature alone -- the return type contributes nothing
 *      -- and because the functions are never invoked through this interface. Do not start calling
 *      one without fixing its signature first.
 */
interface IERC20Extended is IERC20, IERC20Metadata {
    /// @dev OpenZeppelin before v5. The module records it like `approve`.
    function increaseAllowance(address, uint256) external;

    /// @dev Older OpenZeppelin: mainnet LINK and WBTC, Base's LINK. The module records it like `approve`.
    function increaseApproval(address, uint256) external;

    /// @dev Mainnet SAND. The module refuses it; (owner, spender, amount).
    function approveFor(address, address, uint256) external;

    /// @dev Mainnet SAND. The module refuses it; (owner, spender, amount needed).
    function addAllowanceIfNeeded(address, address, uint256) external;

    /// @dev Mainnet SAND. The module refuses it; (spender, amount, call to forward).
    function approveAndCall(address, uint256, bytes calldata) external;

    /// @dev Mainnet SAND. The module refuses it; (spender, amount, call to forward).
    function paidCall(address, uint256, bytes calldata) external;
}
