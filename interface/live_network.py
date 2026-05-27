import secrets
import time
import requests
from dotenv import load_dotenv
from web3.contract import Contract
from network_config import load_network_config
from contracts import (
    load_session_handler,
    load_entry_point,
)
import db
from vault_signer import encrypt_key, decrypt_key

load_dotenv()

# Placeholder gas limits for the dummy op sent to eth_estimateUserOperationGas.
# verificationGasLimit and callGasLimit are kept separate: verification is ECDSA +
# storage reads (~50-100k), while the call may be complex (swap, liquidity, etc.).
# Keeping the verification dummy tight prevents bundlers from echoing an inflated
# value back as the estimate, which would fail the bundler's efficiency check.
DUMMY_VERIFICATION_GAS = 150_000
DUMMY_CALL_GAS = 500_000
DUMMY_PRE_VERIFICATION_GAS = 50_000

GAS_BUFFER_MULTIPLIER = 1.2

USER_OP_RECEIPT_TIMEOUT_SECS = 600
USER_OP_POLL_INTERVAL_SECS = 2


def get_or_create_session_key(chat_id: int, target_address: str) -> tuple[str, str]:
    """
    Returns the session key address and Vault ciphertext for a given user and target.

    On first call for a (chat_id, target_address) pair, generates a cryptographically
    random 32-byte private key, encrypts it via Vault Transit, stores the ciphertext in
    the DB, and wipes the raw key from memory. On subsequent calls, returns the stored
    (address, ciphertext) directly without touching Vault.

    @param chat_id         The Telegram chat ID of the user.
    @param target_address  The contract address the session key is scoped to.
    @return                A tuple of (session_key_address, vault_ciphertext).
    """
    row = db.get_session_key(chat_id, target_address)
    if row:
        return row

    w3, _, _ = load_network_config(chat_id)
    raw_key = secrets.token_bytes(32)
    account = w3.eth.account.from_key(raw_key)
    ciphertext = encrypt_key(raw_key)
    raw_key = b"\x00" * 32
    db.save_session_key(chat_id, target_address, account.address, ciphertext)
    return account.address, ciphertext


def _packed_user_op_to_rpc_json(user_op: tuple) -> dict:
    """
    Unpacks a PackedUserOperation tuple into the JSON object expected by bundler RPC methods.

    The on-chain PackedUserOperation packs two 128-bit gas values into single bytes32
    fields. The bundler RPC (eth_sendUserOperation, eth_estimateUserOperationGas) expects
    them as separate hex strings in the ERC-4337 v0.7 unpacked format.

    @param user_op  A PackedUserOperation tuple (signed or unsigned).
    @return         A dict ready to be passed as a bundler RPC parameter.
    """
    (
        sender,
        nonce,
        init_code,
        call_data,
        account_gas_limits,
        pre_verification_gas,
        gas_fees,
        paymaster_and_data,
        signature,
    ) = user_op

    # accountGasLimits = verificationGasLimit (upper 128 bits) | callGasLimit (lower 128 bits)
    account_gas_limits_int = int.from_bytes(account_gas_limits, "big")
    verification_gas_limit = account_gas_limits_int >> 128
    call_gas_limit = account_gas_limits_int & ((1 << 128) - 1)

    # gasFees = maxFeePerGas (upper 128 bits) | maxPriorityFeePerGas (lower 128 bits)
    gas_fees_int = int.from_bytes(gas_fees, "big")
    max_fee_per_gas = gas_fees_int >> 128
    max_priority_fee_per_gas = gas_fees_int & ((1 << 128) - 1)

    call_data_bytes = call_data if isinstance(call_data, bytes) else bytes(call_data)
    sig_bytes = signature if isinstance(signature, bytes) else bytes(signature)
    init_code_bytes = init_code if isinstance(init_code, bytes) else bytes(init_code)
    paymaster_bytes = paymaster_and_data if isinstance(paymaster_and_data, bytes) else bytes(paymaster_and_data)

    op = {
        "sender": sender,
        "nonce": hex(nonce),
        "callData": "0x" + call_data_bytes.hex(),
        "callGasLimit": hex(call_gas_limit),
        "verificationGasLimit": hex(verification_gas_limit),
        "preVerificationGas": hex(pre_verification_gas),
        "maxFeePerGas": hex(max_fee_per_gas),
        "maxPriorityFeePerGas": hex(max_priority_fee_per_gas),
        "signature": "0x" + sig_bytes.hex(),
    }

    if init_code_bytes:
        op["factory"] = "0x" + init_code_bytes[:20].hex()
        op["factoryData"] = "0x" + init_code_bytes[20:].hex()

    if paymaster_bytes:
        op["paymaster"] = "0x" + paymaster_bytes[:20].hex()
        op["paymasterVerificationGasLimit"] = hex(int.from_bytes(paymaster_bytes[20:36], "big"))
        op["paymasterPostOpGasLimit"] = hex(int.from_bytes(paymaster_bytes[36:52], "big"))
        op["paymasterData"] = "0x" + paymaster_bytes[52:].hex()

    return op


