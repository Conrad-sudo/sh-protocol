"""
The Telegram front end: one bot for every chain, run by this process (`make bot`).

The user picks the network inside the chat, with /chain, and the choice is kept per account
(users.telegram_chain_id). A message is a turn on that network's wallet, whatever network the user
last deployed on, and nothing typed in a message can move a turn to another chain. An account with
a wallet on only one network the bot serves is put on that one without being asked. Every reply
starts with the network's name, so which wallet it speaks for is never in doubt. The token and
username come from .env -- see telegram_bots.

Turns are rationed per account by the same figures as the web app's (chat_limits): a flood of
messages is refused, not queued.

Commands, besides /start, /help and /chain:
  - Reads, answered from the wallet and the database without the assistant, so they cost no model
    call and are never rationed: /balance, /limit, /history, /positions, /contacts, /web.
  - /send and /swap: a fixed form ("/send 10 USDC to Sam") checked here first -- the amount, that the
    wallet knows the token, that the contact is saved -- then put to the assistant in fixed words,
    as a message would be. The assistant quotes; it has to, since only its tools can.
  - /cancel drops the quotes waiting on this network; /feedback stores a note for the team.
A turn that raised exactly one quote is followed by a card the code writes, not the model -- what
the quote does, its network, its fees -- with Confirm and Cancel buttons (quote_button).

Every network's turns share one agent, one checkpointer and one pool of bundler keys
(TELEGRAM_BUNDLER_1, _2, ...). Sharing them is safe because tx_sender counts nonces per (chain,
address) under a lock -- but only within a process, so never run two copies of this.
"""
import asyncio
import collections
import contextlib
import contextvars
import datetime
import functools
import html
import logging
import math
import os
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from langchain_core.tools import ToolException
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from smart_wallet_agent import (
    CANCEL_BUTTON,
    CANCELLED_REPLY,
    CONFIRM_BUTTON,
    STALE_QUOTE,
    cancel_quote,
    cancel_quotes,
    chat_turn,
    close_checkpointer,
    confirm_quote,
    init_agent,
    open_checkpointer,
)
from bundler import TELEGRAM_BUNDLER_ENV, use_bundler_key
from chat_limits import CONFIRM_MAX_CONCURRENT, TELEGRAM_MAX_CONCURRENT, TurnGate, TurnRefused
from redact import install_log_redaction
from constants import SESSION_RENEWAL_WARNING_SECS, get_chain_display_name, get_exchange, get_native_asset_ticker
from positions import read_positions
from quotes import QUOTE_TTL_SECONDS, PendingTransaction, QuoteError, fee_text, offer
# The dashboard's own balance read, so /balance lists exactly what the web app shows.
from routes.wallet import _token_balances
from tools.common import _refuse_same_token, _token_address, _token_label
from tools.wallet_reads import _get_wallet_status
from db import (
    MIGRATIONS,
    acting_network,
    add_feedback,
    consume_telegram_link_nonce,
    count_feedback_since,
    get_all_contacts,
    get_contact,
    get_db,
    get_telegram_chain,
    get_telegram_chats_on_chain,
    get_transactions,
    get_user_id_by_telegram_chat_id,
    get_wallet_address,
    get_wallet_chains,
    link_telegram,
    schema_version,
    set_telegram_chain,
)
from network_config import load_network_config_by_name, network_name
from telegram_bots import BOT_CHAINS, TOKEN_ENV, USERNAME_ENV, bot_token, legacy_vars_set
from telegram_format import plain_text, split_message, to_telegram_html

# Warn when less than this fraction of the window spending cap remains.
BUDGET_ALERT_THRESHOLD = 0.10

# When the bot checks its users' wallets, every day, in UTC. A fixed time of day rather than "every
# 24 hours from startup", so a restart neither repeats a warning nor puts the next one off.
DAILY_CHECK_TIME = datetime.time(hour=12, tzinfo=datetime.timezone.utc)

# How soon after /start the wallets are first checked, so a key about to run out is mentioned now
# rather than at the next daily check.
FIRST_CHECK_DELAY_SECS = 10

# How often "typing…" is sent again while the assistant works: Telegram clears it after 5 seconds.
TYPING_REFRESH_SECS = 4

# A link in a reply stays a link, but Telegram fetches no preview of it: a reply can quote text other
# people wrote (an agent's registration file, say), and the web app never loads what a link points
# at either.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Private chats only. In a group everyone in it could talk to the agent of whoever linked it, so a
# group gets no answer at all -- not even to /start, which is what would link it. Groups are also
# turned off in BotFather; this holds even if that setting changes. And new messages only: an
# edited message is not a new instruction, so it is not answered as one. (A button tap is not a
# message, so chain_chosen makes the same check by hand: see _private_chat_id.)
PRIVATE_MESSAGES = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

# Shown whenever a chat that no account has claimed tries to use the bot.
UNLINKED_MESSAGE = (
    "This Telegram account isn't linked to a wallet yet.\n\n"
    "Sign in on the web app and choose \"Link Telegram\" in Settings — it will send you back here "
    "with a one-time link that binds this chat to your account."
)

# Shown when the account has no wallet on a network the bot is serving: none set up yet, or only on
# networks that were down when the bot started.
NO_SERVED_WALLET_MESSAGE = (
    "You don't have a wallet on a network I'm serving right now. Set one up in the web app, then "
    "come back here."
)

# Sent in place of an empty reply, which Telegram would refuse.
EMPTY_REPLY = "Sorry, I have no answer to that. Please try again."

# When a read command's read fails: the node is down or slow, most likely.
READ_FAILED = "I couldn't read that from the network just now. Please try again in a minute."

# A Confirm or Cancel tap on a quote raised on another network than the one in use.
OTHER_NETWORK_QUOTE = "That quote is for another network. Switch back to it with /chain first."

# How many transactions /history lists. The web app's History tab has them all.
HISTORY_COMMAND_ROWS = 5

# /feedback: how long a note may be, and how many one account may send.
FEEDBACK_MIN_CHARS = 3
FEEDBACK_MAX_CHARS = 1000
FEEDBACK_PER_HOUR = 3
FEEDBACK_PER_DAY = 10

SEND_USAGE = "Write it like this: /send 10 USDC to Sam"
SWAP_USAGE = "Write it like this: /swap 1 ETH for USDC"
FEEDBACK_USAGE = "Write it after the command, like this: /feedback The swap quote was hard to read."

# An amount in /send or /swap: digits, with at most 18 decimals -- the most any token has.
_AMOUNT = re.compile(r"^\d+(\.\d{1,18})?$")

# What a quote's buttons carry: "q:ok:<chain id>:<quote id>" to confirm, "q:no:…" to cancel.
# quote_button checks every part against the tapping chat's own account and network.
_QUOTE_BUTTON = re.compile(r"^q:(ok|no):(\d{1,12}):([A-Za-z0-9_-]{1,32})$")

