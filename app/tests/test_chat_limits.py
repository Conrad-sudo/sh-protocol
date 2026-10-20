"""
Checks the rationing of messages to the assistant (chat_limits): the moving-window limits, the cap
on turns running or waiting per account, one refusal notice per kind per REFUSAL_NOTICE_SECS, and
that the web route still rations by the same CHAT_RATE_LIMIT the bot does.

Time is a fake clock the tests move by hand, so nothing waits. Offline: no RPC, no model call.

Run: make chat-limits-test   (or: python app/tests/test_chat_limits.py)
"""
import os
import tempfile

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

# The last check imports the web route, which pulls in the agent and the database layer. A scratch
# database and placeholder keys, set before any app module imports, keep that offline.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
# smart_wallet_agent builds its model client at import; nothing here ever calls it.
os.environ.setdefault("DEEPSEEK_API_KEY", "offline-test-placeholder")

from limits import parse_many       # noqa: E402

import chat_limits                  # noqa: E402
from chat_limits import CHAT_RATE_LIMIT, REFUSAL_NOTICE_SECS, TurnGate, TurnRefused  # noqa: E402

USER, OTHER = 1, 2
MINUTE, DAY = 60, 86_400


class Clock:
    """Seconds that only move when a test says so."""

    def __init__(self):
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


# The gate's workings are checked at these numbers rather than CHAT_RATE_LIMIT's, so retuning that
# moves no check here; [11] checks the web route and the bot both take CHAT_RATE_LIMIT.
RATE = "10/minute;300/day"


def _gate(**kwargs) -> tuple[TurnGate, Clock]:
    clock = Clock()
    kwargs.setdefault("rate", RATE)
    return TurnGate(clock=clock, **kwargs), clock


def _send(gate: TurnGate, user_id: int = USER) -> TurnRefused | None:
    """One message whose turn ends straight away: None if it was admitted, else the refusal."""
    try:
        with gate.turn(user_id):
            pass
    except TurnRefused as refused:
        return refused
    return None


def _start(gate: TurnGate, user_id: int = USER) -> TurnRefused | None:
    """One message whose turn is left running (admit, no release): None if admitted, else the refusal."""
    try:
        gate.admit(user_id)
    except TurnRefused as refused:
        return refused
    return None


def _send_many(gate: TurnGate, count: int, user_id: int = USER) -> int:
    """Sends `count` messages at the clock's current time and returns how many were admitted."""
    return sum(_send(gate, user_id) is None for _ in range(count))


def test_minute_limit():
    print("\n[1] up to the minute's limit is admitted; the next is refused")
    gate, _ = _gate()
    check("10 messages in a minute are all admitted", _send_many(gate, 10) == 10)
    refused = _send(gate)
    text = refused.text if refused else ""
    check("the 11th is refused", refused is not None)
    check("...saying to try again in a minute", "faster than I can answer them. Try again in a minute." in text, text)
    check("...not that the day is used up", "today" not in text, text)
    check("...and that refusal is sent", refused is not None and refused.notify)
    check("another account is not held back by it", _send(gate, OTHER) is None)


def test_window_expiry():
    print("\n[2] a message stops counting once its window has passed")
    gate, clock = _gate()
    _send_many(gate, 10)
    clock.now += MINUTE - 1
    check("a second before the minute is up, still refused", _send(gate) is not None)
    clock.now += 1
    check("once it is up, the next 10 are admitted", _send_many(gate, 10) == 10)
    check("...and the 11th refused again", _send(gate) is not None)


def test_day_limit():
    print("\n[3] the day's limit, reached at the minute's pace, refuses with its own text")
    gate, clock = _gate()
    start = clock.now
    admitted = 0
    for _ in range(30):              # 10 a minute for 30 minutes: never past the minute's limit
        admitted += _send_many(gate, 10)
        clock.now += MINUTE
    check("300 messages over 30 minutes are all admitted", admitted == 300, str(admitted))
    refused = _send(gate)
    text = refused.text if refused else ""
    check("the 301st, with the minute's window clear, is refused", refused is not None)
    check("...by today's limit, which it names", "today's limit of 300 messages" in text, text)
    check("...not as sending too fast", "faster than I can answer" not in text, text)
    check("...saying when to try again", "Try again in about 24 hours" in text, text)

    clock.now = start + DAY - 1
    refused = _send(gate)
    check("a second short of a day after the first messages, still refused by the day",
          refused is not None and "today's limit" in refused.text, refused and refused.text)
    clock.now = start + DAY
    check("a day after them, their 10 slots come back", _send_many(gate, 10) == 10)


def test_both_limits_full():
    print("\n[4] when both limits are full, the reply is the day's")
    gate, _ = _gate(rate="5/minute;5/day")
    _send_many(gate, 5)
    refused = _send(gate)
    check("the day's text, since in a minute it would still be refused",
          refused is not None and "today's limit of 5 messages" in refused.text, refused and refused.text)


def test_refused_messages_count_nothing():
    print("\n[5] a refused message counts toward no limit")
    gate, clock = _gate()
    _send_many(gate, 10)
    check("50 more in the same minute are all refused", _send_many(gate, 50) == 0)
    clock.now += MINUTE
    check("a minute on there is room for 10, as if they were never sent", _send_many(gate, 10) == 10)

    # Every limit is tested before any counts the message: the refused third message below must not
    # use up the day's last slot.
    gate, clock = _gate(rate="2/minute;3/day")
    _send_many(gate, 2)
    check("past the minute's limit -> refused", _send(gate) is not None)
    clock.now += MINUTE
    check("the day still has its third slot", _send(gate) is None)
    refused = _send(gate)
    check("...and then the day is full", refused is not None and "today's limit of 3" in refused.text,
          refused and refused.text)


