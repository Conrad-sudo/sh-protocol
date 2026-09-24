"""
Self-bundling: how every session-key UserOp reaches the EntryPoint, on every network.

The app is its own ERC-4337 bundler everywhere -- plain Anvil, every fork and every live chain. It
builds the UserOp, signs it with the wallet's session key, wraps it in EntryPoint.handleOps() and
sends that outer transaction from a bundler EOA of its own, named as beneficiary so the EntryPoint
repays the gas out of the wallet's own prefund. No third-party bundler ever holds a signed op.

The order of work is part of the security model:

  1. {quote_user_op} prices the op WITHOUT the session key. The wallet's execute() and
     validateUserOp() are each estimated exactly as the EntryPoint will call them, and the whole
     handleOps bundle is then simulated end to end -- all of it signed by a throwaway key that the
     wallet never authorized, made to pass validation by a state override that exists only inside
     the simulation. Nothing the RPC provider sees in this phase could be executed on chain.
     (This used to sign a placeholder op with the REAL key just to estimate it -- an executable
     transaction handed to the RPC.)
  2. {prepare_user_op} signs the quoted op with the real key and estimates it once more as a whole
     handleOps, which catches anything that moved in between and sizes the outer transaction. This
     is the first executable op in the flow, so nothing reaches it until the user has agreed: the
     two phases are split precisely so the cost can be shown, and confirmed, in between.
  3. {broadcast_user_op} sends it. Afterwards the op is looked up by its hash if our transaction
     reverted or stalled: a UserOp's signature does not cover the beneficiary, so someone else may
     have landed it first.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError
from web3.logs import DISCARD

from contract_errors import describe_revert_data
from contracts import load_entry_point
from network_config import load_network_config
from tx_sender import send_and_confirm
from userop import (
    create_signed_user_op,
    current_session_nonce,
    prepare_execute_call,
    prepare_execute_batch_call,
)

load_dotenv()

# Headroom on the execution estimate. The one limit worth keeping reasonably tight: the EntryPoint
# charges the wallet 10% of any execution gas left unused (beyond 40k). But an op that runs out of
# execution gas is still mined and paid for while doing nothing, so this errs towards too much.
CALL_GAS_BUFFER = 1.2
# Headroom on the validation estimate. Generous on purpose: unused VALIDATION gas is refunded in
# full, and the EntryPoint's own share of validation -- copying and hashing the op, bumping the
# nonce -- counts against this limit too (AA26) while happening outside the call being estimated.
VERIFICATION_GAS_BUFFER = 1.5
# Headroom on the outer handleOps estimate. Free: the bundler pays for gas used, not the limit.
OUTER_GAS_BUFFER = 1.2

# The EntryPoint fines the account 10% of the execution gas it reserved and did not use, but only
# once the shortfall passes 40k -- EntryPoint._getUnusedGasPenalty. Mirrored here so a quote counts
# the same fine the chain will charge, rather than discovering it in the receipt.
UNUSED_GAS_PENALTY_PERCENT = 10
PENALTY_GAS_THRESHOLD = 40_000

# preVerificationGas repays the bundler for what the EntryPoint cannot measure on its own: the outer
# transaction's intrinsic cost, its calldata, and handleOps' bookkeeping around the op (re-entrancy
# guard, BeforeExecution, crediting the refund, UserOperationEvent, paying the beneficiary). The
# calldata part is counted exactly per op; the rest is fixed for a one-op bundle. Measured on a
# mainnet fork (2026-09-22, ERC20 transfers): 20k leaves the bundler ~3.5-4.5k gas ahead per op
# after the outer transaction's storage refunds -- a deliberate cushion, since a fee bump in
# tx_sender is paid by the bundler alone. Lowering it towards 16k trades that cushion away.
TX_BASE_GAS = 21_000
ENTRY_POINT_OVERHEAD_GAS = 20_000

# Storage slot holding SessionHandler's `currentSession` (low 20 bytes) packed with
# `currentSessionValidUntil` (the 6 bytes above it), used to simulate a whole bundle without the
# session key (see {_session_override}). A PLAIN slot, not a mapping base: the wallet authorizes one
# key at a time, so there is no key to hash in. Never trusted blind -- the override is checked
# against the live contract before the simulation is believed, so a layout change costs the quote
# its precision and nothing else. Confirm with `forge inspect SessionHandler storageLayout`.
CURRENT_SESSION_SLOT = 8

# The deadline written into the override's packed slot. Any far-future timestamp works; it only has
# to outlast the simulated op, and a uint48 cannot hold a value this side of the year 8 million.
# Writing 1 here (as the old boolean override did) would simulate a key that expired in 1970 and
# every quote would die on AA22.
OVERRIDE_SESSION_VALID_UNTIL = 2**47 - 1

# Arbitrum charges every transaction for posting its data to Ethereum, as extra gas units that the
# EntryPoint never sees. Unless they go into preVerificationGas, the bundler pays that share of
# every op itself. NodeInterface is virtual -- only a real Arbitrum node answers it -- and a fork
# charges no L1 fee anyway, so only the live chain is priced this way.
ARB_NODE_INTERFACE = "0x00000000000000000000000000000000000000C8"
_L1_DATA_GAS_CHAINS = {"arbitrum"}
# The L1 price can move between this estimate and inclusion.
L1_GAS_BUFFER = 1.2

# Live chains whose public mempool is watched by bots. A UserOp's signature does not cover the
# beneficiary, so a bot that sees our handleOps can lift the op out, resubmit it naming itself, and
# collect the gas repayment: the user's action still happens, but our transaction reverts and pays
# for nothing. Broadcasting privately keeps the transaction out of the public mempool. Only the
# BROADCAST goes here -- every read and estimate still uses the chain's normal RPC -- and a fork
# never matches, since its name differs.
PRIVATE_SEND_RPC_URLS = {
    "mainnet": os.getenv("MAINNET_PRIVATE_RPC_URL") or "https://rpc.flashbots.net/fast",
}
_private_send_w3: dict[str, Web3] = {}

# Which EOA signs handleOps is decided by the PROCESS, not the chain. The API and the Telegram bot
# are separate processes, and tx_sender's nonce counter only coordinates threads within one: two
# processes signing with ONE key would hand out the same nonce and replace each other's
# transactions. So each process has its own key. The API's is the default; telebot.py switches at
# startup. Anything else that runs the agent in a process of its own (the CLI agent, the smoke
# test) signs with the API's key, and so must not run beside the API.
API_BUNDLER_ENV = "API_BUNDLER"
TELEGRAM_BUNDLER_ENV = "TELEGRAM_BUNDLER"
_bundler_key_env = API_BUNDLER_ENV


@dataclass
class UserOpQuote:
    """
    What one UserOp will cost, worked out WITHOUT the session key.

    Everything a signed op needs except the signature and the nonce: the gas limits the op will
    carry, the fee cap it will be priced at, and the simulated cost of the whole bundle. Held
    between the moment the user is quoted and the moment they agree, so the transaction they
    approve is priced from the same numbers it is later sent with.
    """

    calldata: str            # SessionHandler.execute() calldata, hex, 0x-prefixed
    call_gas: int            # callGasLimit: the buffered LIMIT the op carries
    call_gas_used: int       # what execution is estimated to actually burn
    verification_gas: int    # verificationGasLimit: the buffered LIMIT the op carries
    verification_gas_used: int  # what validation is estimated to actually burn
    pre_verification_gas: int
    max_fee: int             # the op's maxFeePerGas: the ceiling it can be charged at
    tip: int
    base_fee: int            # at quote time, for the expected (rather than worst-case) price
    outer_gas: int           # gas limit for the handleOps transaction
    simulated_gas: int | None  # whole-bundle gas, or None if the node refused the state override
    chain_name: str

    @property
    def op_gas(self) -> int:
        """The gas the EntryPoint can charge the wallet for, and what maxOpGasCost is measured on."""
        return self.verification_gas + self.call_gas + self.pre_verification_gas

    @property
    def effective_price(self) -> int:
        """What a wei of gas actually costs right now -- the EntryPoint repays at this, not the cap."""
        return min(self.max_fee, self.base_fee + self.tip)

    @property
    def expected_gas(self) -> int:
        """
        What the EntryPoint is expected to charge the wallet for: actualGasUsed.

        Built from the parts rather than from the bundle simulation, because the two measure
        different things. eth_estimateGas has to return enough for the EntryPoint to FORWARD both
        gas limits, whether or not they are used, so it tracks the limits -- which is right for
        sizing the outer transaction and wrong for quoting a price, over-stating the cost by the
        whole buffer. What the account actually pays is preVerificationGas plus the gas validation
        and execution really burn, plus the EntryPoint's fine for execution gas reserved and left
        unused.
        """
        return (
            self.pre_verification_gas
            + self.verification_gas_used
            + self.call_gas_used
            + _unused_gas_penalty(self.call_gas_used, self.call_gas)
        )

    @property
    def expected_gas_wei(self) -> int:
        """The likely network cost: the gas the op should really burn, at today's price."""
        return self.expected_gas * self.effective_price

    @property
    def max_gas_wei(self) -> int:
        """The most the wallet can be charged: every limit used in full, at the fee cap."""
        return self.op_gas * self.max_fee


