"""
Runs chain reads that don't depend on each other at the same time.

Every read is a round trip to the RPC -- about half a second each against a live provider -- and
quoting one transaction used to make a dozen of them one after another. Where a later read does not
need an earlier one's answer, they now go out together, so the wait is one round trip, not many.

Two pools, so nothing can deadlock. A READ is a leaf: a bound contract `.call`, an estimate, a
balance -- it never waits on anything else here. A TASK may wait on reads (the wallet checks, say,
are two rounds of them) but never on another task. Reads therefore always make progress, however
many tasks are waiting on them.

Everything that touches the database belongs before a read is started, on the caller's thread: a
worker would open a SQLite connection of its own. The caller's context is copied into every worker
anyway, because the turn's network (db.acting_network) is a ContextVar that a bare worker thread
would not see -- a read that consulted it there would get the user's SAVED network instead.
"""
import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

_reads = ThreadPoolExecutor(max_workers=16, thread_name_prefix="chain-read")
_tasks = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chain-task")


def start_read(read: Callable[[], Any]) -> Future:
    """Starts one read and returns its future. `read` must not wait on this module itself."""
    return _reads.submit(contextvars.copy_context().run, read)


def read_all(reads: dict[Any, Callable[[], Any]]) -> dict:
    """
    Runs independent reads at once: {name: read} -> {name: result}.

    @raises  The first failing read's own exception (in the dict's order), unchanged -- so a revert
             still reaches the agent as the named contract error.
    """
    futures = {name: start_read(read) for name, read in reads.items()}
    return {name: future.result() for name, future in futures.items()}


def start_task(task: Callable[[], Any]) -> Future:
    """Starts work that itself waits on reads (but never on another task), and returns its future."""
    return _tasks.submit(contextvars.copy_context().run, task)
