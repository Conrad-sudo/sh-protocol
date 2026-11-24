// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {
    IERC7579Module,
    IERC7579Validator,
    IERC7579Execution,
    IERC7579ModuleConfig,
    MODULE_TYPE_VALIDATOR,
    Execution
} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {PackedUserOperation, IAccountExecute} from "@openzeppelin/contracts/interfaces/draft-IERC4337.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {ERC4337Utils} from "@openzeppelin/contracts/account/utils/draft-ERC4337Utils.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {TransientSlot} from "@openzeppelin/contracts/utils/TransientSlot.sol";
import {SessionHandler} from "./SessionHandler.sol";
import {SpendingLimitModule} from "./SpendingLimitModule.sol";
import {IUserOpHooks} from "./interfaces/IUserOpHooks.sol";

/**
 * @title AgentModule
 * @author Conrad Japhet
 * @notice ERC-7579 VALIDATOR that lets a wallet's owner give a trading agent its own key and its own
 *         USD limit, so the agent can trade without the user approving each transaction. One
 *         contract per chain serves every wallet, and every SessionHandler gets it at deploy.
 * @dev The flow: the owner adds an agent (its address, a USD limit, an expiry) with a direct
 *      transaction. The agent's trades are then ordinary ERC-4337 UserOps from the wallet, sent by
 *      Mitfah's bundler exactly like a passkey's, with the gas paid by the wallet. An agent op names
 *      this module in its nonce key, its callData is `executeUserOp` around the wallet's own `execute`
 *      call, and its signature is the agent's, over the op's hash (see {hashOperation}). The agent
 *      holds its key.
 *        - Validation, {validateUserOp}, read-only: the agent's signature, then the three rules
 *          below, so an op that breaks one is refused before it costs the wallet any gas.
 *        - Execution: the wallet calls {preExecute}, runs the calls through its `execute` (its
 *          guard, the protocol fee and the spending-limit hook, as for a passkey op), then calls
 *          {postExecute}, which charges the agent's own limit.
 *      Everything else that binds a passkey op binds an agent op too: the wallet's per-op gas ceiling,
 *      its daily gas allowance (one allowance for the passkeys and the agents together, the user's
 *      choice), and its pause.
 *
 *      WHO CAN DO WHAT -- the property everything else rests on:
 *        - {addAgent} / {updateAgent}: ONLY the wallet's owner, calling this module DIRECTLY
 *          (msg.sender == account.owner(), owner != account). A passkey acts AS the wallet, so
 *          anything it routes through `execute` arrives with msg.sender == the wallet: it can neither
 *          add an agent nor raise one's limit or expiry.
 *        - {removeAgent}: the owner, OR the wallet itself, so the user can stop an agent with a
 *          passkey from their device. Removing only ever takes authority away. An agent cannot reach
 *          it: a call to this module from inside an agent's trade is refused.
 *        - A trade: only an op the agent signed, and only that exact op.
 *        - A message: never. {isValidSignatureWithSender} always answers no, so an agent can't sign
 *          for the wallet under ERC-1271. Such a signature (a Permit2 grant, an off-chain order) could
 *          move the wallet's tokens without passing its spending limit.
 *
 *      What an agent's trade may do. Anything a passkey may (the wallet's guard: not the wallet's own
 *      settings, not the limit module, not the EntryPoint, no delegatecall, the owner's allowlist when
 *      on), on any exchange or protocol (the user's call), with three rules of its own:
 *        1. Native ETH/BNB must be metered. An agent can't be added, and none of a wallet's agents can
 *           trade, while the wallet's limit leaves native value out ({SpendingLimitModule} meters it
 *           only by choice). Otherwise native value could leave without limit.
 *        2. No transfer or approval of a token the USD limit doesn't count. A call to a contract that
 *           is not on the wallet's watched list, with any of the token-moving functions in
 *           {_movesTokens}, is refused. The limit can't see such a token leave, so the agent may not
 *           send it or let anyone pull it. Tokens capped in their own units are not on the watched
 *           list, so they are refused too: the agent's limit is in USD and can't see them. The check
 *           goes by function name, like the limit module's own approval rules, so a token that moves
 *           under a name on no list is not caught (THREAT_MODEL §3.17).
 *        3. No call to this module, so an agent can't remove, add or raise itself or another agent,
 *           or touch the bookkeeping {preExecute} and {postExecute} keep.
 *
 *      The agent's own limit. The wallet's spending-limit hook already measures, per transaction, the
 *      net USD value that left the wallet. {preExecute} reads the wallet's remaining budget ("as of
 *      now", which already accounts for a window that has run out) and {postExecute} reads it again;
 *      the difference is exactly what the trade cost the wallet's limit. It adds the trade's protocol
 *      fee, which the wallet's limit doesn't count, in USD at the oracle's native price: without that,
 *      a leaked agent key could drain the wallet's ETH in fees with empty trades. Gas is NOT counted,
 *      as it isn't for a passkey; the wallet's gas ceilings bound it instead. The agent's limit runs on
 *      the wallet's own window and starts over when that window does. Over either limit, the whole
 *      trade reverts -- in execution, since what a trade costs is only known once it has run, so the
 *      wallet still pays that op's gas. Mitfah simulates every op before sending it.
 *
 *      Signatures. EIP-712 over (the op's hash, a deadline, the agent's epoch), bound to this
 *      contract and chain by the domain. The op's hash covers everything in the op -- the wallet, the
 *      chain, the EntryPoint, the nonce, the calls and the gas fields -- and the EntryPoint's nonce makes
 *      each op usable once. The deadline makes an op that was signed but never sent die minutes later.
 *      The epoch changes each time an agent is added, so ops signed before a removal die with it, even
 *      if the same address is added again.
 *
 *      Uninstalling this module removes every agent of the wallet.
 */
