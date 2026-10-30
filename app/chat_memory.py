"""
Where the agent's conversations are kept: a LangGraph checkpointer on its own SQLite file
(db.CHAT_DB_PATH, beside wallet.db), with a connection per thread.

LangGraph's own savers make every turn wait in one line. SqliteSaver shares one connection behind
one lock; AsyncSqliteSaver, used until 2026-10-26, did the same on the event loop, so each sync
read or write a turn made also hopped onto the loop the API serves its requests from. ChatMemory
gives each thread its own connection instead: reads run side by side (WAL), and writes, which
SQLite still takes one at a time, hold the file only for the write itself.

Its own file, so a conversation's writes never wait behind wallet.db's (sign-in nonces, sign
requests, the History tab), nor those behind them: SQLite has one write lock per file.
"""
import sqlite3
import threading
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver


class ChatMemory(SqliteSaver):
    """
    SqliteSaver with a connection per thread rather than one shared behind a lock.

    Sync only, as SqliteSaver is: its async methods raise. Every caller runs the agent with
    `invoke` on a worker thread -- chat() on the API's threadpool or the bot's turn pool, the CLI
    through asyncio.to_thread.

    SqliteSaver's methods are used as they are. They reach the database through `conn` and
    `cursor()` only, which are what this overrides.
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        super().__init__(None)  # it stores the connection it is given in `conn`, which is ours
        self.setup()  # here, once, so cursor() needn't (SqliteSaver's runs it under its lock)

    @property
    def conn(self) -> sqlite3.Connection:
        """
        This thread's connection, opened on its first use and closed with the thread.

        busy_timeout is per connection, so set on each: without it, a write that meets another
        process's (the API's beside the bot's) fails "database is locked" at once instead of
        waiting its turn. WAL is a property of the file, set once by setup().
        """
        if not hasattr(self._local, "conn"):
            # Not bound to its thread: a generator from list() can be finished on another one.
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return self._local.conn

    @conn.setter
    def conn(self, _unused) -> None:
        """SqliteSaver.__init__ assigns the connection it was given: there is none to keep."""

    @contextmanager
    def cursor(self, transaction: bool = True):
        """SqliteSaver.cursor without its lock: this thread's connection is no other's."""
        conn = self.conn
        cur = conn.cursor()
        try:
            yield cur
        finally:
            if transaction:
                conn.commit()
            cur.close()
