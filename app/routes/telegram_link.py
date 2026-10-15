"""
Linking a Telegram chat to an account, by a single-use deep link.
"""
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, status

import auth
import telegram_bots
from auth import get_current_user
from db import (
    save_telegram_link_nonce,
    unlink_telegram,
)

router = APIRouter()


@router.post("/api/integrations/telegram/link")
def telegram_link(chain_id: int | None = None, user_id: int = Depends(get_current_user)):
    """
    Mints a single-use deep link that binds the user's Telegram chat to this account.

    The user follows the link, Telegram sends the bot `/start <nonce>`, and the bot binds the chat
    id **from the update it receives**. That indirection is the point: a chat id is self-asserted
    and enumerable, so a form that accepted one would let anyone attach their Telegram to another
    person's wallet and spend against its cap.

    Every chain has a bot of its own, and one link covers them all: the chat id is the same in each
    (see telegram_bots). The link opens the bot of the network the page is on, so the first bot the
    user meets is the one for the wallet they were looking at.

    @param chain_id  The network the page is on. Without a bot there (or without a chain_id), the
                     link opens the first bot that has a username.
    @return  {"url", "nonce", "expires_in"} — send the user to `url`.
    """
    bot = telegram_bots.link_bot(chain_id)
    if not bot:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Telegram is not configured: no MITFAH_<CHAIN>_USERNAME is set",
        )
    nonce = secrets.token_urlsafe(24)
    save_telegram_link_nonce(nonce, user_id, int(time.time()) + auth.TELEGRAM_NONCE_TTL_SECS)
    return {
        "url": f"https://t.me/{bot}?start={nonce}",
        "nonce": nonce,
        "expires_in": auth.TELEGRAM_NONCE_TTL_SECS,
    }


@router.delete("/api/integrations/telegram/link")
def telegram_unlink(user_id: int = Depends(get_current_user)):
    """Detaches the Telegram chat from the signed-in account."""
    unlink_telegram(user_id)
    return {"status": "unlinked"}
