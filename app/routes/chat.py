"""
The assistant: one turn per request, rationed, and the conversation each chain keeps.
"""
import os
import threading
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from slowapi.util import get_remote_address

import auth
import quotes
import signing
from auth import get_current_user
from chat_limits import CHAT_RATE_LIMIT, CONFIRM_MAX_CONCURRENT
from network_config import (
    CHAIN_NAME_BY_ID,
)
from routes.deps import _network_name, limiter
from smart_wallet_agent import (
    CANCELLED_REPLY,
    STALE_QUOTE,
    ConversationBusy,
    answer_yes,
    cancel_quote,
    chat_turn,
    clear_history,
    get_history,
)

router = APIRouter()


class ChatRequest(BaseModel):
    """Body of POST /api/chat."""

    chain_id: int
    message: str = Field(min_length=1, max_length=4000)


class QuoteAnswer(BaseModel):
    """Body of POST /api/chat/quote/cancel: a tap on a quote card's Cancel."""

    chain_id: int
    # quotes.put's ids are token_urlsafe(9). Only looked up: the quote must be this account's.
    quote_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")


# How many chat turns may run at once, across every account. Each holds a worker thread for 10-60
# seconds (see post_chat), out of the pool FastAPI shares with every other plain-`def` route. That
# pool is sized above this and CONFIRM_MAX_CONCURRENT together (api.API_THREADS), so a burst of
# chats can't leave the dashboard and the sign-in routes waiting for a thread. DeepSeek sets no rate
# limit, so the ceiling is this one process, whose CPU a turn reading the chain uses 50-200 ms of,
# and the chain RPC's request limit (see chat_limits.TELEGRAM_MAX_CONCURRENT for the measurements).
# 180 since 2026-10-20 (user's call, after 120 chats at once saw 56 refused at 64): past the CPU's
# 35-60 turns, turns slow down rather than being refused.
CHAT_MAX_CONCURRENT = int(os.getenv("CHAT_MAX_CONCURRENT", "180"))


# CHAT_RATE_LIMIT, the per-account limit below, lives in chat_limits: the Telegram bot rations by
# the same one.


_chat_slots = threading.BoundedSemaphore(CHAT_MAX_CONCURRENT)


# The quote card's taps have slots of their own (CONFIRM_MAX_CONCURRENT), so a Confirm never waits
# for other people's model calls to finish while its quote runs out.
_confirm_slots = threading.BoundedSemaphore(CONFIRM_MAX_CONCURRENT)


_chatting: set[int] = set()


_chatting_lock = threading.Lock()


def _chat_rate_key(request: Request) -> str:
    """
    The chat limit's key: the account the bearer token names. get_current_user has already
    accepted the token by the time the limit is checked; the address is only a fallback.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            return f"user:{auth.decode_access_token(token)}"
        except HTTPException:
            pass
    return get_remote_address(request)


@contextmanager
def _chat_turn(user_id: int, tap: bool = False):
    """
    Holds this account's one running turn and one slot -- a message's (CHAT_MAX_CONCURRENT), or a
    card's approval or cancel's (CONFIRM_MAX_CONCURRENT) when `tap` -- or refuses.

    One turn per account, across chains and tabs: a second message sent before the first is
    answered is refused, not queued, since queueing would hold a thread to do nothing. Both
    refusals are 4xx, so the web app knows the message never reached the assistant and offers to
    send it again.

    @raises HTTPException 409 while this account's previous turn runs; 429 while every slot is taken.
    """
    with _chatting_lock:
        if user_id in _chatting:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The assistant is still answering your last message. Wait for its reply, then send this one.",
            )
        _chatting.add(user_id)
    slots = _confirm_slots if tap else _chat_slots
    try:
        if not slots.acquire(blocking=False):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "The assistant is busy right now. Try again in a minute."
            )
        try:
            yield
        finally:
            slots.release()
    finally:
        with _chatting_lock:
            _chatting.discard(user_id)


@router.post("/api/chat")
@limiter.limit(CHAT_RATE_LIMIT, key_func=_chat_rate_key)
def post_chat(request: Request, req: ChatRequest, user_id: int = Depends(get_current_user)):
    """
    Runs one turn of the wallet agent.

    A plain `def`, not `async def`, on purpose: chat_turn() blocks for 10-60 seconds while the agent
    calls tools and waits on chain confirmations. FastAPI runs a sync handler in its threadpool, so
    one slow turn does not stall the event loop for everybody else. Declared `async def` it would.
    That thread is why the turn is rationed: see _chat_turn and CHAT_RATE_LIMIT.

    The user id comes from the token and is handed to the agent as runtime context, so nothing the
    message says can change whose wallet is acted on. The chain is the one the page is on, and the
    whole turn acts on it -- not on the user's saved network, which is just the chain they last
    deployed on (see db.acting_network).

    A turn that raised exactly one quote -- or asked to show one again -- offers it on a card
    (signing.offer), which the web app reads from GET /api/chat/quote and approves with a passkey
    through /api/sign/start and /finish.

    A typed "yes" while a card is waiting (signing.is_yes) gets no model call at all: it is told to
    tap Confirm and approve with a passkey, and the card stays. A model could do nothing more -- no
    tool sends.

    @param req  The chain and the user's message.
    @return     {"reply": str}
    @raises HTTPException 400 for a chain this server does not serve, as chat_history does; 409, or
            429, when the turn can't run now (see _chat_turn); 429 past CHAT_RATE_LIMIT.
    """
    network = _network_name(req.chain_id)
    with _chat_turn(user_id):
        if signing.is_yes(req.message) and quotes.offered(user_id, req.chain_id) is not None:
            return {"reply": answer_yes(user_id, req.chain_id, req.message)}
        reply, turn_id = chat_turn(user_id, req.chain_id, req.message, network)
        signing.offer(user_id, req.chain_id, turn_id, signing.WEB)
        return {"reply": reply}


def _quote_card(pending: quotes.PendingTransaction) -> dict:
    """
    What the web chat's quote card shows, written from the stored quote, never by the model: what
    approving it sends (the action, written by the package that built the calldata), where, the
    fees as one figure, and until when.
    """
    return {
        "quote_id": pending.quote_id,
        "chain_id": pending.chain_id,
        "action": pending.action,
        "fees": quotes.fee_text(pending.cost),
        "expires_at": int(pending.created_at + quotes.QUOTE_TTL_SECONDS),
    }


@router.get("/api/chat/quote")
def chat_quote(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The quote waiting on a card on one chain: the newest one a turn offered (quotes.offered), so
    the card is back after a reload. Its Confirm approves it with a passkey (POST /api/sign/start,
    then /finish, naming {chain_id, quote_id}). Only quotes raised by this process -- a quote from
    the Telegram bot is approved from its own link.

    @param chain_id  The chain the page is on.
    @return          {"quote": {"quote_id", "chain_id", "action", "fees", "expires_at"} | None}.
                     expires_at is in Unix seconds.
    @raises HTTPException 400 for a chain this server does not serve.
    """
    if chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    pending = quotes.offered(user_id, chain_id)
    return {"quote": None if pending is None else _quote_card(pending)}


