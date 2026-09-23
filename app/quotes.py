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
    key_ciphertext: str     # held so confirming does not have to re-fetch or re-pass it
    quote: UserOpQuote
    cost: dict              # the figures shown to the user; see tools._transaction_cost

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
    key_ciphertext: str,
    quote: UserOpQuote,
    cost: dict,
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
        key_ciphertext=key_ciphertext,
        quote=quote,
        cost=cost,
    )
    with _lock:
        _prune(now)
        _pending[pending.quote_id] = pending
    return pending


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


def drop(user_id: int, quote_id: str) -> bool:
    """Discards a quote the user decided against. True if there was one to discard."""
    with _lock:
        pending = _pending.get(quote_id)
        if pending is None or pending.user_id != user_id:
            return False
        del _pending[quote_id]
        return True
