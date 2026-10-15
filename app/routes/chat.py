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
from auth import get_current_user
from network_config import (
    CHAIN_NAME_BY_ID,
)
from routes.deps import _network_name, limiter
from smart_wallet_agent import (
    ConversationBusy,
    chat,
    clear_history,
    get_history,
)

router = APIRouter()


class ChatRequest(BaseModel):
    """Body of POST /api/chat."""

    chain_id: int
    message: str = Field(min_length=1, max_length=4000)


# How many chat turns may run at once, across every account. Each holds a worker thread for 10-60
# seconds (see post_chat), out of the pool FastAPI shares with every other plain-`def` route --
# 40 threads, anyio's default. Kept well below that, so a burst of chats can't leave the dashboard
# and the sign-in routes waiting for a thread.
CHAT_MAX_CONCURRENT = int(os.getenv("CHAT_MAX_CONCURRENT", "16"))


# Per account. A person sends a message every few seconds at most, so this only stops a script --
# and caps what one account can spend of the model's credits in a day.
CHAT_RATE_LIMIT = "10/minute;300/day"


_chat_slots = threading.BoundedSemaphore(CHAT_MAX_CONCURRENT)


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
def _chat_turn(user_id: int):
    """
    Holds this account's one running turn and one of the CHAT_MAX_CONCURRENT slots, or refuses.

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
    try:
        if not _chat_slots.acquire(blocking=False):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "The assistant is busy right now. Try again in a minute."
            )
        try:
            yield
        finally:
            _chat_slots.release()
    finally:
        with _chatting_lock:
            _chatting.discard(user_id)


@router.post("/api/chat")
@limiter.limit(CHAT_RATE_LIMIT, key_func=_chat_rate_key)
def post_chat(request: Request, req: ChatRequest, user_id: int = Depends(get_current_user)):
    """
    Runs one turn of the wallet agent.

    A plain `def`, not `async def`, on purpose: chat() blocks for 10-60 seconds while the agent
    calls tools and waits on chain confirmations. FastAPI runs a sync handler in its threadpool, so
    one slow turn does not stall the event loop for everybody else. Declared `async def` it would.
    That thread is why the turn is rationed: see _chat_turn and CHAT_RATE_LIMIT.

    The user id comes from the token and is handed to the agent as runtime context, so nothing the
    message says can change whose wallet is acted on. The chain is the one the page is on, and the
    whole turn acts on it -- not on the user's saved network, which is just the chain they last
    deployed on (see db.acting_network).

    @param req  The chain and the user's message.
    @return     {"reply": str}
    @raises HTTPException 400 for a chain this server does not serve, as chat_history does; 409, or
            429, when the turn can't run now (see _chat_turn); 429 past CHAT_RATE_LIMIT.
    """
    network = _network_name(req.chain_id)
    with _chat_turn(user_id):
        return {"reply": chat(user_id, req.chain_id, req.message, network)}


@router.get("/api/chat/history")
def chat_history(
    chain_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    user_id: int = Depends(get_current_user),
):
    """
    Returns the recent conversation for one chain, so the web chat is not blank after a reload.

    Only what was said is returned -- the user's messages and the assistant's text. Tool traffic
    stays inside: those messages carry the session-key ciphertext. See smart_wallet_agent.get_history.
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
