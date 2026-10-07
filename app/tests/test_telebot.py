"""
The Telegram bots, offline: each bot acts on its own chain and only in private chats, one link
covers them all, replies keep their formatting and fit Telegram's limit, and the daily check
reaches the right chats.

No Telegram, no chain and no model: updates and the bot are stand-ins, and chat() and the wallet
read are swapped for recorders. Runs against a throwaway database.

Run: make telebot-test   (or: python app/tests/test_telebot.py)
"""
import asyncio
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

from telegram import Chat, Message, Update        # noqa: E402
from telegram.constants import ParseMode          # noqa: E402
from telegram.error import BadRequest, Forbidden, TimedOut  # noqa: E402

import telebot                                    # noqa: E402
import telegram_bots                              # noqa: E402
import telegram_format                            # noqa: E402

SEPOLIA, BASE, ARBITRUM = 11155111, 8453, 42161
SEPOLIA_BOT = {"chain_id": SEPOLIA, "network": "sepolia"}
BASE_BOT = {"chain_id": BASE, "network": "base"}
ARBITRUM_BOT = {"chain_id": ARBITRUM, "network": "arbitrum"}
WALLET = "0x000000000000000000000000000000000000dEaD"

# Every turn chat() was asked for, as (user_id, chain_id, text, network).
turns: list[tuple] = []
reply_for_turns = {"text": "Done."}


def fake_chat(user_id, chain_id, text, network):
    turns.append((user_id, chain_id, text, network))
    return reply_for_turns["text"]


telebot.chat = fake_chat


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
    """The bot as handlers and jobs see it. `errors` maps a chat to what sending to it raises."""

    def __init__(self, errors: dict | None = None):
        self.errors = errors or {}
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        if chat_id in self.errors:
            raise self.errors[chat_id]
        self.sent.append((chat_id, text))

    async def send_chat_action(self, chat_id, action):
        pass


# How each reply of the last run() was sent: the options handed to reply_text, in order.
reply_options: list[dict] = []


def run(handler, chat_id: int, bot_data: dict, text: str = "", args: list | None = None,
        refuse_formatting: bool = False):
    """
    Runs one handler for a message from `chat_id` to the bot described by `bot_data`.

    @param refuse_formatting  Telegram answers every formatted message with BadRequest, as it does
                              markup it can't parse.
    @return  (the replies, the one-off jobs it scheduled). Their options land in reply_options.
    """
    replies, jobs = [], []
    reply_options.clear()

    async def reply_text(reply, **options):
        if refuse_formatting and "parse_mode" in options:
            raise BadRequest("Can't parse entities: unsupported start tag")
        replies.append(reply)
        reply_options.append(options)

    update = SimpleNamespace(message=SimpleNamespace(chat_id=chat_id, text=text, reply_text=reply_text))
    context = SimpleNamespace(
        args=args or [],
        bot_data=bot_data,
        bot=FakeBot(),
        job_queue=SimpleNamespace(run_once=lambda callback, **kwargs: jobs.append((callback, kwargs))),
    )
    asyncio.run(handler(update, context))
    return replies, jobs


def test_each_bot_acts_on_its_own_chain():
    print("\n[1] each bot acts on its own chain, not the one last deployed on")
    user = make_user(1001, wallet_chains=(SEPOLIA, BASE), saved="sepolia")

    turns.clear()
    replies, _ = run(telebot.start_chat, 1001, BASE_BOT, "what's in my wallet?")
    check("the Base bot runs the turn on Base", turns == [(user, BASE, "what's in my wallet?", "base")], str(turns))
    check("and answers with the reply", replies == ["Done."], str(replies))

    turns.clear()
    run(telebot.start_chat, 1001, SEPOLIA_BOT, "and here?")
    check("the Sepolia bot runs it on Sepolia", turns == [(user, SEPOLIA, "and here?", "sepolia")], str(turns))

    make_user(1002, wallet_chains=(SEPOLIA,))
    turns.clear()
    replies, _ = run(telebot.start_chat, 1002, BASE_BOT, "hi")
    check("without a wallet on the bot's chain, nothing runs", turns == [], str(turns))
    check("and the user is told where to set one up",
          len(replies) == 1 and "don't have a wallet on Base" in replies[0], str(replies))

    replies, _ = run(telebot.start_chat, 1003, BASE_BOT, "hi")
    check("an unlinked chat is refused", replies == [telebot.UNLINKED_MESSAGE] and turns == [], str(replies))