# One turn at a time per conversation. Different users are answered side by side (the bot is built
# with concurrent_updates), but one user's messages wait their turn: a "yes" sent while the quote it
# answers is still being worked out must find that quote. Keyed by chain as well as chat, since each
# chain is a conversation of its own. A message's chain is read when it arrives, before it waits
# here, so switching networks while it waits doesn't move it.
_turn_locks: dict[tuple[int, int], asyncio.Lock] = collections.defaultdict(asyncio.Lock)

# Whether a message may start a turn at all (chat_limits): one running and TELEGRAM_MAX_WAITING
# waiting per account, and the web app's CHAT_RATE_LIMIT. Asked before a message waits for its
# conversation, so a flood is refused at once instead of queueing behind _turn_locks. A module
# global, so tests can swap in a tighter one.
_turn_gate = TurnGate()

# The threads chat turns run on: a pool of their own, the bot's CHAT_MAX_CONCURRENT. It caps how
# many model calls run at once, and turns of 10-60 seconds each can't take every thread of asyncio's
# default pool, which the wallet reads and the daily check use.
_turn_threads = ThreadPoolExecutor(max_workers=TELEGRAM_MAX_CONCURRENT, thread_name_prefix="telegram-turn")

# The threads quote-card taps run on: apart from the turns, so a Confirm never queues behind other
# people's model calls while its quote runs out (QUOTE_TTL_SECONDS). Each is short: a Confirm returns
# once its transaction is on the wire, and the wait for it runs in landing.py.
_tap_threads = ThreadPoolExecutor(max_workers=CONFIRM_MAX_CONCURRENT, thread_name_prefix="telegram-tap")


def _resolve_user(chat_id: int) -> int | None:
    """
    Translates a Telegram chat into the account it belongs to.

    The bot's front door. A chat id is NOT an identity: it is self-asserted, enumerable, and until
    somebody completes the link flow it says nothing about who is on the other end. Every handler
    goes through here and refuses the chat if it comes back None.

    @param chat_id  The chat id from the Telegram update.
    @return         The application user ID, or None if this chat is unlinked.
    """
    return get_user_id_by_telegram_chat_id(chat_id)


