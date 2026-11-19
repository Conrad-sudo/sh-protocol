"""
Signing AI apps in to a Mitfah account: the rules behind /api/oauth/* (routes/oauth.py) and the
check on every /mcp call (mcp_server.py).

OAuth 2.1 for public clients, as MCP's authorization spec asks: the authorization code flow with
PKCE (S256 only), no client secrets, the `iss` parameter on every answer (RFC 9207), and tokens bound
to this server's /mcp (RFC 8707). Only apps on the known list (mcp_clients.py) get a code, and only
at the address that app owns.

What a connection can do is the one permission there is, "wallet": read the account and prepare
transactions. It never sends anything: every send still needs the user's passkey on the confirm
page.

Tokens are opaque random strings, looked up on every call (only their SHA-256 is stored), so
disconnecting an app in Controls stops it at once:
  - access token: 1 hour.
  - refresh token: 30 days from its last use. Each use swaps it for a new pair (rotation). If a
    used one comes back within REFRESH_RETRY_SECS and what it was swapped for hasn't been used yet,
    that is the app retrying a reply it lost: what the first try minted is retired and a fresh
    pair issued. Any other reuse means two parties hold it, so the connection is ended.
  - authorization code: 60 seconds, once. A second exchange ends the connection the first made.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from urllib.parse import urlencode

from db import (
    approve_oauth_request,
    create_mcp_connection,
    deny_oauth_request,
    get_mcp_token,
    get_mcp_tokens_issued_by,
    get_oauth_request,
    get_oauth_request_by_code,
    get_user_by_id,
    mark_mcp_token_used,
    revoke_mcp_connection,
    revoke_mcp_token,
    revoke_mcp_tokens_issued_by,
    save_mcp_token,
    save_oauth_request,
    touch_mcp_connection,
    use_oauth_code,
)

# The one permission a connection has: read the account and prepare transactions (never send).
SCOPE = "wallet"

ACCESS_TOKEN_TTL_SECS = 60 * 60
REFRESH_TOKEN_TTL_SECS = 30 * 86_400
CODE_TTL_SECS = 60
# How long the user has, from the app opening the Allow page, to answer it.
REQUEST_TTL_SECS = 10 * 60
# A used refresh token presented again within this long counts as the app retrying a lost reply.
REFRESH_RETRY_SECS = 60

# Prefixes make a leaked token recognisable for what it is (and to secret scanners).
ACCESS_PREFIX = "mfa_"
REFRESH_PREFIX = "mfr_"
CODE_PREFIX = "mfc_"

# RFC 7636: a verifier is 43-128 unreserved characters; an S256 challenge is 43 base64url ones.
_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_S256_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")

# The longest `state` an app may send; it is echoed back, so it is capped like any other input.
MAX_STATE_LEN = 1024


class OAuthError(Exception):
    """
    An OAuth error answer: `error` is the RFC 6749 code ("invalid_grant"), `description` a sentence
    for the app's logs, `status` the HTTP status the token endpoint answers with.
    """

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


def issuer() -> str:
    """
    This server's OAuth issuer, which is also the web app's address: WEB_APP_URL, or else the first
    of CORS_ORIGINS, with no trailing slash. Read on every call, so a test can set it.

    The issuer, the Allow page and /mcp share one origin: production serves them all from it
    (deploy/Caddyfile), and the dev server proxies /api, /mcp and /.well-known/oauth-* to the API.
    """
    explicit = os.getenv("WEB_APP_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    first = next((o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()), "")
    return (first or "http://localhost:3000").rstrip("/")


def resource() -> str:
    """The protected resource every token is for: this server's /mcp."""
    return issuer() + "/mcp"


def resource_metadata_url() -> str:
    """Where /mcp's protected-resource metadata is (RFC 9728), named in every 401 from /mcp."""
    return issuer() + "/.well-known/oauth-protected-resource/mcp"


def is_our_resource(value: str) -> bool:
    """Whether an app's `resource` names /mcp (with or without a trailing slash)."""
    return value.rstrip("/") == resource()


def hash_token(token: str) -> str:
    """SHA-256 of a code, token or binding cookie. Only digests are stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def s256(verifier: str) -> str:
    """The S256 code challenge of a PKCE verifier: base64url(SHA-256(verifier)), unpadded."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def is_s256_challenge(challenge: str) -> bool:
    """Whether a code challenge is shaped like an S256 one."""
    return _S256_CHALLENGE.fullmatch(challenge) is not None


def with_params(uri: str, **params: str | None) -> str:
    """`uri` with `params` added to its query (those that are None left out)."""
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return f"{uri}{'&' if '?' in uri else '?'}{query}"