def test_one_link_covers_every_bot():
    print("\n[2] a link made in one bot covers them all")
    user = make_user(None, wallet_chains=(SEPOLIA, BASE))
    db.save_telegram_link_nonce("nonce-1004", user, int(time.time()) + 600)

    replies, jobs = run(telebot.start, 1004, SEPOLIA_BOT, args=["nonce-1004"])
    check("the Sepolia bot links the chat", db.get_user_id_by_telegram_chat_id(1004) == user)
    check("and says so", any(r.startswith("✅ Telegram linked") for r in replies), str(replies))

    replies, jobs = run(telebot.start, 1004, BASE_BOT)
    check("the Base bot knows the chat with no link of its own",
          any(r.startswith("Welcome to Mitfah on Base.") for r in replies), str(replies))
    check("and looks at the Base wallet straight away",
          [(callback, kwargs["chat_id"], kwargs["data"]) for callback, kwargs in jobs]
          == [(telebot.first_check, 1004, user)], str(jobs))

    replies, jobs = run(telebot.start, 1004, ARBITRUM_BOT)
    check("a bot whose chain has no wallet says so",
          len(replies) == 1 and "don't have a wallet on Arbitrum One" in replies[0], str(replies))
    check("and schedules no check", jobs == [], str(jobs))

    replies, _ = run(telebot.start, 1005, BASE_BOT)
    check("an unlinked chat is told how to link", replies == [telebot.UNLINKED_MESSAGE], str(replies))


def telegram_update(chat_type: str, edited: bool = False) -> Update:
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=5, type=chat_type), text="hello"
    )
    return Update(update_id=1, edited_message=message) if edited else Update(update_id=1, message=message)


def test_private_chats_only():
    print("\n[3] only new messages in private chats are answered")
    bot = telebot.build_bot("123:test-token", BASE, "base")
    check("the bot carries its chain", bot.bot_data == BASE_BOT, str(bot.bot_data))
    check("it has a daily check", len(bot.job_queue.get_jobs_by_name("daily-check")) == 1)

    handlers = bot.handlers[0]
    check("it has the three handlers", len(handlers) == 3, str(handlers))
    for handler in handlers:
        commands = getattr(handler, "commands", None)
        name = f"/{min(commands)}" if commands else handler.callback.__name__
        check(f"{name}: a private chat is answered",
              bool(handler.filters.check_update(telegram_update(Chat.PRIVATE))))
        for chat_type in (Chat.GROUP, Chat.SUPERGROUP, Chat.CHANNEL):
            check(f"{name}: a {chat_type} is not",
                  not handler.filters.check_update(telegram_update(chat_type)))
        check(f"{name}: nor an edited message",
              not handler.filters.check_update(telegram_update(Chat.PRIVATE, edited=True)))


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

    user_chat = 1001   # linked in [1], with a Base wallet
    reply_for_turns["text"] = text
    replies, _ = run(telebot.start_chat, user_chat, BASE_BOT, "list everything")
    check("the handler sends every piece", "\n".join(replies) == text and len(replies) > 1)
    reply_for_turns["text"] = ""
    replies, _ = run(telebot.start_chat, user_chat, BASE_BOT, "say nothing")
    check("an empty answer still gets a reply", replies == [telebot.EMPTY_REPLY], str(replies))
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

    reply_for_turns["text"] = "**Done.** See [the tx](https://etherscan.io/tx/0x1)."
    replies, _ = run(telebot.start_chat, 1001, BASE_BOT, "send it")
    check("the bot sends the reply formatted",
          replies == ['<b>Done.</b> See <a href="https://etherscan.io/tx/0x1">the tx</a>.'], str(replies))
    check("as HTML, with no link preview fetched",
          reply_options == [{"parse_mode": ParseMode.HTML, "link_preview_options": telebot.NO_PREVIEW}]
          and telebot.NO_PREVIEW.is_disabled, str(reply_options))

    replies, _ = run(telebot.start_chat, 1001, BASE_BOT, "send it", refuse_formatting=True)
    check("if Telegram refuses the formatting, the same words go out plain",
          replies == ["Done. See the tx."] and "parse_mode" not in reply_options[0], f"{replies} {reply_options}")
    reply_for_turns["text"] = "Done."


