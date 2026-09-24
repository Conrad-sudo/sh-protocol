"""
Building blocks for the ERC-4337 session-key flow: session-key management, UserOp calldata and
nonce, and signing.

bundler.py -- the app's own bundler, used on every network -- layers gas estimation and
submission on top of these. Keeping key management, op construction and signing here means there
is exactly one place to fix a signing or nonce bug.
"""
import secrets
from dotenv import load_dotenv
from eth_account import Account
from web3.contract import Contract
from network_config import load_network_config
from contracts import (
    load_session_handler,
    load_entry_point,
    load_spending_limit_module,
    pack_execution_calldata,
    encode_batch_execution_calldata,
    session_key_nonce_key,
    ERC7579_SINGLE_CALL_MODE,
    ERC7579_BATCH_CALL_MODE,
)
import db
from vault_signer import encrypt_key, decrypt_key

load_dotenv()


def get_session_key_or_none(user_id: int, chain_id: int, target_address: str) -> tuple[str, str] | None:
    """
    Returns the session key address and Vault ciphertext this app holds for a wallet, or None.

    READ ONLY, deliberately. It used to mint a key when the row was missing, which is fine at deploy
    time and actively dangerous everywhere else: once a revocation deletes the row, the next agent
    action would silently create a brand-new key, store it, sign with it, and have every UserOp
    rejected by a wallet that never authorized it -- while the database looked perfectly healthy.
    Minting is now an explicit step (see {create_pending_session_key}) that only the grant flows
    take.

    `chain_id` is explicit rather than resolved from `user_id` on purpose. deploy_wallet needs a key
    for the chain it is deploying TO, which is not necessarily the chain the user is currently
    pointed at, and resolving it here would silently look up the wrong one. It is also what keeps a
    user's per-chain wallets on separate keys when they share an address across chains.

    @param user_id         The application user ID.
    @param chain_id        The chain the key is authorized on.
    @param target_address  The wallet address the session key is held for.
    @return                A tuple of (session_key_address, vault_ciphertext), or None.
    """
    return db.get_session_key(user_id, chain_id, target_address)


def create_pending_session_key(user_id: int, chain_id: int, target_address: str) -> tuple[str, str]:
    """
    Mints a FRESH session key for a grant the owner has not signed yet.

    Always a new key, never the one already held: a key is revoked precisely when it might be
    compromised, so handing the same address back on the next grant would undo the revocation. The
    old key is not touched here either -- it stays live until the new grant mines, which is what
    keeps the assistant working in between and what makes an abandoned, unsigned grant harmless.

    Stored in `pending_session_keys`; {promote_pending_session_key} moves it across once the chain
    confirms the wallet actually authorized it.

    Nothing here touches the network. Deriving an address from a private key is pure secp256k1
    arithmetic, so `eth_account` does it without an RPC -- which matters beyond tidiness: this used
    to call load_network_config(user_id), and that reads the user's SAVED network. A first-time user
    of POST /api/deploy has no saved network yet (it is only written in /api/deploy/confirm, once
    the wallet actually exists), so minting their very first session key raised
    "No network configured for user N". The API could never onboard anybody.

    @param user_id         The application user ID.
    @param chain_id        The chain the key will be authorized on.
    @param target_address  The wallet address the key is being minted for.
    @return                A tuple of (session_key_address, vault_ciphertext).
    """
    raw_key = secrets.token_bytes(32)
    account = Account.from_key(raw_key)
    ciphertext = encrypt_key(raw_key)
    raw_key = b"\x00" * 32
    db.save_pending_session_key(user_id, chain_id, target_address, account.address, ciphertext)
    return account.address, ciphertext


def promote_pending_session_key(user_id: int, chain_id: int, target_address: str) -> tuple[str, str] | None:
    """
    Makes the outstanding grant's key the wallet's live key, now that the chain has confirmed it.

    Callers MUST have read `currentSession` off the wallet first and found it equal to the pending
    address; this function does no chain check of its own.

    @return  The promoted (address, ciphertext), or None if no grant was outstanding.
    """
    row = db.get_pending_session_key(user_id, chain_id, target_address)
    if not row:
        return None
    db.save_session_key(user_id, chain_id, target_address, row[0], row[1])
    db.delete_pending_session_key(user_id, chain_id, target_address)
    return row


def reconcile_session_key(
    user_id: int, chain_id: int, target_address: str, on_chain_key: str | None
) -> tuple[str, str] | None:
    """
    Makes the app's key records agree with the key the wallet actually authorizes.

    The one place those records change once a grant or revocation has mined. The confirm endpoint
    calls it straight after the owner's transaction; every wallet read calls it too, as the safety
    net for a confirm that never came -- a closed tab, a dropped connection. Without that, the
    assistant would keep signing with a key the wallet had already evicted until the owner happened
    to toggle it again. Idempotent, so running it from both places is harmless.

      - zero             -> revoked: forget the live key, ciphertext and all
      - the live key     -> already in step
      - the pending key  -> the grant landed: promote it
      - anything else    -> a key this app never minted: touch nothing

    A revocation leaves any PENDING key alone on purpose. A read can land between "Turn on" minting
    a key and the owner's signature, while the wallet still reads zero; deleting the pending row
    then would throw away the only copy of a key the owner is about to authorize.

    @param on_chain_key  The wallet's `currentSession`, or None when it is zero.
    @return              The live (address, ciphertext) afterwards, or None when the app holds none.
    """
    live = db.get_session_key(user_id, chain_id, target_address)
    if on_chain_key is None:
        if live:
            db.delete_session_key(user_id, chain_id, target_address)
        return None
    if live and live[0].lower() == on_chain_key.lower():
        return live
    pending = db.get_pending_session_key(user_id, chain_id, target_address)
    if pending and pending[0].lower() == on_chain_key.lower():
        return promote_pending_session_key(user_id, chain_id, target_address)
    return live


