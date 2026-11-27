//SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {Mode} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";

/**
 * @title SpendingLimitModuleHarness
 * @notice Test harness that exposes SpendingLimitModule internal functions for unit testing.
 * @dev Inherits SpendingLimitModule and re-exports internal functions as external so they can
 *      be called directly in tests without modifying the production contract. Each wrapper
 *      delegates directly to the internal function with no additional logic, so any discrepancy
 *      between this and the production path is a test bug, not a contract bug.
 */
contract SpendingLimitModuleHarness is SpendingLimitModule {
    /// @param registry The SHRegistry the module reads its price oracle from (NOT the oracle itself).
    constructor(address registry, address payeeGuard) SpendingLimitModule(registry, payeeGuard) {}

    /// @notice Exposes _decodeExecuteCalldata: recovers (isExecute, mode, executionCalldata) from
    ///         raw outer account calldata.
    function decodeExecuteCalldata(bytes calldata fullCalldata)
        external
        pure
        returns (bool isExecute, bytes32 mode, bytes memory executionCalldata)
    {
        (bool ok, Mode m, bytes calldata payload) = _decodeExecuteCalldata(fullCalldata);
        return (ok, Mode.unwrap(m), payload);
    }

    /// @notice Exposes _isApproveCandidate: the cheap pure pre-classifier for batch counting.
    function isApproveCandidate(bytes calldata callData) external pure returns (bool) {
        return _isApproveCandidate(callData);
    }

    /// @notice Exposes _validateApproval: full classification of one sub-call (reverts on unlimited
    ///         approvals, and on unpriced tokens whose spender isn't trusted, like the production path).
    function validateApproval(address account, address token, bytes calldata callData)
        external
        view
        returns (bool isApproval, address spender)
    {
        return _validateApproval(account, token, callData);
    }

    /// @notice Exposes _collectApprovals: scans decoded execution calldata for approve sub-calls.
    function collectApprovals(address account, bool isExecute, bytes32 mode, bytes calldata executionCalldata)
        external
        view
        returns (address[] memory tokens, address[] memory spenders)
    {
        return _collectApprovals(account, isExecute, Mode.wrap(mode), executionCalldata);
    }
}
