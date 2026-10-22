"""
Quotes offered for a passkey's approval, frozen at offer time (see schema._migrate_passkeys).

Every state change here is a single conditional UPDATE, so two requests racing for one row -- a
double tap, two devices, the bot and the API -- can never both win: the second finds the row no
longer in the state it needs and changes nothing.
"""
import time

from db import get_db

# The states a request moves through: offered, claimed by a finish, then one of the three ends.
OPEN, SENDING, SENT, FAILED, CANCELLED = "open", "sending", "sent", "failed", "cancelled"


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def open_sign_request(
    quote_id: str,
    user_id: int,
    chain_id: int,
    origin: str,
    quote: str,
    expires_at: int,
    token_hash: str | None = None,
    telegram_chat_id: int | None = None,
) -> dict:
    """
    The sign request for a quote: made the first time the quote is offered, the same row after.

    @param quote       The frozen quote, as JSON (signing.freeze).
    @param expires_at  The quote's own expiry, in Unix seconds.
    @param token_hash  SHA-256 of a Telegram link's token, or None for the web chat. A row that
                       already exists keeps its own; see set_sign_request_token.
    @return            The row.
    """
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO sign_requests (quote_id, user_id, chain_id, origin, token_hash, telegram_chat_id, "
        "quote, created_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (quote_id, user_id, chain_id, origin, token_hash, telegram_chat_id, quote, int(time.time()), expires_at, OPEN),
    )
    db.commit()
    return get_sign_request_by_quote(user_id, chain_id, quote_id)


def set_sign_request_token(request_id: int, token_hash: str) -> bool:
    """
    Gives an open request a new Telegram link token, so the old link dies: a card shown again
    carries a fresh one. False if the request is no longer open.
    """
    db = get_db()
    cur = db.execute(
        "UPDATE sign_requests SET token_hash = ? WHERE id = ? AND status = ?", (token_hash, request_id, OPEN)
    )
    db.commit()
    return cur.rowcount == 1


def set_sign_request_message(request_id: int, chat_id: int, message_id: int):
    """Records the Telegram message carrying the request's buttons, so they can be taken off later."""
    db = get_db()
    db.execute(
        "UPDATE sign_requests SET telegram_chat_id = ?, telegram_message_id = ? WHERE id = ?",
        (chat_id, message_id, request_id),
    )
    db.commit()


def get_sign_request(request_id: int) -> dict | None:
    return _row(get_db().execute("SELECT * FROM sign_requests WHERE id = ?", (request_id,)).fetchone())


def get_sign_request_by_token(token_hash: str) -> dict | None:
    return _row(get_db().execute("SELECT * FROM sign_requests WHERE token_hash = ?", (token_hash,)).fetchone())


def get_sign_request_by_quote(user_id: int, chain_id: int, quote_id: str) -> dict | None:
    return _row(get_db().execute(
        "SELECT * FROM sign_requests WHERE user_id = ? AND chain_id = ? AND quote_id = ?",
        (user_id, chain_id, quote_id),
    ).fetchone())


def get_sign_request_by_sign_id(sign_id: str) -> dict | None:
    return _row(get_db().execute("SELECT * FROM sign_requests WHERE sign_id = ?", (sign_id,)).fetchone())


def get_open_sign_requests(user_id: int, chain_id: int, origin: str | None = None) -> list[dict]:
    """The account's requests on `chain_id` still waiting for an approval, newest first."""
    query = "SELECT * FROM sign_requests WHERE user_id = ? AND chain_id = ? AND status = ? AND expires_at >= ?"
    params: list = [user_id, chain_id, OPEN, int(time.time())]
    if origin is not None:
        query += " AND origin = ?"
        params.append(origin)
    rows = get_db().execute(query + " ORDER BY created_at DESC, id DESC", params).fetchall()
    return [dict(row) for row in rows]


def get_closed_quote_ids(user_id: int, quote_ids: list[str]) -> set[str]:
    """
    Which of these quotes were offered and are no longer waiting: sent, failed, cancelled or
    claimed. How a process learns that a quote it holds was dealt with by the other one.
    """
    if not quote_ids:
        return set()
    marks = ",".join("?" * len(quote_ids))
    rows = get_db().execute(
        f"SELECT quote_id FROM sign_requests WHERE user_id = ? AND status != ? AND quote_id IN ({marks})",
        (user_id, OPEN, *quote_ids),
    ).fetchall()
    return {row["quote_id"] for row in rows}


def save_sign_build(request_id: int, sign_id: str, op: str, challenge: str) -> bool:
    """
    Records a fresh build of the request's UserOperation, replacing the last: the sign_id it is
    known by, the op (JSON) and the challenge (0x hex). An older sign_id stops matching at once.

    @return  False if the request is no longer open, or past its expiry.
    """
    db = get_db()
    cur = db.execute(
        "UPDATE sign_requests SET sign_id = ?, op = ?, challenge = ?, built_at = ? "
        "WHERE id = ? AND status = ? AND expires_at >= ?",
        (sign_id, op, challenge, int(time.time()), request_id, OPEN, int(time.time())),
    )
    db.commit()
    return cur.rowcount == 1


def claim_sign_request(request_id: int, sign_id: str) -> bool:
    """
    Claims an open request for sending, by the build it was approved under. True for exactly one
    caller: the request must still be open, unexpired, and on that build.
    """
    db = get_db()
    cur = db.execute(
        "UPDATE sign_requests SET status = ? WHERE id = ? AND sign_id = ? AND status = ? AND expires_at >= ?",
        (SENDING, request_id, sign_id, OPEN, int(time.time())),
    )
    db.commit()
    return cur.rowcount == 1


def end_sign_request(request_id: int, status: str, outcome: str, transaction_id: int | None = None):
    """Closes a claimed request: SENT or FAILED, with what the user was told and its History row."""
    db = get_db()
    db.execute(
        "UPDATE sign_requests SET status = ?, outcome = ?, transaction_id = ? WHERE id = ?",
        (status, outcome, transaction_id, request_id),
    )
    db.commit()


def cancel_sign_request(request_id: int) -> bool:
    """Cancels an open request. True if it was open; a claimed or finished one is left alone."""
    db = get_db()
    cur = db.execute(
        "UPDATE sign_requests SET status = ? WHERE id = ? AND status = ?", (CANCELLED, request_id, OPEN)
    )
    db.commit()
    return cur.rowcount == 1
