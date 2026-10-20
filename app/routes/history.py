"""
The History tab: every transaction on the account's wallets, and deposits made from the Fund drawer.
"""
import functools

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

import landing
import tx_history
from auth import get_current_user
from db import (
    TransactionFilter,
    get_movement_tickers,
    get_transaction,
    get_transactions,
    get_wallet_address,
    get_wallet_chains,
    transaction_cursor,
)
from network_config import (
    CHAIN_NAME_BY_ID,
)
from routes.deps import (
    CONFIRM_POLL_TIMEOUT_SECS,
    _failed_tx_detail,
    _load_wallet_for_chain,
    _resolve_chain,
)

router = APIRouter()


class DepositRequest(BaseModel):
    """Body of POST /api/transactions/deposit."""

    chain_id: int
    tx_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


def _web3_or_none(chain_id: int) -> Web3 | None:
    """
    A Web3 for `chain_id` when this server serves it and its RPC answers, else None: for work that
    skips a chain it can't reach rather than failing, like settling the History tab's pending rows.
    """
    if chain_id not in CHAIN_NAME_BY_ID:
        return None
    try:
        return _resolve_chain(chain_id)[0]
    except HTTPException:
        return None


def _transaction_json(row: dict) -> dict:
    """A transactions row as the History tab sees it. The fields that find a lost op stay inside."""
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "source": row["source"],
        "action": row["action"],
        "status": row["status"],
        "tx_hash": row["tx_hash"],
        "created_at": row["created_at"],
        "mined_at": row["mined_at"],
        "direction": row["direction"],
    }


@router.get("/api/transactions")
def list_transactions(
    chain_id: int | None = None,
    before: str | None = Query(default=None, pattern=r"^\d{1,12}-\d{1,12}$"),
    limit: int = Query(default=50, ge=1, le=100),
    direction: str | None = Query(default=None, pattern=r"^(in|out|none)$"),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    min_amount: float | None = Query(default=None, ge=0),
    max_amount: float | None = Query(default=None, ge=0),
    address: str | None = Query(default=None, pattern=r"^0x[0-9a-fA-F]{40}$"),
    token: str | None = Query(default=None, min_length=1, max_length=40),
    user_id: int = Depends(get_current_user),
):
    """
    The History tab: every transaction on this account's wallets, newest first -- what the
    assistant sent, from the web or Telegram, what the user signed in the browser, and what
    happened outside Mitfah.

    Reading the first page also settles transactions still pending, where it can: a send that
    outlived the wait, or an owner transaction whose page was closed before it mined. Then it
    reads what a few settled transactions moved, for the filters (tx_history.read_movements). Both
    read the chain, hence a plain `def`. It also starts a background search of the block explorers
    for activity outside Mitfah (tx_history.start_outside_sync); the page never waits on it.

    @param chain_id    Only this chain's transactions; every chain's when omitted.
    @param before      The `next_before` of the previous page, with the same filters.
    @param limit       How many to return (1-100).
    @param direction   'in' (the wallet only received), 'out' (something left it) or 'none'
                       (nothing moved).
    @param since       Only from this time on (Unix seconds, inclusive).
    @param until       Only before this time (Unix seconds, exclusive).
    @param min_amount  Only transactions that moved at least this much of a token, in whole units.
    @param max_amount  ... at most this much.
    @param address     ... to or from this address, or of the token at this address.
    @param token       ... of the token with this ticker, any case, on any chain.
                       The amount, address and token filters must all hold for one movement; with
                       direction 'in' or 'out', it must also go that way. See db.TransactionFilter.
    @return            {"transactions": [{"id", "chain_id", "source", "action", "status",
                       "tx_hash", "created_at", "mined_at", "direction"}, ...],
                       "next_before": str | None, "syncing": bool}. Times are Unix seconds;
                       `mined_at` is the block's. `direction` is null while unknown. `syncing` is
                       true while an explorer search is running, so the tab reads again soon.
    """
    if chain_id is not None and chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    syncing = False
    if before is None:
        web3_for = functools.cache(_web3_or_none)
        tx_history.settle_pending(user_id, web3_for)
        tx_history.read_movements(user_id, web3_for)
        chains = [c for c in get_wallet_chains(user_id) if chain_id is None or c == chain_id]
        wallets = {c: get_wallet_address(user_id, c) for c in chains if c in CHAIN_NAME_BY_ID}
        syncing = tx_history.start_outside_sync(user_id, wallets, web3_for)
    cursor = tuple(int(part) for part in before.split("-")) if before is not None else None
    where = TransactionFilter(
        direction=direction,
        since=since,
        until=until,
        min_amount=min_amount,
        max_amount=max_amount,
        address=address,
        token=token,
    )
    rows = get_transactions(user_id, chain_id, cursor, limit + 1, where)
    page = rows[:limit]
    return {
        "transactions": [_transaction_json(row) for row in page],
        "next_before": "-".join(map(str, transaction_cursor(page[-1]))) if len(rows) > limit else None,
        "syncing": syncing,
    }