def test_wallet_warnings():
    print("\n[5] the warnings say what needs doing")
    now = 1_000_000
    healthy = {"session_active": True, "session_expires_at": now + 30 * 86_400,
               "daily_limit_usd": 100.0, "remaining_usd": 80.0}

    check("all is well: nothing to say", telebot.wallet_warnings(healthy, "Base", now) == [])

    warnings = telebot.wallet_warnings({**healthy, "session_active": False}, "Base", now)
    check("an inactive key: one warning, naming the chain and where to fix it",
          len(warnings) == 1 and "your Base wallet is off" in warnings[0] and "Controls → Assistant" in warnings[0],
          str(warnings))

    for left, when in ((2 * 86_400 + 3_600, "in 2 days"), (86_400 + 60, "in 1 day"), (3_600, "in under a day")):
        warnings = telebot.wallet_warnings({**healthy, "session_expires_at": now + left}, "Base", now)
        check(f"a key running out {when} is mentioned",
              len(warnings) == 1 and f"runs out {when}." in warnings[0], str(warnings))

    warnings = telebot.wallet_warnings({**healthy, "remaining_usd": 5.0}, "Base", now)
    check("a low budget is mentioned, with the figures",
          len(warnings) == 1 and "$5.00 of your $100.00 limit" in warnings[0], str(warnings))


def test_daily_check():
    print("\n[6] the daily check reaches each linked chat with a wallet on the bot's chain")
    low = {"session_active": True, "session_expires_at": int(time.time()) + 30 * 86_400,
           "daily_limit_usd": 100.0, "remaining_usd": 1.0}
    fine = {**low, "remaining_usd": 90.0}

    gone = make_user(2001, wallet_chains=(ARBITRUM,))     # blocked the bot
    flaky = make_user(2002, wallet_chains=(ARBITRUM,))    # Telegram times out
    low_user = make_user(2003, wallet_chains=(ARBITRUM,))
    fine_user = make_user(2004, wallet_chains=(ARBITRUM,))
    make_user(2005, wallet_chains=(SEPOLIA,))            # no wallet on Arbitrum
    make_user(None, wallet_chains=(ARBITRUM,))           # not linked

    check("the bot's list is the linked chats with a wallet on its chain",
          db.get_telegram_chats_on_chain(ARBITRUM)
          == [(gone, 2001), (flaky, 2002), (low_user, 2003), (fine_user, 2004)],
          str(db.get_telegram_chats_on_chain(ARBITRUM)))

    read = []
    original = telebot._read_wallet_status

    def fake_status(user_id, chain_id, network):
        read.append((user_id, chain_id, network))
        return fine if user_id == fine_user else low

    telebot._read_wallet_status = fake_status
    try:
        bot = FakeBot(errors={2001: Forbidden("bot was blocked by the user"), 2002: TimedOut()})
        asyncio.run(telebot.daily_check(SimpleNamespace(bot_data=ARBITRUM_BOT, bot=bot)))
        check("every one of them is read, on the bot's own network",
              read == [(u, ARBITRUM, "arbitrum") for u in (gone, flaky, low_user, fine_user)], str(read))
        check("a blocked or unreachable chat doesn't stop the others; only the low budget is warned",
              [chat for chat, _ in bot.sent] == [2003] and "Low spending budget on Arbitrum One" in bot.sent[0][1],
              str(bot.sent))

        bot = FakeBot()
        job = SimpleNamespace(chat_id=2003, data=low_user)
        asyncio.run(telebot.first_check(SimpleNamespace(bot_data=ARBITRUM_BOT, bot=bot, job=job)))
        check("the check after /start warns that one chat", [chat for chat, _ in bot.sent] == [2003], str(bot.sent))
    finally:
        telebot._read_wallet_status = original

    # The real read makes the bot's chain the acting network, whatever the user last deployed on.
    seen = []
    original = telebot._get_wallet_status
    telebot._get_wallet_status = lambda user_id: seen.append(db.get_user_network(user_id)) or fine
    try:
        status = telebot._read_wallet_status(low_user, BASE, "base")
        check("the wallet is read on the bot's chain", seen == ["base"] and status == fine, str(seen))
    finally:
        telebot._get_wallet_status = original


