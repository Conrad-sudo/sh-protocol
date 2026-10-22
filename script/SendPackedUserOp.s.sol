//SPDX-License Identifier: MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {HelperConfig} from "./HelperConfig.s.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {IERC7579Execution, Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";

/**
 * @title SendPackedUserOp
 * @notice Helper script for generating and signing ERC-4337 PackedUserOperations targeting
 *         SessionHandler, an ERC-7579 account that validates its own UserOps.
 * @dev SessionHandler installs NO validator module (SpendingLimitModule is a hook only). Its own
 *      {_authenticate} tells the two kinds of signer apart by the signature's length: exactly 65
 *      bytes is the owner (plain ECDSA, the case this script signs), anything else is a PASSKEY
 *      the owner registered via addSession (a WebAuthn assertion carrying the passkey's public key).
 *      A secp256k1 key that is not the owner's is nobody. So:
 *        - {generateSignedUserOp} / {generateSignedBatchUserOp} sign with an ECDSA key: the OWNER.
 *        - {generateUnsignedUserOp} / {generateUnsignedBatchUserOp} build the same ops with an
 *          empty signature and hand back the hash, for a signer this script cannot be: a passkey
 *          signs that hash as a WebAuthn challenge (see test/utils/PasskeyHelper.sol).
 *
 *      Signing flow:
 *      1. Fetch nonce from EntryPoint using the nonce key (0 unless the caller picks one). No
 *         validator module is ever consulted, so the key never decides WHO may sign. For a PASSKEY
 *         op it does carry one thing: the op's own deadline, in its top 48 bits, which the account
 *         requires to be non-zero (see SessionHandler._authenticatePasskey and
 *         PasskeyHelper.nonceKey). Below it the app draws a fresh random 144-bit lane per passkey op
 *         so two confirmations never queue behind each other. Owner ops use key 0.
 *      2. Pack the execution into ERC-7579 execute(mode, executionCalldata) calldata
 *         (single-call or batch).
 *      3. Get userOpHash from EntryPoint (EIP-712 typed structured data hash).
 *      4. Owner ops: wrap in EIP-191 envelope via toEthSignedMessageHash (matches what
 *         SessionHandler._authenticate re-wraps before recovery) and sign with vm.sign.
 *         Passkey ops stop before this step: the caller signs the raw userOpHash, with no
 *         envelope, as a WebAuthn challenge.
 *
 *      NOTE: session-key ops are subject to SessionHandler's execution guard -- a non-owner op
 *      whose target is the account itself or its SpendingLimitModule reverts at execution time
 *      (SessionHandler_SessionRestrictedTarget), and delegatecall mode is rejected outright.
 *      This script builds single and batch modes only, which is all the account supports for
 *      session keys anyway.
 */
