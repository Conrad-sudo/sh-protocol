"""
Approving a quote with a passkey: ONE flow for the web chat's card, the dashboard, and the page a
Telegram card links to (/c/<token>).

  POST /api/sign/start   the op to approve, built now, and its hash as the WebAuthn challenge
  POST /api/sign/finish  the passkey's assertion: checked, packed, simulated, sent
  POST /api/sign/cancel  drop a card still waiting

The web chat names a quote under its JWT ({chain_id, quote_id}); the Telegram page has no session
and names it by the link's token instead, which only that card carried. Either way the account is
the request's own -- the JWT or the token must match it -- and nothing the browser sends picks the
wallet, the network or the transaction: those were frozen when the card was offered (signing.py).
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import signing
import telegram_notify
from auth import decode_access_token
from chat_limits import CHAT_RATE_LIMIT
from routes.chat import _chat_rate_key, _chat_turn
from routes.deps import limiter
from smart_wallet_agent import CANCELLED_REPLY, cancel_signed, confirm_signed

router = APIRouter()

# The bearer token is optional here: the Telegram page has none, and uses the link's token.
_bearer = HTTPBearer(auto_error=False)

# quotes.put's ids are token_urlsafe(9); a link token is token_urlsafe(24); a sign_id token_urlsafe(16).
_ID = r"^[A-Za-z0-9_-]+$"


class RequestRef(BaseModel):
    """Which request: the web chat's {chain_id, quote_id} under its JWT, or a Telegram link's token."""

    chain_id: int | None = None
    quote_id: str | None = Field(default=None, min_length=1, max_length=32, pattern=_ID)
    token: str | None = Field(default=None, min_length=16, max_length=64, pattern=_ID)


class FinishRequest(BaseModel):
    """Body of POST /api/sign/finish."""

    sign_id: str = Field(min_length=8, max_length=64, pattern=_ID)
    # The browser's AuthenticationResponseJSON, as navigator.credentials.get() returned it.
    credential: dict
    # The Telegram link's token, when there is no JWT.
    token: str | None = Field(default=None, min_length=16, max_length=64, pattern=_ID)


def _optional_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> int | None:
    """The account a bearer token names, or None without one (or with one that doesn't verify)."""
    if credentials is None:
        return None
    try:
        return decode_access_token(credentials.credentials)
    except HTTPException:
        return None


def _find(ref: RequestRef, user_id: int | None) -> dict:
    """
    The open request a start or a cancel names.

    @raises HTTPException 401 with neither a token nor a JWT; 422 with a JWT but no quote named;
            404 (signing.EXPIRED) for a request that is unknown, someone else's, expired or used.
    """
    try:
        if ref.token is not None:
            return signing.find_by_token(ref.token)
        if user_id is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
        if ref.chain_id is None or ref.quote_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Name the quote: chain_id and quote_id, or the link's token.")
        return signing.find_by_quote(user_id, ref.chain_id, ref.quote_id)
    except signing.Expired:
        raise HTTPException(status.HTTP_404_NOT_FOUND, signing.EXPIRED)


@router.post("/api/sign/start")
@limiter.limit(CHAT_RATE_LIMIT, key_func=_chat_rate_key)
def sign_start(request: Request, ref: RequestRef, user_id: int | None = Depends(_optional_user)):
    """
    Builds the op a card's passkey will approve, and what the browser asks the passkey with.

    The op is built NOW -- fresh fees, a fresh nonce key carrying the op's own deadline (the
    request's expiry plus time to send), gas sized for a passkey's signature -- and its userOpHash
    is the challenge. Repeatable while the request is open: each call builds afresh
    under a new sign_id, and the older sign_id stops working. `allow_credentials` are the account's
    passkeys that the wallet approves now.

    @return  {"sign_id", "challenge", "rp_id", "allow_credentials", "user_verification",
              "timeout_ms", "expires_at", "summary": {"action", "network", "chain_id", "cost"}}.
    @raises HTTPException 404 (signing.EXPIRED) for an unknown, expired or used request; 409
            (signing.NO_PASSKEY) when no passkey of the account's is approved on the wallet; 422
            when the op can't be priced (the base fee is past what the wallet will pay).
    """
    found = _find(ref, user_id)
    try:
        started = signing.start(found)
    except signing.Expired:
        raise HTTPException(status.HTTP_404_NOT_FOUND, signing.EXPIRED)
    except signing.NoPasskey:
        raise HTTPException(status.HTTP_409_CONFLICT, signing.NO_PASSKEY)
    except signing.WouldFail as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    return signing.start_options(started)


@router.post("/api/sign/finish")
@limiter.limit(CHAT_RATE_LIMIT, key_func=_chat_rate_key)
def sign_finish(request: Request, req: FinishRequest, user_id: int | None = Depends(_optional_user)):
    """
    Sends a card's transaction once its passkey's approval checks out.

    The approval is checked first -- py_webauthn, against the challenge THIS server built, this
    site's rp id and origins, a passkey of the request's own account, and user verification -- then
    packed into the signature the wallet reads, run once more as a whole bundle, and sent through
    the bundler's lanes. It answers once the transaction is on the wire; the wait for it to land
    runs in the background (landing.py), and GET /api/transactions/{transaction_id} follows it.

    The tap and its outcome go into the conversation, and a sent transaction starts the chat afresh
    on the next turn (smart_wallet_agent.confirm_signed). For a card offered in Telegram, the outcome
    is posted to that chat as well, and the card's buttons come off (telegram_notify).

    It runs as a turn (routes.chat._chat_turn), on a tap's slot: never beside a message of the same
    account's being answered, whose final write would otherwise undo the record of this one.

    @return  {"status": "sent", "outcome", "transaction_id"} once on the wire; {"status":
             "unknown", "outcome", "transaction_id": null} when the broadcast failed in a way that
             leaves it unknown whether it went out (the History tab will say).
    @raises HTTPException 400 when the passkey check fails (the card stays open for another try);
            409 for a stale request -- expired, used, built again since, or not this caller's --
            or while a message of the account's is being answered; 422 when it would fail on
            chain, with the reason; 401 with neither a token nor a JWT.
    """
    if req.token is None and user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    try:
        found = signing.find_by_sign_id(req.sign_id)
    except signing.Expired:
        raise HTTPException(status.HTTP_409_CONFLICT, signing.EXPIRED)
    # The link's token, or the JWT, must be the request's own. Answered as stale either way, so a
    # caller learns nothing about requests that aren't theirs.
    by_token = req.token is not None and found["token_hash"] is not None and signing.token_hash(req.token) == found["token_hash"]
    if not (by_token or user_id == found["user_id"]):
        raise HTTPException(status.HTTP_409_CONFLICT, signing.EXPIRED)

    with _chat_turn(found["user_id"], tap=True):
        try:
            approved = signing.approve(found, req.credential)
        except signing.Rejected as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        except signing.Expired:
            raise HTTPException(status.HTTP_409_CONFLICT, signing.EXPIRED)
        confirmed = confirm_signed(approved)
    telegram_notify.tell(found, confirmed.text, confirmed.landed)
    if confirmed.status == "failed":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, confirmed.reason)
    return {"status": confirmed.status, "outcome": confirmed.text, "transaction_id": confirmed.transaction_id}


@router.post("/api/sign/cancel")
@limiter.limit(CHAT_RATE_LIMIT, key_func=_chat_rate_key)
def sign_cancel(request: Request, ref: RequestRef, user_id: int | None = Depends(_optional_user)):
    """
    Drops a card that is still waiting: its request can no longer be approved, its quote is
    dropped where this process holds it, and the cancel goes into the conversation. A Telegram
    card's buttons come off and the chat is told.

    @return  {"outcome": CANCELLED_REPLY}.
    @raises HTTPException as /api/sign/start finds the request (401, 422, 404).
    """
    found = _find(ref, user_id)
    with _chat_turn(found["user_id"], tap=True):
        if not cancel_signed(found):
            raise HTTPException(status.HTTP_404_NOT_FOUND, signing.EXPIRED)
    telegram_notify.tell(found, CANCELLED_REPLY)
    return {"outcome": CANCELLED_REPLY}
