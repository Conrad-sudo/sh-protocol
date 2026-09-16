"""
Account authentication for the web API: passwords, JWT access tokens, rotating refresh tokens,
Google sign-in and SIWE wallet binding.

Everything here exists to answer one question -- which `user_id` is this request for? -- from
evidence the caller cannot forge. That id then flows to the agent as runtime context and to the
database as the account key, so getting it wrong here is the whole ballgame: a request that
resolves to the wrong user reaches that user's wallet, bounded only by its spending cap.

Nothing in this module signs blockchain transactions. The user's own EOA owns their SessionHandler
(deployWallet sets owner = msg.sender), so pause, withdraw and ownership changes are signed in the
user's browser, not here.
"""

import hashlib
import os
import secrets
import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from eth_account.messages import encode_defunct
from eth_account import Account
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db import (
    get_refresh_token,
    get_user_by_id,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    save_refresh_token,
    save_siwe_nonce,
    consume_siwe_nonce,
)

# Argon2id with the library's defaults, which track the RFC 9106 recommendations. Deliberately
# argon2-cffi rather than passlib: passlib's last release was 2020 and warns against modern bcrypt,
# and hash/verify is the entire surface needed here.
_hasher = PasswordHasher()

# Signing key for access tokens. Fails closed: an unset secret in production would mean tokens
# anyone can mint, so there is no default value to fall back to.
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"

# Short, because an access token cannot be revoked before it expires -- revocation acts on the
# refresh token instead. Fifteen minutes bounds the damage from a leaked one.
ACCESS_TOKEN_TTL_SECS = 15 * 60
# Long, because this is what keeps a user signed in. It IS revocable, and rotates on every use.
REFRESH_TOKEN_TTL_SECS = 30 * 86_400
# How long a SIWE nonce stays redeemable. Long enough to read the message and click sign.
SIWE_NONCE_TTL_SECS = 10 * 60
# How long a Telegram deep link stays redeemable.
TELEGRAM_NONCE_TTL_SECS = 10 * 60

_bearer = HTTPBearer(auto_error=False)


def _require_secret() -> str:
    """Returns the JWT signing secret, refusing to run without one."""
    if not JWT_SECRET:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "JWT_SECRET is not set. The API cannot issue or verify tokens without it.",
        )
    return JWT_SECRET


# ── Passwords ─────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """
    Hashes a password with Argon2id.

    @param password  The plaintext password. Never stored or logged anywhere.
    @return          The encoded hash, safe to store.
    """
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """
    Checks a password against a stored hash.

    Tolerates a None hash (an account created through Google or SIWE that has no password yet) by
    returning False rather than raising, so the caller has one uniform "bad credentials" path and
    cannot accidentally leak which accounts have passwords.

    @param password_hash  The stored Argon2 hash, or None.
    @param password       The plaintext password to check.
    @return               True if the password matches.
    """
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """
    True when a stored hash was made with weaker parameters than the current policy.

    @param password_hash  The stored Argon2 hash.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


# ── Access tokens ─────────────────────────────────────────────────────────────


def create_access_token(user_id: int) -> str:
    """
    Mints a short-lived signed access token for a user.

    @param user_id  The application user ID to bind the token to.
    @return         The encoded JWT.
    """
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + ACCESS_TOKEN_TTL_SECS, "typ": "access"},
        _require_secret(),
        algorithm=JWT_ALGORITHM,
    )


