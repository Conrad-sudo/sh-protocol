"""
Recurring payments: each wallet's schedules on its RecurringPaymentModule (see
schema._migrate_recurring_payments), as the Controls page lists them and the keeper pays them.

The chain is the source of truth. upsert_schedule writes what getSchedule says; the keeper's own
fields (last_tx, last_error, failures, retry_at) are only ever written by the keeper's two
functions, record_payment and record_failure, so a refresh from the chain never wipes them.
"""
import time

from db import get_db

LIVE, CANCELLED, FINISHED, GONE = "live", "cancelled", "finished", "gone"


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def upsert_schedule(
    user_id: int,
    chain_id: int,
    wallet: str,
    module: str,
    schedule_id: int,
    *,
    token: str,
    recipient: str,
    amount: int,
    interval_secs: int,
    next_due: int,
    end_at: int,
    status: str,
) -> dict:
    """
    Records one schedule as the chain reports it, or updates the row already there.

    A schedule no longer live on chain keeps the terms it had (getSchedule reads all zero once it is
    deleted), so for one that is not LIVE only the status is written. A row the keeper already
    marked FINISHED stays so: the chain can't tell finished from cancelled.

    @param amount  Base units. Stored as text: it can exceed SQLite's 64-bit integers.
    @return        The row.
    """
    db = get_db()
    now = int(time.time())
    existing = get_schedule(chain_id, wallet, schedule_id)
    if existing is None:
        db.execute(
            "INSERT INTO recurring_payments (user_id, chain_id, wallet, module, schedule_id, token, recipient, "
            "amount, interval_secs, next_due, end_at, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, chain_id, wallet, module, schedule_id, token, recipient, str(amount), interval_secs,
             next_due, end_at, status, now, now),
        )
    elif status == LIVE:
        db.execute(
            "UPDATE recurring_payments SET module = ?, token = ?, recipient = ?, amount = ?, interval_secs = ?, "
            "next_due = ?, end_at = ?, status = ?, updated_at = ? WHERE id = ?",
            (module, token, recipient, str(amount), interval_secs, next_due, end_at, LIVE, now, existing["id"]),
        )
    elif existing["status"] == LIVE:
        db.execute(
            "UPDATE recurring_payments SET status = ?, updated_at = ? WHERE id = ?", (status, now, existing["id"])
        )
    db.commit()
    return get_schedule(chain_id, wallet, schedule_id)


def get_schedule(chain_id: int, wallet: str, schedule_id: int) -> dict | None:
    return _row(
        get_db().execute(
            "SELECT * FROM recurring_payments WHERE chain_id = ? AND wallet = ? AND schedule_id = ?",
            (chain_id, wallet, schedule_id),
        ).fetchone()
    )


def get_schedules(user_id: int, chain_id: int) -> list[dict]:
    """The account's schedules on one chain, newest first: live ones, then the rest."""
    rows = get_db().execute(
        "SELECT * FROM recurring_payments WHERE user_id = ? AND chain_id = ? "
        "ORDER BY status = ? DESC, schedule_id DESC",
        (user_id, chain_id, LIVE),
    ).fetchall()
    return [dict(r) for r in rows]


def get_due_schedules(chain_id: int, now: int, limit: int = 100) -> list[dict]:
    """
    Live schedules on `chain_id` whose payment is due by `now` and not waiting out a refusal,
    the longest-overdue first.
    """
    rows = get_db().execute(
        "SELECT * FROM recurring_payments WHERE chain_id = ? AND status = ? AND next_due <= ? "
        "AND (retry_at IS NULL OR retry_at <= ?) ORDER BY next_due LIMIT ?",
        (chain_id, LIVE, now, now, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def record_payment(row_id: int, tx_hash: str, ran_at: int, next_due: int | None):
    """
    The keeper paid one instalment. `next_due` None means that was the last: the schedule is done.
    Clears any refusal, so the next attempt is not held back.
    """
    db = get_db()
    if next_due is None:
        db.execute(
            "UPDATE recurring_payments SET status = ?, last_tx = ?, last_run_at = ?, last_error = NULL, "
            "failures = 0, retry_at = NULL, updated_at = ? WHERE id = ?",
            (FINISHED, tx_hash, ran_at, ran_at, row_id),
        )
    else:
        db.execute(
            "UPDATE recurring_payments SET next_due = ?, last_tx = ?, last_run_at = ?, last_error = NULL, "
            "failures = 0, retry_at = NULL, updated_at = ? WHERE id = ?",
            (next_due, tx_hash, ran_at, ran_at, row_id),
        )
    db.commit()


def record_failure(row_id: int, error: str, retry_at: int) -> int:
    """
    The keeper's attempt was refused (over the limit, paused, not enough funds, ...). Kept for the
    Controls page; the keeper waits until `retry_at` before trying again.

    @return  How many attempts in a row have now been refused.
    """
    db = get_db()
    db.execute(
        "UPDATE recurring_payments SET last_error = ?, failures = failures + 1, retry_at = ?, updated_at = ? "
        "WHERE id = ?",
        (error, retry_at, int(time.time()), row_id),
    )
    db.commit()
    return db.execute("SELECT failures FROM recurring_payments WHERE id = ?", (row_id,)).fetchone()[0]


def get_wallets_on_chain(chain_id: int) -> list[tuple[int, str]]:
    """Every (user_id, wallet address) with a wallet on `chain_id`: what the keeper scans for schedules."""
    rows = get_db().execute(
        "SELECT user_id, address FROM session_handlers WHERE chain_id = ? ORDER BY user_id", (chain_id,)
    ).fetchall()
    return [(r["user_id"], r["address"]) for r in rows]
