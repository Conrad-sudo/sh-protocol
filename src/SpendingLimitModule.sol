// SPDX-License-Identifier: BUSL-1.1
// Copyright (C) 2026 Conrad Japhet
// Use of this software is governed by the Business Source License included in the LICENSE file.
// Change Date: 2029-06-12. Change License: MIT.
pragma solidity ^0.8.20;

import {
    IERC7579Hook,
    IERC7579Execution,
    Execution,
    MODULE_TYPE_HOOK
} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils, Mode, CallType} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {IERC20Extended, IERC20} from "./interfaces/IERC20Extended.sol";
import {SHOracle} from "./SHOracle.sol";
import {SHRegistry} from "./SHRegistry.sol";

/**
 * @title SpendingLimitModule
 * @notice ERC-7579 hook (type 4 only — not a validator) enforcing a global, USD-denominated
 *         daily spending cap across every venue and every key authorized on an installing account.
 * @dev One deployed instance serves every account that installs it; all state is keyed by
 *      `account` (msg.sender in preCheck/postCheck). See usd-spending-limit-module-roadmap.md
 *      for the full design rationale. The spec this implements:
 *
 *      preCheck:  (0) reject outright any execution that tries to reconfigure THIS module through
 *                     the account's execute path (see _isAdminCallDataExec) — checked first, so a
 *                     blocked transaction pays for nothing below it.
 *                 (1) collect any allowance-granting sub-calls — `approve` and legacy
 *                     `increaseAllowance` (rejecting unlimited approvals and unpriced tokens up
 *                     front); snapshot the account's native balance and the raw balance of every
 *                     watched token (no oracle calls yet).
 *      postCheck: (2) budget (net-value) — compare the account's native balance and each watched
 *                     token's current balance to their preCheck snapshots; only balances that
 *                     actually changed are priced via the oracle, and any net USD *decrease* across
 *                     them is added to the running window total; revert the whole transaction if
 *                     that pushes past the limit.
 *                 (3) no standing approvals — any allowance still STANDING after the calls ran
 *                     reverts the whole transaction outright, regardless of size or remaining
 *                     budget. An approval granted and consumed within the same transaction leaves
 *                     nothing standing and costs nothing; one left lingering for even a moment past
 *                     the transaction that granted it is never allowed, closing the deferred-pull
 *                     risk (roadmap §2.4) completely rather than merely bounding it.
 *      Safety defaults: a token with no Chainlink price feed can never be watched or approved
 *      through this hook (SHOracle.isPriced gates both); stale oracle data reverts. Staleness
 *      is enforced by SHOracle itself via each feed's own Chainlink-published heartbeat, not a
 *      per-account setting — see SHOracle's NatSpec for why that's the correct place for it.
 *
 *      The oracle is resolved per call from SHRegistry.priceOracle() rather than fixed at
 *      construction, so an oracle bug can be fixed for every already-deployed account at once
 *      without redeploying this module or any wallet. The cost of that reach is a trust
 *      assumption: whoever owns SHRegistry (SHTreasury, and through it the protocol operator)
 *      can repoint every installed account's oracle in one transaction. An oracle that priced
 *      everything near zero would make netOutflowUsd collapse and silently stop the cap from
 *      binding protocol-wide; one whose isPriced() always returned true would defeat the
 *      unpriced-token approval guard. The registry owner is therefore trusted for spending-cap
 *      integrity — see THREAT_MODEL.md §3.8. Only the registry address itself is immutable here.
 *
 *      Design notes worth knowing:
 *      - Net-value metering, not gross-outflow. postCheck measures the net USD change of the
 *        whole watched portfolio across a single transaction: total value before minus total value
 *        after. A swap that trades one watched token for another of equal value nets ~0 and barely
 *        touches the budget; a bad-rate or sandwiched swap registers its lost value automatically,
 *        as the drop in portfolio USD. This is why the module needs no per-venue swap decoder and
 *        no fair-price promise-check — losing value simply *is* spending, on any venue, and best
 *        execution stays the agent's job (the DEX router already enforces the agent's amountOutMin).
 *        The trade-off deliberately accepted: a compromised agent can still burn up to a full
 *        window's budget on one bad swap, since nothing blocks a bad rate mid-flight — the daily
 *        cap is the bound, not a per-swap rate guard. See roadmap §2.2–2.3, §2.5.
 *      - Inflows offset outflows WITHIN a transaction only. spentInWindow is floored per
 *        transaction (only a net *decrease* is added) and never banks credit across transactions,
 *        so an incoming payment in one transaction can never create spending headroom for a later
 *        one — the cap stays a hard "no more than X of net value may leave per window."
 *      - The no-standing-approval rule (3) and the net-value accounting (2) are deliberately
 *        INDEPENDENT: an approval never itself debits spentInWindow. If it did, a normal
 *        approve-then-swap in one transaction would be charged twice — once at approval, again when
 *        postCheck sees the value actually move. Rule (3) is checked on the RESIDUAL allowance left
 *        standing after the calls ran, not on the requested amount up front, and requires that
 *        residual be exactly zero — not merely "small enough." This is what fixes the gross-vs-net
 *        mismatch without reintroducing any size check: an $80 approval consumed by a net-neutral
 *        swap in the same transaction leaves exactly 0 standing and costs nothing, whereas checking
 *        the gross $80 up front would wrongly block a trade that spends nothing. Unlike a
 *        budget-sized ceiling, requiring exactly zero has no aggregation gap — several concurrent
 *        approvals can never collectively exceed anything, because none of them is allowed to
 *        survive its own transaction unconsumed in the first place. The cost of closing this
 *        completely: the hook cannot support any approval meant to outlive its own transaction
 *        (e.g. "approve once, trade against it all week") — every approval must be spent down to
 *        zero, atomically, in the same transaction that grants it.
 *      - The watched-token list is a bounded, per-account opt-in set (needed so the value
 *        snapshot/diff loop has a predictable gas cost) and governs ONLY steps (1)/(2). The
 *        no-standing-approval rule in step (3) applies globally to every `approve` call regardless
 *        of the target token's watch status — gated on whether SHOracle prices it, not on
 *        whether this account chose to watch it. That's what makes "no silent exemptions" for
 *        unpriced tokens actually hold.
 *      - Only watched tokens whose balance actually changed this transaction are priced via the
 *        oracle; every other watched token is compared by raw balance only (no oracle call, no
 *        staleness check) since a balance that didn't move can't have moved in value either. A
 *        stale feed therefore only reverts a transaction if the token with the stale feed actually
 *        moved this transaction — a plain transfer of one token is no longer at risk of being
 *        blocked by a stale feed on some other, untouched watched token, and this transaction's gas
 *        cost now scales with how many watched tokens actually moved, not with the size of the
 *        whole watched list.
 *      - Both `execute` and `executeFromExecutor` are decoded to collect approvals
 *        (OpenZeppelin's AccountERC7579Hooked wraps preCheck/postCheck around both). Rhinestone-style
 *        capability executors (swaps, payroll, intents) call through executeFromExecutor, so
 *        recognizing only `execute` would skip the no-standing-approval rule for exactly the flows
 *        this module cares about most. The net-value cap in postCheck applies either way, since it
 *        never needs to understand what ran.
 *      - CALLTYPE_DELEGATECALL sub-calls are not decoded to collect approvals (a delegatecall can
 *        run arbitrary code in the account's own context, so there is no reliable "selector" to
 *        inspect the way there is for a plain call). The net-value cap in postCheck still applies
 *        regardless of call type, since it never needs to understand what ran.
 *      - Config setters (limit, window, watched list, trusted-spender list) must stay out of reach
 *        of an untrusted session key: a key that could raise its own limit, reshape the watched list,
 *        or trust a malicious spender mid-transaction would make the cap meaningless. This module no
 *        longer merely *assumes* that — preCheck's step (0) refuses any execution that targets this
 *        module's own admin surface, so the setters cannot be driven through execute at all, on any
 *        host account. That guard cannot distinguish an owner from a session key (it is told the
 *        immediate caller, the EntryPoint for every UserOp, but never which key signed it), so it
 *        blocks the owner's execute path too; the owner's supported route
 *        is a DIRECT call in which the account itself is msg.sender (SessionHandler's owner-only
 *        passthroughs), which never passes through this hook. Scoping WHICH functions a key may call
 *        at all (Smart Sessions, roadmap §4) remains future work and stays the host's job.
 *      - Both `approve` and the legacy non-standard `increaseAllowance` are recognized as
 *        allowance-granting calls. OpenZeppelin removed increaseAllowance in v5, so this only
 *        matters for tokens still carrying it — but recognizing it is what stops such a token being
 *        used to grant an allowance the no-standing-approval rule would otherwise never see.
 *      - SpendMetered is emitted only when a transaction actually consumed budget (net outflow > 0).
 *        A net-neutral or net-inflow transaction is silent by design: nothing was metered.
 *      - Native ETH/BNB IS metered, unconditionally. preCheck snapshots the account's native
 *        balance and postCheck prices its net change (via the address(0) sentinel feed) alongside
 *        the watched tokens. This is required for correct accounting of native-involving swaps — a
 *        swapExactETHForTokens spends ETH to receive a watched token, which without native in the
 *        meter reads as a free inflow — and makes wraps (ETH<->WETH) net to ~0. Consequences worth
 *        knowing: native metering is NOT opt-in like the watched list (address(0) is rejected from
 *        that list), so the address(0) feed must stay registered and fresh or any native-moving
 *        transaction reverts; and gas is excluded from the meter because the ERC-4337 prefund leaves
 *        the account before preCheck and the unused-gas refund returns to the EntryPoint deposit
 *        after postCheck, never touching the preCheck->postCheck native delta.
 *      - Permit2/permit-signature approvals and Argent-style delay-and-escalate are deliberate v1
 *        exclusions per the roadmap. Any unwatched ERC-20 still sits outside the net-value meter.
 *
 */
