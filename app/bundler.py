"""
Self-bundling: how every passkey-signed UserOp reaches the EntryPoint, on every network.

The app is its own ERC-4337 bundler everywhere -- plain Anvil, every fork and every live chain. It
builds the UserOp, has the user's passkey sign its hash in their browser, wraps it in
EntryPoint.handleOps() and sends that outer transaction from a bundler EOA of its own, named as
beneficiary so the EntryPoint repays the gas out of the wallet's own prefund. No third-party bundler
ever holds a signed op, and this server holds no key that can sign one.

The order of work is part of the security model:

  1. {quote_user_op} prices the op with no user key at all. The wallet's execute() and
     validateUserOp() are each estimated exactly as the EntryPoint will call them, and the whole
     handleOps bundle is then simulated end to end -- all of it signed by a throwaway passkey
     (passkey.SoftPasskey) that the wallet never approved, made to pass validation by a state
     override that exists only inside the simulation. Nothing the RPC provider sees in this phase
     could be executed on chain.
  2. {finalize_user_op} builds the op the user will approve, when the approval page opens
     (/api/sign/start): fresh fees, a random nonce key, and gas sized for a passkey's signature.
     Its hash is the WebAuthn challenge; the op can't change after that without the signature
     failing.
  3. {check_signed_user_op} runs the whole handleOps once more with the passkey's real signature,
     which catches anything that moved in between and sizes the outer transaction. This is the
     first executable op in the flow, and it exists only because the user approved it with Face
     ID, a fingerprint or a PIN.
  4. {submit_user_op} puts it in the chain's next bundle -- many users' ops in one handleOps, sent
     by one {Lane} per chain and key (see "Bundles" below) -- and {await_user_op} waits for it:
     apart, so an approval can be answered once it is on the wire and the wait left to a
     background thread (landing.py); {broadcast_user_op} does both in one call. Afterwards the op
     is looked up by its hash if its bundle reverted or stalled: a UserOp's signature does not
     cover the beneficiary, so someone else may have landed it first.
"""
import dataclasses
import os
import threading
from collections import Counter, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import dataclass, field

import rlp
from dotenv import load_dotenv
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError
from web3.logs import DISCARD

from contract_errors import describe_revert_data, failed_op_index
from contracts import load_entry_point
from network_config import load_network_config
from parallel import read_all
from passkey import SoftPasskey, packed_length
from tx_sender import Broadcast, confirm, send
from userop import hash_user_op, passkey_nonce

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

# The storage slot of SessionHandler's `sessionValidUntil` mapping (keyId => uint48 deadline): the
# slot (currentSession, currentSessionValidUntil) held before passkeys, kept so this number stays
# the one to check. A key's deadline lives at keccak256(abi.encode(keyId, uint256(9))), which is
# where the whole-bundle simulation plants a throwaway passkey (see {_session_override}). Never
# trusted blind -- the override is checked against the live contract before the simulation is
# believed, so a layout change costs the quote its precision and nothing else. Confirm with
# `forge inspect SessionHandler storageLayout`.
SESSION_VALID_UNTIL_SLOT = 9

# The deadline the override plants. Any far-future timestamp works; it only has to outlast the
# simulated op, and a uint48 cannot hold a value this side of the year 8 million. The wallet returns
# the deadline as the op's validity window, so planting a small one (1, say) would simulate a key
# that expired in 1970 and every quote would die on AA22.
OVERRIDE_SESSION_VALID_UNTIL = 2**47 - 1

# How big a passkey's signature may be, which preVerificationGas is sized for. PVG pays for the
# handleOps calldata, signature included, and is itself part of the hash the passkey signs -- so it
# is fixed before the real signature exists, for the largest one a browser realistically sends:
# authenticatorData of 37 bytes (64 here: the same padded size), and clientDataJSON of about 120
# to 260 bytes (Chrome sometimes adds a member of its own; 384 leaves room). An approval that comes
# back larger is refused rather than sent short: the bundler would pay the difference, and a
# client could make that difference as large as it liked.
SIGNATURE_AUTH_DATA_BUDGET = 64
SIGNATURE_CLIENT_DATA_BUDGET = 384
SIGNATURE_BUDGET = packed_length(SIGNATURE_AUTH_DATA_BUDGET, SIGNATURE_CLIENT_DATA_BUDGET)

# What checking a passkey's signature adds to validation, for a node that refuses state overrides.
# Without the override the throwaway passkey isn't planted, so the wallet turns it away before
# checking the signature at all and the estimate misses the WebAuthn verification -- decoding,
# base64url, two sha256 and the P-256 precompile (3,450 or 6,900 gas), measured well under this.
# Over-sizing validation costs the user nothing: unused validation gas is refunded in full.
P256_VERIFY_FALLBACK_GAS = 40_000

# Arbitrum charges every transaction for posting its data to Ethereum, as extra gas units that the
# EntryPoint never sees. Unless they go into preVerificationGas, the bundler pays that share of
# every op itself. NodeInterface is virtual -- only a real Arbitrum node answers it -- and a fork
# charges no L1 fee anyway, so only the live chain is priced this way.
ARB_NODE_INTERFACE = "0x00000000000000000000000000000000000000C8"
_L1_DATA_GAS_CHAINS = {"arbitrum"}
# OP Stack chains (Base) charge for the same thing differently: a fee in wei on top of the gas, which
# the GasPriceOracle predeploy works out from the transaction's data. It is taken from the bundler's
# balance, so it goes into preVerificationGas as the gas it buys at the price the EntryPoint repays.
# The predeploy does answer on a fork, but a fork charges no L1 fee, so, as with Arbitrum, only the
# live chain is priced this way.
OP_GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"
_L1_FEE_CHAINS = {"base"}
# The L1 price can move between this estimate and inclusion.
L1_GAS_BUFFER = 1.2

# Live chains whose public mempool is watched by bots. A UserOp's signature does not cover the
# beneficiary, so a bot that sees our handleOps can lift an op out, resubmit it naming itself, and
# collect the gas repayment: the user's action still happens, but our transaction reverts and pays
# for nothing -- and in a bundle, it sinks every other op in it too (they go out again, see
# _receipt_that_ran). Broadcasting privately keeps the transaction out of the public mempool. Only
# the BROADCAST goes here -- every read and estimate still uses the chain's normal RPC -- and a fork
# never matches, since its name differs. Arbitrum and Base have no public mempool to watch: their
# sequencer orders what it is sent.
#
# Both endpoints leave out a transaction that would revert rather than mine it, so a bundle that
# goes wrong between the simulation and the block stalls instead of reverting (tx_sender.confirm's
# TimeoutError) -- and, until it is dropped, holds up the sends behind it.
PRIVATE_SEND_RPC_URLS = {
    "mainnet": os.getenv("MAINNET_PRIVATE_RPC_URL") or "https://rpc.flashbots.net/fast",
    # BlockRazor's full-privacy endpoint: straight to BSC's main block builders, disclosing nothing
    # to back-runners (and so earning no refunds). Needs no API key (checked 2026-10-20).
    "bsc": os.getenv("BSC_PRIVATE_RPC_URL") or "https://bsc.blockrazor.xyz/fullprivacy",
}
_private_send_w3: dict[str, Web3] = {}

