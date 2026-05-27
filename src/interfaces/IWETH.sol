// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20Extended} from "./IERC20Extended.sol";

interface IWETH is IERC20Extended {
    event Deposit(address indexed dst, uint256 amount);

    event Withdrawal(address indexed src, uint256 amount);

    function deposit() external payable;

    function withdraw(uint256 amount) external;
}