# ── The Allow page ────────────────────────────────────────────────────────────


def open_request(app: str, client_id: str, redirect_uri: str, state: str | None, code_challenge: str,
                 binding: str) -> str:
    """
    Files an app's request to connect, for the Allow page to show, and returns its id.

    @param app           The known-list key the client matched.
    @param binding       The binding cookie of the browser that opened the request: only it may answer.
    """
    request_id = secrets.token_urlsafe(24)
    now = int(time.time())
    save_oauth_request({
        "id": request_id,
        "app": app,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "scope": SCOPE,
        "resource": resource(),
        "binding_hash": hash_token(binding),
        "created_at": now,
        "expires_at": now + REQUEST_TTL_SECS,
    })
    return request_id


class RequestProblem(Exception):
    """Why a request to connect can't be shown or answered: `status` for the API, a sentence for the user."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def answerable_request(request_id: str, binding: str | None) -> dict:
    """
    A request to connect this browser may answer now.

    @raises RequestProblem 404 if there is no such request; 403 if another browser opened it (or
            this one has no binding cookie); 410 if it was answered already or has expired.
    """
    request = get_oauth_request(request_id)
    if request is None:
        raise RequestProblem(404, "This request to connect doesn't exist. Start again from the app.")
    if not binding or not hmac.compare_digest(hash_token(binding), request["binding_hash"]):
        raise RequestProblem(
            403,
            "This request was opened in another browser. Start again from the app, and answer it in "
            "the browser the app opens.",
        )
    if request["status"] != "open" or request["expires_at"] < int(time.time()):
        raise RequestProblem(410, "This request to connect has expired or was already answered. Start again from the app.")
    return request


def approve(request_id: str, binding: str | None, user_id: int) -> str:
    """
    The signed-in user allows the app: issues the authorization code, for `user_id`'s account.

    @return  Where to send the browser: the app's redirect with code, state and iss.
    @raises RequestProblem  As answerable_request; 410 too if it was answered meanwhile.
    """
    request = answerable_request(request_id, binding)
    code = CODE_PREFIX + secrets.token_urlsafe(32)
    if not approve_oauth_request(request_id, user_id, hash_token(code), int(time.time()) + CODE_TTL_SECS):
        raise RequestProblem(410, "This request to connect has expired or was already answered. Start again from the app.")
    return with_params(request["redirect_uri"], code=code, state=request["state"], iss=issuer())


def deny(request_id: str, binding: str | None) -> str:
    """
    The user refuses the app.

    @return  Where to send the browser: the app's redirect with error=access_denied, state and iss.
    """
    request = answerable_request(request_id, binding)
    deny_oauth_request(request_id)
    return with_params(
        request["redirect_uri"],
        error="access_denied",
        error_description="The user did not allow the connection.",
        state=request["state"],
        iss=issuer(),
    )


# ── The token endpoint ────────────────────────────────────────────────────────


def _mint(connection_id: int, issued_by: str) -> dict:
    """A new access + refresh pair for a connection, recorded as minted by `issued_by` (a hash)."""
    now = int(time.time())
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    save_mcp_token(hash_token(access), connection_id, "access", issued_by, now + ACCESS_TOKEN_TTL_SECS)
    save_mcp_token(hash_token(refresh), connection_id, "refresh", issued_by, now + REFRESH_TOKEN_TTL_SECS)
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECS,
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def exchange_code(code: str, client_id: str | None, redirect_uri: str | None, code_verifier: str | None,
                  resource_param: str | None) -> dict:
    """
    Swaps an authorization code for the connection's first tokens.

    The code must be one this server issued, approved, unexpired and unused, presented by the client
    it was issued to, with the same redirect_uri, and with the PKCE verifier whose S256 challenge the
    request carried. Every refusal is invalid_grant, and says no more than the app needs.

    @return  The token response.
    @raises OAuthError  invalid_request, invalid_grant or invalid_target.
    """
    if not code or not client_id or not redirect_uri or not code_verifier:
        raise OAuthError("invalid_request", "code, client_id, redirect_uri and code_verifier are all required.")
    if resource_param is not None and not is_our_resource(resource_param):
        raise OAuthError("invalid_target", f"Tokens here are only for {resource()}.")
    code_hash = hash_token(code)
    request = get_oauth_request_by_code(code_hash)
    if request is None:
        raise OAuthError("invalid_grant", "Unknown authorization code.")
    if request["status"] == "used":
        # A second exchange: someone else holds a copy. Whatever the first made is ended.
        if request["connection_id"] is not None:
            revoke_mcp_connection(request["connection_id"])
        raise OAuthError("invalid_grant", "This authorization code was already used. Connect the app again.")
    if request["status"] != "approved" or request["code_expires_at"] < int(time.time()):
        raise OAuthError("invalid_grant", "This authorization code has expired. Connect the app again.")
    if client_id != request["client_id"] or redirect_uri != request["redirect_uri"]:
        raise OAuthError("invalid_grant", "This authorization code was issued to another client or redirect.")
    if not _VERIFIER.fullmatch(code_verifier) or s256(code_verifier) != request["code_challenge"]:
        raise OAuthError("invalid_grant", "The PKCE code_verifier doesn't match.")

    connection_id = create_mcp_connection(request["user_id"], request["app"], request["client_id"], request["scope"])
    if not use_oauth_code(request["id"], connection_id):
        # Another exchange of the same code got there first: both are ended, as for any reuse.
        revoke_mcp_connection(connection_id)
        winner = get_oauth_request(request["id"])
        if winner and winner["connection_id"] is not None:
            revoke_mcp_connection(winner["connection_id"])
        raise OAuthError("invalid_grant", "This authorization code was already used. Connect the app again.")
    return _mint(connection_id, code_hash)


def refresh(refresh_token: str | None, client_id: str | None, resource_param: str | None) -> dict:
    """
    Swaps a refresh token for a new pair (see the module's notes for retries and reuse).

    @return  The token response.
    @raises OAuthError  invalid_request, invalid_grant or invalid_target.
    """
    if not refresh_token:
        raise OAuthError("invalid_request", "refresh_token is required.")
    if resource_param is not None and not is_our_resource(resource_param):
        raise OAuthError("invalid_target", f"Tokens here are only for {resource()}.")
    token_hash = hash_token(refresh_token)
    row = get_mcp_token(token_hash)
    if row is None or row["kind"] != "refresh":
        raise OAuthError("invalid_grant", "Unknown refresh token.")
    if row["connection_revoked_at"] is not None:
        raise OAuthError("invalid_grant", "This connection was ended. Connect the app again.")
    if client_id is not None and client_id != row["client_id"]:
        raise OAuthError("invalid_grant", "This refresh token was issued to another client.")
    now = int(time.time())
    if row["revoked"]:
        # Retired by a retry, so its reply went missing -- yet here it is: two parties hold it.
        revoke_mcp_connection(row["connection_id"])
        raise OAuthError("invalid_grant", "This refresh token was retired, so the connection was ended. Connect the app again.")
    if row["expires_at"] < now:
        raise OAuthError("invalid_grant", "This refresh token has expired. Connect the app again.")

    if row["used_at"] is None:
        if mark_mcp_token_used(token_hash, now):
            return _mint(row["connection_id"], token_hash)
        row = get_mcp_token(token_hash)   # a racing exchange used it first: judge it as a reuse

    minted = get_mcp_tokens_issued_by(token_hash)
    successor_used = any(t["kind"] == "refresh" and t["used_at"] is not None for t in minted)
    if now - row["used_at"] <= REFRESH_RETRY_SECS and not successor_used:
        revoke_mcp_tokens_issued_by(token_hash)
        return _mint(row["connection_id"], token_hash)
    revoke_mcp_connection(row["connection_id"])
    raise OAuthError("invalid_grant", "This refresh token was already used, so the connection was ended. Connect the app again.")


def revoke(token: str | None):
    """
    RFC 7009 revocation. A refresh token ends its whole connection; an access token only itself.
    Unknown tokens are ignored, so the answer reveals nothing about which exist.
    """
    if not token:
        return
    row = get_mcp_token(hash_token(token))
    if row is None:
        return
    if row["kind"] == "refresh":
        revoke_mcp_connection(row["connection_id"])
    else:
        revoke_mcp_token(row["token_hash"])


# ── Every /mcp call ───────────────────────────────────────────────────────────


def verify_access(token: str) -> dict | None:
    """
    The connection an access token speaks for, or None if it is unknown, expired, revoked, of an
    ended connection, or of an account that no longer exists.

    @return  {"user_id", "connection_id", "app", "scope"}.
    """
    if not token.startswith(ACCESS_PREFIX):
        return None
    row = get_mcp_token(hash_token(token))
    now = int(time.time())
    if (
        row is None
        or row["kind"] != "access"
        or row["revoked"]
        or row["connection_revoked_at"] is not None
        or row["expires_at"] < now
        or get_user_by_id(row["user_id"]) is None
    ):
        return None
    touch_mcp_connection(row["connection_id"], now)
    return {"user_id": row["user_id"], "connection_id": row["connection_id"], "app": row["app"], "scope": row["scope"]}
