//SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {SessionHandler} from "../../src/SessionHandler.sol";

/**
 * @title SessionHandlerHarness
 * @notice Test harness that exposes SessionHandler internal functions for unit testing.
 * @dev Inherits SessionHandler and re-exports internal functions as external so they can
 *      be called directly in tests without modifying the production contract.
 *      Deploy this contract in tests that need access to internals; use SessionHandler
 *      for all other tests.
 */
contract SessionHandlerHarness is SessionHandler {
    constructor(address entryPoint, address priceOracle, address uniswapRounter)
        SessionHandler(entryPoint, priceOracle, uniswapRounter)
    {}

    /**
     * @notice Exposes _packValidationData for round-trip testing.
     * @dev Delegates directly to the internal function with no additional logic,
     *      so any discrepancy between this and the production path is a test bug, not a contract bug.
     * @param sigFailed True if signature validation failed (sets lower 160 bits to 1).
     * @param validFrom Session activation timestamp packed into bits 208-255.
     * @param validUntil Session expiry timestamp packed into bits 160-207.
     * @return Packed ERC-4337 validationData uint256.
     */
    function packValidationData(bool sigFailed, uint48 validFrom, uint48 validUntil) external pure returns (uint256) {
        return _packValidationData(sigFailed, validFrom, validUntil);
    }
}
