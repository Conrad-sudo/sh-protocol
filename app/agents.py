"""
Trading agents: each wallet's agents on its AgentModule, read from the chain, with what Mitfah keeps
for them (db.agents) -- the owner's name for each, and an account of what each has spent -- and the
keeper's look at that spend (check_agents).

An agent is an Ethereum key that the wallet's owner gave a USD limit and an expiry on AgentModule
(src/AgentModule.sol), with a transaction from their own wallet (routes/agents.py prepares it). The
agent then trades without the user approving each trade: its trades are ordinary UserOps from the
wallet that IT signs, and that Mitfah's bundler sends like a passkey's (agent_trades.py). They are bound
by everything that binds a passkey's -- the wallet's guard, its USD limit, its gas allowance, its pause
-- and by the agent's own limit and the module's three rules. Mitfah never holds an agent's key.

Because the agent holds its key, it can also trade WITHOUT Mitfah: sign an op and hand it to any
bundler, or call handleOps itself. Mitfah can't stop that -- only the owner's limit, the expiry, removing
the agent or pausing the wallet can -- but it can notice. The keeper reads each agent's record about once
a minute and tells the user when it spent more than Mitfah's own trades account for (check_agents).

This module reads; agent_trades.py prepares and sends. Kept apart so the keeper, which only reads,
doesn't load the tools that build trades.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from web3 import Web3
from web3.contract import Contract

from constants import get_chain_display_name, get_native_asset_ticker
from contracts import load_session_handler, read_spending_config
from db import (
    acting_network,
    add_agent_row,
    agent_trade_in_flight,
    delete_agent_row,
    get_agent_rows,
    get_agents_on_chain,
    get_json,
    get_supported_token_by_address,
    get_wallets_on_chain,
    rebase_agent,
    touch_agent,
)
from network_config import load_network_config, network_name
from parallel import read_all
from redact import redact

log = logging.getLogger(__name__)

MODULE_ABI_PATH = "./out/AgentModule.sol/AgentModule.json"
REGISTRY_ABI_PATH = "./out/SHRegistry.sol/SHRegistry.json"
SESSION_HANDLER_ABI_PATH = "./out/SessionHandler.sol/SessionHandler.json"

# ERC-7579's module type for a validator: how every wallet has AgentModule installed.
MODULE_TYPE_VALIDATOR = 1
# USD amounts on chain carry 18 decimals.
USD = 10**18
# AgentModule.MAX_AGENTS and MAX_VALIDITY, mirrored so a request that can only be refused costs no
# RPC call.
MAX_AGENTS = 3
MAX_DAYS = 90
# The longest name the owner may give an agent.
NAME_MAX_LENGTH = 40

NO_MODULE = (
    "This wallet can't have trading agents: it was created before them, so it has no agent module. A "
    "wallet created now has one."
)


@dataclass(frozen=True)
class AgentState:
    """One agent of one wallet, as AgentModule.getAgent reports it. USD amounts in 18 decimals."""

    address: str
    limit: int           # what it may spend per window of the wallet's limit
    spent: int           # what it has spent in the wallet's current window (0 once that ran out)
    remaining: int       # what it may still spend this window, before the wallet's own limit
    valid_until: int     # Unix seconds
    active: bool         # one of the wallet's agents, and not expired
    epoch: int           # the epoch its signatures must name
    window_start: int    # the window its stored spend belongs to
    stored_spent: int    # its spend as stored: in window_start's window

    @property
    def seen(self) -> tuple[int, int, int]:
        """(epoch, windowStart, spentUsd) of its record: what db.agents accounts against."""
        return self.epoch, self.window_start, self.stored_spent


def module_contract(w3: Web3, address: str) -> Contract:
    """An AgentModule at `address`."""
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=get_json(MODULE_ABI_PATH)["abi"])


def wallet_contract(w3: Web3, address: str) -> Contract:
    """A SessionHandler at `address`."""
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=get_json(SESSION_HANDLER_ABI_PATH)["abi"])


def module_for_wallet(w3: Web3, wallet: Contract) -> Contract | None:
    """
    The wallet's AgentModule, or None when it has none. The module is the registry's agentModule,
    which the factory installs on every wallet as a validator; a wallet from before agents has none.

    @param wallet  The user's SessionHandler, bound.
    """
    registry = w3.eth.contract(address=wallet.functions.REGISTRY().call(), abi=get_json(REGISTRY_ABI_PATH)["abi"])
    try:
        address = registry.functions.agentModule().call()
    except Exception:  # noqa: BLE001 -- a registry from before agents has no such getter
        return None
    if int(address, 16) == 0 or not wallet.functions.isModuleInstalled(MODULE_TYPE_VALIDATOR, address, b"").call():
        return None
    return module_contract(w3, address)


def _state(address: str, answer) -> AgentState | None:
    """getAgent's answer as an AgentState; None when `address` is not one of the wallet's agents."""
    info, spent, remaining, active = answer
    limit, stored_spent, valid_until, window_start, epoch, position = info
    if position == 0:
        return None
    return AgentState(
        Web3.to_checksum_address(address), int(limit), int(spent), int(remaining), int(valid_until), bool(active),
        int(epoch), int(window_start), int(stored_spent),
    )


def read_agent(module: Contract, wallet: str, agent: str, block_identifier="latest") -> AgentState | None:
    """One agent of `wallet`, or None when `agent` isn't one of its agents."""
    return _state(agent, module.functions.getAgent(wallet, agent).call(block_identifier=block_identifier))


