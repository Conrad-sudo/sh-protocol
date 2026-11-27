// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {
    IERC7579Module,
    IERC7579Execution,
    IERC7579ModuleConfig,
    MODULE_TYPE_EXECUTOR
} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";

/**
 * @title RecurringPaymentModule
 * @author Conrad Japhet
 * @notice ERC-7579 EXECUTOR that pays a fixed amount of one token (or the native coin) to one
 *         recipient on a fixed interval, so a wallet owner approves a recurring payment ONCE instead
 *         of signing every instalment. Installed on every SessionHandler wallet by SHFactory.
 * @dev The flow: the owner creates a schedule here with one direct transaction. When a payment falls
 *      due, a keeper (the protocol's backend) calls {run}; this module calls the wallet's
 *      `executeFromExecutor`; the wallet runs its session guard, takes its fee (protocol fee plus
 *      the registry's keeper fee) and sends the payment through its spending-limit hook, so every
 *      instalment counts toward the wallet's USD cap exactly like a session-key transaction.
 *
 *      WHO CAN DO WHAT -- the property everything else rests on:
 *        - {createSchedule} / {cancelSchedule}: ONLY the wallet's owner, calling this module
 *          DIRECTLY (msg.sender == account.owner()). A session key (passkey) acts AS the wallet, so
 *          anything it routes through `execute` arrives here with msg.sender == the wallet, which is
 *          never the owner -- a key can neither create a schedule (which would keep paying after the
 *          key expires) nor cancel one. A wallet that owns itself is refused for the same reason.
 *        - {run}: anyone. The schedule fixes the token, recipient, amount and timing, and {run} only
 *          pays what is due, so the caller chooses nothing but the moment -- and only within the due
 *          window. A stranger calling it early pays nothing; calling it on time pays what the owner
 *          already agreed to.
 *
 *      Missed payments do NOT pile up. If several periods pass without a {run} (the wallet was
 *      paused, out of funds, over its cap, or the keeper was down), the next {run} pays ONE
 *      instalment and moves the due date to the next period still in the future. Catching up would
 *      let a stalled schedule empty a wallet in one burst.
 *
 *      A failed payment reverts the whole {run}, so the due date does not move and nothing is
 *      charged; the keeper retries later. Uninstalling the module invalidates every schedule the
 *      wallet had, in one step, by bumping its {generation}: a reinstall starts with none.
 */
