"""
Pending transactions: what stands between the agent describing a transaction and the chain
executing one.

Every write tool stops at a quote. It builds the calldata, prices the whole UserOperation against
the chain, and parks the result here under a random id; nothing is signed and nothing is sent.
Nothing the model can call sends it, either. A quote is sent only when the user approves it with
their passkey -- Face ID, a fingerprint or a PIN, on their own device -- from a card the CODE writes
(signing.py): in the web chat, or behind the link in the Telegram message.

That split is the point. Before passkeys the only thing standing between an instruction the model
read somewhere and a transaction was, first, the system prompt asking it to wait for a yes, and
later a code check that a "yes" came in a later turn. Both left the decision with whoever could
type into the chat. Now:

  - The transaction is fixed before the user sees it. A quote is the exact executions and gas the
    approval will cover; once offered it is frozen in its sign request, so there is no
    re-describing step for an instruction to sit in. The worst an approved quote can do is what its
    card said.
  - Sending takes the user, not the conversation. The passkey signs the op's hash only after the
    device has verified the user, and the wallet checks that signature itself. Text arriving in a
    conversation -- a tool result, a token name, a registration file, a "yes" -- can at most put the
    card back in front of them (confirm_transaction).
  - Quotes go stale. A quote, and its sign request, expire, and a sign request is sent at most
    once, so one cannot be raised quietly and redeemed later.
  - Asking again is not agreeing. A typed message that repeats the request a quote was raised by,
    word for word, voids it ({repeats}): the user may not have seen that quote at all, so they are
    quoted afresh.

What it does NOT do: a user who approves a card without reading it is still approving whatever it
says. This makes the agent's claims checkable against code-written text and bounds an approval to
one transaction; it does not decide for the user. See THREAT_MODEL 4.2.

The store is per-process and in memory, deliberately: a quote raised but never offered dies with the
process that raised it. An OFFERED quote is also frozen in the database (sign_requests), because the
Telegram bot raises quotes in its own process while their approval page posts to the API. So this
store asks the database which of its quotes the other process has already dealt with
(_forget_closed) before saying one is still waiting.
"""
import secrets
import threading
import time
from dataclasses import dataclass

from bundler import UserOpQuote
from db import get_closed_quote_ids

# How long a quote stays good. Long enough to read a confirmation and approve it, short enough that
# the gas price, the oracle price and the spending cap behind it are still the ones quoted.
QUOTE_TTL_SECONDS = 300


class QuoteError(Exception):
    """A quote that cannot be offered, with a sentence the agent can relay to the user."""


@dataclass
class PendingTransaction:
    """One quoted, unsigned transaction, waiting for the user to approve it."""

    quote_id: str
    user_id: int
    chain_id: int
    turn_id: int            # the conversation turn this was quoted in
    created_at: float
    action: str             # what this does, in English, written here rather than by the model
    calls: list[dict]       # the executions, as {"to", "value", "data"}, for display and audit
    quote: UserOpQuote
    cost: dict              # the figures shown to the user; see tools._transaction_cost
    # A liquidity deposit's pool, as {"token_a", "ticker_a", "token_b", "ticker_b"}: sending it
    # puts it on the dashboard (db.save_lp_token). None for anything else.
    lp_pool: dict | None = None
    # What it is meant to do, 'in' or 'out', for the History tab's groups while it is pending or if
    # it fails (tx_history.start_assistant_tx). None when the quote can't say.
    direction: str | None = None
    # A swap's or a liquidity change's slippage tolerance, in basis points, for its card. None for
    # anything else.
    slippage_bps: int | None = None
    # What it counts toward the spending limit, in USD, for its card. None for a write the limit
    # doesn't meter (removing liquidity, a registry write).
    charged_usd: float | None = None
    # Shown on a card for a passkey's approval, once the turn that raised it was over (offer).
    offered: bool = False
    # The user's message in the turn that raised it (AgentContext.message): the same words again
    # don't agree to it (repeats). Empty when that turn had none.
    request: str = ""
    # The turn that asked for its card again with confirm_transaction (reoffer), if any: that
    # turn's card is this quote, whatever else it raised.
    reoffered_in: int | None = None

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def expires_at(self) -> int:
        """When it stops being good to approve, in Unix seconds."""
        return int(self.created_at + QUOTE_TTL_SECONDS)


_pending: dict[str, PendingTransaction] = {}
_lock = threading.Lock()


def _prune(now: float) -> None:
    """Drop expired quotes. Called under _lock, on every access, so nothing accumulates."""
    for quote_id in [
        q for q, p in _pending.items() if now - p.created_at > QUOTE_TTL_SECONDS
    ]:
        del _pending[quote_id]


def _forget_closed(user_id: int, quotes: list[PendingTransaction]) -> list[PendingTransaction]:
    """
    `quotes` less any whose sign request is no longer open -- sent, failed or cancelled, perhaps by
    the other process -- which are dropped from the store too. Reads the database, so it is called
    outside _lock, and only when there are quotes to ask about.
    """
    if not quotes:
        return quotes
    closed = get_closed_quote_ids(user_id, [p.quote_id for p in quotes])
    if closed:
        with _lock:
            for quote_id in closed:
                _pending.pop(quote_id, None)
    return [p for p in quotes if p.quote_id not in closed]


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
    slippage_bps: int | None = None,
    charged_usd: float | None = None,
) -> PendingTransaction:
    """
    Parks a quoted transaction and returns it, with the id its card will name.

    @return  The stored PendingTransaction. Its quote_id is safe to show: it names a transaction
             that only this user, with their passkey, can approve.
    """
    now = time.time()
    # token_urlsafe, not a counter: an id that could be guessed would let one conversation offer a
    # transaction quoted in another.
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
        slippage_bps=slippage_bps,
        charged_usd=charged_usd,
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
    seen the quote (2026-10-20: a page that lost a quote, its user asking again, and the model
    treating it as a yes) -- so the quote is dropped, and confirm_transaction has them quoted
    afresh rather than shown the old card.

    A reply of one or two words ("yes", "go ahead") is never a repeat, though it raised the quote:
    otherwise a quote raised by a "yes" to a question could never be agreed to with a "yes". A
    reworded request is not caught here; the system prompt covers it.

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


