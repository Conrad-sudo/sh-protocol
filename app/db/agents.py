"""
Trading agents: what Mitfah keeps about each wallet's agents on its AgentModule -- the owner's name for
each, and what Mitfah has accounted for of its spend (see schema._migrate_agents). The chain holds
everything else; agents.py reads it there.

Two processes write the spend columns: the API when one of its agent trades lands (add_relayed) and
the keeper when it looks (rebase_agent). Each write is a compare-and-swap on the values it was
computed from, so neither can overwrite what the other just wrote: a write that loses is retried
from a fresh read, or left for the next look.

Addresses are stored checksummed, as they come from the chain, and compared exactly. Amounts are text
(uint128s in 18 decimals) and only ever added up in Python.
"""
import time

from db import get_db

# How often add_relayed retries a write another process beat it to.
_CAS_TRIES = 5


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def get_agent_row(chain_id: int, wallet: str, address: str) -> dict | None:
    """One agent's row, or None."""
    return _row(get_db().execute(
        "SELECT * FROM agents WHERE chain_id = ? AND wallet = ? AND address = ?", (chain_id, wallet, address)
    ).fetchone())


def get_agent_rows(user_id: int, chain_id: int) -> list[dict]:
    """The account's agents on one chain, oldest first."""
    rows = get_db().execute(
        "SELECT * FROM agents WHERE user_id = ? AND chain_id = ? ORDER BY added_at, id", (user_id, chain_id)
    ).fetchall()
    return [dict(r) for r in rows]


def get_agents_on_chain(chain_id: int) -> list[dict]:
    """Every agent row on one chain, whoever's: what the keeper watches."""
    rows = get_db().execute("SELECT * FROM agents WHERE chain_id = ? ORDER BY id", (chain_id,)).fetchall()
    return [dict(r) for r in rows]


def add_agent_row(
    user_id: int, chain_id: int, wallet: str, address: str, name: str | None, seen: tuple[int, int, int],
) -> dict:
    """
    Records an agent, with what it has spent as of now: only what it spends after this is ever
    reported. An agent already on record keeps its row and its accounts; a name given here replaces
    its own.

    @param seen  (epoch, windowStart, spentUsd) of its record on chain.
    """
    db = get_db()
    epoch, window, spent = seen
    now = int(time.time())
    db.execute(
        "INSERT OR IGNORE INTO agents (user_id, chain_id, wallet, address, name, added_at, seen_epoch, seen_window, "
        "seen_spent, relayed, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '0', ?)",
        (user_id, chain_id, wallet, address, name, now, epoch, window, str(spent), now),
    )
    if name is not None:
        db.execute(
            "UPDATE agents SET name = ? WHERE chain_id = ? AND wallet = ? AND address = ?",
            (name, chain_id, wallet, address),
        )
    db.commit()
    return get_agent_row(chain_id, wallet, address)


def rename_agent(chain_id: int, wallet: str, address: str, name: str) -> bool:
    """Gives an agent a new name. False if it isn't on record."""
    db = get_db()
    cur = db.execute(
        "UPDATE agents SET name = ? WHERE chain_id = ? AND wallet = ? AND address = ?", (name, chain_id, wallet, address)
    )
    db.commit()
    return cur.rowcount == 1


def rebase_agent(chain_id: int, wallet: str, address: str, seen: tuple[int, int, int], was: dict | None = None) -> bool:
    """
    Accounts for everything an agent has spent up to now: its record on chain becomes the new
    baseline, and nothing relayed is pending against it.

    @param seen  (epoch, windowStart, spentUsd) of its record on chain.
    @param was   The row this was decided from. When given, the write happens only if the row is still
                 exactly that -- no trade of Mitfah's landed in between -- so a caller can act on the
                 decision (tell the user) only when this returns True.
    @return      Whether the row was written.
    """
    epoch, window, spent = seen
    query = (
        "UPDATE agents SET seen_epoch = ?, seen_window = ?, seen_spent = ?, relayed = '0', checked_at = ? "
        "WHERE chain_id = ? AND wallet = ? AND address = ?"
    )
    params: list = [epoch, window, str(spent), int(time.time()), chain_id, wallet, address]
    if was is not None:
        query += " AND seen_epoch = ? AND seen_window = ? AND seen_spent = ? AND relayed = ?"
        params += [was["seen_epoch"], was["seen_window"], was["seen_spent"], was["relayed"]]
    db = get_db()
    cur = db.execute(query, params)
    db.commit()
    return cur.rowcount == 1


def add_relayed(chain_id: int, wallet: str, address: str, epoch: int, window: int, charged: int) -> bool:
    """
    Accounts for one of Mitfah's own trades of an agent: what it charged the agent (its USD spend and
    fee), in the window it was charged in. Order-free: trades that land out of order add up the same.

      - The row's own window: added to what is relayed there.
      - A later window than the row's: that window starts here, from nothing spent, with this trade.
        Anything else the agent spent in it shows up beside it, for the keeper to report.
      - An earlier window, or another epoch (the agent was removed and added again): left alone. The
        keeper's next look has already, or will, set a newer baseline.

    @return  Whether it was written (False when left alone, or another process kept winning).
    """
    db = get_db()
    for _ in range(_CAS_TRIES):
        row = get_agent_row(chain_id, wallet, address)
        if row is None or row["seen_epoch"] != epoch or row["seen_window"] > window:
            return False
        if row["seen_window"] == window:
            update = ("relayed = ?", [str(int(row["relayed"]) + charged)])
        else:
            update = ("seen_window = ?, seen_spent = '0', relayed = ?", [window, str(charged)])
        cur = db.execute(
            f"UPDATE agents SET {update[0]} WHERE chain_id = ? AND wallet = ? AND address = ? "
            "AND seen_epoch = ? AND seen_window = ? AND seen_spent = ? AND relayed = ?",
            [*update[1], chain_id, wallet, address, row["seen_epoch"], row["seen_window"], row["seen_spent"],
             row["relayed"]],
        )
        db.commit()
        if cur.rowcount == 1:
            return True
    return False


def touch_agent(chain_id: int, wallet: str, address: str) -> None:
    """Notes that the keeper looked at an agent and found nothing to account for."""
    db = get_db()
    db.execute(
        "UPDATE agents SET checked_at = ? WHERE chain_id = ? AND wallet = ? AND address = ?",
        (int(time.time()), chain_id, wallet, address),
    )
    db.commit()


def delete_agent_row(chain_id: int, wallet: str, address: str) -> None:
    """Forgets an agent that left the wallet."""
    db = get_db()
    db.execute("DELETE FROM agents WHERE chain_id = ? AND wallet = ? AND address = ?", (chain_id, wallet, address))
    db.commit()


def agent_trade_in_flight(chain_id: int, wallet: str, address: str) -> bool:
    """
    Whether Mitfah has a trade of this agent's on its way: its History row written, its outcome not.
    What it charges is accounted for when it lands (agents.settle), so the keeper waits for that.
    """
    return get_db().execute(
        "SELECT 1 FROM transactions WHERE chain_id = ? AND wallet = ? AND agent = ? AND source = 'agent' "
        "AND status = 'pending' LIMIT 1",
        (chain_id, wallet, address),
    ).fetchone() is not None