def test_network_check():
    print("\n[7] a bot starts only when its network answers as its chain")
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
        check("the right chain: it starts", telebot.check_network(BASE, "base") is None)

        telebot.load_network_config_by_name = network_answering(1)
        reason = telebot.check_network(BASE, "base")
        check("another chain: it does not", reason == "the RPC for base is chain 1, not 8453", str(reason))

        telebot.load_network_config_by_name = network_answering(
            ConnectionError("https://base-mainnet.example/v2/SECRET-KEY refused"))
        reason = telebot.check_network(BASE, "base")
        check("no answer: it does not, and the RPC's URL stays out of the reason",
              reason == "can't reach base (ConnectionError)", str(reason))

        def missing(name):
            raise ValueError(f"Chain name '{name}' not found in database")

        telebot.load_network_config_by_name = missing
        reason = telebot.check_network(BASE, "base-fork")
        check("a network the database lacks: it does not", reason == "Chain name 'base-fork' not found in database",
              str(reason))
    finally:
        telebot.load_network_config_by_name = original

    saved = {name: os.environ.get(name) for name in ("MITFAH_ETH_API", "MITFAH_SEPOLIA_API", "MITFAH_BSC_API",
                                                     "MITFAH_ARB_API", "MITFAH_BASE_API")}
    try:
        os.environ.update({name: "" for name in saved})
        os.environ["MITFAH_BASE_API"] = "123:base"
        os.environ["MITFAH_SEPOLIA_API"] = "456:sepolia"
        check("only chains with a token get a bot, in a fixed order",
              telegram_bots.chains_with_bots() == [SEPOLIA, BASE], str(telegram_bots.chains_with_bots()))
        check("a missing token is reported by its .env name", telegram_bots.token_env(ARBITRUM) == "MITFAH_ARB_API")
    finally:
        for name, value in saved.items():
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
        return "ok"

    first = make_user(3001, wallet_chains=(BASE,))
    second = make_user(3002, wallet_chains=(BASE,))
    telebot.chat = slow_chat
    telebot._turn_locks.clear()

    async def both():
        def message(chat_id, text):
            async def reply_text(_, **options):
                pass
            update = SimpleNamespace(message=SimpleNamespace(chat_id=chat_id, text=text, reply_text=reply_text))
            context = SimpleNamespace(args=[], bot_data=BASE_BOT, bot=FakeBot())
            return telebot.start_chat(update, context)

        await asyncio.gather(message(3001, "send 1 ETH to Sam"), message(3001, "yes"), message(3002, "hi"))

    try:
        asyncio.run(both())
        check("one user's two messages never run at once", most[(BASE, first)] == 1, str(most))
        check("while another user's runs beside them", most["all"] == 2 and most[(BASE, second)] == 1, str(most))
    finally:
        telebot.chat = fake_chat
        telebot._turn_locks.clear()


if __name__ == "__main__":
    try:
        test_each_bot_acts_on_its_own_chain()
        test_one_link_covers_every_bot()
        test_private_chats_only()
        test_replies_fit_telegram()
        test_replies_keep_their_formatting()
        test_wallet_warnings()
        test_daily_check()
        test_network_check()
        test_one_turn_at_a_time_per_chat()
    finally:
        os.unlink(_tmp_db.name)

    finish("All Telegram bot checks passed.")