@router.get("/api/transactions/tokens")
def list_transaction_tokens(chain_id: int | None = None, user_id: int = Depends(get_current_user)):
    """
    The tickers this account's transactions have moved, A-Z: the History tab's token filter offers
    these, so it never offers a token that would match nothing.

    @param chain_id  Only this chain's; every chain's when omitted.
    @return          {"tokens": ["ETH", "USDC", ...]}.
    """
    if chain_id is not None and chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    return {"tokens": get_movement_tickers(user_id, chain_id)}


@router.get("/api/transactions/{tx_id:int}")
def get_one_transaction(tx_id: int, user_id: int = Depends(get_current_user)):
    """
    One transaction of this account's, as the History tab lists it: how the web chat follows a send
    it confirmed while it waits to land (landing.py).

    A pending row this process isn't waiting on -- the wait was lost to a restart, or the row is an
    owner transaction -- is settled from the chain first, as reading the History tab would.

    @return  {"id", "chain_id", "source", "action", "status", "tx_hash", "created_at", "mined_at",
             "direction"}, as GET /api/transactions lists each.
    @raises HTTPException 404 when the account has no transaction with this id.
    """
    row = get_transaction(user_id, tx_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such transaction.")
    if row["status"] == "pending" and not landing.is_watched(tx_id):
        tx_history.settle_one(row, _web3_or_none)
        row = get_transaction(user_id, tx_id)
    return _transaction_json(row)


@router.post("/api/transactions/deposit")
def confirm_deposit(req: DepositRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for a deposit made from the Fund drawer, and lists it in the History tab.

    The browser sends a deposit itself -- anyone may fund a wallet, so it never goes through the
    owner confirms -- and the drawer asks here until it has mined, through this node: the one the
    dashboard reads the balance from. Only a transaction actually sent to this user's wallet on the
    chain counts. A deposit is listed from the first answer that finds it on the network, so closing
    the drawer before it mines loses nothing; the History tab settles it later.

    Answers 202 while the transaction is still pending, with `seen` as in /api/wallet/tx/confirm;
    the front end polls until it gets a 200.

    @return  {"status": "confirmed", "tx_hash"} once mined.
    @raises HTTPException 400 if it was not sent to the wallet, or failed.
    """
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    try:
        tx = w3.eth.get_transaction(req.tx_hash)
    except TransactionNotFound:
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": False}
    if tx["to"] is None or Web3.to_checksum_address(tx["to"]) != wallet.address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transaction {req.tx_hash} was not sent to your wallet on chain {req.chain_id}.",
        )
    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash)
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": True}

    tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash, receipt)
    if receipt["status"] != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, _failed_tx_detail(w3, req.tx_hash, receipt, "The transfer")
        )
    return {"status": "confirmed", "tx_hash": req.tx_hash}