def read_agents(module: Contract, wallet: str) -> list[AgentState]:
    """Every agent of `wallet`, read at once."""
    addresses = module.functions.getAgents(wallet).call()
    answers = read_all({a: (lambda a=a: module.functions.getAgent(wallet, a).call()) for a in addresses})
    return [s for s in (_state(a, answers[a]) for a in addresses) if s is not None]


def label(name: str | None, address: str) -> str:
    """How the user reads an agent's name: “its name”, or its short address when it has none."""
    return f"“{name}”" if name else f"{address[:6]}…{address[-4:]}"


def usd(amount: int) -> str:
    """An 18-decimal USD amount as the user reads it: $1,234.56."""
    return f"${amount / USD:,.2f}"


def expiry_text(valid_until: int) -> str:
    """An agent's expiry, as a date and time in UTC."""
    return datetime.fromtimestamp(valid_until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def describe(state: AgentState, name: str | None) -> dict:
    """One agent as the web app and AI apps see it."""
    return {
        "address": state.address,
        "name": name,
        "limit_usd": round(state.limit / USD, 2),
        "spent_usd": round(state.spent / USD, 2),
        "left_usd": round(state.remaining / USD, 2),
        "valid_until": state.valid_until,
        "expires": expiry_text(state.valid_until),
        "active": state.active,
    }


def sync_rows(user_id: int, chain_id: int, wallet: str, states: list[AgentState]) -> dict[str, dict]:
    """
    Brings wallet.db's rows for one wallet in line with its agents on chain. One on chain and not on
    record -- added outside Mitfah, or before its row was written -- is recorded, unnamed, with what it
    has spent so far, so only what it spends from now on is ever reported; one on record and gone from
    the chain is forgotten.

    @return  The rows, by agent address.
    """
    rows = {r["address"]: r for r in get_agent_rows(user_id, chain_id) if r["wallet"] == wallet}
    for address in set(rows) - {s.address for s in states}:
        delete_agent_row(chain_id, wallet, address)
        del rows[address]
    for state in states:
        if state.address not in rows:
            rows[state.address] = add_agent_row(user_id, chain_id, wallet, state.address, None, state.seen)
    return rows


def setup(user_id: int, chain_id: int) -> dict:
    """
    What an agent needs to know to trade from the account's wallet on one network: the wallet's agents,
    what their trades may move, and how one signs. get_agent_setup's answer (mcp_tools.py).
    """
    with acting_network(user_id, network_name(chain_id), chain_id):
        w3, _, _ = load_network_config(user_id)
        wallet = load_session_handler(user_id)
        module = module_for_wallet(w3, wallet)
        if module is None:
            return {"network": get_chain_display_name(chain_id), "wallet": wallet.address, "available": False,
                    "note": NO_MODULE}
        first = read_all({
            "agents": lambda: read_agents(module, wallet.address),
            "config": lambda: read_spending_config(wallet),
        })
    rows = sync_rows(user_id, chain_id, wallet.address, first["agents"])
    config = first["config"]
    native = get_native_asset_ticker(chain_id)
    counted = []
    for address in config["watchedTokens"]:
        token = get_supported_token_by_address(chain_id, address)
        counted.append({"ticker": token["ticker"].upper() if token else None, "address": address})
    return {
        "network": get_chain_display_name(chain_id),
        "wallet": wallet.address,
        "available": True,
        "agents": [describe(state, rows[state.address]["name"]) for state in first["agents"]],
        "counts_native": bool(config["nativeMetered"]),
        "counted_tokens": counted,
        "signing": {
            "standard": "EIP-712",
            "primary_type": "AgentOperation",
            "domain": {"name": "AgentModule", "version": "1", "chainId": chain_id, "verifyingContract": module.address},
        },
        "rules": (
            f"An agent's trade may call any contract, on any exchange or protocol, but: only while counts_native "
            f"is true (the wallet's limit counts {native}); it may never move or approve a token outside "
            f"counted_tokens; and it may never call the agent module. Each trade counts toward the wallet's USD "
            f"limit and toward the agent's own (left_usd), with Mitfah's fee; over either, it is refused. The "
            f"wallet pays the gas. The owner can remove an agent at any time."
        ),
    }


# ── The keeper's look ─────────────────────────────────────────────────────────


def spent_outside(row: dict, state: AgentState) -> int:
    """
    What an agent spent that Mitfah's own trades don't account for: its spend on chain, less the
    baseline the keeper last set and what Mitfah's trades charged since (db.agents.add_relayed).

    In the row's own window, that is what was spent beyond both. In a later window -- one none of
    Mitfah's trades has reached, or a trade of Mitfah's would have moved the row there -- all of it.
    Never negative.
    """
    if state.window_start == row["seen_window"]:
        unexplained = state.stored_spent - int(row["seen_spent"]) - int(row["relayed"])
    elif state.window_start > row["seen_window"]:
        unexplained = state.stored_spent
    else:   # an older window than the row's can't come back: nothing to say
        unexplained = 0
    return max(unexplained, 0)


def outside_notice(name: str | None, state: AgentState, amount: int) -> str:
    """What the user is told when an agent spent outside Mitfah."""
    return (
        f"Your agent {label(name, state.address)} spent {usd(amount)} outside Mitfah: its trades didn't go "
        f"through Mitfah, so you weren't told of them one by one. It has now spent {usd(state.spent)} of its "
        f"{usd(state.limit)} limit this period. If you didn't expect that, remove it in Mitfah's Controls."
    )


def sync_chain(w3: Web3, chain_id: int) -> int:
    """
    Records any agent on `chain_id` that Mitfah has no row for -- one the owner added outside Mitfah, on a
    block explorer -- so check_agents watches it from now on. One getAgents per wallet (and three reads to
    find its module); the records of new agents only.

    @return  How many wallets had new agents recorded.
    """
    synced = 0
    for user_id, address in get_wallets_on_chain(chain_id):
        try:
            if not w3.eth.get_code(address):
                continue   # a stale row: a fork restarted since
            wallet = wallet_contract(w3, address)
            module = module_for_wallet(w3, wallet)
            if module is None:
                continue
            known = {r["address"] for r in get_agent_rows(user_id, chain_id) if r["wallet"] == wallet.address}
            if set(module.functions.getAgents(wallet.address).call()) - known:
                sync_rows(user_id, chain_id, wallet.address, read_agents(module, wallet.address))
                synced += 1
        except Exception as exc:  # noqa: BLE001 -- one wallet must not stop the others
            log.warning("chain %s: could not read the agents of %s: %s", chain_id, address, redact(exc))
    return synced


def check_agents(w3: Web3, chain_id: int, tell: Callable[[int, int, str], None]) -> dict[str, int]:
    """
    The keeper's look at every agent on record on one chain: one read per agent, plus three per wallet
    to find its module.

    For each agent: gone from the wallet -> its row is forgotten; added again since (another epoch) ->
    accounted afresh, silently; a trade of Mitfah's still on its way -> left for its landing to account
    for; spend beyond what Mitfah's trades charged -> the user is told, once. The account is rebased
    only if no trade of Mitfah's landed while this one looked (db.agents.rebase_agent's compare), and the
    user is told only after that, so a look that loses the race says nothing and the next one decides.

    @param tell  (user_id, chain_id, text): how the user is told (keeper._tell_owner).
    @return      {"checked", "outside", "gone", "error"}: how many agents came out each way.
    """
    outcome = {"checked": 0, "outside": 0, "gone": 0, "error": 0}
    modules: dict[str, Contract | None] = {}
    for row in get_agents_on_chain(chain_id):
        wallet, address = row["wallet"], row["address"]
        try:
            if wallet not in modules:
                if not w3.eth.get_code(wallet):
                    modules[wallet] = None   # a stale row: a fork restarted since
                else:
                    modules[wallet] = module_for_wallet(w3, wallet_contract(w3, wallet))
            module = modules[wallet]
            state = read_agent(module, wallet, address) if module is not None else None
            if state is None:
                delete_agent_row(chain_id, wallet, address)
                outcome["gone"] += 1
                continue
            outcome["checked"] += 1
            if state.epoch != row["seen_epoch"]:
                rebase_agent(chain_id, wallet, address, state.seen)
                continue
            if agent_trade_in_flight(chain_id, wallet, address):
                continue
            amount = spent_outside(row, state)
            unchanged = (state.window_start, state.stored_spent) == (row["seen_window"], int(row["seen_spent"]))
            if amount == 0 and unchanged and row["relayed"] == "0":
                touch_agent(chain_id, wallet, address)
                continue
            if not rebase_agent(chain_id, wallet, address, state.seen, was=row):
                continue   # a trade of Mitfah's landed meanwhile: the next look sees it accounted for
            if amount > 0:
                tell(row["user_id"], chain_id, outside_notice(row["name"], state, amount))
                outcome["outside"] += 1
        except Exception as exc:  # noqa: BLE001 -- one agent must not stop the others
            outcome["error"] += 1
            log.warning("chain %s: could not check agent %s of %s: %s", chain_id, address, wallet, redact(exc))
    return outcome