@dataclass
class PreparedUserOp:
    """A signed UserOp, checked and sized, ready for broadcast_user_op."""

    op: tuple            # the signed PackedUserOperation
    user_op_hash: bytes  # the EntryPoint's hash of it: how the op is found on chain afterwards
    outer_gas: int       # gas limit for the handleOps transaction
    fees: dict           # EIP-1559 fee fields, shared by the op and the handleOps transaction
    from_block: int      # the chain head before anything was sent; a lost race is searched from here


def use_bundler_key(env_name: str) -> None:
    """
    Selects the env var holding the key this process signs handleOps with.

    Called once at startup by any process that must not share the API's key (telebot.py).

    @param env_name  API_BUNDLER_ENV or TELEGRAM_BUNDLER_ENV.
    """
    global _bundler_key_env
    if env_name not in (API_BUNDLER_ENV, TELEGRAM_BUNDLER_ENV):
        raise ValueError(f"Unknown bundler key '{env_name}'")
    _bundler_key_env = env_name


def resolve_bundler(w3: Web3) -> LocalAccount:
    """
    Returns this process's bundler account.

    It must be a plain EOA on every chain it bundles for: the EntryPoint pays the beneficiary with a
    bare ETH send, which reverts AA91 against an address carrying code. That is why the well-known
    Anvil keys are never used here -- they are EIP-7702-delegated on real mainnet, Sepolia and BSC,
    and a fork inherits that code.

    @param w3  Web3 connection for the target network.
    @return    A web3.py LocalAccount for the bundler key.
    @raises RuntimeError if the key is not configured.
    """
    key = os.getenv(_bundler_key_env)
    if not key:
        raise RuntimeError(f"{_bundler_key_env} is not set -- this process has no bundler key.")
    return w3.eth.account.from_key(key)