# Which EOAs sign handleOps is decided by the PROCESS, not the chain. The API and the Telegram bot
# are separate processes, and tx_sender's nonce counter only coordinates threads within one: two
# processes signing with ONE key would hand out the same nonce and replace each other's
# transactions. So each process has keys of its own. The API's are the default; telebot.py switches
# at startup. Anything else that runs the agent in a process of its own (the CLI agent, the smoke
# test) signs with the API's keys, and so must not run beside the API.
#
# Each process holds a small pool of keys, numbered from 1 in .env (API_BUNDLER_1, API_BUNDLER_2,
# ...), each with its own nonce counter and its own Lane. The chain mines one key's transactions
# strictly in nonce order, so with one key a stuck transaction held up every send behind it, for up
# to ~4 minutes (tx_sender's fee bumps). With a pool, a new op goes to the least busy key
# (pick_bundler), so a stall holds up only its own lane. The first key is also the deployer and
# protocol owner on every fork and on live Sepolia (deploy_wallet.LIVE_PRIVATE_KEY_ENV).
API_BUNDLER_ENV = "API_BUNDLER"
TELEGRAM_BUNDLER_ENV = "TELEGRAM_BUNDLER"
_bundler_key_env = API_BUNDLER_ENV


@dataclass
class UserOpQuote:
    """
    What one UserOp will cost, worked out with no user key at all.

    Everything a signed op needs except the signature and the nonce: the gas limits the op will
    carry, the fee cap it was priced at, and the simulated cost of the whole bundle. Held -- and,
    once offered, frozen in its sign request -- between the moment the user is quoted and the moment
    they approve, so the transaction they approve carries the gas it was quoted with. Its fees are
    taken afresh when the approval page opens (finalize_user_op).
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


class UserOpReverted(RuntimeError):
    """
    The op was executed and its call reverted. Unlike every other failure here, it is ON CHAIN: the
    wallet paid for the gas, so the History tab records it with the hash of the transaction that ran it.
    """

    def __init__(self, message: str, tx_hash: bytes, receipt):
        super().__init__(message)
        self.tx_hash = tx_hash
        self.receipt = receipt


@dataclass
class FinalUserOp:
    """
    The op a user is asked to approve (finalize_user_op): complete but for its signature, and the
    hash their passkey must sign. Nothing about it can change after the hash is shown: the
    signature covers every field.
    """

    op: tuple            # the PackedUserOperation, with an empty signature
    user_op_hash: bytes  # the WebAuthn challenge
    max_fee: int         # its maxFeePerGas
    tip: int             # its maxPriorityFeePerGas


@dataclass
class PreparedUserOp:
    """A signed UserOp, checked and sized, ready for broadcast_user_op."""

    op: tuple            # the signed PackedUserOperation
    user_op_hash: bytes  # the EntryPoint's hash of it: how the op is found on chain afterwards
    outer_gas: int       # gas limit for the handleOps transaction
    fees: dict           # EIP-1559 fee fields, shared by the op and the handleOps transaction
    from_block: int      # the chain head before anything was sent; a lost race is searched from here


@dataclass
class SubmittedUserOp:
    """A prepared UserOp on the wire inside a bundle: what await_user_op needs to see it land."""

    prepared: PreparedUserOp
    w3: Web3
    entry_point: Contract
    lane: "Lane"         # the sender it went out through, and goes out through again if resent
    bundle: "Bundle"     # the handleOps carrying it


def use_bundler_key(env_name: str) -> None:
    """
    Selects the env vars holding the keys this process signs handleOps with.

    Called once at startup by any process that must not share the API's keys (telebot.py).

    @param env_name  API_BUNDLER_ENV or TELEGRAM_BUNDLER_ENV: the keys are <env_name>_1, _2, ...
    """
    global _bundler_key_env
    if env_name not in (API_BUNDLER_ENV, TELEGRAM_BUNDLER_ENV):
        raise ValueError(f"Unknown bundler key '{env_name}'")
    _bundler_key_env = env_name


# Env var prefix -> its accounts, derived once.
_bundler_keys: dict[str, list[LocalAccount]] = {}


def bundler_keys() -> list[LocalAccount]:
    """
    This process's bundler accounts: <prefix>_1, <prefix>_2, ... up to the first number not set.

    Each must be a plain EOA on every chain it bundles for: the EntryPoint pays the beneficiary with
    a bare ETH send, which reverts AA91 against an address carrying code. That is why the well-known
    Anvil keys are never used here -- they are EIP-7702-delegated on real mainnet, Sepolia and BSC,
    and a fork inherits that code.

    @raises RuntimeError if not even the first key is set.
    """
    if _bundler_key_env not in _bundler_keys:
        accounts = []
        while key := os.getenv(f"{_bundler_key_env}_{len(accounts) + 1}"):
            accounts.append(Account.from_key(key))
        if not accounts:
            raise RuntimeError(f"{_bundler_key_env}_1 is not set -- this process has no bundler key.")
        _bundler_keys[_bundler_key_env] = accounts
    return _bundler_keys[_bundler_key_env]


def resolve_bundler() -> LocalAccount:
    """
    This process's first bundler account. A quote is simulated from it and checks its balance; a
    send picks its key with pick_bundler instead, unless it names one itself (the tests).
    """
    return bundler_keys()[0]


def _priority_fee(w3: Web3) -> int:
    """The node's suggested tip, in wei, or 2 gwei from a node that doesn't implement eth_maxPriorityFeePerGas."""
    try:
        return w3.eth.max_priority_fee
    except Exception:
        return w3.to_wei(2, "gwei")


def _revert_data(exc: Exception) -> bytes | None:
    """The raw revert data inside a web3 exception, or None when it carries none."""
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        data = data.get("data")
    if isinstance(data, str) and data.startswith("0x") and len(data) > 2:
        try:
            return bytes.fromhex(data[2:])
        except ValueError:
            return None
    return None


def _revert_reason(exc: Exception) -> str:
    """Names the revert inside a web3 exception, falling back to its text."""
    data = _revert_data(exc)
    if data is not None:
        try:
            return describe_revert_data(data)
        except ValueError:
            pass
    return str(exc)


def _estimate(w3: Web3, tx: dict, what: str, state_override: dict | None = None) -> int:
    """
    eth_estimateGas, turning a revert into a RuntimeError that names the contract's error.

    A revert here is the cheapest place to learn a transaction would fail: nothing has been signed
    by the user and nothing has been sent, so it costs the user and the bundler nothing.

    Run against the PENDING block, whose timestamp is the next block's. Against "latest" it would
    run at the last mined block's time instead -- on a quiet chain (a fork most of all) that can be
    hours stale, so a price feed that is past its heartbeat by the time the transaction lands still
    passes the estimate, and the op is mined only to fail and charge the wallet for it.

    @param state_override  Applied inside this one call only (see _session_override); None for none.
    """
    try:
        if state_override is None:
            return w3.eth.estimate_gas(tx, block_identifier="pending")
        return w3.eth.estimate_gas(tx, block_identifier="pending", state_override=state_override)
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
    nonce: int,
    calldata: str,
    probe: SoftPasskey,
    override: dict | None,
) -> int:
    """
    Validation gas: validateUserOp estimated as the EntryPoint calls it, signed by a THROWAWAY
    passkey that the override plants in the wallet for this one call.

    The probe must be planted, unlike the old throwaway Ethereum key: the wallet looks a passkey's
    deadline up BEFORE it checks the signature, and turns away a key it doesn't know at once -- far
    cheaper than the real path, which decodes the approval, rebuilds its challenge and runs the
    P-256 check. With the probe planted, the estimate walks that whole path at the real cost, with
    an approval as long as the longest one budgeted for (SIGNATURE_CLIENT_DATA_BUDGET). Without
    overrides (`override` None) the cost of the check is added instead (P256_VERIFY_FALLBACK_GAS).

    The wallet's challenge is the hash validateUserOp is handed, so the probe signs a zero hash and
    the call names that same hash. The probe's gas fields are zero so the wallet's maxOpGasCost check
    prices it at nothing, and it asks for a 1-wei prefund so the transfer to the EntryPoint is always
    counted. Both over-size validation slightly when the real op needs no top-up, which costs the
    user nothing: unused validation gas is refunded in full.

    @param probe     The throwaway passkey, supplied by the caller so the same one can be planted in
                     the whole-bundle simulation that follows.
    @param override  _session_override for the probe, or None when the node won't apply it.
    """
    probe_hash = b"\x00" * 32
    signature = probe.contract_signature(probe_hash, pad_client_data_to=SIGNATURE_CLIENT_DATA_BUDGET)
    op = (
        session_handler.address, nonce, b"", bytes.fromhex(calldata[2:]),
        b"\x00" * 32, 0, b"\x00" * 32, b"", signature,
    )
    data = session_handler.encode_abi(abi_element_identifier="validateUserOp", args=[op, probe_hash, 1])
    # Unbuffered, for the same reason as the execution estimate: the caller buffers the LIMIT and
    # keeps this as what validation will really cost.
    gas = _estimate(
        w3,
        {"from": entry_point.address, "to": session_handler.address, "data": data},
        "Validating this transaction",
        state_override=override,
    )
    return gas if override is not None else gas + P256_VERIFY_FALLBACK_GAS


def _unused_gas_penalty(gas_used: int, gas_limit: int) -> int:
    """The EntryPoint's fine for reserving execution gas and not using it. See its _getUnusedGasPenalty."""
    if gas_limit <= gas_used + PENALTY_GAS_THRESHOLD:
        return 0
    return (gas_limit - gas_used) * UNUSED_GAS_PENALTY_PERCENT // 100


