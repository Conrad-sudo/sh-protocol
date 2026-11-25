"""
Trading agents in Controls: list a wallet's agents, prepare and confirm the owner's transactions that
add one, change one's limit and expiry, or remove one, and rename one.

Only the wallet's OWNER, calling AgentModule directly, can add an agent or raise its limit or expiry:
the module checks msg.sender == the wallet's owner, and a passkey acts AS the wallet, so it can never
give an agent anything. So, like the other owner actions, these endpoints PREPARE an unsigned
transaction the user signs in their browser wallet, and confirm it once mined. The transaction goes to
the module, not the wallet. (The wallet itself may only remove an agent -- a passkey's way to stop one
without the owner wallet.)

Names live in Mitfah's database only (db.agents); everything else is read from the chain on every list.
"""
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from web3.logs import DISCARD

import agents
from auth import get_current_user
from constants import MAX_DAILY_LIMIT_USD, get_native_asset_ticker
from contracts import read_spending_config
from db import add_agent_row, add_transaction, delete_agent_row, get_agent_row, get_wallet_address, rename_agent
from parallel import read_all
from routes.deps import (
    CONFIRM_POLL_TIMEOUT_SECS,
    _failed_tx_detail,
    _load_wallet_for_chain,
    _require_owner,
    _resolve_chain,
    _tx_seen,
)
from routes.owner import TxConfirmRequest, _prepare_owner_tx

router = APIRouter()


class AgentPrepareRequest(BaseModel):
    """Body of POST /api/wallet/agents/prepare."""

    chain_id: int
    action: str = Field(pattern="^(add|update|remove)$")
    # The agent's address: the key it signs its trades with.
    agent: str
    # For add and update: what it may spend per period of the wallet's limit, in US dollars. Decimal,
    # so "12.5" converts exactly.
    limit_usd: Decimal | None = Field(default=None, gt=0, le=MAX_DAILY_LIMIT_USD)
    # For add and update: how long it works from now, in days.
    days: int | None = Field(default=None, ge=1, le=agents.MAX_DAYS)


class AgentConfirmRequest(TxConfirmRequest):
    """Body of POST /api/wallet/agents/confirm."""

    # For an add: the name the owner gives the agent, kept in Mitfah's database only.
    name: str | None = Field(default=None, max_length=agents.NAME_MAX_LENGTH)


class AgentRenameRequest(BaseModel):
    """Body of POST /api/wallet/agents/rename."""

    chain_id: int
    agent: str
    name: str = Field(min_length=1, max_length=agents.NAME_MAX_LENGTH)


def _clean_name(name: str | None) -> str | None:
    """A name as stored: its words, single-spaced; None when there is none. Control characters refused."""
    if name is None:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A name can't contain control characters.")
    return " ".join(name.split()) or None


def _address(w3: Web3, value: str) -> str:
    try:
        return w3.to_checksum_address(value.strip())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {value}")


def _module_or_400(w3: Web3, wallet):
    """The wallet's AgentModule, or a 400 saying it has none."""
    module = agents.module_for_wallet(w3, wallet)
    if module is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, agents.NO_MODULE)
    return module


def _listing(user_id: int, chain_id: int, wallet, module) -> dict:
    """The answer of GET /api/wallet/{chain_id}/agents, after bringing wallet.db's rows in line."""
    if module is None:
        return {
            "available": False, "module": None, "native_metered": None, "max_agents": agents.MAX_AGENTS,
            "max_days": agents.MAX_DAYS, "agents": [], "note": agents.NO_MODULE,
        }
    first = read_all({
        "agents": lambda: agents.read_agents(module, wallet.address),
        "config": lambda: read_spending_config(wallet),
    })
    rows = agents.sync_rows(user_id, chain_id, wallet.address, first["agents"])
    return {
        "available": True,
        "module": module.address,
        "native_metered": bool(first["config"]["nativeMetered"]),
        "max_agents": agents.MAX_AGENTS,
        "max_days": agents.MAX_DAYS,
        "agents": [agents.describe(state, rows[state.address]["name"]) for state in first["agents"]],
    }