def _fees(w3: Web3) -> tuple[int, int]:
    """
    Returns (max_fee_per_gas, max_priority_fee_per_gas) for both the UserOp and the outer tx.

    A UserOp's fees are fixed at signing, so a thin cushion strands it the moment the base fee
    climbs past the cap. The 2x base-fee headroom survives a spike of up to ~2x and is the figure
    SessionHandler's {maxOpGasCost} was sized against. It is a cap, not a price: the EntryPoint
    repays at min(cap, tip + base fee), the same as the outer transaction costs the bundler.

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


def _revert_reason(exc: Exception) -> str:
    """Names the revert inside a web3 exception, falling back to its text."""
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        data = data.get("data")
    if isinstance(data, str) and data.startswith("0x") and len(data) > 2:
        try:
            return describe_revert_data(bytes.fromhex(data[2:]))
        except ValueError:
            pass
    return str(exc)


def _estimate(w3: Web3, tx: dict, what: str) -> int:
    """
    eth_estimateGas, turning a revert into a RuntimeError that names the contract's error.

    A revert here is the cheapest place to learn a transaction would fail: nothing has been signed
    with the real key and nothing has been sent, so it costs the user and the bundler nothing.

    Run against the PENDING block, whose timestamp is the next block's. Against "latest" it would
    run at the last mined block's time instead -- on a quiet chain (a fork most of all) that can be
    hours stale, so a price feed that is past its heartbeat by the time the transaction lands still
    passes the estimate, and the op is mined only to fail and charge the wallet for it.
    """
    try:
        return w3.eth.estimate_gas(tx, block_identifier="pending")
    except ContractLogicError as exc:
        raise RuntimeError(f"{what} would fail: {_revert_reason(exc)}. Nothing was sent.") from exc


def _intrinsic_gas(data: bytes) -> int:
    """A transaction's intrinsic gas: the base cost plus 4 per zero calldata byte and 16 per other."""
    return TX_BASE_GAS + sum(4 if byte == 0 else 16 for byte in data)


def _estimate_call_gas(w3: Web3, entry_point: Contract, session_handler: Contract, calldata: str) -> int:
    """
    Execution gas: the wallet's execute() estimated exactly as the EntryPoint will call it, with no
    UserOp and no signature at all.

    execute() admits only the EntryPoint, itself or the owner, so `from` is the EntryPoint -- which
    an estimate may claim and a real transaction cannot. Every other caller is refused, so the
    estimate walks the session-key path: the admin-surface guard, the protocol fee, and the
    spending-limit hook with every oracle read it makes. A revert here means the op would fail on
    chain, and is reported by name before anything is signed.
    """
    gas = _estimate(
        w3,
        {"from": entry_point.address, "to": session_handler.address, "data": calldata},
        "This transaction",
    )
    # The estimate prices a standalone transaction, so it includes that transaction's intrinsic cost
    # -- which inside handleOps is the outer transaction's, and repaid through preVerificationGas.
    # Left in, it would only swell the unused execution gas the EntryPoint fines the wallet 10% of.
    #
    # Returned UNBUFFERED. The caller adds the headroom, and keeps this figure as its estimate of
    # what the execution will actually burn -- which is what a cost quote needs, the limit being an
    # upper bound rather than a prediction.
    return gas - _intrinsic_gas(bytes.fromhex(calldata[2:]))