contract SpendingLimitModule is IERC7579Hook {
    /*//////////////////////////////////////////////////////////////
                                ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @notice Thrown when an account calls onInstall but the module is already installed for it.
    error SpendingLimitModule_AlreadyInstalled();
    /// @notice Thrown when a call requires the module to be installed but the caller hasn't installed it.
    error SpendingLimitModule_NotInstalled();
    /// @notice Thrown when a daily limit is negative or larger than type(int128).max (~1.7e20 USD).
    ///         The limit is signed for arithmetic convenience with the int256 oracle values, but a
    ///         spending cap can never be below zero; the ceiling is what the int128 field can hold.
    error SpendingLimitModule_InvalidDailyLimit();
    /// @notice Thrown when the window duration is 0 (a window must have length) or exceeds the
    ///         uint48-seconds storage ceiling (~8.9 million years, so no real window trips this).
    error SpendingLimitModule_InvalidWindowDuration();
    /// @notice Thrown when an unpriced token is watched, or approved to a spender that is NOT on the
    ///         account's trusted-spender list. (A trusted spender -- e.g. the canonical DEX router --
    ///         may be approved even for an unpriced token, so LP tokens can flow through removeLiquidity.)
    /// @param token The token address that the oracle does not price.
    error SpendingLimitModule_TokenNotPriced(address token);
    /// @notice Thrown when adding a token would push the watched list past MAX_WATCHED_TOKENS.
    error SpendingLimitModule_TooManyWatchedTokens();
    /// @notice Thrown when adding a spender would push the trusted-spender list past MAX_TRUSTED_SPENDERS.
    error SpendingLimitModule_TooManyTrustedSpenders();
    /// @notice Thrown when address(0) is supplied as a trusted spender.
    error SpendingLimitModule_InvalidTrustedSpender();
    /// @notice Thrown when an unlimited (type(uint256).max) approval is attempted; these are never allowed.
    error SpendingLimitModule_UnlimitedApprovalRejected();
    /// @notice Thrown when an approval made in this transaction is still standing (not fully
    ///         consumed) once the transaction's calls finish executing. Standing approvals are
    ///         never allowed through this hook, regardless of size or remaining budget.
    /// @param token The token whose allowance is still standing.
    /// @param spender The approved spender that still holds a nonzero allowance.
    /// @param residual The amount (in the token's own units) still approved and unconsumed.
    error SpendingLimitModule_StandingApprovalNotAllowed(address token, address spender, uint256 residual);
    /// @notice Thrown when the current transaction pushes the window's spending past the daily limit.
    /// @param spentUsd Total USD spent in the window, including this transaction (18 decimals).
    /// @param dailyLimitUsd The account's configured per-window cap (18 decimals).
    error SpendingLimitModule_BudgetExceeded(int256 spentUsd, int256 dailyLimitUsd);

    /// @notice Thrown when an execution routed through the account's execute/executeFromExecutor path
    ///         targets this module's own admin surface (a config setter, onInstall, or onUninstall).
    ///         Applies to every caller including the account owner — see {_isAdminCallDataExec} for
    ///         why the hook cannot tell them apart, and which owner path is supported instead.
    error SpendingLimitModule_AdminExecution();
    /// @notice Thrown at construction when the supplied registry reports no price oracle, which also
    ///         catches the likelier mistake of passing the SHOracle address itself instead of the
    ///         SHRegistry: a non-registry address has no priceOracle() to call, so it reverts here
    ///         rather than bricking every hook call after deployment.
    error SpendingLimitModule_OracleNotSet();

    /*//////////////////////////////////////////////////////////////
                            STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @dev Bounds the value snapshot/diff loop's gas cost; accounts pick a subset to watch.
    uint256 internal constant MAX_WATCHED_TOKENS = 32;

    /// @dev Bounds the onUninstall cleanup loop over trusted spenders. Trusted spenders are only
    ///      iterated on uninstall (never per-transaction), so this cap exists purely to stop an
    ///      account bloating its own list into an un-uninstallable state -- a small ceiling is ample
    ///      (the canonical router, plus a handful of venues at most).
    uint256 internal constant MAX_TRUSTED_SPENDERS = 16;

    /// @notice The SHRegistry this module resolves its price oracle from on every valuation.
    /// @dev Immutable — the registry ADDRESS can never change, but the oracle it points at can. That
    ///      indirection is the whole point (fix an oracle bug for every deployed account at once) and
    ///      it is also the module's one trust assumption; see the contract-level NatSpec.
    address public immutable REGISTRY;

    /// @dev Per-account settings and running spend state. One entry per installing account.
    /// @dev Field order is chosen for storage packing: `installed`, the two uint48 window fields and
    ///      `spentInWindow` share one 32-byte slot (1 + 6 + 6 + 16 = 29 bytes). preCheck already reads
    ///      that slot for the install flag and the window, so postCheck then reads the tally warm; a
    ///      window reset writes the new start and the zeroed tally in ONE store; and because the slot
    ///      always holds `installed == true`, writing the tally never pays the 20,000-gas cost of
    ///      filling an all-zero slot, which a slot of its own did on every wallet's first spend.
    /// @dev int128 holds ~1.7e20 USD at 18 decimals. It cannot be cut off: {_setDailyLimit} keeps the
    ///      limit inside int128, and postCheck checks the new tally against the limit BEFORE storing
    ///      it, so any value that gets stored is at most the limit. The per-transaction outflow is
    ///      still computed at full int256; only the stored running total is narrow.
    /// @dev uint48 seconds spans ~8.9 million years, so both window fields are effectively unbounded.
    /// @dev Consumers must read this struct by FIELD NAME, not position: the order follows the packing,
    ///      so it can change (it did on 2026-09-22), and a positional read would then silently return
    ///      the wrong field. The Python app goes through contracts.read_spending_config for this.
    struct Config {
        bool installed; // true once onInstall has run; gates every hook and setter for the account
        uint48 windowStart; // timestamp anchoring the current window
        uint48 windowDuration; // seconds
        int128 spentInWindow; // 18-decimal USD net value spent so far in the current window
        int128 dailyLimitUsd; // 18-decimal USD
        address[] watchedTokens; // tokens whose value changes are metered (max MAX_WATCHED_TOKENS)
        address[] trustedSpenders; // spenders approvable even for unpriced tokens (max MAX_TRUSTED_SPENDERS)
    }

    /// @dev account => its full configuration and spend state.
    mapping(address account => Config) private _configs;
    /// @dev account => token => whether that token is on the account's watched list (fast membership test).
    mapping(address account => mapping(address token => bool)) private _isWatched;
    /// @dev account => spender => whether that spender is trusted (may receive approvals on unpriced
    ///      tokens). Fast membership test used by _validateApproval; the parallel Config.trustedSpenders
    ///      list exists only so onUninstall can clear these entries.
    mapping(address account => mapping(address spender => bool)) private _isTrustedSpender;

    /// @dev The module's own admin selectors (config setters + onInstall/onUninstall), populated once
    ///      in the constructor. preCheck rejects any execute-routed sub-call that pairs one of these
    ///      with address(this) as its target. A mapping rather than a chain of comparisons so the cost
    ///      of adding a selector is constant, and so the lookup is skipped entirely (short-circuited by
    ///      the target check) for the overwhelming majority of sub-calls, which aim elsewhere.
    mapping(bytes4 => bool) private _isAdminSelector;

    /*//////////////////////////////////////////////////////////////
                                EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when an account changes any scalar setting (limit or window).
    /// @param account The account whose config changed.
    event ConfigUpdated(address indexed account);
    /// @notice Emitted when a token is added to an account's watched list.
    /// @param account The account that added the token.
    /// @param token The token now being metered.
    event WatchedTokenAdded(address indexed account, address indexed token);
    /// @notice Emitted when a token is removed from an account's watched list.
    /// @param account The account that removed the token.
    /// @param token The token no longer being metered.
    event WatchedTokenRemoved(address indexed account, address indexed token);
    /// @notice Emitted when a spender is added to an account's trusted-spender list.
    /// @param account The account that added the spender.
    /// @param spender The spender now approvable even for unpriced tokens.
    event TrustedSpenderAdded(address indexed account, address indexed spender);
    /// @notice Emitted when a spender is removed from an account's trusted-spender list.
    /// @param account The account that removed the spender.
    /// @param spender The spender no longer trusted.
    event TrustedSpenderRemoved(address indexed account, address indexed spender);

    /// @notice Emitted by postCheck when a transaction consumed budget. Not emitted when the
    ///         transaction was net-neutral or a net inflow, since nothing was metered.
    /// @param account The account that spent.
    /// @param netOutflowUsd Net USD value (18 decimals) that left the metered portfolio in THIS
    ///        transaction — native delta plus every watched token that moved.
    /// @param spentInWindow The account's running window total AFTER adding netOutflowUsd, so a
    ///        consumer can track remaining budget without reading state.
    event SpendMetered(address indexed account, int256 netOutflowUsd, int256 spentInWindow);

    /**
     * @notice Deploys the module and wires it to the registry it resolves its price oracle from.
     * @dev Also registers this module's own admin selectors, which preCheck uses to refuse
     *      execute-routed reconfiguration of itself. Reverts if `registry` reports no price oracle —
     *      which is also what catches the easy mistake of passing the SHOracle address here (the
     *      parameter is a bare `address`, so the compiler cannot): an address with no priceOracle()
     *      reverts on the call, failing loudly at deploy instead of after every wallet is live.
     * @param registry Address of the deployed SHRegistry — the protocol's single source of truth for
     *        the current price oracle. NOT the SHOracle address itself.
     */
    constructor(address registry) {
        if (SHRegistry(registry).priceOracle() == address(0)) revert SpendingLimitModule_OracleNotSet();
        REGISTRY = registry;
        _isAdminSelector[this.setDailyLimit.selector] = true;
        _isAdminSelector[this.setWindowDuration.selector] = true;
        _isAdminSelector[this.addWatchedToken.selector] = true;
        _isAdminSelector[this.removeWatchedToken.selector] = true;
        _isAdminSelector[this.addTrustedSpender.selector] = true;
        _isAdminSelector[this.removeTrustedSpender.selector] = true;
        _isAdminSelector[this.onInstall.selector] = true;
        _isAdminSelector[this.onUninstall.selector] = true;
    }

    /*//////////////////////////////////////////////////////////////
                          IERC7579Module (shared)
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Turns the module on for the calling account and sets its starting rules.
     * @dev The account calls this on itself, so `msg.sender` is the account. Reverts if the
     *      account already has the module installed. The first spending window starts counting
     *      from the moment of install (windowStart = block.timestamp).
     * @param data ABI-encoded initial settings, in this exact order:
     *        - int256 dailyLimitUsd    Max USD (18 decimals) the account may spend per window. Must be
     *                                  >= 0 and <= type(int128).max.
     *        - uint256 windowDuration  Window length in seconds (e.g. 86400 for one day). Must be > 0.
     *        - address[] watchedTokens Tokens to meter; each must already be priced by the oracle.
     */
    function onInstall(bytes calldata data) external {
        address account = msg.sender;
        if (_configs[account].installed) revert SpendingLimitModule_AlreadyInstalled();

        (int256 dailyLimitUsd, uint256 windowDuration, address[] memory watchedTokens) =
            abi.decode(data, (int256, uint256, address[]));

        Config storage config = _configs[account];
        config.installed = true;
        // start the window for the spending limit (uint48 holds timestamps for millennia)
        config.windowStart = uint48(block.timestamp);

        _setDailyLimit(account, dailyLimitUsd);
        _setWindowDuration(account, windowDuration);
        for (uint256 i = 0; i < watchedTokens.length; i++) {
            _addWatchedToken(account, watchedTokens[i]);
        }
    }

    /**
     * @notice Turns the module off for the calling account and wipes all of its state.
     * @dev The account calls this on itself. Clears every watched-token entry so a later
     *      re-install starts from zero state. The `bytes` argument is required by the
     *      interface but unused here.
     */
    function onUninstall(bytes calldata) external {
        address account = msg.sender;
        Config storage config = _configs[account];
        uint256 length = config.watchedTokens.length;
        for (uint256 i = 0; i < length; i++) {
            delete _isWatched[account][config.watchedTokens[i]];
        }
        uint256 spenderLength = config.trustedSpenders.length;
        for (uint256 i = 0; i < spenderLength; i++) {
            delete _isTrustedSpender[account][config.trustedSpenders[i]];
        }
        delete _configs[account];
    }

    /**
     * @notice Reports which ERC-7579 module type this contract is.
     * @param moduleTypeId The module type id being asked about.
     * @return True only for the hook type (type 4); false for every other type, because this
     *         module is a hook and nothing else (not a validator, executor, or fallback handler).
     */
    function isModuleType(uint256 moduleTypeId) external pure returns (bool) {
        return moduleTypeId == MODULE_TYPE_HOOK;
    }

    /*//////////////////////////////////////////////////////////////
              CONFIG SETTERS (called by the account on itself)
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts unless the caller (an account) currently has this module installed.
    modifier onlyInstalled() {
        _requireInstalled();
        _;
    }

    /// @dev Shared guard behind `onlyInstalled`; reverts if the caller has not installed the module.
    function _requireInstalled() internal view {
        if (!_configs[msg.sender].installed) revert SpendingLimitModule_NotInstalled();
    }

    /**
     * @notice Changes the calling account's maximum spend per window.
     * @dev Takes effect immediately for the current window.
     * @param dailyLimitUsd New cap in USD with 18 decimals. Must be >= 0 and <= type(int128).max.
     */
    function setDailyLimit(int256 dailyLimitUsd) external onlyInstalled {
        _setDailyLimit(msg.sender, dailyLimitUsd);
        emit ConfigUpdated(msg.sender);
    }

    /**
     * @notice Changes how long the calling account's spending window lasts.
     * @dev Reverts on 0. Only affects when the window next rolls over; does not reset the
     *      current window on its own.
     * @param windowDuration New window length in seconds. Must be > 0.
     */
    function setWindowDuration(uint256 windowDuration) external onlyInstalled {
        _setWindowDuration(msg.sender, windowDuration);
        emit ConfigUpdated(msg.sender);
    }

    /**
     * @notice Adds a token to the calling account's watched (value-tracked) list.
     * @dev Reverts if SHOracle has no price feed for it — no silent exemptions.
     *      Reverts once the list already holds MAX_WATCHED_TOKENS. Does nothing if the token
     *      is already watched.
     * @param token Address of the ERC-20 token to start metering.
     */
    function addWatchedToken(address token) external onlyInstalled {
        _addWatchedToken(msg.sender, token);
    }

    /**
     * @notice Removes a token from the calling account's watched list. No-op if not watched.
     * @dev Uses swap-and-pop, so the order of the remaining watched tokens is not preserved.
     * @param token Address of the ERC-20 token to stop metering.
     */
    function removeWatchedToken(address token) external onlyInstalled {
        address account = msg.sender;
        if (!_isWatched[account][token]) return;

        Config storage config = _configs[account];
        uint256 length = config.watchedTokens.length;
        for (uint256 i = 0; i < length; i++) {
            if (config.watchedTokens[i] == token) {
                config.watchedTokens[i] = config.watchedTokens[length - 1];
                config.watchedTokens.pop();
                break;
            }
        }
        delete _isWatched[account][token];
        emit WatchedTokenRemoved(account, token);
    }

    /**
     * @notice Adds a spender to the calling account's trusted-spender list. A trusted spender may be
     *         granted an approval on an UNPRICED token (one the oracle has no feed for) -- the escape
     *         hatch that lets an LP-token approval reach the DEX router for removeLiquidity. The
     *         no-standing-approval rule still applies in full: any such approval must be consumed to
     *         exactly zero within the same transaction, and unlimited approvals are still rejected.
     *         Approvals on PRICED tokens never needed this and are entirely unaffected.
     * @dev Reverts on address(0) or once the list already holds MAX_TRUSTED_SPENDERS. No-op if the
     *      spender is already trusted. Assumed owner-reachable only (see contract-level NatSpec): a
     *      trusted spender can pull an unpriced token within one transaction, so this is a trust grant.
     * @param spender The spender (e.g. the canonical Uniswap V2 router) to trust.
     */
    function addTrustedSpender(address spender) external onlyInstalled {
        if (spender == address(0)) revert SpendingLimitModule_InvalidTrustedSpender();
        address account = msg.sender;
        if (_isTrustedSpender[account][spender]) return;

        Config storage config = _configs[account];
        if (config.trustedSpenders.length >= MAX_TRUSTED_SPENDERS) revert SpendingLimitModule_TooManyTrustedSpenders();
        config.trustedSpenders.push(spender);
        _isTrustedSpender[account][spender] = true;
        emit TrustedSpenderAdded(account, spender);
    }

    /**
     * @notice Removes a spender from the calling account's trusted-spender list. No-op if not trusted.
     * @dev Uses swap-and-pop, so the order of the remaining trusted spenders is not preserved.
     * @param spender The spender to stop trusting.
     */
    function removeTrustedSpender(address spender) external onlyInstalled {
        address account = msg.sender;
        if (!_isTrustedSpender[account][spender]) return;

        Config storage config = _configs[account];
        uint256 length = config.trustedSpenders.length;
        for (uint256 i = 0; i < length; i++) {
            if (config.trustedSpenders[i] == spender) {
                config.trustedSpenders[i] = config.trustedSpenders[length - 1];
                config.trustedSpenders.pop();
                break;
            }
        }
        delete _isTrustedSpender[account][spender];
        emit TrustedSpenderRemoved(account, spender);
    }

    /// @dev Writes the new daily limit; reverts on a negative value (0 is allowed — it disables
    ///      spending) and on anything past type(int128).max, so the cast below can never truncate.
    ///      Keeping the limit inside int128 is also what makes postCheck's store of the tally safe.
    function _setDailyLimit(address account, int256 dailyLimitUsd) internal {
        if (dailyLimitUsd < 0 || dailyLimitUsd > type(int128).max) revert SpendingLimitModule_InvalidDailyLimit();
        // forge-lint: disable-next-line(unsafe-typecast)
        _configs[account].dailyLimitUsd = int128(dailyLimitUsd);
    }

    /// @dev Writes the window length; reverts on 0 (zero-length window makes no sense) and on any
    ///      value past the uint48-seconds storage ceiling, so the cast below can never truncate.
    function _setWindowDuration(address account, uint256 windowDuration) internal {
        if (windowDuration == 0 || windowDuration > type(uint48).max) {
            revert SpendingLimitModule_InvalidWindowDuration();
        }
        _configs[account].windowDuration = uint48(windowDuration);
    }

    /// @dev Shared add logic. No-op if already watched; reverts if the token is unpriced or the list is full.
    function _addWatchedToken(address account, address token) internal {
        // Same short-circuit idiom as _validateApproval: address(0) rejects before the registry read.
        if (token == address(0) || !_getOracle().isPriced(token)) revert SpendingLimitModule_TokenNotPriced(token);
        if (_isWatched[account][token]) return;

        Config storage config = _configs[account];
        if (config.watchedTokens.length >= MAX_WATCHED_TOKENS) revert SpendingLimitModule_TooManyWatchedTokens();
        config.watchedTokens.push(token);
        _isWatched[account][token] = true;
        emit WatchedTokenAdded(account, token);
    }

    /*//////////////////////////////////////////////////////////////
                       IERC7579Hook (module type 4)
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Rejects any attempt to reconfigure this module through execute, collects the
     *         transaction's allowance-granting sub-calls, then snapshots the account's native balance
     *         and the raw balance of every watched token for postCheck to diff -- no oracle calls
     *         happen here.
     * @dev `msg.sender` is the installing account itself (it calls its installed hook directly).
     *      The two leading interface parameters (original caller and msg.value) are unused here.
     *      First rolls the spending window if it has expired. Only `execute` and
     *      `executeFromExecutor` calldata is decoded; any other calldata skips that but still gets a
     *      balance snapshot so postCheck can meter the outflow.
     *
     *      Order matters: the admin-surface check ({_isAdminCallDataExec}) runs BEFORE approvals are
     *      collected, so an execution that this hook is going to refuse never pays for the approval
     *      arrays or their oracle lookups. Both decode the same calldata independently, which is a
     *      deliberate trade — the duplicated decode costs on the order of 100 gas against preCheck's
     *      ~25k, and keeping the admin guard standalone keeps it separately readable and testable.
     *
     *      Allowance grants (`approve` / legacy `increaseAllowance`) are only RECORDED here, with
     *      unlimited and unpriced-to-untrusted ones rejected up front — postCheck reverts the whole
     *      transaction if any of them is left STANDING once the calls ran.
     * @param msgData The account's full outer calldata, from which the execution(s) are decoded.
     * @return hookData ABI-encoded snapshot handed straight back to postCheck: the account's native
     *         balance, every watched token's address and its raw balance, plus the (token, spender)
     *         pairs approved in this transaction.
     */
    function preCheck(address, uint256, bytes calldata msgData) external returns (bytes memory hookData) {
        address account = msg.sender;
        uint256 nativeTokenBalanceBefore = account.balance;
        Config storage config = _configs[account]; // loads the config of the specific account
        if (!config.installed) revert SpendingLimitModule_NotInstalled();
        _rollWindowIfNeeded(config);

        (bool isExecute, Mode mode, bytes calldata executionCalldata) = _decodeExecuteCalldata(msgData);

        if (_isAdminCallDataExec(isExecute, mode, executionCalldata)) revert SpendingLimitModule_AdminExecution();

        (address[] memory approvedTokens, address[] memory approvedSpenders) =
            _collectApprovals(account, isExecute, mode, executionCalldata);

        (address[] memory watchedTokens, uint256[] memory balancesBefore) = _watchedBalances(config, account);
        return abi.encode(nativeTokenBalanceBefore, watchedTokens, balancesBefore, approvedTokens, approvedSpenders);
    }

    /**
     * @notice Compares each watched token's current balance to its preCheck snapshot, pricing only
     *         the ones that changed to meter net spending, then reverts if any approval made in
     *         this transaction is still standing once the calls finish. Reverts the whole
     *         transaction if either the daily budget is breached or any approval survives
     *         execution unconsumed.
     * @dev `msg.sender` is the account. Two checks:
     *      (1) Net-value metering — for the account's native balance and each watched token whose
     *          balance differs from the preCheck snapshot, price the difference via the oracle (a
     *          balance that didn't move is never priced, so never touches the oracle at all); sum
     *          the signed differences across native + all watched tokens and add any net *decrease*
     *          to the window tally, reverting if that pushes past the daily limit, and emitting
     *          {SpendMetered} once it is known the spend stands. A net increase adds nothing, never
     *          banks credit for later, and emits nothing. Gas is excluded from the native delta:
     *          the ERC-4337 prefund is paid before preCheck and the refund returns to the EntryPoint
     *          deposit after postCheck, so the snapshot difference reflects only value the calls moved.
     *      (2) No standing approvals — any allowance approved in this transaction must be fully
     *          consumed (down to exactly 0) by the time the calls finish, or the whole transaction
     *          reverts. There is no size or budget threshold: this closes the deferred-pull risk
     *          (roadmap §2.4) outright rather than merely bounding it, and leaves no persistent state
     *          that could drift or be gamed by several small concurrent approvals. It never itself
     *          reduces spentInWindow.
     *      Silently returns if the module is not installed (e.g. uninstalled between the two hooks).
     * @param hookData The ABI-encoded snapshot from preCheck: (uint256 nativeTokenBalanceBefore,
     *        address[] watchedTokens, uint256[] balancesBefore, address[] approvedTokens,
     *        address[] approvedSpenders).
     */
    function postCheck(bytes calldata hookData) external {
        address account = msg.sender;

        Config storage config = _configs[account];
        if (!config.installed) return;

        (
            uint256 nativeTokenBalanceBefore,
            address[] memory watchedTokens,
            uint256[] memory balancesBefore,
            address[] memory approvedTokens,
            address[] memory approvedSpenders
        ) = abi.decode(hookData, (uint256, address[], uint256[], address[], address[]));

        // (1) Net-value spend: only price a watched token if its balance actually changed, then
        //     add any net USD decrease across all of them to the window tally.
        int256 netOutflowUsd;
        uint256 nativeTokenBalanceAfter = account.balance;
        SHOracle oracle = _getOracle();

        if (nativeTokenBalanceBefore > nativeTokenBalanceAfter) {
            netOutflowUsd += oracle.getPrice(address(0), nativeTokenBalanceBefore - nativeTokenBalanceAfter);
        } else if (nativeTokenBalanceBefore < nativeTokenBalanceAfter) {
            netOutflowUsd -= oracle.getPrice(address(0), nativeTokenBalanceAfter - nativeTokenBalanceBefore);
        }

        for (uint256 i = 0; i < watchedTokens.length; i++) {
            uint256 balanceAfter = IERC20Extended(watchedTokens[i]).balanceOf(account);
            if (balanceAfter == balancesBefore[i]) continue; // unchanged -- skip the oracle call entirely
            if (balanceAfter < balancesBefore[i]) {
                netOutflowUsd += oracle.getPrice(watchedTokens[i], balancesBefore[i] - balanceAfter);
            } else {
                netOutflowUsd -= oracle.getPrice(watchedTokens[i], balanceAfter - balancesBefore[i]);
            }
        }
        if (netOutflowUsd > 0) {
            // Summed at full width and checked against the limit BEFORE it is stored.
            int256 spent = int256(config.spentInWindow) + netOutflowUsd;
            if (spent > config.dailyLimitUsd) {
                revert SpendingLimitModule_BudgetExceeded(spent, config.dailyLimitUsd);
            }
            // Safe: 0 < spent <= dailyLimitUsd, and the limit is itself an int128.
            // forge-lint: disable-next-line(unsafe-typecast)
            config.spentInWindow = int128(spent);

            emit SpendMetered(account, netOutflowUsd, spent);
        }

        // (2) No standing approvals: any allowance approved in this transaction must be fully
        //     consumed (spent down to exactly 0) within that same transaction, or the whole
        //     transaction reverts. Size and remaining budget are irrelevant here — a standing
        //     allowance of any amount is disallowed outright.
        for (uint256 i = 0; i < approvedTokens.length; i++) {
            uint256 residual = IERC20Extended(approvedTokens[i]).allowance(account, approvedSpenders[i]);
            if (residual > 0) {
                revert SpendingLimitModule_StandingApprovalNotAllowed(approvedTokens[i], approvedSpenders[i], residual);
            }
        }
    }

    /*//////////////////////////////////////////////////////////////
                        PROMISE-CHECK HELPERS
    //////////////////////////////////////////////////////////////*/

    /**
     * @dev Scans the transaction's decoded calls for allowance-granting sub-calls (`approve` and
     *      legacy `increaseAllowance`) and returns the (token, spender) pairs so postCheck can later
     *      revert if anything is left STANDING on any of them.
     *      Rejects unlimited approvals, and unpriced-token approvals to UNTRUSTED spenders, up front
     *      (those never depend on the residual). Returns empty arrays for non-execute calldata,
     *      delegatecalls (no reliable selector), and transactions with no approvals.
     * @param account The installing account whose trusted-spender list gates unpriced approvals.
     * @param isExecute Whether msgData decoded to an execute/executeFromExecutor call worth scanning.
     * @param mode The ERC-7579 execution mode word (encodes single / batch / delegatecall).
     * @param executionCalldata The inner execution payload to decode per call type.
     * @return tokens The tokens approved in this transaction (parallel to `spenders`).
     * @return spenders The spender approved for each token.
     */
    function _collectApprovals(address account, bool isExecute, Mode mode, bytes calldata executionCalldata)
        internal
        view
        returns (address[] memory tokens, address[] memory spenders)
    {
        if (!isExecute) return (new address[](0), new address[](0));

        (CallType callType,,,) = ERC7579Utils.decodeMode(mode);

        if (callType == ERC7579Utils.CALLTYPE_SINGLE) {
            (address target,, bytes calldata callData) = ERC7579Utils.decodeSingle(executionCalldata);
            (bool isApproval, address spender) = _validateApproval(account, target, callData);
            if (!isApproval) return (new address[](0), new address[](0));
            tokens = new address[](1);
            spenders = new address[](1);
            // for a single call the target IS the token being approved
            tokens[0] = target;
            spenders[0] = spender;
        } else if (callType == ERC7579Utils.CALLTYPE_BATCH) {
            Execution[] calldata batch = ERC7579Utils.decodeBatch(executionCalldata);
            // First pass: count approvals with the CHEAP pure classifier (no oracle/allowlist reads)
            // so the arrays can be sized exactly. The count matches the second pass for any
            // transaction that doesn't revert: every non-zero, non-unlimited grant counted here is
            // one _validateApproval either records (priced token, OR trusted spender) or reverts the
            // whole transaction on (unpriced token to an untrusted spender) — so any survivor of the
            // transaction is always both counted and recorded.
            uint256 count;
            for (uint256 i = 0; i < batch.length; i++) {
                if (_isApproveCandidate(batch[i].callData)) count++;
            }
            tokens = new address[](count);
            spenders = new address[](count);
            // Second pass: the SINGLE full validation per approval (the only oracle isPriced call, and
            // the only place unlimited or unpriced-untrusted approvals revert).
            uint256 j;
            for (uint256 i = 0; i < batch.length; i++) {
                (bool isApproval, address spender) = _validateApproval(account, batch[i].target, batch[i].callData);
                if (isApproval) {
                    tokens[j] = batch[i].target;
                    spenders[j] = spender;
                    j++;
                }
            }
        }
        // CALLTYPE_DELEGATECALL: not decoded — returns empty arrays (see contract-level NatSpec).
    }

    /**
     * @dev Cheap, PURE pre-classifier used only to *count* batch approvals before allocating arrays.
     *      Mirrors _validateApproval's early-outs (selector, zero, unlimited) but deliberately omits
     *      the oracle `isPriced` call and the trusted-spender lookup, so the counting pass costs no
     *      external calls or storage reads. Its count agrees with _validateApproval's tracked-approval
     *      count on every transaction that does not revert: a candidate here (an `approve` or
     *      `increaseAllowance` selector with amount in (0, max)) is exactly what _validateApproval
     *      records, unless the token is unpriced AND its spender is untrusted — in which case
     *      _validateApproval reverts the whole transaction anyway, so the transient count mismatch is
     *      never observable. The two MUST recognize the same selector set for that to hold: adding a
     *      selector to one without the other would silently mis-size these arrays.
     * @param callData The sub-call's calldata, expected to start with a 4-byte selector.
     * @return True if this looks like a trackable non-zero, non-unlimited allowance grant.
     */
    function _isApproveCandidate(bytes calldata callData) internal pure returns (bool) {
        if (callData.length < 4) return false;
        if (
            bytes4(callData[:4]) != IERC20.approve.selector
                && bytes4(callData[:4]) != IERC20Extended.increaseAllowance.selector
        ) return false;
        (, uint256 amount) = abi.decode(callData[4:], (address, uint256));
        return amount != 0 && amount != type(uint256).max;
    }

    /**
     * @dev Classifies one sub-call: returns (true, spender) if it is a trackable non-zero allowance
     *      grant — `approve` or legacy `increaseAllowance`, which share the (address,uint256) shape so
     *      one decode covers both. Rejects unlimited (type(uint256).max) grants outright: those can
     *      never be consumed down to zero (and as an increaseAllowance DELTA, max would overflow in
     *      the token regardless). The token must be priced by the oracle UNLESS `spender` is on
     *      `account`'s trusted-spender list (e.g. the canonical DEX router): that exemption is what
     *      lets an LP-token approval through for removeLiquidity, since the no-standing-approval rule
     *      in postCheck still forces the allowance to exactly zero within the same transaction.
     *      Returns (false, ...) for zero-amount grants, non-approval calls, and calldata too short for
     *      a selector. Must recognize exactly the selectors {_isApproveCandidate} does — see there.
     * @param account The installing account whose trusted-spender list is consulted.
     * @param token The address the sub-call is aimed at (the token, for an approval).
     * @param callData The sub-call's calldata, expected to start with a 4-byte selector.
     * @return isApproval True if this is a trackable non-zero grant (priced token or trusted spender).
     * @return spender The approved spender (only meaningful when isApproval is true).
     */
    function _validateApproval(address account, address token, bytes calldata callData)
        internal
        view
        returns (bool isApproval, address spender)
    {
        if (callData.length < 4) return (false, address(0));
        if (
            bytes4(callData[:4]) != IERC20Extended.increaseAllowance.selector
                && bytes4(callData[:4]) != IERC20.approve.selector
        ) return (false, address(0));

        uint256 amount;
        (spender, amount) = abi.decode(callData[4:], (address, uint256));
        if (amount == 0) return (false, address(0)); // nothing granted; nothing to track
        if (amount == type(uint256).max) revert SpendingLimitModule_UnlimitedApprovalRejected();
        // Unpriced tokens are allowed ONLY when the spender is explicitly trusted; every other
        // approval must be on a token the oracle can price. `_getOracle()` sits inside the `&&` on
        // purpose: a trusted spender short-circuits it, so the common router-approval path costs no
        // registry read at all.
        if (!_isTrustedSpender[account][spender] && !_getOracle().isPriced(token)) {
            revert SpendingLimitModule_TokenNotPriced(token);
        }
        isApproval = true;
    }

    /**
     * @dev Reports whether this transaction's execution tries to reconfigure THIS module through the
     *      account's execute path — i.e. any sub-call whose target is the module itself (address(this))
     *      and whose selector is one of the config setters registered in `_isAdminSelector`. preCheck
     *      reverts when this returns true, so the module's own setters can never be driven via
     *      execute/executeFromExecutor. This is defense-in-depth that holds even on a host account
     *      without an admin-surface guard: the legitimate owner path reaches the setters by a DIRECT
     *      call to the module (msg.sender == account, never wrapped in execute, so this is never hit),
     *      whereas a session key can only reach the module through execute, which this blocks.
     *
     *      It blocks the OWNER's execute path too, and that is intentional rather than a limitation.
     *      By the time preCheck runs, msg.sender is the account whether the owner called execute
     *      directly or a session key drove it through the EntryPoint, so no owner/key distinction is
     *      available here — and inventing one (say, trusting an argument the account passes in) would
     *      hand the decision back to the very surface this is meant to protect. Nothing legitimate is
     *      lost: every selector registered here is reachable by the owner another way, the six config
     *      setters through the host's owner-only passthroughs (SessionHandler.setDailyLimit and
     *      friends) and onInstall/onUninstall through installModule/uninstallModule, whose outer
     *      calldata is not an execute selector and so never reaches this check.
     *
     *      Scope and limitations:
     *      - Only calls TARGETED at address(this) are flagged; a selector collision on some unrelated
     *        contract is not blocked, since target is checked alongside the selector.
     *      - It protects only the module's OWN surface. The account's admin surface (its config
     *        passthroughs, session management, module install/uninstall) is the host account's
     *        responsibility, not this hook's.
     *      - CALLTYPE_DELEGATECALL is not decoded (no reliable selector), matching _collectApprovals;
     *        a host that allows session-key delegatecalls must block that itself.
     * @param isExecute Whether msgData decoded to an execute/executeFromExecutor call worth scanning.
     * @param mode The ERC-7579 execution mode word (encodes single / batch / delegatecall).
     * @param executionCalldata The inner execution payload to decode per call type.
     * @return isAdminCall True if any decoded sub-call reconfigures this module; false otherwise.
     */
    function _isAdminCallDataExec(bool isExecute, Mode mode, bytes calldata executionCalldata)
        internal
        view
        returns (bool isAdminCall)
    {
        if (!isExecute) return false;

        (CallType callType,,,) = ERC7579Utils.decodeMode(mode);

        if (callType == ERC7579Utils.CALLTYPE_SINGLE) {
            (address target,, bytes calldata callData) = ERC7579Utils.decodeSingle(executionCalldata);

            return target == address(this) && callData.length >= 4 && _isAdminSelector[bytes4(callData[:4])];
        }

        if (callType == ERC7579Utils.CALLTYPE_BATCH) {
            Execution[] calldata batch = ERC7579Utils.decodeBatch(executionCalldata);

            for (uint256 i = 0; i < batch.length; i++) {
                if (
                    batch[i].target == address(this) && batch[i].callData.length >= 4
                        && _isAdminSelector[bytes4(batch[i].callData[:4])]
                ) return true;
            }
        }

        return false; // DELEGATECALL / anything else: not decoded, same limitation as _collectApprovals
    }

    /*//////////////////////////////////////////////////////////////
                             INTERNAL HELPERS
    //////////////////////////////////////////////////////////////*/

    /**
     * @dev Snapshots the account's raw balance of every watched token -- no oracle calls at all.
     *      Called once in preCheck (the "before" snapshot); postCheck compares each entry against
     *      the account's current balance and only prices (via the oracle) the tokens that actually
     *      changed, so a stale feed on an untouched watched token can never block this transaction.
     * @param config The account's config, supplying the watched-token list.
     * @param account The account whose balances are read.
     * @return tokens The watched tokens at the time of this snapshot (parallel to `balances`).
     * @return balances Each token's raw balance (in its own units) at the time of this snapshot.
     */
    function _watchedBalances(Config storage config, address account)
        internal
        view
        returns (address[] memory tokens, uint256[] memory balances)
    {
        uint256 length = config.watchedTokens.length;
        tokens = new address[](length);
        balances = new uint256[](length);
        for (uint256 i = 0; i < length; i++) {
            address token = config.watchedTokens[i];
            tokens[i] = token;
            balances[i] = IERC20Extended(token).balanceOf(account);
        }
    }

    /**
     * @dev Starts a fresh spending window if the current one has fully elapsed: moves windowStart
     *      to now and zeroes spentInWindow. This is what makes the cap a *rolling per-window*
     *      allowance rather than a lifetime one — each new window the account gets its full budget
     *      back, so nothing here needs to be reset by hand.
     * @param config The calling account's config, updated in place.
     */
    function _rollWindowIfNeeded(Config storage config) internal {
        // Widen to uint256 before adding so the window-end sum can never overflow the uint48 fields.
        if (block.timestamp >= uint256(config.windowStart) + uint256(config.windowDuration)) {
            // set the current time to the start of the new window (uint48 holds timestamps for millennia)
            config.windowStart = uint48(block.timestamp);
            config.spentInWindow = 0;
        }
    }

    /**
     * @dev "As of now" remaining budget, accounting for a window that has expired but not yet
     *      been touched by a state-changing preCheck call. Safe to use both mid-preCheck (after
     *      _rollWindowIfNeeded already ran, so this just reduces to the simple case) and from a
     *      pure external view call at any time.
     * @param config The account's config to read from.
     * @return The USD amount (18 decimals) the account may still spend in the current window; 0 if
     *         the limit is already reached.
     */
    function _remainingBudget(Config storage config) internal view returns (int256) {
        // Widen to uint256 before adding so the window-end sum can never overflow the uint48 fields.
        bool windowExpired = block.timestamp >= uint256(config.windowStart) + uint256(config.windowDuration);
        int256 spent = windowExpired ? int256(0) : int256(config.spentInWindow);
        return spent >= config.dailyLimitUsd ? int256(0) : config.dailyLimitUsd - spent;
    }

    /**
     * @dev Recovers (mode, executionCalldata) from the raw calldata of a call to
     *      IERC7579Execution.execute(bytes32,bytes) OR executeFromExecutor(bytes32,bytes) —
     *      both have the identical (mode, executionCalldata) shape, just different selectors.
     *      See contract-level NatSpec on why both must be recognized. Adapted from
     *      SpendingLimitModule's helper in session-key-infra (same calldata shape, same trick).
     * @param fullCalldata The account's raw outer calldata to inspect.
     * @return isExecute True if the calldata is an execute/executeFromExecutor call worth decoding;
     *         false (with empty outputs) for anything else or calldata too short to hold the args.
     * @return mode The ERC-7579 execution mode word (encodes single / batch / delegatecall).
     * @return executionCalldata The inner execution payload, to be further decoded per call type.
     */
    function _decodeExecuteCalldata(bytes calldata fullCalldata)
        internal
        pure
        returns (bool isExecute, Mode mode, bytes calldata executionCalldata)
    {
        if (fullCalldata.length < 68) {
            return (false, Mode.wrap(0), fullCalldata[0:0]);
        }
        bytes4 selector = bytes4(fullCalldata[:4]);
        if (
            selector != IERC7579Execution.execute.selector && selector != IERC7579Execution.executeFromExecutor.selector
        ) {
            return (false, Mode.wrap(0), fullCalldata[0:0]);
        }

        bytes calldata args = fullCalldata[4:];
        mode = Mode.wrap(bytes32(args[0:32]));
        uint256 dataOffset = uint256(bytes32(args[32:64]));
        uint256 dataLen = uint256(bytes32(args[dataOffset:dataOffset + 32]));
        executionCalldata = args[dataOffset + 32:dataOffset + 32 + dataLen];
        isExecute = true;
    }

    /**
     * @dev Resolves the protocol's CURRENT price oracle from the registry. Called at the point of use
     *      rather than cached in a local, so every call site pays for it only when it actually prices
     *      something: callers put it inside a short-circuiting `&&` or behind their early returns (see
     *      {_validateApproval} and {_addWatchedToken}) so a path that never needs a valuation makes no
     *      registry read at all. Re-reading it per use is cheap after the first — the registry account
     *      and its slot are warm for the rest of the transaction (~200 gas, not the 2.6k cold cost).
     * @return The SHOracle currently registered in SHRegistry. Can differ between transactions if the
     *         registry owner repoints it; the contract-level NatSpec covers what that implies.
     */
    function _getOracle() internal view returns (SHOracle) {
        return SHOracle(SHRegistry(REGISTRY).priceOracle());
    }

    /*//////////////////////////////////////////////////////////////
                             VIEW FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Returns the full stored config for an account.
     * @param account The account to look up.
     * @return The account's Config struct (install flag, limit, window, spend state, and
     *         watched-token list).
     */
    function getConfig(address account) external view returns (Config memory) {
        return _configs[account];
    }

    /**
     * @notice Reports whether a token is on an account's watched list.
     * @param account The account to look up.
     * @param token The token to check.
     * @return True if that token's value changes are metered for that account.
     */
    function isWatched(address account, address token) external view returns (bool) {
        return _isWatched[account][token];
    }

    /**
     * @notice Reports whether a spender is on an account's trusted-spender list.
     * @param account The account to look up.
     * @param spender The spender to check.
     * @return True if that spender may be approved even for an unpriced token on that account.
     */
    function isTrustedSpender(address account, address spender) external view returns (bool) {
        return _isTrustedSpender[account][spender];
    }

    /**
     * @notice Returns the account's remaining USD budget for the current (or about-to-roll) window.
     * @dev Reflects an expired-but-not-yet-rolled window as a full budget, matching what the next
     *      preCheck would see.
     * @param account The account to look up.
     * @return Remaining spendable USD (18 decimals); 0 if the limit is already reached.
     */
    function getRemainingBudget(address account) external view returns (int256) {
        return _remainingBudget(_configs[account]);
    }
}
