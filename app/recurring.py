"""
Recurring payments: reading each wallet's schedules off its RecurringPaymentModule, and wording
them the way the rest of the app words transactions.

A schedule lives on chain. The owner creates and cancels it with a direct transaction from their
own wallet (routes/recurring.py prepares it), and the keeper (keeper.py) pays each instalment when
it falls due by calling the module's run(). wallet.db keeps a copy of each schedule
(db.recurring) so the keeper can find the due ones without reading every wallet, and so the
Controls page can say how the keeper's last attempt went.

The module installed on a wallet is the registry's recurringPaymentModule at the time it was
deployed. The registry's current one is tried first, then any module already on record for that
wallet, so a wallet deployed before the operator changed it keeps working.
"""
import time

from web3 import Web3
from web3.contract import Contract
from web3.logs import DISCARD

import tx_history
from db import (
    get_json,
    get_schedule,
    get_schedules,
    upsert_schedule,
)
from db.recurring import CANCELLED, FINISHED, GONE, LIVE

MODULE_ABI_PATH = "./out/RecurringPaymentModule.sol/RecurringPaymentModule.json"
REGISTRY_ABI_PATH = "./out/SHRegistry.sol/SHRegistry.json"

# ERC-7579's module type for an executor.
MODULE_TYPE_EXECUTOR = 2
# The module's own floor (RecurringPaymentModule.MIN_INTERVAL): checked here too, so a request that
# can only be refused costs no RPC call.
MIN_INTERVAL_SECS = 3_600
# The longest interval the app offers: a year. The module itself takes any.
MAX_INTERVAL_SECS = 366 * 86_400
# Schedules read back per wallet when listing them: the newest this many. A wallet with more than
# this has the older ones only as wallet.db last saw them.
MAX_SCHEDULES_READ = 50

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def module_contract(w3: Web3, address: str) -> Contract:
    """A RecurringPaymentModule at `address`."""
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=get_json(MODULE_ABI_PATH)["abi"])


def module_for_wallet(w3: Web3, chain_id: int, wallet: Contract) -> Contract | None:
    """
    The RecurringPaymentModule installed on `wallet`, or None when it has none -- uninstalled by the
    owner, or a wallet from before recurring payments.

    @param wallet  The user's SessionHandler, bound.
    """
    candidates: list[str] = []
    registry = w3.eth.contract(address=wallet.functions.REGISTRY().call(), abi=get_json(REGISTRY_ABI_PATH)["abi"])
    try:
        current = registry.functions.recurringPaymentModule().call()
    except Exception:  # noqa: BLE001 -- a registry from before recurring payments has no such getter
        current = ZERO_ADDRESS
    if int(current, 16) != 0:
        candidates.append(Web3.to_checksum_address(current))
    for row in _rows_for_wallet(chain_id, wallet.address):
        module = Web3.to_checksum_address(row["module"])
        if module not in candidates:
            candidates.append(module)
    for address in candidates:
        if wallet.functions.isModuleInstalled(MODULE_TYPE_EXECUTOR, address, b"").call():
            return module_contract(w3, address)
    return None


def _rows_for_wallet(chain_id: int, wallet: str) -> list[dict]:
    """Every schedule on record for one wallet, whichever account it is under."""
    from db import get_db

    rows = get_db().execute(
        "SELECT * FROM recurring_payments WHERE chain_id = ? AND wallet = ? ORDER BY schedule_id DESC",
        (chain_id, wallet),
    ).fetchall()
    return [dict(r) for r in rows]


def read_schedule(module: Contract, wallet: str, schedule_id: int) -> tuple[dict, bool]:
    """
    One schedule as the module stores it, and whether it is live.

    @return  ({"token", "recipient", "amount", "interval", "next_due", "end"}, live). All zero once
             the schedule was cancelled or ran its last payment.
    """
    (token, next_due, end, recipient, interval, _generation, amount), live = (
        module.functions.getSchedule(wallet, schedule_id).call()
    )
    return {
        "token": Web3.to_checksum_address(token),
        "recipient": Web3.to_checksum_address(recipient),
        "amount": int(amount),
        "interval": int(interval),
        "next_due": int(next_due),
        "end": int(end),
    }, bool(live)


def _gone_status(row: dict | None, terms: dict) -> str:
    """
    What became of a schedule the module no longer holds as live.

    The module keeps the terms of one wiped by an uninstall (its generation moved on) but zeroes one
    cancelled or finished, and can't say which of those two it was. The row can: a schedule whose
    last due date had come is finished; anything else was cancelled.
    """
    if terms["amount"] != 0:
        return GONE
    if row and row["end_at"] and row["next_due"] + row["interval_secs"] > row["end_at"] \
            and row["next_due"] <= int(time.time()):
        return FINISHED
    return CANCELLED