def get(user_id: int, chain_id: int, quote_id: str) -> PendingTransaction:
    """
    One of the user's quotes on `chain_id`, still waiting. Not a claim on it: approving it goes
    through its sign request.

    @param user_id   The user, from the runtime context or their own authentication -- never from
                     the model.
    @param chain_id  The chain they are on now; a quote from another chain is not theirs to offer.
    @raises QuoteError  If it is unknown, expired, someone else's, or already dealt with.
    """
    now = time.time()
    with _lock:
        _prune(now)
        pending = _pending.get(quote_id)
        # Same message whether the id is unknown, expired or another user's: distinguishing them
        # would tell a caller which ids exist.
        found = pending is not None and pending.user_id == user_id and pending.chain_id == chain_id
    if not found or not _forget_closed(user_id, [pending]):
        raise QuoteError(
            f"There is no pending transaction {quote_id!r}. It may have expired (quotes last "
            f"{QUOTE_TTL_SECONDS // 60} minutes), or already been sent or cancelled. Ask the user "
            f"what they want to do and quote it again — do not retry this id."
        )
    return pending


def reoffer(user_id: int, chain_id: int, quote_id: str, turn_id: int) -> PendingTransaction:
    """
    Puts a quote's card in front of the user again, at the end of turn `turn_id`: the quote that
    turn offers ({offer}), whatever the turn raised. How confirm_transaction answers a "yes" the
    model was given -- it can show the card, never send.

    @raises QuoteError  As get does.
    """
    pending = get(user_id, chain_id, quote_id)
    with _lock:
        pending.reoffered_in = turn_id
    return pending


def drop(user_id: int, quote_id: str, chain_id: int | None = None) -> bool:
    """
    Discards a quote the user decided against, or that was dealt with. True if there was one.

    @param chain_id  When given, only a quote on that chain is discarded: a Cancel button acts on
                     the network the user is on now.
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
    first. A turn that raised exactly one gets a card ({offer}).

    A copy of the store's entries, not a claim on them.
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
    The quote to put a card under once a turn is over: the one it raised, when it raised exactly
    one -- or else the one it asked to show again (reoffer). With several, the user says which in
    words; with none, or a turn that failed (no id, so it has shown the user no quote), there is
    nothing to offer. Marks it offered, so the web chat can show its card again after a reload
    ({offered}).

    The card only names the quote. Sending it takes the user's passkey (signing.py).
    """
    if turn_id is None:
        return None
    raised = raised_in(user_id, chain_id, turn_id)
    if len(raised) == 1:
        chosen = raised[0]
    else:
        with _lock:
            again = [
                p for p in _pending.values()
                if p.user_id == user_id and p.chain_id == chain_id and p.reoffered_in == turn_id
            ]
        chosen = max(again, key=lambda p: p.created_at, default=None)
    if chosen is None:
        return None
    with _lock:
        chosen.offered = True
    return chosen


def offered(user_id: int, chain_id: int) -> PendingTransaction | None:
    """
    The newest quote on `chain_id` that was offered on a card ({offer}) and is still waiting: the
    card the web chat shows. None when there is none.
    """
    with _lock:
        _prune(time.time())
        found = [p for p in _pending.values() if p.user_id == user_id and p.chain_id == chain_id and p.offered]
    return max(_forget_closed(user_id, found), key=lambda p: p.created_at, default=None)


def fee_text(cost: dict) -> str:
    """
    A quote's fees, network and Mitfah's together, as one figure: "$0.35", or in the native asset
    when it couldn't be priced. What a card shows, in Telegram and on the web.
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
    description and id live only there, so clearing it would leave the user's next message about it
    with nothing to refer to.
    """
    with _lock:
        _prune(time.time())
        found = [p for p in _pending.values() if p.user_id == user_id and p.chain_id == chain_id]
    return bool(_forget_closed(user_id, found))


def drop_others(user_id: int, chain_id: int, keep: str) -> list[str]:
    """
    Discards every quote the user has on `chain_id` but `keep`: a new card is in front of them, and
    an older quote left waiting would come back on the card once this one is dealt with
    (signing.offer).

    @return  The ids discarded.
    """
    with _lock:
        doomed = [
            q for q, p in _pending.items() if p.user_id == user_id and p.chain_id == chain_id and q != keep
        ]
        for quote_id in doomed:
            del _pending[quote_id]
        return doomed


def drop_all(user_id: int, chain_id: int) -> list[str]:
    """
    Discards every quote the user has on `chain_id`: they cancelled them all, or their chat there
    was just deleted, and a quote must not outlive the conversation that showed it.

    @return  The ids discarded.
    """
    with _lock:
        doomed = [q for q, p in _pending.items() if p.user_id == user_id and p.chain_id == chain_id]
        for quote_id in doomed:
            del _pending[quote_id]
        return doomed