def _bundler_rpc(rpc_url: str, method: str, params: list) -> dict:
    """
    Sends a JSON-RPC request to the bundler endpoint and returns the full response dict.

    Raises RuntimeError if the response contains an error field. For methods where a
    null result is valid (e.g. eth_getUserOperationReceipt before inclusion), callers
    should check response.get("result") rather than response["result"].

    @param rpc_url  The Alchemy (or other bundler) RPC endpoint URL.
    @param method   The JSON-RPC method name (e.g. "eth_sendUserOperation").
    @param params   The params array for the JSON-RPC call.
    @return         The full parsed JSON-RPC response dict.
    @raises RuntimeError on HTTP errors, empty bodies, or JSON-RPC error responses.
    """
    response = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"{method} HTTP {response.status_code}: {response.text[:200]}"
        )
    if not response.text.strip():
        raise RuntimeError(f"{method} returned an empty response body")
    result = response.json()
    if "error" in result:
        print(f"[Bundler RPC Error] {method}: {result['error']}")
        raise RuntimeError(f"{method} failed: {result['error']}")
    return result


def create_unsigned_user_op(
    chat_id: int,
    session_handler: Contract,
    key_ciphertext: str,
    entry_point: Contract,
    nonce: int,
    calldata: str,
) -> tuple:
    """
    Constructs an unsigned ERC-4337 PackedUserOperation with bundler-estimated gas limits.

    Builds a signed dummy op with placeholder gas limits, submits it to
    eth_estimateUserOperationGas to get accurate per-component limits, then constructs
    the final unsigned op with a 20% buffer applied to each limit and the live gas price.

    @param chat_id         The Telegram chat ID of the user.
    @param session_handler Bound SessionHandler contract (the UserOp sender).
    @param key_ciphertext  Vault Transit ciphertext for the session key ('vault:v1:...').
    @param entry_point     Bound EntryPoint contract.
    @param nonce           The sender's current nonce from the EntryPoint.
    @param calldata        Hex-encoded SessionHandler.execute() calldata (0x-prefixed).
    @return                An unsigned PackedUserOperation tuple (empty signature field).
    """
    w3, _, _ = load_network_config(chat_id)
    rpc_url = str(w3.provider.endpoint_uri)

    gas_price = w3.eth.gas_price
    dummy_op = (
        session_handler.address,
        nonce,
        b"",
        bytes.fromhex(calldata[2:]),
        (DUMMY_VERIFICATION_GAS << 128 | DUMMY_CALL_GAS).to_bytes(32, "big"),
        DUMMY_PRE_VERIFICATION_GAS,
        (gas_price << 128 | gas_price).to_bytes(32, "big"),
        b"",
        b"",
    )

    signed_dummy_op = create_signed_user_op(
        chat_id=chat_id,
        user_op=dummy_op,
        entry_point=entry_point,
        key_ciphertext=key_ciphertext,
    )

    estimates = _bundler_rpc(
        rpc_url,
        "eth_estimateUserOperationGas",
        [_packed_user_op_to_rpc_json(signed_dummy_op), entry_point.address],
    )["result"]

    call_gas_limit = int(int(estimates["callGasLimit"], 16) * GAS_BUFFER_MULTIPLIER)
    verification_gas_limit = int(estimates["verificationGasLimit"], 16)
    pre_verification_gas = int(int(estimates["preVerificationGas"], 16) * GAS_BUFFER_MULTIPLIER)

    # Re-fetch gas price after estimation so the final op uses the latest base fee.
    # Apply the same buffer to stay above the bundler's minimum even if the fee ticks up slightly.
    fresh_gas_price = int(w3.eth.gas_price * GAS_BUFFER_MULTIPLIER)
    account_gas_limits = (verification_gas_limit << 128 | call_gas_limit).to_bytes(32, "big")
    gas_fees = (fresh_gas_price << 128 | fresh_gas_price).to_bytes(32, "big")

    return (
        session_handler.address,
        nonce,
        b"",
        bytes.fromhex(calldata[2:]),
        account_gas_limits,
        pre_verification_gas,
        gas_fees,
        b"",
        b"",
    )