def available_chains(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    """
    The networks this account can use in the bot right now: the ones it has a wallet on that the
    bot is serving (bot_data["networks"], those that answered as themselves at startup).

    @param user_id  The application user ID.
    @param context  A handler's or a job's context.
    @return         Chain IDs, in BOT_CHAINS order -- the order /chain lists them in.
    """
    networks = context.bot_data["networks"]
    wallets = set(get_wallet_chains(user_id))
    return [chain_id for chain_id in BOT_CHAINS if chain_id in networks and chain_id in wallets]


def resolve_active_chain(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple[int, str] | None:
    """
    The network this account's messages act on, when that is settled.

    It is the one the account picked with /chain, while that is still available. With none picked
    and exactly one available, it is that one, and it is stored, so a wallet set up later on another
    network doesn't make the bot start asking. Otherwise there is none, and the caller asks
    (require_active_chain). A picked network that has become unavailable is never swapped for
    another without asking: the user believes they are on it, and acting on a different wallet must
    be their choice.

    @param user_id  The application user ID.
    @param context  A handler's or a job's context.
    @return         (chain_id, this server's network name for it), or None.
    """
    available = available_chains(user_id, context)
    chain_id = get_telegram_chain(user_id)
    if chain_id is None and len(available) == 1:
        chain_id = available[0]
        set_telegram_chain(user_id, chain_id)
    if chain_id not in available:
        return None
    return chain_id, context.bot_data["networks"][chain_id]


def _chain_button(chain_id: int) -> str:
    """The data a network button carries, which chain_chosen gets back when it is tapped."""
    return f"chain:{chain_id}"


def chain_keyboard(chain_ids: list[int], current: int | None) -> InlineKeyboardMarkup:
    """
    The network buttons: one per row, in the order given, with a ✓ on the one in use.

    @param chain_ids  The networks to offer: available_chains.
    @param current    The network in use, or None.
    @return           The keyboard. Each button carries "chain:<id>" for chain_chosen.
    """
    rows = []
    for chain_id in chain_ids:
        name = get_chain_display_name(chain_id)
        label = f"✓ {name}" if chain_id == current else name
        rows.append([InlineKeyboardButton(label, callback_data=_chain_button(chain_id))])
    return InlineKeyboardMarkup(rows)


def _which_network(user_id: int) -> str:
    """
    The question that goes with the network buttons when no network is in use. When the account
    had picked one that has become unavailable, it says so first: otherwise being asked again would
    look like the bot forgot.
    """
    picked = get_telegram_chain(user_id)
    if picked is None:
        return "Which network should I use?"
    return f"{get_chain_display_name(picked)} isn't available right now. Which network should I use?"


async def require_active_chain(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> tuple[int, str] | None:
    """
    The network a message acts on; when there is none, replies saying why and returns None.

    The entry point for everything that acts on a wallet. With no wallet on a network the bot
    serves, the user is told so; with several to choose from and none picked, they get the network
    buttons. Either way the message is dropped rather than held until they choose: they asked
    without knowing where it would go, so they send it again once they do.

    @param update   The message's update, to reply to.
    @param context  The handler's context.
    @param user_id  The account, already resolved from the chat.
    @return         (chain_id, network name), or None after replying.
    """
    active = resolve_active_chain(user_id, context)
    if active is not None:
        return active
    available = available_chains(user_id, context)
    if not available:
        await update.message.reply_text(NO_SERVED_WALLET_MESSAGE)
        return None
    await update.message.reply_text(
        f"{_which_network(user_id)} Pick one, then send your message again.",
        reply_markup=chain_keyboard(available, None),
    )
    return None


def _private_chat_id(update: Update) -> int | None:
    """
    The chat a button was tapped in, or None unless it is a private chat and the tap came from the
    person on the other end of it.

    A button tap is a callback query, and a CallbackQueryHandler takes no filters, so this does by
    hand what PRIVATE_MESSAGES does for messages. A tap on a message sent inline has no chat at all.
    In a private chat the chat id is the user's own id, so a tap by anyone else -- which Telegram
    would never deliver -- is refused too, in case it ever does.
    """
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type != ChatType.PRIVATE or user.id != chat.id:
        return None
    return chat.id


async def _send_reply(send, piece: str, **extra):
    """
    Sends one message of a reply, formatted (telegram_format). Should Telegram refuse the
    formatting, the same words go out plain rather than not at all.

    @param send   What sends a message with this text: a message's reply_text, or the bot's
                  send_message bound to a chat (a button tap has no message to reply to).
    @param extra  More options for Telegram, such as the reply_markup a quote card carries.
    """
    try:
        await send(piece, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, **extra)
    except BadRequest:
        logging.getLogger(__name__).warning("Telegram refused a reply's formatting; sent it plain", exc_info=True)
        await send(plain_text(piece), link_preview_options=NO_PREVIEW, **extra)


async def _reply_on(send, chain_id: int, markdown: str):
    """
    Sends the assistant's reply, in Markdown, headed by its network's name. The name is a paragraph
    of its own, so the reply under it renders exactly as it would alone.
    """
    chain = get_chain_display_name(chain_id)
    for piece in to_telegram_html(f"**{chain}**\n\n{markdown.strip() or EMPTY_REPLY}"):
        await _send_reply(send, piece)


def _escaped(text: str) -> str:
    """
    Text the bot passes on as it is -- a send's outcome, which repeats the quote's action -- as
    Telegram HTML: escaped, so a token's symbol in it can't become a link, with `code` kept as code.
    """
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", html.escape(text, quote=False))


async def _reply_lines(send, chain_id: int, lines: list[str]):
    """
    Sends a reply the bot wrote itself -- a read command's -- as Telegram HTML, headed by its
    network's name, split between lines when long. Not through Markdown: what goes in it (a token's
    symbol, a contact's name, a transaction's description) is escaped here, line by line, so text
    that came from a token contract can never turn into a link or formatting.

    @param lines  Lines of Telegram HTML, each complete in itself, so a split between two of them
                  never cuts a tag in half.
    """
    chain = html.escape(get_chain_display_name(chain_id))
    for piece in split_message(f"<b>{chain}</b>\n\n" + "\n".join(lines)):
        await _send_reply(send, piece)


def _key_warning(status: dict, chain: str, now: int) -> str | None:
    """
    The warning about the assistant's key, if one is due: it is inactive, or it runs out within
    SESSION_RENEWAL_WARNING_SECS.

    This is why the warnings matter to a Telegram-only user: renewing the key is an owner-signed
    transaction they can only make in the web app, so being told after it lapsed means being told
    too late.
    """
    if not status.get("session_active"):
        return (
            f"⚠️ My access to your {chain} wallet is off: it was turned off, replaced or has run "
            "out. I can still answer questions, but I can't send anything until you turn it back on "
            "in the web app (Controls → Assistant)."
        )
    seconds_left = (status.get("session_expires_at") or 0) - now
    if 0 < seconds_left < SESSION_RENEWAL_WARNING_SECS:
        days_left = seconds_left // 86_400
        when = f"in {days_left} day{'s' if days_left != 1 else ''}" if days_left else "in under a day"
        return (
            f"⏳ My access to your {chain} wallet runs out {when}. Renew it in the web app "
            "(Controls → Assistant) to keep me able to send transactions for you."
        )
    return None


def wallet_warnings(status: dict, chain: str, now: int) -> list[str]:
    """
    What to warn the user about: the assistant's key (_key_warning), or less than
    BUDGET_ALERT_THRESHOLD of the cap left. With the key off, only that: nothing can be spent anyway.

    @param status  The wallet's status, as _get_wallet_status returns it.
    @param chain   The chain's name, for the text.
    @param now     Unix seconds.
    @return        The messages to send, in order; none when all is well.
    """
    key = _key_warning(status, chain, now)
    if not status.get("session_active"):
        return [key]
    warnings = [key] if key else []
    limit = status.get("daily_limit_usd") or 0
    remaining = status.get("remaining_usd") or 0
    if limit > 0 and remaining < BUDGET_ALERT_THRESHOLD * limit:
        warnings.append(
            f"⚠️ Low spending budget on {chain}: only ${remaining:,.2f} of your ${limit:,.2f} limit "
            "is left. It refills when the current period ends."
        )
    return warnings


def _price(value: float) -> str:
    """
    A price or an amount for a message, as the web app's formatPrice shows a price: five significant
    figures with thousands separators ("3,150.4", "0.00041231"), whole numbers from 100,000 up, never
    an exponent. Five figures keep a stablecoin range's two ends apart (0.995–1.005).
    """
    if value >= 100_000:
        return f"{value:,.0f}"
    if value <= 0:
        return "0"
    places = max(4 - math.floor(math.log10(value)), 0)
    text = f"{value:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def position_warnings(positions: list[dict], chain: str) -> list[str]:
    """
    One warning per Uniswap V3 position whose price has left its range. Such a position earns no
    fees until the price comes back, and nothing about it changes on its own to say so -- the
    dashboard tags it, but a Telegram-only user would never look. Sent every day while it stays out:
    the user's choice, so they hear about it until they act.

    A full-range position covers every price, so it is never out of range.

    @param positions  The rows positions.read_positions returns.
    @param chain      The chain's name, for the text.
    @return           The messages to send, in order; none when every position is earning.
    """
    warnings = []
    for position in positions:
        if position["in_range"] or position["full_range"] or position["max_price"] is None:
            continue
        base, quote = position["base"].upper(), position["quote"].upper()
        warnings.append(
            f"⚠️ Your {position['pair'].upper()} position on {chain} "
            f"({_price(position['min_price'])}–{_price(position['max_price'])} {quote} per {base}) "
            f"is out of range: {base} is at {_price(position['current_price'])} {quote}, so it isn't "
            "earning fees. Ask me to move it or take it out."
        )
    return warnings


def _read_wallet_status(user_id: int, chain_id: int, network: str) -> dict | None:
    """
    The user's wallet status on a chain, or None when it can't be read.

    Blocking (chain reads), so it runs on a worker thread. The chain is made the acting network for
    the read, as for a chat turn; otherwise it would read the chain the user last deployed on.
    """
    try:
        with acting_network(user_id, network, chain_id):
            # The plain function, not the @tool: no agent runs here, so there is no ToolRuntime.
            return _get_wallet_status(user_id)
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not read the wallet of user %s on %s", user_id, network, exc_info=True
        )
        return None


def _read_positions(user_id: int, chain_id: int, network: str) -> list[dict] | None:
    """
    The user's Uniswap V3 positions on a chain, read like _read_wallet_status: on a worker thread,
    with the chain as the acting network. None when they can't be read.
    """
    try:
        with acting_network(user_id, network, chain_id):
            return read_positions(user_id, chain_id)
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not read the positions of user %s on %s", user_id, network, exc_info=True
        )
        return None


def _read_balances(user_id: int, chain_id: int, network: str) -> list[dict] | None:
    """
    The wallet's balances on a chain, as the dashboard reads them (routes.wallet._token_balances),
    read like _read_wallet_status. None when they can't be read.
    """
    try:
        with acting_network(user_id, network, chain_id):
            w3, _ = load_network_config_by_name(network)
            return _token_balances(w3, user_id, chain_id, get_wallet_address(user_id, chain_id))
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not read the balances of user %s on %s", user_id, network, exc_info=True
        )
        return None


