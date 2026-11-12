"""
Recurring payments on the Controls page: list a wallet's schedules, and prepare and confirm the
owner's transactions that create or cancel one.

A schedule can only be created or cancelled by the wallet's OWNER calling the RecurringPaymentModule
directly (the module checks msg.sender == the wallet's owner), so, like the other owner actions,
these endpoints PREPARE an unsigned transaction the user signs in their browser wallet and confirm
it once mined. The transaction goes to the module, not the wallet: that is what keeps a passkey --
which acts as the wallet -- from ever creating one. The keeper (keeper.py) pays each instalment.
"""
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from langchain_erc20.amounts import to_base_units
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

import recurring
from auth import get_current_user
from constants import get_native_asset_ticker
from db import add_transaction, get_schedule
from routes.deps import (
    CONFIRM_POLL_TIMEOUT_SECS,
    _failed_tx_detail,
    _load_wallet_for_chain,
    _require_owner,
    _resolve_chain,
    _tx_seen,
)
from routes.owner import TxConfirmRequest, _prepare_owner_tx, _resolve_withdraw_token

router = APIRouter()


class RecurringCreateRequest(BaseModel):
    """Body of POST /api/wallet/recurring/prepare."""

    chain_id: int
    # A listed ticker, a ticker the user added, a token's contract address, or "eth"/the chain's
    # native ticker for the native coin.
    token: str
    # Each payment, in whole units (e.g. "100"); Decimal, so "0.1" converts exactly.
    amount: Decimal = Field(gt=0)
    # Who is paid: a plain address. The web app turns a contact into its address; the owner signs
    # this themselves, so there is no injection to defend against.
    to: str
    interval_secs: int = Field(ge=recurring.MIN_INTERVAL_SECS, le=recurring.MAX_INTERVAL_SECS)
    # Unix seconds. None, or a time already past, means the first payment is due now.
    first_due: int | None = Field(default=None, ge=0)
    # Unix seconds: the last moment a payment may fall due. None for no end.
    end_at: int | None = Field(default=None, gt=0)


class RecurringCancelRequest(BaseModel):
    """Body of POST /api/wallet/recurring/cancel/prepare."""

    chain_id: int
    schedule_id: int = Field(gt=0)


def _module_or_400(w3: Web3, chain_id: int, wallet):
    """The wallet's RecurringPaymentModule, or a 400 saying the wallet has none."""
    module = recurring.module_for_wallet(w3, chain_id, wallet)
    if module is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This wallet has no recurring payments module: it was created before recurring payments, "
            "or the module was removed.",
        )
    return module


def _fee_per_payment(wallet) -> int | None:
    """What each payment pays the protocol in wei -- its fee plus the keeper fee -- or None if unreadable."""
    try:
        registry = wallet.w3.eth.contract(
            address=wallet.functions.REGISTRY().call(), abi=recurring.get_json(recurring.REGISTRY_ABI_PATH)["abi"]
        )
        _treasury, fee, keeper_fee = registry.functions.feeConfig().call()
        return fee + keeper_fee
    except Exception:  # noqa: BLE001 -- the page shows the schedules without the fee
        return None


def _listing(w3: Web3, user_id: int, chain_id: int, wallet, module) -> dict:
    """The answer of GET /api/wallet/{chain_id}/recurring, after bringing wallet.db up to date."""
    rows = recurring.sync_wallet(user_id, chain_id, wallet.address, module) if module else []
    rows = [r for r in rows if r["wallet"] == wallet.address]
    fee = _fee_per_payment(wallet)
    return {
        "available": module is not None,
        "module": module.address if module else None,
        "fee_per_payment_wei": str(fee) if fee is not None else None,
        "native_ticker": get_native_asset_ticker(chain_id),
        "min_interval_secs": recurring.MIN_INTERVAL_SECS,
        "schedules": [recurring.describe(w3, user_id, chain_id, r) for r in rows],
    }


