"""
Pending transactions: what stands between the agent describing a transaction and the chain
executing one.

Every write tool now stops at a quote. It builds the calldata, prices the whole UserOperation
against the chain, and parks the result here under a random id; nothing is signed and nothing is
sent. A separate confirm_transaction(quote_id) tool is the only thing in the app that broadcasts,
and it sends the bytes that were parked -- not whatever the model says they were.

That split is the point. Before it, the only thing standing between an instruction the model read
somewhere and a transaction was the system prompt asking it to wait for a yes, which is advice, not
a control: text in a message, a token name, or a tool result could talk the model into treating a
"yes" as covering a different transaction, and nothing in the code disagreed. Now:

  - The transaction is fixed before the user sees it. A quote is the exact executions and gas the
    confirm will use, so there is no re-describing step for an instruction to sit in. The worst a
    confirmed quote can do is what its quote said.
  - Confirming takes a real turn boundary. {take} refuses a quote raised in the turn that is
    confirming it, so text arriving WITHIN one turn -- a tool result, a token name, a registration
    file, anything the model read rather than the user typed -- cannot quote and confirm on its own.
    The user has to send another message for the turn counter to move.
  - Quotes go stale. A quote is single-use and expires, so one cannot be raised quietly and
    redeemed later in the conversation.
  - Asking again is not agreeing. A typed message that repeats the request a quote was raised
    by, word for word, cannot confirm it ({repeats}): the user may not have seen that quote at all
    -- the page failed to show it, or they never read it -- so they are quoted afresh and asked.

What it does NOT do: a user who reads the quote and approves it without looking is still approving
whatever it says. This makes the agent's claims checkable against code-written text and bounds a
confirmation to one transaction; it does not decide for the user. See THREAT_MODEL 4.2.

The store is per-process and in memory, deliberately. A signed op is never what is held -- only the
inputs -- but a pending transaction is still a live intent, and it should die with the process that
made it rather than outlive a restart in a database. The practical consequence is that a quote
raised in the web app cannot be confirmed from Telegram, which run as separate processes: the
second one reports an unknown quote and the user asks again.
"""
import secrets
import threading
import time
from dataclasses import dataclass

from bundler import UserOpQuote

# How long a quote stays good. Long enough to read a confirmation and reply, short enough that the
# gas price, the oracle price and the spending cap behind it are still the ones quoted.
QUOTE_TTL_SECONDS = 300


class QuoteError(Exception):
    """A quote that cannot be confirmed, with a sentence the agent can relay to the user."""


@dataclass
class PendingTransaction:
    """One quoted, unsigned transaction, waiting for the user to agree to it."""

    quote_id: str
    user_id: int
    chain_id: int
    turn_id: int            # the conversation turn this was quoted in; confirming needs a later one
    created_at: float
    action: str             # what this does, in English, written here rather than by the model
    calls: list[dict]       # the executions, as {"to", "value", "data"}, for display and audit
    quote: UserOpQuote
    cost: dict              # the figures shown to the user; see tools._transaction_cost
    # A liquidity deposit's pool, as {"token_a", "ticker_a", "token_b", "ticker_b"}: confirming
    # puts it on the dashboard (db.save_lp_token). None for anything else.
    lp_pool: dict | None = None
    # What it is meant to do, 'in' or 'out', for the History tab's groups while it is pending or if
    # it fails (tx_history.start_assistant_tx). None when the quote can't say.
    direction: str | None = None
    # Shown on a card with Confirm and Cancel buttons, once the turn that raised it was over (offer).
    offered: bool = False
    # The user's message in the turn that raised it (AgentContext.message): the same words again
    # don't confirm it (repeats). Empty when that turn had none.
    request: str = ""

    @property
    def age(self) -> float:
        return time.time() - self.created_at


_pending: dict[str, PendingTransaction] = {}
_lock = threading.Lock()