async def _warn(bot, chat_id: int, user_id: int, chain_id: int, network: str):
    """
    Checks the user's wallet on one chain and sends them whatever warnings apply: the wallet's own,
    then any position out of range where the chain trades on Uniswap V3. The two are read side by
    side, and either failing leaves the other's warnings to go out.
    """
    reads = [asyncio.to_thread(_read_wallet_status, user_id, chain_id, network)]
    if get_exchange(chain_id) == "uniswap-v3":
        reads.append(asyncio.to_thread(_read_positions, user_id, chain_id, network))
    status, *positions = await asyncio.gather(*reads)
    chain = get_chain_display_name(chain_id)
    texts = wallet_warnings(status, chain, int(time.time())) if status is not None else []
    texts += position_warnings((positions[0] or []) if positions else [], chain)
    for text in texts:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Forbidden:
            # The user never pressed Start in the bot, or blocked it: it may not write to them.
            return


async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    """
    The daily round, network by network: every linked chat with a wallet on a network the bot
    serves hears whatever applies to that wallet. Every network, not just the one each user has
    picked: a key running out on another network still needs renewing in time.

    The lists are read afresh each time, so a chat linked or a wallet set up since the bot started
    is included without a restart.
    """
    for chain_id, network in context.bot_data["networks"].items():
        for user_id, chat_id in get_telegram_chats_on_chain(chain_id):
            try:
                await _warn(context.bot, chat_id, user_id, chain_id, network)
            except TelegramError:
                # Telegram unreachable for this one: the rest still hear from the bot.
                logging.getLogger(__name__).warning(
                    "Could not send the daily check to chat %s on %s", chat_id, network, exc_info=True
                )


async def first_check(context: ContextTypes.DEFAULT_TYPE):
    """
    The first look at a user's wallets, just after /start: on every network they can use in the
    bot, as the daily check does. The job carries the chat and the user.
    """
    user_id = context.job.data
    for chain_id in available_chains(user_id, context):
        await _warn(context.bot, context.job.chat_id, user_id, chain_id, context.bot_data["networks"][chain_id])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /start, optionally completing a Telegram link carried in the deep-link payload.

    `/start <nonce>` arrives when the user follows the t.me link the web app minted for them. The
    account comes from the NONCE and the chat id comes from the Telegram update — neither is taken
    from anything the user typed. That is the whole point of the flow: a chat id is enumerable, so
    a form that let someone enter one would let them attach their Telegram to another person's
    wallet and spend against its cap.

    Then it greets the user on the network in use, or asks which one to use.
    """
    chat_id = update.message.chat_id
    nonce = context.args[0] if context.args else None

    user_id = _resolve_user(chat_id)
    if nonce and user_id is None:
        linked_to = consume_telegram_link_nonce(nonce)
        if linked_to is None:
            await update.message.reply_text(
                "That link has expired or has already been used. Generate a new one from the web app."
            )
            return
        try:
            link_telegram(linked_to, chat_id)
        except ValueError:
            await update.message.reply_text(
                "That account already has a different Telegram chat linked to it."
            )
            return
        user_id = linked_to
        await update.message.reply_text(
            "✅ Telegram linked to your wallet account. This one bot serves all your networks: "
            "/chain switches between them."
        )
    elif nonce and user_id is not None:
        # Already linked. Burn the nonce anyway so a leaked link cannot sit around unredeemed.
        linked_to = consume_telegram_link_nonce(nonce)
        if linked_to is not None and linked_to != user_id:
            # Otherwise the web page that minted the link would wait out its expiry in silence.
            await update.message.reply_text(
                "This Telegram chat is already linked to a different account. "
                "Unlink it in that account's settings first, then generate a new link."
            )

    if user_id is None:
        await update.message.reply_text(UNLINKED_MESSAGE)
        return

    available = available_chains(user_id, context)
    if not available:
        await update.message.reply_text(NO_SERVED_WALLET_MESSAGE)
        return

    context.job_queue.run_once(
        first_check,
        when=FIRST_CHECK_DELAY_SECS,
        chat_id=chat_id,
        # The account to report on. Kept separate from chat_id: one addresses Telegram, the other
        # addresses the wallet, and they are not the same number.
        data=user_id,
    )
    active = resolve_active_chain(user_id, context)
    if active is None:
        await update.message.reply_text(
            f"Welcome to Mitfah. {_which_network(user_id)} You can switch any time with /chain.",
            reply_markup=chain_keyboard(available, None),
        )
        return
    chain = get_chain_display_name(active[0])
    await update.message.reply_text(
        f"Welcome to Mitfah. You're on {chain}: ask me about your {chain} wallet, or ask me to pay a "
        "contact or swap tokens. Send /help for examples, or /chain to switch networks."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /help: what the bot does and its commands, with examples for the network in use."""
    user_id = _resolve_user(update.message.chat_id)
    active = resolve_active_chain(user_id, context) if user_id is not None else None
    if active is None:
        chain_id, wallet, ticker, switch = None, "your wallets", "ETH", "/chain picks it"
    else:
        chain_id, _ = active
        wallet = f"your {get_chain_display_name(chain_id)} wallet"
        ticker, switch = get_native_asset_ticker(chain_id), "/chain switches"
    # Positions are Uniswap V3's: on a network that trades elsewhere the command only says so.
    positions = "/positions – your Uniswap positions\n" if chain_id is None or get_exchange(chain_id) == "uniswap-v3" else ""
    await update.message.reply_text(
        f"I look after {wallet}. Write to me — “Send 0.01 {ticker} to Sam”, “What can I still "
        "spend?” — or use a command:\n"
        "/balance – what's in the wallet\n"
        "/limit – your spending limit and my access\n"
        "/history – the last transactions\n"
        f"{positions}"
        "/contacts – who I can pay\n"
        f"/send 0.01 {ticker} to Sam – pay a contact\n"
        f"/swap 0.01 {ticker} for USDC – swap tokens\n"
        "/cancel – drop a quote I'm holding\n"
        "/web – open the web app\n"
        "/feedback – tell the team what you think\n\n"
        "Before I send anything I show what it costs, and nothing goes out until you tap Confirm or "
        "say yes. I can only pay contacts saved in the web app, within your spending limit.\n\n"
        f"I act on one network at a time: {switch}."
    )


