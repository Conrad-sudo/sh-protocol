// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, StdInvariant} from "forge-std/Test.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {SHHandler} from "./SHHandler.sol";

/**
 * @title InvariantSH
 * @notice Invariant test suite for SessionHandler.
 *
 * Run with:
 *   forge test --match-contract InvariantSH -vv
 *   forge test --match-contract InvariantSH --fuzz-runs 1000 -vv
 */
contract InvariantSH is StdInvariant, Test {
    SessionHandler sessionHandler;
    SHHandler handler;
    ERC20Mock usdc;
    SHOracle oracle;
    SHRegistry feeRegistry;
    HelperConfig.NetworkConfig config;

    function setUp() public {
        DeploySHProtocol deployer = new DeploySHProtocol();
        SHFactory factory;
        SHTreasury treasury;
        (factory, treasury, config, oracle) = deployer.run();
        feeRegistry = SHRegistry(treasury.REGISTRY());
        vm.prank(config.account);
        sessionHandler = SessionHandler(payable(factory.deployWallet()));
        usdc = ERC20Mock(config.usdc);

        vm.deal(address(sessionHandler), 1 ether);
        usdc.mint(address(sessionHandler), 10_000e6);

        handler = new SHHandler(sessionHandler, usdc, sessionHandler.owner());

        targetContract(address(handler));
    }

    // ──────────────────────────────────────────────
    //  Invariants
    // ──────────────────────────────────────────────

    /**
     * @notice spentAmount must never exceed spendingLimit.
     * @dev This is the core financial safety invariant. If broken, a session key
     *      could drain more funds than its owner authorised.
     */
    function invariant_spentAmountNeverExceedsLimit() public view {
        uint256 n = handler.sessionKeyCount();
        for (uint256 i; i < n; i++) {
            address key = handler.sessionKeys(i);
            SessionHandler.Session memory s = sessionHandler.getSession(key);
            assertLe(s.spentAmount, s.spendingLimit, "spentAmount > spendingLimit");
        }
    }

    /**
     * @notice Every successfully registered session must have validFrom < validUntil.
     * @dev addSessionKey reverts on violated time range, so any persisted session
     *      must satisfy this. A ghost session with inverted timestamps would indicate
     *      a storage-write bug.
     */
    function invariant_validTimeRangeForAllSessions() public view {
        uint256 n = handler.sessionKeyCount();
        for (uint256 i; i < n; i++) {
            address key = handler.sessionKeys(i);
            SessionHandler.Session memory s = sessionHandler.getSession(key);
            // spendingLimit == 0 means the session was revoked/deleted — skip the zero struct
            if (s.spendingLimit == 0) continue;
            assertLt(s.validFrom, s.validUntil, "validFrom >= validUntil");
        }
    }

    /**
     * @notice Revoked sessions (active == false) must report inactive.
     * @dev Tests that revokeSessionKey cannot be bypassed and that isSessionActive
     *      correctly reads the active flag.
     */
    function invariant_revokedSessionsAreInactive() public view {
        uint256 n = handler.sessionKeyCount();
        for (uint256 i; i < n; i++) {
            address key = handler.sessionKeys(i);
            SessionHandler.Session memory s = sessionHandler.getSession(key);
            if (!s.active) {
                assertFalse(sessionHandler.isSessionActive(key), "revoked session reported active");
            }
        }
    }

    /**
     * @notice getRemainingBudget must equal spendingLimit - spentAmount, clamped to 0.
     * @dev Verifies that the view function is consistent with raw storage.
     */
    function invariant_remainingBudgetConsistency() public view {
        uint256 n = handler.sessionKeyCount();
        for (uint256 i; i < n; i++) {
            address key = handler.sessionKeys(i);
            SessionHandler.Session memory s = sessionHandler.getSession(key);
            uint256 remaining = sessionHandler.getRemainingBudget(key);

            if (s.spentAmount >= s.spendingLimit) {
                assertEq(remaining, 0, "remaining should be 0 when limit exhausted");
            } else {
                assertEq(remaining, s.spendingLimit - s.spentAmount, "remaining != limit - spent");
            }
        }
    }

    /**
     * @notice isSessionWithinBudget(key, 0) must agree with raw storage.
     * @dev isSessionWithinBudget checks spentAmount + value <= spendingLimit.
     *      With value == 0 this is equivalent to spentAmount <= spendingLimit.
     */
    function invariant_budgetCheckAgreesWithStorage() public view {
        uint256 n = handler.sessionKeyCount();
        for (uint256 i; i < n; i++) {
            address key = handler.sessionKeys(i);
            SessionHandler.Session memory s = sessionHandler.getSession(key);
            if (!s.active) continue;

            bool withinBudget = sessionHandler.isSpendingWithinBudget(key, address(0), 0);
            assertEq(withinBudget, s.spentAmount <= s.spendingLimit, "isSessionWithinBudget disagrees with storage");
        }
    }

    /**
     * @notice address(0) must never have a session stored with active == true.
     * @dev addSessionKey rejects address(0) — this verifies that invariant is preserved.
     */
    function invariant_zeroAddressNeverHasActiveSession() public view {
        assertFalse(sessionHandler.getSession(address(0)).active, "address(0) has an active session");
    }
}