def _prune(now: float) -> None:
    """Drop expired quotes. Called under _lock, on every access, so nothing accumulates."""
    for quote_id in [
        q for q, p in _pending.items() if now - p.created_at > QUOTE_TTL_SECONDS
    ]:
        del _pending[quote_id]


def put(
    user_id: int,
    chain_id: int,
    turn_id: int,
    action: str,
    calls: list[dict],
    quote: UserOpQuote,
    cost: dict,
    lp_pool: dict | None = None,
    direction: str | None = None,
    request: str = "",
) -> PendingTransaction:
    """
    Parks a quoted transaction and returns it, with the id the user will confirm against.

    @return  The stored PendingTransaction. Its quote_id is safe to show: it names a transaction
             that only this user, in a later turn, can confirm.
    """
    now = time.time()
    # token_urlsafe, not a counter: an id that could be guessed would let one conversation confirm
    # a transaction quoted in another, and the turn check alone would not catch that.
    pending = PendingTransaction(
        quote_id=secrets.token_urlsafe(9),
        user_id=user_id,
        chain_id=chain_id,
        turn_id=turn_id,
        created_at=now,
        action=action,
        calls=calls,
        quote=quote,
        cost=cost,
        lp_pool=lp_pool,
        direction=direction,
        request=request,
    )
    with _lock:
        _prune(now)
        _pending[pending.quote_id] = pending
    return pending


def _words(message: str) -> list[str]:
    """A message as repeats compares it: its words, ignoring case, spacing and closing punctuation."""
    return message.casefold().strip().rstrip(".!?,;:").split()


def repeats(user_id: int, quote_id: str, message: str) -> bool:
    """
    Whether `message` asks for a quote's transaction again rather than agreeing to it: the same
    words as the message that raised it. Such a message is a new request -- the user may never have
    seen the quote it would confirm (2026-10-20: a page that lost a quote, its user asking again, and
    the model sending the old quote as if they had said yes) -- so the quote is dropped, and
    confirm_transaction has them quoted afresh and asked.

    A reply of one or two words ("yes", "go ahead") is never a repeat, though it raised the quote:
    otherwise a quote raised by a "yes" to a question could never be confirmed by a "yes". A reworded
    request is not caught here; the system prompt covers it.

    @return  True, and the quote dropped, if it is a repeat.
    """
    asked = _words(message)
    if len(asked) <= 2:
        return False
    with _lock:
        pending = _pending.get(quote_id)
        if pending is None or pending.user_id != user_id or _words(pending.request) != asked:
            return False
        del _pending[quote_id]
    return True


def take(user_id: int, chain_id: int, quote_id: str, turn_id: int) -> PendingTransaction:
    """
    Claims a quote for sending, removing it from the store.

    Single-use on purpose: a broadcast that fails may still have landed (a rival bundler can lift
    the op out and submit it first), so a quote must never be replayable after one attempt. The
    user re-asks and is re-quoted against fresh prices.

    @param user_id   The user confirming, from the runtime context -- never from the model.
    @param chain_id  The chain they are on now; a quote from another chain is not theirs to send.
    @param quote_id  The id from the quote.
    @param turn_id   The current conversation turn.
    @return          The pending transaction, no longer in the store.
    @raises QuoteError if it is unknown, expired, someone else's, or was quoted in this same turn.
    """
    now = time.time()
    with _lock:
        _prune(now)
        pending = _pending.get(quote_id)
        # Same message whether the id is unknown, expired or another user's: distinguishing them
        # would tell a caller which ids exist.
        if pending is None or pending.user_id != user_id or pending.chain_id != chain_id:
            raise QuoteError(
                f"There is no pending transaction {quote_id!r} to confirm. It may have expired "
                f"(quotes last {QUOTE_TTL_SECONDS // 60} minutes), or already been sent. Ask the "
                f"user what they want to do and quote it again — do not retry this id."
            )
        if pending.turn_id >= turn_id:
            raise QuoteError(
                "This transaction was quoted in this same turn, so the user has not seen the cost "
                "yet. Show them the quote — what it does and what it costs — and wait for them to "
                "reply before confirming. Nothing was sent."
            )
        del _pending[quote_id]
    return pending


