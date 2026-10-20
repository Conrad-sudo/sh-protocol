"""
How much one account may ask of the assistant: the limits the web app and the Telegram bot share.

Every message to the assistant is a model call, paid for in credits, that holds a worker thread for
10-60 seconds while the agent calls tools. Nobody types that fast, so these limits only stop a
script -- and cap what one account can spend in a day.

The web route rations with slowapi (routes/chat.py), which only knows HTTP requests. The bot uses
TurnGate below, which reads the same CHAT_RATE_LIMIT with the same parser -- limits' parse_many, the
one slowapi calls -- so both front ends ration by one number, written one way.

Like slowapi's, these counts are in memory and per process. They start again on a restart, and the
web app and the bot count separately, so one account's worst case is twice CHAT_RATE_LIMIT. A count
both share needs Redis or a database counter.
"""
import math
import os
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, Iterator, NoReturn

from limits import RateLimitItem, parse_many

# Per account. A turn takes several seconds and an account runs one at a time, so a person stays
# well under the minute's limit even counting quote-card taps, which count here too: it stops a
# script. The day's caps what one account can spend of the model's credits. slowapi reads it on the
# web and TurnGate in the bot, so it takes slowapi's syntax: "20/minute", "1000 per day", joined by ";".
CHAT_RATE_LIMIT = "20/minute;1000/day"

# How many messages from one account the bot holds behind the one the assistant is answering. One,
# so a follow-up sent before the reply is still answered after it. Past that a message is refused,
# not queued: a queue is what lets a flood pile up turns that each cost a model call.
TELEGRAM_MAX_WAITING = 1

# How many Telegram turns may run at once, across every account: the bot's CHAT_MAX_CONCURRENT.
# DeepSeek sets no rate limit (under load it answers slower rather than refusing), so the ceiling is
# this one process, whose CPU every turn shares, and the chain RPC's request limit: a turn makes a
# dozen or more reads.
# Measured 2026-10-19 (make load-test, and turns on a mainnet fork): a turn that reads the chain
# costs this process 50-200 ms of CPU (a swap quote 200), so one core is full at about 35-60 such
# turns at once. Past that nobody is refused but every turn slows, so the cap sits just above it.
TELEGRAM_MAX_CONCURRENT = int(os.getenv("TELEGRAM_MAX_CONCURRENT", "64"))

# How many quote-card taps (Confirm or Cancel) may run at once, in the web app and in the bot,
# each on threads of their own. Apart from the turns, so a tap never waits behind other people's
# model calls -- a quote lasts only QUOTE_TTL_SECONDS -- and short: a Confirm returns once its
# transaction is on the wire, and the wait for it runs in landing.py. 16 until 2026-10-20, when the
# bundler sent one op per transaction; now the taps waiting share the next bundle (bundler.Lane).
CONFIRM_MAX_CONCURRENT = int(os.getenv("CONFIRM_MAX_CONCURRENT", "64"))

# A refused account is told why at most once per this many seconds for each kind of refusal, so a
# flood gets one reply, not one per message.
REFUSAL_NOTICE_SECS = 60

_BUSY_TEXT = "I'm still working on your last message — wait for my reply, then send the next one."

# A limit whose window is a day or longer is the daily allowance, in a refusal's words; a shorter
# one is the "slow down" kind.
_DAY_SECS = 86_400

# How often TurnGate walks every account to forget the idle ones. Not on every message: the walk
# grows with the number of accounts.
_SWEEP_SECS = 60


class TurnRefused(Exception):
    """
    A message the assistant won't take now.

    @param text    The reply that says why, ready to send.
    @param notify  False when this account was already sent this kind of refusal within
                   REFUSAL_NOTICE_SECS. The message is refused all the same, just without a reply.
    """

    def __init__(self, text: str, notify: bool = True):
        super().__init__(text)
        self.text = text
        self.notify = notify


def _in_words(secs: float) -> str:
    """A wait in plain words, rounded up so trying again then works: "a minute", "5 minutes", "about 3 hours"."""
    minutes = max(1, math.ceil(secs / 60))
    if minutes == 1:
        return "a minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours = math.ceil(minutes / 60)
    return "about an hour" if hours == 1 else f"about {hours} hours"


