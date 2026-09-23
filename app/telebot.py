import os
import asyncio
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from smart_wallet_agent import chat, init_agent,open_checkpointer,close_checkpointer
from bundler import TELEGRAM_BUNDLER_ENV, use_bundler_key
from tools import _get_all_sessions
from db import consume_telegram_link_nonce, get_user_id_by_telegram_chat_id, link_telegram
from network_config import load_network_config

telegram_token = os.getenv("TELEGRAM_TOKEN")

# Warn when less than this fraction of the window spending cap remains.
BUDGET_ALERT_THRESHOLD = 0.10

# Shown whenever a chat that no account has claimed tries to use the bot.
UNLINKED_MESSAGE = (
    "This Telegram account isn't linked to a wallet yet.\n\n"
    "Sign in on the web app and choose \"Connect Telegram\" — it will send you back here with a "
    "one-time link that binds this chat to your account."
)


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


async def budget_alert(context: ContextTypes.DEFAULT_TYPE):
    """
    Job callback that checks the user's wallet spending-cap status and warns them when the
    session key is inactive or the remaining USD budget for the current window has dropped
    below BUDGET_ALERT_THRESHOLD of the cap.

    Replaces the old per-token session-expiry alert: session keys no longer expire, and spending
    is bounded by a single wallet-wide USD cap per rolling window. Scheduled via JobQueue — not
    triggered by a user message. The job carries the chat to message in context.job.chat_id and the
    account to report on in context.job.data.
    """
    chat_id = context.job.chat_id
    user_id = context.job.data
    try:
        # The plain function, not the @tool: this job runs on a timer with no agent, so there is
        # no ToolRuntime to satisfy the tool wrapper's first parameter.
        status = _get_all_sessions(user_id)
    except Exception:
        # No wallet deployed for this user yet (load_session_handler raises) — nothing to report.
        return

    if not status.get("session_active"):
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Your wallet's session key is not currently authorized. "
                "New transactions will be rejected until it is re-added."
            ),
        )
        return

    limit = status.get("daily_limit_usd") or 0
    remaining = status.get("remaining_usd") or 0
    if limit > 0 and remaining < BUDGET_ALERT_THRESHOLD * limit:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Low spending budget: only ${remaining:,.2f} of your ${limit:,.2f} "
                f"per-window cap remains. It refills when the current window rolls over."
            ),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /start, optionally completing a Telegram link carried in the deep-link payload.

    `/start <nonce>` arrives when the user follows the t.me link the web app minted for them. The
    account comes from the NONCE and the chat id comes from the Telegram update — neither is taken
    from anything the user typed. That is the whole point of the flow: a chat id is enumerable, so
    a form that let someone enter one would let them attach their Telegram to another person's
    wallet and spend against its cap.
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
        await update.message.reply_text("✅ Telegram linked to your wallet account.")
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

    # Schedule the budget check for this user, replacing any existing job
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()
    context.job_queue.run_repeating(
        budget_alert,
        interval=86400,  # every 24 hours
        first=10,  # first run 10 seconds after /start
        chat_id=chat_id,
        name=str(chat_id),
        # The account to report on. Kept separate from chat_id: one addresses Telegram, the other
        # addresses the wallet, and they are no longer the same number.
        data=user_id,
    )
    await update.message.reply_text(
        "Welcome to your smart wallet assistant.\n" "Simply say Hi to start chatting."
    )


async def help_cmd(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Help menu")


async def start_chat(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    Routes an ordinary message to the agent, for linked chats only.

    The identity handed to chat() is resolved here, from the database, and is never anything the
    message says. The chain comes from the user's saved network — the same source every tool
    resolves against — so the conversation history and the tools can never end up on different
    chains.
    """
    chat_id = update.message.chat_id
    user_id = _resolve_user(chat_id)
    if user_id is None:
        await update.message.reply_text(UNLINKED_MESSAGE)
        return

    try:
        _, chain_id, _ = load_network_config(user_id)
    except ValueError:
        await update.message.reply_text(
            "You don't have a wallet yet. Deploy one from the web app to get started."
        )
        return

    query = update.message.text

    # chat() is synchronous/blocking — run it in a thread to avoid blocking the event loop
    response = await asyncio.to_thread(chat, user_id, chain_id, query)
    await update.message.reply_text(response)


async def post_init(application: Application) -> None:
    """
    Called once after the Application is initialised but before polling starts.

    Opens the checkpointer and initialises the agent.
    """
    await open_checkpointer()
    init_agent()

async def post_shutdown(application: Application) -> None:
    await close_checkpointer()


def main():
    # This process bundles with its own key, never the API's: the two run side by side, and each
    # keeps its own nonce counter, so sharing one key would have them hand out the same nonces.
    use_bundler_key(TELEGRAM_BUNDLER_ENV)
    app = Application.builder().token(telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start_chat))
    app.run_polling()


if __name__ == "__main__":
    main()
