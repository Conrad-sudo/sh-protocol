//SPDX-License Identifier: MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {HelperConfig} from "./HelperConfig.s.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

/**
 * @title SendPackedUserOp
 * @notice Helper script for generating and signing ERC-4337 PackedUserOperations
 * @dev Used in tests and deployment scripts to construct valid signed UserOps
 *      for submission to the EntryPoint via handleOps.
 *
 *      Supports two signing modes:
 *      - Owner mode: pass sessionSigner = address(0) and sessionSignerKey = 0
 *        to sign with the default account from HelperConfig (or the anvil key on chain 31337)
 *      - Session key mode: pass a valid sessionSigner address and its corresponding
 *        private key to sign as a delegated session key
 *
 *      Signing flow:
 *      1. Fetch nonce from EntryPoint
 *      2. Build unsigned PackedUserOperation
 *      3. Get userOpHash from EntryPoint (EIP-712 typed structured data hash)
 *      4. Wrap in EIP-191 envelope via toEthSignedMessageHash
 *      5. Sign the digest with vm.sign
 *      6. Attach (r, s, v) signature to the UserOp
 */
contract SendPackedUserOp is Script {
    using MessageHashUtils for bytes32;

    /**
     * @notice Generates a signed ERC-4337 PackedUserOperation ready for EntryPoint submission
     * @dev Determines the signing key based on whether a session signer is provided:
     *      - If sessionSigner == address(0) and sessionSignerKey == 0: signs as owner
     *        using config.account. On Anvil (chainid 31337) uses the hardcoded default key.
     *      - Otherwise: signs as the provided session key using sessionSignerKey.
     *
     *      The digest is constructed as:
     *      keccak256("\x19Ethereum Signed Message:\n32" || userOpHash)
     *
     * @param sender The smart account address that will send the UserOp (i.e. SessionHandler)
     * @param config Network configuration containing entryPoint address and default account
     * @param callData Encoded calldata to be executed by the smart account (e.g. execute(...))
     * @param sessionSigner Address of the session key signer. Pass address(0) to use owner mode
     * @param sessionSignerKey Private key of the session signer. Pass 0 to use owner mode
     * @return userOp The fully constructed and signed PackedUserOperation
     * @return userOpHash The raw EIP-712 hash of the UserOp as returned by the EntryPoint
     * @return digest The EIP-191 wrapped digest that was actually signed
     */
    function generateSignedUserOp(
        address sender,
        HelperConfig.NetworkConfig memory config,
        bytes calldata callData,
        address sessionSigner,
        uint256 sessionSignerKey
    ) external view returns (PackedUserOperation memory, bytes32, bytes32) {
        uint256 privateKey;
        address signer;

        if (sessionSigner == address(0) && sessionSignerKey == 0) {
            // Owner mode: use the default account from config
            signer = config.account;
            if (block.chainid == 31337) {
                // Anvil default private key for address 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
                privateKey = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
            }
        } else {
            // Session key mode: use the provided signer and key
            signer = sessionSigner;
            privateKey = sessionSignerKey;
        }

        uint256 nonce = IEntryPoint(config.entryPoint).getNonce(sender, 0);

        // 1. Build the unsigned UserOp with hardcoded gas parameters
        PackedUserOperation memory userOp = _generateUnsignedUserOp(sender, nonce, callData);

        // 2. Get the EIP-712 typed structured data hash from the EntryPoint
        bytes32 userOpHash = IEntryPoint(config.entryPoint).getUserOpHash(userOp);

        // 3. Wrap in EIP-191 envelope to produce the signable digest
        bytes32 digest = userOpHash.toEthSignedMessageHash();

        // 4. Sign the digest — vm.sign returns v as 0 or 1 on Anvil, adjusted to 27/28
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);

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
     *
     * @param sender The smart account address submitting the operation
     * @param nonce The current nonce for the sender as returned by the EntryPoint
     * @param callData The encoded function call to execute on the smart account
     * @return userOp An unsigned PackedUserOperation ready to be hashed and signed
     */
    function _generateUnsignedUserOp(address sender, uint256 nonce, bytes calldata callData)
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