def session_valid_until_slot(key_id: str) -> bytes:
    """
    Where SessionHandler keeps a passkey's deadline: keccak256(abi.encode(keyId, uint256(9))), the
    mapping's own slot rule.

    @param key_id  0x hex: keccak256(abi.encode(qx, qy)).
    """
    return keccak(abi_encode(["bytes32", "uint256"], [bytes.fromhex(key_id[2:]), SESSION_VALID_UNTIL_SLOT]))


def _session_override(session_handler: Contract, key_id: str) -> dict:
    """
    A state override that makes the wallet approve the passkey `key_id` until far in the future.

    Lets the WHOLE bundle be simulated -- EntryPoint.handleOps end to end, validation and execution
    together -- while it is signed by a throwaway passkey. Without it the simulation stops at AA24,
    so the only way to measure the real cost would be an op the user had already approved, handed
    to the node before they had seen what it costs.

    The wallet returns the key's deadline as the op's ERC-4337 validity window, so the deadline
    planted is far in the future: a small one would simulate as expired and the whole bundle would
    fail AA22 instead of being priced. The key needs no place in the wallet's list of keys
    (getSessions): validation reads only the deadline.

    The override exists only inside that one eth_call: nothing is written to the chain and the key
    is approved nowhere else, so the op being simulated cannot be executed on chain by anybody.
    The slot is checked against the live contract before it is trusted (_override_honoured), so a
    storage-layout change degrades the quote rather than corrupting it.
    """
    slot = session_valid_until_slot(key_id)
    return {
        session_handler.address: {
            "stateDiff": {"0x" + slot.hex(): "0x" + OVERRIDE_SESSION_VALID_UNTIL.to_bytes(32, "big").hex()}
        }
    }


def _override_honoured(session_handler: Contract, key_id: str, override: dict) -> bool:
    """
    Whether the node honours `override` -- and whether SESSION_VALID_UNTIL_SLOT still holds the
    sessionValidUntil mapping.

    A node that silently ignores overrides would otherwise fail the simulation on the signature and
    have it reported to the user as "this transaction would fail". isSessionActive(keyId) reads the
    very slot the override writes, so a moved mapping reads zero and is caught. Needs only the
    throwaway passkey, so it runs alongside the quote's first round of reads.
    """
    try:
        return bool(
            session_handler.functions.isSessionActive(bytes.fromhex(key_id[2:])).call(state_override=override)
        )
    except Exception:  # noqa: BLE001 -- any refusal means overrides are unusable here
        return False


def _simulate_handle_ops(
    w3: Web3,
    entry_point: Contract,
    op: tuple,
    bundler: LocalAccount,
    override: dict,
) -> int | None:
    """
    Gas for the whole handleOps bundle, with the throwaway passkey planted by the override.

    Only called once _override_honoured has said the node will apply `override`.

    @return  The simulated gas, or None if the node refuses the override on estimateGas -- in which
             case the caller sizes the outer transaction from the parts instead. None is a
             degraded quote, not a failure; a revert is a failure and is raised.
    @raises RuntimeError if the bundle would revert.
    """
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