@router.get("/api/wallet/{chain_id}/agents")
def list_agents(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The wallet's trading agents, read from the chain, with the names the owner gave them.

    @return  {"available", "module", "native_metered", "max_agents", "max_days", "agents": [{"address",
             "name", "limit_usd", "spent_usd", "left_usd", "valid_until", "expires", "active"}]}.
             `available` is false for a wallet without the module; it then lists nothing. Agents can be
             added only while `native_metered` is true.
    """
    w3, _ = _resolve_chain(chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, chain_id)
    return _listing(user_id, chain_id, wallet, agents.module_for_wallet(w3, wallet))


@router.post("/api/wallet/agents/prepare")
def prepare_agent(req: AgentPrepareRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned addAgent / updateAgent / removeAgent transaction, sent to the wallet's module.

    The module refuses an agent that is the wallet, the module or nobody, a fourth agent, one already
    added, a zero limit, and an expiry past MAX_DAYS; the simulation shows its reason before the user pays
    for a failing transaction. An add needs the wallet's limit to count the native asset (an agent's
    trades are refused while it doesn't): that one is answered here, with what to do first.

    @return  {"tx", "action", "agent", "limit_usd", "valid_until"}; the last two for add and update.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    agent = _address(w3, req.agent)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    module = _module_or_400(w3, wallet)
    if req.action == "remove":
        fn = module.functions.removeAgent(wallet.address, agent)
        return {**_prepare_owner_tx(w3, owner, fn, req.chain_id), "action": "remove", "agent": agent}

    if req.limit_usd is None or req.days is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the agent's limit_usd and how many days it works.")
    limit = int(req.limit_usd * agents.USD)
    # From the chain's own time, which the module checks the expiry against when the transaction mines.
    valid_until = w3.eth.get_block("latest")["timestamp"] + req.days * 86_400
    if req.action == "add":
        if not read_spending_config(wallet)["nativeMetered"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"First turn on counting {get_native_asset_ticker(req.chain_id)} toward the limit: an agent can't "
                f"be added, or trade, while the limit leaves it out.",
            )
        fn = module.functions.addAgent(wallet.address, agent, limit, valid_until)
    else:
        fn = module.functions.updateAgent(wallet.address, agent, limit, valid_until)
    prepared = _prepare_owner_tx(w3, owner, fn, req.chain_id)
    return {**prepared, "action": req.action, "agent": agent, "limit_usd": str(req.limit_usd), "valid_until": valid_until}


@router.post("/api/wallet/agents/confirm")
def confirm_agent(req: AgentConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for an add, change or removal to mine, records it in the History tab and in wallet.db (an
    added agent with its name, starting from nothing spent), and answers with the agents as they are.

    Only a transaction to this wallet's module that changed THIS wallet's agents is accepted, so one user
    can't report another's hash and have it recorded as theirs. Answers 202 while pending, like
    /api/wallet/tx/confirm.

    @return  {"status": "confirmed", "tx_hash", "agents": <GET /api/wallet/{chain_id}/agents>}.
    """
    _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    module = _module_or_400(w3, wallet)

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
            f"Transaction {req.tx_hash} was not sent to your wallet's agent module on chain {req.chain_id}.",
        )
    if receipt["status"] != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _failed_tx_detail(w3, req.tx_hash, receipt, "That transaction"))

    def ours(name: str) -> list:
        events = getattr(module.events, name)().process_receipt(receipt, errors=DISCARD)
        return [e["args"] for e in events if e["args"]["account"] == wallet.address]

    added, updated, removed = ours("AgentAdded"), ours("AgentUpdated"), ours("AgentRemoved")
    if not (added or updated or removed):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Transaction {req.tx_hash} didn't change this wallet's agents.")

    name = _clean_name(req.name)
    actions = []
    for args in removed:
        row = get_agent_row(req.chain_id, wallet.address, args["agent"])
        actions.append(f"Remove agent {agents.label(row['name'] if row else None, args['agent'])}")
        delete_agent_row(req.chain_id, wallet.address, args["agent"])
    for args in added:
        row = add_agent_row(user_id, req.chain_id, wallet.address, args["agent"], name, (args["epoch"], 0, 0))
        actions.append(
            f"Add agent {agents.label(row['name'], args['agent'])}: {agents.usd(args['limitUsd'])} a period, "
            f"until {agents.expiry_text(args['validUntil'])}"
        )
    for args in updated:
        row = get_agent_row(req.chain_id, wallet.address, args["agent"])
        actions.append(
            f"Change agent {agents.label(row['name'] if row else None, args['agent'])}: "
            f"{agents.usd(args['limitUsd'])} a period, until {agents.expiry_text(args['validUntil'])}"
        )

    mined_at = w3.eth.get_block(receipt["blockNumber"])["timestamp"]
    add_transaction(
        user_id, req.chain_id, wallet.address, "owner", "; ".join(actions), "confirmed",
        tx_hash=req.tx_hash if req.tx_hash.startswith("0x") else f"0x{req.tx_hash}", mined_at=mined_at, direction="none",
    )
    return {"status": "confirmed", "tx_hash": req.tx_hash, "agents": _listing(user_id, req.chain_id, wallet, module)}


@router.post("/api/wallet/agents/rename")
def rename(req: AgentRenameRequest, user_id: int = Depends(get_current_user)):
    """
    Gives one of the wallet's agents a new name, in Mitfah's database only: nothing on chain changes.

    @return  {"agent", "name"}. 404 when the agent isn't on record: list the agents first, which records
             any added outside Mitfah.
    """
    try:
        wallet = get_wallet_address(user_id, req.chain_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"You have no wallet on chain {req.chain_id}.")
    try:
        agent = Web3.to_checksum_address(req.agent.strip())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.agent}")
    name = _clean_name(req.name)
    if name is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the agent a name.")
    if not rename_agent(req.chain_id, Web3.to_checksum_address(wallet), agent, name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That agent isn't one of this wallet's on record.")
    return {"agent": agent, "name": name}
