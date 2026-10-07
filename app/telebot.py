"""
The Telegram front end: one bot per chain, all run by this one process (`make bot`).

Each bot is tied to its chain. A message to the Base bot is a turn on the user's Base wallet,
whatever network they last deployed on: the chat the user picks IS the network, and nothing typed
in it can move a turn elsewhere. Tokens and usernames come from .env -- see telegram_bots.

One process rather than five, so the bots share one agent, one checkpointer and one bundler key
(TELEGRAM_BUNDLER). Sharing the key is safe because tx_sender counts nonces per (chain, address)
under one lock -- but only within a process, so never run two copies of this.
"""
import asyncio
import collections
import contextlib
import datetime
import html
import logging
import signal
import time

from telegram import BotCommand, LinkPreviewOptions, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from smart_wallet_agent import chat, init_agent, open_checkpointer, close_checkpointer
from bundler import TELEGRAM_BUNDLER_ENV, use_bundler_key
from constants import get_chain_display_name, get_native_asset_ticker
from tools import _get_wallet_status
from db import (
    acting_network,
    consume_telegram_link_nonce,
    get_telegram_chats_on_chain,
    get_user_id_by_telegram_chat_id,
    get_wallet_chains,
    link_telegram,
)
from network_config import load_network_config_by_name, network_name
from telegram_bots import bot_token, chains_with_bots, token_env
from telegram_format import plain_text, to_telegram_html

# Warn when less than this fraction of the window spending cap remains.
BUDGET_ALERT_THRESHOLD = 0.10

# Warn this far ahead of the session key's deadline (3 days). A fixed lead time rather than a
# fraction of the key's life: what the user needs is enough notice to sign a renewal in the web
# app, and that does not scale with how long the key was granted for.
SESSION_EXPIRY_WARN_SECS = 3 * 86_400

# When each bot checks its users' wallets, every day, in UTC. A fixed time of day rather than "every
# 24 hours from startup", so a restart neither repeats a warning nor puts the next one off.
DAILY_CHECK_TIME = datetime.time(hour=12, tzinfo=datetime.timezone.utc)

# How soon after /start the wallet is first checked, so a key about to run out is mentioned now
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
# edited message is not a new instruction, so it is not answered as one.
PRIVATE_MESSAGES = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

# Shown whenever a chat that no account has claimed tries to use a bot.
UNLINKED_MESSAGE = (
    "This Telegram account isn't linked to a wallet yet.\n\n"
    "Sign in on the web app and choose \"Link Telegram\" in Settings — it will send you back here "
    "with a one-time link that binds this chat to your account."
)

# Sent in place of an empty reply, which Telegram would refuse.
EMPTY_REPLY = "Sorry, I have no answer to that. Please try again."

# One turn at a time per conversation. Different users are answered side by side (the bots are
# built with concurrent_updates), but one user's messages wait their turn: a "yes" sent while the
# quote it answers is still being worked out must find that quote. Keyed by chain as well as chat,
# since the chat id is the same in every bot and each chain is a conversation of its own.
_turn_locks: dict[tuple[int, int], asyncio.Lock] = collections.defaultdict(asyncio.Lock)


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


def _bot_chain(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, str]:
    """The chain this bot serves and this server's network name for it, fixed when it was built."""
    return context.bot_data["chain_id"], context.bot_data["network"]


def _no_wallet_message(chain_id: int) -> str:
    chain = get_chain_display_name(chain_id)
    return f"You don't have a wallet on {chain} yet. Set one up in the web app, then come back here."


