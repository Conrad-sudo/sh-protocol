"""
Account authentication for the web API: Sign-In With Ethereum (the only way in), JWT access
tokens, rotating refresh tokens, and the EIP-712 signature that adds a contact.

Everything here exists to answer one question -- which `user_id` is this request for? -- from
evidence the caller cannot forge. That id then flows to the agent as runtime context and to the
database as the account key, so getting it wrong here is the whole ballgame: a request that
resolves to the wrong user reaches that user's wallet, bounded only by its spending cap.

An account IS an address: whoever signs a SIWE message for it signs in to its account, and the
same address owns the account's wallets on chain. There is no email, password or Google sign-in
(removed 2026-10-01), so there is nothing to recover an account with -- losing the wallet loses the
account, just as it already lost control of the wallet itself.

Nothing in this module signs blockchain transactions. The user's own EOA owns their SessionHandler
(deployWallet sets owner = msg.sender), so pause, withdraw and ownership changes are signed in the
user's browser, not here.
"""

import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timezone

import jwt
from eth_account.messages import encode_defunct, encode_typed_data
from eth_account import Account
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db import (
    consume_contact_nonce,
    get_refresh_token,
    get_user_by_id,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    save_contact_nonce,
    save_refresh_token,
    save_siwe_nonce,
    consume_siwe_nonce,
)

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
# How long the nonce in a contact's typed data stays redeemable. The same reasoning.
CONTACT_NONCE_TTL_SECS = 10 * 60
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


# ── SIWE (EIP-4361) sign-in ───────────────────────────────────────────────────


# The site(s) a SIWE message must name as its `domain`. This check is what makes SIWE worth having.
# The nonce endpoint is open, so a phishing page can fetch a nonce and get a victim to sign it, then
# replay the signature here to sign in AS the victim. The victim's wallet shows the domain written
# in the message and warns when it does not match the page asking, so a phishing page that wants no
# warning has to write ITS OWN domain -- and this set refuses it. Writing ours instead gets the
# victim a deceptive-site warning before they sign.
#
# Comma-separated, for an apex plus www. Defaults to the local dev server, so a production deploy
# that forgets to set it fails closed: every sign-in is refused rather than every domain accepted.
SIWE_DOMAINS = {
    d.strip().lower() for d in os.getenv("SIWE_DOMAIN", "localhost:3000").split(",") if d.strip()
}

# EIP-4361's layout, exactly as viem's createSiweMessage writes it: optional scheme, a one-line
# optional statement, then the fields in the order the spec fixes. Anchored at both ends so nothing
# can ride along outside it.
_SIWE_MESSAGE = re.compile(
    r"\A(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?(?P<domain>[^\s/]+)"
    r" wants you to sign in with your Ethereum account:\n"
    r"(?P<address>0x[0-9a-fA-F]{40})\n"
    r"\n"
    r"(?:[^\n]+\n)?"
    r"\n"
    r"URI: [^\n]+\n"
    r"Version: 1\n"
    r"Chain ID: [0-9]+\n"
    r"Nonce: (?P<nonce>[a-zA-Z0-9]{8,})\n"
    r"Issued At: [^\n]+"
    r"(?:\nExpiration Time: (?P<expiration_time>[^\n]+))?"
    r"(?:\nNot Before: (?P<not_before>[^\n]+))?"
    r"(?:\nRequest ID: [^\n]*)?"
    r"(?:\nResources:(?:\n- [^\n]+)+)?"
    r"\Z"
)


def issue_siwe_nonce() -> str:
    """
    Issues a nonce to embed in a SIWE message.

    Hex, because EIP-4361 requires an alphanumeric nonce and client libraries enforce it --
    token_urlsafe's `-` and `_` would be rejected before the user ever saw the message.

    @return  The nonce, to be included in the message the user signs.
    """
    nonce = secrets.token_hex(16)
    save_siwe_nonce(nonce, SIWE_NONCE_TTL_SECS)
    return nonce


def build_siwe_message(
    domain: str,
    address: str,
    nonce: str,
    chain_id: int,
    statement: str | None = None,
    expiration_time: datetime | None = None,
) -> str:
    """
    Writes a SIWE message in the layout verify_siwe accepts.

    The web app builds its message with viem's createSiweMessage; this produces the same text, for
    the tests and any client that is not a browser.

    @param domain           The site the message is for; must be in SIWE_DOMAINS to verify.
    @param address          The address that will sign it.
    @param nonce            From issue_siwe_nonce.
    @param chain_id         The chain the signing wallet is on.
    @param statement        Optional one-line text shown to the user.
    @param expiration_time  Optional timezone-aware expiry.
    @return                 The message to sign.
    """
    def iso(t: datetime) -> str:
        return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    message = (
        f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\n"
        + (f"{statement}\n" if statement else "")
        + f"\nURI: http://{domain}\nVersion: 1\nChain ID: {chain_id}\nNonce: {nonce}\n"
        + f"Issued At: {iso(datetime.now(timezone.utc))}"
    )
    if expiration_time:
        message += f"\nExpiration Time: {iso(expiration_time)}"
    return message


