"""
The History tab's record: every transaction made through Mitfah, kept apart from the chat.

The chat is cleared after each transaction (smart_wallet_agent.chat), so a conversation can't be
where a transaction hash lives. Two kinds of row land in the `transactions` table:

  - 'assistant': a UserOperation the agent sent with the session key (tools.confirm_transaction,
    from the web chat or Telegram). Recorded BEFORE it is broadcast, keyed by its userOpHash, and
    filled in with the hash of the transaction that executed it once that is known. A send that
    outlives the wait is therefore still listed, as pending, and settle_pending finishes it later.
  - 'owner': a transaction the user's own wallet signed in the browser -- creating the wallet, the
    owner actions, deposits from the Fund drawer. Recorded when the app is handed its hash, and
    described from the transaction's own calldata, never from anything the browser says.

Every recording function here is best effort and never raises. By the time one runs, the
transaction is already on its way, and an exception would turn a payment that went through into one
that looks failed -- the outcome that leads a user to send it twice.
"""
import functools
import logging
import time
from decimal import Decimal
from typing import Callable

from langchain_erc20 import ERC20_ABI
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.logs import DISCARD

from abi import ientry_point
from bundler import find_user_op_receipt
from constants import get_native_asset_ticker, get_router
from db import (
    add_transaction,
    delete_transaction,
    get_all_contacts,
    get_custom_tokens,
    get_owner_transaction,
    get_pending_transactions,
    get_supported_token_by_address,
    get_user_by_id,
    settle_transaction,
)

log = logging.getLogger(__name__)

# Pending rows settled per read of the History tab. Each costs an RPC call or two, and there is
# rarely more than one.
SETTLE_BATCH = 10
# How long an owner transaction may be unknown to the node before it counts as dropped: replaced by
# a "speed up" in the browser wallet, or never mined at all.
DROP_OWNER_TX_AFTER_SECS = 86_400

_ENTRY_POINT_OF_WALLET_ABI = [
    {
        "type": "function",
        "name": "ENTRY_POINT",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    }
]


