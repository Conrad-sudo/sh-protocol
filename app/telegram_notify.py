"""
The API process's few words to Telegram: how an approval made on the /c/<token> page went, and the
buttons taken off the quote's card.

The Telegram bot is a process of its own (`make bot`) and it raised the quote, but the approval
page posts to the API, which checks the passkey and sends the transaction. So the API tells the chat
itself, through the Bot API over HTTPS with the bot's own token (telegram_bots.bot_token): no
python-telegram-bot here and no polling, just sendMessage and editMessageReplyMarkup. Messages are
written as the bot writes its own (telegram_format.headed): the network's name, then the outcome.

Everything here is best effort and runs on a daemon thread of its own: the transaction is already
decided, and a Telegram outage must neither hold up the approval page's answer nor make a sent
transaction look failed. A failure is logged by its type only -- the method's URL carries the token.
"""
import logging
import threading
from concurrent.futures import Future

import requests

from constants import get_chain_display_name
from telegram_bots import bot_token
from telegram_format import code_escaped, headed

# How long one Bot API call may take before it is given up.
TIMEOUT_SECS = 10

log = logging.getLogger(__name__)


def _post(method: str, payload: dict) -> bool:
    """
    Calls one Bot API method. True if Telegram accepted it; False, logged, otherwise -- including
    when this server has no bot (no MITFAH_API), where there is nobody to tell.
    """
    token = bot_token()
    if token is None:
        return False
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=TIMEOUT_SECS)
        if response.ok and response.json().get("ok") is True:
            return True
        log.warning("Telegram refused %s (HTTP %s)", method, response.status_code)
    except Exception as exc:  # noqa: BLE001 -- best effort; the text may hold the token-bearing URL
        log.warning("Telegram %s failed (%s)", method, type(exc).__name__)
    return False


def remove_buttons(chat_id: int, message_id: int) -> bool:
    """Takes the buttons off a message the bot sent: editMessageReplyMarkup with no markup."""
    return _post("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id})


def send(chat_id: int, chain_id: int, text: str) -> bool:
    """Posts `text` to the chat as the bot's own replies look: the network's name, then the text."""
    sent = True
    for piece in headed(get_chain_display_name(chain_id), [code_escaped(text)]):
        sent = _post("sendMessage", {
            "chat_id": chat_id, "text": piece, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
        }) and sent
    return sent


def tell(request: dict, text: str, landed: Future | None = None) -> threading.Thread | None:
    """
    For a request offered in Telegram: takes the buttons off its card, posts `text`, and -- given
    `landed` -- posts what to say once the transaction lands, in that order, on a thread of its own.

    @param request  The sign request (db.sign_requests row). Anything not offered in Telegram, or
                    with no chat recorded, is left alone.
    @param landed   A future of the text for when it lands (landing.watch); None for no second message.
    @return         The thread doing it, or None when there is nothing to do.
    """
    chat_id = request.get("telegram_chat_id")
    if request.get("origin") != "telegram" or chat_id is None:
        return None
    message_id, chain_id = request.get("telegram_message_id"), request["chain_id"]

    def run():
        if message_id is not None:
            remove_buttons(chat_id, message_id)
        send(chat_id, chain_id, text)
        if landed is not None:
            try:
                final = landed.result()
            except Exception:  # noqa: BLE001 -- the landing logs its own failure
                return
            send(chat_id, chain_id, final)

    thread = threading.Thread(target=run, name="telegram-notify", daemon=True)
    thread.start()
    return thread
