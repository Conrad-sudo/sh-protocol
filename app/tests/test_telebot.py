"""
The Telegram bot, offline: one bot for every network, each account acting on the network it picked
with /chain (or the only one it has), only private chats answered, one link enough, every reply
headed by its network's name and kept within Telegram's limit and formatting, a quote confirmable
only on the network it was raised on, turns rationed per account, and the daily check reaching the
right chats -- with a word about any Uniswap V3 position out of range. Then the commands: the reads
answered without the assistant, /send and /swap checked before they reach it, the quote card and
its buttons, /cancel and /feedback.

No Telegram, no chain and no model: updates and the bot are stand-ins, and the turn, the wallet
reads and the button actions are swapped for recorders. Runs against a throwaway database.

Run: make telebot-test   (or: python app/tests/test_telebot.py)
"""
import asyncio
import contextlib
import io
import itertools
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

# A scratch database, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()

import db                                         # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User  # noqa: E402
from telegram.constants import ParseMode          # noqa: E402
from telegram.error import BadRequest, Forbidden, TimedOut  # noqa: E402
from telegram.ext import CallbackQueryHandler     # noqa: E402

import quotes                                     # noqa: E402
import signing                                    # noqa: E402
import telebot                                    # noqa: E402
import telegram_format                            # noqa: E402
from bundler import UserOpQuote                   # noqa: E402
from chat_limits import TurnGate                  # noqa: E402
from web3 import Web3                             # noqa: E402

MAINNET, SEPOLIA, BSC, ARBITRUM, BASE, CELO = 1, 11155111, 56, 42161, 8453, 42220
# What the bot serves in most checks: every network but Ethereum, as if its RPC were down.
BOT_DATA = {"networks": {SEPOLIA: "sepolia", BSC: "bsc", ARBITRUM: "arbitrum", BASE: "base"}}
WALLET = "0x000000000000000000000000000000000000dEaD"
SAM = Web3.to_checksum_address("0x" + "5a" * 20)
# Where a card's Confirm leads: the approval page under this address.
WEB_APP = "https://app.mitfah.example"
os.environ["WEB_APP_URL"] = WEB_APP
# A priced op for the quotes the fake turns raise: a card freezes it into its sign request.
PRICED = UserOpQuote(
    calldata="0x1234", call_gas=100_000, call_gas_used=80_000, verification_gas=200_000,
    verification_gas_used=150_000, pre_verification_gas=60_000, max_fee=2 * 10**9, tip=10**9,
    base_fee=10**9, outer_gas=500_000, simulated_gas=None, chain_name="base",
)
# Message ids for what the fake Telegram sends, so a card's can be recorded and looked for.
_message_ids = itertools.count(700)


def _message(chat_id: int) -> SimpleNamespace:
    """What Telegram answers a send with: the message, with its chat and a new id."""
    return SimpleNamespace(chat_id=chat_id, message_id=next(_message_ids))


def _token(n: int) -> str:
    return Web3.to_checksum_address(f"0x{n:040x}")


# The networks and listed tokens /send and /swap check against. The real tables are seeded by
# `make db`; a throwaway database starts empty, so the rows these checks need are written here.
# BSC lists WETH (Binance's bridged ETH), so "ETH" there is a token's name, not the native coin's.
_conn = db.get_db()
for name, chain_id in (("base", BASE), ("bsc", BSC), ("sepolia", SEPOLIA)):
    _conn.execute("INSERT OR REPLACE INTO chains (name, chain_id) VALUES (?, ?)", (name, chain_id))
    _conn.execute("INSERT OR REPLACE INTO rpcs (name, rpc_url) VALUES (?, ?)", (name, "http://127.0.0.1:1"))
for chain_id, ticker, n in ((BASE, "usdc", 0x05DC), (BASE, "weth", 0x0E7E), (BSC, "usdc", 0x05DC),
                            (BSC, "weth", 0x0E7E), (BSC, "wbnb", 0x0B4B)):
    _conn.execute("INSERT OR REPLACE INTO supported_tokens (chain_id, ticker, address) VALUES (?, ?, ?)",
                  (chain_id, ticker, _token(n)))
_conn.commit()

# Every turn the assistant was asked for, as (user_id, chain_id, text, network).
turns: list[tuple] = []
reply_for_turns = {"text": "Done."}
# What the next turns quote, as {"action", "cost"}: each turn puts every one in the quote store under
# its own id, as the transaction tools would.
quotes_for_turns: list[dict] = []
_turn_ids = itertools.count(10_000)


def fake_chat(user_id, chain_id, text, network):
    turns.append((user_id, chain_id, text, network))
    turn_id = next(_turn_ids)
    for q in quotes_for_turns:
        quotes.put(user_id, chain_id, turn_id, q["action"], [], PRICED, q["cost"])
    return reply_for_turns["text"], turn_id


telebot.chat_turn = fake_chat

# The checks below send many messages from some accounts; the rationing has checks of its own ([11]).
ROOMY_GATE = TurnGate(rate="1000/minute")
telebot._turn_gate = ROOMY_GATE


def make_user(chat_id: int | None, wallet_chains=(SEPOLIA,), saved: str = "sepolia") -> int:
    """An account with wallets on `wallet_chains`, last deployed on `saved`, linked to `chat_id`."""
    user_id = db.create_user()
    for chain_id in wallet_chains:
        db.save_wallet_address(user_id, chain_id, WALLET)
    db.save_user_network(user_id, saved)
    if chat_id is not None:
        db.link_telegram(user_id, chat_id)
    return user_id


class FakeBot:
    """
    The bot as handlers and jobs see it. `errors` maps a chat to what sending to it raises. What it
    sends lands in `sent` as (chat, text), and the options it was sent with in `options`. The cards
    it took the buttons off land in `unbuttoned` as (chat, message id).
    """

    def __init__(self, errors: dict | None = None):
        self.errors = errors or {}
        self.sent: list[tuple[int, str]] = []
        self.options: list[dict] = []
        self.unbuttoned: list[tuple[int, int]] = []

    async def send_message(self, chat_id, text, **options):
        if chat_id in self.errors:
            raise self.errors[chat_id]
        self.sent.append((chat_id, text))
        self.options.append(options)
        return _message(chat_id)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **_):
        self.unbuttoned.append((chat_id, message_id))

    async def send_chat_action(self, chat_id, action):
        pass


# How each reply of the last run() was sent: the options handed to reply_text, in order. And the
# bot the last run() used, for what it did besides replying.
reply_options: list[dict] = []
last_bot: dict = {}


def _has_link_button(options: dict) -> bool:
    markup = options.get("reply_markup")
    return bool(markup) and any(b.url for row in markup.inline_keyboard for b in row)


def run(handler, chat_id: int, text: str = "", args: list | None = None, bot_data: dict | None = None,
        refuse_formatting: bool = False, refuse_links: bool = False):
    """
    Runs one handler for a message from `chat_id` to the bot, serving BOT_DATA's networks unless
    `bot_data` says otherwise.

    @param refuse_formatting  Telegram answers every formatted message with BadRequest, as it does
                              markup it can't parse.
    @param refuse_links       Telegram answers a message with a link button with BadRequest, as it
                              does a link a phone can't open (a web app on localhost).
    @return  (the replies, the one-off jobs it scheduled). Their options land in reply_options.
    """
    replies, jobs = [], []
    reply_options.clear()

    async def reply_text(reply, **options):
        if refuse_formatting and "parse_mode" in options:
            raise BadRequest("Can't parse entities: unsupported start tag")
        if refuse_links and _has_link_button(options):
            raise BadRequest("Wrong HTTP URL")
        replies.append(reply)
        reply_options.append(options)
        return _message(chat_id)

    update = SimpleNamespace(message=SimpleNamespace(chat_id=chat_id, text=text, reply_text=reply_text))
    last_bot["bot"] = FakeBot()
    context = SimpleNamespace(
        args=args or [],
        bot_data=bot_data or BOT_DATA,
        bot=last_bot["bot"],
        job_queue=SimpleNamespace(run_once=lambda callback, **kwargs: jobs.append((callback, kwargs))),
    )
    asyncio.run(handler(update, context))
    return replies, jobs


def tap(chat_id: int, data: str, chat_type: str = Chat.PRIVATE, from_id: int | None = None,
        bot_data: dict | None = None, no_chat: bool = False):
    """
    Taps a network button carrying `data`: in a private chat, by that chat's own user, unless told
    otherwise. `no_chat` is a tap on a message sent inline, which has no chat.

    @return  (how the query was answered, as [(text, show_alert)], the texts the message was edited
             to, what the bot sent).
    """
    answers, edits = [], []

    async def answer(text=None, show_alert=None, **_):
        answers.append((text, show_alert))

    async def edit_message_text(text, **_):
        edits.append(text)

    update = SimpleNamespace(
        callback_query=SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text),
        effective_chat=None if no_chat else SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=chat_id if from_id is None else from_id),
    )
    bot = FakeBot()
    asyncio.run(telebot.chain_chosen(update, SimpleNamespace(bot_data=bot_data or BOT_DATA, bot=bot)))
    return answers, edits, bot.sent


def buttons(options: dict) -> list[tuple[str, str]]:
    """A reply's buttons, as (label, data) -- (label, url) for a link -- or [] when it has none."""
    markup = options.get("reply_markup")
    return [(b.text, b.callback_data or b.url) for row in markup.inline_keyboard for b in row] if markup else []


def test_each_account_acts_on_its_network():
    print("\n[1] a message acts on the network the account picked, not the one it last deployed on")
    user = make_user(1001, wallet_chains=(SEPOLIA, BASE), saved="sepolia")

    turns.clear()
    replies, _ = run(telebot.start_chat, 1001, "what's in my wallet?")
    check("with two networks and none picked, nothing runs", turns == [], str(turns))
    check("the user is asked which, and to send the message again",
          len(replies) == 1 and replies[0] == "Which network should I use? Pick one, then send your message again.",
          str(replies))
    check("with a button for each network", buttons(reply_options[0])
          == [("Sepolia", f"chain:{SEPOLIA}"), ("Base", f"chain:{BASE}")], str(buttons(reply_options[0])))
    check("and nothing is picked for them", db.get_telegram_chain(user) is None)

    answers, edits, _ = tap(1001, f"chain:{BASE}")
    check("tapping Base picks it", db.get_telegram_chain(user) == BASE)
    check("and says so", answers == [("You're on Base now.", None)]
          and len(edits) == 1 and edits[0].startswith("You're on Base now"), f"{answers} {edits}")

    replies, _ = run(telebot.start_chat, 1001, "what's in my wallet?")
    check("the turn runs on Base", turns == [(user, BASE, "what's in my wallet?", "base")], str(turns))
    check("and the reply is headed by the network's name", replies == ["<b>Base</b>\n\nDone."], str(replies))

    tap(1001, f"chain:{SEPOLIA}")
    turns.clear()
    replies, _ = run(telebot.start_chat, 1001, "and here?")
    check("after switching, it runs on Sepolia", turns == [(user, SEPOLIA, "and here?", "sepolia")], str(turns))
    check("and the reply says Sepolia", replies == ["<b>Sepolia</b>\n\nDone."], str(replies))

    only = make_user(1002, wallet_chains=(SEPOLIA,), saved="sepolia")
    turns.clear()
    replies, _ = run(telebot.start_chat, 1002, "hi")
    check("with one network, it is picked without asking", turns == [(only, SEPOLIA, "hi", "sepolia")]
          and replies == ["<b>Sepolia</b>\n\nDone."], f"{turns} {replies}")
    check("and remembered", db.get_telegram_chain(only) == SEPOLIA)

    make_user(1003, wallet_chains=(CELO,), saved="celo")
    turns.clear()
    replies, _ = run(telebot.start_chat, 1003, "hi")
    check("with wallets only where the bot doesn't serve, nothing runs", turns == [], str(turns))
    check("and the user is told where to set one up", replies == [telebot.NO_SERVED_WALLET_MESSAGE], str(replies))

    replies, _ = run(telebot.start_chat, 1009, "hi")
    check("an unlinked chat is refused", replies == [telebot.UNLINKED_MESSAGE] and turns == [], str(replies))


