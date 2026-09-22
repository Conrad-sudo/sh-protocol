import os
from dotenv import load_dotenv
from web3.contract import Contract
from web3.logs import DISCARD
from network_config import load_network_config
from constants import CHAIN_ID_ANVIL
from userop import create_signed_user_op, prepare_execute_call, prepare_execute_batch_call
from tx_sender import send_and_confirm
from contract_errors import describe_revert_data

load_dotenv()

# Gas estimation constants for the dummy UserOp used in eth_estimateGas simulation.
# These are placeholder limits — large enough that the EntryPoint prefund check passes,
# but not so large they cause issues. The real limits are set after estimation.
DUMMY_INNER_GAS = 500_000
DUMMY_PRE_VERIFICATION_GAS = 50_000
_DUMMY_GAS_PRICE_WEI = 256  # minimal non-zero value; real price applied post-estimation

GAS_BUFFER_MULTIPLIER = 1.2  # 20% headroom added to estimated gas
PRE_VERIFICATION_GAS = 50_000


def resolve_bundler(w3, chain_id: int):
    """
    Returns the account that signs outer handleOps transactions on this chain.

    Plain Anvil has no real chain state to inherit, so its own burner key is safe there. Every
    other self-bundled network — the four forks and live Sepolia — uses SEPOLIA_PRIVATE_KEY,
    which is also their deployer key (see deploy_wallet.LIVE_PRIVATE_KEY_ENV), keeping deployer,
    protocol owner and bundler as one address.

    Whichever it is, it must stay a plain EOA: the EntryPoint pays the beneficiary with a bare
    ETH send, which reverts AA91 against an address carrying code. That is what rules out the
    well-known Anvil keys off plain Anvil — they are EIP-7702-delegated on real mainnet/Sepolia/BSC,
    and a fork inherits that code.

    On every network but plain Anvil this is also the deployer key, so `make fund`'s single
    anvil_setBalance on a fork covers both roles at once.

    @param w3        Web3 connection for the target network.
    @param chain_id  EIP-155 chain ID of that network.
    @return          A web3.py LocalAccount for the bundler key.
    """
    key = os.getenv("ANVIL_BUNDLER") if chain_id == CHAIN_ID_ANVIL else os.getenv("SEPOLIA_PRIVATE_KEY")
    return w3.eth.account.from_key(key)


def _fees(w3) -> tuple[int, int]:
    """
    Returns (max_fee_per_gas, max_priority_fee_per_gas) for both the UserOp and the outer tx.

    A UserOp's fees are fixed at signing, so a thin cushion strands it the moment the base fee
    climbs past the cap. The 2x base-fee headroom survives a spike of up to ~2x and is the
    figure SessionHandler's {maxOpGasCost} was sized against ("clears a ~600k-gas swap at 2x a
    spiking base fee"), so a legitimate op stays inside the account's own ceiling.

    @param w3  Web3 connection for the target network.
    @return    (max_fee_per_gas, max_priority_fee_per_gas), both in wei.
    """
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    try:
        tip = w3.eth.max_priority_fee
    except Exception:
        # Some nodes don't implement eth_maxPriorityFeePerGas; a 2 gwei tip is a safe default.
        tip = w3.to_wei(2, "gwei")
    return 2 * base_fee + tip, tip


