// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";

/**
 * @title SessionHandlerHandler
 * @notice Fuzzer-facing handler that wraps every mutating action on SessionHandler.
 *         The invariant test targets this contract so Foundry calls its functions
 *         in random order with random inputs.
 *
 *         Ghost variables (sessionKeys, wasAdded) let invariant checks iterate
 *         over all sessions ever created.
 */
contract SessionHandlerHandler is Test {
    SessionHandler public sessionHandler;
    ERC20Mock public usdc;
    address public owner;

    address[] public sessionKeys;
    mapping(address => bool) public wasAdded;

    constructor(SessionHandler _sessionHandler, ERC20Mock _usdc, address _owner) {
        sessionHandler = _sessionHandler;
        usdc = _usdc;
        owner = _owner;
    }

    // ──────────────────────────────────────────────
    //  Handler actions
    // ──────────────────────────────────────────────

    /// @dev Fuzz-safe addSessionKey: bounds inputs to always-valid ranges so the
    ///      call succeeds whenever the contract logic allows it.
    function addSession(
        address sessionKey,
        uint48 validFromOffset, // seconds from now before session starts
        uint48 duration, // how long the session lasts
        uint256 spendingLimit
    )
        public
    {
        // Reject zero address (mirrors contract behaviour)
        sessionKey = address(uint160(bound(uint256(uint160(sessionKey)), 1, type(uint160).max)));
        duration = uint48(bound(duration, 1, 365 days));
        validFromOffset = uint48(bound(validFromOffset, 0, 1 days));
        spendingLimit = bound(spendingLimit, 1, type(uint128).max);

        uint48 validFrom = uint48(block.timestamp + validFromOffset);
        uint48 validUntil = uint48(block.timestamp + validFromOffset + duration);

        bytes4[] memory sels = new bytes4[](1);
        sels[0] = ERC20Mock.transfer.selector;

        vm.prank(owner);
        try sessionHandler.addSessionKey(sessionKey, address(usdc), sels, validFrom, validUntil, spendingLimit) {
            if (!wasAdded[sessionKey]) {
                sessionKeys.push(sessionKey);
                wasAdded[sessionKey] = true;
            }
        } catch { /* invalid inputs revert — that's fine */ }
    }

    /// @dev Revoke a randomly selected session from those ever created.
    function revokeSession(uint256 keyIndex) public {
        if (sessionKeys.length == 0) return;
        keyIndex = bound(keyIndex, 0, sessionKeys.length - 1);
        address key = sessionKeys[keyIndex];

        SessionHandler.Session memory s = sessionHandler.getSession(key);
        if (s.spendingLimit == 0) return; // session deleted/never-existed — revokeSessionKey would revert

        vm.prank(owner);
        try sessionHandler.revokeSessionKey(key) {} catch {}
    }

    /// @dev Try to revoke using a non-owner to verify access control holds.
    function revokeSessionAsNonOwner(uint256 keyIndex, address caller) public {
        if (sessionKeys.length == 0) return;
        caller = address(uint160(bound(uint256(uint160(caller)), 1, type(uint160).max)));
        keyIndex = bound(keyIndex, 0, sessionKeys.length - 1);
        if (caller == owner) return;

        address key = sessionKeys[keyIndex];
        vm.prank(caller);
        try sessionHandler.revokeSessionKey(key) {
        // If this succeeds the invariant contract will catch the broken state
        }
            catch {}
    }

    // ──────────────────────────────────────────────
    //  Helpers for invariant iteration
    // ──────────────────────────────────────────────

    function sessionKeyCount() external view returns (uint256) {
        return sessionKeys.length;
    }
}