async def chain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /chain: the networks the account can use, as buttons, with a ✓ on the one in use."""
    user_id = _resolve_user(update.message.chat_id)
    if user_id is None:
        await update.message.reply_text(UNLINKED_MESSAGE)
        return
    available = available_chains(user_id, context)
    if not available:
        await update.message.reply_text(NO_SERVED_WALLET_MESSAGE)
        return
    active = resolve_active_chain(user_id, context)
    if active is None:
        await update.message.reply_text(_which_network(user_id), reply_markup=chain_keyboard(available, None))
        return
    chain_id, _ = active
    await update.message.reply_text(
        f"You're on {get_chain_display_name(chain_id)}. Tap a network to switch.",
        reply_markup=chain_keyboard(available, chain_id),
    )


async def chain_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles a tap on a network button: the account acts on that network from now on.

    Everything about the tap is checked here, as it happens, because a button's data comes from the
    client and an old keyboard can outlive what it offered: the tap must come from a private chat,
    from that chat's own user, whose account is linked, and name a network the account can use right
    now. The account comes from the chat, never from the button. The query is answered whatever
    happens, so the button stops spinning.
    """
    query = update.callback_query
    chat_id = _private_chat_id(update)
    if chat_id is None:
        # No answer beyond stopping the spinner, as a group gets no answer to a message either.
        await query.answer()
        return
    user_id = _resolve_user(chat_id)
    if user_id is None:
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text=UNLINKED_MESSAGE)
        return

    # Compared, never parsed: only a network the account can use right now matches.
    chain_id = next((c for c in available_chains(user_id, context) if query.data == _chain_button(c)), None)
    if chain_id is None:
        await query.answer("That network isn't available. Send /chain to see yours.", show_alert=True)
        return

    set_telegram_chain(user_id, chain_id)
    chain = get_chain_display_name(chain_id)
    await query.answer(f"You're on {chain} now.")
    with contextlib.suppress(BadRequest):
        # Refused for a message too old to edit; the answer above has said it already. Editing the
        # text also takes the buttons off, so a choice once made isn't tapped again by mistake.
        await query.edit_message_text(
            f"You're on {chain} now: what you send acts on your {chain} wallet until you switch "
            "with /chain."
        )


@contextlib.asynccontextmanager
async def _typing(bot, chat_id: int):
    """Shows "typing…" in the chat until the block ends. Cosmetic, so a failure to send it is ignored."""

    async def keep_typing():
        while True:
            with contextlib.suppress(TelegramError):
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(TYPING_REFRESH_SECS)

    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()


async def _acting_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[int, int, str] | None:
    """
    The account a message comes from and the network it acts on, or None after replying why there
    is none: the chat isn't linked, or no network is settled (require_active_chain). Both from the
    chat and the database, never from what the message says.

    @return  (user_id, chain_id, network name), or None.
    """
    user_id = _resolve_user(update.message.chat_id)
    if user_id is None:
        await update.message.reply_text(UNLINKED_MESSAGE)
        return None
    active = await require_active_chain(update, context, user_id)
    if active is None:
        return None
    return user_id, *active


async def _on_turn_thread(fn, *args):
    """
    Runs `fn(*args)` on a turn thread (_turn_threads) and returns what it returns. For what blocks
    for a whole turn -- a model's answer -- leaving the bot free meanwhile. The context is copied
    in, as app/parallel.py does, so the thread sees this task's.
    """
    return await asyncio.get_running_loop().run_in_executor(
        _turn_threads, contextvars.copy_context().run, fn, *args
    )


async def _on_tap_thread(fn, *args):
    """Runs `fn(*args)` as _on_turn_thread does, on a tap thread (_tap_threads) instead."""
    return await asyncio.get_running_loop().run_in_executor(
        _tap_threads, contextvars.copy_context().run, fn, *args
    )


async def _run_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chain_id: int, network: str,
                    text: str):
    """
    Puts `text` to the assistant as the account's next message on `chain_id`, and sends its reply.

    Rationed first (_turn_gate): a message past the account's ration gets no turn, and at most one
    reply per spell. Then one turn at a time per conversation (_turn_locks). A turn that raised
    exactly one quote is followed by the quote's card, with its Confirm and Cancel buttons.

    @param text  What the user wrote, or the fixed words a /send or /swap stands for.
    """
    chat_id = update.message.chat_id
    try:
        _turn_gate.admit(user_id)
    except TurnRefused as refused:
        if refused.notify:
            await update.message.reply_text(refused.text)
        return
    try:
        async with _turn_locks[(chain_id, chat_id)]:
            async with _typing(context.bot, chat_id):
                response, turn_id = await _on_turn_thread(chat_turn, user_id, chain_id, text, network)
            await _reply_on(update.message.reply_text, chain_id, response)
            await _send_quote_card(update.message.reply_text, user_id, chain_id, turn_id)
    finally:
        _turn_gate.release(user_id)


async def start_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes an ordinary message to the agent, for linked chats only.

    The identity handed to the turn is resolved here, from the database, and is never anything the
    message says. So is the network: the one the account picked with /chain (resolve_active_chain),
    read as the message arrives. While there is none, the message gets the network buttons and no
    turn.
    """
    acting = await _acting_account(update, context)
    if acting is None:
        return
    await _run_turn(update, context, *acting, update.message.text)


def quote_card(pending: PendingTransaction, now: float) -> tuple[str, InlineKeyboardMarkup]:
    """
    What a quote's Confirm and Cancel buttons sit under: written here from the stored quote, never
    by the model. Tapping Confirm sends exactly this quote, so this, not the assistant's reply, is
    what the user agrees to: the action (written by the package that built the calldata), the
    network and the fees. Escaped, since the action can carry a token's own symbol.

    @return  (the card as Telegram HTML, its buttons).
    """
    chain = get_chain_display_name(pending.chain_id)
    minutes = max(1, math.ceil((QUOTE_TTL_SECONDS - (now - pending.created_at)) / 60))
    text = (
        f"<b>Confirm on {html.escape(chain)}?</b>\n"
        f"{html.escape(pending.action)}\n"
        f"Fees: {html.escape(fee_text(pending.cost))} · good for {minutes} minute{'s' if minutes != 1 else ''}"
    )
    data = f"{pending.chain_id}:{pending.quote_id}"
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(CONFIRM_BUTTON, callback_data=f"q:ok:{data}"),
        InlineKeyboardButton(CANCEL_BUTTON, callback_data=f"q:no:{data}"),
    ]])
    return text, buttons


async def _send_quote_card(send, user_id: int, chain_id: int, turn_id: int | None):
    """Sends the quote card when the turn has one to offer (quotes.offer): it raised exactly one."""
    pending = offer(user_id, chain_id, turn_id)
    if pending is None:
        return
    text, buttons = quote_card(pending, time.time())
    await _send_reply(send, text, reply_markup=buttons)


async def quote_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles a tap on a quote's Confirm or Cancel button.

    The button's data is checked, never trusted: the tap must come from a private chat, from that
    chat's own user, whose account is linked, and name the network the account is on now. The
    account comes from the chat, the network from resolve_active_chain; the quote id is only looked
    up, and quotes.take refuses it unless it is this account's, on this network, unexpired and
    unused -- so a forged or replayed tap sends nothing. The query is answered whatever happens, so
    the button stops spinning.

    The buttons come off the card before anything runs, so a second tap has nothing to press. A
    confirm then runs in the conversation's turn (_turn_locks), as a "yes" would, on a tap thread
    (_tap_threads) rather than queueing behind other people's turns. It is answered once the
    transaction is on the wire; a second message follows when it lands (landing.py), sent after
    the turn is over, so the user can carry on meanwhile.
    """
    query = update.callback_query
    chat_id = _private_chat_id(update)
    if chat_id is None:
        await query.answer()
        return
    user_id = _resolve_user(chat_id)
    if user_id is None:
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text=UNLINKED_MESSAGE)
        return
    tapped = _QUOTE_BUTTON.match(query.data or "")
    active = resolve_active_chain(user_id, context)
    if tapped is None or active is None or int(tapped[2]) != active[0]:
        await query.answer(OTHER_NETWORK_QUOTE, show_alert=True)
        return
    confirm, quote_id = tapped[1] == "ok", tapped[3]
    chain_id, network = active

    await query.answer()
    with contextlib.suppress(BadRequest):
        # Refused for a message too old to edit: quotes.take still refuses a second confirm.
        await query.edit_message_reply_markup(reply_markup=None)
    send = functools.partial(context.bot.send_message, chat_id)
    landed = None
    async with _turn_locks[(chain_id, chat_id)]:
        if confirm:
            async with _typing(context.bot, chat_id):
                try:
                    confirmed = await _on_tap_thread(confirm_quote, user_id, chain_id, quote_id, network)
                    outcome, landed = confirmed.text, confirmed.landed
                except QuoteError:
                    outcome = STALE_QUOTE
        else:
            cancelled = await _on_tap_thread(cancel_quote, user_id, chain_id, quote_id)
            outcome = CANCELLED_REPLY if cancelled else STALE_QUOTE
        await _reply_lines(send, chain_id, [_escaped(outcome)])
    if landed is not None:
        # Awaited here, not on a thread: the wait itself runs in landing's pool.
        await _reply_lines(send, chain_id, [_escaped(await asyncio.wrap_future(landed))])


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /cancel: drops every quote waiting on the network in use."""
    acting = await _acting_account(update, context)
    if acting is None:
        return
    user_id, chain_id, _ = acting
    async with _turn_locks[(chain_id, update.message.chat_id)]:
        dropped = await _on_tap_thread(cancel_quotes, user_id, chain_id)
    await update.message.reply_text(CANCELLED_REPLY if dropped else "There was nothing to cancel.")


class _Refused(Exception):
    """A /send or /swap that doesn't check out, with the reply that says why."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def _checked_amount(amount: str, usage: str) -> str:
    """An amount from /send or /swap, as typed, once it is a number above zero."""
    if not _AMOUNT.match(amount) or Decimal(amount) <= 0:
        raise _Refused(f"“{amount}” isn't an amount. {usage}")
    return amount