async def _send_reply(message: Message, piece: str):
    """
    Sends one message of a reply, formatted (telegram_format). Should Telegram refuse the
    formatting, the same words go out plain rather than not at all.
    """
    try:
        await message.reply_text(piece, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
    except BadRequest:
        logging.getLogger(__name__).warning("Telegram refused a reply's formatting; sent it plain", exc_info=True)
        await message.reply_text(plain_text(piece), link_preview_options=NO_PREVIEW)


def wallet_warnings(status: dict, chain: str, now: int) -> list[str]:
    """
    What to warn the user about: the assistant's key is inactive, it runs out within
    SESSION_EXPIRY_WARN_SECS, or less than BUDGET_ALERT_THRESHOLD of the cap is left.

    The expiry warning is the reason this matters to a Telegram-only user: renewing the key is an
    owner-signed transaction they can only make in the web app, so being told after it lapsed means
    being told too late.

    @param status  The wallet's status, as _get_wallet_status returns it.
    @param chain   The chain's name, for the text.
    @param now     Unix seconds.
    @return        The messages to send, in order; none when all is well.
    """
    if not status.get("session_active"):
        return [
            f"⚠️ My access to your {chain} wallet is off: it was turned off, replaced or has run "
            "out. I can still answer questions, but I can't send anything until you turn it back on "
            "in the web app (Controls → Assistant)."
        ]

    warnings = []
    seconds_left = (status.get("session_expires_at") or 0) - now
    if 0 < seconds_left < SESSION_EXPIRY_WARN_SECS:
        days_left = seconds_left // 86_400
        when = f"in {days_left} day{'s' if days_left != 1 else ''}" if days_left else "in under a day"
        warnings.append(
            f"⏳ My access to your {chain} wallet runs out {when}. Renew it in the web app "
            "(Controls → Assistant) to keep me able to send transactions for you."
        )

    limit = status.get("daily_limit_usd") or 0
    remaining = status.get("remaining_usd") or 0
    if limit > 0 and remaining < BUDGET_ALERT_THRESHOLD * limit:
        warnings.append(
            f"⚠️ Low spending budget on {chain}: only ${remaining:,.2f} of your ${limit:,.2f} limit "
            "is left. It refills when the current period ends."
        )
    return warnings


def _read_wallet_status(user_id: int, chain_id: int, network: str) -> dict | None:
    """
    The user's wallet status on a bot's chain, or None when it can't be read.

    Blocking (chain reads), so it runs on a worker thread. The bot's chain is made the acting
    network for the read, as for a chat turn; otherwise it would read the chain the user last
    deployed on.
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


async def _warn(bot, chat_id: int, user_id: int, chain_id: int, network: str):
    """Checks the user's wallet on a bot's chain and sends them whatever warnings apply."""
    status = await asyncio.to_thread(_read_wallet_status, user_id, chain_id, network)
    if status is None:
        return
    for text in wallet_warnings(status, get_chain_display_name(chain_id), int(time.time())):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Forbidden:
            # The user never pressed Start in this bot, or blocked it: it may not write to them.
            return


async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    """
    The daily round: every linked chat with a wallet on this bot's chain hears whatever applies.

    The list is read afresh each time, so a chat linked or a wallet set up since the bot started is
    included without a restart.
    """
    chain_id, network = _bot_chain(context)
    for user_id, chat_id in get_telegram_chats_on_chain(chain_id):
        try:
            await _warn(context.bot, chat_id, user_id, chain_id, network)
        except TelegramError:
            # Telegram unreachable for this one: the rest still hear from the bot.
            logging.getLogger(__name__).warning(
                "Could not send the daily check to chat %s on %s", chat_id, network, exc_info=True
            )


async def first_check(context: ContextTypes.DEFAULT_TYPE):
    """The first look at a user's wallet, just after /start. The job carries the chat and the user."""
    chain_id, network = _bot_chain(context)
    await _warn(context.bot, context.job.chat_id, context.job.data, chain_id, network)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /start, optionally completing a Telegram link carried in the deep-link payload.

    `/start <nonce>` arrives when the user follows the t.me link the web app minted for them. The
    account comes from the NONCE and the chat id comes from the Telegram update — neither is taken
    from anything the user typed. That is the whole point of the flow: a chat id is enumerable, so
    a form that let someone enter one would let them attach their Telegram to another person's
    wallet and spend against its cap.

    A link made through any one bot covers them all, since the chat id is the same in every bot:
    in the others the user just presses Start.
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
            "✅ Telegram linked to your wallet account. Each network has a Mitfah bot of its own, "
            "and they all know you now: open any of them from the web app's Settings."
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

    chain_id, _ = _bot_chain(context)
    if chain_id not in get_wallet_chains(user_id):
        await update.message.reply_text(_no_wallet_message(chain_id))
        return

    context.job_queue.run_once(
        first_check,
        when=FIRST_CHECK_DELAY_SECS,
        chat_id=chat_id,
        # The account to report on. Kept separate from chat_id: one addresses Telegram, the other
        # addresses the wallet, and they are not the same number.
        data=user_id,
    )
    chain = get_chain_display_name(chain_id)
    await update.message.reply_text(
        f"Welcome to Mitfah on {chain}. Ask me about your {chain} wallet, or ask me to pay a "
        "contact or swap tokens. Send /help for examples."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /help: what the bot does, with examples for its chain."""
    chain_id, _ = _bot_chain(context)
    chain = get_chain_display_name(chain_id)
    await update.message.reply_text(
        f"I look after your {chain} wallet. Just write to me, for example:\n"
        "• What's in my wallet?\n"
        "• How much can I still spend?\n"
        f"• Send 0.01 {get_native_asset_ticker(chain_id)} to Sam\n\n"
        "Before I send anything I tell you what it costs, and I only send it once you say yes. I "
        "can only pay contacts saved in the web app, and only within your spending limit.\n\n"
        f"Each network has its own Mitfah bot; this one only acts on your {chain} wallet. Your "
        "limit, contacts and my access are managed in the web app."
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


async def start_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes an ordinary message to the agent, for linked chats only.

    The identity handed to chat() is resolved here, from the database, and is never anything the
    message says. The chain is this bot's own: the turn acts on the wallet the user chose by
    choosing the chat, not on the network they last deployed on.
    """
    chat_id = update.message.chat_id
    user_id = _resolve_user(chat_id)
    if user_id is None:
        await update.message.reply_text(UNLINKED_MESSAGE)
        return

    chain_id, network = _bot_chain(context)
    if chain_id not in get_wallet_chains(user_id):
        await update.message.reply_text(_no_wallet_message(chain_id))
        return

    async with _turn_locks[(chain_id, chat_id)]:
        # chat() blocks for the whole turn, so it runs on a thread, leaving the bots free.
        async with _typing(context.bot, chat_id):
            response = await asyncio.to_thread(chat, user_id, chain_id, update.message.text, network)
        for piece in to_telegram_html(response) or [html.escape(EMPTY_REPLY)]:
            await _send_reply(update.message, piece)


def build_bot(token: str, chain_id: int, network: str) -> Application:
    """
    One chain's bot: its handlers, its daily check, and the chain it serves.

    The chain goes in bot_data, where every handler and job of this bot reads it. Nothing else sets
    it, so no message can move a turn to another chain.

    @param token     The bot's token from BotFather.
    @param chain_id  The chain the bot serves.
    @param network   This server's network name for that chain (network_config.network_name).
    """
    bot = Application.builder().token(token).concurrent_updates(True).build()
    bot.bot_data["chain_id"] = chain_id
    bot.bot_data["network"] = network
    bot.add_handler(CommandHandler("start", start, filters=PRIVATE_MESSAGES))
    bot.add_handler(CommandHandler("help", help_cmd, filters=PRIVATE_MESSAGES))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & PRIVATE_MESSAGES, start_chat))
    bot.job_queue.run_daily(daily_check, time=DAILY_CHECK_TIME, name="daily-check")
    return bot


