// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {
    IERC7579Module,
    IERC7579Execution,
    IERC7579ModuleConfig,
    MODULE_TYPE_EXECUTOR,
    Execution
} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {SessionHandler} from "./SessionHandler.sol";
import {SpendingLimitModule} from "./SpendingLimitModule.sol";

/**
 * @title AgentModule
 * @author Conrad Japhet
 * @notice ERC-7579 EXECUTOR that lets a wallet's owner give a trading agent its own key and its own
 *         USD limit, so the agent can trade without the user approving each transaction. One
 *         contract per chain serves every wallet; an owner installs it once on their wallet.
 * @dev The flow: the owner installs this module and adds an agent (its address, a USD limit, an
 *      expiry) with direct transactions. The agent signs each trade (EIP-712) with its own
 *      secp256k1 key, which it holds and Mitfah never sees. Anyone may submit a signed trade (Mitfah's
 *      backend normally; the agent itself also can, without a signature). This module checks its
 *      rules, then calls the wallet's `executeFromExecutor`, where the wallet runs its session guard,
 *      takes its fee (protocol fee plus keeper fee) and runs the calls through its spending-limit
 *      hook, exactly as for a recurring payment. Afterwards it charges the agent's own limit with
 *      what the trade cost the wallet's limit, plus the fee.
 *
 *      WHO CAN DO WHAT -- the property everything else rests on:
 *        - {addAgent} / {updateAgent}: ONLY the wallet's owner, calling this module DIRECTLY
 *          (msg.sender == account.owner(), owner != account). A passkey acts AS the wallet, so
 *          anything it routes through `execute` arrives with msg.sender == the wallet: it can neither
 *          add an agent nor raise one's limit or expiry.
 *        - {removeAgent}: the owner, OR the wallet itself, so the user can stop an agent with a
 *          passkey from their phone. Removing only ever takes authority away. An agent cannot reach
 *          it: a call to this module from inside an agent's trade is refused.
 *        - {execute}: any caller, with the agent's signature over exactly this trade; or the agent
 *          itself. The caller chooses nothing the agent did not sign.
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
 *        3. No call to this module, so an agent can't remove, add or raise itself or another agent.
 *
 *      The agent's own limit. The wallet's spending-limit hook already measures, per transaction, the
 *      net USD value that left the wallet. This module reads the wallet's remaining budget ("as of
 *      now", which already accounts for a window that has run out) before and after the calls; the
 *      difference is exactly what this trade cost the wallet's limit. It adds the trade's fee, which
 *      the wallet's limit doesn't count, in USD at the oracle's native price: without that, a leaked
 *      agent key could drain the wallet's ETH in fees by submitting empty trades. The agent's limit
 *      runs on the wallet's own window and starts over when that window does. Over either limit, the
 *      whole trade reverts.
 *
 *      Signatures. EIP-712 over (account, mode, the calls, nonce, deadline, epoch), bound to this
 *      contract and chain by the domain. Nonces are a bitmap per agent, so an agent may sign several
 *      trades at once and they may land in any order; each is usable once. The epoch changes each
 *      time an agent is added, so signatures made before a removal die with it, even if the same
 *      address is added again.
 *
 *      Uninstalling this module removes every agent of the wallet. Pausing the wallet stops them, as
 *      `executeFromExecutor` is `whenNotPaused`. One trade can't start another inside itself
 *      ({ReentrancyGuardTransient}, one lock for the whole module).
 */