def _l1_fee_gas(w3: Web3, to: str, calldata: bytes, gas_price: int) -> int:
    """
    An OP Stack chain's L1 data fee for a transaction to `to` carrying `calldata`, from GasPriceOracle,
    as gas at `gas_price`.

    getL1Fee prices the whole unsigned transaction, and pads for the signature itself. So the
    calldata goes in an EIP-1559 envelope, whose nonce, fees and gas limit aren't known yet and are
    stood in for by numbers of the same size: the fee depends on the transaction's compressed
    length, not on those values. Priced from the calldata alone it came out 4-17% short of what Base
    charged real handleOps transactions; with the envelope, within 1%.
    """
    chain_id, nonce, tip, fee_cap, gas_limit = 8453, 2**24, 10**6, 10**7, 2**21  # stand-ins, sized like real ones
    unsigned_tx = b"\x02" + rlp.encode(
        [chain_id, nonce, tip, fee_cap, gas_limit, bytes.fromhex(to[2:]), 0, calldata, []]
    )
    data = keccak(text="getL1Fee(bytes)")[:4] + abi_encode(["bytes"], [unsigned_tx])
    raw = w3.eth.call({"to": OP_GAS_PRICE_ORACLE, "data": "0x" + data.hex()})
    (fee_wei,) = abi_decode(["uint256"], raw)
    return -(-fee_wei // max(gas_price, 1))  # rounded up


def _pre_verification_gas(
    w3: Web3, chain_name: str, entry_point: Contract, op: tuple, beneficiary: str, gas_price: int
) -> int:
    """
    preVerificationGas: what the bundler spends on this op that the EntryPoint cannot measure.

    Counted from the handleOps transaction this op will actually ride in: its calldata at 4 gas per
    zero byte and 16 per non-zero byte, plus the fixed costs, plus the L1 data charge where the live
    chain makes one -- Arbitrum's in gas, an OP Stack chain's in wei, turned into gas at `gas_price`,
    the base fee plus tip the EntryPoint will repay at. `op` must carry a signature as long as the
    real one may be -- SIGNATURE_BUDGET bytes, see _budget_signature -- or the count is short.
    """
    handle_ops = bytes.fromhex(
        entry_point.encode_abi(abi_element_identifier="handleOps", args=[[op], beneficiary])[2:]
    )
    pvg = _intrinsic_gas(handle_ops) + ENTRY_POINT_OVERHEAD_GAS
    if chain_name in _L1_DATA_GAS_CHAINS:
        pvg += int(_l1_data_gas(w3, entry_point.address, handle_ops) * L1_GAS_BUFFER)
    elif chain_name in _L1_FEE_CHAINS:
        pvg += int(_l1_fee_gas(w3, entry_point.address, handle_ops, gas_price) * L1_GAS_BUFFER)
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


def _budget_signature(probe: SoftPasskey) -> bytes:
    """
    A passkey signature exactly SIGNATURE_BUDGET bytes long, for sizing preVerificationGas: the
    probe's approval of a zero hash, its clientDataJSON padded to the budget. Laid out as a real
    approval is -- the same words, with zero and non-zero bytes where a browser's would have them --
    so the calldata counted, and an L2's L1 fee, are a real signature's at its largest.
    """
    return probe.contract_signature(b"\x00" * 32, pad_client_data_to=SIGNATURE_CLIENT_DATA_BUDGET)


def _fees(
    w3: Web3,
    chain_name: str,
    entry_point: Contract,
    build: Callable[[int, int, int, bytes], tuple],
    base_fee: int,
    tip: int,
    max_op_gas_cost: int,
    call_gas: int,
    verification_gas: int,
    beneficiary: str,
    signature: bytes,
) -> tuple[int, int, int]:
    """
    preVerificationGas, and the fee cap and tip an op is priced at, from the chain's base fee and
    tip right now. Shared by the quote and the op built for approval, so the two price alike.

    A UserOp's fees are fixed at signing, so a thin cushion strands it the moment the base fee
    climbs past the cap. The 2x base-fee headroom survives a spike of up to ~2x and is the figure
    SessionHandler's {maxOpGasCost} was sized against. It is a cap, not a price: the EntryPoint
    repays at min(cap, tip + base fee), the same as the outer transaction costs the bundler.

    SessionHandler._validateUserOp prices the op at
      (verificationGasLimit + callGasLimit + preVerificationGas) * maxFeePerGas
    and reverts above the account's own maxOpGasCost. That revert happens during validation, where
    the EntryPoint repays nothing, so the cap is clamped rather than let it happen.

    @param build      (preVerificationGas, fee cap, tip, signature) -> the op: PVG is counted from
                      its calldata.
    @param signature  A signature as long as the real one may be (_budget_signature).
    @return           (pre_verification_gas, max_fee, tip).
    @raises RuntimeError  If the base fee is already past what the wallet will pay for the op.
    """
    max_fee = 2 * base_fee + tip
    pre_verification_gas = _pre_verification_gas(
        w3, chain_name, entry_point, build(0, max_fee, tip, signature), beneficiary, base_fee + tip
    )
    op_gas = verification_gas + call_gas + pre_verification_gas
    affordable_max_fee = max_op_gas_cost // op_gas
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
    return pre_verification_gas, max_fee, tip


def quote_user_op(
    user_id: int,
    session_handler: Contract,
    entry_point: Contract,
    calldata: str,
    bundler: LocalAccount,
) -> UserOpQuote:
    """
    Works out what a UserOp will cost, with no user key and without anything the user has approved.

    This is the half of the flow that runs before the user has agreed to the transaction. It
    estimates both gas limits against the wallet as the EntryPoint will call them, sizes
    preVerificationGas from the handleOps calldata the op will ride in (with a signature as long as
    a passkey's may be), clamps the fee cap to what the wallet's own maxOpGasCost allows, and
    simulates the entire bundle signed by a throwaway passkey the override plants in the wallet.
    Every op it touches is worthless on chain: the only key that signed anything here was made for
    this call and is approved nowhere.

    A revert anywhere in it means the transaction would fail, and is raised by name. That is the
    cheapest possible place to find out -- before the user is asked, and before the bundler pays.

    @param user_id         The application user ID.
    @param session_handler Bound SessionHandler contract (the UserOp sender).
    @param entry_point     Bound EntryPoint contract.
    @param calldata        Hex-encoded SessionHandler.execute() calldata (0x-prefixed).
    @param bundler         The account that will sign and pay for handleOps.
    @return                The gas limits, fees and cost of the op.
    @raises RuntimeError   If the op would fail, or the gas price exceeds what the wallet allows.
    """
    w3, chain_id, chain_name = load_network_config(user_id)
    probe = SoftPasskey()
    override = _session_override(session_handler, probe.key_id)
    # Any nonce key nobody has used validates, as long as it carries an op deadline: the op the user
    # approves takes one of its own when it is built (finalize_user_op), so there is no nonce to read.
    # The deadline is the planted key's, so neither can end the simulation early.
    nonce = passkey_nonce(OVERRIDE_SESSION_VALID_UNTIL)

    # Rounds of reads rather than a dozen one after another: everything that needs nothing but the
    # calldata goes out at once, then validation (which needs to know whether the override is
    # honoured), then the whole bundle (which needs every gas figure). See parallel.py.
    first = read_all({
        # First, so that when the transaction would fail, its revert is the error raised.
        "call_gas_used": lambda: _estimate_call_gas(w3, entry_point, session_handler, calldata),
        "block": lambda: w3.eth.get_block("latest"),
        "tip": lambda: _priority_fee(w3),
        "max_op_gas_cost": session_handler.functions.maxOpGasCost().call,
        "override_honoured": lambda: _override_honoured(session_handler, probe.key_id, override),
    })
    honoured = first["override_honoured"]
    base_fee = first["block"]["baseFeePerGas"]

    # Estimated first, then buffered: the op carries the LIMIT, the quote keeps the estimate.
    call_gas_used = first["call_gas_used"]
    verification_gas_used = _estimate_verification_gas(
        w3, entry_point, session_handler, nonce, calldata, probe, override if honoured else None
    )
    call_gas = int(call_gas_used * CALL_GAS_BUFFER)
    verification_gas = int(verification_gas_used * VERIFICATION_GAS_BUFFER)

    def build(pre_verification_gas: int, fee_cap: int, priority_fee: int, signature: bytes) -> tuple:
        return _pack_op(
            session_handler, calldata, nonce, call_gas, verification_gas,
            pre_verification_gas, fee_cap, priority_fee, signature,
        )

    pre_verification_gas, max_fee, tip = _fees(
        w3, chain_name, entry_point, build, base_fee, first["tip"], first["max_op_gas_cost"],
        call_gas, verification_gas, bundler.address, _budget_signature(probe),
    )

    # The whole bundle, signed by the throwaway passkey and simulated with it planted in the
    # wallet. Catches everything a real submission would hit -- the pause, the spending cap, the
    # oracle, a failing inner call -- and measures what the transaction actually burns.
    #
    # The probe signs the op's REAL userOpHash, which getUserOpHash derives from every field except
    # the signature: the wallet's challenge is that hash, so an approval of anything else fails the
    # check and the simulation dies on AA24 instead of running. Its approval is as long as the
    # longest budgeted, as the PVG above assumed.
    unsigned_probe = build(pre_verification_gas, max_fee, tip, b"")
    probe_op = build(
        pre_verification_gas, max_fee, tip,
        probe.contract_signature(
            hash_user_op(unsigned_probe, entry_point, chain_id), pad_client_data_to=SIGNATURE_CLIENT_DATA_BUDGET
        ),
    )
    simulated_gas = _simulate_handle_ops(w3, entry_point, probe_op, bundler, override) if honoured else None
    # Without a simulation, size the outer transaction from the parts. It is an over-estimate of
    # the whole bundle, which costs nothing: the bundler pays for gas used, not for the limit.
    op_gas = verification_gas + call_gas + pre_verification_gas
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
    user_id: int,
    quote: UserOpQuote,
    bundler: LocalAccount,
    outer_gas: int | None = None,
    balance: int | None = None,
) -> None:
    """
    Refuses a transaction the service could not afford to submit.

    Called twice: once at quote time, so the user is never shown a price the service cannot honour,
    and again just before broadcast against the gas the signed op actually needs.

    @param outer_gas  The outer transaction's gas limit, when a better figure than the quote's is
                      available (check_signed_user_op re-estimates it with the real signature).
    @param balance    The bundler's balance, when the caller has already read it alongside other
                      reads; read here otherwise.
    @raises RuntimeError if the bundler EOA cannot cover the outer transaction at the fee cap.
    """
    needed = (quote.outer_gas if outer_gas is None else outer_gas) * quote.max_fee
    if balance is None:
        w3, _, _ = load_network_config(user_id)
        balance = w3.eth.get_balance(bundler.address)
    if balance < needed:
        raise RuntimeError(
            f"The service's bundler account {bundler.address} on {quote.chain_name} holds "
            f"{balance / 1e18:.6f} of the native asset but needs up to {needed / 1e18:.6f} to "
            f"submit this transaction. Nothing was sent; the operator has to top it up."
        )