def _estimate_verification_gas(
    w3: Web3,
    entry_point: Contract,
    session_handler: Contract,
    sender: str,
    nonce: int,
    calldata: str,
    probe_key: LocalAccount,
) -> int:
    """
    Validation gas: validateUserOp estimated as the EntryPoint calls it, signed by a THROWAWAY key.

    The wallet treats a signature it does not recognise as a failed check, not a revert (tryRecover
    -> SIG_VALIDATION_FAILED), so a throwaway key walks the same path as the session key -- one
    ecrecover, the owner read, one cold currentSession read -- at the same cost, while the op it
    signs is worthless on chain: that key was never authorized.

    The probe's gas fields are zero so the wallet's maxOpGasCost check prices it at nothing, and it
    asks for a 1-wei prefund so the transfer to the EntryPoint is always counted. Both over-size
    validation slightly when the real op needs no top-up, which costs the user nothing: unused
    validation gas is refunded in full.

    @param probe_key  The throwaway account, supplied by the caller rather than made here so the
                      same key can be marked allowed in the whole-bundle simulation that follows.
    """
    zero_hash = b"\x00" * 32
    signature = probe_key.sign_message(encode_defunct(zero_hash)).signature
    probe = (
        sender, nonce, b"", bytes.fromhex(calldata[2:]),
        b"\x00" * 32, 0, b"\x00" * 32, b"", bytes(signature),
    )
    data = session_handler.encode_abi(
        abi_element_identifier="validateUserOp", args=[probe, zero_hash, 1]
    )
    # Unbuffered, for the same reason as the execution estimate: the caller buffers the LIMIT and
    # keeps this as what validation will really cost.
    return _estimate(
        w3,
        {"from": entry_point.address, "to": session_handler.address, "data": data},
        "Validating this transaction",
    )


def _unused_gas_penalty(gas_used: int, gas_limit: int) -> int:
    """The EntryPoint's fine for reserving execution gas and not using it. See its _getUnusedGasPenalty."""
    if gas_limit <= gas_used + PENALTY_GAS_THRESHOLD:
        return 0
    return (gas_limit - gas_used) * UNUSED_GAS_PENALTY_PERCENT // 100


def _session_override(session_handler: Contract, key_address: str) -> dict:
    """
    A state override that makes the wallet treat `key_address` as its session key, unexpired.

    Lets the WHOLE bundle be simulated -- EntryPoint.handleOps end to end, validation and execution
    together -- while it is still signed by a throwaway key. Without it the simulation stops at
    AA24, so the only way to measure the real cost would be to sign with the session key and hand
    an executable op to the node before the user has agreed to anything.

    Writes the key and a far-future deadline TOGETHER, because they share one slot and the wallet
    returns the deadline as the op's ERC-4337 validity window: a key with a zero deadline simulates
    as expired and the whole bundle fails AA22 instead of being priced.

    The override exists only inside that one eth_call: nothing is written to the chain and the key
    is authorized nowhere else, so the op being simulated cannot be executed on chain by anybody.
    The slot number is checked against the live contract before it is trusted (see
    {_simulate_handle_ops}), so a storage-layout change degrades the quote rather than corrupting it.
    """
    packed = int(key_address, 16) | (OVERRIDE_SESSION_VALID_UNTIL << 160)
    slot = CURRENT_SESSION_SLOT.to_bytes(32, "big")
    return {
        session_handler.address: {
            "stateDiff": {"0x" + slot.hex(): "0x" + packed.to_bytes(32, "big").hex()}
        }
    }


def _simulate_handle_ops(
    w3: Web3,
    entry_point: Contract,
    session_handler: Contract,
    op: tuple,
    bundler: LocalAccount,
    probe_key: LocalAccount,
) -> int | None:
    """
    Gas for the whole handleOps bundle, with the throwaway key overridden into the allowlist.

    @return  The simulated gas, or None if this node will not honour a state override -- in which
             case the caller sizes the outer transaction from the parts instead. None is a
             degraded quote, not a failure; a revert is a failure and is raised.
    @raises RuntimeError if the bundle would revert.
    """
    override = _session_override(session_handler, probe_key.address)

    # Confirm the node honoured the override AND that CURRENT_SESSION_SLOT still points at the
    # packed (currentSession, currentSessionValidUntil) pair. A node that silently ignores overrides
    # would otherwise fail the simulation on the signature and have it reported to the user as
    # "this transaction would fail". isSessionActive checks BOTH halves of the slot, so a layout
    # change that moved only the deadline is caught too.
    try:
        if not session_handler.functions.isSessionActive(probe_key.address).call(
            state_override=override
        ):
            return None
    except Exception:  # noqa: BLE001 -- any refusal means overrides are unusable here
        return None

    data = entry_point.encode_abi(abi_element_identifier="handleOps", args=[[op], bundler.address])
    try:
        return w3.eth.estimate_gas(
            {"from": bundler.address, "to": entry_point.address, "data": data},
            block_identifier="pending",
            state_override=override,
        )
    except ContractLogicError as exc:
        raise RuntimeError(f"This transaction would fail: {_revert_reason(exc)}. Nothing was sent.") from exc
    except Exception:  # noqa: BLE001 -- not a revert: the node refused the override on estimateGas
        return None