def _best_effort(fn):
    """Logs and swallows any exception, for the reason in the module docstring."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 -- recording must never fail the transaction it records
            log.exception("Could not update the transaction history (%s)", fn.__name__)
            return None

    return wrapper


def _hex(value) -> str:
    """A hash as 0x-prefixed lowercase hex, whether it arrived as bytes, HexBytes or a string."""
    if isinstance(value, str):
        value = value.lower()
        return value if value.startswith("0x") else f"0x{value}"
    return f"0x{bytes(value).hex()}"


def _block_time(w3: Web3, receipt) -> int:
    """When a transaction was mined: its block's timestamp, or now if the block can't be read."""
    try:
        return int(w3.eth.get_block(receipt["blockNumber"])["timestamp"])
    except Exception:  # noqa: BLE001 -- a close-enough time is better than no row
        return int(time.time())


def _outcome(w3: Web3, receipt) -> tuple[str, int | None]:
    """(status, mined_at) for a transaction: pending until there is a receipt."""
    if receipt is None:
        return "pending", None
    return ("confirmed" if receipt["status"] == 1 else "failed"), _block_time(w3, receipt)


# ── The assistant's transactions ──────────────────────────────────────────────


@_best_effort
def start_assistant_tx(user_id: int, chain_id: int, wallet: str, action: str, prepared) -> int | None:
    """
    Records a signed UserOperation as pending, just before it is broadcast.

    @param action    The quote's description, written by code from the calldata (tools._action_of).
    @param prepared  The bundler.PreparedUserOp about to be sent.
    @return          The row's id, for finish_assistant_tx or discard_assistant_tx.
    """
    return add_transaction(
        user_id,
        chain_id,
        wallet,
        "assistant",
        action,
        "pending",
        user_op_hash=_hex(prepared.user_op_hash),
        op_nonce=prepared.op[1],
        from_block=prepared.from_block,
    )


@_best_effort
def finish_assistant_tx(tx_id: int | None, w3: Web3, receipt, succeeded: bool):
    """Fills in the transaction that executed the op, and whether its call went through."""
    if tx_id is None:
        return
    settle_transaction(
        tx_id,
        "confirmed" if succeeded else "failed",
        _hex(receipt["transactionHash"]),
        _block_time(w3, receipt),
    )


@_best_effort
def discard_assistant_tx(tx_id: int | None):
    """Drops the row of an op that was never executed: nothing happened, so there is nothing to list."""
    if tx_id is not None:
        delete_transaction(tx_id)


# ── The owner's transactions ──────────────────────────────────────────────────


@_best_effort
def record_owner_tx(w3: Web3, user_id: int, chain_id: int, wallet, tx_hash: str, receipt=None):
    """
    Records a transaction sent TO the user's Mitfah wallet -- an owner action or a deposit -- or
    settles the row already recorded for it.

    The web confirms call this on every poll, so it must be idempotent: the first call to see the
    hash records it, the call that has the receipt settles it. Only a transaction actually sent to
    `wallet` is recorded, so somebody else's hash can't be written into this user's history.

    @param wallet   The user's SessionHandler on `chain_id`, as a bound contract: its ABI decodes the call.
    @param tx_hash  The transaction, as the browser reported it.
    @param receipt  Its receipt once mined; None while it is pending.
    """
    tx_hash = _hex(tx_hash)
    existing = get_owner_transaction(user_id, chain_id, tx_hash)
    if existing and (existing["status"] != "pending" or receipt is None):
        return
    status, mined_at = _outcome(w3, receipt)
    if existing:
        settle_transaction(existing["id"], status, mined_at=mined_at)
        return

    try:
        tx = w3.eth.get_transaction(tx_hash)
    except TransactionNotFound:
        return  # this node hasn't seen it yet; a later poll records it
    if tx["to"] is None or Web3.to_checksum_address(tx["to"]) != wallet.address:
        return
    action = describe_wallet_tx(w3, user_id, chain_id, wallet, tx)
    add_transaction(
        user_id, chain_id, wallet.address, "owner", action, status, tx_hash=tx_hash, mined_at=mined_at
    )


@_best_effort
def record_deploy(w3: Web3, user_id: int, chain_id: int, wallet_address: str, tx_hash: str, receipt):
    """
    Records the transaction that created the user's wallet, or failed to.

    @param wallet_address  The new wallet, or the predicted address when the deploy reverted.
    """
    status, mined_at = _outcome(w3, receipt)
    add_transaction(
        user_id,
        chain_id,
        wallet_address,
        "owner",
        "Create your Mitfah wallet",
        status,
        tx_hash=_hex(tx_hash),
        mined_at=mined_at,
    )


def describe_wallet_tx(w3: Web3, user_id: int, chain_id: int, wallet, tx) -> str:
    """
    What a transaction to the user's wallet does, in one line, read from its own calldata.

    Worded like the assistant's quotes ("Transfer 5 USDC to ..."), and like the controls the user
    pressed, so the History tab reads as one list.
    """
    native = _native(chain_id)
    data = tx.get("input") or b""
    if isinstance(data, str):
        data = bytes.fromhex(data.removeprefix("0x"))
    if not data:
        return f"Add {_amount(tx['value'], 18)} {native} to the wallet"

    try:
        fn, args = wallet.decode_function_input(data)
    except ValueError:
        return "Call your wallet"
    name = fn.fn_name

    if name == "pause":
        return "Pause the wallet"
    if name == "unpause":
        return "Unpause the wallet"
    if name == "withdraw":
        ticker, decimals = _token(w3, user_id, chain_id, args["token"])
        amount = _amount(args["amount"], decimals) if decimals is not None else f"{args['amount']} units of"
        return f"Withdraw {amount} {ticker} to {_who(user_id, args['to'])}"
    if name == "addWatchedToken":
        return f"Count {_token(w3, user_id, chain_id, args['token'])[0]} toward the limit"
    if name == "removeWatchedToken":
        return f"Stop counting {_token(w3, user_id, chain_id, args['token'])[0]} toward the limit"
    if name == "setDailyLimit":
        return f"Set the spending limit to {_usd(args['dailyLimitUsd'])}"
    if name == "setWindowDuration":
        return f"Set the spending period to {_window(args['windowDuration'])}"
    if name == "addSession":
        days = round((args["validUntil"] - time.time()) / 86_400)
        lasting = _plural(days, "day") if days >= 1 else "less than a day"
        return f"Turn the assistant on for {lasting}"
    if name == "removeSession":
        return "Turn the assistant off"
    if name == "addTrustedSpender":
        return f"Trust {_spender(chain_id, args['spender'])} to spend from the wallet"
    if name == "removeTrustedSpender":
        return f"Stop trusting {_spender(chain_id, args['spender'])}"
    if name == "setMaxOpGasCost":
        return f"Set the network fee cap to {_amount(args['newMax'], 18)} {native}"
    return f"Call {name} on your wallet"


def _native(chain_id: int) -> str:
    try:
        return get_native_asset_ticker(chain_id)
    except ValueError:
        return "ETH"


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _short(address: str) -> str:
    """`0x1234…abcd`, as the web app shortens addresses."""
    return f"{address[:6]}…{address[-4:]}"


def _amount(raw: int, decimals: int) -> str:
    """Base units as whole units: no exponent, no trailing zeros."""
    value = (Decimal(raw) / (Decimal(10) ** decimals)).normalize()
    return format(value, "f")


def _usd(scaled: int) -> str:
    """An 18-decimal USD figure as "$100" or "$12.50"."""
    text = f"${Decimal(scaled) / Decimal(10**18):,.2f}"
    return text.removesuffix(".00")


def _window(seconds: int) -> str:
    """A spending period in words, as the web app's formatWindow writes it."""
    if seconds % 86_400 == 0 and seconds > 86_400:
        return _plural(seconds // 86_400, "day")
    if seconds % 3_600 == 0:
        return _plural(seconds // 3_600, "hour")
    return _plural(round(seconds / 60), "minute")


def _token(w3: Web3, user_id: int, chain_id: int, address: str) -> tuple[str, int | None]:
    """(TICKER, decimals) for a token the wallet holds. Decimals are None if they can't be read."""
    address = Web3.to_checksum_address(address)
    if int(address, 16) == 0:
        return _native(chain_id), 18
    listed = get_supported_token_by_address(chain_id, address)
    custom = next(
        (t for t in get_custom_tokens(user_id, chain_id) if t["address"].lower() == address.lower()), None
    )
    ticker = (listed or custom or {}).get("ticker")
    decimals = custom["decimals"] if custom else None
    if decimals is None:
        try:
            decimals = w3.eth.contract(address=address, abi=ERC20_ABI).functions.decimals().call()
        except Exception:  # noqa: BLE001 -- the line still says what happened, in base units
            decimals = None
    return (ticker.upper() if ticker else _short(address)), decimals


def _who(user_id: int, address: str) -> str:
    """A recipient as the user knows it: a contact's name, their owner address, or the address."""
    address = Web3.to_checksum_address(address)
    for contact in get_all_contacts(user_id):
        if Web3.to_checksum_address(contact["address"]) == address:
            return contact["name"]
    user = get_user_by_id(user_id)
    if user and user.get("owner_addr") and Web3.to_checksum_address(user["owner_addr"]) == address:
        return "your owner address"
    return _short(address)


def _spender(chain_id: int, address: str) -> str:
    address = Web3.to_checksum_address(address)
    try:
        if address == Web3.to_checksum_address(get_router(chain_id)):
            return "the exchange router"
    except ValueError:
        pass
    return _short(address)


# ── Settling ──────────────────────────────────────────────────────────────────


def settle_pending(user_id: int, web3_for: Callable[[int], Web3 | None]):
    """
    Finishes the user's rows that are still pending: an assistant send that outlived the wait, or
    an owner transaction whose page was closed before it mined. Run when the History tab is read;
    best effort, row by row, so one unreachable chain holds up nothing else.

    @param web3_for  A Web3 for a chain this server serves, or None for one it doesn't.
    """
    for row in get_pending_transactions(user_id, SETTLE_BATCH):
        try:
            w3 = web3_for(row["chain_id"])
            if w3 is None:
                continue
            if row["source"] == "assistant":
                _settle_assistant_row(w3, row)
            else:
                _settle_owner_row(w3, row)
        except Exception:  # noqa: BLE001 -- e.g. an RPC that refuses a wide log search; try next read
            log.exception("Could not settle transaction %s", row["id"])


def _settle_owner_row(w3: Web3, row: dict):
    try:
        receipt = w3.eth.get_transaction_receipt(row["tx_hash"])
    except TransactionNotFound:
        receipt = None
    if receipt is not None:
        status, mined_at = _outcome(w3, receipt)
        settle_transaction(row["id"], status, mined_at=mined_at)
        return
    if time.time() - row["created_at"] < DROP_OWNER_TX_AFTER_SECS:
        return
    try:
        w3.eth.get_transaction(row["tx_hash"])  # still waiting to be mined
    except TransactionNotFound:
        settle_transaction(row["id"], "dropped")


def _settle_assistant_row(w3: Web3, row: dict):
    if row["user_op_hash"] is None or row["from_block"] is None or row["op_nonce"] is None:
        return
    wallet = Web3.to_checksum_address(row["wallet"])
    entry_point_address = (
        w3.eth.contract(address=wallet, abi=_ENTRY_POINT_OF_WALLET_ABI).functions.ENTRY_POINT().call()
    )
    entry_point = w3.eth.contract(address=entry_point_address, abi=ientry_point)
    op_hash = bytes.fromhex(row["user_op_hash"].removeprefix("0x"))
    nonce = int(row["op_nonce"])

    # Read BEFORE the search: if the search then finds nothing, the nonce was used by some other
    # op, not by a block that executed this one between the two reads.
    nonce_used = entry_point.functions.getNonce(wallet, nonce >> 64).call() > nonce
    receipt = find_user_op_receipt(w3, entry_point, op_hash, row["from_block"])
    if receipt is not None:
        events = [
            e
            for e in entry_point.events.UserOperationEvent().process_receipt(receipt, errors=DISCARD)
            if bytes(e["args"]["userOpHash"]) == op_hash
        ]
        succeeded = bool(events) and bool(events[0]["args"]["success"])
        settle_transaction(
            row["id"],
            "confirmed" if succeeded else "failed",
            _hex(receipt["transactionHash"]),
            _block_time(w3, receipt),
        )
    elif nonce_used:
        settle_transaction(row["id"], "dropped")