def drop(user_id: int, quote_id: str, chain_id: int | None = None) -> bool:
    """
    Discards a quote the user decided against. True if there was one to discard.

    @param chain_id  When given, only a quote on that chain is discarded: a Cancel button acts on
                     the network the user is on now, as its Confirm would.
    """
    with _lock:
        pending = _pending.get(quote_id)
        if pending is None or pending.user_id != user_id:
            return False
        if chain_id is not None and pending.chain_id != chain_id:
            return False
        del _pending[quote_id]
        return True


def raised_in(user_id: int, chain_id: int, turn_id: int) -> list[PendingTransaction]:
    """
    The quotes the user's turn `turn_id` on `chain_id` raised and that are still waiting, oldest
    first. A turn that raised exactly one gets Confirm and Cancel buttons ({offer}).

    A copy of the store's entries, not a claim on them: taking one still goes through {take}.
    """
    with _lock:
        _prune(time.time())
        found = [
            p for p in _pending.values()
            if p.user_id == user_id and p.chain_id == chain_id and p.turn_id == turn_id
        ]
    return sorted(found, key=lambda p: p.created_at)


def offer(user_id: int, chain_id: int, turn_id: int | None) -> PendingTransaction | None:
    """
    The quote to put Confirm and Cancel buttons under once a turn is over: the one it raised, when
    it raised exactly one. With several, the user says which in words; with none, or a turn that
    failed (no id, so it has shown the user no quote), there is nothing to offer. Marks it offered,
    so the web chat can show its card again after a reload ({offered}).

    The buttons only name the quote. Confirming one still goes through {take}.
    """
    if turn_id is None:
        return None
    raised = raised_in(user_id, chain_id, turn_id)
    if len(raised) != 1:
        return None
    with _lock:
        raised[0].offered = True
    return raised[0]


def offered(user_id: int, chain_id: int) -> PendingTransaction | None:
    """
    The newest quote on `chain_id` that was offered with buttons ({offer}) and is still waiting:
    the card the web chat shows. None when there is none.
    """
    with _lock:
        _prune(time.time())
        found = [p for p in _pending.values() if p.user_id == user_id and p.chain_id == chain_id and p.offered]
    return max(found, key=lambda p: p.created_at, default=None)


def fee_text(cost: dict) -> str:
    """
    A quote's fees, network and Mitfah's together, as one figure: "$0.35", or in the native asset
    when it couldn't be priced. What a Confirm card shows, in Telegram and on the web.
    """
    if "total_usd" in cost:
        usd = cost["total_usd"]
        return "under $0.01" if usd < 0.01 else f"${usd:,.2f}"
    native = f"{cost['total_native']:.8f}".rstrip("0").rstrip(".")
    return f"{native} {cost['native_asset']}"


def has_pending(user_id: int, chain_id: int) -> bool:
    """
    Whether the user has a quote on `chain_id` still waiting for an answer.

    The conversation is not started afresh while one is (smart_wallet_agent.chat): the quote's
    description and id live only there, so clearing it would leave the user's next "yes" with
    nothing to confirm.
    """
    with _lock:
        _prune(time.time())
        return any(p.user_id == user_id and p.chain_id == chain_id for p in _pending.values())


def drop_all(user_id: int, chain_id: int) -> int:
    """
    Discards every quote the user has on `chain_id`: their chat there was just deleted, and a quote
    must not outlive the conversation that showed it.

    @return  How many were discarded.
    """
    with _lock:
        doomed = [q for q, p in _pending.items() if p.user_id == user_id and p.chain_id == chain_id]
        for quote_id in doomed:
            del _pending[quote_id]
        return len(doomed)