def _l1_data_gas(w3: Web3, to: str, calldata: bytes) -> int:
    """Arbitrum's charge for posting `calldata` to Ethereum, in L2 gas units, from NodeInterface."""
    selector = keccak(text="gasEstimateL1Component(address,bool,bytes)")[:4]
    data = selector + abi_encode(["address", "bool", "bytes"], [to, False, calldata])
    raw = w3.eth.call({"to": ARB_NODE_INTERFACE, "data": "0x" + data.hex()})
    gas_for_l1, _, _ = abi_decode(["uint64", "uint256", "uint256"], raw)
    return gas_for_l1


def _pre_verification_gas(w3: Web3, chain_name: str, entry_point: Contract, op: tuple, beneficiary: str) -> int:
    """
    preVerificationGas: what the bundler spends on this op that the EntryPoint cannot measure.

    Counted from the handleOps transaction this op will actually ride in: its calldata at 4 gas per
    zero byte and 16 per non-zero byte, plus the fixed costs, plus Arbitrum's L1 data charge where
    one applies. `op` must carry a signature of the real length -- 65 bytes -- or the count is short.
    """
    handle_ops = bytes.fromhex(
        entry_point.encode_abi(abi_element_identifier="handleOps", args=[[op], beneficiary])[2:]
    )
    pvg = _intrinsic_gas(handle_ops) + ENTRY_POINT_OVERHEAD_GAS
    if chain_name in _L1_DATA_GAS_CHAINS:
        pvg += int(_l1_data_gas(w3, entry_point.address, handle_ops) * L1_GAS_BUFFER)
    return pvg


def _send_w3_for(chain_name: str) -> Web3 | None:
    """The private RPC to broadcast through on chain_name, or None to use its normal RPC."""
    url = PRIVATE_SEND_RPC_URLS.get(chain_name)
    if url is None:
        return None
    if chain_name not in _private_send_w3:
        _private_send_w3[chain_name] = Web3(Web3.HTTPProvider(url))
    return _private_send_w3[chain_name]


def _pack_op(
    session_handler: Contract,
    calldata: str,
    nonce: int,
    call_gas: int,
    verification_gas: int,
    pre_verification_gas: int,
    fee_cap: int,
    priority_fee: int,
    signature: bytes,
) -> tuple:
    """
    A PackedUserOperation tuple, packed per ERC-4337 v0.7.

    accountGasLimits = verificationGasLimit (HIGH 128) | callGasLimit (LOW 128); gasFees =
    maxPriorityFeePerGas (HIGH 128) | maxFeePerGas (LOW 128) -- see UserOperationLib and the
    matching read in SessionHandler._validateUserOp. A real cap-and-tip pair, so the EntryPoint
    repays min(cap, tip + base fee): what the outer transaction costs the bundler at the same
    block, and no more.
    """
    return (
        session_handler.address,
        nonce,
        b"",
        bytes.fromhex(calldata[2:]),
        (verification_gas << 128 | call_gas).to_bytes(32, "big"),
        pre_verification_gas,
        (priority_fee << 128 | fee_cap).to_bytes(32, "big"),
        b"",
        signature,
    )


