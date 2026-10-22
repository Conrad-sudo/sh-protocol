"""
The account's passkeys, its WebAuthn user handle, and the challenges a registration is checked
against. Which wallet a passkey is approved on is NOT here: that lives on chain (getSessions).
"""
import secrets
import sqlite3
import time

from db import get_db


def get_or_create_webauthn_handle(user_id: int) -> bytes:
    """
    The account's WebAuthn user handle: 16 random bytes, made the first time it is asked for.

    Random rather than the account id, as WebAuthn asks: an authenticator may show it or store it,
    and it must say nothing about who the account is. One per account, so every passkey the account
    makes names the same user.

    @param user_id  The application user ID.
    @return         The handle.
    """
    db = get_db()
    row = db.execute("SELECT webauthn_handle FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is not None and row["webauthn_handle"]:
        return bytes(row["webauthn_handle"])
    handle = secrets.token_bytes(16)
    # Only where none is set yet: two first requests at once must not leave two handles about.
    db.execute("UPDATE users SET webauthn_handle = ? WHERE id = ? AND webauthn_handle IS NULL", (handle, user_id))
    db.commit()
    return bytes(db.execute("SELECT webauthn_handle FROM users WHERE id = ?", (user_id,)).fetchone()["webauthn_handle"])


def save_passkey_challenge(user_id: int, challenge: str, expires_at: int):
    """
    Remembers a registration challenge until it is used or expires, and drops the expired ones.

    @param challenge  The challenge, base64url, as the browser sends it back in clientDataJSON.
    """
    db = get_db()
    db.execute("DELETE FROM passkey_challenges WHERE expires_at < ?", (int(time.time()),))
    db.execute(
        "INSERT INTO passkey_challenges (challenge, user_id, expires_at) VALUES (?, ?, ?)",
        (challenge, user_id, expires_at),
    )
    db.commit()


def consume_passkey_challenge(user_id: int, challenge: str) -> bool:
    """
    Uses up a registration challenge: true only once, for the account it was issued to, before it
    expires. A response to a challenge nobody issued, or issued to another account, gets false.
    """
    db = get_db()
    cur = db.execute(
        "DELETE FROM passkey_challenges WHERE challenge = ? AND user_id = ? AND expires_at >= ?",
        (challenge, user_id, int(time.time())),
    )
    db.commit()
    return cur.rowcount == 1


def save_passkey(
    user_id: int,
    key_id: str,
    credential_id: str,
    public_key: bytes,
    qx: str,
    qy: str,
    device_name: str,
    sign_count: int = 0,
) -> dict:
    """
    Files a verified passkey under the account.

    @param key_id         0x hex: keccak256(abi.encode(qx, qy)), the wallet's name for the key.
    @param credential_id  base64url: the browser's name for the credential.
    @param public_key     The COSE key the passkey's approvals are verified with.
    @return               The row, as get_passkey returns it.
    @raises ValueError    If the credential or the key is already registered, by anyone.
    """
    db = get_db()
    try:
        db.execute(
            "INSERT INTO passkeys (key_id, user_id, credential_id, public_key, qx, qy, sign_count, device_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key_id, user_id, credential_id, public_key, qx, qy, sign_count, device_name, int(time.time())),
        )
    except sqlite3.IntegrityError as exc:
        # The failed INSERT leaves this thread's write transaction open, holding the database's one
        # write lock: every other thread's write would wait and fail "database is locked".
        db.rollback()
        raise ValueError("That passkey is already registered.") from exc
    db.commit()
    return get_passkey(user_id, key_id)


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def get_passkeys(user_id: int) -> list[dict]:
    """The account's passkeys, oldest first."""
    rows = get_db().execute(
        "SELECT * FROM passkeys WHERE user_id = ? ORDER BY created_at, rowid", (user_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_passkey(user_id: int, key_id: str) -> dict | None:
    """One of the account's passkeys by its key id, or None (unknown, or another account's)."""
    return _row(get_db().execute(
        "SELECT * FROM passkeys WHERE user_id = ? AND key_id = ?", (user_id, key_id.lower())
    ).fetchone())


def get_passkey_by_credential(user_id: int, credential_id: str) -> dict | None:
    """One of the account's passkeys by the browser's credential id, or None."""
    return _row(get_db().execute(
        "SELECT * FROM passkeys WHERE user_id = ? AND credential_id = ?", (user_id, credential_id)
    ).fetchone())


def rename_passkey(user_id: int, key_id: str, device_name: str) -> dict | None:
    """Renames one of the account's passkeys. Returns it, or None if the account has no such key."""
    db = get_db()
    cur = db.execute(
        "UPDATE passkeys SET device_name = ? WHERE user_id = ? AND key_id = ?",
        (device_name, user_id, key_id.lower()),
    )
    db.commit()
    return get_passkey(user_id, key_id) if cur.rowcount else None


def delete_passkey(user_id: int, key_id: str) -> bool:
    """Forgets one of the account's passkeys. True if there was one to forget."""
    db = get_db()
    cur = db.execute("DELETE FROM passkeys WHERE user_id = ? AND key_id = ?", (user_id, key_id.lower()))
    db.commit()
    return cur.rowcount == 1


def update_passkey_sign_count(key_id: str, sign_count: int):
    """
    Records the counter a passkey's latest approval reported. Never moves it backwards: two
    approvals finished out of order must not let an old counter be accepted again.
    """
    db = get_db()
    db.execute(
        "UPDATE passkeys SET sign_count = MAX(sign_count, ?) WHERE key_id = ?", (sign_count, key_id.lower())
    )
    db.commit()
