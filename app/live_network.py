import time
import requests
from dotenv import load_dotenv
from web3.contract import Contract
from network_config import load_network_config
from userop import create_signed_user_op, prepare_execute_call, prepare_execute_batch_call

load_dotenv()

# Placeholder gas limits for the dummy op sent to eth_estimateUserOperationGas.
# verificationGasLimit and callGasLimit are kept separate: verification is ECDSA + a few
# storage reads (measured ~44k for this account's SessionHandler validation), while the call
# may be complex (swap, liquidity, etc.).
#
# DUMMY_VERIFICATION_GAS is echoed straight back by Alchemy's bundler as the estimate, so it
# effectively IS the submitted verificationGasLimit. eth_sendUserOperation then enforces a
# verification-gas efficiency floor (actualGasUsed / limit >= 0.4), i.e. the limit must be
# <= 2.5x actual. At 150_000 the limit was ~3.4x the ~44k actually used (efficiency ~0.29),
# which the bundler rejected. 55_000 sits ~25% above real usage (safe against on-chain
# out-of-gas) while keeping efficiency ~0.8 first-op and >0.4 for cheaper follow-up ops.
# Since every wallet shares one SessionHandler implementation, verification gas is uniform,
# so a single tuned constant is appropriate. Must stay >= real verification gas.
DUMMY_VERIFICATION_GAS = 65_000
DUMMY_CALL_GAS = 500_000
DUMMY_PRE_VERIFICATION_GAS = 50_000

GAS_BUFFER_MULTIPLIER = 1.2

USER_OP_RECEIPT_TIMEOUT_SECS = 600
USER_OP_POLL_INTERVAL_SECS = 2


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
    user_id: int,
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

    @param user_id         The application user ID.
    @param session_handler Bound SessionHandler contract (the UserOp sender).
    @param key_ciphertext  Vault Transit ciphertext for the session key ('vault:v1:...').
    @param entry_point     Bound EntryPoint contract.
    @param nonce           The sender's current nonce from the EntryPoint.
    @param calldata        Hex-encoded SessionHandler.execute() calldata (0x-prefixed).
    @return                An unsigned PackedUserOperation tuple (empty signature field).
    """
    w3, _, _ = load_network_config(user_id)
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
        user_id=user_id,
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

    # A UserOp's maxFeePerGas is fixed at submission, but Sepolia's base fee is highly volatile
    # (observed swinging ~1 <-> 40+ gwei within the hour). A thin buffer over the current price
    # gets stranded the moment the base fee climbs past it: the bundler can no longer include the
    # op without losing money, so it parks it until it's dropped (the 600s inclusion timeout).
    # Set the fee to 2x the current base fee + a tip so it survives a base-fee spike of up to ~2x.
    #
    # maxFeePerGas and maxPriorityFeePerGas are packed to the SAME value on purpose. The signed
    # hash comes from EntryPoint.getUserOpHash() over these packed bytes, but _packed_user_op_to_rpc_json
    # splits them and the bundler re-packs the halves in the opposite 128-bit order (per the
    # ERC-4337 v0.7 spec), so the on-chain hash only matches our signature while the two halves are
    # identical. Splitting them into a real (low) tip + (high) cap requires aligning that packing
    # order first (in _packed_user_op_to_rpc_json and here) — see the note on gasFees above.
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    try:
        tip = w3.eth.max_priority_fee
    except Exception:
        # Some nodes don't implement eth_maxPriorityFeePerGas; a 2 gwei tip is a safe default.
        tip = w3.to_wei(2, "gwei")
    fresh_gas_price = 2 * base_fee + tip
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


def send_live_user_op_as_session(
    user_id: int, key_ciphertext: str, target: str, value: int, data: bytes
):
    """
    Orchestrates the full ERC-4337 UserOperation flow for a session key holder.

    Packs (target, value, data) into ERC-7579 executionCalldata and encodes
    SessionHandler.execute(mode, executionCalldata) as the UserOp calldata, fetches a
    nonce keyed to the installed SpendingLimitModule (so the account routes validation to
    it), estimates gas via the bundler, builds and signs a PackedUserOperation, submits it
    via eth_sendUserOperation, and polls for inclusion via eth_getUserOperationReceipt.

    No bundler EOA is required — the Alchemy bundler handles submission and gas payment.

    @param user_id        The application user ID.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param target         The contract address SessionHandler will call (e.g. USDC).
    @param value          The ETH value in wei to forward with the inner call.
    @param data           ABI-encoded inner calldata to execute on the target.
    @return               A tuple of (user_op_hash_bytes, receipt) where receipt["status"]
                          is 1 on success, matching the return shape of anvil.py for
                          compatibility with tools.py callers.
    """
    session_handler, entry_point, calldata, nonce = prepare_execute_call(
        user_id, target, value, data
    )
    return _submit_user_op(user_id, key_ciphertext, session_handler, entry_point, calldata, nonce)


def send_live_batch_user_op_as_session(
    user_id: int, key_ciphertext: str, executions: list[tuple[str, int, bytes]]
):
    """
    Batch variant of send_live_user_op_as_session: submits several sub-calls as ONE atomic
    execute(batchMode, ...) UserOp via the bundler. Required for any flow that grants an
    approval — SpendingLimitModule reverts the whole transaction if an approval survives it,
    so [approve, spend(, approve 0)] must land together.

    @param user_id        The application user ID.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param executions     List of (target_address, value_wei, calldata_bytes) triples, in order.
    @return               A tuple of (user_op_hash_bytes, receipt), same shape as the single-call flow.
    """
    session_handler, entry_point, calldata, nonce = prepare_execute_batch_call(
        user_id, executions
    )
    return _submit_user_op(user_id, key_ciphertext, session_handler, entry_point, calldata, nonce)


def _submit_user_op(
    user_id: int,
    key_ciphertext: str,
    session_handler,
    entry_point,
    calldata: str,
    nonce: int,
):
    """
    Shared tail of the live-bundler UserOp flow: estimates gas via the bundler, signs the op
    with the session key, submits via eth_sendUserOperation, and polls for the receipt.
    Everything after calldata construction is identical for single-call and batch ops.
    """
    w3, _, _ = load_network_config(user_id)
    rpc_url = str(w3.provider.endpoint_uri)

    print("\n[1/3] Creating transaction  ...")
    user_op = create_unsigned_user_op(
        user_id=user_id,
        session_handler=session_handler,
        key_ciphertext=key_ciphertext,
        entry_point=entry_point,
        nonce=nonce,
        calldata=calldata,
    )

    print("[2/3] Signing transaction   ...")
    user_op_signed = create_signed_user_op(
        user_id=user_id,
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
    # `logs` and `transactionHash` are carried through from the bundler receipt so a caller
    # that needs an on-chain OUTPUT can read it: ERC-8004's register() returns the new agentId
    # only in a Registered event, so tools.register_agent has nothing else to parse. Both are
    # in the JSON form (hex strings), which is what the packages' receipt parsers expect.
    # Additive — `status` keeps the shape every other tool checks, and a bundler that omits
    # either field leaves an empty list rather than raising.
    inner_receipt = bundler_receipt.get("receipt") or {}
    return op_hash_bytes, {
        "status": 1,
        "logs": bundler_receipt.get("logs") or inner_receipt.get("logs") or [],
        "transactionHash": inner_receipt.get("transactionHash"),
    }