contract AgentModule is IERC7579Validator, IUserOpHooks, EIP712 {
    using TransientSlot for *;

    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when the caller may not do this for `account`: not its owner (or, for
    ///      {removeAgent}, not the wallet itself), or the wallet owns itself.
    error AgentModule_NotOwner(address account, address caller);
    /// @dev Thrown by {addAgent} when this module is not installed on `account` as a validator.
    error AgentModule_NotInstalled(address account);
    /// @dev Thrown when the wallet's spending limit leaves native ETH/BNB out (rule 1).
    error AgentModule_NativeNotMetered(address account);
    /// @dev Thrown on a zero agent address, the wallet itself, or this module.
    error AgentModule_InvalidAgent(address agent);
    /// @dev Thrown on a zero limit.
    error AgentModule_InvalidLimit();
    /// @dev Thrown when an expiry is not in the future or is more than {MAX_VALIDITY} away.
    error AgentModule_InvalidExpiry(uint48 validUntil, uint256 latest);
    /// @dev Thrown by {addAgent} when the wallet already has {MAX_AGENTS} agents.
    error AgentModule_TooManyAgents(address account);
    /// @dev Thrown by {addAgent} when `agent` is already one of the wallet's agents.
    error AgentModule_AlreadyAdded(address account, address agent);
    /// @dev Thrown when `agent` is not one of the wallet's agents. In {preExecute}, also when it was
    ///      removed and added again after the op was signed (another epoch).
    error AgentModule_UnknownAgent(address account, address agent);
    /// @dev Thrown by {validateUserOp} when the op's callData is not `executeUserOp` around the
    ///      wallet's `execute`: the one shape that makes the wallet call {preExecute} and {postExecute}.
    error AgentModule_NotAnAgentOp();
    /// @dev Thrown on any mode but a plain single call or a plain batch.
    error AgentModule_UnsupportedMode(bytes32 mode);
    /// @dev Thrown when a call in an agent's trade targets this module (rule 3).
    error AgentModule_TargetNotAllowed(address target);
    /// @dev Thrown when a call in an agent's trade would move or approve a token the wallet's USD
    ///      limit doesn't count (rule 2).
    /// @param token    The call's target.
    /// @param selector The token-moving function it called.
    error AgentModule_UncountedToken(address token, bytes4 selector);
    /// @dev Thrown when a trade would take the agent past its own limit for the window.
    /// @param spentUsd The agent's spend in the window had the trade gone through (18 decimals).
    /// @param limitUsd The agent's limit (18 decimals).
    error AgentModule_AgentLimitExceeded(uint256 spentUsd, uint256 limitUsd);
    /// @dev Thrown by {postExecute} when the calling wallet has no {preExecute} open.
    error AgentModule_NoTradeOpen(address account);

    /*//////////////////////////////////////////////////////////////
                                 EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when the owner adds an agent.
    /// @param limitUsd   USD the agent may spend per window of the wallet's limit (18 decimals).
    /// @param validUntil When the agent stops working.
    /// @param epoch      The epoch the agent's signatures must name.
    event AgentAdded(address indexed account, address indexed agent, uint256 limitUsd, uint48 validUntil, uint32 epoch);

    /// @notice Emitted when the owner changes an agent's limit or expiry.
    event AgentUpdated(address indexed account, address indexed agent, uint256 limitUsd, uint48 validUntil);

    /// @notice Emitted when an agent is removed.
    /// @param by The owner, or the wallet itself (a passkey).
    event AgentRemoved(address indexed account, address indexed agent, address by);

    /// @notice Emitted when the wallet uninstalls this module, removing all of its agents.
    event AgentsCleared(address indexed account);

    /// @notice Emitted for every agent trade, so the user can be told of each one.
    /// @param userOpHash The op's hash: the EntryPoint's UserOperationEvent carries the same one.
    /// @param chargedUsd What the trade cost the wallet's USD limit (18 decimals).
    /// @param feeUsd     The trade's protocol fee in USD, charged to the agent's limit only.
    /// @param spentUsd   The agent's spend in the window after this trade.
    event AgentExecuted(
        address indexed account,
        address indexed agent,
        bytes32 indexed userOpHash,
        uint256 chargedUsd,
        uint256 feeUsd,
        uint256 spentUsd
    );

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice The most agents one wallet may have at once.
    uint256 public constant MAX_AGENTS = 3;

    /// @notice The furthest ahead an agent's expiry may be set, counted from the moment it is set.
    uint256 public constant MAX_VALIDITY = 90 days;

    /// @notice The EIP-712 type an agent signs for each op.
    bytes32 public constant OPERATION_TYPEHASH =
        keccak256("AgentOperation(bytes32 userOpHash,uint48 deadline,uint32 epoch)");

    /// @notice The length of an agent op's signature: agent (20 bytes) ‖ epoch (4) ‖ deadline (6) ‖
    ///         r (32) ‖ s (32) ‖ v (1). The wallet sends an op here by its signature's length: neither
    ///         the owner's 65 bytes nor a passkey's 256 or more.
    /// @dev The agent and the epoch ride along, though both are known once the signature is checked,
    ///      so {preExecute} and {postExecute} can read them without recovering the signer again.
    uint256 public constant SIGNATURE_LENGTH = 95;

    /// @dev ERC-7579 modes an agent may use: one plain call, or a plain batch; both revert as a whole
    ///      on any failure. CALLTYPE_SINGLE / CALLTYPE_BATCH in the first byte, EXECTYPE_DEFAULT and
    ///      no selector or payload after it.
    bytes32 private constant _SINGLE_MODE = bytes32(0);
    bytes32 private constant _BATCH_MODE = bytes32(uint256(0x01) << 248);

    /// @dev Base of the TRANSIENT slot that carries the wallet's remaining budget from {preExecute} to
    ///      {postExecute}, keyed by the wallet: keccak256(abi.encode(account, _REMAINING_BEFORE)).
    ///      Never in storage.
    bytes32 private constant _REMAINING_BEFORE = keccak256("mitfah.AgentModule.remainingBefore");

    /**
     * @notice One agent of one wallet. Two slots.
     * @param limitUsd    USD the agent may spend per window of the wallet's limit (18 decimals).
     * @param spentUsd    What it spent in the window that started at `windowStart`.
     * @param validUntil  When it stops working.
     * @param windowStart The wallet's window start when `spentUsd` was last written. A different
     *                    window now means `spentUsd` belongs to an earlier one and counts as 0.
     * @param epoch       The epoch its signatures must name; set when it was added. Never 0.
     * @param position    1-based place in the wallet's agent list; 0 marks no agent.
     */
    struct Agent {
        uint128 limitUsd;
        uint128 spentUsd;
        uint48 validUntil;
        uint32 windowStart;
        uint32 epoch;
        uint8 position;
    }

    /// @dev account => agent => its terms and spend.
    mapping(address account => mapping(address agent => Agent)) private _agents;

    /// @dev account => its agents, at most {MAX_AGENTS}.
    mapping(address account => address[]) private _agentList;

    /// @dev account => agent => the last epoch handed out. Never reset, removals and uninstalls
    ///      included, so a re-added agent never gets an epoch it had before.
    mapping(address account => mapping(address agent => uint32)) private _lastEpoch;

    constructor() EIP712("AgentModule", "1") {}

    /*//////////////////////////////////////////////////////////////
                           ERC-7579 MODULE
    //////////////////////////////////////////////////////////////*/

    /// @inheritdoc IERC7579Module
    /// @dev Takes nothing, on purpose: anything acting as the wallet (a passkey through `execute`, an
    ///      executor) can call a module's onInstall directly, so it must not be able to add an agent.
    ///      Agents are added by the owner with {addAgent}.
    function onInstall(bytes calldata) external override {}

    /// @inheritdoc IERC7579Module
    /// @dev Removes every agent of the calling wallet (at most {MAX_AGENTS}). Keyed by msg.sender, so a
    ///      direct call from anyone else only touches that caller's own agents; a direct call from the
    ///      wallet itself (a passkey) stops its agents without uninstalling, which only takes authority
    ///      away. Never reverts, so it can't block an uninstall.
    function onUninstall(bytes calldata) external override {
        address[] storage list = _agentList[msg.sender];
        for (uint256 i = 0; i < list.length; i++) {
            delete _agents[msg.sender][list[i]];
        }
        delete _agentList[msg.sender];
        emit AgentsCleared(msg.sender);
    }

    /// @inheritdoc IERC7579Module
    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_VALIDATOR;
    }

    /*//////////////////////////////////////////////////////////////
                            OWNER: AGENTS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Gives `account` a new agent. Only the wallet's owner, calling directly.
     * @dev Refused unless this module is installed on the wallet and the wallet's limit meters native
     *      ETH/BNB (rule 1); the web app turns metering on first when it is off.
     * @param account    The SessionHandler wallet the agent will trade from.
     * @param agent      The agent's address: the key it signs ops with. Not zero, not the wallet, not
     *                   this module.
     * @param limitUsd   USD it may spend per window of the wallet's limit (18 decimals). Non-zero. The
     *                   wallet's own limit binds too, whichever is lower.
     * @param validUntil When it stops working: in the future, at most {MAX_VALIDITY} from now.
     */
    function addAgent(address account, address agent, uint128 limitUsd, uint48 validUntil) external {
        _requireOwner(account);
        if (!IERC7579ModuleConfig(account).isModuleInstalled(MODULE_TYPE_VALIDATOR, address(this), "")) {
            revert AgentModule_NotInstalled(account);
        }
        if (!SessionHandler(payable(account)).getConfig().nativeMetered) revert AgentModule_NativeNotMetered(account);
        if (agent == address(0) || agent == account || agent == address(this)) revert AgentModule_InvalidAgent(agent);
        _checkTerms(limitUsd, validUntil);
        if (_agents[account][agent].position != 0) revert AgentModule_AlreadyAdded(account, agent);

        address[] storage list = _agentList[account];
        if (list.length >= MAX_AGENTS) revert AgentModule_TooManyAgents(account);
        list.push(agent);

        uint32 epoch = ++_lastEpoch[account][agent];
        _agents[account][agent] = Agent({
            limitUsd: limitUsd,
            spentUsd: 0,
            validUntil: validUntil,
            windowStart: 0,
            epoch: epoch,
            position: uint8(list.length)
        });
        emit AgentAdded(account, agent, limitUsd, validUntil, epoch);
    }

    /**
     * @notice Changes an agent's limit and expiry. Only the wallet's owner, calling directly.
     * @dev What it already spent this window stays, and so do its signatures (same epoch). A limit
     *      below what it already spent stops it until the window starts over.
     * @param account    The wallet.
     * @param agent      One of its agents.
     * @param limitUsd   The new limit (18 decimals). Non-zero.
     * @param validUntil The new expiry: in the future, at most {MAX_VALIDITY} from now.
     */
    function updateAgent(address account, address agent, uint128 limitUsd, uint48 validUntil) external {
        _requireOwner(account);
        Agent storage info = _requireAgent(account, agent);
        _checkTerms(limitUsd, validUntil);
        info.limitUsd = limitUsd;
        info.validUntil = validUntil;
        emit AgentUpdated(account, agent, limitUsd, validUntil);
    }

    /**
     * @notice Removes an agent at once. The wallet's owner calling directly, or the wallet itself (a
     *         passkey through `execute`).
     * @dev The wallet itself may only remove, never add or raise, so a passkey can stop an agent
     *      without the owner wallet. An agent's own trade can't call this: rule 3. An op the agent
     *      signed that has not run yet fails when it does ({preExecute}), even within the same bundle.
     * @param account The wallet.
     * @param agent   One of its agents.
     */
    function removeAgent(address account, address agent) external {
        if (msg.sender != account) _requireOwner(account);
        uint256 position = _requireAgent(account, agent).position;

        // Swap and pop: the last agent takes the removed one's place.
        address[] storage list = _agentList[account];
        address last = list[list.length - 1];
        if (last != agent) {
            list[position - 1] = last;
            // Safe: a position is at most MAX_AGENTS.
            // forge-lint: disable-next-line(unsafe-typecast)
            _agents[account][last].position = uint8(position);
        }
        list.pop();
        delete _agents[account][agent];
        emit AgentRemoved(account, agent, msg.sender);
    }

    /*//////////////////////////////////////////////////////////////
                           AGENT OPS: VALIDATION
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Validates an agent's op for the calling wallet: the agent's signature, then rules 1-3.
     * @dev Called by the wallet, read-only, during the EntryPoint's validation of the op, with the
     *      wallet as msg.sender. The signature is `agent ‖ epoch ‖ deadline ‖ r ‖ s ‖ v` (see
     *      {SIGNATURE_LENGTH}), over {hashOperation}. Anything wrong with it -- its length, an unknown
     *      agent, another epoch, a zero deadline, another signer -- returns SIG_VALIDATION_FAILED, as
     *      ERC-4337 asks, never a revert. A signed op that breaks a rule REVERTS, with the rule's
     *      error, which the EntryPoint passes on (`AA23 reverted`) so the cause is visible in a
     *      simulation.
     * @dev The op runs only until the earlier of its deadline and the agent's expiry: returned as the
     *      op's validity window, which the EntryPoint enforces. A zero deadline is refused rather than
     *      returned, since the EntryPoint would read it as "valid forever".
     * @dev The callData must be `executeUserOp` (selector only) followed by a call to the wallet's
     *      `execute`. That is the shape that makes the wallet call {preExecute} and {postExecute}; any
     *      other would run the calls without charging the agent. The wallet refuses other shapes too.
     *      The inner call is decoded here exactly as the wallet and its hook decode it, so the calls
     *      checked are the calls that run.
     * @param userOp     The op.
     * @param userOpHash Its hash, as the EntryPoint computed it.
     * @return validationData SIG_VALIDATION_FAILED, or success with the op's validity window.
     */
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash)
        external
        view
        override
        returns (uint256)
    {
        address account = msg.sender;
        bytes calldata signature = userOp.signature;
        if (signature.length != SIGNATURE_LENGTH) return ERC4337Utils.SIG_VALIDATION_FAILED;
        address agent = address(bytes20(signature[0:20]));
        uint32 epoch = uint32(bytes4(signature[20:24]));
        uint48 deadline = uint48(bytes6(signature[24:30]));

        Agent storage info = _agents[account][agent];
        // An unknown agent reads all zero, and no agent ever has epoch 0.
        if (info.position == 0 || info.epoch != epoch || deadline == 0) return ERC4337Utils.SIG_VALIDATION_FAILED;
        (address signer, ECDSA.RecoverError error,) =
            ECDSA.tryRecoverCalldata(hashOperation(userOpHash, deadline, epoch), signature[30:]);
        if (error != ECDSA.RecoverError.NoError || signer != agent) return ERC4337Utils.SIG_VALIDATION_FAILED;

        (bytes32 mode, bytes calldata executionCalldata) = _decodeOp(userOp.callData);
        SpendingLimitModule.Config memory config = SessionHandler(payable(account)).getConfig();
        if (!config.nativeMetered) revert AgentModule_NativeNotMetered(account);
        _checkCalls(mode, executionCalldata, config.watchedTokens);

        uint48 validUntil = info.validUntil;
        return ERC4337Utils.packValidationData(true, 0, deadline < validUntil ? deadline : validUntil);
    }

    /// @inheritdoc IERC7579Validator
    /// @dev Always "invalid": an agent never signs messages for the wallet. ERC-1271 signatures can
    ///      authorize token movements (Permit2, off-chain orders) that would never pass the wallet's
    ///      spending limit or this module's rules.
    function isValidSignatureWithSender(address, bytes32, bytes calldata) external pure override returns (bytes4) {
        return 0xffffffff;
    }

    /*//////////////////////////////////////////////////////////////
                           AGENT OPS: EXECUTION
    //////////////////////////////////////////////////////////////*/

    /**
     * @inheritdoc IUserOpHooks
     * @dev Called by the wallet just before an agent op's calls run. Re-checks the agent: every op in
     *      a bundle is validated before any of them runs, so an op earlier in the same bundle (a
     *      passkey removing the agent) may have changed what validation saw. Removal and a fresh add
     *      (another epoch) are what can change; expiry needs no second look, since validation and
     *      execution share one block and an expiry can only ever be moved to a later moment. Then
     *      notes the wallet's remaining budget for {postExecute}, in transient storage keyed by the
     *      wallet.
     * @dev Keyed by msg.sender, so a direct call from anyone else only writes that caller's own note.
     *      The wallet itself can call this outside an agent op (a passkey through `execute`); that can
     *      only ever ADD to an agent's spend, never take any off (see {postExecute}). An agent can't
     *      call it: rule 3.
     */
    function preExecute(PackedUserOperation calldata userOp, bytes32) external override {
        address account = msg.sender;
        bytes calldata signature = userOp.signature;
        address agent = address(bytes20(signature[0:20]));
        Agent storage info = _agents[account][agent];
        if (info.position == 0 || info.epoch != uint32(bytes4(signature[20:24]))) {
            revert AgentModule_UnknownAgent(account, agent);
        }

        // +1 so that 0 keeps meaning "no trade open", even with nothing left in the budget.
        _remainingBefore(account).tstore(SafeCast.toUint256(SessionHandler(payable(account)).getRemainingBudget()) + 1);
    }

    /**
     * @inheritdoc IUserOpHooks
     * @dev Called by the wallet just after an agent op's calls ran. Charges the agent what the calls
     *      cost the wallet's limit -- the drop in its remaining budget since {preExecute} -- plus the
     *      op's protocol fee in USD, reverting past the agent's limit, and emits {AgentExecuted}.
     * @dev The drop can't be negative: the wallet's spend only rises within a transaction and its
     *      limit can't change inside one (a rise would revert here rather than credit the agent). With
     *      the agent's earlier spend in this window only ever added to, nothing that calls this --
     *      the wallet itself included -- can lower what an agent has spent.
     * @dev The window is the one the wallet's hook just spent in: its preCheck starts a new one when the
     *      last has run out, so the wallet's windowStart after the calls is always it.
     */
    function postExecute(PackedUserOperation calldata userOp, bytes32 userOpHash) external override {
        address account = msg.sender;
        TransientSlot.Uint256Slot slot = _remainingBefore(account);
        uint256 remainingBefore = slot.tload();
        if (remainingBefore == 0) revert AgentModule_NoTradeOpen(account);
        slot.tstore(0);

        address agent = address(bytes20(userOp.signature[0:20]));
        Agent storage info = _requireAgent(account, agent);
        SessionHandler wallet = SessionHandler(payable(account));
        uint256 chargedUsd = remainingBefore - 1 - SafeCast.toUint256(wallet.getRemainingBudget());
        uint256 feeUsd = _feeUsd(wallet);
        uint32 window = wallet.getConfig().windowStart;

        uint256 spentUsd = (info.windowStart == window ? uint256(info.spentUsd) : 0) + chargedUsd + feeUsd;
        if (spentUsd > info.limitUsd) revert AgentModule_AgentLimitExceeded(spentUsd, info.limitUsd);
        // Safe: spentUsd <= limitUsd, a uint128.
        // forge-lint: disable-next-line(unsafe-typecast)
        info.spentUsd = uint128(spentUsd);
        info.windowStart = window;

        emit AgentExecuted(account, agent, userOpHash, chargedUsd, feeUsd, spentUsd);
    }

    /*//////////////////////////////////////////////////////////////
                                 VIEWS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The EIP-712 digest an agent signs for one op.
     * @param userOpHash The op's hash: EntryPoint.getUserOpHash(op).
     * @param deadline   The last moment the op may run. Non-zero.
     * @param epoch      The agent's current epoch, from {getAgent}.
     */
    function hashOperation(bytes32 userOpHash, uint48 deadline, uint32 epoch) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(OPERATION_TYPEHASH, userOpHash, deadline, epoch)));
    }

    /// @notice The wallet's agents, in no particular order.
    function getAgents(address account) external view returns (address[] memory) {
        return _agentList[account];
    }

    /**
     * @notice One agent's terms, and what it has spent and has left in the wallet's current window.
     * @return info         The stored record (all zero when it isn't one of the wallet's agents).
     * @return spentUsd     What it spent in the current window (0 once the window has run out).
     * @return remainingUsd What it may still spend this window, before the wallet's own limit.
     * @return active       True while it is an agent and has not expired.
     */
    function getAgent(address account, address agent)
        external
        view
        returns (Agent memory info, uint256 spentUsd, uint256 remainingUsd, bool active)
    {
        info = _agents[account][agent];
        if (info.position == 0) return (info, 0, 0, false);
        SpendingLimitModule.Config memory config = SessionHandler(payable(account)).getConfig();
        bool windowOver = block.timestamp >= uint256(config.windowStart) + uint256(config.windowDuration);
        spentUsd = !windowOver && info.windowStart == config.windowStart ? info.spentUsd : 0;
        remainingUsd = spentUsd >= info.limitUsd ? 0 : info.limitUsd - spentUsd;
        active = block.timestamp <= info.validUntil;
    }

    /*//////////////////////////////////////////////////////////////
                                INTERNAL
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts unless the caller is `account`'s owner and that owner is not the wallet itself.
    ///      The second half matters: a wallet that owns itself would let a passkey, acting as the
    ///      wallet, pass the first half.
    function _requireOwner(address account) private view {
        address owner = Ownable(account).owner();
        if (msg.sender != owner || owner == account) revert AgentModule_NotOwner(account, msg.sender);
    }

    /// @dev Returns the agent's record, reverting unless it is one of the wallet's agents.
    function _requireAgent(address account, address agent) private view returns (Agent storage info) {
        info = _agents[account][agent];
        if (info.position == 0) revert AgentModule_UnknownAgent(account, agent);
    }

    /// @dev A non-zero limit and an expiry in (now, now + MAX_VALIDITY].
    function _checkTerms(uint128 limitUsd, uint48 validUntil) private view {
        if (limitUsd == 0) revert AgentModule_InvalidLimit();
        uint256 latest = block.timestamp + MAX_VALIDITY;
        if (validUntil <= block.timestamp || validUntil > latest) revert AgentModule_InvalidExpiry(validUntil, latest);
    }

    /// @dev The transient slot holding `account`'s remaining budget (+1) between {preExecute} and
    ///      {postExecute}.
    function _remainingBefore(address account) private pure returns (TransientSlot.Uint256Slot) {
        return keccak256(abi.encode(account, _REMAINING_BEFORE)).asUint256();
    }

    /**
     * @dev Reads (mode, executionCalldata) out of an agent op's callData, which must be
     *      `executeUserOp.selector ‖ execute.selector ‖ abi.encode(mode, executionCalldata)`, and
     *      refuses any mode but a plain single call or a plain batch.
     * @dev Follows the ABI offset exactly as Solidity's decoder does in the wallet's `execute`, and as
     *      the spending-limit hook's `_decodeExecuteCalldata` does in its preCheck, so the calls checked
     *      here are byte for byte the calls that run. An encoding this can't read reverts (an
     *      out-of-range slice); one that only the wallet's decoder refuses fails the op in execution.
     */
    function _decodeOp(bytes calldata callData) private pure returns (bytes32 mode, bytes calldata executionCalldata) {
        if (
            callData.length < 8 || bytes4(callData[:4]) != IAccountExecute.executeUserOp.selector
                || bytes4(callData[4:8]) != IERC7579Execution.execute.selector
        ) revert AgentModule_NotAnAgentOp();
        bytes calldata args = callData[8:];
        mode = bytes32(args[0:32]);
        if (mode != _SINGLE_MODE && mode != _BATCH_MODE) revert AgentModule_UnsupportedMode(mode);
        uint256 offset = uint256(bytes32(args[32:64]));
        uint256 length = uint256(bytes32(args[offset:offset + 32]));
        executionCalldata = args[offset + 32:offset + 32 + length];
    }

    /// @dev Rules 2 and 3 for every call of the trade. `mode` is already one of the two allowed.
    function _checkCalls(bytes32 mode, bytes calldata executionCalldata, address[] memory counted) private view {
        if (mode == _SINGLE_MODE) {
            (address target,, bytes calldata callData) = ERC7579Utils.decodeSingle(executionCalldata);
            _checkCall(target, callData, counted);
        } else {
            Execution[] calldata batch = ERC7579Utils.decodeBatch(executionCalldata);
            for (uint256 i = 0; i < batch.length; i++) {
                _checkCall(batch[i].target, batch[i].callData, counted);
            }
        }
    }

    /// @dev Refuses a call to this module (rule 3), and a token-moving call to anything the wallet's
    ///      USD limit doesn't count (rule 2).
    function _checkCall(address target, bytes calldata callData, address[] memory counted) private view {
        if (target == address(this)) revert AgentModule_TargetNotAllowed(target);
        if (callData.length < 4) return;
        bytes4 selector = bytes4(callData[:4]);
        if (!_movesTokens(selector)) return;
        for (uint256 i = 0; i < counted.length; i++) {
            if (counted[i] == target) return;
        }
        revert AgentModule_UncountedToken(target, selector);
    }

    /**
     * @dev Whether `selector` sends a token held by the caller, or lets someone else take it: the
     *      standard and common names across ERC-20, ERC-721, ERC-1155, ERC-1363 / ERC-677, ERC-777,
     *      ERC-6909, and DSToken's (DAI, MKR) `push` and `move`. The grants SpendingLimitModule can't
     *      track (approveFor, addAllowanceIfNeeded, the 3-argument approveAndCall, paidCall) are
     *      refused by its hook on every call, so they aren't repeated here. `permit` needs the
     *      wallet's own signature, which an agent can't make ({isValidSignatureWithSender}).
     */
    function _movesTokens(bytes4 selector) private pure returns (bool) {
        return selector == 0xa9059cbb // transfer(address,uint256)
            || selector == 0x23b872dd // transferFrom(address,address,uint256): ERC-20 and ERC-721
            || selector == 0x095ea7b3 // approve(address,uint256): ERC-20 and ERC-721
            || selector == 0x39509351 // increaseAllowance(address,uint256)
            || selector == 0xd73dd623 // increaseApproval(address,uint256)
            || selector == 0x42842e0e // safeTransferFrom(address,address,uint256)
            || selector == 0xb88d4fde // safeTransferFrom(address,address,uint256,bytes)
            || selector == 0xa22cb465 // setApprovalForAll(address,bool): ERC-721 and ERC-1155
            || selector == 0xf242432a // safeTransferFrom(address,address,uint256,uint256,bytes)
            || selector == 0x2eb2c2d6 // safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)
            || selector == 0x1296ee62 // transferAndCall(address,uint256)
            || selector == 0x4000aea0 // transferAndCall(address,uint256,bytes)
            || selector == 0xd8fbe994 // transferFromAndCall(address,address,uint256)
            || selector == 0xc1d34b89 // transferFromAndCall(address,address,uint256,bytes)
            || selector == 0x3177029f // approveAndCall(address,uint256)
            || selector == 0x9bd9bbc6 // send(address,uint256,bytes)
            || selector == 0x62ad1b83 // operatorSend(address,address,uint256,bytes,bytes)
            || selector == 0x959b8c3f // authorizeOperator(address)
            || selector == 0x095bcdb6 // transfer(address,uint256,uint256): ERC-6909
            || selector == 0xfe99049a // transferFrom(address,address,uint256,uint256): ERC-6909
            || selector == 0x426a8493 // approve(address,uint256,uint256): ERC-6909
            || selector == 0x558a7297 // setOperator(address,bool): ERC-6909
            || selector == 0xb753a98c // push(address,uint256): DSToken
            || selector == 0xbb35783b; // move(address,address,uint256): DSToken
    }

    /// @dev The protocol fee the wallet pays for an agent op (in wei, read where the wallet reads it),
    ///      in USD at the oracle's native price. Zero when the fee is. No keeper fee: the wallet pays
    ///      its own gas, as for a passkey op.
    function _feeUsd(SessionHandler wallet) private view returns (uint256) {
        (, uint256 fee,) = wallet.REGISTRY().feeConfig();
        if (fee == 0) return 0;
        return SafeCast.toUint256(wallet.getUsdValue(address(0), fee));
    }
}