@router.get("/api/wallet/{chain_id}/recurring")
def list_recurring(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The wallet's recurring payments, read from the chain: live ones first, then cancelled and
    finished ones, each with how the keeper's last attempt went.

    @return  {"available", "module", "fee_per_payment_wei", "native_ticker", "min_interval_secs",
             "schedules": [recurring.describe(...)]}. `available` is false for a wallet without the
             module; it then lists nothing.
    """
    w3, _ = _resolve_chain(chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, chain_id)
    module = recurring.module_for_wallet(w3, chain_id, wallet)
    return _listing(w3, user_id, chain_id, wallet, module)


@router.post("/api/wallet/recurring/prepare")
def prepare_recurring(req: RecurringCreateRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `createSchedule(...)` transaction, sent to the wallet's module.

    The module refuses anything the owner didn't send, a recipient that is the wallet itself, a token
    with no code, and an end before the first payment; the simulation shows its reason before the
    user pays for a failing transaction. A payment the wallet can't make when it falls due -- over
    the spending limit, not enough funds -- is only found then, and shown on the Controls page.

    @return  {"tx", "token_address", "amount_base_units"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    try:
        to = w3.to_checksum_address(req.to)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.to}")
    if req.end_at is not None and req.end_at < (req.first_due or int(time.time())):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The end date is before the first payment.")

    token_address, decimals = _resolve_withdraw_token(w3, user_id, req.chain_id, req.token)
    amount, _truncated = to_base_units(str(req.amount), decimals)
    if amount == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{req.amount} {req.token} rounds to zero at {decimals} decimals."
        )

    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    if to == wallet.address:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A wallet can't pay itself.")
    module = _module_or_400(w3, req.chain_id, wallet)
    fn = module.functions.createSchedule(
        wallet.address, token_address, to, amount, req.interval_secs, req.first_due or 0, req.end_at or 0
    )
    prepared = _prepare_owner_tx(w3, owner, fn, req.chain_id)
    return {**prepared, "token_address": token_address, "amount_base_units": str(amount)}


@router.post("/api/wallet/recurring/cancel/prepare")
def prepare_cancel_recurring(req: RecurringCancelRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `cancelSchedule(wallet, id)` transaction, sent to the wallet's module.
    A schedule that is no longer live is refused by the simulation (RecurringPayment_UnknownSchedule).

    @return  {"tx"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    module = _module_or_400(w3, req.chain_id, wallet)
    return _prepare_owner_tx(w3, owner, module.functions.cancelSchedule(wallet.address, req.schedule_id), req.chain_id)


@router.post("/api/wallet/recurring/confirm")
def confirm_recurring(req: TxConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for a create or cancel to mine, records it in the History tab, and answers with the
    wallet's schedules as they are now.

    Only a transaction to this wallet's module that created or cancelled one of THIS wallet's
    schedules is accepted, so one user can't report another's hash and have it recorded as theirs.
    Answers 202 while pending, like /api/wallet/tx/confirm.

    @return  {"status": "confirmed", "tx_hash", "recurring": <GET /api/wallet/{chain_id}/recurring>}.
    """
    _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    module = _module_or_400(w3, req.chain_id, wallet)

    if not _tx_seen(w3, req.tx_hash):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": False}
    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": True}

    if receipt["to"] is None or w3.to_checksum_address(receipt["to"]) != module.address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transaction {req.tx_hash} was not sent to your wallet's recurring payments on chain {req.chain_id}.",
        )
    if receipt["status"] != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _failed_tx_detail(w3, req.tx_hash, receipt, "That transaction"))

    events = recurring.events_in(module, receipt)
    created = [e for e in events["ScheduleCreated"] if e["args"]["account"] == wallet.address]
    cancelled = [e for e in events["ScheduleCancelled"] if e["args"]["account"] == wallet.address]
    if not created and not cancelled:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Transaction {req.tx_hash} didn't change this wallet's recurring payments."
        )

    # Before the History line, which words the cancelled schedule from the row it is about to update.
    actions = []
    for event in cancelled:
        row = get_schedule(req.chain_id, wallet.address, event["args"]["id"])
        summary = recurring.describe(w3, user_id, req.chain_id, row)["summary"] if row else "a recurring payment"
        actions.append(f"Stop paying {summary}" if row else "Cancel a recurring payment")
    listing = _listing(w3, user_id, req.chain_id, wallet, module)
    for event in created:
        row = get_schedule(req.chain_id, wallet.address, event["args"]["id"])
        if row:
            actions.append(f"Pay {recurring.describe(w3, user_id, req.chain_id, row)['summary']}")

    mined_at = w3.eth.get_block(receipt["blockNumber"])["timestamp"]
    add_transaction(
        user_id, req.chain_id, wallet.address, "owner", "; ".join(actions) or "Change a recurring payment",
        "confirmed", tx_hash=req.tx_hash if req.tx_hash.startswith("0x") else f"0x{req.tx_hash}",
        mined_at=mined_at, direction="none",
    )
    return {"status": "confirmed", "tx_hash": req.tx_hash, "recurring": listing}
