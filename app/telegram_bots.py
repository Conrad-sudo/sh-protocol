"""
The Telegram bots: one per chain, each a bot of its own in BotFather.

A bot serves exactly one chain -- a message to the Base bot acts on the user's Base wallet -- so the
user picks the network by picking the chat, and nothing said in a chat can move a turn to another
chain. One link covers every bot: in a private chat Telegram gives a user the same chat id in every
bot, and users.telegram_chat_id holds that one number.

Each bot's token and username come from .env, under the stem below: MITFAH_<STEM>_API holds the
token, MITFAH_<STEM>_USERNAME the username. A chain with no token has no bot: `make bot` skips it.
A chain with no username gets no link: the web app offers none and the link endpoint skips it.
"""
import os

from constants import CHAIN_ID_ARBITRUM, CHAIN_ID_BASE, CHAIN_ID_BSC, CHAIN_ID_MAINNET, CHAIN_ID_SEPOLIA

# The chains with a bot, in the order `make bot` starts them, and each one's .env stem.
_ENV_STEM = {
    CHAIN_ID_MAINNET: "ETH",
    CHAIN_ID_SEPOLIA: "SEPOLIA",
    CHAIN_ID_BSC: "BSC",
    CHAIN_ID_ARBITRUM: "ARB",
    CHAIN_ID_BASE: "BASE",
}


def token_env(chain_id: int) -> str:
    """The name of the env var holding `chain_id`'s bot token, for messages that point at it."""
    return f"MITFAH_{_ENV_STEM[chain_id]}_API"


def bot_token(chain_id: int) -> str | None:
    """The token of `chain_id`'s bot, or None where it has none."""
    if chain_id not in _ENV_STEM:
        return None
    return os.getenv(token_env(chain_id)) or None


def bot_username(chain_id: int) -> str | None:
    """
    The username of `chain_id`'s bot, without the @, or None where it has none.

    The @ is dropped if .env has one: the username goes into t.me links, which take it bare.
    """
    if chain_id not in _ENV_STEM:
        return None
    return (os.getenv(f"MITFAH_{_ENV_STEM[chain_id]}_USERNAME") or "").strip().lstrip("@") or None


def chains_with_bots() -> list[int]:
    """The chains whose bot has a token, in the order `make bot` starts them."""
    return [chain_id for chain_id in _ENV_STEM if bot_token(chain_id)]


def link_bot(chain_id: int | None) -> str | None:
    """
    The username a new Telegram link opens: `chain_id`'s bot, else the first one with a username.

    Any bot can finish a link, since the chat id it binds is the same in all of them.

    @param chain_id  The network the user is on, or None.
    @return          A username without the @, or None when no bot has one.
    """
    if chain_id is not None and (username := bot_username(chain_id)):
        return username
    return next(filter(None, map(bot_username, _ENV_STEM)), None)
