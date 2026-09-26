// SPDX-License-Identifier: BUSL-1.1
// Copyright (C) 2026 Conrad Japhet
// Use of this software is governed by the Business Source License included in the LICENSE file.
// Change Date: 2029-06-12. Change License: MIT.
pragma solidity ^0.8.24;

import {Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils, Mode, CallType} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";

/// @notice An owner-signed, per-key authorization. The owner signs it ONCE off-chain; it is verified
///         on-chain from calldata with no service in the loop. It extends a session key from a bare
///         signer (bounded only by the USD cap, {SessionHandler-_requireUnrestrictedTarget}, and
///         expiry) to one admissible only for an explicit set of (target, selector) pairs.
struct SessionGrant {
    address account; // the wallet; also the EIP-712 verifyingContract
    address sessionKey; // must equal SessionHandler.currentSession
    bytes32 callsRoot; // merkle root over allowed (target, selector) leaves
    uint48 validUntil; // grant expiry (inclusive, matching the key's own <= semantics)
    uint256 grantNonce; // owner-bumpable => instant revoke of every prior grant
}

/**
 * @title SessionGrantLib
 * @notice Reference implementation for sh-protocol#1: per-key (target, selector) scope for session
 *         keys, closing the gap the USD cap structurally cannot see (a key may only *value*-spend
 *         under the cap, but today may call ANY selector on any non-restricted target -- THREAT_MODEL
 *         §3.13). The admission decision is made a *validate-time*, per-call, recomputable check:
 *         given the same grant and calldata, any party recomputes the identical PASS/REVERT.
 *
 * @dev This is additive and non-invasive: it does NOT change {SessionHandler} storage or the ABI of
 *      {execute}. It is written against the SAME decode path {SessionHandler-_guardSessionExecution}
 *      already uses ({ERC7579Utils-decodeMode} -> SINGLE / BATCH / DELEGATECALL), so it can be lifted
 *      in directly. The intended wiring, once the owner-signed grant + proofs ride alongside the op:
 *
 *      ```solidity
 *      // inside SessionHandler, for a non-owner (session-key) execution:
 *      // 1. account-level checks (domain, key == currentSession, expiry, nonce, owner EIP-712 sig)
 *      //    stay in the account, which already is an EIP712 + Ownable context; then:
 *      SessionGrantLib.checkScope(execMode, executionCalldata, grant.callsRoot, proofs);
 *      // existing address-granular guard remains as defence-in-depth:
 *      // _guardSessionExecution(execMode, executionCalldata);
 *      ```
 *
 * @dev Why merkle, not an on-chain mapping: an owner can authorize an arbitrarily large scope with a
 *      single off-chain signature (one 65-byte sig + O(log n) proof per call), rather than one owner
 *      transaction per (target, selector) as {SessionHandler-addAllowedTargets} requires for the
 *      address-granular list. Leaves are `keccak256(bytes20(target) ++ bytes4(selector))`; the root
 *      is committed in the signed {SessionGrant}. No JCS / no sha256 -- everything is keccak/`abi`
 *      native to the verifier, so it stays cheap.
 * @dev Fail-closed: a call whose (target, selector) is not provable against `callsRoot` reverts; a
 *      batch reverts atomically on the first out-of-scope call; delegatecall is refused outright,
 *      exactly as {SessionHandler-_guardSessionExecution} refuses it (delegated code runs in the
 *      account's context and could reach the admin surface regardless of the encoded target).
 * @author Contributed by Shxnque (Quelum Wilson) against sh-protocol#1.
 */
library SessionGrantLib {
    /// @dev A sub-call's (target, selector) is not provable against the grant's committed root.
    error SelectorNotGranted(address target, bytes4 selector);
    /// @dev Delegatecall is never in scope for a session key (mirrors {SessionHandler}).
    error SessionDelegateCallForbidden();

    /// @dev EIP-712 struct type hash for {SessionGrant}. The account combines this with its own
    ///      domain separator ({EIP712-_hashTypedDataV4}) to bind the grant to (name, version,
    ///      chainid, verifyingContract) -- so a grant cannot be replayed across wallets or chains.
    bytes32 internal constant GRANT_TYPEHASH =
        keccak256("SessionGrant(address account,address sessionKey,bytes32 callsRoot,uint48 validUntil,uint256 grantNonce)");

    /// @notice The EIP-712 struct hash (inner hash) of a grant. Feed to {EIP712-_hashTypedDataV4}.
    function hashStruct(SessionGrant calldata g) internal pure returns (bytes32) {
        return keccak256(abi.encode(GRANT_TYPEHASH, g.account, g.sessionKey, g.callsRoot, g.validUntil, g.grantNonce));
    }

    /// @notice The merkle leaf for an admissible (target, selector) pair.
    function leaf(address target, bytes4 selector) internal pure returns (bytes32) {
        return keccak256(bytes.concat(bytes20(target), bytes4(selector)));
    }

    /// @notice The 4-byte selector of an ERC-7579 sub-call's calldata, or 0x00000000 if it carries
    ///         fewer than 4 bytes (a bare value transfer). A value transfer must be committed as the
    ///         zero selector to be admissible, so it is never silently allowed.
    function selectorOf(bytes calldata cd) internal pure returns (bytes4) {
        return cd.length >= 4 ? bytes4(cd[:4]) : bytes4(0);
    }

    /// @notice Reverts unless every sub-call in `executionCalldata` is provable against `callsRoot`.
    /// @dev Same decode path as {SessionHandler-_guardSessionExecution}; `proofs[i]` is the merkle
    ///      proof for sub-call `i` (for SINGLE, `proofs[0]`). Pure: decode + merkle verification are
    ///      all calldata/hash operations, so this is safe to call during ERC-4337 validation.
    function checkScope(Mode mode, bytes calldata executionCalldata, bytes32 callsRoot, bytes32[][] calldata proofs)
        internal
        pure
    {
        (CallType callType,,,) = ERC7579Utils.decodeMode(mode);
        if (callType == ERC7579Utils.CALLTYPE_SINGLE) {
            (address target,, bytes calldata cd) = ERC7579Utils.decodeSingle(executionCalldata);
            _admit(callsRoot, target, selectorOf(cd), proofs[0]);
        } else if (callType == ERC7579Utils.CALLTYPE_BATCH) {
            Execution[] calldata batch = ERC7579Utils.decodeBatch(executionCalldata);
            for (uint256 i; i < batch.length; ++i) {
                _admit(callsRoot, batch[i].target, selectorOf(batch[i].callData), proofs[i]);
            }
        } else if (callType == ERC7579Utils.CALLTYPE_DELEGATECALL) {
            revert SessionDelegateCallForbidden();
        }
    }

    function _admit(bytes32 root, address target, bytes4 selector, bytes32[] calldata proof) private pure {
        if (!MerkleProof.verifyCalldata(proof, root, leaf(target, selector))) {
            // forge-lint: disable-next-line(require-revert-in-loop)
            revert SelectorNotGranted(target, selector);
        }
    }
}
