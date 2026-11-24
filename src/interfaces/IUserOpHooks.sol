// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {PackedUserOperation} from "@openzeppelin/contracts/interfaces/draft-IERC4337.sol";

/**
 * @title IUserOpHooks
 * @author Conrad Japhet
 * @notice What a SessionHandler wallet calls on a validator module around each UserOp that module
 *         approved. AgentModule is the one such module.
 * @dev An op signed by neither the wallet's owner nor one of its passkeys runs only through
 *      {SessionHandler-executeUserOp}, and only when a validator module installed on the wallet
 *      approved it. The wallet calls {preExecute} on that module, then runs the op's calls through its
 *      own `execute` (guard, fee, spending-limit hook), then calls {postExecute}. Both get the
 *      arguments the EntryPoint passed to executeUserOp, with msg.sender == the wallet. A revert in
 *      either fails the op.
 */
interface IUserOpHooks {
    /// @notice Runs before the op's calls.
    /// @param userOp     The op being executed.
    /// @param userOpHash Its hash, as the EntryPoint computed it.
    function preExecute(PackedUserOperation calldata userOp, bytes32 userOpHash) external;

    /// @notice Runs after the op's calls.
    /// @param userOp     The op being executed.
    /// @param userOpHash Its hash, as the EntryPoint computed it.
    function postExecute(PackedUserOperation calldata userOp, bytes32 userOpHash) external;
}