def _checked_token(user_id: int, chain_id: int, token: str) -> str:
    """
    A token from /send or /swap, named as the assistant should hear it: the network's own coin by
    its ticker (ETH, BNB), any other token by its ticker in capitals. Only a token the wallet knows
    on this network passes -- listed, or added in the web app -- and "ETH" is the native coin only
    where it is: on BSC it would be a token, and the assistant reads "eth" as the native coin.

    Runs with the network acting (db.acting_network).
    """
    native = get_native_asset_ticker(chain_id)
    if token.upper() == native.upper():
        return native
    try:
        return _token_label(user_id, _token_address(user_id, token))
    except ToolException:
        raise _Refused(
            f"I don't know a token called {token} on {get_chain_display_name(chain_id)}. You can add "
            "it in the web app (Dashboard → Balances → Add token)."
        )


def send_words(user_id: int, chain_id: int, args: list[str]) -> str:
    """
    What /send <amount> <token> [to] <contact> asks the assistant, once it checks out: "Send 10
    USDC to sam". The contact must be saved; the assistant checks again when it quotes.

    Runs with the network acting (db.acting_network).

    @raises _Refused  With the reply that says what's wrong.
    """
    if args[3:] and args[2].lower() == "to":
        args = args[:2] + args[3:]
    if len(args) < 3:
        raise _Refused(SEND_USAGE)
    amount = _checked_amount(args[0], SEND_USAGE)
    token = _checked_token(user_id, chain_id, args[1])
    contact = " ".join(args[2:])
    if get_contact(user_id, contact) is None:
        raise _Refused(
            f"{contact} isn't one of your contacts. Add them in the web app (Contacts), then send this again."
        )
    return f"Send {amount} {token} to {contact}"


def swap_words(user_id: int, chain_id: int, args: list[str]) -> str:
    """
    What /swap <amount> <token> [for|to] <token> asks the assistant, once it checks out: "Swap 1
    ETH for USDC".

    Runs with the network acting (db.acting_network).

    @raises _Refused  With the reply that says what's wrong.
    """
    if len(args) == 4 and args[2].lower() in ("for", "to"):
        args = [args[0], args[1], args[3]]
    if len(args) != 3:
        raise _Refused(SWAP_USAGE)
    amount = _checked_amount(args[0], SWAP_USAGE)
    sold, bought = _checked_token(user_id, chain_id, args[1]), _checked_token(user_id, chain_id, args[2])
    try:
        _refuse_same_token(user_id, sold, bought, "there is nothing to swap")
    except ToolException:
        raise _Refused(f"{sold} and {bought} are the same token here, so there's nothing to swap.")
    return f"Swap {amount} {sold} for {bought}"


def _words_for(words, user_id: int, chain_id: int, network: str, args: list[str]) -> str:
    """Runs a /send or /swap check (send_words, swap_words) with the network acting."""
    with acting_network(user_id, network, chain_id):
        return words(user_id, chain_id, args)


async def _action_command(update: Update, context: ContextTypes.DEFAULT_TYPE, words):
    """
    /send and /swap: checks the command (`words`) and puts its fixed words to the assistant, as a
    message would be. A command that doesn't check out is answered here and costs no turn.
    """
    acting = await _acting_account(update, context)
    if acting is None:
        return
    user_id, chain_id, network = acting
    try:
        text = await asyncio.to_thread(_words_for, words, user_id, chain_id, network, list(context.args or []))
    except _Refused as refused:
        await update.message.reply_text(refused.text)
        return
    except Exception:
        logging.getLogger(__name__).exception("Could not check a command (user %s, %s)", user_id, network)
        await update.message.reply_text("Something went wrong checking that. Please try again.")
        return
    await _run_turn(update, context, user_id, chain_id, network, text)


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /send <amount> <token> [to] <contact>."""
    await _action_command(update, context, send_words)


async def swap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /swap <amount> <token> [for] <token>."""
    await _action_command(update, context, swap_words)


def balance_lines(rows: list[dict]) -> list[str]:
    """
    /balance's lines: the network's own coin first, even at zero, then every token with something
    in it, LP tokens included, then any that couldn't be read.
    """
    lines, unread = [], []
    for row in rows:
        ticker = html.escape(row["ticker"].upper())
        if row.get("amount") is None:
            unread.append(ticker)
        elif row["native"] or row["amount"] > 0:
            lines.append(f"• {_price(row['amount'])} {ticker}")
    if unread:
        lines.append(f"Couldn't read: {', '.join(unread)}.")
    return lines