def test_a_lost_network_is_not_replaced():
    print("\n[1b] a picked network the bot no longer serves is not swapped for another without asking")
    user = make_user(1006, wallet_chains=(SEPOLIA, BASE))
    tap(1006, f"chain:{BASE}")
    base_down = {"networks": {SEPOLIA: "sepolia", BSC: "bsc"}}

    turns.clear()
    replies, _ = run(telebot.start_chat, 1006, "send 1 ETH to Sam", bot_data=base_down)
    check("nothing runs, though only one network is left", turns == [], str(turns))
    check("the user is told why they're asked",
          replies == ["Base isn't available right now. Which network should I use? Pick one, then send your "
                      "message again."], str(replies))
    check("and offered what is left", buttons(reply_options[0]) == [("Sepolia", f"chain:{SEPOLIA}")],
          str(buttons(reply_options[0])))
    check("the pick is kept", db.get_telegram_chain(user) == BASE)

    replies, _ = run(telebot.chain_cmd, 1006, bot_data=base_down)
    check("/chain says the same", replies == ["Base isn't available right now. Which network should I use?"]
          and buttons(reply_options[0]) == [("Sepolia", f"chain:{SEPOLIA}")], str(replies))

    run(telebot.start_chat, 1006, "send 1 ETH to Sam")
    check("once Base is back, the message runs there", turns == [(user, BASE, "send 1 ETH to Sam", "base")],
          str(turns))


def test_start_and_help():
    print("\n[2] one link is enough; /start and /help name the network, or ask for one")
    user = make_user(None, wallet_chains=(SEPOLIA, BASE))
    db.save_telegram_link_nonce("nonce-1004", user, int(time.time()) + 600)

    replies, jobs = run(telebot.start, 1004, args=["nonce-1004"])
    check("/start with the link links the chat", db.get_user_id_by_telegram_chat_id(1004) == user)
    check("and says so, pointing at /chain",
          len(replies) == 2 and replies[0].startswith("✅ Telegram linked") and "/chain" in replies[0], str(replies))
    check("no network picked yet: it asks which",
          replies[1] == "Welcome to Mitfah. Which network should I use? You can switch any time with /chain."
          and buttons(reply_options[1]) == [("Sepolia", f"chain:{SEPOLIA}"), ("Base", f"chain:{BASE}")],
          str(replies))
    check("and looks at the wallets straight away",
          [(callback, kwargs["chat_id"], kwargs["data"]) for callback, kwargs in jobs]
          == [(telebot.first_check, 1004, user)], str(jobs))
    check("nothing says each network has a bot of its own",
          not any("of its own" in r or "own bot" in r or "each network" in r.lower() for r in replies), str(replies))

    tap(1004, f"chain:{BASE}")
    replies, jobs = run(telebot.start, 1004)
    check("once picked, /start welcomes them on it",
          len(replies) == 1 and replies[0].startswith("Welcome to Mitfah. You're on Base: ask me about your Base wallet")
          and "/chain to switch networks" in replies[0] and buttons(reply_options[0]) == [], str(replies))
    check("and looks at the wallets again", len(jobs) == 1, str(jobs))

    only = make_user(1007, wallet_chains=(BSC,))
    replies, _ = run(telebot.start, 1007)
    check("with one network, /start puts them on it", replies[0].startswith("Welcome to Mitfah. You're on BNB Smart Chain")
          and db.get_telegram_chain(only) == BSC, str(replies))

    make_user(1008, wallet_chains=(CELO,))
    replies, jobs = run(telebot.start, 1008)
    check("with no wallet the bot serves, /start says so", replies == [telebot.NO_SERVED_WALLET_MESSAGE], str(replies))
    check("and schedules no check", jobs == [], str(jobs))

    replies, _ = run(telebot.start, 1005)
    check("an unlinked chat is told how to link", replies == [telebot.UNLINKED_MESSAGE], str(replies))

    replies, _ = run(telebot.help_cmd, 1004)
    check("/help names the network in use and how to switch",
          len(replies) == 1 and "I look after your Base wallet." in replies[0] and "Send 0.01 ETH to Sam" in replies[0]
          and "/chain switches" in replies[0], str(replies))
    check("and lists every command, with Base's examples",
          all(f"/{name}" in replies[0] for name, _, _ in telebot.COMMANDS if name not in ("start", "help", "chain"))
          and "/send 0.01 ETH to Sam" in replies[0] and "/swap 0.01 ETH for USDC" in replies[0], str(replies))
    replies, _ = run(telebot.help_cmd, 1007)
    check("its examples use that network's coin", "Send 0.01 BNB to Sam" in replies[0]
          and "/send 0.01 BNB to Sam" in replies[0], str(replies))
    check("and on BSC, which has no Uniswap positions, /positions isn't offered", "/positions" not in replies[0],
          str(replies))
    replies, _ = run(telebot.help_cmd, 1005)
    check("unlinked, or with no network yet, it says how to pick one",
          "I look after your wallets." in replies[0] and "/chain picks it" in replies[0], str(replies))
    check("and never mentions a bot per network",
          not any("own bot" in r or "each network" in r.lower() for r in replies), str(replies))


def test_chain_command():
    print("\n[2b] /chain offers the networks the account has a wallet on that the bot is serving")
    user = make_user(1010, wallet_chains=(SEPOLIA, BSC, BASE, CELO))
    bsc_down = {"networks": {SEPOLIA: "sepolia", ARBITRUM: "arbitrum", BASE: "base"}}

    replies, _ = run(telebot.chain_cmd, 1010, bot_data=bsc_down)
    check("only live networks with a wallet, in a fixed order",
          replies == ["Which network should I use?"]
          and buttons(reply_options[0]) == [("Sepolia", f"chain:{SEPOLIA}"), ("Base", f"chain:{BASE}")],
          f"{replies} {buttons(reply_options[0])}")

    tap(1010, f"chain:{BASE}", bot_data=bsc_down)
    replies, _ = run(telebot.chain_cmd, 1010, bot_data=bsc_down)
    check("the one in use has a ✓", replies == ["You're on Base. Tap a network to switch."]
          and buttons(reply_options[0]) == [("Sepolia", f"chain:{SEPOLIA}"), ("✓ Base", f"chain:{BASE}")],
          f"{replies} {buttons(reply_options[0])}")
    check("as the account's own choice", db.get_telegram_chain(user) == BASE)

    make_user(1011, wallet_chains=(SEPOLIA,))
    replies, _ = run(telebot.chain_cmd, 1011)
    check("with one network, it is already picked",
          replies == ["You're on Sepolia. Tap a network to switch."]
          and buttons(reply_options[0]) == [("✓ Sepolia", f"chain:{SEPOLIA}")], str(replies))

    make_user(1012, wallet_chains=(CELO,))
    replies, _ = run(telebot.chain_cmd, 1012)
    check("with none, it says so", replies == [telebot.NO_SERVED_WALLET_MESSAGE] and buttons(reply_options[0]) == [],
          str(replies))
    replies, _ = run(telebot.chain_cmd, 1019)
    check("an unlinked chat is refused", replies == [telebot.UNLINKED_MESSAGE], str(replies))


def test_taps_are_checked():
    print("\n[2c] a tap counts only in the account's own private chat, for a network it can use now")
    user = make_user(1020, wallet_chains=(SEPOLIA, BASE))
    refused = [("That network isn't available. Send /chain to see yours.", True)]

    for data, why in ((f"chain:{BSC}", "a live network it has no wallet on"),
                      (f"chain:{MAINNET}", "a network the bot isn't serving"),
                      (f"chain:{CELO}", "a network the bot never serves"),
                      ("chain:8453x", "data that isn't a network"), ("chain:", "no network at all")):
        answers, edits, _ = tap(1020, data)
        check(f"forged {data!r} ({why}) is refused",
              answers == refused and edits == [] and db.get_telegram_chain(user) is None, f"{answers} {edits}")

    for chat_type in (Chat.GROUP, Chat.SUPERGROUP, Chat.CHANNEL):
        answers, edits, sent = tap(1020, f"chain:{BASE}", chat_type=chat_type)
        check(f"a tap in a {chat_type} is answered with nothing, and changes nothing",
              answers == [(None, None)] and edits == [] and sent == [] and db.get_telegram_chain(user) is None,
              f"{answers} {edits} {sent}")
    answers, edits, sent = tap(1020, f"chain:{BASE}", from_id=1021)
    check("so is one in the chat by someone else",
          answers == [(None, None)] and edits == [] and db.get_telegram_chain(user) is None, f"{answers} {edits}")
    answers, edits, sent = tap(1020, f"chain:{BASE}", no_chat=True)
    check("and one on a message with no chat",
          answers == [(None, None)] and edits == [] and db.get_telegram_chain(user) is None, f"{answers} {edits}")

    answers, edits, sent = tap(1029, f"chain:{BASE}")
    check("an unlinked chat's tap is answered, and the chat told how to link",
          answers == [(None, None)] and edits == [] and sent == [(1029, telebot.UNLINKED_MESSAGE)], f"{answers} {sent}")

    answers, edits, _ = tap(1020, f"chain:{BASE}")
    check("the real thing counts", db.get_telegram_chain(user) == BASE and answers == [("You're on Base now.", None)]
          and edits == ["You're on Base now: what you send acts on your Base wallet until you switch with /chain."],
          f"{answers} {edits}")


def telegram_update(chat_type: str, edited: bool = False, text: str = "hello") -> Update:
    entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0]))] if text.startswith("/") else None
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=5, type=chat_type), text=text, entities=entities
    )
    return Update(update_id=1, edited_message=message) if edited else Update(update_id=1, message=message)


def button_update(data: str, chat_type: str = Chat.PRIVATE, from_id: int = 5) -> Update:
    """A real tap on a button carrying `data`, under a message in chat 5."""
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=5, type=chat_type), text="Which network?"
    )
    query = CallbackQuery(id="1", from_user=User(id=from_id, first_name="Sam", is_bot=False), chat_instance="1",
                          data=data, message=message)
    return Update(update_id=1, callback_query=query)