def save_terms(user_id: int, chain_id: int, wallet: str, module: str, schedule_id: int,
               terms: dict, live: bool) -> dict | None:
    """
    Writes one schedule as read from the chain. A schedule not live and never on record is skipped:
    there is nothing about it worth showing.

    @return  The row, or None when skipped.
    """
    row = get_schedule(chain_id, wallet, schedule_id)
    if live:
        status = LIVE
    elif row is None:
        return None
    else:
        status = _gone_status(row, terms)
    return upsert_schedule(
        user_id, chain_id, wallet, module, schedule_id,
        token=terms["token"], recipient=terms["recipient"], amount=terms["amount"],
        interval_secs=terms["interval"], next_due=terms["next_due"], end_at=terms["end"], status=status,
    )


def sync_wallet(user_id: int, chain_id: int, wallet: str, module: Contract) -> list[dict]:
    """
    Brings wallet.db's copy of one wallet's schedules up to date with the chain: every schedule still
    live on record, and the newest MAX_SCHEDULES_READ ids, which catches one created anywhere --
    from the Controls page, or straight from a block explorer.

    @return  The account's schedules on this chain after the sync (db.get_schedules).
    """
    count = module.functions.scheduleCount(wallet).call()
    ids = set(range(max(1, count - MAX_SCHEDULES_READ + 1), count + 1))
    ids |= {r["schedule_id"] for r in _rows_for_wallet(chain_id, wallet) if r["status"] == LIVE}
    for schedule_id in sorted(ids):
        row = get_schedule(chain_id, wallet, schedule_id)
        if row is not None and row["status"] != LIVE:
            continue  # cancelled or finished: that never changes back
        terms, live = read_schedule(module, wallet, schedule_id)
        save_terms(user_id, chain_id, wallet, module.address, schedule_id, terms, live)
    return get_schedules(user_id, chain_id)


# ── Wording ────────────────────────────────────────────────────────────────────


def interval_text(seconds: int) -> str:
    """"every 30 days", "every week", "every day", "every 6 hours"."""
    for unit, size in (("week", 604_800), ("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds % size == 0 and seconds >= size:
            n = seconds // size
            return f"every {unit}" if n == 1 else f"every {n} {unit}s"
    return f"every {seconds} seconds"


def describe(w3: Web3, user_id: int, chain_id: int, row: dict) -> dict:
    """
    A schedule as the Controls page shows it, worded like the History tab.

    @return  The row's fields plus "ticker", "decimals", "amount_text" (whole units), "recipient_name"
             (a contact's name, "your owner address", or the shortened address) and "summary"
             ("100 USDC to sam every 30 days").
    """
    ticker, decimals = tx_history.token_label(w3, user_id, chain_id, row["token"])
    amount = int(row["amount"])
    amount_text = tx_history.amount_text(amount, decimals) if decimals is not None else f"{amount} base units of"
    recipient_name = tx_history.recipient_label(user_id, row["recipient"])
    return {
        "schedule_id": row["schedule_id"],
        "token": row["token"],
        "ticker": ticker,
        "decimals": decimals,
        "amount_raw": str(amount),
        "amount_text": amount_text,
        "recipient": row["recipient"],
        "recipient_name": recipient_name,
        "interval_secs": row["interval_secs"],
        "next_due": row["next_due"],
        "end_at": row["end_at"] or None,
        "status": row["status"],
        "last_tx": row["last_tx"],
        "last_run_at": row["last_run_at"],
        "last_error": row["last_error"],
        "retry_at": row["retry_at"],
        "summary": f"{amount_text} {ticker} to {recipient_name} {interval_text(row['interval_secs'])}",
    }


def events_in(module: Contract, receipt) -> dict[str, list]:
    """The module's events in a receipt, by name: ScheduleCreated, ScheduleCancelled, RecurringPaymentExecuted."""
    found = {}
    for name in ("ScheduleCreated", "ScheduleCancelled", "RecurringPaymentExecuted"):
        logs = getattr(module.events, name)().process_receipt(receipt, errors=DISCARD)
        # Matched by signature only, so a look-alike event from another contract is dropped here.
        found[name] = [log for log in logs if Web3.to_checksum_address(log["address"]) == module.address]
    return found
