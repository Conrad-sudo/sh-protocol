import os
import secrets
from dotenv import load_dotenv
from web3.contract import Contract
from web3.logs import DISCARD
from network_config import load_network_config
from contracts import (
    load_session_handler,
    load_entry_point,
)
from constants import CHAIN_ID_ANVIL, CHAIN_ID_MAINNET, CHAIN_ID_SEPOLIA
import db
from vault_signer import encrypt_key, decrypt_key

load_dotenv()

# Gas estimation constants for the dummy UserOp used in eth_estimateGas simulation.
# These are placeholder limits — large enough that the EntryPoint prefund check passes,
# but not so large they cause issues. The real limits are set after estimation.
DUMMY_INNER_GAS = 500_000
DUMMY_PRE_VERIFICATION_GAS = 50_000
_DUMMY_GAS_PRICE_WEI = 256  # minimal non-zero value; real price applied post-estimation

GAS_BUFFER_MULTIPLIER = 1.2  # 20% headroom added to estimated gas
PRE_VERIFICATION_GAS = 50_000


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


def create_unsigned_user_op(
    chat_id: int,
    session_handler: Contract,
    key_ciphertext: str,
    entry_point: Contract,
    nonce: int,
    calldata: str,
) -> tuple[tuple, int, int]:
    """
    Constructs an unsigned ERC-4337 PackedUserOperation tuple.

    Builds a dummy op with placeholder gas limits, signs it to estimate actual gas
    via eth_estimateGas, then constructs the final op with a 20% gas buffer and the
    live gas price from the node.

    @param chat_id        The Telegram chat ID of the user.
    @param session_handler Bound SessionHandler contract (the UserOp sender).
    @param key_ciphertext  Vault Transit ciphertext for the session key ('vault:v1:...').
    @param entry_point     Bound EntryPoint contract.
    @param nonce           The sender's current nonce from the EntryPoint.
    @param calldata        Hex-encoded SessionHandler.execute() calldata (0x-prefixed).
    @return                A tuple of (unsigned PackedUserOperation, outer_gas, gas_price) where
                           outer_gas is 2x the estimated inner gas so the EntryPoint AA95 check
                           passes, and gas_price is the snapshot used to build gas_fees — must be
                           reused on the outer tx so the EntryPoint prefund check is consistent.
    """
    owner_address = session_handler.functions.owner().call()
    w3, _, _ = load_network_config(chat_id)

    # Use modest placeholder limits for the dummy op so the EntryPoint's prefund
    # calculation (verificationGasLimit + callGasLimit + preVerificationGas) * gasPrice
    # stays within the SessionHandler's ETH balance during eth_estimateGas simulation.
    dummy_op = (
        session_handler.address,
        nonce,
        b"",
        bytes.fromhex(calldata[2:]),
        (DUMMY_INNER_GAS << 128 | DUMMY_INNER_GAS).to_bytes(32, "big"),
        DUMMY_PRE_VERIFICATION_GAS,
        (_DUMMY_GAS_PRICE_WEI << 128 | _DUMMY_GAS_PRICE_WEI).to_bytes(32, "big"),
        b"",
        b"",
    )

    signed_dummy_op = create_signed_user_op(
        chat_id=chat_id,
        user_op=dummy_op,
        entry_point=entry_point,
        key_ciphertext=key_ciphertext,
    )

    estimated = w3.eth.estimate_gas(
        {
            "to": entry_point.address,
            "data": entry_point.encode_abi(
                abi_element_identifier="handleOps",
                args=[[signed_dummy_op], owner_address],
            ),
        }
    )

    # outer_gas: total gas for the handleOps transaction — must comfortably exceed
    # verificationGasLimit + callGasLimit so the EntryPoint's AA95 check passes.
    # inner_gas: per-component limit packed into the UserOp. Set to estimated so each
    # component has enough headroom; outer_gas = 2x covers the sum.
    gas_price = w3.eth.gas_price
    inner_gas = int(estimated * GAS_BUFFER_MULTIPLIER)
    outer_gas = inner_gas * 2
    pre_verification_gas = PRE_VERIFICATION_GAS

    account_gas_limits = (inner_gas << 128 | inner_gas).to_bytes(32, "big")
    gas_fees = (gas_price << 128 | gas_price).to_bytes(32, "big")

    return (
        (
            session_handler.address,
            nonce,
            b"",
            bytes.fromhex(calldata[2:]),
            account_gas_limits,
            pre_verification_gas,
            gas_fees,
            b"",
            b"",
        ),
        outer_gas,
        gas_price,
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
    @return               A signed PackedUserOperation tuple ready for handleOps.
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


def send_user_op_as_session(
    chat_id: int, key_ciphertext: str, target: str, value: int, data: bytes
):
    """
    Orchestrates the full ERC-4337 UserOperation flow for a session key holder.

    Encodes SessionHandler.execute() as the UserOp calldata, builds an unsigned
    PackedUserOperation, signs it with the session key via EIP-191, and submits
    it to the EntryPoint via handleOps(). The bundler key from the environment
    signs and sends the outer transaction.

    @param chat_id        The Telegram chat ID of the user.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param target         The contract address SessionHandler will call (e.g. USDC).
    @param value          The ETH value in wei to forward with the inner call.
    @param data           ABI-encoded inner calldata to execute on the target.
    @return               A tuple of (tx_hash, receipt).
    """

    w3, chain_id, _ = load_network_config(chat_id)
    if chain_id == CHAIN_ID_ANVIL:
        bundler = w3.eth.account.from_key(os.getenv("ANVIL_BUNDLER"))
    elif chain_id == CHAIN_ID_MAINNET:
        bundler = w3.eth.account.from_key(os.getenv("MAINNET_BUNDLER"))
    elif chain_id == CHAIN_ID_SEPOLIA:
        bundler = w3.eth.account.from_key(os.getenv("SEPOLIA_BUNDLER"))
    else:
        raise ValueError(f"No bundler configured for chain_id {chain_id}")
    session_handler = load_session_handler(chat_id=chat_id)

    calldata = session_handler.encode_abi(
        abi_element_identifier="execute", args=[target, value, data]
    )

    entry_point = load_entry_point(chat_id=chat_id)
    nonce = entry_point.functions.getNonce(session_handler.address, 0).call()

    print("\n[1/3] Creating transaction  ...")
    user_op, gas_limit, gas_price = create_unsigned_user_op(
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

    print("[3/3] Sending transaction   ...")
    tx = entry_point.functions.handleOps(
        [user_op_signed], bundler.address
    ).build_transaction(
        {
            "from": bundler.address,
            "nonce": w3.eth.get_transaction_count(bundler.address),
            "chainId": chain_id,
            "gas": gas_limit,
            "gasPrice": gas_price,
        }
    )
    signed_tx = w3.eth.account.sign_transaction(tx, bundler.key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    if receipt["status"] == 0:
        try:
            w3.eth.call(
                {
                    "from": bundler.address,
                    "to": entry_point.address,
                    "data": tx["data"],
                    "gas": tx["gas"],
                    "gasPrice": gas_price,
                },
                block_identifier=receipt["blockNumber"] - 1,
            )
        except Exception as revert_err:
            print(f"[revert reason] {revert_err}")
        raise RuntimeError("handleOps outer transaction reverted")

    # In ERC-4337, the EntryPoint catches inner call reverts and still mines the outer
    # transaction successfully (status 1). The actual inner result is in UserOperationEvent.
    events = entry_point.events.UserOperationEvent().process_receipt(
        receipt, errors=DISCARD
    )
    for evt in events:
        if not evt["args"]["success"]:
            raise RuntimeError(
                f"UserOperation inner call failed "
                f"(nonce={evt['args']['nonce']}, gas_cost={evt['args']['actualGasCost']} wei). "
                f"The transaction was mined but the inner call reverted — check token balances and allowances."
            )

    return tx_hash, receipt
