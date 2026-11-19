"""
Signing in -- with Ethereum (SIWE), or with a passkey a wallet already approved -- the refresh-token
cookie, and /api/me.
"""
import os
import secrets
import time

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field
from web3 import Web3
from webauthn import generate_authentication_options, verify_authentication_response
from webauthn.helpers import bytes_to_base64url, parse_authentication_credential_json, parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.options_to_json_dict import options_to_json_dict
from webauthn.helpers.structs import UserVerificationRequirement

import auth
import passkey
import telegram_bots
from auth import get_current_user
from db import (
    consume_passkey_signin_challenge,
    create_user,
    find_passkey_by_credential,
    get_user_by_id,
    get_user_by_owner_addr,
    get_wallet_chains,
    get_webauthn_handle,
    save_passkey_signin_challenge,
    update_passkey_sign_count,
)
from routes.deps import limiter
from routes.passkeys import approved_on_a_wallet

router = APIRouter()


# Name of the refresh-token cookie, and the path it is scoped to. Scoping it to the auth routes
# means it is not attached to every other API call, so it cannot leak through logs or a mistake in
# an unrelated handler.
#
# /api/auth, not /api/auth/refresh: logout has to RECEIVE the cookie to revoke it. With the
# narrower path the browser never sent it to /api/auth/logout, so signing out only deleted the
# browser's copy and left the token redeemable for its full 30 days.
REFRESH_COOKIE = "refresh_token"


REFRESH_COOKIE_PATH = "/api/auth"


# Where the cookie lived before the fix above. Cleared whenever the cookie is replaced or removed,
# so a browser that still holds one does not carry two.
LEGACY_REFRESH_COOKIE_PATH = "/api/auth/refresh"


# Secure cookies require HTTPS, which local development does not have. Defaults to on.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1").lower() in ("1", "true", "yes")


class SiweLoginRequest(BaseModel):
    """Body of POST /api/auth/siwe/login."""

    # Capped so an oversized body is refused before the regex and the signature recovery run.
    message: str = Field(max_length=2048)
    signature: str = Field(max_length=200)
    nonce: str = Field(max_length=64)


class PasskeyLoginRequest(BaseModel):
    """Body of POST /api/auth/passkey/login."""

    # The browser's AuthenticationResponseJSON, as navigator.credentials.get() returned it.
    credential: dict


# How long a passkey sign-in challenge stays good: the browser's own timeout, plus room to find the
# device.
SIGNIN_CHALLENGE_TTL_SECS = 5 * 60


def _set_refresh_cookie(response: Response, token: str):
    """
    Attaches the refresh token as an httpOnly cookie.

    httpOnly keeps it out of reach of JavaScript, so an XSS bug on the site cannot read the
    long-lived credential -- it would reach at most the in-memory access token, which expires in
    minutes. SameSite=Lax plus the narrow path is the CSRF defence: the cookie is only ever sent
    to the auth routes, and not on cross-site POSTs.

    @param response  The response to attach the cookie to.
    @param token     The refresh token.
    """
    response.delete_cookie(REFRESH_COOKIE, path=LEGACY_REFRESH_COOKIE_PATH)
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=auth.REFRESH_TOKEN_TTL_SECS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def _issue_session(response: Response, user_id: int) -> dict:
    """
    Issues a fresh token pair for a user: access token in the body, refresh token in the cookie.

    @param response  The response to attach the refresh cookie to.
    @param user_id   The account to sign in.
    @return          The JSON body: the access token, its lifetime, and the user id.
    """
    _set_refresh_cookie(response, auth.issue_refresh_token(user_id))
    return {
        "access_token": auth.create_access_token(user_id),
        "token_type": "bearer",
        "expires_in": auth.ACCESS_TOKEN_TTL_SECS,
        "user_id": user_id,
    }


@router.post("/api/auth/refresh")
def refresh(response: Response, refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE)):
    """
    Exchanges the refresh cookie for a new access token, rotating the refresh token.

    @return  A new access token; a new refresh cookie replaces the old one.
    """
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token")
    user_id, new_token = auth.rotate_refresh_token(refresh_token)
    _set_refresh_cookie(response, new_token)
    return {
        "access_token": auth.create_access_token(user_id),
        "token_type": "bearer",
        "expires_in": auth.ACCESS_TOKEN_TTL_SECS,
        "user_id": user_id,
    }


@router.post("/api/auth/logout")
def logout(response: Response, refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE)):
    """
    Signs out by revoking the presented refresh token and clearing the cookie.

    Always answers 200: an unknown or absent token is not an error, and treating it as one would
    reveal which tokens exist.
    """
    if refresh_token:
        auth.revoke_refresh(refresh_token)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    response.delete_cookie(REFRESH_COOKIE, path=LEGACY_REFRESH_COOKIE_PATH)
    return {"status": "signed out"}


@router.get("/api/auth/siwe/nonce")
@limiter.limit("30/minute")
def siwe_nonce(request: Request):
    """
    Issues a nonce for the SIWE message the user is about to sign.

    @return  {"nonce": str} — include it verbatim in the message.
    """
    return {"nonce": auth.issue_siwe_nonce()}


@router.get("/api/auth/siwe/account")
@limiter.limit("15/minute")
def siwe_account(request: Request, address: str = Query(max_length=64)):
    """
    Says whether an address already has an account, so the sign-in page can greet a returning user
    and call the button "Sign up" for a new one. Sign-in itself does not depend on it.

    Open to anyone: it reveals only whether an address has signed in to Mitfah before. Anyone with a
    wallet already shows that on chain through deployWallet, and the rate limit keeps a sweep slow.

    @param address  The connected wallet's address, any case.
    @return         {"registered": bool}.
    """
    try:
        owner = Web3.to_checksum_address(address)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{address}' is not an Ethereum address")
    return {"registered": get_user_by_owner_addr(owner) is not None}