contract RecurringPaymentModule is IERC7579Module {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when the caller is not the wallet's owner, or the wallet owns itself.
    error RecurringPayment_NotOwner(address account, address caller);
    /// @dev Thrown by {createSchedule} when this module is not installed on `account` as an executor.
    error RecurringPayment_NotInstalled(address account);
    /// @dev Thrown when a schedule id is unknown, cancelled, finished, or from before an uninstall.
    error RecurringPayment_UnknownSchedule(address account, uint256 id);
    /// @dev Thrown by {run} before a schedule's next payment is due.
    error RecurringPayment_NotDue(uint256 dueAt);
    /// @dev Thrown on a zero recipient, or the wallet itself as the recipient.
    error RecurringPayment_InvalidRecipient();
    /// @dev Thrown on a token address with no code (address(0) means native, and is allowed).
    error RecurringPayment_InvalidToken();
    /// @dev Thrown on a zero amount.
    error RecurringPayment_InvalidAmount();
    /// @dev Thrown on an interval shorter than {MIN_INTERVAL}.
    error RecurringPayment_InvalidInterval(uint256 interval, uint256 minInterval);
    /// @dev Thrown when a non-zero end falls before the first payment.
    error RecurringPayment_InvalidEnd(uint256 end, uint256 firstDue);
    /// @dev Thrown when a token's `transfer` returned false instead of reverting.
    error RecurringPayment_TransferFailed();

    /*//////////////////////////////////////////////////////////////
                                 EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when the owner creates a schedule.
    /// @param account   The wallet that will pay.
    /// @param id        The schedule's id within that wallet.
    /// @param token     The token paid, or address(0) for the native coin.
    /// @param recipient Who is paid.
    /// @param amount    The amount of each payment, in the token's smallest unit.
    /// @param interval  Seconds between payments.
    /// @param firstDue  When the first payment falls due.
    /// @param end       Last moment a payment may fall due, or 0 for no end.
    event ScheduleCreated(
        address indexed account,
        uint256 indexed id,
        address token,
        address recipient,
        uint256 amount,
        uint256 interval,
        uint256 firstDue,
        uint256 end
    );

    /// @notice Emitted when the owner cancels a schedule.
    event ScheduleCancelled(address indexed account, uint256 indexed id);

    /// @notice Emitted when {run} pays a schedule's instalment.
    /// @param account The wallet that paid.
    /// @param id      The schedule.
    /// @param dueAt   The due date this payment settled.
    /// @param nextDue When the next payment falls due, or 0 when this was the schedule's last.
    event RecurringPaymentExecuted(address indexed account, uint256 indexed id, uint256 dueAt, uint256 nextDue);

    /// @notice Emitted when a wallet uninstalls this module, invalidating all of its schedules.
    event SchedulesCleared(address indexed account, uint256 newGeneration);

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Shortest interval a schedule may have. Each payment pays the wallet's fee, so a very
    ///         short interval mostly buys fees; an hour still covers any real billing cycle.
    uint48 public constant MIN_INTERVAL = 1 hours;

    /// @dev ERC-7579 mode for one plain call that reverts on failure: CALLTYPE_SINGLE (0x00) and
    ///      EXECTYPE_DEFAULT (0x00), no selector, no payload -- all zero bytes.
    bytes32 private constant _SINGLE_DEFAULT_MODE = bytes32(0);

    /**
     * @notice One recurring payment. Three slots.
     * @param token      Token paid, or address(0) for the native coin.
     * @param nextDue    When the next payment falls due.
     * @param end        Last moment a payment may fall due (inclusive), or 0 for no end.
     * @param recipient  Who is paid.
     * @param interval   Seconds between due dates.
     * @param generation The wallet's {generation} when the schedule was created. A schedule from an
     *                   older generation was wiped by an uninstall.
     * @param amount     Amount of each payment. Zero marks an empty slot (never created, cancelled or
     *                   finished), since {createSchedule} refuses a zero amount.
     */
    struct Schedule {
        address token;
        uint48 nextDue;
        uint48 end;
        address recipient;
        uint48 interval;
        uint48 generation;
        uint256 amount;
    }

    /// @dev account => id => schedule. Ids start at 1 and are never reused, uninstalls included.
    mapping(address account => mapping(uint256 id => Schedule)) private _schedules;

    /// @notice How many schedules `account` has ever created; also the most recent id. Lets a keeper
    ///         walk a wallet's ids 1..scheduleCount without reading logs.
    mapping(address account => uint256) public scheduleCount;

    /// @notice Bumped each time `account` uninstalls this module. Only schedules created under the
    ///         current generation are live.
    mapping(address account => uint48) public generation;

    /*//////////////////////////////////////////////////////////////
                           ERC-7579 MODULE
    //////////////////////////////////////////////////////////////*/

    /// @inheritdoc IERC7579Module
    /// @dev Nothing to set up: schedules are created afterwards, by the owner.
    function onInstall(bytes calldata) external override {}

    /// @inheritdoc IERC7579Module
    /// @dev Wipes every schedule of the calling wallet in O(1) by moving it to a new generation. Keyed
    ///      by msg.sender, so a direct call from anyone else only touches that caller's own schedules.
    function onUninstall(bytes calldata) external override {
        uint48 next = generation[msg.sender] + 1;
        generation[msg.sender] = next;
        emit SchedulesCleared(msg.sender, next);
    }

    /// @inheritdoc IERC7579Module
    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_EXECUTOR;
    }

    /*//////////////////////////////////////////////////////////////
                          OWNER: SCHEDULES
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Creates a recurring payment from `account`. Only the wallet's owner, calling directly.
     * @dev Checks what can be known now: a real recipient, a token with code, a non-zero amount, an
     *      interval of at least {MIN_INTERVAL}, an end no earlier than the first payment. What can
     *      only be known at payment time -- the balance, the spending cap, the wallet's allowlist --
     *      is checked by the wallet on each {run}.
     * @param account   The SessionHandler wallet that will pay. This module must be installed on it.
     * @param token     Token to pay, or address(0) for the native coin.
     * @param recipient Who to pay. Not address(0), not the wallet itself.
     * @param amount    Amount of each payment, in the token's smallest unit. Non-zero.
     * @param interval  Seconds between payments. At least {MIN_INTERVAL}.
     * @param firstDue  When the first payment falls due. A time already past means now.
     * @param end       Last moment a payment may fall due, or 0 for no end. Otherwise at least the
     *                  first due date.
     * @return id The new schedule's id within `account`.
     */
    function createSchedule(
        address account,
        address token,
        address recipient,
        uint256 amount,
        uint48 interval,
        uint48 firstDue,
        uint48 end
    ) external returns (uint256 id) {
        _requireOwner(account);
        if (!IERC7579ModuleConfig(account).isModuleInstalled(MODULE_TYPE_EXECUTOR, address(this), "")) {
            revert RecurringPayment_NotInstalled(account);
        }
        if (recipient == address(0) || recipient == account) revert RecurringPayment_InvalidRecipient();
        if (token != address(0) && token.code.length == 0) revert RecurringPayment_InvalidToken();
        if (amount == 0) revert RecurringPayment_InvalidAmount();
        if (interval < MIN_INTERVAL) revert RecurringPayment_InvalidInterval(interval, MIN_INTERVAL);
        if (firstDue < block.timestamp) firstDue = SafeCast.toUint48(block.timestamp);
        if (end != 0 && end < firstDue) revert RecurringPayment_InvalidEnd(end, firstDue);

        id = ++scheduleCount[account];
        _schedules[account][id] = Schedule({
            token: token,
            nextDue: firstDue,
            end: end,
            recipient: recipient,
            interval: interval,
            generation: generation[account],
            amount: amount
        });
        emit ScheduleCreated(account, id, token, recipient, amount, interval, firstDue, end);
    }

    /**
     * @notice Cancels a schedule. Only the wallet's owner, calling directly.
     * @param account The wallet the schedule belongs to.
     * @param id      The schedule to cancel. Must be live.
     */
    function cancelSchedule(address account, uint256 id) external {
        _requireOwner(account);
        _requireLive(account, id);
        delete _schedules[account][id];
        emit ScheduleCancelled(account, id);
    }

    /*//////////////////////////////////////////////////////////////
                              KEEPER
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Pays a schedule's instalment if it is due. Callable by anyone; the protocol's backend
     *         is the keeper that calls it on time.
     * @dev Checks-effects-interactions: the due date moves (or the finished schedule is deleted)
     *      BEFORE the wallet is called, so anything re-entering {run} during the payment -- a native
     *      recipient's code, a token hook -- finds nothing due.
     * @dev One instalment per call, however many periods were missed; the next due date is the first
     *      one on the schedule's grid that is still in the future. When that passes the schedule's
     *      end, the schedule is deleted after this payment.
     * @dev The wallet charges its fee and applies the session guard and spending cap; any failure
     *      there reverts this call and leaves the schedule untouched.
     * @param account The wallet to pay from.
     * @param id      The schedule to pay.
     */
    function run(address account, uint256 id) external {
        Schedule storage schedule = _requireLive(account, id);
        uint256 dueAt = schedule.nextDue;
        if (block.timestamp < dueAt) revert RecurringPayment_NotDue(dueAt);

        // Copied before the effects below, which may delete the schedule.
        address token = schedule.token;
        address recipient = schedule.recipient;
        uint256 amount = schedule.amount;
        uint256 interval = schedule.interval;
        uint256 end = schedule.end;

        // The first due date on the grid (dueAt + k * interval) still in the future.
        uint256 nextDue = dueAt + ((block.timestamp - dueAt) / interval + 1) * interval;
        if (end != 0 && nextDue > end) {
            delete _schedules[account][id];
            nextDue = 0;
        } else {
            schedule.nextDue = SafeCast.toUint48(nextDue);
        }
        emit RecurringPaymentExecuted(account, id, dueAt, nextDue);


        (address target, uint256 value, bytes memory data)= token== address(0)
        ?(recipient,amount, bytes("")):
        (token,uint256(0),abi.encodeWithSelector(IERC20.transfer.selector,recipient,amount));

        // ERC-7579 single-call encoding (what ERC7579Utils.encodeSingle builds, which only takes
        // calldata): target, value and call data, packed.
        bytes[] memory results =
            IERC7579Execution(account).executeFromExecutor(_SINGLE_DEFAULT_MODE, abi.encodePacked(target, value, data));

        // A token that signals failure by returning false (instead of reverting) must not count as a
        // payment. No return data at all (USDT on Ethereum) is a success, as in SafeERC20.
        if (token != address(0) && results[0].length != 0 && !abi.decode(results[0], (bool))) {
            revert RecurringPayment_TransferFailed();
        }
    }

    /*//////////////////////////////////////////////////////////////
                                 VIEWS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice A schedule and whether it is live.
     * @return schedule The stored schedule (all zero when never created, cancelled or finished).
     * @return live     True when the schedule exists in the wallet's current generation.
     */
    function getSchedule(address account, uint256 id) external view returns (Schedule memory schedule, bool live) {
        schedule = _schedules[account][id];
        live = schedule.amount != 0 && schedule.generation == generation[account];
    }

    /// @notice True when `id` is live and its next payment is due now.
    function isDue(address account, uint256 id) external view returns (bool) {
        Schedule storage schedule = _schedules[account][id];
        return schedule.amount != 0 && schedule.generation == generation[account] && block.timestamp >= schedule.nextDue;
    }

    /*//////////////////////////////////////////////////////////////
                                INTERNAL
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts unless the caller is `account`'s owner and that owner is not the wallet itself.
    ///      The second half matters: a wallet that owns itself would let a session key, acting as the
    ///      wallet, pass the first half.
    function _requireOwner(address account) private view {
        address owner = Ownable(account).owner();
        if (msg.sender != owner || owner == account) revert RecurringPayment_NotOwner(account, msg.sender);
    }

    /// @dev Returns the schedule, reverting unless it exists in the wallet's current generation.
    function _requireLive(address account, uint256 id) private view returns (Schedule storage schedule) {
        schedule = _schedules[account][id];
        if (schedule.amount == 0 || schedule.generation != generation[account]) {
            revert RecurringPayment_UnknownSchedule(account, id);
        }
    }
}