def _window(hours: float) -> str:
    """A spending window in words: "per day", "per 7 days", "per hour", "per 12 hours"."""
    if hours == 24:
        return "per day"
    if hours > 24 and hours % 24 == 0:
        return f"per {hours / 24:g} days"
    return "per hour" if hours == 1 else f"per {hours:g} hours"


def limit_lines(status: dict, chain: str, now: int) -> list[str]:
    """
    /limit's lines: what is left of the spending limit, whether the owner has paused the wallet,
    and the assistant's access -- its warning when one is due (_key_warning), otherwise its date.
    """
    lines = [
        f"Spending limit: ${status['remaining_usd']:,.2f} left of ${status['daily_limit_usd']:,.2f} "
        f"{_window(status['window_hours'])}."
    ]
    if status.get("paused"):
        lines.append("⏸ The owner has paused this wallet: nothing can be sent until it is unpaused in "
                     "the web app (Controls).")
    warning = _key_warning(status, chain, now)
    if warning:
        lines.append(html.escape(warning))
    else:
        until = datetime.datetime.fromtimestamp(status["session_expires_at"], tz=datetime.timezone.utc)
        lines.append(f"My access runs out on {until.day} {until:%b %Y}.")
    return lines


# How /history marks a transaction that isn't simply done.
_HISTORY_STATUS = {"pending": " (pending)", "failed": " (failed)", "dropped": " (dropped)"}


def history_lines(rows: list[dict]) -> list[str]:
    """/history's lines, newest first: when (UTC), what, how it ended, and its hash, shortened."""
    lines = []
    for row in rows:
        when = datetime.datetime.fromtimestamp(row["mined_at"] or row["created_at"], tz=datetime.timezone.utc)
        tx_hash = row["tx_hash"]
        short = f" <code>{tx_hash[:10]}…{tx_hash[-4:]}</code>" if tx_hash else ""
        lines.append(
            f"• {when.day} {when:%b %H:%M} UTC: {html.escape(row['action'] or 'Transaction')}"
            f"{_HISTORY_STATUS.get(row['status'], '')}{short}"
        )
    return lines


def _amounts(shares: list[dict]) -> str:
    """A position's token amounts ([{ticker, decimals, raw}]), as "0.5 ETH + 1,200 USDC"."""
    return " + ".join(
        f"{_price(float(Decimal(s['raw']) / Decimal(10 ** s['decimals'])))} {s['ticker'].upper()}" for s in shares
    )


def position_lines(positions: list[dict]) -> list[str]:
    """/positions' lines: each position's pair, id, range, whether it is earning, what it holds and its fees."""
    lines = []
    for p in positions:
        base, quote = p["base"].upper(), p["quote"].upper()
        if p["full_range"] or p["max_price"] is None:
            span = "full range"
        else:
            span = f"{_price(p['min_price'])}–{_price(p['max_price'])} {quote} per {base}"
        state = "earning fees" if p["in_range"] or p["full_range"] else (
            f"out of range ({base} at {_price(p['current_price'])} {quote}), not earning"
        )
        lines.append(html.escape(
            f"• {p['pair'].upper()} #{p['id']}: {span}, {state}. Holds {_amounts(p['amounts'])}; "
            f"fees to collect {_amounts(p['fees'])}."
        ))
    return lines


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /balance: the wallet's balances on the network in use. No model call."""
    acting = await _acting_account(update, context)
    if acting is None:
        return
    user_id, chain_id, network = acting
    rows = await asyncio.to_thread(_read_balances, user_id, chain_id, network)
    await _reply_lines(update.message.reply_text, chain_id, balance_lines(rows) if rows is not None else [READ_FAILED])


async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /limit: the spending limit, the pause, and the assistant's access. No model call."""
    acting = await _acting_account(update, context)
    if acting is None:
        return
    user_id, chain_id, network = acting
    status = await asyncio.to_thread(_read_wallet_status, user_id, chain_id, network)
    lines = (limit_lines(status, get_chain_display_name(chain_id), int(time.time()))
             if status is not None else [READ_FAILED])
    await _reply_lines(update.message.reply_text, chain_id, lines)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /history: the last few transactions on the network in use, from the database."""
    acting = await _acting_account(update, context)
    if acting is None:
        return
    user_id, chain_id, _ = acting
    rows = get_transactions(user_id, chain_id, limit=HISTORY_COMMAND_ROWS)
    lines = history_lines(rows) if rows else ["No transactions yet."]
    lines.append("All of them are in the web app's History tab (/web).")
    await _reply_lines(update.message.reply_text, chain_id, lines)


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /positions: the wallet's Uniswap V3 positions, on a network that trades there."""
    acting = await _acting_account(update, context)
    if acting is None:
        return
    user_id, chain_id, network = acting
    if get_exchange(chain_id) != "uniswap-v3":
        lines = [f"Positions are Uniswap V3's, and on {html.escape(get_chain_display_name(chain_id))} I trade "
                 "on PancakeSwap: your LP tokens are in /balance."]
    else:
        positions = await asyncio.to_thread(_read_positions, user_id, chain_id, network)
        if positions is None:
            lines = [READ_FAILED]
        else:
            lines = position_lines(positions) or ["No positions. Ask me to add liquidity to open one."]
    await _reply_lines(update.message.reply_text, chain_id, lines)


async def contacts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /contacts: the people the assistant may pay. Read only: they are changed on the web."""
    acting = await _acting_account(update, context)
    if acting is None:
        return
    user_id, chain_id, _ = acting
    contacts = get_all_contacts(user_id)
    lines = [f"• {html.escape(c['name'])}: <code>{html.escape(c['address'])}</code>" for c in contacts]
    if not lines:
        lines = ["No contacts yet. I can only pay people saved in the web app."]
    lines.append("Add or remove contacts in the web app (/web).")
    await _reply_lines(update.message.reply_text, chain_id, lines)


def web_app_url() -> str:
    """
    The web app's address: WEB_APP_URL, or else the first origin the API lets in (CORS_ORIGINS,
    with api.py's default), which is the web app's own.
    """
    explicit = os.getenv("WEB_APP_URL", "").strip()
    if explicit:
        return explicit
    origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
    return origins[0] if origins else "http://localhost:3000"


async def web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /web: a link to the web app, where contacts, the limit and the key are managed."""
    if _resolve_user(update.message.chat_id) is None:
        await update.message.reply_text(UNLINKED_MESSAGE)
        return
    await update.message.reply_text(f"The web app: {web_app_url()}", link_preview_options=NO_PREVIEW)