def quote_user_op(
    user_id: int,
    session_handler: Contract,
    entry_point: Contract,
    calldata: str,
    nonce: int,
    bundler: LocalAccount,
) -> UserOpQuote:
    """
    Works out what a UserOp will cost, WITHOUT the session key and without signing anything.

    This is the half of the flow that can run before the user has agreed to the transaction. It
    estimates both gas limits against the wallet as the EntryPoint will call it, sizes
    preVerificationGas from the handleOps calldata the op will ride in, clamps the fee cap to what
    the wallet's own maxOpGasCost allows, and simulates the entire bundle with a throwaway key
    overridden into the session allowlist. Every op it touches is worthless on chain: the only key
    that signed anything here was created for this call and is authorized nowhere.

    A revert anywhere in it means the transaction would fail, and is raised by name. That is the
    cheapest possible place to find out -- before the user is asked, and before the bundler pays.

    @param user_id         The application user ID.
    @param session_handler Bound SessionHandler contract (the UserOp sender).
    @param entry_point     Bound EntryPoint contract.
    @param calldata        Hex-encoded SessionHandler.execute() calldata (0x-prefixed).
    @param nonce           The sender's current nonce, for the estimates only -- the op is built
                           with a freshly read nonce when it is finally signed.
    @param bundler         The account that will sign and pay for handleOps.
    @return                The gas limits, fees and cost of the op.
    @raises RuntimeError   If the op would fail, or the gas price exceeds what the wallet allows.
    """
    w3, _, chain_name = load_network_config(user_id)
    max_fee, tip = _fees(w3)
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    probe_key = Account.create()

    # Estimated first, then buffered: the op carries the LIMIT, the quote keeps the estimate.
    call_gas_used = _estimate_call_gas(w3, entry_point, session_handler, calldata)
    verification_gas_used = _estimate_verification_gas(
        w3, entry_point, session_handler, session_handler.address, nonce, calldata, probe_key
    )
    call_gas = int(call_gas_used * CALL_GAS_BUFFER)
    verification_gas = int(verification_gas_used * VERIFICATION_GAS_BUFFER)

    def build(pre_verification_gas: int, fee_cap: int, priority_fee: int, signature: bytes) -> tuple:
        return _pack_op(
            session_handler, calldata, nonce, call_gas, verification_gas,
            pre_verification_gas, fee_cap, priority_fee, signature,
        )

    pre_verification_gas = _pre_verification_gas(
        w3, chain_name, entry_point, build(0, max_fee, tip, b"\x01" * 65), bundler.address
    )

    # SessionHandler._validateUserOp prices the op at
    #   (verificationGasLimit + callGasLimit + preVerificationGas) * maxFeePerGas
    # and reverts above the account's own maxOpGasCost. That revert happens during validation, where
    # the EntryPoint repays nothing, so clamp the cap rather than let it happen.
    op_gas = verification_gas + call_gas + pre_verification_gas
    affordable_max_fee = session_handler.functions.maxOpGasCost().call() // op_gas
    if max_fee > affordable_max_fee:
        if affordable_max_fee <= base_fee:
            raise RuntimeError(
                f"base fee ({base_fee / 1e9:.1f} gwei) exceeds what this wallet will pay for one "
                f"UserOp ({affordable_max_fee / 1e9:.1f} gwei over {op_gas:,} gas, capped by its "
                f"maxOpGasCost). Nothing was sent — retry when the base fee falls, or raise "
                f"maxOpGasCost on the wallet."
            )
        # Trim the spike cushion, not the tip: the tip is what actually buys inclusion.
        max_fee = affordable_max_fee
        tip = min(tip, max_fee)

    # The whole bundle, signed by the throwaway key and simulated with that key overridden into the
    # allowlist. Catches everything a real submission would hit -- the pause, the spending cap, the
    # oracle, a failing inner call -- and measures what the transaction actually burns.
    #
    # The probe signs the op's REAL userOpHash, which getUserOpHash derives from every field except
    # the signature. Signing anything else recovers some unrelated address, which is not the key the
    # override allows, and the whole simulation dies on AA24 instead of running.
    unsigned_probe = build(pre_verification_gas, max_fee, tip, b"")
    probe_op = build(
        pre_verification_gas, max_fee, tip,
        bytes(
            probe_key.sign_message(
                encode_defunct(entry_point.functions.getUserOpHash(unsigned_probe).call())
            ).signature
        ),
    )
    simulated_gas = _simulate_handle_ops(
        w3, entry_point, session_handler, probe_op, bundler, probe_key
    )
    # Without a simulation, size the outer transaction from the parts. It is an over-estimate of
    # the whole bundle, which costs nothing: the bundler pays for gas used, not for the limit.
    outer_gas = int(simulated_gas * OUTER_GAS_BUFFER) if simulated_gas else op_gas

    return UserOpQuote(
        calldata=calldata,
        call_gas=call_gas,
        call_gas_used=call_gas_used,
        verification_gas=verification_gas,
        verification_gas_used=verification_gas_used,
        pre_verification_gas=pre_verification_gas,
        max_fee=max_fee,
        tip=tip,
        base_fee=base_fee,
        outer_gas=outer_gas,
        simulated_gas=simulated_gas,
        chain_name=chain_name,
    )


def check_bundler_funds(
    user_id: int, quote: UserOpQuote, bundler: LocalAccount, outer_gas: int | None = None
) -> None:
    """
    Refuses a transaction the service could not afford to submit.

    Called twice: once at quote time, so the user is never shown a price the service cannot honour,
    and again just before broadcast against the gas the signed op actually needs.

    @param outer_gas  The outer transaction's gas limit, when a better figure than the quote's is
                      available (prepare_user_op re-estimates it with the real signature).
    @raises RuntimeError if the bundler EOA cannot cover the outer transaction at the fee cap.
    """
    w3, _, _ = load_network_config(user_id)
    needed = (quote.outer_gas if outer_gas is None else outer_gas) * quote.max_fee
    balance = w3.eth.get_balance(bundler.address)
    if balance < needed:
        raise RuntimeError(
            f"The service's bundler account {bundler.address} on {quote.chain_name} holds "
            f"{balance / 1e18:.6f} of the native asset but needs up to {needed / 1e18:.6f} to "
            f"submit this transaction. Nothing was sent; the operator has to top it up."
        )