def test_private_chats_only():
    print("\n[3] only new messages, and taps, in private chats are answered")
    bot = telebot.build_bot("123:test-token", {BASE: "base"})
    check("the bot carries the networks it serves", bot.bot_data == {"networks": {BASE: "base"}}, str(bot.bot_data))
    check("it has a daily check", len(bot.job_queue.get_jobs_by_name("daily-check")) == 1)

    handlers = bot.handlers[0]
    commands = {name for h in handlers for name in getattr(h, "commands", ())}
    check("every command has a handler", commands == {name for name, _, _ in telebot.COMMANDS}, str(sorted(commands)))
    check("plus two kinds of button, ordinary messages, and commands it doesn't have",
          len(handlers) == len(telebot.COMMANDS) + 4, str(len(handlers)))
    check("a command it doesn't have is answered last, after every real one",
          handlers[-1].callback is telebot.unknown_cmd and handlers[-2].callback is telebot.start_chat)
    for handler in handlers:
        if isinstance(handler, CallbackQueryHandler):
            continue
        commands = getattr(handler, "commands", None)
        name = f"/{min(commands)}" if commands else handler.callback.__name__
        # The unknown-command handler takes commands only; every other filter is what decides here.
        text = "/nope" if handler.callback is telebot.unknown_cmd else "hello"
        check(f"{name}: a private chat is answered",
              bool(handler.filters.check_update(telegram_update(Chat.PRIVATE, text=text))))
        for chat_type in (Chat.GROUP, Chat.SUPERGROUP, Chat.CHANNEL):
            check(f"{name}: a {chat_type} is not",
                  not handler.filters.check_update(telegram_update(chat_type, text=text)))
        check(f"{name}: nor an edited message",
              not handler.filters.check_update(telegram_update(Chat.PRIVATE, edited=True, text=text)))
    check("an ordinary message isn't taken for a command",
          not handlers[-1].filters.check_update(telegram_update(Chat.PRIVATE)))

    taps = [h for h in handlers if isinstance(h, CallbackQueryHandler)]
    check("network taps go to chain_chosen, quote taps to quote_button",
          [t.callback for t in taps] == [telebot.chain_chosen, telebot.quote_button], str(taps))
    check("a network button's tap is taken by the first", bool(taps[0].check_update(button_update(f"chain:{BASE}")))
          and not taps[1].check_update(button_update(f"chain:{BASE}")))
    check("a quote button's by the second", bool(taps[1].check_update(button_update("q:ok:8453:abc")))
          and not taps[0].check_update(button_update("q:ok:8453:abc")))
    # The handler can't filter by chat, so chain_chosen checks by hand ([2c]); here, on real updates.
    check("a real tap in a private chat is from that chat", telebot._private_chat_id(button_update("chain:1")) == 5)
    for chat_type in (Chat.GROUP, Chat.SUPERGROUP, Chat.CHANNEL):
        check(f"a real tap in a {chat_type} is not taken",
              telebot._private_chat_id(button_update("chain:1", chat_type=chat_type)) is None)
    check("nor one by someone else", telebot._private_chat_id(button_update("chain:1", from_id=6)) is None)


def test_replies_fit_telegram():
    print("\n[4] long replies are split to fit Telegram")
    limit = telegram_format.MAX_MESSAGE_CHARS
    text = "\n".join(f"line {i}: " + "x" * 60 for i in range(200))   # ~14k characters
    parts = telegram_format.split_message(text)
    check("a long reply becomes several messages", len(parts) > 1, str(len(parts)))
    check("each fits", all(len(part) <= limit for part in parts), str([len(p) for p in parts]))
    check("cut at line breaks, nothing lost", "\n".join(parts) == text)

    parts = telegram_format.split_message("y" * (limit * 2 + 5))
    check("a reply without line breaks is cut at the limit",
          [len(p) for p in parts] == [limit, limit, 5], str([len(p) for p in parts]))
    check("an empty reply is no messages", telegram_format.split_message("  \n") == [])

    # Messages break between blocks, never inside one, so no formatting is cut in half.
    long_reply = "\n\n".join(f"**Paragraph {i}**: " + "word " * 150 for i in range(20))   # ~16k characters
    messages = telegram_format.to_telegram_html(long_reply)
    check("a long formatted reply becomes several messages", len(messages) > 1, str(len(messages)))
    check("each fits", all(len(telegram_format.plain_text(m)) <= limit for m in messages),
          str([len(telegram_format.plain_text(m)) for m in messages]))
    check("each opens and closes its own formatting", all(m.count("<b>") == m.count("</b>") for m in messages))
    check("nothing is lost", sum(m.count("word") for m in messages) == 20 * 150)
    messages = telegram_format.to_telegram_html("**" + "y " * 4_500 + "**")
    check("one block too long for a message goes out plain, split",
          len(messages) == 3 and not any("<b>" in m for m in messages)
          and all(len(telegram_format.plain_text(m)) <= limit for m in messages), str([len(m) for m in messages]))

    make_user(1101, wallet_chains=(BASE,))
    reply_for_turns["text"] = long_reply
    replies, _ = run(telebot.start_chat, 1101, "tell me everything")
    check("the network's name heads the first message only",
          len(replies) > 1 and replies[0].startswith("<b>Base</b>\n\n<b>Paragraph 0</b>")
          and not any("<b>Base</b>" in r for r in replies[1:]), str([r[:30] for r in replies]))
    check("and every message still fits", all(len(telegram_format.plain_text(r)) <= limit for r in replies),
          str([len(telegram_format.plain_text(r)) for r in replies]))
    check("with nothing lost", sum(r.count("word") for r in replies) == 20 * 150)

    reply_for_turns["text"] = text
    replies, _ = run(telebot.start_chat, 1101, "list everything")
    check("a first block too long to share a message leaves the name a message of its own",
          replies[0] == "<b>Base</b>" and "\n".join(replies[1:]) == text and len(replies) > 2, str(len(replies)))
    reply_for_turns["text"] = ""
    replies, _ = run(telebot.start_chat, 1101, "say nothing")
    check("an empty answer still gets a reply", replies == [f"<b>Base</b>\n\n{telebot.EMPTY_REPLY}"], str(replies))
    reply_for_turns["text"] = "Done."


