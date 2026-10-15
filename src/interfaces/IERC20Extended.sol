// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

import {IERC20} from "@openzeppelin/contracts/interfaces/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

/**
 * @title IERC20Extended
 * @notice ERC-20 plus the non-standard allowance helper some older tokens still expose, so
 *         SpendingLimitModule can recognize it.
 * @dev `increaseAllowance` was never part of ERC-20 and OpenZeppelin removed it in v5, so it exists
 *      here for exactly one purpose: giving SpendingLimitModule an `IERC20Extended.increaseAllowance
 *      .selector` to compare calldata against, alongside `IERC20.approve.selector`. A token that
 *      grants an allowance this way would otherwise slip past the hook's no-standing-approval rule
 *      unseen. Nothing in this repo ever CALLS it.
 *
 *      Declared without a return value while OpenZeppelin v4's returned `bool`. That mismatch is
 *      harmless because a selector is derived from the signature `increaseAllowance(address,uint256)`
 *      alone -- the return type contributes nothing -- and because the function is never invoked
 *      through this interface. Do not start calling it without fixing the signature first.
 */
interface IERC20Extended is IERC20, IERC20Metadata {
    function increaseAllowance(address, uint256) external;
}