def create_unsigned_user_op(
    user_id: int,
    session_handler: Contract,
    key_ciphertext: str,
    entry_point: Contract,
    nonce: int,
    calldata: str,
    beneficiary: str,
) -> tuple[tuple, int, int]:
    """
    Constructs an unsigned ERC-4337 PackedUserOperation tuple.

    Builds a dummy op with placeholder gas limits, signs it to estimate actual gas
    via eth_estimateGas, then constructs the final op with a 20% gas buffer and the
    live gas price from the node.

    @param user_id        The application user ID.
    @param session_handler Bound SessionHandler contract (the UserOp sender).
    @param key_ciphertext  Vault Transit ciphertext for the session key ('vault:v1:...').
    @param entry_point     Bound EntryPoint contract.
    @param nonce           The sender's current nonce from the EntryPoint.
    @param calldata        Hex-encoded SessionHandler.execute() calldata (0x-prefixed).
    @param beneficiary     Address the dummy handleOps probe credits gas compensation to.
                           Must be the same address the real handleOps call will use — the
                           EntryPoint's final _compensate() step does a plain ETH send to it,
                           which reverts (AA91) against any address with code that lacks a
                           payable receive/fallback (e.g. an EIP-7702-delegated EOA).
    @return                A tuple of (unsigned PackedUserOperation, outer_gas, fees) where
                           outer_gas is 2x the estimated inner gas so the EntryPoint AA95 check
                           passes, and fees is the EIP-1559 fee dict for the outer transaction,
                           built from the same snapshot as the op's own gas_fees so the bundler
                           is reimbursed for what it pays.
    """
    w3, _, _ = load_network_config(user_id)

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
        user_id=user_id,
        user_op=dummy_op,
        entry_point=entry_point,
        key_ciphertext=key_ciphertext,
    )

    estimated = w3.eth.estimate_gas(
        {
            "to": entry_point.address,
            "data": entry_point.encode_abi(
                abi_element_identifier="handleOps",
                args=[[signed_dummy_op], beneficiary],
            ),
        }
    )

    # outer_gas: total gas for the handleOps transaction — must comfortably exceed
    # verificationGasLimit + callGasLimit so the EntryPoint's AA95 check passes.
    # inner_gas: per-component limit packed into the UserOp. Set to estimated so each
    # component has enough headroom; outer_gas = 2x covers the sum.
    max_fee_per_gas, max_priority_fee_per_gas = _fees(w3)
    inner_gas = int(estimated * GAS_BUFFER_MULTIPLIER)
    outer_gas = inner_gas * 2
    pre_verification_gas = PRE_VERIFICATION_GAS

    # SessionHandler._validateUserOp prices the op at
    #   (verificationGasLimit + callGasLimit + preVerificationGas) * maxFeePerGas
    # and reverts above the account's own maxOpGasCost. That revert happens during validation, so
    # the EntryPoint reimburses nothing and this bundler eats the whole handleOps transaction —
    # worth one eth_call to avoid. Clamping the cap keeps the op inside the ceiling instead.
    #
    # The ceiling is reached sooner than it looks: both halves of accountGasLimits are set to
    # inner_gas below, so the account prices roughly twice the gas an op really uses.
    op_gas = 2 * inner_gas + pre_verification_gas
    affordable_max_fee = session_handler.functions.maxOpGasCost().call() // op_gas
    if max_fee_per_gas > affordable_max_fee:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
        if affordable_max_fee <= base_fee:
            raise RuntimeError(
                f"base fee ({base_fee / 1e9:.1f} gwei) exceeds what this wallet will pay for one "
                f"UserOp ({affordable_max_fee / 1e9:.1f} gwei over {op_gas:,} gas, capped by its "
                f"maxOpGasCost). Submitting would revert in validation at the bundler's expense — "
                f"retry when the base fee falls, or raise maxOpGasCost on the wallet."
            )
        # Trim the spike cushion, not the tip: the tip is what actually buys inclusion, and the
        # EntryPoint reimburses at min(maxFee, tip + basefee) either way.
        max_fee_per_gas = affordable_max_fee
        max_priority_fee_per_gas = min(max_priority_fee_per_gas, max_fee_per_gas)

    account_gas_limits = (inner_gas << 128 | inner_gas).to_bytes(32, "big")
    # Packing per ERC-4337 v0.7: gasFees = maxPriorityFeePerGas (HIGH 128) | maxFeePerGas (LOW
    # 128) — see UserOperationLib.unpack{MaxPriorityFeePerGas,MaxFeePerGas} and the matching read
    # in SessionHandler._validateUserOp. The order only became load-bearing once the two stopped
    # being equal, so it must not be flipped.
    #
    # They are a real cap-and-tip pair rather than one repeated value. The EntryPoint reimburses
    # this bundler at min(maxFee, maxPriority + basefee) = tip + basefee, which is what the outer
    # transaction below actually costs at the same block: the bundler is made whole and no more.
    # Setting both halves to the cap, as the bundler-RPC path is forced to, would instead repay
    # 2 * basefee + tip and charge the user a whole extra base fee per op.
    gas_fees = (max_priority_fee_per_gas << 128 | max_fee_per_gas).to_bytes(32, "big")

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
        {
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
        },
    )