def prepare_user_op(
    user_id: int,
    key_ciphertext: str,
    session_handler: Contract,
    entry_point: Contract,
    quote: UserOpQuote,
    bundler: LocalAccount,
) -> PreparedUserOp:
    """
    Signs a quoted UserOp with the session key and checks it once more, ready for broadcast_user_op.

    The second half of the flow, and the first point at which an executable op exists. It carries
    the quote's gas limits and fee cap unchanged -- so the transaction that goes out is the one the
    user was quoted -- and re-reads only the nonce, which another op of theirs may have moved since.

    @param user_id         The application user ID.
    @param key_ciphertext  Vault Transit ciphertext for the session key ('vault:v1:...').
    @param session_handler Bound SessionHandler contract (the UserOp sender).
    @param entry_point     Bound EntryPoint contract.
    @param quote           The quote from quote_user_op.
    @param bundler         The account that will sign and pay for handleOps.
    @return                The signed op, its hash, and the outer transaction's gas and fees.
    @raises RuntimeError   If the op would fail, or the bundler cannot afford the outer
                           transaction. Nothing is sent.
    """
    w3, _, _ = load_network_config(user_id)
    nonce = current_session_nonce(user_id, session_handler, entry_point)

    unsigned = _pack_op(
        session_handler, quote.calldata, nonce, quote.call_gas, quote.verification_gas,
        quote.pre_verification_gas, quote.max_fee, quote.tip, b"",
    )
    signed = create_signed_user_op(
        user_id=user_id, user_op=unsigned, entry_point=entry_point, key_ciphertext=key_ciphertext
    )
    user_op_hash = bytes(entry_point.functions.getUserOpHash(unsigned).call())
    from_block = w3.eth.block_number

    # Re-run the whole bundle with the real signature. Nothing has been sent yet, so this is still
    # free, and it catches anything that moved between the quote and the user agreeing to it.
    outer_gas = _estimate(
        w3,
        {
            "from": bundler.address,
            "to": entry_point.address,
            "data": entry_point.encode_abi(
                abi_element_identifier="handleOps", args=[[signed], bundler.address]
            ),
        },
        "Submitting this transaction",
    )
    outer_gas = int(outer_gas * OUTER_GAS_BUFFER)
    fees = {"maxFeePerGas": quote.max_fee, "maxPriorityFeePerGas": quote.tip}

    check_bundler_funds(user_id, quote, bundler, outer_gas)
    return PreparedUserOp(signed, user_op_hash, outer_gas, fees, from_block)


def _find_user_op_receipt(w3: Web3, entry_point: Contract, user_op_hash: bytes, from_block: int):
    """The receipt of whichever transaction executed this UserOp since from_block, or None."""
    events = entry_point.events.UserOperationEvent().get_logs(
        argument_filters={"userOpHash": user_op_hash}, from_block=from_block, to_block="latest"
    )
    if not events:
        return None
    return w3.eth.get_transaction_receipt(events[0]["transactionHash"])


def _replay_revert(w3: Web3, tx: dict, receipt) -> str:
    """Why a mined handleOps reverted, by replaying it against the block before."""
    try:
        w3.eth.call(
            {key: tx[key] for key in ("from", "to", "data", "gas", "maxFeePerGas", "maxPriorityFeePerGas")},
            block_identifier=receipt["blockNumber"] - 1,
        )
    except ContractLogicError as exc:
        return _revert_reason(exc)
    except Exception as exc:  # noqa: BLE001 -- diagnostics only; the revert itself is the error
        return f"replay failed: {exc}"
    return "reason not reported"


