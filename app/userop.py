"""
Building blocks for the wallet's UserOperations: the execute() calldata, the nonce, the hash a
passkey signs, and which passkeys the wallet approves.

The server holds no session key any more. A session key is a passkey on the user's device, so there
is nothing here to mint, store, promote or sign with: the wallet itself says which passkeys it
approves and until when (SessionHandler.getSessions), and the app's own records are only the names
the user gave them (db.passkeys). bundler.py layers gas estimation and submission on top; signing.py
turns an approval into the op's signature.
"""
import secrets
import time
from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3.contract import Contract

from constants import MAX_SESSION_KEYS, MAX_SESSION_TTL_SECS, SESSION_RENEWAL_WARNING_SECS
from contracts import (
    ERC7579_BATCH_CALL_MODE,
    ERC7579_SINGLE_CALL_MODE,
    encode_batch_execution_calldata,
    load_entry_point,
    load_session_handler,
    pack_execution_calldata,
)
from db import get_passkeys
from tx_sender import ATTEMPT_TIMEOUT_SECS, MAX_FEE_BUMPS


# How long past its sign request's expiry a signed op stays valid on chain. The request can be
# approved up to its last second, and the send that follows may run every fee bump (tx_sender) before
# it gives up; a minute more covers a clock that differs a little from the chain's. Past it, the
# wallet refuses the op (AA22) however it reaches the chain.
OP_DEADLINE_GRACE_SECS = (MAX_FEE_BUMPS + 1) * ATTEMPT_TIMEOUT_SECS + 60


def passkey_nonce(op_deadline: int) -> int:
    """
    A fresh nonce for a passkey-signed op: its own deadline, a random lane, sequence 0 (spec 3).

    An ERC-4337 nonce is (key, sequence), and the EntryPoint keeps a sequence per key. Every
    passkey-signed op takes a key nobody has used, so ops never queue behind each other: two
    Confirms at once -- two quotes, or two devices -- can't take the same nonce, and an op that is
    never sent leaves no gap for the next one to wait on. Nothing may assume a session op's nonce
    follows the last one's.

    The key is `op_deadline` (48 bits) followed by 144 random bits. SessionHandler reads the top 48
    bits as the op's own deadline and refuses a passkey op whose deadline is 0; the op is valid until
    the earlier of it and the passkey's approval. Without it, nothing would ever use a random lane
    again, so a signed op that was never sent would stay valid until the passkey's approval ran out
    -- up to 90 days. The nonce is inside the hash the passkey signs, so nobody can move it.

    @param op_deadline  Unix time after which the wallet refuses the op. Must fit 48 bits, non-zero.
    """
    if not 0 < op_deadline < 2**48:
        raise ValueError(f"op deadline {op_deadline} is not a non-zero uint48")
    return ((op_deadline << 144) | secrets.randbits(144)) << 64


def build_execute_calldata(
    user_id: int, executions: list[tuple[str, int, bytes]]
) -> tuple[Contract, Contract, str]:
    """
    The bound SessionHandler and EntryPoint, and the SessionHandler.execute(mode, executionCalldata)
    calldata that carries `executions`.

    One call goes out as an ERC-7579 single execution. Anything longer is a batch, which is not an
    optimisation -- it is the only way an approval can be granted and consumed without tripping
    SpendingLimitModule's no-standing-approval rule, so [approve, spend, (reset)] lands atomically.

    @param user_id     The application user ID.
    @param executions  [(target_address, value_wei, calldata_bytes), ...], in order.
    @return            (session_handler, entry_point, calldata_hex).
    """
    session_handler = load_session_handler(user_id=user_id)
    if len(executions) == 1:
        mode, execution_calldata = ERC7579_SINGLE_CALL_MODE, pack_execution_calldata(*executions[0])
    else:
        mode, execution_calldata = ERC7579_BATCH_CALL_MODE, encode_batch_execution_calldata(executions)
    calldata = session_handler.encode_abi(abi_element_identifier="execute", args=[mode, execution_calldata])
    return session_handler, load_entry_point(user_id=user_id), calldata


# (chain_id, EntryPoint address) -> whether hash_user_op's own hash has matched the EntryPoint's.
# Missing until the first op on that chain is checked; False means "ask the EntryPoint every time".
_local_hash_matches: dict[tuple[int, str], bool] = {}


