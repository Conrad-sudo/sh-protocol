"""
Personal access tokens: how the user's own scripts and agents connect to /mcp, made in Controls.

Claude, Claude Code and ChatGPT sign in through OAuth (routes/oauth.py), which only apps on the known
list may use. Anything else -- a trading bot, a script, an app running a local model -- uses a token
the user makes here and pastes into it, sent as `Authorization: Bearer mfp_...`.

Making one takes the user's passkey, and only a passkey one of the account's wallets approves right
now: a session alone (a stolen access token, a borrowed laptop) can't make a token that outlives it.
The flow:

  1. POST /api/tokens/options: a challenge, and the account's passkeys to answer it with. Fetched
     before the user taps Create, since Safari opens the passkey prompt only inside the tap.
  2. POST /api/tokens: the name, the days (7, 30 or 90), the access (read only, or read and
     prepare) and the passkey's answer. The token is in the answer, and only there: just its
     SHA-256 is stored.

Listed and revoked with the connected apps (GET /api/connections, DELETE /api/connections/{id}).
The rules are mcp_auth.py's.
"""
import secrets
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from webauthn.helpers import bytes_to_base64url

import mcp_auth
import passkey
from auth import get_current_user
from db import PERSONAL_APP, consume_passkey_challenge, get_mcp_connections, get_passkeys, save_passkey_challenge
from routes.deps import limiter
from routes.passkeys import approved_on_a_wallet, verify_assertion

router = APIRouter()

# How long the challenge stays good: the browser's own timeout, plus time to fill in the form.
CHALLENGE_TTL_SECS = 10 * 60

NAME_MAX = 40

# The answer holds the token itself: nothing on the way may keep a copy.
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

AT_LIMIT = f"You have {mcp_auth.MAX_PERSONAL_TOKENS} tokens already. Revoke one before making another."


class CreateTokenRequest(BaseModel):
    """Body of POST /api/tokens."""

    name: str = Field(min_length=1, max_length=NAME_MAX)
    days: int
    access: Literal["read", "prepare"]
    # The browser's AuthenticationResponseJSON, as navigator.credentials.get() returned it.
    credential: dict


def _live_tokens(user_id: int) -> int:
    """How many personal tokens the account has live."""
    return sum(1 for row in get_mcp_connections(user_id) if row["app"] == PERSONAL_APP)


@router.post("/api/tokens/options")
def token_options(user_id: int = Depends(get_current_user)):
    """
    What the passkey prompt for a new token needs: a fresh challenge (single use, kept with the
    account's other passkey challenges) and the account's passkeys. Which of them a wallet approves
    is checked when the answer comes back, as for signing in.

    @return  {"challenge", "rp_id", "allow_credentials", "user_verification", "timeout_ms"}: the
             shape POST /api/sign/start answers with, so the web app opens both prompts alike.
    @raises HTTPException 409 if the account has no passkey, or already has the most tokens.
    """
    passkeys = get_passkeys(user_id)
    if not passkeys:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Making a token takes a passkey your wallet approves. Add this device in Controls → Devices first.",
        )
    if _live_tokens(user_id) >= mcp_auth.MAX_PERSONAL_TOKENS:
        raise HTTPException(status.HTTP_409_CONFLICT, AT_LIMIT)
    challenge = secrets.token_bytes(32)
    save_passkey_challenge(user_id, bytes_to_base64url(challenge), int(time.time()) + CHALLENGE_TTL_SECS)
    return {
        "challenge": bytes_to_base64url(challenge),
        "rp_id": passkey.rp_id(),
        "allow_credentials": [{"type": "public-key", "id": row["credential_id"]} for row in passkeys],
        "user_verification": "required",
        "timeout_ms": passkey.WEBAUTHN_TIMEOUT_MS,
    }


@router.post("/api/tokens")
@limiter.limit("10/minute")
def create_token(request: Request, req: CreateTokenRequest, user_id: int = Depends(get_current_user)):
    """
    Makes a personal access token, once the user's passkey has answered the challenge from
    /api/tokens/options -- a passkey of this account that one of its wallets approves right now.

    @return  {"id", "token", "name", "access", "expires_at"}. The token is shown this once.
    @raises HTTPException 422 for an empty name or days not 7, 30 or 90; 400 / 401 as
            verify_assertion; 403 if no wallet approves the passkey; 503 if the wallets couldn't be
            read; 409 if the account already has the most tokens.
    """
    name = req.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give the token a name.")
    if req.days not in mcp_auth.PERSONAL_TOKEN_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A token lasts 7, 30 or 90 days.")
    row = verify_assertion(
        req.credential,
        lambda challenge: consume_passkey_challenge(user_id, challenge),
        expired="This request has expired. Try again.",
        unknown="That passkey isn't one of this account's.",
        user_id=user_id,
    )
    approved = approved_on_a_wallet(user_id, row["key_id"])
    if approved is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Couldn't check your wallets just now. Try again.")
    if not approved:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "None of your wallets approves this passkey right now, so it can't make a token. Approve it in "
            "Controls → Devices, or use a device that's approved.",
        )
    made = mcp_auth.create_personal(user_id, name, req.days, read_only=req.access == "read")
    if made is None:
        raise HTTPException(status.HTTP_409_CONFLICT, AT_LIMIT)
    return JSONResponse(
        {
            "id": made["id"],
            "token": made["token"],
            "name": name,
            "access": mcp_auth.access_of(made["scope"]),
            "expires_at": made["expires_at"],
        },
        headers=NO_STORE,
    )
