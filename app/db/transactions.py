"""
The History tab's records: every transaction on a user's wallets and what each one moved.
"""
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal

from web3 import Web3

from db import get_db

# Plain storage. What gets recorded, and when, is tx_history.py's business.
_TRANSACTION_COLUMNS = (
    "id, user_id, chain_id, wallet, source, action, status, tx_hash, user_op_hash, op_nonce, "
    "from_block, created_at, mined_at, direction, movements_read"
)


def add_transaction(
    user_id: int,
    chain_id: int,
    wallet: str,
    source: str,
    action: str,
    status: str,
    *,
    tx_hash: str | None = None,
    user_op_hash: str | None = None,
    op_nonce: int | None = None,
    from_block: int | None = None,
    mined_at: int | None = None,
    direction: str | None = None,
    movements: list | None = None,
) -> int | None:
    """
    Records a transaction for the History tab.

    @param source     'assistant', 'owner' or 'outside'.
    @param status     'pending', 'confirmed', 'failed' or 'dropped'.
    @param direction  'in', 'out' or 'none', when already known; see save_movements.
    @param movements  What it moved (tx_movements.Moved), when already known. The row is then
                      marked read, so tx_history.read_movements leaves it alone.
    @return           The new row's id, or None if this op or transaction is already recorded.
    """
    db = get_db()
    cur = db.execute(
        "INSERT OR IGNORE INTO transactions (user_id, chain_id, wallet, source, action, status, "
        "tx_hash, user_op_hash, op_nonce, from_block, created_at, mined_at, direction) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, chain_id, wallet, source, action, status, tx_hash, user_op_hash,
            None if op_nonce is None else str(op_nonce), from_block, int(time.time()), mined_at,
            direction,
        ),
    )
    row_id = cur.lastrowid if cur.rowcount else None
    if row_id is not None and movements is not None:
        _write_movements(db, row_id, movements, direction)
    if row_id is not None and source != "outside" and tx_hash is not None:
        _drop_outside_twin(db, row_id)
    db.commit()
    return row_id


def settle_transaction(
    tx_id: int, status: str, tx_hash: str | None = None, mined_at: int | None = None
) -> bool:
    """
    Moves a pending transaction to its outcome. A row that is no longer pending is left alone.

    @return  True if the row was pending and is now settled.
    """
    db = get_db()
    cur = db.execute(
        "UPDATE transactions SET status = ?, tx_hash = COALESCE(?, tx_hash), "
        "mined_at = COALESCE(?, mined_at) WHERE id = ? AND status = 'pending'",
        (status, tx_hash, mined_at, tx_id),
    )
    if cur.rowcount and tx_hash is not None:
        _drop_outside_twin(db, tx_id)
    db.commit()
    return cur.rowcount > 0


def _drop_outside_twin(db: sqlite3.Connection, tx_id: int):
    """
    Deletes the outside row for the same wallet and hash as Mitfah row `tx_id`, if there is one.

    An explorer search can run while an assistant send is still pending, before its row knows its
    hash. It then records the send as outside activity. Once the Mitfah row learns the hash, the
    outside copy goes. Leaves the commit to the caller.
    """
    twins = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM transactions WHERE source = 'outside' AND id != ? AND (chain_id, lower(wallet), tx_hash) = "
            "(SELECT chain_id, lower(wallet), tx_hash FROM transactions WHERE id = ?)",
            (tx_id, tx_id),
        ).fetchall()
    ]
    for twin in twins:
        db.execute("DELETE FROM transaction_movements WHERE tx_id = ?", (twin,))
        db.execute("DELETE FROM transactions WHERE id = ?", (twin,))


def delete_transaction(tx_id: int):
    """Removes a row recorded for something that never reached the chain."""
    db = get_db()
    db.execute("DELETE FROM transaction_movements WHERE tx_id = ?", (tx_id,))
    db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    db.commit()


def save_movements(tx_id: int, movements: list, direction: str | None):
    """
    Stores what a transaction moved, replacing anything stored before, and marks it read.

    @param movements  tx_movements.Moved items. Empty when it moved nothing, or when what it moved
                      can't be known (the node no longer has it).
    @param direction  The History tab's group: 'in', 'out' or 'none'. None keeps the row's own
                      (the intended one an assistant send was recorded with).
    """
    db = get_db()
    _write_movements(db, tx_id, movements, direction)
    db.commit()