def test_replies_keep_their_formatting():
    print("\n[4b] the assistant's Markdown becomes Telegram formatting, under the web app's rules")

    def as_html(markdown):
        return "\n=\n".join(telegram_format.to_telegram_html(markdown))

    for label, markdown, expected in (
        ("bold, italic, code and strikethrough", "**a** *b* `c` ~~d~~", "<b>a</b> <i>b</i> <code>c</code> <s>d</s>"),
        ("a heading is bold", "## Quote", "<b>Quote</b>"),
        ("paragraphs stand apart", "one\n\ntwo", "one\n\ntwo"),
        ("a web link stays a link", "[Etherscan](https://etherscan.io/tx/0x1)",
         '<a href="https://etherscan.io/tx/0x1">Etherscan</a>'),
        ("a link of another scheme keeps only its words", "[file](ftp://example.com/f)", "file"),
        ("code inside a link stays plain, as Telegram requires", "[`0xabc`](https://x.io)",
         '<a href="https://x.io">0xabc</a>'),
        ("raw HTML stays text", "<b>hi</b> <script>x</script>", "&lt;b&gt;hi&lt;/b&gt; &lt;script&gt;x&lt;/script&gt;"),
        ("an image is never loaded: its alt text shows", "![chart](https://evil.example/p.png)", "[image: chart]"),
        ("special characters are escaped", "a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("a name with underscores stays as it is", "send to snake_case_name", "send to snake_case_name"),
        ("a two-column table: a 'label: value' line per row",
         "| Field | Value |\n| --- | --- |\n| Amount | 20 USDC |\n| **Fee** | $0.42 |",
         "<b>Amount</b>: 20 USDC\n<b>Fee</b>: $0.42"),
        ("a wider table labels each cell with its column",
         "| Token | Balance | Value |\n| --- | ---: | ---: |\n| USDC | 25 | $25 |",
         "<b>USDC</b> — Balance: 25 · Value: $25"),
        ("lists keep their markers; a nested one is indented", "3. one\n4. two\n   - nested",
         "3. one\n4. two\n   • nested"),
        ("a quote", "> careful", "<blockquote>careful</blockquote>"),
        ("a code block keeps its text, escaped", "```\nx = 1 < 2\n```", "<pre>x = 1 &lt; 2</pre>"),
    ):
        got = as_html(markdown)
        check(label, got == expected, repr(got))
    check("a javascript: link never becomes one", "href" not in as_html("[x](javascript:alert(1))"))

    # Whatever block a reply opens with, the network's name above it leaves it as it would be alone.
    for shape, markdown in (("a paragraph", "Done."), ("a heading", "## Quote\n\nSend 1 ETH"),
                            ("a table", "| Field | Value |\n| --- | --- |\n| Amount | 20 USDC |"),
                            ("a list", "3. one\n4. two\n   - nested"), ("a rule", "---\nafter"),
                            ("a code block", "```\nx = 1\n```"), ("a quote", "> careful")):
        alone = telegram_format.to_telegram_html(markdown)
        headed = telegram_format.to_telegram_html(f"**Base**\n\n{markdown}")
        check(f"a reply opening with {shape} keeps its shape under the name",
              headed == [f"<b>Base</b>\n\n{alone[0]}", *alone[1:]], f"{headed} {alone}")

    make_user(1102, wallet_chains=(BASE,))
    reply_for_turns["text"] = "**Done.** See [the tx](https://etherscan.io/tx/0x1)."
    replies, _ = run(telebot.start_chat, 1102, "send it")
    check("the bot sends the reply formatted, under the network's name",
          replies == ['<b>Base</b>\n\n<b>Done.</b> See <a href="https://etherscan.io/tx/0x1">the tx</a>.'], str(replies))
    check("as HTML, with no link preview fetched",
          reply_options == [{"parse_mode": ParseMode.HTML, "link_preview_options": telebot.NO_PREVIEW}]
          and telebot.NO_PREVIEW.is_disabled, str(reply_options))

    replies, _ = run(telebot.start_chat, 1102, "send it", refuse_formatting=True)
    check("if Telegram refuses the formatting, the same words go out plain",
          replies == ["Base\n\nDone. See the tx."] and "parse_mode" not in reply_options[0], f"{replies} {reply_options}")
    reply_for_turns["text"] = "Done."


def test_wallet_warnings():
    print("\n[5] the warnings say what needs doing")
    now = 1_000_000
    healthy = {"session_active": True, "session_expires_at": now + 30 * 86_400,
               "daily_limit_usd": 100.0, "remaining_usd": 80.0}

    check("all is well: nothing to say", telebot.wallet_warnings(healthy, "Base", now) == [])

    warnings = telebot.wallet_warnings({**healthy, "session_active": False}, "Base", now)
    check("no device approved: one warning, naming the chain and where to fix it",
          len(warnings) == 1 and "None of your devices is approved on your Base wallet" in warnings[0]
          and "Controls → Devices" in warnings[0],
          str(warnings))

    for left, when in ((2 * 86_400 + 3_600, "in 2 days"), (86_400 + 60, "in 1 day"), (3_600, "in under a day")):
        warnings = telebot.wallet_warnings({**healthy, "session_expires_at": now + left}, "Base", now)
        check(f"a key running out {when} is mentioned",
              len(warnings) == 1 and f"runs out {when}." in warnings[0], str(warnings))

    warnings = telebot.wallet_warnings({**healthy, "remaining_usd": 5.0}, "Base", now)
    check("a low budget is mentioned, with the figures",
          len(warnings) == 1 and "$5.00 of your $100.00 limit" in warnings[0], str(warnings))


def _position(**overrides) -> dict:
    """A row as positions.read_positions returns it: ETH/USDC, 2,850-3,150 USDC per ETH, in range."""
    share = [{"ticker": "eth", "decimals": 18, "raw": "1"}, {"ticker": "usdc", "decimals": 6, "raw": "1"}]
    return {"id": 7, "pair": "eth/usdc", "base": "eth", "quote": "usdc", "fee_pct": 0.05,
            "min_price": 2850.0, "max_price": 3150.0, "full_range": False, "current_price": 3000.0,
            "in_range": True, "amounts": share, "fees": share} | overrides


OUT_OF_RANGE_ABOVE = (
    "⚠️ Your ETH/USDC position on Base (2,850–3,150 USDC per ETH) is out of range: ETH is at 3,420 USDC, "
    "so it isn't earning fees. Ask me to move it or take it out."
)


def test_position_warnings():
    print("\n[5b] a position out of range is mentioned, with its range and where the price is")
    check("no positions: nothing to say", telebot.position_warnings([], "Base") == [])
    check("in range: nothing to say", telebot.position_warnings([_position()], "Base") == [])

    warnings = telebot.position_warnings([_position(current_price=3420.0, in_range=False)], "Base")
    check("above its range: one warning, saying what to do", warnings == [OUT_OF_RANGE_ABOVE], str(warnings))
    warnings = telebot.position_warnings([_position(current_price=2400.5, in_range=False)], "Base")
    check("below its range: the same, with the price below",
          len(warnings) == 1 and "(2,850–3,150 USDC per ETH) is out of range: ETH is at 2,400.5 USDC" in warnings[0],
          str(warnings))

    full = _position(min_price=0.0, max_price=None, full_range=True, in_range=False)
    check("a full-range position is never out of range", telebot.position_warnings([full], "Base") == [])

    small = _position(pair="pepe/eth", base="pepe", quote="eth", min_price=0.00000041231, max_price=0.0000005,
                      current_price=0.00000061234, in_range=False)
    warnings = telebot.position_warnings([small], "Base")
    check("small prices keep five significant figures and never an exponent",
          len(warnings) == 1 and "Your PEPE/ETH position on Base (0.00000041231–0.0000005 ETH per PEPE)" in warnings[0]
          and "PEPE is at 0.00000061234 ETH" in warnings[0], str(warnings))
    large = _position(pair="wbtc/usdc", base="wbtc", quote="usdc", min_price=95_000.25, max_price=120_000.4,
                      current_price=131_234.4, in_range=False)
    warnings = telebot.position_warnings([large], "Base")
    check("from 100,000 up, whole numbers; below it, five figures",
          len(warnings) == 1 and "(95,000–120,000 USDC per WBTC)" in warnings[0]
          and "WBTC is at 131,234 USDC" in warnings[0], str(warnings))
    stable = _position(pair="usdc/usdt", base="usdc", quote="usdt", min_price=0.995, max_price=1.005,
                       current_price=1.0112, in_range=False)
    warnings = telebot.position_warnings([stable], "Base")
    check("a stablecoin range keeps its two ends apart",
          len(warnings) == 1 and "(0.995–1.005 USDT per USDC)" in warnings[0] and "USDC is at 1.0112 USDT" in warnings[0],
          str(warnings))

    mixed = [_position(id=1), _position(id=2, current_price=3420.0, in_range=False), full,
             _position(id=3, current_price=2400.5, in_range=False)]
    warnings = telebot.position_warnings(mixed, "Base")
    check("one warning per position out of range, in order",
          len(warnings) == 2 and "3,420" in warnings[0] and "2,400.5" in warnings[1], str(warnings))


def test_position_check():
    print("\n[6b] the check mentions positions out of range on a Uniswap V3 chain, and only there")
    now = int(time.time())
    fine = {"session_active": True, "session_expires_at": now + 30 * 86_400,
            "daily_limit_usd": 100.0, "remaining_usd": 90.0}
    low = {**fine, "remaining_usd": 1.0}
    out = [_position(current_price=3420.0, in_range=False), _position(id=8)]
    user = make_user(4001, wallet_chains=(BASE, BSC))
    answers = {"status": fine, "positions": out}
    reads = []
    original = telebot._read_wallet_status, telebot._read_positions
    telebot._read_wallet_status = lambda user_id, chain_id, network: answers["status"]

    def fake_positions(user_id, chain_id, network):
        reads.append((user_id, chain_id, network))
        return answers["positions"]

    telebot._read_positions = fake_positions
    try:
        bot = FakeBot()
        asyncio.run(telebot._warn(bot, 4001, user, BASE, "base"))
        check("on Base the positions are read, on Base's network", reads == [(user, BASE, "base")], str(reads))
        check("and the one out of range is mentioned", bot.sent == [(4001, OUT_OF_RANGE_ABOVE)], str(bot.sent))

        reads.clear()
        bot = FakeBot()
        asyncio.run(telebot._warn(bot, 4001, user, BSC, "bsc"))
        check("on BSC (PancakeSwap V2) no positions are read", reads == [], str(reads))
        check("and nothing is said about them", bot.sent == [], str(bot.sent))

        answers["status"] = low
        bot = FakeBot()
        asyncio.run(telebot._warn(bot, 4001, user, BASE, "base"))
        check("with the wallet's own warnings first",
              [text.split(":")[0] for _, text in bot.sent]
              == ["⚠️ Low spending budget on Base", "⚠️ Your ETH/USDC position on Base (2,850–3,150 USDC per ETH) is out of range"],
              str(bot.sent))

        answers["status"] = None
        bot = FakeBot()
        asyncio.run(telebot._warn(bot, 4001, user, BASE, "base"))
        check("a wallet that can't be read still gets its position warning", bot.sent == [(4001, OUT_OF_RANGE_ABOVE)],
              str(bot.sent))

        reads.clear()
        bot = FakeBot()
        job = SimpleNamespace(chat_id=4001, data=user)
        answers["status"] = fine
        asyncio.run(telebot.first_check(SimpleNamespace(bot_data=BOT_DATA, bot=bot, job=job)))
        check("the check after /start covers both networks, reading positions only on Base",
              reads == [(user, BASE, "base")], str(reads))
        check("and mentions the one out of range", bot.sent == [(4001, OUT_OF_RANGE_ABOVE)], str(bot.sent))
    finally:
        telebot._read_wallet_status, telebot._read_positions = original

    # The real positions read: on the chain asked for, and a failure leaves the wallet's warnings alone.
    seen = []
    original_read = telebot.read_positions
    try:
        telebot.read_positions = lambda user_id, chain_id: seen.append((db.get_user_network(user_id), chain_id)) or out
        check("the positions are read on the chain asked for", telebot._read_positions(user, BASE, "base") == out
              and seen == [("base", BASE)], str(seen))

        def broken(user_id, chain_id):
            raise RuntimeError("node down")

        telebot.read_positions = broken
        check("a failed positions read says so", telebot._read_positions(user, BASE, "base") is None)
        original_status = telebot._read_wallet_status
        telebot._read_wallet_status = lambda user_id, chain_id, network: low
        try:
            bot = FakeBot()
            asyncio.run(telebot._warn(bot, 4001, user, BASE, "base"))
            check("and doesn't stop the wallet's own warnings",
                  len(bot.sent) == 1 and "Low spending budget on Base" in bot.sent[0][1], str(bot.sent))
        finally:
            telebot._read_wallet_status = original_status
    finally:
        telebot.read_positions = original_read


def test_daily_check():
    print("\n[6] the daily check reaches each linked chat with a wallet on a network the bot serves")
    low = {"session_active": True, "session_expires_at": int(time.time()) + 30 * 86_400,
           "daily_limit_usd": 100.0, "remaining_usd": 1.0}
    fine = {**low, "remaining_usd": 90.0}
    # No other check makes an account with a wallet on Arbitrum or Ethereum, so these are all of them.
    networks = {"networks": {ARBITRUM: "arbitrum", MAINNET: "mainnet"}}

    gone = make_user(2001, wallet_chains=(ARBITRUM,))     # blocked the bot
    flaky = make_user(2002, wallet_chains=(ARBITRUM,))    # Telegram times out
    low_user = make_user(2003, wallet_chains=(ARBITRUM,))
    fine_user = make_user(2004, wallet_chains=(ARBITRUM,))
    make_user(2005, wallet_chains=(SEPOLIA,))            # a network this check doesn't serve
    make_user(None, wallet_chains=(ARBITRUM,))           # not linked
    both = make_user(2006, wallet_chains=(ARBITRUM, MAINNET))

    check("Arbitrum's list is the linked chats with a wallet there",
          db.get_telegram_chats_on_chain(ARBITRUM)
          == [(gone, 2001), (flaky, 2002), (low_user, 2003), (fine_user, 2004), (both, 2006)],
          str(db.get_telegram_chats_on_chain(ARBITRUM)))

    read = []
    original = telebot._read_wallet_status

    def fake_status(user_id, chain_id, network):
        read.append((user_id, chain_id, network))
        return fine if user_id == fine_user else low

    telebot._read_wallet_status = fake_status
    # Arbitrum and Ethereum trade on Uniswap V3, so the check reads positions too: none here (see [6b]).
    original_positions = telebot._read_positions
    telebot._read_positions = lambda user_id, chain_id, network: []
    try:
        bot = FakeBot(errors={2001: Forbidden("bot was blocked by the user"), 2002: TimedOut()})
        asyncio.run(telebot.daily_check(SimpleNamespace(bot_data=networks, bot=bot)))
        check("every one of them is read, network by network, each on its own network",
              read == [(u, ARBITRUM, "arbitrum") for u in (gone, flaky, low_user, fine_user, both)]
              + [(both, MAINNET, "mainnet")], str(read))
        check("a blocked or unreachable chat doesn't stop the others; only low budgets are warned",
              [chat for chat, _ in bot.sent] == [2003, 2006, 2006], str(bot.sent))
        check("each warning names its network",
              "on Arbitrum One" in bot.sent[1][1] and "on Ethereum" in bot.sent[2][1], str(bot.sent))

        bot = FakeBot()
        read.clear()
        job = SimpleNamespace(chat_id=2006, data=both)
        asyncio.run(telebot.first_check(SimpleNamespace(bot_data=networks, bot=bot, job=job)))
        check("the check after /start covers every network the user can use, in /chain's order",
              read == [(both, MAINNET, "mainnet"), (both, ARBITRUM, "arbitrum")]
              and [chat for chat, _ in bot.sent] == [2006, 2006], f"{read} {bot.sent}")
    finally:
        telebot._read_wallet_status = original
        telebot._read_positions = original_positions

    # The real read makes the chain the acting network, whatever the user last deployed on.
    seen = []
    original = telebot._get_wallet_status
    telebot._get_wallet_status = lambda user_id: seen.append(db.get_user_network(user_id)) or fine
    try:
        status = telebot._read_wallet_status(low_user, BASE, "base")
        check("the wallet is read on the chain asked for", seen == ["base"] and status == fine, str(seen))
    finally:
        telebot._get_wallet_status = original


LEGACY_VARS = [f"MITFAH_{stem}_{kind}" for stem in ("ETH", "SEPOLIA", "BSC", "ARB", "BASE")
               for kind in ("API", "USERNAME")]


def _exit_reason(call) -> str | None:
    """What `call` exits the process with, or None if it doesn't. Its prints go nowhere."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            call()
    except SystemExit as e:
        return str(e)
    return None


def test_network_check():
    print("\n[7] the bot starts only on an up-to-date database, with its token, on networks that answer")
    original = telebot.load_network_config_by_name

    def network_answering(answer):
        def load(name):
            if isinstance(answer, Exception):
                class Eth:
                    @property
                    def chain_id(self):
                        raise answer
                return SimpleNamespace(eth=Eth()), BASE
            return SimpleNamespace(eth=SimpleNamespace(chain_id=answer)), BASE
        return load

    try:
        telebot.load_network_config_by_name = network_answering(BASE)
        check("the right chain: it is served", telebot.check_network(BASE, "base") is None)

        telebot.load_network_config_by_name = network_answering(1)
        reason = telebot.check_network(BASE, "base")
        check("another chain: it is not", reason == "the RPC for base is chain 1, not 8453", str(reason))

        telebot.load_network_config_by_name = network_answering(
            ConnectionError("https://base-mainnet.example/v2/SECRET-KEY refused"))
        reason = telebot.check_network(BASE, "base")
        check("no answer: it is not, and the RPC's URL stays out of the reason",
              reason == "can't reach base (ConnectionError)", str(reason))

        def missing(name):
            raise ValueError(f"Chain name '{name}' not found in database")

        telebot.load_network_config_by_name = missing
        reason = telebot.check_network(BASE, "base-fork")
        check("a network the database lacks: it is not", reason == "Chain name 'base-fork' not found in database",
              str(reason))
    finally:
        telebot.load_network_config_by_name = original

    # main() itself, up to the point where it would start polling. load_dotenv has filled the
    # environment from the real .env, so every bot variable is set here by hand.
    names = ["MITFAH_API", "MITFAH_USERNAME", *LEGACY_VARS]
    saved_env = {name: os.environ.get(name) for name in names}
    original_check, original_serve = telebot.check_network, telebot.serve
    served = []

    async def fake_serve(bot):
        served.append(bot)

    conn = db.get_db()
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["MITFAH_BASE_API"] = "123:base"
        os.environ["MITFAH_BASE_USERNAME"] = "mitfah_base_bot"

        conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) - 1}")
        reason = _exit_reason(telebot.main)
        check("an out-of-date database: it stops and says to run make db",
              reason is not None and "make db" in reason, str(reason))
        conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS)}")

        reason = _exit_reason(telebot.main)
        check("no MITFAH_API but the old per-network variables: it names what to change",
              reason is not None and "MITFAH_API is not set" in reason and "MITFAH_USERNAME" in reason
              and "delete MITFAH_BASE_API, MITFAH_BASE_USERNAME from .env" in reason, str(reason))
        for name in LEGACY_VARS:
            os.environ.pop(name, None)
        reason = _exit_reason(telebot.main)
        check("no token at all: it says where it goes",
              reason == "MITFAH_API is not set: put the bot's token from BotFather in .env.", str(reason))

        os.environ["MITFAH_API"] = "123:test-token"
        telebot.serve = fake_serve
        telebot.check_network = lambda chain_id, network: None if chain_id in (SEPOLIA, BASE) else "down"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            telebot.main()
        check("only the networks that answer are served, in a fixed order",
              len(served) == 1 and list(served[0].bot_data["networks"].items())
              == [(SEPOLIA, telebot.network_name(SEPOLIA)), (BASE, telebot.network_name(BASE))],
              str(served and served[0].bot_data))
        check("each one left out is named, with why",
              out.getvalue().splitlines() == ["Not serving Ethereum: down.", "Not serving BNB Smart Chain: down.",
                                              "Not serving Arbitrum One: down."], out.getvalue())

        telebot.check_network = lambda chain_id, network: "down"
        reason = _exit_reason(telebot.main)
        check("with none up, it doesn't start", reason is not None and "none of its networks is up" in reason
              and len(served) == 1, str(reason))
    finally:
        telebot.check_network, telebot.serve = original_check, original_serve
        conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS)}")
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_one_turn_at_a_time_per_chat():
    print("\n[8] one turn at a time in a conversation; different users side by side")
    running: dict = {}
    most: dict = {}
    lock = threading.Lock()

    def slow_chat(user_id, chain_id, text, network):
        key = (chain_id, user_id)
        with lock:
            running[key] = running.get(key, 0) + 1
            running["all"] = running.get("all", 0) + 1
            for k in (key, "all"):
                most[k] = max(most.get(k, 0), running[k])
        time.sleep(0.3)
        with lock:
            running[key] -= 1
            running["all"] -= 1
        return "ok", None

    first = make_user(3001, wallet_chains=(BASE,))
    second = make_user(3002, wallet_chains=(BASE,))
    telebot.chat_turn = slow_chat
    telebot._turn_locks.clear()

    async def both():
        def message(chat_id, text):
            async def reply_text(_, **options):
                pass
            update = SimpleNamespace(message=SimpleNamespace(chat_id=chat_id, text=text, reply_text=reply_text))
            context = SimpleNamespace(args=[], bot_data=BOT_DATA, bot=FakeBot())
            return telebot.start_chat(update, context)

        await asyncio.gather(message(3001, "send 1 ETH to Sam"), message(3001, "yes"), message(3002, "hi"))

    try:
        asyncio.run(both())
        check("one user's two messages never run at once", most[(BASE, first)] == 1, str(most))
        check("while another user's runs beside them", most["all"] == 2 and most[(BASE, second)] == 1, str(most))
        check("each conversation waits on its own lock, keyed by network and chat",
              set(telebot._turn_locks) == {(BASE, 3001), (BASE, 3002)}, str(set(telebot._turn_locks)))
    finally:
        telebot.chat_turn = fake_chat
        telebot._turn_locks.clear()


def test_quotes_stay_on_their_network():
    print("\n[9] a quote raised on one network can't be confirmed from another")
    user = make_user(5001, wallet_chains=(SEPOLIA, BASE))
    tap(5001, f"chain:{BASE}")

    turns.clear()
    run(telebot.start_chat, 5001, "send 1 ETH to Sam")
    tap(5001, f"chain:{SEPOLIA}")
    replies, _ = run(telebot.start_chat, 5001, "yes")
    check("a quote on Base, a switch to Sepolia, then yes: the yes is a turn on Sepolia",
          turns == [(user, BASE, "send 1 ETH to Sam", "base"), (user, SEPOLIA, "yes", "sepolia")], str(turns))
    check("and its reply says Sepolia", replies == ["<b>Sepolia</b>\n\nDone."], str(replies))

    # What Sepolia could do with Base's card: nothing. Its sign request is filed under Base.
    pending = quotes.put(user, BASE, turn_id=1, action="Send 1 ETH to Sam", calls=[], quote=PRICED, cost={})
    offered = signing.open_request(pending, signing.TELEGRAM, telegram_chat_id=5001)
    try:
        signing.find_by_quote(user, SEPOLIA, pending.quote_id)
        refused = False
    except signing.Expired:
        refused = True
    check("Base's card can't be approved as a Sepolia one", refused)
    found = signing.find_by_quote(user, BASE, pending.quote_id)
    check("while on Base it is still there to approve", found["id"] == offered.request["id"] and found["chain_id"] == BASE)
    check("and its link names that one request", signing.find_by_token(offered.token)["id"] == offered.request["id"])
    signing.cancel(found)


def test_unlinking_forgets_the_network():
    print("\n[10] unlinking the chat forgets the network it picked")
    user = make_user(6001, wallet_chains=(SEPOLIA, BASE))
    tap(6001, f"chain:{BASE}")
    check("Base is picked", db.get_telegram_chain(user) == BASE)

    db.unlink_telegram(user)
    check("unlinked, no network is picked", db.get_telegram_chain(user) is None)
    turns.clear()
    replies, _ = run(telebot.start_chat, 6001, "hi")
    check("and the chat is a stranger", replies == [telebot.UNLINKED_MESSAGE] and turns == [], str(replies))

    db.link_telegram(user, 6001)
    replies, _ = run(telebot.start_chat, 6001, "hi")
    check("linked again, it is asked afresh", turns == [] and replies[0].startswith("Which network should I use?"),
          str(replies))


def test_turns_are_rationed():
    print("\n[11] turns are rationed per account: a flood gets no turns and one notice")
    user = make_user(7001, wallet_chains=(BASE,))
    try:
        telebot._turn_gate = TurnGate(rate="2/minute", clock=lambda: 1_000.0)
        turns.clear()
        replies = [run(telebot.start_chat, 7001, f"message {i}")[0] for i in range(6)]
        check("only the first two reach the assistant",
              turns == [(user, BASE, "message 0", "base"), (user, BASE, "message 1", "base")], str(turns))
        check("they are answered", replies[:2] == [["<b>Base</b>\n\nDone."]] * 2, str(replies[:2]))
        check("the first refused one is told why, once",
              len(replies[2]) == 1 and "faster than I can answer" in replies[2][0], str(replies[2]))
        check("the rest of the flood gets no reply", replies[3:] == [[], [], []], str(replies[3:]))
    finally:
        telebot._turn_gate = ROOMY_GATE

    print("\n[11b] a third message, while one runs and one waits, is refused at once")
    busy = make_user(7002, wallet_chains=(BASE,))
    go = threading.Event()

    def held_chat(user_id, chain_id, text, network):
        turns.append((user_id, chain_id, text, network))
        go.wait(5)
        return "ok", None

    replies: dict[str, list] = {}

    async def three():
        def message(text):
            replies[text] = []

            async def reply_text(reply, **options):
                replies[text].append(reply)
            update = SimpleNamespace(message=SimpleNamespace(chat_id=7002, text=text, reply_text=reply_text))
            return telebot.start_chat(update, SimpleNamespace(args=[], bot_data=BOT_DATA, bot=FakeBot()))

        # The third is turned away before the first finishes, so the first may finish whenever.
        threading.Timer(0.3, go.set).start()
        await asyncio.gather(message("first"), message("second"), message("third"))

    telebot.chat_turn = held_chat
    telebot._turn_gate = TurnGate(rate="1000/minute")
    telebot._turn_locks.clear()
    turns.clear()
    try:
        asyncio.run(three())
        check("the first runs and the second waits its turn, then runs",
              turns == [(busy, BASE, "first", "base"), (busy, BASE, "second", "base")], str(turns))
        check("the third never reaches the assistant, and is told why",
              len(replies["third"]) == 1 and "still working on your last message" in replies["third"][0],
              str(replies["third"]))
    finally:
        telebot.chat_turn = fake_chat
        telebot._turn_gate = ROOMY_GATE
        telebot._turn_locks.clear()

    print("\n[11c] a turn that fails still gives its place back")
    failing = make_user(7003, wallet_chains=(BASE,))

    def broken_chat(user_id, chain_id, text, network):
        raise RuntimeError("the turn's thread blew up")

    # Nothing may wait: if the failed turn kept its place, the next message would be refused as busy.
    telebot._turn_gate = TurnGate(rate="1000/minute", max_waiting=0)
    telebot.chat_turn = broken_chat
    try:
        try:
            run(telebot.start_chat, 7003, "this fails")
            raised = None
        except RuntimeError as e:
            raised = e
        check("the failure isn't hidden", raised is not None)
        telebot.chat_turn = fake_chat
        turns.clear()
        replies, _ = run(telebot.start_chat, 7003, "and this?")
        check("the next message runs", turns == [(failing, BASE, "and this?", "base")]
              and replies == ["<b>Base</b>\n\nDone."], f"{turns} {replies}")
    finally:
        telebot.chat_turn = fake_chat
        telebot._turn_gate = ROOMY_GATE


def test_read_commands():
    print("\n[12] /balance, /limit, /history, /positions, /contacts and /web answer without the assistant")
    user = make_user(8001, wallet_chains=(BASE, BSC))
    tap(8001, f"chain:{BASE}")
    telebot._turn_locks.clear()
    turns.clear()
    saved = (telebot._token_balances, telebot.load_network_config_by_name, telebot._get_wallet_status,
             telebot.read_positions)
    read_on = []

    def fake_balances(w3, user_id, chain_id, account):
        read_on.append((user_id, chain_id, account, db.get_user_network(user_id)))
        return [
            {"ticker": "eth", "native": True, "amount": 0.5},
            {"ticker": "usdc", "native": False, "amount": 25.0},
            {"ticker": "weth", "native": False, "amount": 0.0},
            {"ticker": "<b>[claim](https://evil.example)</b>", "native": False, "amount": 3.0},
            {"ticker": "eth/usdc lp", "native": False, "amount": 0.0012, "lp": True},
            {"ticker": "dai", "native": False, "amount": None, "error": "reverted"},
        ]

    try:
        telebot._token_balances = fake_balances
        telebot.load_network_config_by_name = lambda name: (SimpleNamespace(), BASE)
        replies, _ = run(telebot.balance_cmd, 8001)
        check("/balance reads the wallet on the network in use", read_on == [(user, BASE, WALLET, "base")], str(read_on))
        check("the coin first, then what isn't zero, then what couldn't be read -- a token's own name escaped",
              replies == ["<b>Base</b>\n\n• 0.5 ETH\n• 25 USDC\n• 3 &lt;B&gt;[CLAIM](HTTPS://EVIL.EXAMPLE)&lt;/B&gt;\n"
                          "• 0.0012 ETH/USDC LP\nCouldn't read: DAI."], str(replies))
        check("as HTML, so a symbol shaped like a link stays text", "<a " not in replies[0]
              and reply_options[0].get("parse_mode") == ParseMode.HTML, str(reply_options))

        def unreadable(*_):
            raise ConnectionError("https://base.example/v2/SECRET down")

        telebot._token_balances = unreadable
        replies, _ = run(telebot.balance_cmd, 8001)
        check("a failed read says so, and nothing of why", replies == [f"<b>Base</b>\n\n{telebot.READ_FAILED}"], str(replies))

        expires = int(time.time()) + 20 * 86_400 + 600
        status = {"paused": False, "session_active": True, "session_expires_at": expires,
                  "daily_limit_usd": 100.0, "remaining_usd": 80.0, "window_hours": 24.0}
        telebot._get_wallet_status = lambda user_id: status
        until = datetime.fromtimestamp(expires, tz=timezone.utc)
        replies, _ = run(telebot.limit_cmd, 8001)
        check("/limit: what's left, per what, and when the devices' approval runs out",
              replies == [f"<b>Base</b>\n\nSpending limit: $80.00 left of $100.00 per day.\n"
                          f"Your devices' approval runs out on {until.day} {until:%b %Y}."], str(replies))
        status.update(paused=True, session_expires_at=int(time.time()) + 86_400 + 600)
        replies, _ = run(telebot.limit_cmd, 8001)
        check("a pause, and a key running out soon, get their warnings instead",
              "⏸ The owner has paused this wallet" in replies[0] and "runs out in 1 day" in replies[0]
              and "approval runs out on" not in replies[0], str(replies))
        status.update(session_active=False)
        replies, _ = run(telebot.limit_cmd, 8001)
        check("no device approved says so", "None of your devices is approved" in replies[0], str(replies))
        check("windows read naturally", [telebot._window(h) for h in (24, 168, 1, 12, 36)]
              == ["per day", "per 7 days", "per hour", "per 12 hours", "per 36 hours"])

        now = int(time.time())
        db.add_transaction(user, BASE, WALLET, "assistant", "Transfer 10 USDC to 0xabc", "confirmed",
                           tx_hash="0x" + "ab" * 31 + "cdef", mined_at=now - 7200)
        db.add_transaction(user, BASE, WALLET, "assistant", "Swap <1 ETH> for USDC", "pending",
                           user_op_hash="0x" + "11" * 32)
        db.add_transaction(user, BSC, WALLET, "assistant", "on another network", "confirmed",
                           tx_hash="0x" + "cd" * 32, mined_at=now)
        replies, _ = run(telebot.history_cmd, 8001)
        mined = datetime.fromtimestamp(now - 7200, tz=timezone.utc)
        lines = replies[0].split("\n")
        check("/history: this network's, newest first, escaped, hashes shortened, pointing at the web app",
              len(lines) == 5 and lines[2].endswith("UTC: Swap &lt;1 ETH&gt; for USDC (pending)")
              and lines[3] == f"• {mined.day} {mined:%b %H:%M} UTC: Transfer 10 USDC to 0xabc <code>0xabababab…cdef</code>"
              and lines[4] == "All of them are in the web app's History tab (/web).", str(lines))

        positions = [_position(), _position(id=8, current_price=3420.0, in_range=False),
                     _position(id=9, min_price=0.0, max_price=None, full_range=True, in_range=False)]
        telebot.read_positions = lambda user_id, chain_id: positions
        replies, _ = run(telebot.positions_cmd, 8001)
        held = "Holds 0.000000000000000001 ETH + 0.000001 USDC; fees to collect 0.000000000000000001 ETH + 0.000001 USDC."
        check("/positions: one line each, saying whether it earns",
              replies[0].split("\n")[2:] == [
                  f"• ETH/USDC #7: 2,850–3,150 USDC per ETH, earning fees. {held}",
                  f"• ETH/USDC #8: 2,850–3,150 USDC per ETH, out of range (ETH at 3,420 USDC), not earning. {held}",
                  f"• ETH/USDC #9: full range, earning fees. {held}",
              ], str(replies))
        telebot.read_positions = lambda user_id, chain_id: []
        replies, _ = run(telebot.positions_cmd, 8001)
        check("none: it says how to open one", replies == ["<b>Base</b>\n\nNo positions. Ask me to add liquidity to open one."],
              str(replies))
        tap(8001, f"chain:{BSC}")
        telebot.read_positions = lambda user_id, chain_id: positions
        replies, _ = run(telebot.positions_cmd, 8001)
        check("on BSC there are none to read: it says where the LP tokens are",
              len(replies) == 1 and "PancakeSwap" in replies[0] and "/balance" in replies[0], str(replies))
        tap(8001, f"chain:{BASE}")

        replies, _ = run(telebot.contacts_cmd, 8001)
        check("/contacts with none: says so, and where to add them",
              replies == ["<b>Base</b>\n\nNo contacts yet. I can only pay people saved in the web app.\n"
                          "Add or remove contacts in the web app (/web)."], str(replies))
        db.save_contact(user, "Sam", SAM)
        db.save_contact(user, "<i>eve</i>", SAM)
        replies, _ = run(telebot.contacts_cmd, 8001)
        check("with some: each with its address, names escaped",
              replies == [f"<b>Base</b>\n\n• &lt;i&gt;eve&lt;/i&gt;: <code>{SAM}</code>\n• sam: <code>{SAM}</code>\n"
                          "Add or remove contacts in the web app (/web)."], str(replies))

        saved_env = {name: os.environ.get(name) for name in ("WEB_APP_URL", "CORS_ORIGINS")}
        try:
            os.environ["WEB_APP_URL"] = "https://mitfah.com"
            replies, _ = run(telebot.web_cmd, 8001)
            check("/web links the web app", replies == ["The web app: https://mitfah.com"]
                  and reply_options[0] == {"link_preview_options": telebot.NO_PREVIEW}, f"{replies} {reply_options}")
            os.environ.pop("WEB_APP_URL")
            os.environ["CORS_ORIGINS"] = " https://app.example , https://other.example"
            check("without WEB_APP_URL, the first origin the API lets in", telebot.web_app_url() == "https://app.example")
        finally:
            for name, value in saved_env.items():
                os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)

        for command in (telebot.balance_cmd, telebot.limit_cmd, telebot.history_cmd, telebot.positions_cmd,
                        telebot.contacts_cmd, telebot.web_cmd):
            replies, _ = run(command, 8009)
            check(f"{command.__name__} refuses an unlinked chat", replies == [telebot.UNLINKED_MESSAGE], str(replies))
        check("none of them asked the assistant", turns == [], str(turns))
        check("or waited for the conversation's turn", dict(telebot._turn_locks) == {}, str(dict(telebot._turn_locks)))
    finally:
        (telebot._token_balances, telebot.load_network_config_by_name, telebot._get_wallet_status,
         telebot.read_positions) = saved


def test_send_and_swap():
    print("\n[13] /send and /swap are checked before the assistant hears them, in fixed words")
    user = make_user(8101, wallet_chains=(BASE,))
    db.save_contact(user, "Sam", SAM)
    db.save_contact(user, "Mary Ann", SAM)
    usdc = _token(0x05DC)

    def ask(command, args, chat_id=8101):
        turns.clear()
        replies, _ = run(command, chat_id, args=args)
        return replies, [text for _, _, text, _ in turns]

    for args, expected in (
        ([], telebot.SEND_USAGE),
        (["10", "USDC"], telebot.SEND_USAGE),
        (["ten", "USDC", "to", "Sam"], f"“ten” isn't an amount. {telebot.SEND_USAGE}"),
        (["0", "USDC", "to", "Sam"], f"“0” isn't an amount. {telebot.SEND_USAGE}"),
        (["-1", "USDC", "to", "Sam"], f"“-1” isn't an amount. {telebot.SEND_USAGE}"),
        (["1e3", "USDC", "to", "Sam"], f"“1e3” isn't an amount. {telebot.SEND_USAGE}"),
        (["0." + "1" * 19, "USDC", "to", "Sam"], f"“0.{'1' * 19}” isn't an amount. {telebot.SEND_USAGE}"),
        (["10", "DOGE", "to", "Sam"],
         "I don't know a token called DOGE on Base. You can add it in the web app (Dashboard → Balances → Add token)."),
        (["10", "BNB", "to", "Sam"],
         "I don't know a token called BNB on Base. You can add it in the web app (Dashboard → Balances → Add token)."),
        (["10", "USDC", "to", "Bob"], "Bob isn't one of your contacts. Add them in the web app (Contacts), then send this again."),
    ):
        replies, asked = ask(telebot.send_cmd, args)
        check(f"/send {' '.join(args)!r}: refused, the assistant never asked", replies == [expected] and asked == [],
              f"{replies} {asked}")

    for args, expected in (
        (["10", "usdc", "to", "Sam"], "Send 10 USDC to Sam"),
        (["0.5", "eth", "Sam"], "Send 0.5 ETH to Sam"),
        (["10", usdc, "to", "sam"], "Send 10 USDC to sam"),
        (["1", "weth", "to", "mary", "ann"], "Send 1 WETH to mary ann"),
    ):
        replies, asked = ask(telebot.send_cmd, args)
        check(f"/send {' '.join(args)!r} asks: {expected!r}", asked == [expected] and replies == ["<b>Base</b>\n\nDone."],
              f"{asked} {replies}")

    for args, expected in (
        (["1", "ETH", "for", "USDC"], "Swap 1 ETH for USDC"),
        (["1", "eth", "usdc"], "Swap 1 ETH for USDC"),
        (["25", "usdc", "to", "weth"], "Swap 25 USDC for WETH"),
    ):
        replies, asked = ask(telebot.swap_cmd, args)
        check(f"/swap {' '.join(args)!r} asks: {expected!r}", asked == [expected], f"{asked} {replies}")
    for args, expected in (
        (["1", "ETH"], telebot.SWAP_USAGE),
        (["1", "ETH", "USDC", "now"], telebot.SWAP_USAGE),
        (["x", "ETH", "for", "USDC"], f"“x” isn't an amount. {telebot.SWAP_USAGE}"),
        (["1", "ETH", "for", "WETH"], "ETH and WETH are the same token here, so there's nothing to swap."),
        (["1", "usdc", "for", "USDC"], "USDC and USDC are the same token here, so there's nothing to swap."),
        (["1", "ETH", "for", "PEPE"],
         "I don't know a token called PEPE on Base. You can add it in the web app (Dashboard → Balances → Add token)."),
    ):
        replies, asked = ask(telebot.swap_cmd, args)
        check(f"/swap {' '.join(args)!r}: refused", replies == [expected] and asked == [], f"{replies} {asked}")

    bsc_user = make_user(8102, wallet_chains=(BSC,))
    db.save_contact(bsc_user, "Sam", SAM)
    replies, asked = ask(telebot.send_cmd, ["1", "ETH", "to", "Sam"], chat_id=8102)
    check("on BSC, ETH is not the native coin: refused rather than sent as BNB",
          asked == [] and replies[0].startswith("I don't know a token called ETH on BNB Smart Chain"), str(replies))
    replies, asked = ask(telebot.send_cmd, ["1", "bnb", "to", "Sam"], chat_id=8102)
    check("BNB is", asked == ["Send 1 BNB to Sam"], str(asked))
    replies, asked = ask(telebot.swap_cmd, ["1", "BNB", "for", "WBNB"], chat_id=8102)
    check("and BNB for WBNB is no swap", asked == [] and "same token" in replies[0], str(replies))

    replies, asked = ask(telebot.send_cmd, ["10", "USDC", "to", "Sam"], chat_id=8109)
    check("an unlinked chat is refused", replies == [telebot.UNLINKED_MESSAGE] and asked == [], str(replies))

    telebot._turn_gate = TurnGate(rate="1/minute", clock=lambda: 5_000.0)
    try:
        ask(telebot.send_cmd, ["10", "DOGE", "to", "Sam"])
        replies, asked = ask(telebot.send_cmd, ["10", "USDC", "to", "Sam"])
        check("a refused command spends none of the ration", asked == ["Send 10 USDC to Sam"], str(asked))
        replies, asked = ask(telebot.send_cmd, ["10", "USDC", "to", "Sam"])
        check("a checked one is rationed like any message", asked == [] and "faster than I can answer" in replies[0],
              str(replies))
    finally:
        telebot._turn_gate = ROOMY_GATE


def _card_options(i: int) -> list[tuple[str, str]]:
    return buttons(reply_options[i]) if i < len(reply_options) else []


CARD_TAIL = "Tap “Confirm with passkey”, then approve with Face ID, your fingerprint or your PIN."


def _card_text(action: str, fees: str, minutes: int = 5) -> str:
    return f"<b>Confirm on Base?</b>\n{action}\nFees: {fees} · good for {minutes} minutes\n{CARD_TAIL}"


def _request_for(link: str) -> dict:
    """The sign request a card's link names, found as the approval page finds it."""
    return signing.find_by_token(link.rsplit("/c/", 1)[1])


def test_quote_card():
    print("\n[14] a turn that raised one quote is followed by a card the code writes: a passkey link and Cancel")
    user = make_user(8201, wallet_chains=(BASE,))
    db.save_contact(user, "Sam", SAM)
    action = f"Transfer 10 USDC to {SAM}"
    try:
        quotes_for_turns[:] = [{"action": action, "cost": {"total_usd": 0.3512, "max_total_usd": 0.5}}]
        replies, _ = run(telebot.start_chat, 8201, "send 10 usdc to sam")
        check("the reply, then the card", len(replies) == 2 and replies[0] == "<b>Base</b>\n\nDone.", str(replies))
        check("the card says what the passkey approves, where, for what, for how long, and how",
              replies[1] == _card_text(action, "$0.35"), replies[1])
        card = _card_options(1)
        quote_id = card[1][1].rsplit(":", 1)[1] if len(card) == 2 else ""
        link = card[0][1] if card else ""
        check("with a link to the approval page and a Cancel naming the network and the quote",
              len(card) == 2 and card[0][0] == "Confirm with passkey" and link.startswith(f"{WEB_APP}/c/")
              and card[1] == ("✖️ Cancel", f"q:no:{BASE}:{quote_id}") and quotes._pending[quote_id].action == action,
              str(card))
        check("Cancel within Telegram's 64 bytes", len(card[1][1].encode()) <= 64 if card else False)
        check("sent as HTML", reply_options[1].get("parse_mode") == ParseMode.HTML, str(reply_options[1]))
        request = _request_for(link)
        check("the link names this quote's sign request, on Base, offered in Telegram, frozen as quoted",
              request["quote_id"] == quote_id and request["chain_id"] == BASE and request["origin"] == "telegram"
              and signing.thaw(request)["action"] == action and signing.thaw(request)["quote"] == PRICED)
        check("the token is kept only as its hash", link.rsplit("/c/", 1)[1] not in str(request)
              and request["token_hash"] == signing.token_hash(link.rsplit("/c/", 1)[1]))
        check("and the card's message is recorded, for the API to take its buttons off",
              request["telegram_chat_id"] == 8201 and request["telegram_message_id"] is not None)

        turns.clear()
        replies, _ = run(telebot.send_cmd, 8201, args=["10", "USDC", "to", "Sam"])
        check("/send gets its card the same way", len(replies) == 2 and replies[1].startswith("<b>Confirm on Base?</b>"),
              str(replies))

        quotes_for_turns[:] = [{"action": "Transfer 1 <b>[x](https://evil.example)</b> to 0xabc",
                                "cost": {"total_usd": 0.004}}]
        replies, _ = run(telebot.start_chat, 8201, "send 1 x to 0xabc")
        check("the action is escaped, and a fee under a cent says so",
              replies[1] == _card_text("Transfer 1 &lt;b&gt;[x](https://evil.example)&lt;/b&gt; to 0xabc", "under $0.01"),
              replies[1])
        quotes_for_turns[:] = [{"action": "Wrap 1 ETH", "cost": {"usd_unavailable": "no price", "total_native": 0.00021,
                                                                 "native_asset": "ETH"}}]
        replies, _ = run(telebot.start_chat, 8201, "wrap 1 eth")
        check("fees the quote couldn't price in USD are in the coin",
              "\nFees: 0.00021 ETH · good for 5 minutes\n" in replies[1], replies[1])

        replies, _ = run(telebot.start_chat, 8201, "wrap 1 eth", refuse_links=True)
        card = _card_options(1)
        check("a link Telegram refuses on a button (a localhost web app) goes in the text; Cancel stays",
              len(replies) == 2 and f"\n\nApprove it here: {WEB_APP}/c/" in replies[1]
              and [label for label, _ in card] == ["✖️ Cancel"], f"{replies} {card}")

        quotes_for_turns[:] = [{"action": "one", "cost": {"total_usd": 1}}, {"action": "two", "cost": {"total_usd": 1}}]
        replies, _ = run(telebot.start_chat, 8201, "two things")
        check("two quotes in one turn: no card, the user says which in words", replies == ["<b>Base</b>\n\nDone."],
              str(replies))
        quotes_for_turns.clear()
        replies, _ = run(telebot.start_chat, 8201, "hi")
        check("no quote: no card", replies == ["<b>Base</b>\n\nDone."], str(replies))

        def failed_turn(user_id, chain_id, text, network):
            quotes.put(user_id, chain_id, 99_999, "half-quoted", [], PRICED, {"total_usd": 1})
            return "Sorry, something went wrong while handling that. Please try again.", None

        telebot.chat_turn = failed_turn
        replies, _ = run(telebot.start_chat, 8201, "send 2 usdc to sam")
        check("a turn that failed shows no card, whatever it raised", len(replies) == 1, str(replies))

        pending = quotes.put(user, BASE, 1, action, [], PRICED, {"total_usd": 0.35})
        request = signing.open_request(pending, signing.TELEGRAM, telegram_chat_id=8201).request
        text, _ = telebot.quote_card(request, "t0ken", request["expires_at"] - 50)
        check("a card for a quote near its end says so", "good for 1 minute\n" in text, text)
    finally:
        telebot.chat_turn = fake_chat
        quotes_for_turns.clear()
        quotes.drop_all(user, BASE)
        for request in db.get_open_sign_requests(user, BASE):
            db.cancel_sign_request(request["id"])


def test_typed_yes():
    print("\n[14b] a typed “yes” while a card waits gets the card again, with a new link, and no turn")
    user = make_user(8251, wallet_chains=(BASE,))
    saved = telebot.answer_yes
    answered = []
    telebot.answer_yes = lambda user_id, chain_id, typed: answered.append((user_id, chain_id, typed)) or signing.YES_REPLY
    try:
        turns.clear()
        replies, _ = run(telebot.start_chat, 8251, "yes")
        check("with no card waiting, a yes is an ordinary turn",
              turns == [(user, BASE, "yes", "base")] and answered == [], f"{turns} {answered}")

        quotes_for_turns[:] = [{"action": "Wrap 1 ETH", "cost": {"total_usd": 0.2}}]
        run(telebot.start_chat, 8251, "wrap 1 eth")
        quotes_for_turns.clear()
        first_link = _card_options(1)[0][1]
        first = _request_for(first_link)

        turns.clear()
        replies, _ = run(telebot.start_chat, 8251, "Send it!")
        check("“Send it!” while it waits: no turn, answered in fixed words",
              turns == [] and answered == [(user, BASE, "Send it!")]
              and replies[0] == f"<b>Base</b>\n\n{signing.YES_REPLY}", f"{turns} {replies}")
        new_link = _card_options(1)[0][1] if len(replies) == 2 else ""
        check("then the same card again", len(replies) == 2 and replies[1].startswith("<b>Confirm on Base?</b>\nWrap 1 ETH"),
              str(replies))
        check("with a new link to the same request", new_link != first_link and _request_for(new_link)["id"] == first["id"])
        try:
            _request_for(first_link)
            old_dead = False
        except signing.Expired:
            old_dead = True
        check("the old link is dead", old_dead)
        check("and the old card's buttons come off",
              last_bot["bot"].unbuttoned == [(8251, first["telegram_message_id"])], str(last_bot["bot"].unbuttoned))
        check("the request now points at the new card",
              db.get_sign_request(first["id"])["telegram_message_id"] != first["telegram_message_id"])

        for text in ("yes please send 1 eth", "no", "y?"):
            turns.clear()
            run(telebot.start_chat, 8251, text)
            check(f"“{text}” is not a bare yes: a turn", turns == [(user, BASE, text, "base")], str(turns))
    finally:
        telebot.answer_yes = saved
        quotes_for_turns.clear()
        quotes.drop_all(user, BASE)
        for request in db.get_open_sign_requests(user, BASE):
            db.cancel_sign_request(request["id"])
        telebot._turn_locks.clear()


# Everything a quote tap did, in order: how it was answered, the buttons coming off, the actions it ran.
tap_events: list[tuple] = []


def quote_tap(chat_id: int, data: str, chat_type: str = Chat.PRIVATE, from_id: int | None = None,
              no_chat: bool = False, bot_data: dict | None = None):
    """
    Taps a quote's button carrying `data`, as tap() taps a network's.

    @return  What the bot sent, as [(chat, text)]. What the tap did lands in tap_events.
    """
    tap_events.clear()

    async def answer(text=None, show_alert=None, **_):
        tap_events.append(("answer", text, show_alert))

    async def edit_message_reply_markup(reply_markup=None, **_):
        tap_events.append(("buttons off", reply_markup))

    update = SimpleNamespace(
        callback_query=SimpleNamespace(data=data, answer=answer, edit_message_reply_markup=edit_message_reply_markup),
        effective_chat=None if no_chat else SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=chat_id if from_id is None else from_id),
    )
    bot = FakeBot()
    asyncio.run(telebot.quote_button(update, SimpleNamespace(bot_data=bot_data or BOT_DATA, bot=bot)))
    return bot.sent