@router.post("/api/auth/siwe/login")
@limiter.limit("10/minute")
def siwe_login(request: Request, req: SiweLoginRequest, response: Response):
    """
    Signs in with a Sign-In With Ethereum message, creating the account on the address's first one.

    The way every account is made, and always open. The account IS the address that signed: the same EOA owns the account's
    wallets on chain (deployWallet sets owner = msg.sender), so whoever can sign for it already
    controls everything the account stands for. verify_siwe makes the signature evidence: this
    site's domain, a fresh single-use nonce, and a signer that is the address the message names.

    Mitfah cannot recover an account whose address is lost. Neither could it before: only that
    address could ever pause, limit or withdraw from the wallet.

    @param req  The signed message, its signature, and the nonce it carries.
    @return     An access token; the refresh token is set as a cookie.
    """
    address = auth.verify_siwe(req.message, req.signature, req.nonce)
    user = get_user_by_owner_addr(address)
    if user is not None:
        return _issue_session(response, user["id"])
    try:
        return _issue_session(response, create_user(owner_addr=address))
    except ValueError:
        # The same address's first sign-in, twice at once: the other request made the account.
        return _issue_session(response, get_user_by_owner_addr(address)["id"])


@router.post("/api/auth/passkey/options")
@limiter.limit("30/minute")
def passkey_signin_options(request: Request):
    """
    The options for navigator.credentials.get() when signing in with a passkey.

    No account is named: allowCredentials is empty, so the device offers the user the Mitfah
    passkeys it holds, and the one they pick names the account. User verification is required, as
    for every approval. The challenge is single use and kept in the database.

    @return  {"options": PublicKeyCredentialRequestOptionsJSON}.
    """
    challenge = secrets.token_bytes(32)
    options = generate_authentication_options(
        rp_id=passkey.rp_id(),
        challenge=challenge,
        timeout=passkey.WEBAUTHN_TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    save_passkey_signin_challenge(bytes_to_base64url(challenge), int(time.time()) + SIGNIN_CHALLENGE_TTL_SECS)
    return {"options": options_to_json_dict(options)}


@router.post("/api/auth/passkey/login")
@limiter.limit("10/minute")
def passkey_login(request: Request, req: PasskeyLoginRequest, response: Response):
    """
    Signs in with a passkey: for returning users whose wallet already approves the passkey.

    py_webauthn checks the answer against the challenge this server issued (single use, unexpired),
    this site's rp id and origins, the passkey's stored public key and counter, and user
    verification. The passkey names the account; its user handle must be that account's.

    Then the rule that makes this as strong as it needs to be: the passkey must be approved, right
    now, on at least one of the account's wallets -- an owner transaction. Registering a passkey
    needs only a signed-in session, so without that rule anyone who held a session for a few minutes
    could plant a passkey of their own and sign in with it for good. First-time users, and anyone
    whose passkeys aren't approved (or have lapsed), sign in with their wallet.

    @param req  The browser's answer.
    @return     An access token; the refresh token is set as a cookie (as for SIWE).
    @raises HTTPException 400 if the answer can't be read or its challenge expired; 401 if the
            passkey is unknown or fails the check; 403 if no wallet approves it; 503 if the wallets
            couldn't be read.
    """
    try:
        credential = parse_authentication_credential_json(req.credential)
        challenge = parse_client_data_json(credential.response.client_data_json).challenge
    except (WebAuthnException, ValueError, TypeError, KeyError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The browser's answer couldn't be read.")
    if not consume_passkey_signin_challenge(bytes_to_base64url(challenge)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in request has expired. Try again.")
    unknown = "Mitfah doesn't know this passkey. Sign in with your wallet."
    row = find_passkey_by_credential(bytes_to_base64url(credential.raw_id))
    if row is None or get_user_by_id(row["user_id"]) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, unknown)
    user_handle = credential.response.user_handle
    if user_handle is not None and bytes(user_handle) != get_webauthn_handle(row["user_id"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, unknown)
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=passkey.rp_id(),
            expected_origin=passkey.allowed_origins(),
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
            require_user_verification=True,
        )
    except WebAuthnException:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That passkey couldn't be verified. Try again.")
    update_passkey_sign_count(row["key_id"], verified.new_sign_count)

    approved = approved_on_a_wallet(row["user_id"], row["key_id"])
    if approved is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Couldn't check your wallet just now. Try again, or sign in with your wallet.",
        )
    if not approved:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "None of your wallets approves this passkey right now, so it can't sign you in. Sign in "
            "with your wallet.",
        )
    return _issue_session(response, row["user_id"])


@router.get("/api/me")
def me(user_id: int = Depends(get_current_user)):
    """
    Returns the signed-in account, without anything secret.

    @return  The account's id, the address it signs in as (which owns its wallets), whether
             Telegram is linked, the Telegram bot's username (no @; None when MITFAH_USERNAME is
             not set), and its wallets. The bot is the same for every account and every chain, and
             public anyway: anyone can find it in Telegram.
    """
    user = get_user_by_id(user_id)
    return {
        "user_id": user["id"],
        "owner_addr": user["owner_addr"],
        "telegram_linked": user["telegram_chat_id"] is not None,
        "telegram_bot": telegram_bots.bot_username(),
        "wallet_chains": get_wallet_chains(user_id),
    }
