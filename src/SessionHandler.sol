/* SPDX-License-Identifier: BUSL-1.1
 * Licensor: Conrad Japhet
 * Licensed Work: SessionHandler.sol
 * Change Date: 2029-06-12
 * Change License: MIT
*/
pragma solidity ^0.8.24;
//Account Abstraction imports
import {IAccount} from "@account-abstraction/contracts/interfaces/IAccount.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {SIG_VALIDATION_FAILED, SIG_VALIDATION_SUCCESS} from "@account-abstraction/contracts/core/Helpers.sol";
//Openzeppelin imports
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
//Chainlink imports
import {SHOracle} from "./SHOracle.sol";
import {IReputationRegistry} from "./interfaces/IReputationRegistry.sol";
import {IIdentityRegistry} from "./interfaces/IIdentityRegistry.sol";
import {SHRegistry} from "./SHRegistry.sol";
import {SHValueInterpreter} from "./SHValueInterpreter.sol";

/**
 * @title SessionHandler
 * @author Conrad Japhet
 * @notice ERC-4337 smart account with delegated session key permissions
 * @dev Enables temporary, limited-permission keys for account abstraction without exposing
 *      the main private key. Sessions are time-bound, spending-limited, and function-restricted.
 *
 * Features:
 * - Time windows (validFrom to validUntil, 48-bit timestamps)
 * - Spending limits (per-session wei tracking)
 * - Target + selector whitelisting (O(1) validation)
 * - Owner-controlled revocation
 * - ERC-4337 v0.7 compatible
 */