def finalize_user_op(
    user_id: int,
    session_handler: Contract,
    entry_point: Contract,
    quote: UserOpQuote,
    bundler: LocalAccount,
    op_deadline: int,
) -> FinalUserOp:
    """
    Builds the op a user is about to approve with their passkey, when the approval page opens.

    It carries the quote's calldata and gas limits unchanged -- so the transaction approved is the
    one quoted -- with a fresh nonce key carrying `op_deadline` (userop.passkey_nonce) and fees taken
    now: a quote can be minutes old, and the fee cap it was priced at may already be under the base
    fee.
    preVerificationGas is sized for the largest signature a browser realistically sends
    (SIGNATURE_BUDGET), because it is part of the hash the passkey signs, and so has to be settled
    before the signature exists. The quote's validation limit already has room for the passkey's
    check: it was measured with an approval of that size.

    @param quote    The quote it was offered from (its fees are not used).
    @param bundler      The account the PVG is counted for; any of the process's bundlers sizes alike.
    @param op_deadline  Unix time after which the wallet refuses the op, signed or not.
    @return             The op and its hash, the WebAuthn challenge.
    @raises RuntimeError  If the base fee is already past what the wallet will pay.
    """
    w3, chain_id, chain_name = load_network_config(user_id)
    first = read_all({
        "block": lambda: w3.eth.get_block("latest"),
        "tip": lambda: _priority_fee(w3),
        "max_op_gas_cost": session_handler.functions.maxOpGasCost().call,
    })
    nonce = passkey_nonce(op_deadline)

    def build(pre_verification_gas: int, fee_cap: int, priority_fee: int, signature: bytes) -> tuple:
        return _pack_op(
            session_handler, quote.calldata, nonce, quote.call_gas, quote.verification_gas,
            pre_verification_gas, fee_cap, priority_fee, signature,
        )

    pre_verification_gas, max_fee, tip = _fees(
        w3, chain_name, entry_point, build, first["block"]["baseFeePerGas"], first["tip"],
        first["max_op_gas_cost"], quote.call_gas, quote.verification_gas, bundler.address,
        _budget_signature(SoftPasskey()),
    )
    op = build(pre_verification_gas, max_fee, tip, b"")
    return FinalUserOp(op, hash_user_op(op, entry_point, chain_id), max_fee, tip)


def check_signed_user_op(
    user_id: int,
    entry_point: Contract,
    quote: UserOpQuote,
    final: FinalUserOp,
    signature: bytes,
    bundler: LocalAccount,
) -> PreparedUserOp:
    """
    The approved op with its passkey signature, checked once more and sized, ready to submit.

    The first point at which an executable op exists. The whole handleOps runs again with the real
    signature: nothing has been sent yet, so this is still free, and it catches anything that moved
    since the quote -- the price, the cap, the key itself revoked or run out -- before the bundler
    pays. The estimate also sizes the outer transaction, and the bundler's balance is checked
    against it.

    @param quote      The quote the op was built from: the chain and the outer gas fall back to it.
    @param final      The op as finalize_user_op built it, and its hash.
    @param signature  The passkey's approval, packed as the wallet reads it (passkey.contract_signature).
    @param bundler    The account that will sign and pay for handleOps.
    @return           The signed op, its hash, and the outer transaction's gas and fees.
    @raises RuntimeError  If the op would fail, or the bundler cannot afford the outer transaction.
                          Nothing is sent.
    """
    w3, _, _ = load_network_config(user_id)
    signed = final.op[:-1] + (signature,)
    data = entry_point.encode_abi(abi_element_identifier="handleOps", args=[[signed], bundler.address])
    # The three reads this needs depend on nothing but the op, so they go out together. from_block
    # is read before the op is sent, so the search for its receipt can only start early.
    first = read_all({
        # First, so that a revert is the error raised.
        "outer_gas": lambda: _estimate(
            w3, {"from": bundler.address, "to": entry_point.address, "data": data}, "Submitting this transaction"
        ),
        "from_block": lambda: w3.eth.block_number,
        "bundler_balance": lambda: w3.eth.get_balance(bundler.address),
    })
    outer_gas = int(first["outer_gas"] * OUTER_GAS_BUFFER)
    priced = dataclasses.replace(quote, max_fee=final.max_fee, tip=final.tip)
    check_bundler_funds(user_id, priced, bundler, outer_gas, balance=first["bundler_balance"])
    fees = {"maxFeePerGas": final.max_fee, "maxPriorityFeePerGas": final.tip}
    return PreparedUserOp(signed, final.user_op_hash, outer_gas, fees, first["from_block"])


