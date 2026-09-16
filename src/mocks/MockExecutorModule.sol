// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC7579Module, MODULE_TYPE_EXECUTOR} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {IERC7579Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";

/**
 * @title MockExecutorModule
 * @notice A minimal ERC-7579 executor module, used to drive {SessionHandler-executeFromExecutor}.
 * @dev That path had no test of any kind until 2026-09-14 — which is how it came to be the one
 *      execution entry point that skipped the account's admin-surface guard. The module is
 *      deliberately dumb: it forwards whatever mode and calldata the test hands it, so the test
 *      controls the attack, not the mock.
 *
 *      `onlyModule(MODULE_TYPE_EXECUTOR, ...)` on the account checks the CALLER is an installed
 *      executor, so the call must originate here rather than from the test contract.
 */
contract MockExecutorModule is IERC7579Module {
    /// @notice Set by the account when this module is installed, cleared on uninstall.
    mapping(address account => bool) public installed;

    function onInstall(bytes calldata) external override {
        installed[msg.sender] = true;
    }

    function onUninstall(bytes calldata) external override {
        installed[msg.sender] = false;
    }

    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_EXECUTOR;
    }

    /**
     * @notice Forwards an execution to `account` as an installed executor.
     * @dev Open to any caller on purpose: the access control under test belongs to the ACCOUNT
     *      (`onlyModule` plus, as of the fix, `_guardSessionExecution`), not to this mock. Locking
     *      the mock down would only obscure which contract actually refused the call.
     */
    function callExecute(address account, bytes32 mode, bytes calldata executionCalldata)
        external
        returns (bytes[] memory)
    {
        return IERC7579Execution(account).executeFromExecutor(mode, executionCalldata);
    }
}