def decode_access_token(token: str) -> int:
    """
    Verifies an access token and returns the user it belongs to.

    @param token  The bearer token from the Authorization header.
    @return       The application user ID.
    @raises HTTPException 401 if the token is malformed, expired, wrongly typed or badly signed.
    """
    try:
        claims = jwt.decode(token, _require_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    # A refresh token is also a bearer string; typ stops one being presented as an access token.
    if claims.get("typ") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> int:
    """
    FastAPI dependency resolving the caller to a user ID.

    THE identity boundary for the API. Every handler that touches a wallet takes its user_id from
    here and never from the request body -- a body-supplied id would let any signed-in user name
    somebody else's account.

    @param credentials  The parsed Authorization header, injected by FastAPI.
    @return             The authenticated application user ID.
    @raises HTTPException 401 if there is no valid token, or the account no longer exists.
    """
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = decode_access_token(credentials.credentials)
    # A token outlives a deleted account; check the account is still real before trusting it.
    if get_user_by_id(user_id) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account no longer exists")
    return user_id


# ── Refresh tokens ────────────────────────────────────────────────────────────


def _hash_token(token: str) -> str:
    """SHA-256 of an opaque token. Only the digest is stored, so a DB read cannot be replayed."""
    return hashlib.sha256(token.encode()).hexdigest()


def issue_refresh_token(user_id: int) -> str:
    """
    Mints an opaque refresh token and stores only its hash.

    @param user_id  The account the token authenticates.
    @return         The token to hand to the client. This is the only time it exists in plaintext.
    """
    token = secrets.token_urlsafe(48)
    save_refresh_token(_hash_token(token), user_id, int(time.time()) + REFRESH_TOKEN_TTL_SECS)
    return token


def rotate_refresh_token(token: str) -> tuple[int, str]:
    """
    Redeems a refresh token and issues a replacement.

    Rotation on every use is what makes theft detectable: the old token is revoked but its row is
    kept, so if it is ever presented again that is proof two parties hold it. The response is to
    revoke EVERY token for the account -- the legitimate user gets logged out and has to sign in
    again, which is the correct outcome when their session is known to be compromised.

    @param token  The refresh token presented by the client.
    @return       (user_id, a new refresh token).
    @raises HTTPException 401 if the token is unknown, expired, or already used.
    """
    row = get_refresh_token(_hash_token(token))
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    if row["revoked"]:
        revoke_all_refresh_tokens(row["user_id"])
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "This refresh token has already been used. All sessions have been signed out.",
        )
    if row["expires_at"] < int(time.time()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token has expired")

    revoke_refresh_token(_hash_token(token))
    return row["user_id"], issue_refresh_token(row["user_id"])


def revoke_refresh(token: str):
    """
    Revokes a single refresh token; used by logout. Unknown tokens are ignored, so logging out
    twice is not an error and the endpoint reveals nothing about which tokens exist.

    @param token  The refresh token to retire.
    """
    revoke_refresh_token(_hash_token(token))


# ── SIWE (EIP-4361) wallet binding ────────────────────────────────────────────


def issue_siwe_nonce() -> str:
    """
    Issues a nonce to embed in a SIWE message.

    @return  The nonce, to be included in the message the user signs.
    """
    nonce = secrets.token_urlsafe(16)
    save_siwe_nonce(nonce)
    return nonce


def verify_siwe(message: str, signature: str, nonce: str) -> str:
    """
    Recovers and returns the address that signed a SIWE message.

    The nonce is burned on the way through, so a captured (message, signature) pair cannot be
    replayed to bind the same address again. The nonce is also required to appear IN the message:
    without that check a signature over any other text could be presented alongside a fresh nonce.

    @param message    The exact message the user signed.
    @param signature  The hex signature returned by their wallet.
    @param nonce      The nonce this message was supposed to carry.
    @return           The checksummed address that produced the signature.
    @raises HTTPException 400 if the nonce is stale/unknown/absent, or the signature is unreadable.
    """
    if nonce not in message:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The signed message does not carry this nonce")
    if not consume_siwe_nonce(nonce, SIWE_NONCE_TTL_SECS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown or expired nonce")
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not verify that signature")


# ── Google sign-in ────────────────────────────────────────────────────────────


def verify_google_id_token(id_token_str: str) -> tuple[str, str | None]:
    """
    Verifies a Google ID token server-side and returns its subject and email.

    Verification happens here, never in the browser: a client-side check proves nothing to this
    server. The returned `sub` is what accounts are keyed on, because a Google account's email can
    change while `sub` cannot, and an unverified email must never select an account.

    @param id_token_str  The ID token from Google Identity Services.
    @return              (google_sub, email or None if Google did not assert a verified one).
    @raises HTTPException 400/500 if the token is invalid or the client ID is unset.
    """
    # Imported lazily so the module loads (and the rest of auth works) without google-auth present.
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "GOOGLE_CLIENT_ID is not configured"
        )
    try:
        claims = google_id_token.verify_oauth2_token(
            id_token_str, google_requests.Request(), client_id
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid Google token: {e}")

    email = claims.get("email") if claims.get("email_verified") else None
    return claims["sub"], email