def _siwe_time(value: str) -> datetime:
    """Parses a SIWE timestamp, refusing one with no timezone (its meaning would be a guess)."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unreadable time in the message: {value}")
    return parsed


def verify_siwe(message: str, signature: str, nonce: str) -> str:
    """
    Checks a SIWE message and returns the address that signed it.

    In order, each check before anything that costs state:
    - the text must be an EIP-4361 message, so every field below is read from a fixed place;
    - its domain must be this site (see SIWE_DOMAINS for why this is the check that matters);
    - its Nonce field must equal the issued nonce -- exactly, not merely appear somewhere;
    - Expiration Time / Not Before, when present, must hold now;
    - the nonce is burned, so a captured (message, signature) pair cannot be replayed;
    - the signer must be the address the message names, or the message is describing someone else.

    The chain ID is parsed but not restricted: signing in is not a per-chain act, and refusing a
    user whose wallet happens to sit on another network would add friction, not safety.

    @param message    The exact message the user signed.
    @param signature  The hex signature returned by their wallet.
    @param nonce      The nonce this message was supposed to carry.
    @return           The checksummed address that produced the signature.
    @raises HTTPException 400 if any check fails.
    """
    parsed = _SIWE_MESSAGE.match(message)
    if parsed is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That is not a Sign-In With Ethereum (EIP-4361) message"
        )
    if parsed["domain"].lower() not in SIWE_DOMAINS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This message was written for {parsed['domain']}, not for this site",
        )
    if parsed["nonce"] != nonce:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The signed message does not carry this nonce")

    now = datetime.now(timezone.utc)
    if parsed["expiration_time"] and _siwe_time(parsed["expiration_time"]) <= now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in message has expired")
    if parsed["not_before"] and _siwe_time(parsed["not_before"]) > now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in message is not valid yet")

    if not consume_siwe_nonce(nonce, SIWE_NONCE_TTL_SECS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown or expired nonce")
    try:
        signer = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not verify that signature")

    if signer.lower() != parsed["address"].lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The message names a different address from the one that signed it",
        )
    return signer


# ── Signed contacts (EIP-712) ─────────────────────────────────────────────────


# The contact list is the allowlist of destinations for the wallet's funds (see the note above the
# contacts routes in api.py), so adding to it takes the owner wallet's signature over the exact name
# and address, not just a session. A stolen session alone -- a token lifted by an XSS bug, a browser
# left signed in -- can then add no payee, and the wallet shows the user the address they are
# approving.
#
# No chainId in the domain: a contact belongs to the account, not to one network, and wallets refuse
# typed data whose chainId differs from the network they are on. Replay is stopped by the nonce
# instead, which is single-use and issued for one account, name and address.
CONTACT_DOMAIN = {"name": "Mitfah", "version": "1"}
CONTACT_TYPES = {
    "AddContact": [
        {"name": "name", "type": "string"},
        {"name": "address", "type": "address"},
        {"name": "nonce", "type": "string"},
    ],
}
_DOMAIN_TYPE = [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}]


def contact_typed_data(name: str, address: str, nonce: str) -> dict:
    """
    The EIP-712 typed data the owner signs to save `name` as `address`.

    Shaped for viem's signTypedData and eth_signTypedData_v4: the domain's own type is left out of
    `types`, because both derive it from `domain`.

    @param name     The contact name, already normalised (lowercase, trimmed).
    @param address  The checksummed address.
    @param nonce    From issue_contact_nonce.
    """
    return {
        "domain": CONTACT_DOMAIN,
        "types": CONTACT_TYPES,
        "primaryType": "AddContact",
        "message": {"name": name, "address": address, "nonce": nonce},
    }


def issue_contact_nonce(user_id: int, name: str, address: str) -> str:
    """
    Issues the nonce for one contact's typed data, bound to the account and to that name and address.

    @return  The nonce, alphanumeric like a SIWE one.
    """
    nonce = secrets.token_hex(16)
    save_contact_nonce(nonce, user_id, name, address, CONTACT_NONCE_TTL_SECS)
    return nonce


def verify_contact_signature(user_id: int, owner: str, name: str, address: str, nonce: str, signature: str):
    """
    Checks that the account's owner signed contact_typed_data(name, address, nonce).

    The nonce is burned first, as in verify_siwe, so a captured signature cannot be replayed -- not
    even to put back a contact the owner has since deleted.

    @param owner  The account's owner address: the only signer accepted.
    @raises HTTPException 400 if the nonce is unknown, stale or for another contact, or the
            signature is not the owner's.
    """
    if not consume_contact_nonce(nonce, user_id, name, address, CONTACT_NONCE_TTL_SECS):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That approval has expired or was for a different contact. Please try again.",
        )
    typed = contact_typed_data(name, address, nonce)
    full = {**typed, "types": {"EIP712Domain": _DOMAIN_TYPE, **typed["types"]}}
    try:
        signer = Account.recover_message(encode_typed_data(full_message=full), signature=signature)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not verify that signature")
    if signer.lower() != owner.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"That was signed by {signer}, not by your wallet's owner {owner}.",
        )