def _write_movements(db: sqlite3.Connection, tx_id: int, movements: list, direction: str | None):
    """save_movements without the commit."""
    db.execute("DELETE FROM transaction_movements WHERE tx_id = ?", (tx_id,))
    db.executemany(
        "INSERT INTO transaction_movements (tx_id, direction, token, ticker, amount, amount_value, counterparty) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                tx_id, m.direction, m.token, m.ticker, str(m.amount),
                # Decimals that couldn't be read leave the amount in base units, as the row says it.
                float(Decimal(m.amount) / (Decimal(10) ** (m.decimals or 0))), m.counterparty,
            )
            for m in movements
        ],
    )
    db.execute(
        "UPDATE transactions SET movements_read = 1, direction = COALESCE(?, direction) WHERE id = ?",
        (direction, tx_id),
    )


def get_unread_movement_rows(user_id: int, limit: int) -> list[dict]:
    """The user's newest settled rows whose movements haven't been read yet, up to `limit`."""
    rows = (
        get_db()
        .execute(
            f"SELECT {_TRANSACTION_COLUMNS} FROM transactions "
            "WHERE user_id = ? AND movements_read = 0 AND status != 'pending' ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def get_movements(tx_id: int) -> list[dict]:
    """What a transaction moved, as stored by save_movements."""
    rows = (
        get_db()
        .execute(
            "SELECT direction, token, ticker, amount, amount_value, counterparty "
            "FROM transaction_movements WHERE tx_id = ? ORDER BY rowid",
            (tx_id,),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def get_movement_tickers(user_id: int, chain_id: int | None = None) -> list[str]:
    """Every ticker the user's transactions have moved, A-Z."""
    query = (
        "SELECT DISTINCT m.ticker AS ticker FROM transaction_movements m "
        "JOIN transactions t ON t.id = m.tx_id WHERE t.user_id = ?"
    )
    params: list = [user_id]
    if chain_id is not None:
        query += " AND t.chain_id = ?"
        params.append(chain_id)
    return [row["ticker"] for row in get_db().execute(query + " ORDER BY upper(ticker)", params).fetchall()]


def get_owner_transaction(user_id: int, chain_id: int, tx_hash: str) -> dict | None:
    """The user's row for an owner transaction, by hash, or None."""
    row = (
        get_db()
        .execute(
            f"SELECT {_TRANSACTION_COLUMNS} FROM transactions "
            "WHERE user_id = ? AND chain_id = ? AND tx_hash = ? AND source = 'owner'",
            (user_id, chain_id, tx_hash),
        )
        .fetchone()
    )
    return dict(row) if row else None


@dataclass(frozen=True)
class TransactionFilter:
    """
    The History tab's filters. Every field is optional; the ones given must all hold.

    The money fields (amounts, address, token) must all hold for ONE movement of the transaction,
    so "100 USDC to Sandy" can't match 100 ETH to Sandy and 5 USDC to someone else. With direction
    'in' or 'out', that movement must also go that way: outgoing USDC is the USDC that left.
    """

    direction: str | None = None     # 'in', 'out' or 'none'
    since: int | None = None         # Unix seconds, inclusive
    until: int | None = None         # Unix seconds, exclusive
    min_amount: float | None = None  # whole units
    max_amount: float | None = None
    address: str | None = None       # the other side, or the token's contract
    token: str | None = None         # a ticker, any case


def get_transactions(
    user_id: int,
    chain_id: int | None = None,
    before: tuple[int, int] | None = None,
    limit: int = 50,
    where: TransactionFilter = TransactionFilter(),
) -> list[dict]:
    """
    The user's transactions, newest first: by when they mined, or by when Mitfah recorded them
    while they haven't. Not by id, since outside rows are recorded long after they mined.

    @param chain_id  Only this chain's, or every chain's when None.
    @param before    Only rows older than this (time, id): the cursor for the next page. See
                     transaction_cursor.
    @param where     The History tab's filters.
    """
    query = f"SELECT {_TRANSACTION_COLUMNS} FROM transactions WHERE user_id = ?"
    params: list = [user_id]
    if chain_id is not None:
        query += " AND chain_id = ?"
        params.append(chain_id)
    if where.direction is not None:
        query += " AND direction = ?"
        params.append(where.direction)
    if where.since is not None:
        query += " AND COALESCE(mined_at, created_at) >= ?"
        params.append(where.since)
    if where.until is not None:
        query += " AND COALESCE(mined_at, created_at) < ?"
        params.append(where.until)

    moved: list[str] = []
    moved_params: list = []
    if where.min_amount is not None:
        moved.append("m.amount_value >= ?")
        moved_params.append(where.min_amount)
    if where.max_amount is not None:
        moved.append("m.amount_value <= ?")
        moved_params.append(where.max_amount)
    if where.address is not None:
        moved.append("(lower(m.counterparty) = lower(?) OR lower(m.token) = lower(?))")
        moved_params.extend([where.address, where.address])
    if where.token is not None:
        moved.append("upper(m.ticker) = upper(?)")
        moved_params.append(where.token)
    if moved:
        if where.direction in ("in", "out"):
            moved.append("m.direction = ?")
            moved_params.append(where.direction)
        query += (
            " AND EXISTS (SELECT 1 FROM transaction_movements m WHERE m.tx_id = transactions.id AND "
            + " AND ".join(moved)
            + ")"
        )
        params.extend(moved_params)

    if before is not None:
        query += " AND (COALESCE(mined_at, created_at), id) < (?, ?)"
        params.extend(before)
    query += " ORDER BY COALESCE(mined_at, created_at) DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in get_db().execute(query, params).fetchall()]


def transaction_cursor(row: dict) -> tuple[int, int]:
    """Where a row sits in get_transactions' order: the `before` that starts the page after it."""
    return (row["mined_at"] if row["mined_at"] is not None else row["created_at"], row["id"])


def get_wallet_tx_hashes(chain_id: int, wallet: str) -> set[str]:
    """Every transaction hash already recorded for this wallet, whatever its source."""
    rows = (
        get_db()
        .execute(
            "SELECT tx_hash FROM transactions WHERE chain_id = ? AND lower(wallet) = lower(?) AND tx_hash IS NOT NULL",
            (chain_id, wallet),
        )
        .fetchall()
    )
    return {row["tx_hash"].lower() for row in rows}


def get_first_wallet_tx_hash(chain_id: int, wallet: str) -> str | None:
    """
    The hash of the earliest Mitfah transaction recorded for this wallet: normally the one that
    created it. Where a block-by-block search of its history can start.
    """
    row = (
        get_db()
        .execute(
            "SELECT tx_hash FROM transactions WHERE chain_id = ? AND lower(wallet) = lower(?) "
            "AND source != 'outside' AND tx_hash IS NOT NULL ORDER BY id ASC LIMIT 1",
            (chain_id, wallet),
        )
        .fetchone()
    )
    return row["tx_hash"] if row else None


def get_history_sync(chain_id: int, wallet: str) -> dict | None:
    """
    How far the explorer search has got for a wallet: {"next_block", "synced_at"}, or None if it
    has never run. `next_block` is None until a search has finished a batch.
    """
    row = (
        get_db()
        .execute(
            "SELECT next_block, synced_at FROM history_sync WHERE chain_id = ? AND wallet = ?",
            (chain_id, Web3.to_checksum_address(wallet)),
        )
        .fetchone()
    )
    return dict(row) if row else None


def save_history_sync(chain_id: int, wallet: str, next_block: int | None = None):
    """
    Saves where the next explorer search starts, and that one ran just now. With no `next_block`
    it only marks the run (a search that failed) and keeps the saved start.
    """
    db = get_db()
    db.execute(
        "INSERT INTO history_sync (chain_id, wallet, next_block, synced_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (chain_id, wallet) DO UPDATE SET "
        "next_block = COALESCE(?, next_block), synced_at = excluded.synced_at",
        (chain_id, Web3.to_checksum_address(wallet), next_block, int(time.time()), next_block),
    )
    db.commit()


def get_pending_transactions(user_id: int, limit: int) -> list[dict]:
    """The user's oldest still-pending transactions, up to `limit`."""
    rows = (
        get_db()
        .execute(
            f"SELECT {_TRANSACTION_COLUMNS} FROM transactions "
            "WHERE user_id = ? AND status = 'pending' ORDER BY id ASC LIMIT ?",
            (user_id, limit),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]