def find_user_op_receipt(w3: Web3, entry_point: Contract, user_op_hash: bytes, from_block: int):
    """
    The receipt of whichever transaction executed this UserOp since from_block, or None.

    Also how tx_history settles a send that outlived the wait in broadcast_user_op.
    """
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


# ── Bundles: many users' ops in one handleOps ─────────────────────────────────
#
# Every op is prepared on its own -- priced, signed and checked as a bundle of one. Until 2026-10-20
# each went out in a handleOps of its own too, and since one key's transactions are signed and
# broadcast one at a time (tx_sender's nonce lock), that held each process to one op per RPC round
# trip, about ten a second, however fast the chain. Now each op joins a queue, and one sender
# thread per chain and key (a Lane) sends whatever has piled up while it was busy as ONE handleOps.
#
# There is no timer. An op that finds the lane idle goes straight out on its own, exactly as before,
# so a quiet server is no slower; under load the bundles grow by themselves. What sharing a
# transaction changes, and how each is handled:
#
#   - One bad op reverts the lot. handleOps validates every op before running any, and one that
#     fails validation reverts the whole transaction with FailedOp(index). So a bundle of several
#     is simulated first; the op that fails is refused to its own user (NotSent) and dropped, and
#     the rest are simulated again. A bundle of one skips this: check_signed_user_op has just done it.
#   - One fee for many ops. The EntryPoint repays each op at min(its cap, base fee + its tip), so a
#     bundle is priced at the lowest cap and the lowest tip among its ops (_bundle_fees). An op
#     whose cap the base fee has already passed would hold that price down for everyone, so it
#     goes out on its own.
#   - One wait. Every op in a bundle shares one tx_sender.confirm, and with it one set of fee bumps;
#     each op's own wait (await_user_op) reads its result out of the shared receipt.
#   - One op per wallet. Two ops of one wallet are prepared with the same nonce, so the second
#     would sink the bundle; it waits for the next one instead.

# How many ops one handleOps may carry, and how much gas (user's call, 2026-10-20). The gas counted
# is the sum of each op's outer gas as a bundle of one -- an over-count, since a bundle pays the
# transaction's base cost once -- and is held under the chain's own per-transaction limit, past
# which the node refuses the transaction outright.
BUNDLE_MAX_OPS = 40
BUNDLE_MAX_GAS = 20_000_000
# EIP-7825's per-transaction cap, 2^24 gas: enforced on Ethereum (Fusaka), Sepolia, BSC (BEP-652)
# and Base (Azul). Arbitrum allows 32M (ArbOS Dia). A chain not listed is held to the lower figure.
TX_GAS_CAP = 2**24
_TX_GAS_CAP_BY_CHAIN = {"arbitrum": 32_000_000}
# How many more bundles an op goes out in after one reverted without running it -- a rival landed
# another op of that bundle first, or something moved between the simulation and the block.
MAX_RESENDS = 2


def bundle_gas_cap(chain_name: str) -> int:
    """The most gas one bundle may carry on chain_name. A fork is held to its live chain's limit."""
    return min(BUNDLE_MAX_GAS, _TX_GAS_CAP_BY_CHAIN.get(chain_name.removesuffix("-fork"), TX_GAS_CAP))


class NotSent(RuntimeError):
    """The op was refused before any transaction carried it: nothing went out, and nothing will."""


@dataclass
class Bundle:
    """One handleOps on the wire, and the one wait for it that every op in it shares."""

    tx: dict             # as built; tx_sender's replacements change only its fees
    sent: Broadcast
    landed: Future       # its receipt, or tx_sender.confirm's TimeoutError


@dataclass(eq=False)
class _Queued:
    """An op waiting for a bundle, and how its sender hears it went out -- or why it can't."""

    prepared: PreparedUserOp
    on_wire: Future = field(default_factory=Future)  # -> the Bundle carrying it
    alone: bool = False  # to go out in a bundle of its own (see Lane._check)


def _bundle_fees(ops: list[PreparedUserOp]) -> dict:
    """
    One fee for a bundle: the lowest cap and the lowest tip among its ops. The EntryPoint repays
    each op at min(its cap, base fee + its tip), so priced any higher the transaction could cost the
    bundler more than an op repays. A bundle of one is priced at its op's own fees, as before.
    """
    return {
        "maxFeePerGas": min(p.fees["maxFeePerGas"] for p in ops),
        "maxPriorityFeePerGas": min(p.fees["maxPriorityFeePerGas"] for p in ops),
    }