async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /feedback <text>: a note about the app for the team, stored with the network in use (if
    any). FEEDBACK_PER_HOUR and FEEDBACK_PER_DAY per account, counted in the database, so a restart
    doesn't reset them. Not a rating of the service's on-chain agent: that is a transaction, which
    the assistant quotes when asked to leave a review.
    """
    user_id = _resolve_user(update.message.chat_id)
    if user_id is None:
        await update.message.reply_text(UNLINKED_MESSAGE)
        return
    # The text after the command, as written: line breaks and all.
    parts = update.message.text.split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if len(text) < FEEDBACK_MIN_CHARS:
        await update.message.reply_text(FEEDBACK_USAGE)
        return
    if len(text) > FEEDBACK_MAX_CHARS:
        await update.message.reply_text(f"That's a bit long: please keep it under {FEEDBACK_MAX_CHARS:,} characters.")
        return
    now = int(time.time())
    if (count_feedback_since(user_id, now - 3600) >= FEEDBACK_PER_HOUR
            or count_feedback_since(user_id, now - 86_400) >= FEEDBACK_PER_DAY):
        await update.message.reply_text("Thanks — that's plenty for now. Please send more later.")
        return
    active = resolve_active_chain(user_id, context)
    add_feedback(user_id, active[0] if active else None, "telegram", text)
    await update.message.reply_text("Thanks, I've passed that on to the team.")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers a command the bot doesn't have, rather than leaving it unanswered."""
    await update.message.reply_text("I don't know that command. /help lists the ones I have.")


# Every command: its name, its handler, and its line in Telegram's command menu, in menu order.
COMMANDS = (
    ("start", start, "Start, or link this chat"),
    ("help", help_cmd, "What I can do"),
    ("balance", balance_cmd, "What's in the wallet"),
    ("limit", limit_cmd, "Your spending limit and my access"),
    ("history", history_cmd, "The last transactions"),
    ("positions", positions_cmd, "Your Uniswap positions"),
    ("contacts", contacts_cmd, "Who I can pay"),
    ("send", send_cmd, "Pay a contact: /send 10 USDC to Sam"),
    ("swap", swap_cmd, "Swap tokens: /swap 1 ETH for USDC"),
    ("cancel", cancel_cmd, "Drop a quote I'm holding"),
    ("chain", chain_cmd, "Pick the network I act on"),
    ("web", web_cmd, "Open the web app"),
    ("feedback", feedback_cmd, "Tell the team what you think"),
)


def build_bot(token: str, networks: dict[int, str]) -> Application:
    """
    The bot: its handlers, its daily check, and the networks it serves.

    The networks go in bot_data, where every handler and job reads them. Which one a turn acts on
    is the account's own choice (/chain), read from the database -- nothing in a message sets it.

    @param token     The bot's token from BotFather.
    @param networks  {chain_id: this server's network name (network_config.network_name)} for every
                     chain to serve: the BOT_CHAINS whose network answered at startup.
    """
    bot = Application.builder().token(token).concurrent_updates(True).build()
    bot.bot_data["networks"] = dict(networks)
    for name, handler, _ in COMMANDS:
        bot.add_handler(CommandHandler(name, handler, filters=PRIVATE_MESSAGES))
    bot.add_handler(CallbackQueryHandler(chain_chosen, pattern=r"^chain:"))
    bot.add_handler(CallbackQueryHandler(quote_button, pattern=r"^q:"))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & PRIVATE_MESSAGES, start_chat))
    # Last: only a command none of the handlers above took gets here.
    bot.add_handler(MessageHandler(filters.COMMAND & PRIVATE_MESSAGES, unknown_cmd))
    bot.job_queue.run_daily(daily_check, time=DAILY_CHECK_TIME, name="daily-check")
    return bot


def check_network(chain_id: int, network: str) -> str | None:
    """
    Why the bot must not serve `chain_id`, or None when its network answers as that chain.

    The bot must never act on one chain while it means another, so the network's RPC is asked which
    chain it is: a wrong rpcs row or a fork on the wrong port leaves that network out instead.

    @return  The reason, or None.
    """
    try:
        w3, _ = load_network_config_by_name(network)
    except ValueError as e:
        return str(e)
    try:
        live_id = w3.eth.chain_id
    except Exception as e:
        # The type only: the error's text can hold the RPC's URL, API key and all.
        return f"can't reach {network} ({type(e).__name__})"
    if live_id != chain_id:
        return f"the RPC for {network} is chain {live_id}, not {chain_id}"
    return None


async def serve(bot: Application):
    """
    Runs the bot until the process is told to stop (Ctrl-C, or SIGTERM from a service manager).

    Not Application.run_polling, which owns the event loop: here the checkpointer every turn shares
    opens in the same loop before the first update and closes after the last.
    """
    await open_checkpointer()
    init_agent()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with contextlib.AsyncExitStack() as running:
            try:
                await running.enter_async_context(bot)  # initialize() now, shutdown() at exit
            except InvalidToken:
                raise SystemExit(f"Telegram refused the token in {TOKEN_ENV}. Check it in .env.")
            await bot.bot.set_my_commands([BotCommand(name, menu) for name, _, menu in COMMANDS])
            await bot.start()
            running.push_async_callback(bot.stop)
            await bot.updater.start_polling()
            running.push_async_callback(bot.updater.stop)
            print(f"@{bot.bot.username} is serving {', '.join(bot.bot_data['networks'].values())}.")
            await stop.wait()
    finally:
        await close_checkpointer()


def main():
    install_log_redaction()
    # This process bundles with its own key, never the API's: the two run side by side, and each
    # keeps its own nonce counter, so sharing one key would have them hand out the same nonces.
    use_bundler_key(TELEGRAM_BUNDLER_ENV)

    # The bot reads columns the latest migrations add (users.telegram_chain_id, from 6). It never
    # migrates the database itself: that is `make db`'s job.
    if schema_version(get_db()) < len(MIGRATIONS):
        raise SystemExit("The database is out of date: run `make db`, then start the bot again.")

    token = bot_token()
    if token is None:
        legacy = legacy_vars_set()
        if legacy:
            # Set up before there was one bot for every network: say what to change, not just "missing".
            raise SystemExit(
                f"{TOKEN_ENV} is not set. Mitfah runs one Telegram bot for every network now: put its "
                f"token in {TOKEN_ENV} and its username in {USERNAME_ENV}, and delete "
                f"{', '.join(legacy)} from .env."
            )
        raise SystemExit(f"{TOKEN_ENV} is not set: put the bot's token from BotFather in .env.")

    networks = {}
    for chain_id in BOT_CHAINS:
        network = network_name(chain_id)
        problem = check_network(chain_id, network)
        if problem:
            print(f"Not serving {get_chain_display_name(chain_id)}: {problem}.")
            continue
        networks[chain_id] = network
    if not networks:
        raise SystemExit("The bot can't start: none of its networks is up (see above).")
    asyncio.run(serve(build_bot(token, networks)))


if __name__ == "__main__":
    main()
