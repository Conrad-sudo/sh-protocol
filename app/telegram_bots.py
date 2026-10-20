"""
The Telegram bot: one bot for every chain, set up in BotFather.

The user picks the network inside the chat, with /chain (telebot), and that choice is stored per
account (users.telegram_chain_id). Nothing typed in a message can move a turn to another chain or
another account.

The bot's token and username come from .env: MITFAH_API holds the token, MITFAH_USERNAME the
username. Without a token `make bot` does not start; without a username the web app offers no link.
"""
import os

from constants import CHAIN_ID_ARBITRUM, CHAIN_ID_BASE, CHAIN_ID_BSC, CHAIN_ID_MAINNET, CHAIN_ID_SEPOLIA

TOKEN_ENV = "MITFAH_API"
USERNAME_ENV = "MITFAH_USERNAME"

# The chains the bot can serve, in the order /chain lists them. One whose network is down when the
# bot starts is left out until the next start (telebot.check_network).
BOT_CHAINS: tuple[int, ...] = (
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
    CHAIN_ID_BSC,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BASE,
)

# The stems of the per-chain variables from when every chain had a bot of its own
# (MITFAH_<STEM>_API / MITFAH_<STEM>_USERNAME). Read only to say what to rename.
_LEGACY_STEMS = ("ETH", "SEPOLIA", "BSC", "ARB", "BASE")


def bot_token() -> str | None:
    """The bot's token, or None when MITFAH_API is not set."""
    return (os.getenv(TOKEN_ENV) or "").strip() or None


def bot_username() -> str | None:
    """
    The bot's username, without the @, or None when MITFAH_USERNAME is not set.

    The @ is dropped if .env has one: the username goes into t.me links, which take it bare.
    """
    return (os.getenv(USERNAME_ENV) or "").strip().lstrip("@") or None


def legacy_vars_set() -> list[str]:
    """
    The per-chain bot variables still set in the environment, so a start that finds no MITFAH_API
    can say what to rename instead of just "not set".
    """
    return [
        name
        for stem in _LEGACY_STEMS
        for name in (f"MITFAH_{stem}_API", f"MITFAH_{stem}_USERNAME")
        if os.getenv(name)
    ]
