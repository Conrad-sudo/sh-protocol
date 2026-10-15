"""
What signing in leaves behind: refresh tokens, and the single-use nonces for SIWE, contact signatures
and Telegram links.
"""
import time

from db import get_db


def save_telegram_link_nonce(nonce: str, user_id: int, expires_at: int):
    """
    Records a pending Telegram link.

    @param nonce       A single-use random token, carried in the t.me deep link.
    @param user_id     The account the nonce will bind the chat to.
    @param expires_at  Unix seconds after which the nonce is refused.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO telegram_link_nonces (nonce, user_id, expires_at) VALUES (?, ?, ?)",
        (nonce, user_id, expires_at),
    )
    db.commit()


def consume_telegram_link_nonce(nonce: str) -> int | None:
    """
    Redeems a Telegram link nonce, returning the user it was minted for.

    Burns the nonce whether or not it had expired, so a leaked deep link cannot be retried, and
    deletes before checking expiry for the same reason.

    @param nonce  The token from /start's payload.
    @return       The user_id, or None if the nonce is unknown or expired.
    """
    db = get_db()
    row = (
        db.execute(
            "SELECT user_id, expires_at FROM telegram_link_nonces WHERE nonce = ?", (nonce,)
        ).fetchone()
    )
    db.execute("DELETE FROM telegram_link_nonces WHERE nonce = ?", (nonce,))
    db.commit()
    if row is None or row["expires_at"] < int(time.time()):
        return None
    return row["user_id"]


def save_refresh_token(token_hash: str, user_id: int, expires_at: int):
    """
    Stores the SHA-256 of a refresh token.

    @param token_hash  Hex SHA-256 of the opaque token. The token itself is never stored, so a
                       database read cannot be replayed as a login.
    @param user_id     The account the token authenticates.
    @param expires_at  Unix seconds after which the token is refused.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO refresh_tokens (token_hash, user_id, expires_at, revoked) "
        "VALUES (?, ?, ?, 0)",
        (token_hash, user_id, expires_at),
    )
    db.commit()


def get_refresh_token(token_hash: str) -> dict | None:
    """
    Looks up a stored refresh token by hash.

    Returns revoked and expired rows too: the caller has to tell "unknown" from "revoked" apart,
    because presenting an already-rotated token means the token was stolen and every session for
    that user should be dropped.

    @param token_hash  Hex SHA-256 of the presented token.
    @return            A dict with user_id, expires_at and revoked, or None if unknown.
    """
    row = (
        get_db()
        .execute("SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,))
        .fetchone()
    )
    return dict(row) if row else None


def revoke_refresh_token(token_hash: str):
    """
    Marks one refresh token revoked, leaving the row in place so its later reuse is detectable.

    @param token_hash  Hex SHA-256 of the token to revoke.
    """
    db = get_db()
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
    db.commit()


def revoke_all_refresh_tokens(user_id: int):
    """
    Revokes every refresh token for an account -- logout-everywhere, and the response to a
    detected token reuse.

    @param user_id  The application user ID.
    """
    db = get_db()
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
    db.commit()


def purge_expired_refresh_tokens():
    """Deletes refresh tokens that expired more than 30 days ago, so the table cannot grow forever."""
    db = get_db()
    db.execute(
        "DELETE FROM refresh_tokens WHERE expires_at < ?", (int(time.time()) - 30 * 86_400,)
    )
    db.commit()


def save_siwe_nonce(nonce: str, ttl_secs: int):
    """
    Records a SIWE nonce as issued, and forgets the ones nobody redeemed in time.

    The nonce endpoint is open to anyone, so without the clean-up every unredeemed request would
    leave a row behind for good.

    @param nonce     The random nonce embedded in the message the user will sign.
    @param ttl_secs  How long a nonce stays redeemable.
    """
    db = get_db()
    now = int(time.time())
    db.execute("DELETE FROM siwe_nonces WHERE issued_at < ?", (now - ttl_secs,))
    db.execute("INSERT OR REPLACE INTO siwe_nonces (nonce, issued_at) VALUES (?, ?)", (nonce, now))
    db.commit()


def consume_siwe_nonce(nonce: str, ttl_secs: int) -> bool:
    """
    Redeems a SIWE nonce, burning it so a captured signature cannot be replayed.

    @param nonce     The nonce parsed out of the signed message.
    @param ttl_secs  How long after issue a nonce stays valid.
    @return          True if the nonce was outstanding and still fresh.
    """
    db = get_db()
    row = db.execute("SELECT issued_at FROM siwe_nonces WHERE nonce = ?", (nonce,)).fetchone()
    db.execute("DELETE FROM siwe_nonces WHERE nonce = ?", (nonce,))
    db.commit()
    return row is not None and row["issued_at"] >= int(time.time()) - ttl_secs


def save_contact_nonce(nonce: str, user_id: int, name: str, address: str, ttl_secs: int):
    """
    Records the nonce of one contact's typed data, for that account, name and address only, and
    forgets the ones nobody redeemed in time.

    @param ttl_secs  How long a nonce stays redeemable.
    """
    db = get_db()
    now = int(time.time())
    db.execute("DELETE FROM contact_nonces WHERE issued_at < ?", (now - ttl_secs,))
    db.execute(
        "INSERT INTO contact_nonces (nonce, user_id, name, address, issued_at) VALUES (?, ?, ?, ?, ?)",
        (nonce, user_id, name, address, now),
    )
    db.commit()


def consume_contact_nonce(nonce: str, user_id: int, name: str, address: str, ttl_secs: int) -> bool:
    """
    Redeems a contact nonce, burning it whatever the outcome.

    @return  True if it was outstanding, still fresh, and issued for exactly this account, name and
             address.
    """
    db = get_db()
    row = db.execute(
        "SELECT user_id, name, address, issued_at FROM contact_nonces WHERE nonce = ?", (nonce,)
    ).fetchone()
    db.execute("DELETE FROM contact_nonces WHERE nonce = ?", (nonce,))
    db.commit()
    return (
        row is not None
        and row["user_id"] == user_id
        and row["name"] == name
        and row["address"] == address
        and row["issued_at"] >= int(time.time()) - ttl_secs
    )