def check_network(chain_id: int, network: str) -> str | None:
    """
    Why `chain_id`'s bot must not start, or None when its network answers as that chain.

    A bot named for one chain must never act on another, so its RPC is asked which chain it is: a
    wrong rpcs row or a fork on the wrong port keeps the bot off instead.

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


async def serve(bots: list[Application]):
    """
    Runs the bots until the process is told to stop (Ctrl-C, or SIGTERM from a service manager).

    Not Application.run_polling: that runs ONE bot and owns the event loop. Here every bot polls in
    one loop, beside the checkpointer all their turns share.
    """
    await open_checkpointer()
    init_agent()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with contextlib.AsyncExitStack() as running:
            for bot in bots:
                try:
                    await running.enter_async_context(bot)  # initialize() now, shutdown() at exit
                except InvalidToken:
                    raise SystemExit(
                        f"Telegram refused the token in {token_env(bot.bot_data['chain_id'])}. "
                        "Check it in .env."
                    )
                await bot.bot.set_my_commands(
                    [BotCommand("start", "Start, or link this chat"), BotCommand("help", "What I can do")]
                )
                await bot.start()
                running.push_async_callback(bot.stop)
                await bot.updater.start_polling()
                running.push_async_callback(bot.updater.stop)
                print(f"@{bot.bot.username} is serving {bot.bot_data['network']}.")
            await stop.wait()
    finally:
        await close_checkpointer()


def main():
    # This process bundles with its own key, never the API's: the two run side by side, and each
    # keeps its own nonce counter, so sharing one key would have them hand out the same nonces.
    use_bundler_key(TELEGRAM_BUNDLER_ENV)

    bots = []
    for chain_id in chains_with_bots():
        network = network_name(chain_id)
        problem = check_network(chain_id, network)
        if problem:
            print(f"Not starting the {get_chain_display_name(chain_id)} bot: {problem}.")
            continue
        bots.append(build_bot(bot_token(chain_id), chain_id, network))
    if not bots:
        raise SystemExit(
            "No Telegram bot can start: set MITFAH_<CHAIN>_API in .env for a chain whose network is up."
        )
    asyncio.run(serve(bots))


if __name__ == "__main__":
    main()