contract AgentModule is IERC7579Module, EIP712, ReentrancyGuardTransient {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when the caller may not do this for `account`: not its owner (or, for
    ///      {removeAgent}, not the wallet itself), or the wallet owns itself.
    error AgentModule_NotOwner(address account, address caller);
    /// @dev Thrown by {addAgent} when this module is not installed on `account` as an executor.
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
    /// @dev Thrown when `agent` is not one of the wallet's agents.
    error AgentModule_UnknownAgent(address account, address agent);
    /// @dev Thrown by {execute} once the agent's expiry has passed.
    error AgentModule_AgentExpired(address account, address agent, uint48 validUntil);
    /// @dev Thrown by {execute} when the caller is not the agent and the signature is not the agent's.
    error AgentModule_BadSignature();
    /// @dev Thrown by {execute} after the signed deadline.
    error AgentModule_DeadlinePassed(uint48 deadline);
    /// @dev Thrown by {execute} when the nonce was used before.
    error AgentModule_NonceUsed(uint256 nonce);
    /// @dev Thrown by {execute} on any mode but a plain single call or a plain batch.
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
    /// @param nonce      The nonce the trade used.
    /// @param chargedUsd What the trade cost the wallet's USD limit (18 decimals).
    /// @param feeUsd     The trade's fee in USD, charged to the agent's limit only.
    /// @param spentUsd   The agent's spend in the window after this trade.
    event AgentExecuted(
        address indexed account,
        address indexed agent,
        uint256 nonce,
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

    /// @notice The EIP-712 type an agent signs for each trade. `executionCalldata` is hashed as EIP-712
    ///         hashes `bytes`: keccak256 of its contents.
    bytes32 public constant EXECUTION_TYPEHASH = keccak256(
        "AgentExecution(address account,bytes32 mode,bytes executionCalldata,uint256 nonce,uint48 deadline,uint32 epoch)"
    );

    /// @dev ERC-7579 modes an agent may use: one plain call, or a plain batch; both revert as a whole
    ///      on any failure. CALLTYPE_SINGLE / CALLTYPE_BATCH in the first byte, EXECTYPE_DEFAULT and
    ///      no selector or payload after it.
    bytes32 private constant _SINGLE_MODE = bytes32(0);
    bytes32 private constant _BATCH_MODE = bytes32(uint256(0x01) << 248);

    /**
     * @notice One agent of one wallet. Two slots.
     * @param limitUsd    USD the agent may spend per window of the wallet's limit (18 decimals).
     * @param spentUsd    What it spent in the window that started at `windowStart`.
     * @param validUntil  When it stops working.
     * @param windowStart The wallet's window start when `spentUsd` was last written. A different
     *                    window now means `spentUsd` belongs to an earlier one and counts as 0.
     * @param epoch       The epoch its signatures must name; set when it was added.
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

    /// @dev account => agent => word => bitmap of used nonces (nonce >> 8 picks the word, its low 8
    ///      bits the bit). Kept across removals: the epoch already voids old signatures.
    mapping(address account => mapping(address agent => mapping(uint256 word => uint256))) private _usedNonces;

    constructor() EIP712("AgentModule", "1") {}

    /*//////////////////////////////////////////////////////////////
                           ERC-7579 MODULE
    //////////////////////////////////////////////////////////////*/

    /// @inheritdoc IERC7579Module
    /// @dev Takes nothing, on purpose: anything acting as the wallet (a passkey through `execute`, an
    ///      agent, another executor) can call a module's onInstall directly, so it must not be able to
    ///      add an agent. Agents are added by the owner with {addAgent}.
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
        return moduleTypeId == MODULE_TYPE_EXECUTOR;
    }

    /*//////////////////////////////////////////////////////////////
                            OWNER: AGENTS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Gives `account` a new agent. Only the wallet's owner, calling directly.
     * @dev Refused unless this module is installed on the wallet and the wallet's limit meters native
     *      ETH/BNB (rule 1); the web app turns metering on first when it is off.
     * @param account    The SessionHandler wallet the agent will trade from.
     * @param agent      The agent's address: the key it signs trades with. Not zero, not the wallet,
     *                   not this module.
     * @param limitUsd   USD it may spend per window of the wallet's limit (18 decimals). Non-zero. The
     *                   wallet's own limit binds too, whichever is lower.
     * @param validUntil When it stops working: in the future, at most {MAX_VALIDITY} from now.
     */
    function addAgent(address account, address agent, uint128 limitUsd, uint48 validUntil) external {
        _requireOwner(account);
        if (!IERC7579ModuleConfig(account).isModuleInstalled(MODULE_TYPE_EXECUTOR, address(this), "")) {
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
     *      without the owner wallet. An agent's own trade can't call this: rule 3.
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
                              AGENT TRADES
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Runs one trade of `agent` from `account`: one call or a batch, through the wallet's
     *         guard, fee and spending limit, charged to the agent's own limit too.
     * @dev Callable by anyone with the agent's signature, or by the agent itself with none.
     *      Checks-effects-interactions: the nonce is used before the wallet is called, and
     *      {ReentrancyGuardTransient} keeps any call the trade makes from starting another trade.
     *      The agent's spend is written after the wallet's call, because it depends on it; a trade
     *      over the agent's limit reverts as a whole, nonce included.
     * @param account           The wallet to trade from.
     * @param agent             The agent whose trade it is.
     * @param mode              A plain single call (bytes32(0)) or a plain batch (0x01 then zeros).
     * @param executionCalldata The calls, ERC-7579 encoded for `mode`.
     * @param nonce             Any nonce the agent has not used before.
     * @param deadline          The last moment the trade may run.
     * @param signature         The agent's 65-byte signature of {hashExecution}; ignored when the
     *                          agent itself calls.
     * @return results Each call's return data.
     */
    function execute(
        address account,
        address agent,
        bytes32 mode,
        bytes calldata executionCalldata,
        uint256 nonce,
        uint48 deadline,
        bytes calldata signature
    ) external nonReentrant returns (bytes[] memory results) {
        Agent storage info = _requireAgent(account, agent);
        if (block.timestamp > info.validUntil) revert AgentModule_AgentExpired(account, agent, info.validUntil);
        if (block.timestamp > deadline) revert AgentModule_DeadlinePassed(deadline);
        if (mode != _SINGLE_MODE && mode != _BATCH_MODE) revert AgentModule_UnsupportedMode(mode);
        if (msg.sender != agent) {
            bytes32 digest = hashExecution(account, mode, executionCalldata, nonce, deadline, info.epoch);
            (address signer, ECDSA.RecoverError error,) = ECDSA.tryRecover(digest, signature);
            if (error != ECDSA.RecoverError.NoError || signer != agent) revert AgentModule_BadSignature();
        }
        _useNonce(account, agent, nonce);

        SessionHandler wallet = SessionHandler(payable(account));
        // Its storage reads warm the slots the wallet's hook reads next, so most of their cost is
        // paid once.
        SpendingLimitModule.Config memory config = wallet.getConfig();
        if (!config.nativeMetered) revert AgentModule_NativeNotMetered(account);
        _checkCalls(mode, executionCalldata, config.watchedTokens);

        uint256 feeUsd = _feeUsd(wallet);
        int256 remainingBefore = wallet.getRemainingBudget();
        results = IERC7579Execution(account).executeFromExecutor(mode, executionCalldata);
        // The wallet's spend only rises within a transaction, and its limit can't change inside one,
        // so the drop in what remains is exactly what this trade cost the limit.
        uint256 chargedUsd = SafeCast.toUint256(remainingBefore - wallet.getRemainingBudget());

        // The window the wallet's hook spent in: a new one if the last had run out, as the hook's
        // preCheck just started it then.
        uint32 window = block.timestamp >= uint256(config.windowStart) + uint256(config.windowDuration)
            ? uint32(block.timestamp)
            : config.windowStart;
        uint256 spentUsd = (info.windowStart == window ? uint256(info.spentUsd) : 0) + chargedUsd + feeUsd;
        if (spentUsd > info.limitUsd) revert AgentModule_AgentLimitExceeded(spentUsd, info.limitUsd);
        // Safe: spentUsd <= limitUsd, a uint128.
        // forge-lint: disable-next-line(unsafe-typecast)
        info.spentUsd = uint128(spentUsd);
        info.windowStart = window;

        emit AgentExecuted(account, agent, nonce, chargedUsd, feeUsd, spentUsd);
    }

    /*//////////////////////////////////////////////////////////////
                                 VIEWS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The EIP-712 digest an agent signs for one trade.
     * @param epoch The agent's current epoch, from {getAgent}.
     */
    function hashExecution(
        address account,
        bytes32 mode,
        bytes calldata executionCalldata,
        uint256 nonce,
        uint48 deadline,
        uint32 epoch
    ) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(EXECUTION_TYPEHASH, account, mode, keccak256(executionCalldata), nonce, deadline, epoch)
            )
        );
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

    /// @notice True when `agent` has used `nonce` on `account`.
    function isNonceUsed(address account, address agent, uint256 nonce) external view returns (bool) {
        return _usedNonces[account][agent][nonce >> 8] & (uint256(1) << (nonce & 0xff)) != 0;
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

    /// @dev Marks `nonce` used, reverting if it already was.
    function _useNonce(address account, address agent, uint256 nonce) private {
        uint256 bit = uint256(1) << (nonce & 0xff);
        mapping(uint256 => uint256) storage words = _usedNonces[account][agent];
        uint256 word = words[nonce >> 8];
        if (word & bit != 0) revert AgentModule_NonceUsed(nonce);
        words[nonce >> 8] = word | bit;
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
     *      wallet's own signature, which an agent can't make.
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

    /// @dev The fee the wallet pays for this trade (protocol fee plus keeper fee, in wei, read where the
    ///      wallet reads it), in USD at the oracle's native price. Zero when both fees are.
    function _feeUsd(SessionHandler wallet) private view returns (uint256) {
        (, uint256 fee, uint256 keeperFee) = wallet.REGISTRY().feeConfig();
        uint256 total = fee + keeperFee;
        if (total == 0) return 0;
        return SafeCast.toUint256(wallet.getUsdValue(address(0), total));
    }
}