def current_session_nonce(user_id: int, session_handler: Contract, entry_point: Contract) -> int:
    """
    The wallet's next UserOp nonce on the session-key path.

    An ERC-4337 nonce is (key, sequence), and this account routes validation by the key: the one
    derived from the installed SpendingLimitModule is what tells it to run the session-key checks
    rather than the owner's. Read separately from prepare_execute_call because an op is quoted and
    signed at two different moments, and only the nonce can move in between.

    @param user_id         The application user ID.
    @param session_handler The user's SessionHandler (the UserOp sender).
    @param entry_point     Bound EntryPoint contract.
    @return                The nonce to build the next op with.
    """
    module = load_spending_limit_module(user_id=user_id)
    return entry_point.functions.getNonce(
        session_handler.address, session_key_nonce_key(module.address)
    ).call()


def prepare_execute_call(
    user_id: int, target: str, value: int, data: bytes
) -> tuple[Contract, Contract, str, int]:
    """
    Builds the shared inputs for a session-key UserOp, identical across both backends:
    the bound SessionHandler and EntryPoint contracts, the ABI-encoded
    SessionHandler.execute(mode, executionCalldata) calldata, and a nonce keyed to the
    installed SpendingLimitModule (so the account routes validation to it). The caller
    layers its own gas estimation and submission on top.

    @param user_id  The application user ID.
    @param target   The contract address SessionHandler will call.
    @param value    ETH value in wei to forward with the inner call.
    @param data     ABI-encoded inner calldata to execute on the target.
    @return         (session_handler, entry_point, calldata_hex, nonce).
    """
    session_handler = load_session_handler(user_id=user_id)

    execution_calldata = pack_execution_calldata(target, value, data)
    calldata = session_handler.encode_abi(
        abi_element_identifier="execute",
        args=[ERC7579_SINGLE_CALL_MODE, execution_calldata],
    )

    entry_point = load_entry_point(user_id=user_id)
    nonce = current_session_nonce(user_id, session_handler, entry_point)

    return session_handler, entry_point, calldata, nonce


def prepare_execute_batch_call(
    user_id: int, executions: list[tuple[str, int, bytes]]
) -> tuple[Contract, Contract, str, int]:
    """
    Batch variant of prepare_execute_call: builds SessionHandler.execute(batchMode,
    abi.encode(Execution[])) calldata for several sub-calls that must land atomically in ONE
    transaction — the only way an approval can be granted and consumed without tripping
    SpendingLimitModule's no-standing-approval rule.

    @param user_id     The application user ID.
    @param executions  List of (target_address, value_wei, calldata_bytes) triples, in order.
    @return            (session_handler, entry_point, calldata_hex, nonce).
    """
    session_handler = load_session_handler(user_id=user_id)

    execution_calldata = encode_batch_execution_calldata(executions)
    calldata = session_handler.encode_abi(
        abi_element_identifier="execute",
        args=[ERC7579_BATCH_CALL_MODE, execution_calldata],
    )

    entry_point = load_entry_point(user_id=user_id)
    nonce = current_session_nonce(user_id, session_handler, entry_point)

    return session_handler, entry_point, calldata, nonce


def create_signed_user_op(
    user_id: int, user_op: tuple, entry_point: Contract, key_ciphertext: str
) -> tuple:
    """
    Signs a PackedUserOperation with a session key using EIP-191 message signing.

    Fetches the userOpHash from the EntryPoint, wraps it in the Ethereum signed
    message envelope via encode_defunct (matching toEthSignedMessageHash in
    SessionHandler._rawSignatureValidation, the account's own UserOp signature check —
    the module is not a validator), and returns the op with the signature attached.

    The raw private key is decrypted from Vault transiently and wiped from memory
    immediately after signing.

    @param user_id        The application user ID.
    @param user_op        An unsigned PackedUserOperation tuple (empty signature field).
    @param entry_point    Bound EntryPoint contract.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @return               A signed PackedUserOperation tuple ready for submission.
    """
    from eth_account.messages import encode_defunct

    w3, _, _ = load_network_config(user_id)
    user_op_hash = entry_point.functions.getUserOpHash(user_op).call()
    raw_key = decrypt_key(key_ciphertext)
    try:
        signed = w3.eth.account.sign_message(
            encode_defunct(user_op_hash), private_key=raw_key
        )
        return user_op[:-1] + (signed.signature,)
    finally:
        raw_key = b"\x00" * len(raw_key)
        del raw_key