def test_quote_buttons():
    print("\n[15] a card's Cancel acts only for its account, on its network, once")
    user = make_user(8301, wallet_chains=(SEPOLIA, BASE))
    tap(8301, f"chain:{BASE}")
    saved = telebot.cancel_quote
    outcome = {"cancel": {"id": 1}}
    threads = []

    def fake_cancel(user_id, chain_id, quote_id):
        tap_events.append(("cancel", user_id, chain_id, quote_id))
        threads.append(threading.current_thread().name)
        return outcome["cancel"]

    telebot.cancel_quote = fake_cancel
    telebot._turn_locks.clear()
    try:
        sent = quote_tap(8301, f"q:no:{BASE}:Abc_12-x")
        check("Cancel: answered, the buttons off first, then the quote discarded for this account on this network",
              tap_events == [("answer", None, None), ("buttons off", None), ("cancel", user, BASE, "Abc_12-x")],
              str(tap_events))
        check("and the user told, under the network's name", sent == [(8301, "<b>Base</b>\n\nCancelled. Nothing was sent.")],
              str(sent))
        check("in the conversation's own turn", set(telebot._turn_locks) == {(BASE, 8301)}, str(set(telebot._turn_locks)))
        check("on a tap thread, not behind other people's turns", threads[-1].startswith("telegram-tap"), str(threads))
        outcome["cancel"] = {}
        sent = quote_tap(8301, f"q:no:{BASE}:Abc_12-x")
        check("a quote with no sign request is cancelled all the same",
              sent == [(8301, "<b>Base</b>\n\nCancelled. Nothing was sent.")], str(sent))
        outcome["cancel"] = None
        sent = quote_tap(8301, f"q:no:{BASE}:Abc_12-x")
        check("one already gone -- sent, cancelled, expired -- says so",
              sent == [(8301, f"<b>Base</b>\n\n{telebot.STALE_QUOTE}")], str(sent))

        sent = quote_tap(8301, f"q:ok:{BASE}:Abc_12-x")
        check("a Confirm button from before passkeys: told how it works now, nothing run",
              tap_events == [("answer", telebot.OLD_CONFIRM, True)] and sent == [], f"{tap_events} {sent}")

        refused = [("answer", telebot.OTHER_NETWORK_QUOTE, True)]
        for data, why in ((f"q:no:{SEPOLIA}:Abc", "a quote from the network the user switched away from"),
                          (f"q:no:{BASE}x:Abc", "a network that isn't a number"),
                          ("q:no::Abc", "no network"), (f"q:no:{BASE}:", "no quote"),
                          (f"q:no:{BASE}:Abc!", "a quote id that isn't one"), (f"q:yes:{BASE}:Abc", "no such button"),
                          (f"q:no:{BASE}:Abc:more", "more than a button carries")):
            sent = quote_tap(8301, data)
            check(f"{why}: refused, nothing run", tap_events == refused and sent == [], f"{tap_events} {sent}")

        nothing = [("answer", None, None)]
        for chat_type in (Chat.GROUP, Chat.SUPERGROUP, Chat.CHANNEL):
            sent = quote_tap(8301, f"q:no:{BASE}:Abc", chat_type=chat_type)
            check(f"a tap in a {chat_type}: nothing run, nothing said", tap_events == nothing and sent == [],
                  f"{tap_events} {sent}")
        sent = quote_tap(8301, f"q:no:{BASE}:Abc", from_id=8302)
        check("a tap by someone else: the same", tap_events == nothing and sent == [], f"{tap_events} {sent}")
        sent = quote_tap(8301, f"q:no:{BASE}:Abc", no_chat=True)
        check("a tap with no chat: the same", tap_events == nothing and sent == [], f"{tap_events} {sent}")
        sent = quote_tap(8309, f"q:no:{BASE}:Abc")
        check("an unlinked chat's tap: told how to link, nothing run",
              tap_events == nothing and sent == [(8309, telebot.UNLINKED_MESSAGE)], f"{tap_events} {sent}")

        make_user(8303, wallet_chains=(SEPOLIA, BASE))
        sent = quote_tap(8303, f"q:no:{BASE}:Abc")
        check("with no network picked: refused", tap_events == refused and sent == [], f"{tap_events} {sent}")
    finally:
        telebot.cancel_quote = saved
        telebot._turn_locks.clear()


