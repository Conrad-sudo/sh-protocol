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
import {P256} from "@openzeppelin/contracts/utils/cryptography/P256.sol";
import {PasskeyHelper} from "../utils/PasskeyHelper.sol";

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
 *      - The PASSKEY LIST has a ghost model of its own (see {addPasskey}): it replays the wallet's
 *        rules (renewal in place, a free slot or a sweep of expired keys for a new key, the cap of
 *        five) and, before every call, works out whether the call should succeed. A mismatch either
 *        way sets {violated}, which an invariant checks -- handler reverts are swallowed
 *        (fail_on_revert = false), so a plain assert here would never be seen.
 */
contract SHHandler is Test {
    using PasskeyHelper for PasskeyHelper.Passkey;

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

    /// @dev The passkeys the handler draws from. More than the cap of five, so the cap can be hit.
    uint256 public constant POOL_SIZE = 8;
    PasskeyHelper.Passkey[] internal _pool;

    /// @dev Ghost mirror of the wallet's passkey list: which keys it should hold and until when.
    struct GhostKey {
        bytes32 id;
        uint48 validUntil;
    }

    GhostKey[] internal _ghost;
    /// @dev Set when the wallet answered a call differently from the model (and why).
    bool public violated;
    string public violation;

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

        for (uint256 i = 0; i < POOL_SIZE; i++) {
            _pool.push(PasskeyHelper.create(string.concat("handler passkey ", vm.toString(i))));
        }
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

    /// @dev Owner grants (or renews) one of the pool's passkeys. The model decides first whether that
    ///      should work: a key already held is renewed in place; a NEW key needs a free slot, and when
    ///      five are held the wallet sweeps the expired ones before it refuses. Half the deadlines are
    ///      short (up to 3 days) so the clock in {warpTime} really does expire keys.
    function addPasskey(uint256 which, uint256 ttl) public {
        PasskeyHelper.Passkey memory key = _pool[which % POOL_SIZE];
        uint256 span = ttl % 2 == 0 ? 3 days : uint256(wallet.MAX_SESSION_TTL());
        uint48 validUntil = uint48(block.timestamp + bound(ttl, 1, span));
        bytes32 id = key.keyId();
        (uint256 index, bool found) = _ghostIndex(id);
        bool expectOk = found || _ghost.length < wallet.MAX_SESSION_KEYS() || _ghostExpiredCount() > 0;

        vm.prank(owner);
        try wallet.addSession(key.qx, key.qy, validUntil) {
            if (!expectOk) {
                _flag("addSession succeeded with a full list of live keys");
            } else if (found) {
                _ghost[index].validUntil = validUntil;
            } else {
                if (_ghost.length >= wallet.MAX_SESSION_KEYS()) _ghostSweep();
                _ghost.push(GhostKey({id: id, validUntil: validUntil}));
            }
        } catch {
            if (expectOk) _flag("addSession reverted when the model said it should work");
        }
    }

    /// @dev Owner revokes one pool key. Revoking a key the wallet does not hold must revert.
    function removePasskey(uint256 which) public {
        bytes32 id = _pool[which % POOL_SIZE].keyId();
        (uint256 index, bool found) = _ghostIndex(id);

        vm.prank(owner);
        try wallet.removeSession(id) {
            if (!found) _flag("removeSession succeeded for a key that is not held");
            else _ghostRemoveAt(index);
        } catch {
            if (found) _flag("removeSession reverted for a held key");
        }
    }

    /// @dev Owner revokes everything.
    function removeAllPasskeys() public {
        vm.prank(owner);
        try wallet.removeAllSessions() {
            delete _ghost;
        } catch {
            _flag("removeAllSessions reverted");
        }
    }

    /// @dev A non-owner tries every way of changing the list; none may work. (The model is NOT
    ///      updated, so a success also shows up as a divergence.)
    function nonOwnerManagesPasskeys(address caller, uint256 which) public {
        if (caller == owner) return;
        PasskeyHelper.Passkey memory key = _pool[which % POOL_SIZE];
        uint48 validUntil = uint48(block.timestamp + 1 days);

        vm.prank(caller);
        try wallet.addSession(key.qx, key.qy, validUntil) {
            _flag("a non-owner added a passkey");
        } catch {}
        vm.prank(caller);
        try wallet.removeSession(key.keyId()) {
            _flag("a non-owner removed a passkey");
        } catch {}
        vm.prank(caller);
        try wallet.removeAllSessions() {
            _flag("a non-owner removed every passkey");
        } catch {}
    }

    /// @dev The owner offers a public key that is not on the curve; the wallet must refuse it.
    function addOffCurveKey(bytes32 qx, bytes32 qy) public {
        if (P256.isValidPublicKey(qx, qy)) return;
        vm.prank(owner);
        try wallet.addSession(qx, qy, uint48(block.timestamp + 1 days)) {
            _flag("an off-curve key was accepted");
        } catch {}
    }

    /// @dev Advances time (possibly past the window) and refreshes the USDC feed so metering keeps
    ///      working after the jump; the window-roll logic is what's under test, not feed staleness.
    function warpTime(uint256 secondsForward) public {
        secondsForward = bound(secondsForward, 1, 3 days);
        vm.warp(block.timestamp + secondsForward);
        usdcFeed.updateAnswer(usdcFeed.latestAnswer());
    }

    /*//////////////////////////////////////////////////////////////
                     THE PASSKEY GHOST MODEL
    //////////////////////////////////////////////////////////////*/

    function _flag(string memory why) internal {
        if (!violated) {
            violated = true;
            violation = why;
        }
    }

    function _ghostIndex(bytes32 id) internal view returns (uint256 index, bool found) {
        for (uint256 i = 0; i < _ghost.length; i++) {
            if (_ghost[i].id == id) return (i, true);
        }
    }

    /// @dev Keys past their deadline (`block.timestamp > validUntil`, the EntryPoint's rule).
    function _ghostExpiredCount() internal view returns (uint256 n) {
        for (uint256 i = 0; i < _ghost.length; i++) {
            if (block.timestamp > _ghost[i].validUntil) n++;
        }
    }

    function _ghostRemoveAt(uint256 index) internal {
        _ghost[index] = _ghost[_ghost.length - 1];
        _ghost.pop();
    }

    /// @dev What the wallet does to a full list before it takes a new key: drop every expired key.
    function _ghostSweep() internal {
        for (uint256 i = _ghost.length; i > 0; i--) {
            if (block.timestamp > _ghost[i - 1].validUntil) _ghostRemoveAt(i - 1);
        }
    }

    /*//////////////////////////////////////////////////////////////
                     HELPERS FOR INVARIANT ITERATION
    //////////////////////////////////////////////////////////////*/

    function ghostLength() external view returns (uint256) {
        return _ghost.length;
    }

    /// @dev Whether the model holds `id`, and until when.
    function ghostFind(bytes32 id) external view returns (bool found, uint48 validUntil) {
        (uint256 index, bool isThere) = _ghostIndex(id);
        if (!isThere) return (false, 0);
        return (true, _ghost[index].validUntil);
    }

    function poolKeyId(uint256 i) external view returns (bytes32) {
        return _pool[i].keyId();
    }
}