class Lane:
    """
    The one sender for a chain and a bundler key in this process: a queue of prepared ops, and a
    thread that sends whatever is waiting as one handleOps each time it is free.
    """

    def __init__(self, w3: Web3, chain_name: str, chain_id: int, entry_point: Contract, bundler: LocalAccount):
        self.w3, self.chain_name, self.chain_id = w3, chain_name, chain_id
        self.entry_point, self.bundler = entry_point, bundler
        self.send_w3 = _send_w3_for(chain_name)
        self.gas_cap = bundle_gas_cap(chain_name)
        self._queue: deque[_Queued] = deque()
        self._ready = threading.Condition()
        # What the bundles still in flight could cost the bundler, at their fee caps. A node refuses
        # a transaction its sender can't pay for on top of the ones it already has pending.
        self._reserved = 0
        # The ops held against this lane, by wallet: picked for it and not yet on the wire
        # (pick_bundler), or in a bundle that has not landed yet. What pick_bundler weighs it by.
        self._held: Counter[str] = Counter()
        self._lock = threading.Lock()  # guards _reserved and _held
        threading.Thread(target=self._run, name=f"lane-{chain_name}", daemon=True).start()

    def load(self) -> int:
        """How many ops are held against this lane: picked for it, queued, or in a bundle not landed."""
        with self._lock:
            return sum(self._held.values())

    def holds(self, wallet: str) -> bool:
        """Whether one of this wallet's ops is held against this lane."""
        with self._lock:
            return wallet in self._held

    def count(self, wallets: list[str], step: int) -> None:
        """Holds (step 1) or lets go of (step -1) one op of each wallet against this lane."""
        with self._lock:
            for wallet in wallets:
                self._held[wallet] += step
                if not self._held[wallet]:
                    del self._held[wallet]

    def submit(self, prepared: PreparedUserOp) -> Bundle:
        """
        Queues a prepared op, and returns the bundle carrying it once that is on the wire.

        @raises NotSent  If it was refused before going out: it would fail inside the bundle, or the
                         bundler can't afford the bundle.
        @raises          The node's refusal, unchanged, when the bundle could not be broadcast.
        """
        queued = _Queued(prepared)
        with self._ready:
            self._queue.append(queued)
            self._ready.notify()
        return queued.on_wire.result()

    def _run(self) -> None:
        while True:
            with self._ready:
                while not self._queue:
                    self._ready.wait()
                batch = self._take()
            try:
                self._send(batch)
            except BaseException as error:  # noqa: BLE001 -- each op's sender hears it; the lane goes on
                for queued in batch:
                    if not queued.on_wire.done():
                        queued.on_wire.set_exception(error)

    def _take(self) -> list[_Queued]:
        """
        The next bundle's ops, in the order they came: up to BUNDLE_MAX_OPS and the gas cap, one per
        wallet. The first op waiting always goes, whatever its size; one marked `alone` goes only
        first, on its own. Called holding the queue's lock.
        """
        head = self._queue.popleft()
        if head.alone:
            return [head]
        batch, gas, wallets, skipped = [head], head.prepared.outer_gas, {head.prepared.op[0]}, []
        while self._queue and len(batch) < BUNDLE_MAX_OPS:
            queued = self._queue.popleft()
            wallet = queued.prepared.op[0]
            if queued.alone or wallet in wallets or gas + queued.prepared.outer_gas > self.gas_cap:
                skipped.append(queued)
                continue
            batch.append(queued)
            gas += queued.prepared.outer_gas
            wallets.add(wallet)
        self._queue.extendleft(reversed(skipped))
        return batch

    def _requeue(self, ops: list[_Queued]) -> None:
        """Puts ops back at the front of the queue, in order, each to go out on its own."""
        for queued in ops:
            queued.alone = True
        with self._ready:
            self._queue.extendleft(reversed(ops))
            self._ready.notify()

    def _send(self, batch: list[_Queued]) -> None:
        """Sends `batch` as one handleOps -- checked first if it holds several -- and starts its wait."""
        if len(batch) > 1:
            outer_gas = self._check(batch)
            if not batch:
                return
        else:
            outer_gas = batch[0].prepared.outer_gas
        fees = _bundle_fees([queued.prepared for queued in batch])

        tx = self.entry_point.functions.handleOps(
            [queued.prepared.op for queued in batch], self.bundler.address
        ).build_transaction(
            {
                "from": self.bundler.address,
                # Placeholder only: send_tx overwrites this with a nonce allocated under its lock.
                # Supplying one at all just stops build_transaction from making its own (racy)
                # eth_getTransactionCount call while assembling the dict.
                "nonce": 0,
                "chainId": self.chain_id,
                "gas": outer_gas,
                **fees,
            }
        )
        sent = send(self.w3, self.chain_name, self.bundler, tx, send_w3=self.send_w3)
        cost = outer_gas * fees["maxFeePerGas"]
        wallets = [queued.prepared.op[0] for queued in batch]
        with self._lock:
            self._reserved += cost
        # Held until the bundle lands; counted before any sender hears it is on the wire, so an op
        # is never left out between pick_bundler letting go of it and this taking it up.
        self.count(wallets, 1)
        bundle = Bundle(tx, sent, Future())
        threading.Thread(
            target=self._wait, args=(bundle, cost, wallets), name="bundle-wait", daemon=True
        ).start()
        for queued in batch:
            queued.on_wire.set_result(bundle)

    def _check(self, batch: list[_Queued]) -> int:
        """
        Simulates a bundle of several, and returns the gas it needs. `batch` is whittled down IN
        PLACE, so whoever holds it never hears about an op twice.

        An op the EntryPoint rejects is refused to its own user (NotSent) and dropped, and the rest
        simulated again. One whose fee cap the base fee has already passed goes back to the queue to
        go out alone, and so does every op of a bundle that reverts without saying which op it was.
        The bundler's balance is read alongside, and the lot refused if it can't cover this bundle
        on top of the ones in flight.

        @raises NotSent  For every op left, if the bundle couldn't be simulated or afforded.
        """
        while len(batch) > 1:
            data = self.entry_point.encode_abi(
                abi_element_identifier="handleOps", args=[[q.prepared.op for q in batch], self.bundler.address]
            )
            try:
                first = read_all({
                    # First, so that a revert is the error raised.
                    "gas": lambda: self.w3.eth.estimate_gas(
                        {"from": self.bundler.address, "to": self.entry_point.address, "data": data},
                        block_identifier="pending",
                    ),
                    "block": lambda: self.w3.eth.get_block("latest"),
                    "balance": lambda: self.w3.eth.get_balance(self.bundler.address),
                })
            except ContractLogicError as exc:
                revert = _revert_data(exc)
                index = failed_op_index(revert) if revert is not None else None
                if index is None or index >= len(batch):
                    self._requeue(batch[:])
                    batch.clear()
                    return 0
                batch.pop(index).on_wire.set_exception(
                    NotSent(f"Submitting this transaction would fail: {_revert_reason(exc)}. Nothing was sent.")
                )
                continue
            except Exception as exc:
                raise NotSent(
                    f"Couldn't check this transaction before sending it ({type(exc).__name__}). Nothing was sent."
                ) from exc

            base_fee = first["block"].get("baseFeePerGas", 0)
            stale = [q for q in batch if q.prepared.fees["maxFeePerGas"] < base_fee]
            if stale:
                batch[:] = [q for q in batch if q.prepared.fees["maxFeePerGas"] >= base_fee]
                self._requeue(stale)
                continue

            outer_gas = int(first["gas"] * OUTER_GAS_BUFFER)
            needed = outer_gas * _bundle_fees([q.prepared for q in batch])["maxFeePerGas"]
            with self._lock:
                reserved = self._reserved
            if first["balance"] < reserved + needed:
                raise NotSent(
                    f"The service's bundler account {self.bundler.address} on {self.chain_name} holds "
                    f"{first['balance'] / 1e18:.6f} of the native asset but needs up to "
                    f"{(reserved + needed) / 1e18:.6f} to submit this transaction alongside the ones "
                    f"still in flight. Nothing was sent; the operator has to top it up."
                )
            return outer_gas
        # Whittled down to one: check_signed_user_op has already simulated it as a bundle of one.
        return batch[0].prepared.outer_gas if batch else 0

    def _wait(self, bundle: Bundle, cost: int, wallets: list[str]) -> None:
        """The bundle's one wait, fee bumps and all (tx_sender.confirm), shared by every op in it."""
        try:
            bundle.landed.set_result(confirm(bundle.sent))
        except BaseException as error:  # noqa: BLE001 -- handed to every op's wait
            bundle.landed.set_exception(error)
        finally:
            with self._lock:
                self._reserved -= cost
            self.count(wallets, -1)


# (chain name, bundler address, EntryPoint address) -> its Lane, made on first use.
_lanes: dict[tuple[str, str, str], Lane] = {}
_lanes_lock = threading.Lock()  # guards _lanes, and makes each pick_bundler choice in turn