@router.post("/api/chat/quote/cancel")
def cancel_chat_quote(req: QuoteAnswer, user_id: int = Depends(get_current_user)):
    """
    Discards a quote the user turned down with the card's button, with its sign request, and
    records it in the conversation. A turn too (_chat_turn), so the record can't land in the middle
    of one. The same as POST /api/sign/cancel naming {chain_id, quote_id}, kept for the card.

    @return  {"outcome": CANCELLED_REPLY}.
    @raises HTTPException 400 for a chain this server does not serve; 409 (STALE_QUOTE) when there
            was no such quote of this account's on that chain, or while a turn runs.
    """
    _network_name(req.chain_id)
    with _chat_turn(user_id, tap=True):
        if cancel_quote(user_id, req.chain_id, req.quote_id) is None:
            raise HTTPException(status.HTTP_409_CONFLICT, STALE_QUOTE)
    return {"outcome": CANCELLED_REPLY}


@router.get("/api/chat/history")
def chat_history(
    chain_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    user_id: int = Depends(get_current_user),
):
    """
    Returns the recent conversation for one chain, so the web chat is not blank after a reload.

    Only what was said is returned -- the user's messages and the assistant's text. Tool traffic
    stays inside: those messages carry raw calldata (and, in conversations from before 2026-10-02,
    an old session key's ciphertext). See smart_wallet_agent.get_history.
    The thread is shared with Telegram, so messages sent there appear too.

    A plain `def` for the same reason as post_chat: the checkpointer's sync read must not run on the
    event loop.

    @param chain_id  The chain whose conversation to read.
    @param limit     How many of the most recent messages to return (1-200).
    @return          {"chain_id", "messages": [{"role": "user" | "assistant", "text"}, ...]}, oldest first.
    """
    if chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    return {"chain_id": chain_id, "messages": get_history(user_id, chain_id, limit)}


@router.delete("/api/chat/history")
def delete_chat_history(chain_id: int | None = None, user_id: int = Depends(get_current_user)):
    """
    Deletes the conversation on one chain, or on every chain when `chain_id` is left out.

    The conversation is shared with Telegram, so this also clears what the assistant remembers
    there. Quotes raised in it are discarded with it. The transaction history is kept: it records
    what happened on chain, which deleting a chat can't undo.

    A plain `def`: deleting a checkpoint thread is a sync call into the async checkpointer, which
    works only off the event loop.

    @param chain_id  The chain whose conversation to delete; every chain's when omitted.
    @return          {"status": "cleared", "chain_ids": [...]}.
    @raises HTTPException 400 for a chain this server does not serve; 409 while the assistant is
            still answering a message in one of them (nothing is deleted then).
    """
    if chain_id is not None and chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    chain_ids = [chain_id] if chain_id is not None else sorted(CHAIN_NAME_BY_ID)
    try:
        clear_history(user_id, chain_ids)
    except ConversationBusy:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The assistant is still answering a message. Try again once it has replied.",
        )
    return {"status": "cleared", "chain_ids": chain_ids}