def test_cancel_command():
    print("\n[16] /cancel drops the quotes waiting on the network in use, and their cards' buttons")
    user = make_user(8401, wallet_chains=(BASE,))
    saved = telebot.cancel_quotes
    asked = []
    dropped = {"requests": [{"id": 1, "telegram_chat_id": 8401, "telegram_message_id": 77}, {}]}
    telebot.cancel_quotes = lambda user_id, chain_id: asked.append((user_id, chain_id)) or dropped["requests"]
    try:
        replies, _ = run(telebot.cancel_cmd, 8401)
        check("it drops them on this network, and says so",
              asked == [(user, BASE)] and replies == ["Cancelled. Nothing was sent."], f"{asked} {replies}")
        check("and takes the buttons off each card it had", last_bot["bot"].unbuttoned == [(8401, 77)],
              str(last_bot["bot"].unbuttoned))
        dropped["requests"] = []
        replies, _ = run(telebot.cancel_cmd, 8401)
        check("with none waiting, it says that", replies == ["There was nothing to cancel."], str(replies))
        replies, _ = run(telebot.cancel_cmd, 8409)
        check("an unlinked chat is refused", replies == [telebot.UNLINKED_MESSAGE] and len(asked) == 2, str(replies))
    finally:
        telebot.cancel_quotes = saved
        telebot._turn_locks.clear()


