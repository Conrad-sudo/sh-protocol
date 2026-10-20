"""
Feedback about the app itself, sent with the Telegram bot's /feedback. Read by whoever runs the
service, straight from the table. Not the on-chain kind: a rating of the service's ERC-8004 agent is
a transaction the assistant quotes when asked to leave a review.
"""
import time

from db import get_db


def add_feedback(user_id: int, chain_id: int | None, source: str, text: str) -> int:
    """
    Stores one piece of feedback and returns its id.

    @param chain_id  The network the account was on when it wrote, for context; None if none.
    @param source    Where it was written: "telegram".
    @param text      What the user wrote, as they wrote it. The caller bounds its length.
    """
    db = get_db()
    cur = db.execute(
        "INSERT INTO app_feedback (user_id, chain_id, source, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, chain_id, source, text, int(time.time())),
    )
    db.commit()
    return cur.lastrowid


def count_feedback_since(user_id: int, since: int) -> int:
    """How much feedback the account has sent since `since` (Unix seconds), from every source."""
    row = (
        get_db()
        .execute("SELECT COUNT(*) FROM app_feedback WHERE user_id = ? AND created_at >= ?", (user_id, since))
        .fetchone()
    )
    return row[0]