def create_signed_user_op(
    chat_id: int, user_op: tuple, entry_point: Contract, key_ciphertext: str
) -> tuple:
    """
    Signs a PackedUserOperation with a session key using EIP-191 message signing.

    Fetches the userOpHash from the EntryPoint, wraps it in the Ethereum signed
    message envelope via encode_defunct (matching toEthSignedMessageHash in
    SessionHandler._validateSignature), and returns the op with the signature attached.

    The raw private key is decrypted from Vault transiently and wiped from memory
    immediately after signing.

    @param chat_id        The Telegram chat ID of the user.
    @param user_op        An unsigned PackedUserOperation tuple (empty signature field).
    @param entry_point    Bound EntryPoint contract.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @return               A signed PackedUserOperation tuple ready for submission.
    """
    from eth_account.messages import encode_defunct

    w3, _, _ = load_network_config(chat_id)
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


def send_live_user_op_as_session(
    chat_id: int, key_ciphertext: str, target: str, value: int, data: bytes
):
    """
    Orchestrates the full ERC-4337 UserOperation flow for a session key holder.

    Encodes SessionHandler.execute() as the UserOp calldata, estimates gas via the
    bundler, builds and signs a PackedUserOperation, submits it via eth_sendUserOperation,
    and polls for inclusion via eth_getUserOperationReceipt.

    No bundler EOA is required — the Alchemy bundler handles submission and gas payment.

    @param chat_id        The Telegram chat ID of the user.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param target         The contract address SessionHandler will call (e.g. USDC).
    @param value          The ETH value in wei to forward with the inner call.
    @param data           ABI-encoded inner calldata to execute on the target.
    @return               A tuple of (user_op_hash_bytes, receipt) where receipt["status"]
                          is 1 on success, matching the return shape of anvil.py for
                          compatibility with tools.py callers.
    """
    w3, _, _ = load_network_config(chat_id)
    rpc_url = str(w3.provider.endpoint_uri)

    session_handler = load_session_handler(chat_id=chat_id)
    calldata = session_handler.encode_abi(
        abi_element_identifier="execute", args=[target, value, data]
    )

    entry_point = load_entry_point(chat_id=chat_id)
    nonce = entry_point.functions.getNonce(session_handler.address, 0).call()

    print("\n[1/3] Creating transaction  ...")
    user_op = create_unsigned_user_op(
        chat_id=chat_id,
        session_handler=session_handler,
        key_ciphertext=key_ciphertext,
        entry_point=entry_point,
        nonce=nonce,
        calldata=calldata,
    )

    print("[2/3] Signing transaction   ...")
    user_op_signed = create_signed_user_op(
        chat_id=chat_id,
        user_op=user_op,
        entry_point=entry_point,
        key_ciphertext=key_ciphertext,
    )

    print("[3/3] Sending to bundler    ...")
    user_op_json = _packed_user_op_to_rpc_json(user_op_signed)
    user_op_hash = _bundler_rpc(
        rpc_url,
        "eth_sendUserOperation",
        [user_op_json, entry_point.address],
    )["result"]

    print(f"UserOp hash: {user_op_hash} — polling for inclusion ...")
    deadline = time.time() + USER_OP_RECEIPT_TIMEOUT_SECS
    bundler_receipt = None
    while time.time() < deadline:
        try:
            resp = _bundler_rpc(rpc_url, "eth_getUserOperationReceipt", [user_op_hash])
            if resp.get("result") is not None:
                bundler_receipt = resp["result"]
                break
        except RuntimeError as exc:
            # Transient bundler errors (rate limits, empty bodies) are retryable;
            # the op was already submitted so keep polling until the deadline.
            print(f"[poll] transient error, retrying: {exc}")
        time.sleep(USER_OP_POLL_INTERVAL_SECS)

    if bundler_receipt is None:
        raise TimeoutError(
            f"UserOperation {user_op_hash} was not included within {USER_OP_RECEIPT_TIMEOUT_SECS}s"
        )

    if not bundler_receipt["success"]:
        reason = bundler_receipt.get("reason") or "no revert reason provided"
        raise RuntimeError(
            f"UserOperation inner call failed "
            f"(userOpHash={user_op_hash}, "
            f"actualGasCost={bundler_receipt.get('actualGasCost')} wei, "
            f"reason={reason!r}). "
            f"The op was included but the inner call reverted."
        )

    op_hash_bytes = bytes.fromhex(user_op_hash[2:])
    return op_hash_bytes, {"status": 1}