class TurnGate:
    """
    Decides whether a message may start a turn: the bot's rationing, as slowapi and _chat_turn are
    the web route's (routes/chat.py).

    Two checks, in this order:
      1. Busy. An account has at most one turn running and `max_waiting` waiting behind it, across
         every chain. A message past that is refused before the rate is looked at, so it spends
         nothing: it never reached the assistant.
      2. Rate. A moving window for each limit in `rate`: a message stops counting toward a limit
         once that limit's window has passed since it came. It is admitted only if every limit has
         room, and only then counted -- once, toward all of them -- so a refused message counts
         toward none.

    No lock, on purpose: the bot calls admit and release on its event loop only -- one thread -- and
    neither awaits, so no other message is handled between a check and the count it guards. Call
    them on the loop, never from the worker thread a turn runs on.
    """

    def __init__(
        self,
        rate: str = CHAT_RATE_LIMIT,
        max_waiting: int = TELEGRAM_MAX_WAITING,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        @param rate         The limits, in slowapi's syntax ("20/minute;1000/day").
        @param max_waiting  How many turns one account may have waiting behind the one running.
        @param clock        The time in seconds, never going backwards. Tests pass their own, to move
                            time without waiting.
        @raises ValueError  For a rate slowapi would refuse too.
        """
        self._limits: list[RateLimitItem] = parse_many(rate)
        self._longest = max(item.get_expiry() for item in self._limits)
        self._max_turns = 1 + max_waiting
        self._clock = clock
        # account -> when its counted messages came, oldest first. Never longer than the longest
        # window's limit, since nothing past a limit is counted.
        self._stamps: dict[int, deque[float]] = {}
        # account -> its turns running or waiting; absent when it has none.
        self._turns: dict[int, int] = {}
        # (account, kind of refusal) -> when the account was last told it.
        self._told: dict[tuple[int, str], float] = {}
        self._swept_at = clock()

    def admit(self, user_id: int) -> None:
        """
        Counts a message from `user_id` as a turn starting, or refuses it. Each admit needs a
        release once the turn is over -- `turn` does both.

        @raises TurnRefused  While the account has as many turns running or waiting as it may, or
                             when any limit in the rate has no room left. Nothing is counted then.
        """
        now = self._clock()
        self._sweep(now)
        if self._turns.get(user_id, 0) >= self._max_turns:
            self._refuse(user_id, "busy", _BUSY_TEXT, now)

        stamps = self._stamps.get(user_id, deque())
        while stamps and now - stamps[0] >= self._longest:
            stamps.popleft()
        full = [item for item in self._limits if self._counted(stamps, item, now) >= item.amount]
        if full:
            # The longest window decides the reply: when the day's allowance is spent, "try again
            # in a minute" would be refused again in a minute.
            item = max(full, key=RateLimitItem.get_expiry)
            wait = _in_words(self._wait(stamps, item, now))
            if item.get_expiry() >= _DAY_SECS:
                text = f"You've reached today's limit of {item.amount} messages. Try again in {wait}."
            else:
                text = f"You're sending messages faster than I can answer them. Try again in {wait}."
            self._refuse(user_id, str(item), text, now)

        stamps.append(now)
        self._stamps[user_id] = stamps
        self._turns[user_id] = self._turns.get(user_id, 0) + 1

    def release(self, user_id: int) -> None:
        """Ends a turn that admit let start. Releasing an account with no turn does nothing."""
        left = self._turns.pop(user_id, 0) - 1
        if left > 0:
            self._turns[user_id] = left

    @contextmanager
    def turn(self, user_id: int) -> Iterator[None]:
        """
        Holds one of `user_id`'s turns for the body of the `with`, however it ends.

        @raises TurnRefused  From admit, before the body runs; nothing is held then.
        """
        self.admit(user_id)
        try:
            yield
        finally:
            self.release(user_id)

    @staticmethod
    def _counted(stamps: deque[float], item: RateLimitItem, now: float) -> int:
        """How many of the account's messages `item` still counts: those inside its window."""
        window = item.get_expiry()
        return sum(1 for t in stamps if now - t < window)

    @staticmethod
    def _wait(stamps: deque[float], item: RateLimitItem, now: float) -> float:
        """
        Seconds until `item` has room again: until enough of the messages it counts leave its
        window to bring the count below its limit.
        """
        window = item.get_expiry()
        counted = [t for t in stamps if now - t < window]
        i = len(counted) - item.amount
        return counted[i] + window - now if 0 <= i < len(counted) else window

    def _refuse(self, user_id: int, kind: str, text: str, now: float) -> NoReturn:
        """Raises TurnRefused, with notify set only if the account hasn't been told `kind` lately."""
        told = self._told.get((user_id, kind))
        notify = told is None or now - told >= REFUSAL_NOTICE_SECS
        if notify:
            self._told[(user_id, kind)] = now
        raise TurnRefused(text, notify)

    def _sweep(self, now: float) -> None:
        """
        Forgets every account's messages that no window counts any more, and refusals old enough to
        be told again, so an account that stops writing leaves nothing behind. At most once per
        _SWEEP_SECS: it walks every account.
        """
        if now - self._swept_at < _SWEEP_SECS:
            return
        self._swept_at = now
        for user_id in [u for u, s in self._stamps.items() if not s or now - s[-1] >= self._longest]:
            del self._stamps[user_id]
        for key in [k for k, t in self._told.items() if now - t >= REFUSAL_NOTICE_SECS]:
            del self._told[key]