contract SendPackedUserOp is Script {
    using MessageHashUtils for bytes32;

    /**
     * @notice Generates a signed PackedUserOperation for a single-call execute() on a
     *         SessionHandler account.
     * @param sender     The SessionHandler account address that will send the UserOp.
     * @param config     Network configuration containing the entryPoint address.
     * @param dest       Target contract for the inner call.
     * @param value      Native ETH value (wei) to forward with the inner call.
     * @param data       Calldata for the inner call.
     * @param signer     Address whose key signs the op: the owner (a session key is a passkey now and
     *                   cannot sign here; see {generateUnsignedUserOp}).
     * @param signerKey  Private key matching `signer`.
     * @return userOp     The fully constructed and signed PackedUserOperation.
     * @return userOpHash The raw EIP-712 hash of the UserOp as returned by the EntryPoint.
     * @return digest     The EIP-191 wrapped digest that was actually signed.
     */
    function generateSignedUserOp(
        address sender,
        HelperConfig.NetworkConfig memory config,
        address dest,
        uint256 value,
        bytes memory data,
        address signer,
        uint256 signerKey
    ) external view returns (PackedUserOperation memory, bytes32, bytes32) {
        // Single-call mode: CALLTYPE_SINGLE (0x00) | EXECTYPE_DEFAULT (0x00) => bytes32(0).
        // The inner (target, value, data) triple is packed with abi.encodePacked, not abi.encode,
        // matching ERC7579Utils.encodeSingle/decodeSingle's fixed-offset layout.
        bytes memory executionCalldata = abi.encodePacked(dest, value, data);
        bytes memory callData = abi.encodeCall(IERC7579Execution.execute, (bytes32(0), executionCalldata));
        return _signUserOp(sender, config, callData, signer, signerKey);
    }

    /**
     * @notice Generates a signed PackedUserOperation for a batch execute() on a SessionHandler
     *         account -- e.g. [approve router, swap] so the approval is consumed atomically in the
     *         same transaction (SpendingLimitModule reverts any approval left standing).
     * @param sender     The SessionHandler account address that will send the UserOp.
     * @param config     Network configuration containing the entryPoint address.
     * @param executions The (target, value, callData) triples to execute in order.
     * @param signer     Address whose key signs the op: the owner.
     * @param signerKey  Private key matching `signer`.
     * @return userOp     The fully constructed and signed PackedUserOperation.
     * @return userOpHash The raw EIP-712 hash of the UserOp as returned by the EntryPoint.
     * @return digest     The EIP-191 wrapped digest that was actually signed.
     */
    function generateSignedBatchUserOp(
        address sender,
        HelperConfig.NetworkConfig memory config,
        Execution[] memory executions,
        address signer,
        uint256 signerKey
    ) external view returns (PackedUserOperation memory, bytes32, bytes32) {
        // Batch mode word: CALLTYPE_BATCH (0x01) in the top byte, EXECTYPE_DEFAULT (0x00) next,
        // everything else zero -- matches ERC7579Utils.decodeMode's byte layout.
        bytes32 mode = bytes32(uint256(0x01) << 248);
        bytes memory callData = abi.encodeCall(IERC7579Execution.execute, (mode, ERC7579Utils.encodeBatch(executions)));
        return _signUserOp(sender, config, callData, signer, signerKey);
    }

    /**
     * @notice Builds the same single-call op as {generateSignedUserOp}, but leaves `signature` empty
     *         and returns the hash a signer has to sign. For signers that are not a secp256k1 key: a
     *         passkey signs this hash as a WebAuthn challenge, with no EIP-191 envelope.
     * @param sender   The SessionHandler account address that will send the UserOp.
     * @param config   Network configuration containing the entryPoint address.
     * @param dest     Target contract for the inner call.
     * @param value    Native ETH value (wei) to forward with the inner call.
     * @param data     Calldata for the inner call.
     * @param nonceKey The EntryPoint nonce key (the high 192 bits of the nonce). Sequence is whatever
     *                 the EntryPoint holds for that key: 0 for a key never used. A passkey op's key
     *                 must carry a non-zero op deadline in its top 48 bits.
     * @return userOp     The constructed PackedUserOperation, `signature` empty.
     * @return userOpHash The EntryPoint's hash of it: what the signer signs (its WebAuthn challenge).
     */
    function generateUnsignedUserOp(
        address sender,
        HelperConfig.NetworkConfig memory config,
        address dest,
        uint256 value,
        bytes memory data,
        uint192 nonceKey
    ) external view returns (PackedUserOperation memory userOp, bytes32 userOpHash) {
        bytes memory executionCalldata = abi.encodePacked(dest, value, data);
        bytes memory callData = abi.encodeCall(IERC7579Execution.execute, (bytes32(0), executionCalldata));
        return _buildUserOp(sender, config, callData, nonceKey);
    }

    /**
     * @notice The batch twin of {generateUnsignedUserOp}.
     * @param sender     The SessionHandler account address that will send the UserOp.
     * @param config     Network configuration containing the entryPoint address.
     * @param executions The (target, value, callData) triples to execute in order.
     * @param nonceKey   The EntryPoint nonce key (the high 192 bits of the nonce).
     * @return userOp     The constructed PackedUserOperation, `signature` empty.
     * @return userOpHash The EntryPoint's hash of it: what the signer signs (its WebAuthn challenge).
     */
    function generateUnsignedBatchUserOp(
        address sender,
        HelperConfig.NetworkConfig memory config,
        Execution[] memory executions,
        uint192 nonceKey
    ) external view returns (PackedUserOperation memory userOp, bytes32 userOpHash) {
        bytes32 mode = bytes32(uint256(0x01) << 248);
        bytes memory callData = abi.encodeCall(IERC7579Execution.execute, (mode, ERC7579Utils.encodeBatch(executions)));
        return _buildUserOp(sender, config, callData, nonceKey);
    }

    /// @dev Nonce lookup, unsigned op construction and hashing, shared by every builder here.
    function _buildUserOp(
        address sender,
        HelperConfig.NetworkConfig memory config,
        bytes memory callData,
        uint192 nonceKey
    ) internal view returns (PackedUserOperation memory userOp, bytes32 userOpHash) {
        uint256 nonce = IEntryPoint(config.entryPoint).getNonce(sender, nonceKey);

        // 1. Build the unsigned UserOp with hardcoded gas parameters
        userOp = _generateUnsignedUserOp(sender, nonce, callData);

        // 2. Get the EIP-712 typed structured data hash from the EntryPoint
        userOpHash = IEntryPoint(config.entryPoint).getUserOpHash(userOp);
    }

    /// @dev Shared tail of the owner builders: build the op (nonce key 0), then EIP-191 sign it.
    function _signUserOp(
        address sender,
        HelperConfig.NetworkConfig memory config,
        bytes memory callData,
        address signer,
        uint256 signerKey
    ) internal view returns (PackedUserOperation memory, bytes32, bytes32) {
        // 1-2. Build the unsigned UserOp (nonce key 0) and get its hash from the EntryPoint
        (PackedUserOperation memory userOp, bytes32 userOpHash) = _buildUserOp(sender, config, callData, 0);

        // 3. Wrap in EIP-191 envelope to produce the signable digest
        bytes32 digest = userOpHash.toEthSignedMessageHash();

        // 4. Sign the digest — vm.sign returns v as 27/28 already adjusted
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, digest);
        require(ecrecover(digest, v, r, s) == signer, "signer does not match signerKey");

        // 5. Attach the packed (r, s, v) signature to the UserOp
        userOp.signature = abi.encodePacked(r, s, v);

        return (userOp, userOpHash, digest);
    }

    /**
     * @notice Constructs an unsigned PackedUserOperation with hardcoded gas parameters
     * @dev Gas values are intentionally set high for testing purposes and should not
     *      be used in production. Fields are packed according to ERC-4337 v0.7 spec:
     *      - accountGasLimits: verificationGasLimit (upper 128 bits) | callGasLimit (lower 128 bits)
     *      - gasFees: maxFeePerGas (upper 128 bits) | maxPriorityFeePerGas (lower 128 bits)
     *      initCode and paymasterAndData are empty as this account is already deployed
     *      and self-funded. signature is empty as it is attached after hashing.
     */
    function _generateUnsignedUserOp(address sender, uint256 nonce, bytes memory callData)
        internal
        pure
        returns (PackedUserOperation memory)
    {
        uint128 verificationGasLimit = 17e6;
        uint128 callGasLimit = 17e6;
        uint128 maxFeePerGas = 256;
        uint128 maxPriorityFeePerGas = 256;

        return PackedUserOperation({
            sender: sender,
            nonce: nonce,
            initCode: hex"",
            callData: callData,
            accountGasLimits: bytes32(uint256(uint256(verificationGasLimit) << 128 | uint256(callGasLimit))),
            preVerificationGas: verificationGasLimit,
            gasFees: bytes32(uint256(maxFeePerGas) << 128 | uint256(maxPriorityFeePerGas)),
            paymasterAndData: hex"",
            signature: hex""
        });
    }
}
