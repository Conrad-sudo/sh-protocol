"""
Waiting for confirmed transactions to land, off the request that sent them.

A Confirm button, in Telegram or the web chat, answers as soon as its transaction is on the wire
(tools.sending.submit_quoted). The wait for the receipt -- a block or more, and up to tx_sender's
MAX_FEE_BUMPS replacements when it stalls -- runs off the request instead: once per bundle in
bundler.Lane, and for each send on a thread of its own here, at most LANDING_MAX_WAITS at once,
which reads that send's own outcome from the bundle's receipt. So the slot the tap held is free
again in a second or two, not after the block.

The outcome is written into the History tab here (tools.sending.settle_quoted), whoever is still
listening. Telegram awaits the future and sends it as a second message; the web chat reads the
History row (GET /api/transactions/{id}). A restart loses the waits in flight, not the record:
each send's row is written as pending before it goes out, and the History tab settles it from the
chain (tx_history.settle_pending). That is why the waits run on daemon threads: stopping the
process never waits for a block, or for a stalled transaction's fee bumps.
"""
import contextvars
import logging
import os
import threading
from concurrent.futures import Future
from typing import Callable

# How many sends may be waited on at once, per process. Past that a wait queues, which only delays
# when it is written down: the transaction itself is already on the wire, and its bundle's own wait
# (bundler.Lane._wait) replaces it at a higher fee if it stalls, queued or not. Sized for a few
# seconds of confirms at chat_limits.CONFIRM_MAX_CONCURRENT.
LANDING_MAX_WAITS = int(os.getenv("LANDING_MAX_WAITS", "256"))

_wait_slots = threading.BoundedSemaphore(LANDING_MAX_WAITS)

# The History rows being waited on in this process, so a read of one knows whether to settle it
# from the chain itself (a wait lost to a restart) or leave it to the wait.
_watched: set[int] = set()
_watched_lock = threading.Lock()


def watch(transaction_id: int | None, settle: Callable[[], str]) -> Future:
    """
    Waits for one send on a thread of the pool, and returns a future of what to tell the user.

    @param transaction_id  The send's History row, or None if it couldn't be recorded.
    @param settle          Waits for it and returns the outcome, already worded for the user. It
                           must not raise: the future's result is all a listener gets.
    """
    if transaction_id is not None:
        with _watched_lock:
            _watched.add(transaction_id)
    future: Future = Future()
    future.add_done_callback(_log_failure)

    def run():
        try:
            with _wait_slots:
                future.set_result(settle())
        except BaseException as error:  # noqa: BLE001 -- handed to whoever awaits the future
            future.set_exception(error)
        finally:
            if transaction_id is not None:
                with _watched_lock:
                    _watched.discard(transaction_id)

    # The caller's context is copied in, as parallel.py does, so the wait sees what the turn saw.
    threading.Thread(target=contextvars.copy_context().run, args=(run,), name="landing", daemon=True).start()
    return future


def is_watched(transaction_id: int) -> bool:
    """True while this process is waiting for the send in this History row."""
    with _watched_lock:
        return transaction_id in _watched


def _log_failure(future: Future) -> None:
    """A wait that raised anyway: nobody may be listening, so the log is where it shows."""
    error = future.exception()
    if error is not None:
        logging.getLogger(__name__).error("A wait for a sent transaction failed", exc_info=error)