def hash_user_op(op: tuple, entry_point: Contract, chain_id: int) -> bytes:
    """
    The op's userOpHash: what the passkey signs (the WebAuthn challenge) and what its receipt is
    found by.

    Computed here rather than asked of the EntryPoint: ERC-4337 v0.7 defines it as
    keccak256(abi.encode(keccak256(UserOperationLib.encode(op)), entryPoint, chainId)), which is pure
    arithmetic, and asking cost a round trip on every quote and every send. The first op on each
    (chain, EntryPoint) is checked against EntryPoint.getUserOpHash. If they ever disagree -- a
    different EntryPoint version hashes differently -- the EntryPoint is asked every time after.

    @param op           A PackedUserOperation tuple (its signature is not part of the hash).
    @param entry_point  Bound EntryPoint contract.
    @param chain_id     The chain the op is for.
    @return             The 32-byte hash.
    """
    key = (chain_id, entry_point.address)
    if _local_hash_matches.get(key) is False:
        return bytes(entry_point.functions.getUserOpHash(op).call())
    local = _v07_user_op_hash(op, entry_point.address, chain_id)
    if key not in _local_hash_matches:
        remote = bytes(entry_point.functions.getUserOpHash(op).call())
        _local_hash_matches[key] = remote == local
        if remote != local:
            print(f"[userop] EntryPoint {entry_point.address} hashes ops differently; asking it from now on")
            return remote
    return local


def _v07_user_op_hash(op: tuple, entry_point_address: str, chain_id: int) -> bytes:
    """EntryPoint v0.7's getUserOpHash, in Python. See UserOperationLib.encode and EntryPoint.getUserOpHash."""
    sender, nonce, init_code, call_data, account_gas_limits, pre_verification_gas, gas_fees, paymaster_and_data, _ = op
    packed = abi_encode(
        ["address", "uint256", "bytes32", "bytes32", "bytes32", "uint256", "bytes32", "bytes32"],
        [
            sender, nonce, keccak(init_code), keccak(call_data), account_gas_limits,
            pre_verification_gas, gas_fees, keccak(paymaster_and_data),
        ],
    )
    return keccak(abi_encode(["bytes32", "address", "uint256"], [keccak(packed), entry_point_address, chain_id]))


# ── Which passkeys the wallet approves ────────────────────────────────────────


@dataclass(frozen=True)
class SessionKey:
    """One passkey the wallet approves, as getSessions reports it."""

    key_id: str          # 0x hex, lowercase: keccak256(abi.encode(qx, qy))
    valid_until: int     # Unix seconds; the wallet accepts the key through this second

    def active(self, now: int, margin: int = 0) -> bool:
        """
        Whether the wallet accepts the key `margin` seconds from now: isSessionActive's own test
        (validUntil != 0 and the time at or before it), shifted by the margin a caller needs to
        get an op signed and mined before it runs out.
        """
        return self.valid_until != 0 and now + margin <= self.valid_until


def read_sessions(wallet: Contract) -> list[SessionKey]:
    """
    Every passkey the wallet approves, from SessionHandler.getSessions() -- expired ones included:
    the wallet keeps a key in its set until it is removed, or until a new one needs the room.

    @param wallet  A bound SessionHandler.
    """
    key_ids, valid_untils = wallet.functions.getSessions().call()
    return [SessionKey("0x" + bytes(key).hex(), int(until)) for key, until in zip(key_ids, valid_untils)]


def session_keys(user_id: int, sessions: list[SessionKey], now: int | None = None) -> list[dict]:
    """
    The wallet's passkeys for the dashboard, each with the name the user gave it here.

    A key the app doesn't know -- granted on chain directly, or from another account's device --
    still shows, with no name: the wallet trusts it, so the owner must be able to see it to remove
    it.

    @return  [{"key_id", "device_name", "credential_id", "valid_until", "expires_in_secs",
              "active", "needs_renewal"}], in the wallet's order.
    """
    now = int(time.time()) if now is None else now
    known = {row["key_id"]: row for row in get_passkeys(user_id)}
    keys = []
    for session in sessions:
        row = known.get(session.key_id)
        active = session.active(now)
        left = max(session.valid_until - now, 0)
        keys.append({
            "key_id": session.key_id,
            "device_name": row["device_name"] if row else None,
            "credential_id": row["credential_id"] if row else None,
            "valid_until": session.valid_until,
            "expires_in_secs": left,
            "active": active,
            # True once it is worth prompting the owner to renew, while the key still works.
            "needs_renewal": active and left < SESSION_RENEWAL_WARNING_SECS,
        })
    return keys


def session_block(user_id: int, sessions: list[SessionKey], now: int | None = None) -> dict:
    """
    The wallet read's `session` block (spec 5): the wallet's limits on keys, and each key.
    """
    return {
        "max_keys": MAX_SESSION_KEYS,
        "max_ttl_secs": MAX_SESSION_TTL_SECS,
        "renewal_warning_secs": SESSION_RENEWAL_WARNING_SECS,
        "keys": session_keys(user_id, sessions, now),
    }