def _lanes_for(user_id: int, accounts: list[LocalAccount]) -> list[Lane]:
    """The Lanes for the user's acting chain and these bundler keys, in the same order."""
    w3, chain_id, chain_name = load_network_config(user_id)
    entry_point = load_entry_point(user_id=user_id)
    lanes = []
    with _lanes_lock:
        for account in accounts:
            key = (chain_name, account.address, entry_point.address)
            if key not in _lanes:
                _lanes[key] = Lane(w3, chain_name, chain_id, entry_point, account)
            lanes.append(_lanes[key])
    return lanes


def _lane_for(user_id: int, bundler: LocalAccount) -> Lane:
    """The Lane for the user's acting chain and this bundler key."""
    return _lanes_for(user_id, [bundler])[0]


@contextmanager
def pick_bundler(user_id: int, wallet: str) -> Iterator[LocalAccount]:
    """
    The bundler key to send one of a wallet's ops with, held against that key until it is on the
    wire. Prepare the op with it, and submit it, inside the `with`.

    A wallet with an op still held against one key goes with that key again: the chain then mines
    its ops in order, and the lane keeps one op per wallet in each bundle (Lane._take). Any other op
    goes to the key with the fewest ops held -- picked, queued, or in a bundle not landed yet -- and
    so away from a key whose transaction is stuck; on a tie, the first key. Held from the moment it
    is picked, so ops picked at the same moment spread across the keys rather than all going to
    whichever was idle.

    @param user_id  The application user ID: the lanes are its acting chain's.
    @param wallet   The op's sender, as its PackedUserOperation names it (op[0]).
    """
    lanes = _lanes_for(user_id, bundler_keys())
    with _lanes_lock:
        lane = next((lane for lane in lanes if lane.holds(wallet)), None) or min(lanes, key=Lane.load)
        lane.count([wallet], 1)
    try:
        yield lane.bundler
    finally:
        lane.count([wallet], -1)


def submit_user_op(user_id: int, prepared: PreparedUserOp, bundler: LocalAccount) -> SubmittedUserOp:
    """
    Puts a prepared UserOp in the chain's next bundle, and returns once that is on the wire --
    at once, as the only op in it, when nothing else is waiting.

    @param user_id   The application user ID.
    @param prepared  The op from check_signed_user_op.
    @param bundler   The account that prepared it.
    @return          What await_user_op waits on. Everything it needs is bound here, so it can wait
                     on any thread, outside the turn's acting network (db.acting_network).
    @raises NotSent  If it was refused before going out: it would fail inside the bundle, or the
                     bundler can't afford the bundle. Nothing was sent.
    @raises          The node's refusal, unchanged, when the bundle could not be broadcast.
    """
    lane = _lane_for(user_id, bundler)
    return SubmittedUserOp(prepared, lane.w3, lane.entry_point, lane, lane.submit(prepared))


def _receipt_that_ran(submitted: SubmittedUserOp):
    """
    The receipt of the transaction that executed a submitted op: its bundle's, or a rival's.

    A UserOp's signature does not cover the beneficiary, so once the op has been seen -- in the
    public mempool, or by the RPC during our own estimate -- anyone can land it first, naming
    themselves. Our bundle then reverts on the used nonce, yet the user's action HAS happened.
    Reporting that revert as a failure invites the user to retry and pay twice, so a revert or a
    stall is checked against the chain by the op's hash first.

    A bundle that reverted without anyone running the op -- a rival landed ANOTHER of its ops, or
    something moved since the simulation -- is not this op's failure either. The op is simulated
    once more on its own (if it is the one that sank the bundle, its user hears why, and no more gas
    is spent on it) and goes out in a later bundle, up to MAX_RESENDS times.

    @raises RuntimeError  if the op was not executed.
    @raises TimeoutError  if neither our bundle nor anyone else's executed it in time.
    """
    prepared, w3, entry_point = submitted.prepared, submitted.w3, submitted.entry_point
    bundle = submitted.bundle
    for resend in range(MAX_RESENDS + 1):
        try:
            receipt = bundle.landed.result()
        except TimeoutError:
            landed = find_user_op_receipt(w3, entry_point, prepared.user_op_hash, prepared.from_block)
            if landed is None:
                raise
            print(f"[bundler] our handleOps stalled, but the op was executed in {landed['transactionHash'].hex()}")
            return landed
        if receipt["status"] == 1:
            return receipt

        landed = find_user_op_receipt(w3, entry_point, prepared.user_op_hash, prepared.from_block)
        if landed is not None:
            print(
                f"[bundler] our handleOps reverted because the op had already been executed, in "
                f"{landed['transactionHash'].hex()} — reporting that one"
            )
            return landed
        if resend == MAX_RESENDS:
            break
        beneficiary = submitted.lane.bundler.address
        _estimate(
            w3,
            {
                "from": beneficiary,
                "to": entry_point.address,
                "data": entry_point.encode_abi(abi_element_identifier="handleOps", args=[[prepared.op], beneficiary]),
            },
            "Submitting this transaction",
        )
        print("[bundler] its bundle reverted without running the op — sending it again")
        bundle = submitted.lane.submit(prepared)

    raise RuntimeError(f"handleOps outer transaction reverted: {_replay_revert(w3, bundle.tx, receipt)}")


def await_user_op(submitted: SubmittedUserOp):
    """
    Waits for a submitted UserOp and returns the transaction that executed it.

    Usually that is its own bundle; see _receipt_that_ran for when it is not, and for a bundle that
    reverted without running it. The bundle's one wait (Lane._wait, tx_sender.confirm) bounds how
    long, and replaces the transaction at a higher fee if the base fee outruns it.

    @param submitted  The op from submit_user_op.
    @return           (tx_hash, receipt) of the transaction that executed the op.
    @raises UserOpReverted  if the op was executed and its call reverted (a RuntimeError, carrying
                            that transaction's hash and receipt).
    @raises RuntimeError    if the op was not executed.
    @raises TimeoutError    if neither our transaction nor anyone else's executed it in time.
    """
    prepared, entry_point = submitted.prepared, submitted.entry_point
    receipt = _receipt_that_ran(submitted)

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
        raise UserOpReverted(
            f"UserOperation inner call failed with {reason} "
            f"(nonce={evt['args']['nonce']}, gas_cost={evt['args']['actualGasCost']} wei). "
            f"The transaction was mined but the inner call reverted.",
            receipt["transactionHash"],
            receipt,
        )

    return receipt["transactionHash"], receipt


def broadcast_user_op(user_id: int, prepared: PreparedUserOp, bundler: LocalAccount):
    """
    Sends a prepared UserOp and waits for it, on the calling thread: submit_user_op, then
    await_user_op. For a caller with nothing to answer in between (the tests).

    @return  (tx_hash, receipt) of the transaction that executed the op.
    @raises  As submit_user_op and await_user_op do.
    """
    return await_user_op(submit_user_op(user_id, prepared, bundler))