def test_feedback():
    print("\n[17] /feedback stores a note for the team, a few an hour and a day at most")
    user = make_user(8501, wallet_chains=(BASE,))
    stored = lambda: db.get_db().execute(  # noqa: E731
        "SELECT chain_id, source, text FROM app_feedback WHERE user_id = ? ORDER BY id", (user,)).fetchall()

    replies, _ = run(telebot.feedback_cmd, 8501, text="/feedback   The swap quote\nwas hard to read.  ")
    check("it is stored as written, with the network and where it came from",
          [tuple(r) for r in stored()] == [(BASE, "telegram", "The swap quote\nwas hard to read.")], str(stored()))
    check("and thanked", replies == ["Thanks, I've passed that on to the team."], str(replies))

    for text, why in (("/feedback", "nothing"), ("/feedback ok", "too little")):
        replies, _ = run(telebot.feedback_cmd, 8501, text=text)
        check(f"{why}: says how to write it, stores nothing", replies == [telebot.FEEDBACK_USAGE] and len(stored()) == 1,
              str(replies))
    replies, _ = run(telebot.feedback_cmd, 8501, text="/feedback " + "x" * (telebot.FEEDBACK_MAX_CHARS + 1))
    check("too long: refused", "keep it under 1,000 characters" in replies[0] and len(stored()) == 1, str(replies))
    replies, _ = run(telebot.feedback_cmd, 8501, text="/feedback " + "x" * telebot.FEEDBACK_MAX_CHARS)
    check("the longest allowed is stored", len(stored()) == 2, str(len(stored())))

    replies, _ = run(telebot.feedback_cmd, 8501, text="/feedback third this hour")
    replies, _ = run(telebot.feedback_cmd, 8501, text="/feedback fourth this hour")
    check(f"past {telebot.FEEDBACK_PER_HOUR} an hour: refused, kindly",
          len(stored()) == 3 and replies == ["Thanks — that's plenty for now. Please send more later."], str(replies))

    db.get_db().execute("UPDATE app_feedback SET created_at = created_at - 7200 WHERE user_id = ?", (user,))
    for i in range(telebot.FEEDBACK_PER_DAY - 3):
        db.add_feedback(user, BASE, "telegram", f"earlier {i}")
    db.get_db().execute("UPDATE app_feedback SET created_at = ? WHERE user_id = ?", (int(time.time()) - 7200, user))
    db.get_db().commit()
    replies, _ = run(telebot.feedback_cmd, 8501, text="/feedback one more today")
    check(f"past {telebot.FEEDBACK_PER_DAY} a day: refused, though none this hour",
          len(stored()) == telebot.FEEDBACK_PER_DAY and "plenty" in replies[0], str(replies))

    two = make_user(8502, wallet_chains=(SEPOLIA, BASE))
    run(telebot.feedback_cmd, 8502, text="/feedback no network picked yet")
    row = db.get_db().execute("SELECT chain_id, text FROM app_feedback WHERE user_id = ?", (two,)).fetchone()
    check("with no network picked, it is stored without one", tuple(row) == (None, "no network picked yet"), str(row))
    replies, _ = run(telebot.feedback_cmd, 8509, text="/feedback who am I")
    check("an unlinked chat is refused", replies == [telebot.UNLINKED_MESSAGE], str(replies))


def test_unknown_command():
    print("\n[18] a command the bot doesn't have is answered")
    replies, _ = run(telebot.unknown_cmd, 8601, text="/nope")
    check("with where to find the real ones", replies == ["I don't know that command. /help lists the ones I have."],
          str(replies))


if __name__ == "__main__":
    try:
        test_each_account_acts_on_its_network()
        test_a_lost_network_is_not_replaced()
        test_start_and_help()
        test_chain_command()
        test_taps_are_checked()
        test_private_chats_only()
        test_replies_fit_telegram()
        test_replies_keep_their_formatting()
        test_wallet_warnings()
        test_position_warnings()
        test_daily_check()
        test_position_check()
        test_network_check()
        test_one_turn_at_a_time_per_chat()
        test_quotes_stay_on_their_network()
        test_unlinking_forgets_the_network()
        test_turns_are_rationed()
        test_read_commands()
        test_send_and_swap()
        test_quote_card()
        test_typed_yes()
        test_quote_buttons()
        test_cancel_command()
        test_feedback()
        test_unknown_command()
    finally:
        os.unlink(_tmp_db.name)

    finish("All Telegram bot checks passed.")