def test_busy_cap():
    print("\n[6] one turn running and one waiting per account; the next is refused, spending nothing")
    gate, _ = _gate(rate="3/minute;300/day")
    check("a first message starts a turn", _start(gate) is None)
    check("a second, while it runs, may wait", _start(gate) is None)
    refused = _start(gate)
    check("a third is refused", refused is not None)
    check("...saying the assistant is still on the last one",
          refused is not None and "still working on your last message" in refused.text, refused and refused.text)
    check("another account still gets in", _send(gate, OTHER) is None)

    check("5 more while both are held are all refused", sum(_start(gate) is None for _ in range(5)) == 0)
    gate.release(USER)
    gate.release(USER)
    check("once both turns end, the minute's third is admitted: the busy refusals spent nothing",
          _send(gate) is None)
    refused = _send(gate)
    check("...and the fourth is past the limit", refused is not None and "faster than I can answer" in refused.text,
          refused and refused.text)

    gate, _ = _gate(max_waiting=0)
    gate.admit(USER)
    check("with no waiting allowed, a message during a turn is refused", _send(gate) is not None)
    check("...and turn() refused releases nothing: the running turn still holds", _send(gate) is not None)
    gate.release(USER)
    check("once it ends the account is free", _send(gate) is None)


def test_one_notice_per_kind():
    print(f"\n[7] each kind of refusal is sent at most once per {REFUSAL_NOTICE_SECS}s")
    gate, clock = _gate(rate="2/minute;300/day", max_waiting=0)
    gate.admit(USER)
    first, second = _send(gate), _send(gate)
    check("the first busy refusal is sent", first is not None and first.notify)
    check("the next is quiet", second is not None and not second.notify)
    clock.now += REFUSAL_NOTICE_SECS / 2
    quiet = _send(gate)
    check("still quiet half-way through", quiet is not None and not quiet.notify)
    gate.release(USER)

    check("the second message of the minute is admitted", _send(gate) is None)
    fast = _send(gate)
    check("a rate refusal is another kind, so it is sent",
          fast is not None and fast.notify and "faster than I can answer" in fast.text, fast and fast.text)
    again = _send(gate)
    check("...once", again is not None and not again.notify)

    clock.now += REFUSAL_NOTICE_SECS / 2   # REFUSAL_NOTICE_SECS since the first busy notice
    gate.admit(USER)
    busy = _send(gate)
    check("the busy refusal is sent again once the time is up -- the quiet ones didn't push it back",
          busy is not None and busy.notify)
    gate.admit(OTHER)
    other = _send(gate, OTHER)
    check("another account is told about its own refusals", other is not None and other.notify)


def test_release_after_exception():
    print("\n[8] a turn that raises still releases the account")
    gate, _ = _gate(max_waiting=0)
    try:
        with gate.turn(USER):
            raise RuntimeError("the agent failed")
    except RuntimeError:
        pass
    check("the next message is admitted, not refused as busy", _send(gate) is None)

    gate.release(USER)                  # no turn to end
    gate.admit(USER)
    check("a release with no turn frees nothing extra: the cap still holds", _send(gate) is not None)


def test_idle_accounts_forgotten():
    print("\n[9] an account that stops writing leaves nothing behind")
    gate, clock = _gate(max_waiting=0)
    for _ in range(40):                 # 10 minutes past the day's limit
        _send_many(gate, 10)
        clock.now += MINUTE
    check("an account's record never outgrows the day's limit", len(gate._stamps[USER]) == 300,
          str(len(gate._stamps[USER])))
    gate.admit(OTHER)
    _send(gate, OTHER)                  # a busy refusal, remembered so it isn't told twice
    gate.release(OTHER)

    clock.now += DAY + MINUTE
    _send(gate, 3)                      # any message sweeps
    check("a day later the idle accounts' messages are gone", set(gate._stamps) == {3}, str(set(gate._stamps)))
    check("...and so is the refusal one of them was told", not gate._told, str(gate._told))
    check("...and no account holds a turn", not gate._turns, str(gate._turns))


def test_bad_rate_refused():
    print("\n[10] a rate slowapi would refuse is refused here too")
    try:
        TurnGate(rate="10 a minute")
        error = None
    except ValueError as e:
        error = str(e)
    check("TurnGate stops with a ValueError", error is not None, str(error))


def test_web_route_same_limit():
    print("\n[11] the web route rations by the same CHAT_RATE_LIMIT")
    import db
    db.DB_PATH = _tmp_db.name
    db.init_db()
    import routes.chat
    import routes.deps

    check("routes.chat takes it from chat_limits", routes.chat.CHAT_RATE_LIMIT is chat_limits.CHAT_RATE_LIMIT)
    # slowapi logs and skips a limit it can't parse, so a typo would leave the route with none at all.
    applied = [limit.limit for limit in routes.deps.limiter._route_limits.get("routes.chat.post_chat", [])]
    check("slowapi applies exactly those limits to POST /api/chat",
          applied == parse_many(CHAT_RATE_LIMIT), str([str(item) for item in applied]))
    check("...the ones the bot's gate reads by default", TurnGate()._limits == applied)


if __name__ == "__main__":
    try:
        test_minute_limit()
        test_window_expiry()
        test_day_limit()
        test_both_limits_full()
        test_refused_messages_count_nothing()
        test_busy_cap()
        test_one_notice_per_kind()
        test_release_after_exception()
        test_idle_accounts_forgotten()
        test_bad_rate_refused()
        test_web_route_same_limit()
    finally:
        os.unlink(_tmp_db.name)
    finish("ALL CHAT LIMIT CHECKS PASSED")
