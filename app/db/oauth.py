"""
AI apps connected over MCP: their requests to connect, the connections the user allowed, and the
tokens each connection holds. Only SHA-256 hashes of codes and tokens are stored, so a read of the
database cannot be replayed as a login. The rules that use these rows live in mcp_auth.py.
"""
import time

from db import get_db

# How long a finished request (denied, used, or never answered) is kept after it expires: long
# enough that a code presented a second time is still recognised as used, and so revokes what it
# made, rather than being treated as one nobody issued.
FINISHED_REQUEST_KEEP_SECS = 86_400


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


# ── Requests to connect ───────────────────────────────────────────────────────


def save_oauth_request(request: dict):
    """
    Files a new request to connect (status 'open'), and drops requests that ended over a day ago.

    @param request  Every column of oauth_requests but the ones approval fills in.
    """
    db = get_db()
    db.execute("DELETE FROM oauth_requests WHERE expires_at < ?", (int(time.time()) - FINISHED_REQUEST_KEEP_SECS,))
    db.execute(
        """INSERT INTO oauth_requests
           (id, app, client_id, redirect_uri, state, code_challenge, scope, resource, binding_hash,
            created_at, expires_at)
           VALUES (:id, :app, :client_id, :redirect_uri, :state, :code_challenge, :scope, :resource,
                   :binding_hash, :created_at, :expires_at)""",
        request,
    )
    db.commit()


def get_oauth_request(request_id: str) -> dict | None:
    """A request to connect by its id, whatever its status, or None."""
    return _row(get_db().execute("SELECT * FROM oauth_requests WHERE id = ?", (request_id,)).fetchone())


def get_oauth_request_by_code(code_hash: str) -> dict | None:
    """The request an authorization code was issued for, by the code's hash, or None."""
    return _row(get_db().execute("SELECT * FROM oauth_requests WHERE code_hash = ?", (code_hash,)).fetchone())


def approve_oauth_request(request_id: str, user_id: int, code_hash: str, code_expires_at: int) -> bool:
    """
    Marks an open, unexpired request approved by `user_id`, with the code it now has. True only for
    the one call that does it: a request is approved (or denied) once.
    """
    db = get_db()
    cur = db.execute(
        """UPDATE oauth_requests SET status = 'approved', user_id = ?, code_hash = ?, code_expires_at = ?
           WHERE id = ? AND status = 'open' AND expires_at >= ?""",
        (user_id, code_hash, code_expires_at, request_id, int(time.time())),
    )
    db.commit()
    return cur.rowcount == 1


def deny_oauth_request(request_id: str) -> bool:
    """Marks an open request denied. True only for the call that does it."""
    db = get_db()
    cur = db.execute("UPDATE oauth_requests SET status = 'denied' WHERE id = ? AND status = 'open'", (request_id,))
    db.commit()
    return cur.rowcount == 1


def use_oauth_code(request_id: str, connection_id: int) -> bool:
    """
    Marks an approved request's code used, naming the connection it made. True only for the one
    exchange that gets there first, so two racing exchanges of one code make one connection.
    """
    db = get_db()
    cur = db.execute(
        "UPDATE oauth_requests SET status = 'used', connection_id = ? WHERE id = ? AND status = 'approved'",
        (connection_id, request_id),
    )
    db.commit()
    return cur.rowcount == 1


# ── Connections ───────────────────────────────────────────────────────────────


def create_mcp_connection(user_id: int, app: str, client_id: str, scope: str) -> int:
    """Files a connection the user allowed and returns its id."""
    db = get_db()
    cur = db.execute(
        "INSERT INTO mcp_connections (user_id, app, client_id, scope, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, app, client_id, scope, int(time.time())),
    )
    db.commit()
    return cur.lastrowid


def get_mcp_connection(connection_id: int) -> dict | None:
    """A connection by id, revoked or not, or None."""
    return _row(get_db().execute("SELECT * FROM mcp_connections WHERE id = ?", (connection_id,)).fetchone())


def get_mcp_connections(user_id: int) -> list[dict]:
    """The account's live connections, newest first."""
    rows = get_db().execute(
        "SELECT * FROM mcp_connections WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC, id DESC",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def revoke_mcp_connection(connection_id: int, user_id: int | None = None) -> bool:
    """
    Ends a connection and every token it holds. With `user_id`, only that account's connection.
    True if there was a live connection to end.
    """
    db = get_db()
    if user_id is None:
        cur = db.execute(
            "UPDATE mcp_connections SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (int(time.time()), connection_id),
        )
    else:
        cur = db.execute(
            "UPDATE mcp_connections SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (int(time.time()), connection_id, user_id),
        )
    ended = cur.rowcount == 1
    # Only when this call ended it: another account's id must not reach that account's tokens. An
    # already-ended connection's tokens were retired with it, in the same transaction.
    if ended:
        db.execute("UPDATE mcp_tokens SET revoked = 1 WHERE connection_id = ?", (connection_id,))
    db.commit()
    return ended


def touch_mcp_connection(connection_id: int, now: int):
    """Records that a connection was just used, at most once a minute so busy apps don't write on every call."""
    db = get_db()
    db.execute(
        "UPDATE mcp_connections SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
        (now, connection_id, now - 60),
    )
    db.commit()


# ── Tokens ────────────────────────────────────────────────────────────────────


def save_mcp_token(token_hash: str, connection_id: int, kind: str, issued_by: str, expires_at: int):
    """Files one token of a connection: kind 'access' or 'refresh'."""
    db = get_db()
    db.execute(
        """INSERT INTO mcp_tokens (token_hash, connection_id, kind, issued_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token_hash, connection_id, kind, issued_by, int(time.time()), expires_at),
    )
    db.commit()


def get_mcp_token(token_hash: str) -> dict | None:
    """
    A token by its hash, with its connection's account, app and revocation, or None.

    @return  The mcp_tokens row plus user_id, app, client_id, scope and connection_revoked_at.
    """
    return _row(get_db().execute(
        """SELECT t.*, c.user_id, c.app, c.client_id, c.scope, c.revoked_at AS connection_revoked_at
           FROM mcp_tokens t JOIN mcp_connections c ON c.id = t.connection_id
           WHERE t.token_hash = ?""",
        (token_hash,),
    ).fetchone())


def mark_mcp_token_used(token_hash: str, now: int) -> bool:
    """Marks a refresh token exchanged. True only for the one exchange that gets there first."""
    db = get_db()
    cur = db.execute(
        "UPDATE mcp_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND revoked = 0",
        (now, token_hash),
    )
    db.commit()
    return cur.rowcount == 1


def get_mcp_tokens_issued_by(issued_by: str) -> list[dict]:
    """The tokens a code or refresh token minted, live or not."""
    rows = get_db().execute("SELECT * FROM mcp_tokens WHERE issued_by = ?", (issued_by,)).fetchall()
    return [dict(row) for row in rows]


def revoke_mcp_tokens_issued_by(issued_by: str):
    """Retires every token a code or refresh token minted."""
    db = get_db()
    db.execute("UPDATE mcp_tokens SET revoked = 1 WHERE issued_by = ?", (issued_by,))
    db.commit()


def revoke_mcp_token(token_hash: str):
    """Retires one token. An unknown hash is not an error."""
    db = get_db()
    db.execute("UPDATE mcp_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
    db.commit()


def purge_expired_mcp_tokens():
    """Drops tokens that expired over a day ago; a used refresh token that old can't be retried anyway."""
    db = get_db()
    db.execute("DELETE FROM mcp_tokens WHERE expires_at < ?", (int(time.time()) - 86_400,))
    db.commit()
