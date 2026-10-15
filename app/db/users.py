"""
Accounts, and the Telegram chat each may be linked to.
"""
import sqlite3
import time

from db import get_db


def create_user(owner_addr: str | None = None) -> int:
    """
    Creates an account and returns its new user ID.

    The web app creates one on an address's first sign-in, so `owner_addr` is always set there;
    only tests make an account without one. The returned id is THE identity for this person
    everywhere else in the app.

    @param owner_addr  The checksummed EOA that signs in to the account and owns its wallets.
    @return            The new user_id.
    @raises ValueError If that address already has an account.
    """
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (owner_addr, created_at) VALUES (?, ?)",
            (owner_addr, int(time.time())),
        )
    except sqlite3.IntegrityError as e:
        raise ValueError(f"Account already exists: {e}")
    db.commit()
    return cur.lastrowid


def get_user_by_id(user_id: int) -> dict | None:
    """
    Returns the account row for a user ID, or None.

    @param user_id  The application user ID.
    @return         A dict of the users row, or None if no such account.
    """
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_owner_addr(owner_addr: str) -> dict | None:
    """
    Returns the account that signs in as this EOA, or None.

    @param owner_addr  A checksummed Ethereum address.
    @return            A dict of the users row, or None.
    """
    row = (
        get_db()
        .execute("SELECT * FROM users WHERE owner_addr = ?", (owner_addr,))
        .fetchone()
    )
    return dict(row) if row else None


def get_user_id_by_telegram_chat_id(chat_id: int) -> int | None:
    """
    Translates a Telegram chat ID into the application user ID it is bound to.

    The bot's entry point. A chat that has not been linked returns None, and the caller must
    refuse to serve it -- an unlinked chat has no account and therefore no wallet.

    @param chat_id  The chat ID taken from the Telegram update (never from user input).
    @return         The user_id, or None if this chat is not linked to any account.
    """
    row = (
        get_db()
        .execute("SELECT id FROM users WHERE telegram_chat_id = ?", (chat_id,))
        .fetchone()
    )
    return row["id"] if row else None


def link_telegram(user_id: int, chat_id: int):
    """
    Binds a Telegram chat to an account.

    @param user_id  The application user ID.
    @param chat_id  The chat ID from the Telegram update.
    @raises ValueError If that chat is already linked to another account.
    """
    db = get_db()
    try:
        db.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat_id, user_id))
    except sqlite3.IntegrityError:
        raise ValueError("That Telegram account is already linked to another user.")
    db.commit()


def unlink_telegram(user_id: int):
    """
    Detaches whatever Telegram chat is bound to an account.

    @param user_id  The application user ID.
    """
    db = get_db()
    db.execute("UPDATE users SET telegram_chat_id = NULL WHERE id = ?", (user_id,))
    db.commit()


def get_telegram_chats_on_chain(chain_id: int) -> list[tuple[int, int]]:
    """
    Every linked Telegram chat whose account has a wallet on `chain_id`: who that chain's bot
    checks on each day.

    @param chain_id  The chain the bot serves.
    @return          [(user_id, chat_id), ...], by user ID.
    """
    rows = (
        get_db()
        .execute(
            """
            SELECT u.id AS user_id, u.telegram_chat_id AS chat_id
            FROM users u
            JOIN session_handlers s ON s.user_id = u.id
            WHERE s.chain_id = ? AND u.telegram_chat_id IS NOT NULL
            ORDER BY u.id
            """,
            (chain_id,),
        )
        .fetchall()
    )
    return [(row["user_id"], row["chat_id"]) for row in rows]