def send_user_op_as_session(
    user_id: int, key_ciphertext: str, target: str, value: int, data: bytes
):
    """
    Orchestrates the full ERC-4337 UserOperation flow for a session key holder.

    Packs (target, value, data) into ERC-7579 executionCalldata and encodes
    SessionHandler.execute(mode, executionCalldata) as the UserOp calldata, fetches a
    nonce keyed to the installed SpendingLimitModule (so the account routes validation to
    it), builds an unsigned PackedUserOperation, signs it with the session key via EIP-191,
    and submits it to the EntryPoint via handleOps(). The bundler key from the environment
    signs and sends the outer transaction.

    @param user_id        The application user ID.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param target         The contract address SessionHandler will call (e.g. USDC).
    @param value          The ETH value in wei to forward with the inner call.
    @param data           ABI-encoded inner calldata to execute on the target.
    @return               A tuple of (tx_hash, receipt).
    """
    session_handler, entry_point, calldata, nonce = prepare_execute_call(
        user_id, target, value, data
    )
    return _submit_user_op(user_id, key_ciphertext, session_handler, entry_point, calldata, nonce)


def send_batch_user_op_as_session(
    user_id: int, key_ciphertext: str, executions: list[tuple[str, int, bytes]]
):
    """
    Batch variant of send_user_op_as_session: submits several sub-calls as ONE atomic
    execute(batchMode, ...) UserOp. Required for any flow that grants an approval —
    SpendingLimitModule reverts the whole transaction if an approval survives it, so
    [approve, spend(, approve 0)] must land together.

    @param user_id        The application user ID.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param executions     List of (target_address, value_wei, calldata_bytes) triples, in order.
    @return               A tuple of (tx_hash, receipt).
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
    Shared tail of the Anvil/fork UserOp flow: estimates gas, signs the op with the session key,
    and submits it to the EntryPoint via handleOps() signed by the local bundler key. Everything
    after calldata construction is identical for single-call and batch ops.
    """
    w3, chain_id, chain_name = load_network_config(user_id)
    bundler = resolve_bundler(w3, chain_id)

    print("\n[1/3] Creating transaction  ...")
    user_op, gas_limit, fees = create_unsigned_user_op(
        user_id=user_id,
        session_handler=session_handler,
        key_ciphertext=key_ciphertext,
        entry_point=entry_point,
        nonce=nonce,
        calldata=calldata,
        beneficiary=bundler.address,
    )
    print("[2/3] Signing transaction   ...")
    user_op_signed = create_signed_user_op(
        user_id=user_id,
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
            # Placeholder only: send_tx overwrites this with a nonce allocated under its lock.
            # Supplying one at all just stops build_transaction from making its own (racy)
            # eth_getTransactionCount call while assembling the dict.
            "nonce": 0,
            "chainId": chain_id,
            "gas": gas_limit,
            **fees,
        }
    )
    # One bundler EOA signs for every user, so the nonce is allocated under a lock rather than
    # read here — telebot.py serves concurrent users on separate threads. send_and_confirm also
    # bounds the wait and replaces the transaction at a higher fee if the base fee outruns it,
    # instead of parking a user's request on an unbounded wait_for_transaction_receipt.
    receipt = send_and_confirm(w3, chain_name, bundler, tx)
    tx_hash = receipt["transactionHash"]

    if receipt["status"] == 0:
        try:
            w3.eth.call(
                {
                    "from": bundler.address,
                    "to": entry_point.address,
                    "data": tx["data"],
                    "gas": tx["gas"],
                    **fees,
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
            # The EntryPoint records WHY in a separate event, carrying the call's raw revert data.
            # Naming it is what lets the agent tell a user "prices are paused" (an L2 sequencer
            # outage) apart from "not enough balance", rather than guessing from a bare failure.
            reasons = entry_point.events.UserOperationRevertReason().process_receipt(receipt, errors=DISCARD)
            reason = describe_revert_data(reasons[0]["args"]["revertReason"]) if reasons else "reason not reported"
            raise RuntimeError(
                f"UserOperation inner call failed with {reason} "
                f"(nonce={evt['args']['nonce']}, gas_cost={evt['args']['actualGasCost']} wei). "
                f"The transaction was mined but the inner call reverted."
            )

    return tx_hash, receipt