def broadcast_user_op(user_id: int, prepared: PreparedUserOp, bundler: LocalAccount):
    """
    Sends a prepared UserOp inside handleOps and returns the transaction that executed it.

    Usually that is our own. But a UserOp's signature does not cover the beneficiary, so once the op
    has been seen -- in the public mempool, or by the RPC during our own estimate -- anyone can land
    it first, naming themselves. Our transaction then reverts on the used nonce, yet the user's
    action HAS happened. Reporting that revert as a failure invites the user to retry and pay twice,
    so a revert or a stall is checked against the chain by the op's hash first.

    @param user_id   The application user ID.
    @param prepared  The op from prepare_user_op.
    @param bundler   The account that prepared it.
    @return          (tx_hash, receipt) of the transaction that executed the op.
    @raises RuntimeError if the op was not executed, or was executed and failed.
    @raises TimeoutError if neither our transaction nor anyone else's executed it in time.
    """
    w3, chain_id, chain_name = load_network_config(user_id)
    entry_point = load_entry_point(user_id=user_id)

    tx =entry_point.functions.handleOps([prepared.op], bundler.address).build_transaction(
        {
            "from": bundler.address,
            # Placeholder only: send_tx overwrites this with a nonce allocated under its lock.
            # Supplying one at all just stops build_transaction from making its own (racy)
            # eth_getTransactionCount call while assembling the dict.
            "nonce": 0,
            "chainId": chain_id,
            "gas": prepared.outer_gas,
            **prepared.fees,
        }
    )

    # One bundler EOA per process signs for every user, so the nonce is allocated under a lock
    # rather than read here. send_and_confirm also bounds the wait and replaces the transaction at a
    # higher fee if the base fee outruns it.
    try:
        receipt = send_and_confirm(w3, chain_name, bundler, tx, send_w3=_send_w3_for(chain_name))
    except TimeoutError:
        receipt = _find_user_op_receipt(w3, entry_point, prepared.user_op_hash, prepared.from_block)
        if receipt is None:
            raise
        print(f"[bundler] our handleOps stalled, but the op was executed in {receipt['transactionHash'].hex()}")
    else:
        if receipt["status"] == 0:
            landed = _find_user_op_receipt(w3, entry_point, prepared.user_op_hash, prepared.from_block)
            if landed is None:
                raise RuntimeError(
                    f"handleOps outer transaction reverted: {_replay_revert(w3, tx, receipt)}"
                )
            print(
                f"[bundler] our handleOps reverted because the op had already been executed, in "
                f"{landed['transactionHash'].hex()} — reporting that one"
            )
            receipt = landed

    # In ERC-4337 the EntryPoint catches an inner-call revert and still mines the outer transaction
    # (status 1); the op's own result is in UserOperationEvent, found here by the op's hash.
    events = [
        e for e in entry_point.events.UserOperationEvent().process_receipt(receipt, errors=DISCARD)
        if bytes(e["args"]["userOpHash"]) == prepared.user_op_hash
    ]
    if not events:
        raise RuntimeError(
            f"handleOps {receipt['transactionHash'].hex()} was mined but did not execute this UserOp."
        )
    evt = events[0]
    if not evt["args"]["success"]:
        # The EntryPoint records WHY in a separate event, carrying the call's raw revert data.
        # Naming it is what lets the agent tell a user "prices are paused" (an L2 sequencer outage)
        # apart from "not enough balance", rather than guessing from a bare failure.
        reasons = [
            r for r in entry_point.events.UserOperationRevertReason().process_receipt(receipt, errors=DISCARD)
            if bytes(r["args"]["userOpHash"]) == prepared.user_op_hash
        ]
        reason = describe_revert_data(reasons[0]["args"]["revertReason"]) if reasons else "reason not reported"
        raise RuntimeError(
            f"UserOperation inner call failed with {reason} "
            f"(nonce={evt['args']['nonce']}, gas_cost={evt['args']['actualGasCost']} wei). "
            f"The transaction was mined but the inner call reverted."
        )

    return receipt["transactionHash"], receipt


def send_user_op_as_session(user_id: int, key_ciphertext: str, target: str, value: int, data: bytes):
    """
    Orchestrates the full ERC-4337 UserOperation flow for a session key holder.

    Packs (target, value, data) into ERC-7579 executionCalldata and encodes
    SessionHandler.execute(mode, executionCalldata) as the UserOp calldata, fetches a nonce keyed to
    the installed SpendingLimitModule, then prepares (estimates, signs, checks) and broadcasts it.

    @param user_id        The application user ID.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param target         The contract address SessionHandler will call (e.g. USDC).
    @param value          The ETH value in wei to forward with the inner call.
    @param data           ABI-encoded inner calldata to execute on the target.
    @return               A tuple of (tx_hash, receipt).
    """
    session_handler, entry_point, calldata, nonce = prepare_execute_call(user_id, target, value, data)
    return _submit_user_op(user_id, key_ciphertext, session_handler, entry_point, calldata, nonce)


def send_batch_user_op_as_session(user_id: int, key_ciphertext: str, executions: list[tuple[str, int, bytes]]):
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
    session_handler, entry_point, calldata, nonce = prepare_execute_batch_call(user_id, executions)
    return _submit_user_op(user_id, key_ciphertext, session_handler, entry_point, calldata, nonce)


def _submit_user_op(user_id: int, key_ciphertext: str, session_handler, entry_point, calldata: str, nonce: int):
    """
    Shared tail of the single-call and batch flows: quote, sign, then broadcast, in one go.

    The unattended path. Anything the user is asked to approve first goes through quote_user_op
    and prepare_user_op separately, so the cost can be shown between the two (see app/quotes.py).
    """
    w3, _, _ = load_network_config(user_id)
    bundler = resolve_bundler(w3)

    print("\n[1/2] Preparing transaction ...")
    quote = quote_user_op(user_id, session_handler, entry_point, calldata, nonce, bundler)
    check_bundler_funds(user_id, quote, bundler)
    prepared = prepare_user_op(
        user_id, key_ciphertext, session_handler, entry_point, quote, bundler
    )
    print("[2/2] Sending transaction   ...")
    return broadcast_user_op(user_id, prepared, bundler)