contract SessionHandler is IAccount, Ownable, ReentrancyGuard, Pausable {
    using SafeERC20 for IERC20;

    /*//////////////////////////////////////////////////////////////
                                    ERRORS
    //////////////////////////////////////////////////////////////*/
    /// @dev Thrown when the caller is not the canonical ERC-4337 EntryPoint.
    error SessionHandler_NotEntryPoint();
    /// @dev Thrown when the caller is neither the EntryPoint nor the contract owner.
    error SessionHandler_NotEntryPointOrOwner();
    /// @dev Thrown when the low-level call inside execute() reverts.
    error SessionHandler_ExecutionFailed();
    /// @dev Thrown when address(0) is passed as the session key.
    error SessionHandler_InvalidSessionKey();
    /// @dev Thrown when a non-ETH session is registered with address(0) as target but non-empty selectors, or vice-versa.
    error SessionHandler_InvalidTarget();
    /// @dev Thrown when validFrom >= validUntil.
    error SessionHandler_InvalidTimeRange();
    /// @dev Thrown when the requested spending limit exceeds the wallet's current token balance.
    error SessionHandler_SpendingLimitExceedsBalance();
    /// @dev Thrown when attempting to revoke a session key that has never been registered.
    error SessionHandler_SessionIsNotActive();
    /// @dev Thrown when validUntil is already in the past at the time of registration.
    error SessionHandler_InvalidEndTime();
    /// @dev Thrown when a zero spending limit is passed to addSessionKey.
    error SessionHandler_SpendingLimitCannotBeZero();
    /// @dev Thrown when a withdrawal amount exceeds the wallet's available balance.
    error SessionHandler_NotEnoughBalance();
    /// @dev Thrown when a session key's USD spending limit would be exceeded by the current call.
    error SessionHandler_SpendingLimitExceeded();
    /// @dev Thrown when the protocol fee ETH transfer to the treasury fails.
    error SessionHandler_FeeTransferFailed();
    /// @dev Thrown when address(0) is passed as the withdrawal recipient.
    error SessionHandler_InvalidRecipient();

    /*//////////////////////////////////////////////////////////////

                           STATE VARIABLES
    //////////////////////////////////////////////////////////////*/
    /// @dev The canonical ERC-4337 EntryPoint. Immutable — only this address may call validateUserOp.
    address public immutable ENTRY_POINT;
    /// @dev Reputation Registry address. Immutable — used to identify and permit reputation calls.
    address public immutable REPUTATION_REGISTRY;
    /// @dev Identity Registry address. Immutable — used for ERC-8004 agent identity lookups.
    address public immutable IDENTITY_REGISTRY;
    /// @dev SHRegistry providing uniswapRouter, priceOracle, treasury, and protocol fee at runtime.
    SHRegistry public immutable REGISTRY;
    /// @dev Sequential ID assigned by SHFactory at deployment. Unique per wallet, unlike the shared ERC-8004 agentId.
    uint256 public immutable WALLET_ID;
    /// @dev Maps each registered session key address to its Session configuration.
    mapping(address => Session) sessions;
    /// @dev Maps (sessionKey, selector) to whether that selector is whitelisted for the session.
    mapping(address => mapping(bytes4 => bool)) isSelectorAllowed;
    /// @dev Tracks every session key ever registered. Stays true after revocation so re-registration triggers cleanup.
    mapping(address => bool) sessionExists;

    /**
     * @dev Full configuration and runtime state for a single delegated session.
     *      Stored in the `sessions` mapping keyed by the session key address.
     */
    struct Session {
        /// @dev True once the session has been activated (set at creation if validFrom <= block.timestamp).
        bool active;
        /// @dev Whitelisted target contract. address(0) is the sentinel for native ETH sends.
        address target;
        /// @dev Unix timestamp from which the session is valid.
        uint48 validFrom;
        /// @dev Unix timestamp after which the session expires.
        uint48 validUntil;
        /// @dev Maximum cumulative USD spend the session may authorize (18 decimals).
        uint256 spendingLimit;
        /// @dev Running total of USD charged against this session (18 decimals).
        uint256 spentAmount;
        /// @dev Function selectors the session key is allowed to call on `target`.
        bytes4[] selectors;
    }

    /// @dev EIP-1153 transient variables bridging validateUserOp → execute within the same handleOps transaction.
    ///      Written by _setPendingSession on successful validation; read and consumed in execute().
    ///      Both slots are automatically zeroed at transaction end — no manual cleanup required.
    address transient tPendingSessionKey;
    bytes4 transient tPendingSelector;

    /*//////////////////////////////////////////////////////////////
                                   EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when a new session key is registered by the owner.
    /// @param sessionKey The address granted session access.
    /// @param target     The contract the session key is authorised to call (address(0) for native ETH sends).
    /// @param validUntil Unix timestamp at which the session expires.
    event SessionAdded(address indexed sessionKey, address indexed target, uint48 validUntil);

    /// @notice Emitted when a session key is revoked by the owner.
    /// @param sessionKey The address whose session was revoked.
    event SessionRevoked(address indexed sessionKey);

    /*//////////////////////////////////////////////////////////////
                                 MODIFIERS
    //////////////////////////////////////////////////////////////*/

    modifier onlyEntryPoint() {
        _onlyEntryPoint();
        _;
    }

    modifier onlyEntryPointOrOwner() {
        _onlyEntryPointOrOwner();
        _;
    }

    /*//////////////////////////////////////////////////////////////
                                CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/
    /**
     * @notice Deploys the SessionHandler and wires it to the protocol registry.
     * @param owner              The address that will own this wallet and control session key management.
     * @param entryPoint         The canonical ERC-4337 EntryPoint. Only this address may call validateUserOp.
     * @param reputationRegistry The Reputation Registry address.
     * @param identityRegistry   The ERC-8004 Identity Registry address.
     * @param registry           The SHRegistry address supplying uniswapRouter, priceOracle, treasury,
     *                           and protocol fee at runtime.
     * @param walletId           Sequential wallet ID assigned by SHFactory.
     */
    constructor(
        address owner,
        address entryPoint,
        address reputationRegistry,
        address identityRegistry,
        address registry,
        uint256 walletId
    ) Ownable(owner) {
        ENTRY_POINT = entryPoint;
        REPUTATION_REGISTRY = reputationRegistry;
        IDENTITY_REGISTRY = identityRegistry;
        REGISTRY = SHRegistry(registry);
        WALLET_ID = walletId;
    }

    /*//////////////////////////////////////////////////////////////
                            RECEIVE FUNCTION
    //////////////////////////////////////////////////////////////*/
    /// @notice Accepts ETH sent directly to the wallet. Required so the EntryPoint can charge gas prefunds.
    receive() external payable {}

    /*//////////////////////////////////////////////////////////////
                           EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /// @notice Pauses the contract, disabling execute(). Only callable by the owner.
    function pause() public onlyOwner {
        _pause();
    }

    /// @notice Unpauses the contract, re-enabling execute(). Only callable by the owner.
    function unpause() public onlyOwner {
        _unpause();
    }

    /**
     * @notice Executes an arbitrary call from this wallet.
     * @dev When called by the EntryPoint on behalf of a session key, reads the validated spend amounts
     *      from EIP-1153 transient storage and applies the budget debit (or credit for removeLiquidity)
     *      before dispatching the call. Session auto-cleanup fires if the session expired or exhausted
     *      its budget after the transaction settles.
     * @param dest  Target address to call.
     * @param value ETH value in wei to forward with the call.
     * @param data  Encoded calldata to pass.
     */
    function execute(address dest, uint256 value, bytes calldata data) external onlyEntryPointOrOwner whenNotPaused {
        bool isSessionKeyExecution = msg.sender == ENTRY_POINT && tPendingSessionKey != address(0);
        if (isSessionKeyExecution) {
            address sessionKey = tPendingSessionKey;
            Session storage selectedSession = sessions[sessionKey];
            bytes4 selector = tPendingSelector;

            // Oracle access is unrestricted in the execution phase (no opcode ban).
            bytes memory dataMem = data;
            (uint256 debitValueInUsd, uint256 creditValueInUsd) =
                SHValueInterpreter(REGISTRY.callValueInterpreter()).computeUsdValue(dest, value, dataMem, selector);

            if (creditValueInUsd > 0) {
                /* For removeLiquidity we use the minimum amounts from the UserOp, not the actual
                   amounts received, so we credit conservatively rather than charging. */
                creditValueInUsd >= selectedSession.spentAmount
                    ? selectedSession.spentAmount = 0
                    : selectedSession.spentAmount -= creditValueInUsd;
            } else {
                if (selectedSession.spentAmount + debitValueInUsd > selectedSession.spendingLimit) {
                    revert SessionHandler_SpendingLimitExceeded();
                }
                selectedSession.spentAmount += debitValueInUsd;
            }

            if (!isSessionActive(sessionKey)) {
                _deleteSelectorMappings(sessionKey);
                delete sessions[sessionKey];
                sessionExists[sessionKey] = false;
            }
        }

        (bool success,) = payable(dest).call{value: value}(data);
        if (!success) {
            revert SessionHandler_ExecutionFailed();
        }

        uint256 fee = REGISTRY.protocolFee();
        if (isSessionKeyExecution && fee > 0) {
            (bool feeSuccess,) = payable(REGISTRY.treasury()).call{value: fee}("");
            if (!feeSuccess) revert SessionHandler_FeeTransferFailed();
        }
    }

    /**
     * @notice Validates a UserOperation and prepays gas to the EntryPoint. Required by IAccount (ERC-4337).
     * @dev Recovers the signer via ECDSA over the EIP-191-wrapped userOpHash. Owner signatures return
     *      unconditional success; session key signatures are delegated to _validateSession which also
     *      writes spend amounts to transient storage for execute() to consume.
     * @param userOp              The packed UserOperation to validate.
     * @param userOpHash          ERC-4337 hash of the UserOperation used to recover the signer.
     * @param missingAccountFunds ETH in wei owed to the EntryPoint for gas prefunding.
     * @return validationData     Packed uint256 encoding signature result, validAfter, and validUntil per ERC-4337.
     */
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash, uint256 missingAccountFunds)
        external
        onlyEntryPoint
        returns (uint256 validationData)
    {
        validationData = _validateSignature(userOp, userOpHash);
        _payPreFund(missingAccountFunds);
    }

    /**
     * @notice Registers a new delegated session key with scoped permissions. Only callable by the owner.
     * @dev If the key was previously registered, its selector mappings are cleaned up before the new
     *      session overwrites the old one. Pass address(0) as `target` for a native ETH-send session —
     *      in that case `selectors` must be empty.
     * @param sessionKey    Address of the EOA or contract being granted session access.
     * @param target        Contract the session key may call. Use address(0) to permit native ETH sends.
     * @param selectors     Function selectors the session key is allowed to invoke on `target`.
     * @param validFrom     Unix timestamp at which the session becomes active.
     * @param validUntil    Unix timestamp at which the session expires. Must be in the future.
     * @param spendingLimit Maximum cumulative USD the session may spend (18 decimals). Must be non-zero.
     */
    function addSessionKey(
        address sessionKey,
        address target,
        bytes4[] calldata selectors,
        uint48 validFrom,
        uint48 validUntil,
        uint256 spendingLimit
    ) external onlyOwner {
        //check to see weather the selector actually exists on the target contract
        if (spendingLimit == 0 && target != REPUTATION_REGISTRY) revert SessionHandler_SpendingLimitCannotBeZero();
        if (sessionKey == address(0)) revert SessionHandler_InvalidSessionKey();
        // address(0) is the sentinel for a native ETH-send session; it must have no selectors
        if (target == address(0) && selectors.length != 0) revert SessionHandler_InvalidTarget();
        if (validFrom >= validUntil) revert SessionHandler_InvalidTimeRange();
        if (validUntil <= block.timestamp) revert SessionHandler_InvalidEndTime();
        if (sessionExists[sessionKey]) _deleteSelectorMappings(sessionKey);
        if (!sessionExists[sessionKey]) sessionExists[sessionKey] = true; // mark session key as existing

        sessions[sessionKey] = Session({
            target: target,
            selectors: selectors,
            validFrom: validFrom,
            validUntil: validUntil,
            spendingLimit: spendingLimit,
            spentAmount: 0,
            active: validFrom <= block.timestamp //activate session immediately if validFrom is in the past
        });

        uint256 length = selectors.length;
        for (uint256 i = 0; i < length; i++) {
            isSelectorAllowed[sessionKey][selectors[i]] = true;
        }

        emit SessionAdded(sessionKey, target, validUntil);
    }

    /**
     * @notice Revokes a session key, clearing its selector mappings and spending state. Only callable by the owner.
     * @dev Reverts with SessionHandler_SessionIsNotActive if the key was never registered.
     * @param sessionKey The session key address to revoke.
     */
    function revokeSessionKey(address sessionKey) public onlyOwner {
        //Session storage session = sessions[sessionKey];

        if (!sessionExists[sessionKey]) revert SessionHandler_SessionIsNotActive();
        _deleteSelectorMappings(sessionKey);

        delete sessions[sessionKey]; // remove session data

        sessionExists[sessionKey] = false; // mark session key as not existing

        emit SessionRevoked(sessionKey);
    }

    /**
     * @notice Withdraws ERC20 tokens or native ETH from the wallet to a recipient chosen by the owner.
     *         Only callable by the owner.
     * @dev Pass address(0) as `token` to withdraw native ETH. Reverts with SessionHandler_NotEnoughBalance
     *      if the wallet holds less than `amount`. `to` is owner-supplied so that an owner that is itself
     *      a multisig (e.g. a Safe) can send funds directly to their final destination in a single
     *      approved transaction, rather than withdrawing to the multisig and then forwarding again.
     * @param token  ERC20 token address to withdraw, or address(0) for native ETH.
     * @param amount Amount to withdraw in the token's base units (e.g. 1e6 for 1 USDC at 6 decimals).
     * @param to     Recipient address for the withdrawn funds.
     */
    function withdraw(address token, uint256 amount, address to) external onlyOwner {
        if (to == address(0)) revert SessionHandler_InvalidRecipient();
        if (token != address(0)) {
            if (IERC20(token).balanceOf(address(this)) < amount) revert SessionHandler_NotEnoughBalance();
            SafeERC20.safeTransfer(IERC20(token), to, amount);
        } else {
            if (address(this).balance < amount) revert SessionHandler_NotEnoughBalance();
            (bool success,) = payable(to).call{value: amount}("");
            if (!success) {
                revert SessionHandler_ExecutionFailed();
            }
        }
    }

    /*//////////////////////////////////////////////////////////////
                           INTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @dev Clears all selector allowances for a given session key.
     *      Called before overwriting or deleting a session to avoid stale mappings.
     * @param sessionKey The session key whose selector mappings will be removed.
     */
    /**
     * @dev Validation-safe liveness check: returns true if the session exists and has budget.
     *      Does NOT read block.timestamp — time-window enforcement is delegated to the EntryPoint
     *      via the validFrom/validUntil packed into validationData.
     *      Use only from validateUserOp contexts where TIMESTAMP is a banned opcode.
     */
    function _isSessionUsable(address sessionKey) internal view returns (bool) {
        return sessionExists[sessionKey] && getRemainingBudget(sessionKey) > 0;
    }

    function _deleteSelectorMappings(address sessionKey) internal {
        Session storage session = sessions[sessionKey];
        uint256 length = session.selectors.length;
        for (uint256 i = 0; i < length; i++) {
            delete isSelectorAllowed[sessionKey][session.selectors[i]];
        }
    }

    /**
     * @dev Recovers the signer from the userOp signature and routes validation.
     *      Returns success for the owner, delegates to _validateSession for session keys,
     *      and returns failure for unrecognized signers.
     * @param userOp The packed user operation containing the signature.
     * @param userOpHash The ERC-4337 hash of the user operation.
     * @return validationData Packed ERC-4337 validation result.
     */
    function _validateSignature(PackedUserOperation calldata userOp, bytes32 userOpHash)
        internal
        returns (uint256 validationData)
    {
        //recover the signer from the userOp and the userOpHash
        //format the userOpHash to the ERC191 signed data
        bytes32 digest = MessageHashUtils.toEthSignedMessageHash(userOpHash);
        address signer = ECDSA.recover(digest, userOp.signature);

        //the owner
        if (signer == owner()) {
            tPendingSessionKey = address(0);
            return _packValidationData(false, 0, 0);
        }
        //there is an  session
        else if (sessionExists[signer]) {
            return _validateSession(signer, userOp);
        }
        //invalid signer or inactive session
        else {
            return _packValidationData(true, 0, 0);
        }
    }

    /**
     * @dev pay back the entry Point contract
     * @param missingAccountFunds The minimum amount to transfer to the sender (entryPoint)
     */
    function _payPreFund(uint256 missingAccountFunds) internal {
        (bool success,) = payable(ENTRY_POINT).call{value: missingAccountFunds}("");

        (success);
    }

    /**
     * @dev Validates a session-key-signed UserOperation against the registered session constraints.
     *      Decodes the execute() calldata to extract dest, value, and inner data, then routes through
     *      ERC20 or Uniswap-specific assembly parsing to compute the USD value being authorized.
     *      On success, writes the computed amounts to EIP-1153 transient storage via _setPendingSession
     *      so execute() can apply the budget debit after the inner call completes.
     * @param signer  Address recovered from the UserOperation signature — must be a registered session key.
     * @param userOp  The packed UserOperation whose calldata is decoded and validated.
     * @return validationData Packed ERC-4337 result encoding signature success, validFrom, and validUntil.
     */

    function _validateSession(address signer, PackedUserOperation calldata userOp)
        internal
        returns (uint256 validationData)
    {
        Session storage selectedSession = sessions[signer];

        (address dest, uint256 value, bytes memory data) = abi.decode(userOp.callData[4:], (address, uint256, bytes));
        bytes4 selector;

        // Native ETH send: address(0) target is the sentinel for ETH sends to any recipient.
        if (data.length == 0 && value > 0) {
            if (selectedSession.target != address(0) || !_isSessionUsable(signer)) {
                return _packValidationData(true, selectedSession.validFrom, selectedSession.validUntil);
            }
            _setPendingSession(signer, bytes4(0));
            return _packValidationData(false, selectedSession.validFrom, selectedSession.validUntil);
        }

        if (selectedSession.target != dest) {
            return _packValidationData(true, selectedSession.validFrom, selectedSession.validUntil);
        }

        if (dest == REPUTATION_REGISTRY) {
            // For calls to the Reputation Registry, we ignore the selector and allow any function
            // so that session keys can perform all reputation-related actions (e.g. giving feedback,
            // reading summaries, etc.) without needing separate sessions for each function.
            return _packValidationData(false, selectedSession.validFrom, selectedSession.validUntil);
        }

        if (data.length >= 4) {
            assembly {
                selector := mload(add(data, 32))
            }
        }

        // Only own-storage reads here — no oracle calls.
        // Spending limit is enforced in execute() where external storage access is unrestricted.
        if (!_isSessionUsable(signer) || !isSelectorAllowed[signer][selector]) {
            return _packValidationData(true, selectedSession.validFrom, selectedSession.validUntil);
        }

        _setPendingSession(signer, selector);
        return _packValidationData(false, selectedSession.validFrom, selectedSession.validUntil);
    }

    /**
     * @dev Writes the two EIP-1153 transient variables that bridge validateUserOp → execute.
     *      The slots are zeroed automatically at the end of the transaction.
     */
    function _setPendingSession(address sessionKey, bytes4 sel) internal {
        tPendingSessionKey = sessionKey;
        tPendingSelector = sel;
    }

    /**
     * @dev Pack validation data according to ERC-4337 spec
     * @param sigFailed Whether signature validation passed
     * @param validUntil Expiration timestamp
     * @param validFrom Activation timestamp
     * @return validationData Packed uint256 for ERC-4337
     */
    function _packValidationData(bool sigFailed, uint48 validFrom, uint48 validUntil)
        internal
        pure
        returns (uint256 validationData)
    {
        validationData = (sigFailed ? SIG_VALIDATION_FAILED : SIG_VALIDATION_SUCCESS) //first 160 bytes 0-159
            | (uint256(validUntil) << 160) //next 48 bytes 160-207
            | (uint256(validFrom) << (160 + 48)); //next 48 bytes 208-256
    }

    /*//////////////////////////////////////////////////////////////
                             VIEW FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /// @notice Returns the full Session struct for a given session key.
    /// @param sessionKey The session key address to look up.
    /// @return The Session struct containing target, selectors, time window, and spending data.
    function getSession(address sessionKey) public view returns (Session memory) {
        return sessions[sessionKey];
    }

   

    /// @notice Returns whether a session key is currently active.
    /// @param sessionKey The session key address to check.
    /// @return True if the session is active and within its valid time window with budget remaining.
    function isSessionActive(address sessionKey) public view returns (bool) {
        Session storage session = sessions[sessionKey];
        //check if session is active first

        if (session.active) {
            //check if session meets criteria for auto-revocation: expired or budget exhausted

            if (block.timestamp > session.validUntil || getRemainingBudget(sessionKey) == 0) {
                return false;
            }
        } else if (
            sessionExists[sessionKey] && block.timestamp >= session.validFrom && block.timestamp <= session.validUntil
                && getRemainingBudget(sessionKey) > 0
        ) {
            return true; // auto-activate session if within valid time range
        }

        return session.active;
    }

    /// @notice Returns the current USD price of a token by querying the registered SHOracle.
    /// @param token The token address to price. Use address(0) for native ETH.
    /// @return price The current price with 8 decimals (Chainlink standard).
    /// @return decimals The decimal count of the returned price (always 8 for Chainlink USD feeds).
    function getPrice(address token) public view returns (uint256 price, uint8 decimals) {
        return SHOracle(REGISTRY.priceOracle()).getPrice(token);
    }

    function getAgentId() public view returns (uint256){
        return REGISTRY.agentId();
    }


    /// @notice Returns the agent's ERC-8004 on-chain identity.
    /// @return registered True if the agent token exists in the Identity Registry.
    /// @return agentId    The ERC-8004 tokenId. 0 if not registered.
    /// @return agentUri   The agent card URI. Empty string if not registered.
    function getAgentIdentity() public view returns (bool registered, uint256 agentId, string memory agentUri) {
        agentId = getAgentId();
        try IIdentityRegistry(IDENTITY_REGISTRY).ownerOf(agentId) returns (address) {
            agentUri = IIdentityRegistry(IDENTITY_REGISTRY).tokenURI(agentId);
            registered = true;
        } catch {
            registered = false;
        }
    }

    /// @notice Returns the agent's on-chain reputation from the Reputation Registry, scoped to this wallet.
    /// @return agentId              The ERC-8004 tokenId.
    /// @return feedbackCount        Number of feedback entries from this wallet.
    /// @return summaryValue         Aggregated score (raw — divide by 10**summaryValueDecimals to scale).
    /// @return summaryValueDecimals Decimal precision of summaryValue.
    function getAgentReputation()
        public
        view
        returns (uint256 agentId, uint64 feedbackCount, int128 summaryValue, uint8 summaryValueDecimals)
    {
        agentId = getAgentId();
        address[] memory clients = new address[](1);
        clients[0] = address(this);
        (feedbackCount, summaryValue, summaryValueDecimals) =
            IReputationRegistry(REPUTATION_REGISTRY).getSummary(agentId, clients, "", "");
    }

    /// @notice Returns the Uniswap V2 Router address from the registry.
    function getUniswapRouter() public view returns (address) {
        return REGISTRY.uniswapRouter();
    }

    

    /// @notice Checks whether a proposed spend is within the session's remaining budget.
    /// @param sessionKey The session key to check.
    /// @param token The ERC20 token address used to convert `amount` to USD via the price oracle.
    /// @param amount The token amount to check, in base units (e.g. 100e6 for 100 USDC).
    /// @return True if the session is active and the proposed spend fits within the remaining budget.
    function isSpendingWithinBudget(address sessionKey, address token, uint256 amount) public view returns (bool) {
        Session memory session = sessions[sessionKey];
        if (!isSessionActive(sessionKey)) return false;
        uint256 valueInUsd = SHOracle(REGISTRY.priceOracle()).getUsdValue(token, amount);
        return session.spentAmount + valueInUsd <= session.spendingLimit;
    }

    /// @notice Returns the remaining spending budget for a session key in USD (same units as spendingLimit).
    /// @param sessionKey The session key address to check.
    /// @return The remaining budget, or 0 if the limit has been reached or exceeded.
    function getRemainingBudget(address sessionKey) public view returns (uint256) {
        Session memory session = sessions[sessionKey];
        if (session.spentAmount >= session.spendingLimit) {
            return 0;
        }
        return session.spendingLimit - session.spentAmount;
    }

    /// @dev Reverts if the caller is not the EntryPoint.
    function _onlyEntryPoint() internal view {
        if (msg.sender != ENTRY_POINT) {
            revert SessionHandler_NotEntryPoint();
        }
    }

    /// @dev Reverts if the caller is neither the EntryPoint nor the owner.
    function _onlyEntryPointOrOwner() internal view {
        if (msg.sender != ENTRY_POINT && msg.sender != owner()) {
            revert SessionHandler_NotEntryPointOrOwner();
        }
    }
}

