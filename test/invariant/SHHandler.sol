// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";

/**
 * @title SHHandler
 * @notice Fuzzer-facing handler wrapping every mutating action on a SessionHandler wallet under
 *         the USD-spending-cap design. The invariant suite targets this contract so Foundry calls
 *         its functions in random order with random inputs.
 * @dev Ghost variables mirror what the module SHOULD be tracking, computed independently with the
 *      same oracle the module uses:
 *      - ghostSpent replicates the net-value metering incl. window rolls: before each execute it
 *        applies the same "roll if expired" rule, and on success adds the same oracle-priced net
 *        outflow postCheck would have added. Any divergence from the module's spentInWindow is a
 *        metering bug.
 *      - ghostLimit / ghostWindow mirror the owner's config writes; if a non-owner ever managed to
 *        change them, the config-consistency invariant catches the divergence.
 *      USDC stays permanently watched (the handler never removes it) so ghost metering of USDC
 *      flows is always live; DAI's watch status is toggled to fuzz list add/remove.
 */
contract SHHandler is Test {
    SessionHandler public wallet;
    SpendingLimitModule public module;
    SHOracle public oracle;
    ERC20Mock public usdc;
    ERC20Mock public dai;
    MockV3Aggregator public usdcFeed;
    address public owner;
    address public sink = makeAddr("sink");

    // ── ghost state ──────────────────────────────────────────────
    int256 public ghostSpent;
    uint48 public ghostWindowStart;
    int256 public ghostLimit;
    uint256 public ghostWindowDuration;
    /// @dev Session keys ever touched, with their expected allowlist state.
    /// @dev Ghost mirror of the wallet's ONE session key and its deadline. A list is no longer the
    ///      right shape: the wallet holds a single key, and granting another evicts it.
    address public expectedSession;
    uint48 public expectedSessionValidUntil;
    mapping(address => bool) private _tracked;

    constructor(
        SessionHandler _wallet,
        SpendingLimitModule _module,
        SHOracle _oracle,
        ERC20Mock _usdc,
        ERC20Mock _dai,
        MockV3Aggregator _usdcFeed,
        address _owner
    ) {
        wallet = _wallet;
        module = _module;
        oracle = _oracle;
        usdc = _usdc;
        dai = _dai;
        usdcFeed = _usdcFeed;
        owner = _owner;

        SpendingLimitModule.Config memory cfg = module.getConfig(address(wallet));
        ghostSpent = cfg.spentInWindow;
        ghostWindowStart = cfg.windowStart;
        ghostLimit = cfg.dailyLimitUsd;
        ghostWindowDuration = cfg.windowDuration;
    }

    /*//////////////////////////////////////////////////////////////
                          GHOST BOOKKEEPING
    //////////////////////////////////////////////////////////////*/

    /// @dev Mirror of the module's _rollWindowIfNeeded, applied to the ghost BEFORE an execute.
    ///      Returns the ghost spend base the coming transaction accumulates on top of.
    function _ghostBaseAfterRoll() internal view returns (int256 base, uint48 startAfter) {
        if (block.timestamp >= uint256(ghostWindowStart) + ghostWindowDuration) {
            return (0, uint48(block.timestamp));
        }
        return (ghostSpent, ghostWindowStart);
    }

    /*//////////////////////////////////////////////////////////////
                            HANDLER ACTIONS
    //////////////////////////////////////////////////////////////*/

    /// @dev Owner-driven single transfer of USDC out. Mirrors the metering into ghostSpent when it
    ///      lands; a revert (budget exceeded, stale feed) leaves both real and ghost state untouched.
    function transferOut(uint256 amount) public {
        amount = bound(amount, 0, usdc.balanceOf(address(wallet)));
        (int256 base, uint48 startAfter) = _ghostBaseAfterRoll();
        int256 outUsd = amount == 0 ? int256(0) : oracle.getPrice(address(usdc), amount);

        bytes memory payload =
            abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.transfer, (sink, amount)));
        vm.prank(owner);
        try wallet.execute(bytes32(0), payload) {
            ghostSpent = base + outUsd;
            ghostWindowStart = startAfter;
        } catch { /* budget/staleness revert: nothing changed on-chain either */ }
    }

    /// @dev Batch that mints USDC in and transfers USDC out in one tx: only the net outflow may be
    ///      metered, and a net inflow must meter nothing.
    function mintAndTransfer(uint256 inAmount, uint256 outAmount) public {
        inAmount = bound(inAmount, 0, 1_000_000e6);
        outAmount = bound(outAmount, 0, usdc.balanceOf(address(wallet)) + inAmount);
        (int256 base, uint48 startAfter) = _ghostBaseAfterRoll();

        int256 netUsd;
        if (outAmount > inAmount) {
            netUsd = oracle.getPrice(address(usdc), outAmount - inAmount);
        }

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.mint, (address(wallet), inAmount)));
        execs[1] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (sink, outAmount)));

        vm.prank(owner);
        try wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs)) {
            ghostSpent = base + netUsd;
            ghostWindowStart = startAfter;
        } catch {}
    }

    /// @dev Owner reconfigures the cap; ghost mirrors only successful writes.
    function setDailyLimit(int256 newLimit) public {
        newLimit = bound(newLimit, 0, int256(1e30));
        vm.prank(owner);
        try wallet.setDailyLimit(newLimit) {
            ghostLimit = newLimit;
        } catch {}
    }

    /// @dev Owner reconfigures the window; ghost mirrors only successful writes.
    function setWindowDuration(uint256 newDuration) public {
        newDuration = bound(newDuration, 1, 30 days);
        vm.prank(owner);
        try wallet.setWindowDuration(newDuration) {
            ghostWindowDuration = newDuration;
        } catch {}
    }

    /// @dev Toggles DAI's watch status (USDC deliberately stays watched forever — see contract dev note).
    function toggleDaiWatched(bool add) public {
        vm.prank(owner);
        if (add) {
            try wallet.addWatchedToken(address(dai)) {} catch {}
        } else {
            try wallet.removeWatchedToken(address(dai)) {} catch {}
        }
    }

    /// @dev A non-owner tries to change the cap; must never succeed (ghost intentionally NOT updated,
    ///      so if it ever lands the config-consistency invariant breaks).
    function nonOwnerSetLimit(address caller, int256 newLimit) public {
        if (caller == owner || caller == address(wallet)) return;
        vm.prank(caller);
        try wallet.setDailyLimit(newLimit) {} catch {}
        vm.prank(caller);
        try module.setDailyLimit(newLimit) {} catch {}
    }

    /// @dev Owner grants or revokes THE session key; ghosts mirror the key and its deadline. An
    ///      add always evicts whatever was authorized, which is the behaviour under test.
    function manageSession(address key, bool add, uint48 ttl) public {
        key = address(uint160(bound(uint256(uint160(key)), 1, type(uint160).max)));
        uint48 validUntil = uint48(block.timestamp) + uint48(bound(ttl, 1, wallet.MAX_SESSION_TTL()));
        vm.prank(owner);
        if (add) {
            try wallet.addSession(key, validUntil) {
                expectedSession = key;
                expectedSessionValidUntil = validUntil;
            } catch {}
        } else {
            try wallet.removeSession() {
                expectedSession = address(0);
                expectedSessionValidUntil = 0;
            } catch {}
        }
    }

    /// @dev Advances time (possibly past the window) and refreshes the USDC feed so metering keeps
    ///      working after the jump; the window-roll logic is what's under test, not feed staleness.
    function warpTime(uint256 secondsForward) public {
        secondsForward = bound(secondsForward, 1, 3 days);
        vm.warp(block.timestamp + secondsForward);
        usdcFeed.updateAnswer(usdcFeed.latestAnswer());
    }

    /*//////////////////////////////////////////////////////////////
                     HELPERS FOR INVARIANT ITERATION
    //////////////////////////////////////////////////////////////*/

    function sessionKeyCount() external view returns (uint256) {
        return expectedSession == address(0) ? 0 : 1;
    }
}
