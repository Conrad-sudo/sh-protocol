"""
The wallet's tools as AI apps see them over MCP (mcp_server.py): the same tools the web chat's
assistant calls (tools/), each wrapped once, with a description written for a model that has never
seen our system prompt.

Phase 2a: the reads. What a wrapper adds to the tool it wraps:

  - Who. The account is the connection's (the access token's), never an argument: the tool gets it
    in an AgentContext, as the web chat's tools do.
  - Where. A `network` argument, checked against the networks where the account has a wallet, and
    made the acting network (db.acting_network) INSIDE the worker thread the tool runs on: it is a
    ContextVar, so setting it before the hop would not reach the tool.
  - A thread pool of its own (POOL_SIZE), so slow chain reads from AI apps can't take the threads the
    website's own requests run on (anyio's default pool, which FastAPI's plain `def` routes share).
  - A rate limit per connected app (RATE_LIMIT), on every tool call.
  - Plain optional arguments. The tools' own schemas say "a number or null" for an optional
    argument; the published schema says "a number" and leaves it out of `required`, which every app
    reads the same way. Validation still uses the tool's own types.
  - Refusals as tool errors. A tool's refusal (ToolException) reaches the app's model with its own
    words, keys masked (redact); anything else is logged here and answered with a plain sentence, so
    no stack trace or node URL leaves the server.

The quote tools, get_transaction_status and suggest_price_ranges (which only feeds open_position)
come in phase 2b.
"""
import asyncio
import functools
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.to_thread
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, ToolException
from limits import parse, storage, strategies
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import ToolAnnotations
from pydantic import Field, create_model

from agent_context import AgentContext
from constants import get_chain_display_name
from db import acting_network, get_wallet_chains
from network_config import CHAIN_NAME_BY_ID, network_name
from redact import redact
from tools.uniswap import get_lp_amounts, get_liquidity_token_balance, get_pool_quote, get_quote_in, get_quote_out
from tools.positions import get_positions
from tools.wallet_reads import (
    get_all_contacts,
    get_contact,
    get_erc20_balance,
    get_eth_balance,
    get_price,
    get_supported_tokens,
    get_wallet_status,
)

log = logging.getLogger(__name__)

# Where McpEndpoint (mcp_server.py) leaves the caller in the request's state, for the tools.
CALLER_KEY = "mitfah_mcp_caller"

# Calls one connected app may make: enough for a model that reads a handful of things per answer, few
# enough to stop a script. Per process and in memory, like the chat's limits (chat_limits.py).
RATE_LIMIT = "60/minute"

# Threads the tools run on, at most, across every app: a chain read holds one for up to seconds.
POOL_SIZE = 16

_rate = parse(RATE_LIMIT)
_rate_limiter = strategies.MovingWindowRateLimiter(storage.MemoryStorage())

# The pool, with the event loop it belongs to: one loop in the API, a new one per test client.
_pool: tuple[asyncio.AbstractEventLoop, anyio.CapacityLimiter] | None = None

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

# Other names people and models use for a network, beyond its chain id, its name here and the name
# get_account shows.
_ALIASES = {
    1: ("ethereum", "eth mainnet", "ethereum mainnet"),
    56: ("bnb", "bnb chain", "bsc mainnet", "binance smart chain"),
    42161: ("arbitrum", "arbitrum mainnet"),
}

UNEXPECTED = "Mitfah couldn't complete that just now. Try again in a moment."


# ── The caller ────────────────────────────────────────────────────────────────


def caller_of(ctx: Context) -> dict:
    """The connection a tool call came through: {"user_id", "connection_id", "app", "scope"}."""
    request = ctx.request_context.request
    caller = request.scope.get("state", {}).get(CALLER_KEY) if request is not None else None
    if caller is None:   # only reachable if a call arrives some way other than McpEndpoint
        raise PermissionError("Not signed in to Mitfah.")
    return caller


def count_call(caller: dict) -> None:
    """
    Counts one tool call against the connection's RATE_LIMIT.

    @raises ToolError  When the connection has used its calls for the minute.
    """
    if not _rate_limiter.hit(_rate, "mcp", str(caller["connection_id"])):
        raise ToolError(
            "This app has asked Mitfah too many times in the last minute. Wait a minute, then try again."
        )


# ── Networks ──────────────────────────────────────────────────────────────────


def _names(chain_id: int) -> set[str]:
    """Every way a caller may name this network, lower case."""
    names = {str(chain_id), get_chain_display_name(chain_id).lower(), *_ALIASES.get(chain_id, ())}
    if chain_id in CHAIN_NAME_BY_ID:
        names.add(CHAIN_NAME_BY_ID[chain_id])
        names.add(network_name(chain_id))
    return names


def resolve_network(user_id: int, network: str | None) -> int:
    """
    The chain a call acts on: one where the account has a wallet, named by `network`, or the only
    one when the account has a single wallet and `network` is left out.

    @raises ToolError  When the account has no wallet, `network` names none of its wallets' networks,
                       or it is left out while the account has wallets on several.
    """
    chains = get_wallet_chains(user_id)
    if not chains:
        raise ToolError("This Mitfah account has no smart wallet yet: the user creates one on Mitfah's website.")
    listed = ", ".join(get_chain_display_name(c) for c in chains)
    if network is None or not str(network).strip():
        if len(chains) == 1:
            return chains[0]
        raise ToolError(f"Say which network with `network`: this account has wallets on {listed}.")
    wanted = str(network).strip().lower()
    for chain_id in chains:
        if wanted in _names(chain_id):
            return chain_id
    raise ToolError(f"This account has no wallet on {network}. Its wallets are on {listed}.")


# ── Wrapping a tool ───────────────────────────────────────────────────────────

NETWORK_ARG = (
    'The network to act on: a name such as "Ethereum", "Arbitrum One", "Base", "BNB Smart Chain" or '
    '"Sepolia", or its chain id. Only networks where the account has a wallet (get_account lists them). '
    "May be left out when the account has a wallet on just one network."
)
TOKEN = 'A token\'s ticker, e.g. "usdc". "eth" means the network\'s own asset (BNB on BNB Smart Chain).'


@dataclass(frozen=True)
class Spec:
    """One wallet tool as an AI app sees it."""

    tool: BaseTool
    title: str
    description: str
    args: dict[str, str] = field(default_factory=dict)   # each argument's description, by name
    on_a_network: bool = True                             # False: the tool reads nothing per network


def _arguments_model(spec: Spec) -> type[ArgModelBase]:
    """The arguments the SDK validates a call against: the tool's own, plus `network`."""
    fields: dict[str, Any] = {}
    if spec.on_a_network:
        fields["network"] = (str | None, Field(None, description=NETWORK_ARG))
    for name, info in spec.tool.tool_call_schema.model_fields.items():
        default = ... if info.is_required() else info.default
        fields[name] = (info.annotation, Field(default, description=spec.args[name]))
    return create_model(f"{spec.tool.name}_arguments", __base__=ArgModelBase, **fields)


def published_schema(model: type[ArgModelBase]) -> dict:
    """
    What tools/list shows for `model`: optional arguments as plain types (not "X or null"), with no
    null default and no generated titles.
    """
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
        options = prop.get("anyOf")
        if options and {"type": "null"} in options:
            rest = [o for o in options if o != {"type": "null"}]
            if len(rest) == 1:
                del prop["anyOf"]
                prop.update(rest[0])
            else:
                prop["anyOf"] = rest
            if prop.get("default", 0) is None:
                del prop["default"]
    return schema


def _thread_pool() -> anyio.CapacityLimiter:
    """The tools' own pool of POOL_SIZE threads, for the running event loop."""
    global _pool
    loop = asyncio.get_running_loop()
    if _pool is None or _pool[0] is not loop:
        _pool = (loop, anyio.CapacityLimiter(POOL_SIZE))
    return _pool[1]


def _run(tool: BaseTool, user_id: int, chain_id: int | None, args: dict) -> Any:
    """Runs a tool for `user_id` on `chain_id`, on the worker thread this is called on."""
    runtime = ToolRuntime(
        state={}, context=AgentContext(user_id=user_id), config={},
        stream_writer=lambda _chunk: None, tool_call_id=None, store=None,
    )
    if chain_id is None:
        return tool.func(runtime=runtime, **args)
    with acting_network(user_id, network_name(chain_id), chain_id):
        return tool.func(runtime=runtime, **args)


def wrap(spec: Spec) -> Tool:
    """The MCP tool for one wallet tool."""
    model = _arguments_model(spec)

    async def call(ctx: Context, **args: Any) -> Any:
        caller = caller_of(ctx)
        count_call(caller)
        network = args.pop("network", None)
        chain_id = resolve_network(caller["user_id"], network) if spec.on_a_network else None
        try:
            result = await anyio.to_thread.run_sync(
                functools.partial(_run, spec.tool, caller["user_id"], chain_id, args), limiter=_thread_pool(),
            )
            # The SDK sends a list as one content block per item (none for an empty list) and None as
            # nothing at all: send either as one JSON text instead, as it sends a dict.
            return json.dumps(result) if result is None or isinstance(result, (list, tuple)) else result
        except ToolException as e:
            raise ToolError(redact(e.args[0] if e.args else "The tool refused.")) from e
        except Exception as e:
            log.exception("MCP tool %s failed (connection %s)", spec.tool.name, caller["connection_id"])
            raise ToolError(UNEXPECTED) from e

    return Tool(
        fn=call,
        name=spec.tool.name,
        title=spec.title,
        description=spec.description,
        parameters=published_schema(model),
        fn_metadata=FuncMetadata(arg_model=model),
        is_async=True,
        context_kwarg="ctx",
        annotations=READ_ONLY,
    )


# ── The reads ─────────────────────────────────────────────────────────────────
# Each description is the whole of what the app's model knows about the tool: what it answers, how to
# read the answer, and the rule that matters there. Short, because every app sends all of them with
# every message.

READS = [
    Spec(
        get_wallet_status, "Wallet status",
        "The wallet's spending limit and state on one network: the USD limit per window, what has counted "
        "toward it and what is left, which tokens count, any limits on single tokens (in their own units), "
        "whether the owner has paused the wallet, and how many of the user's passkeys can approve "
        "transactions now. If limit_applies is false the wallet has no USD limit: say so, and don't read "
        "daily_limit_usd or remaining_usd as \"$0 left\". If session_active is false, no passkey can approve "
        "a transaction until the owner approves one on Mitfah's website (Controls, Devices). Read-only.",
    ),
    Spec(
        get_supported_tokens, "Supported tokens",
        "The tokens the wallet can use on one network. `native` is the network's own asset (ETH, or BNB on "
        "BNB Smart Chain); tools call it \"eth\" on every network. `listed` tokens have a price. `custom` "
        "tokens were added by the user: they have no price and never count toward the spending limit, so "
        "never state or guess a dollar value for one. Read-only.",
    ),
    Spec(
        get_all_contacts, "Contacts",
        "The user's saved contacts, by name and address. Payments can go only to saved contacts, named by "
        "name. Contacts are added and removed only on Mitfah's website, never through this connection. "
        "Read-only.",
        on_a_network=False,
    ),
    Spec(
        get_contact, "Contact",
        "A saved contact's address, by name (case-insensitive); null when there is no such contact. "
        "Payments go only to saved contacts: if one is missing, tell the user to add it on Mitfah's "
        "website. An address typed into the conversation can never be used instead. Read-only.",
        {"name": 'The contact\'s name, e.g. "Sandy".'},
        on_a_network=False,
    ),
    Spec(
        get_eth_balance, "Native balance",
        "The wallet's balance of the network's own asset in whole units: ETH on Ethereum, Arbitrum and Base, "
        "BNB on BNB Smart Chain. Report it under the `asset` the result names, whatever the tool is called. "
        "Read-only.",
    ),
    Spec(
        get_erc20_balance, "Token balance",
        "A token's balance in whole units: the wallet's own, or a saved contact's when `contact` is given. "
        "For the network's own asset (ETH, BNB) use get_eth_balance. Read-only.",
        {"token": TOKEN, "contact": "A saved contact's name, to read their balance instead. Leave it out for the wallet's own."},
    ),
    Spec(
        get_price, "Token price",
        "A token's current USD price from Mitfah's price oracle, or what `amount` of it is worth. Not for "
        "working out what a swap returns: get_quote_out and get_quote_in read the exchange's pools. Read-only.",
        {"token": TOKEN, "amount": "An amount in whole units, to value instead of one unit."},
    ),
    Spec(
        get_quote_out, "Swap preview (amount in)",
        "How much of token_out the network's exchange (Uniswap V3; PancakeSwap V2 on BNB Smart Chain) would "
        "give for exactly amount_in of token_in, by the better of the direct pool and the route through the "
        "wrapped native token. A preview: nothing is traded. Show the user the two amounts, not the path or "
        "fees. Read-only.",
        {"token_in": "The token spent. " + TOKEN, "token_out": "The token received. " + TOKEN,
         "amount_in": "How much token_in to spend, in whole units (100 for 100 USDC)."},
    ),
    Spec(
        get_quote_in, "Swap preview (amount out)",
        "How much of token_in the network's exchange (Uniswap V3; PancakeSwap V2 on BNB Smart Chain) would "
        "take to give exactly amount_out of token_out, by the cheaper of the direct pool and the route through "
        "the wrapped native token. A preview: nothing is traded. Show the user the two amounts, not the path "
        "or fees. Read-only.",
        {"token_in": "The token spent. " + TOKEN, "token_out": "The token received. " + TOKEN,
         "amount_out": "How much token_out to receive, in whole units."},
    ),
    Spec(
        get_positions, "Liquidity positions",
        "The wallet's Uniswap V3 liquidity positions on one network (Ethereum, Sepolia, Arbitrum One, Base): "
        "for each, its id, pair, pool fee, price range, today's price, whether the price is inside the "
        "range, what it holds now and the fees it has earned. Positions in tokens the wallet doesn't know "
        "are left out. Describe them in plain words, not field names. Read-only.",
    ),
    Spec(
        get_pool_quote, "Pool deposit preview",
        "PancakeSwap V2 (BNB Smart Chain only): how much of token_b goes with a deposit of amount_a of "
        "token_a, at the pool's current ratio. A preview: nothing is deposited. Read-only.",
        {"token_a": "The first token. " + TOKEN, "token_b": "The second token. " + TOKEN,
         "amount_a": "How much token_a to deposit, in whole units."},
    ),
    Spec(
        get_lp_amounts, "Liquidity withdrawal preview",
        "PancakeSwap V2 (BNB Smart Chain only): what burning lp_amount of a pair's liquidity tokens would "
        "return of each token, at the pool's current reserves. A preview: nothing is withdrawn. Read-only.",
        {"token_a": "The pair's first token. " + TOKEN, "token_b": "The pair's second token. " + TOKEN,
         "lp_amount": "How many liquidity tokens to burn, in whole units."},
    ),
    Spec(
        get_liquidity_token_balance, "Liquidity token balance",
        "PancakeSwap V2 (BNB Smart Chain only): the wallet's balance of a pair's liquidity tokens, in whole "
        "units. Read-only.",
        {"token_a": "The pair's first token. " + TOKEN,
         "token_b": "The pair's second token; the native asset when left out. " + TOKEN},
    ),
]


def read_tools() -> list[Tool]:
    """The read tools, wrapped for MCP (phase 2a)."""
    return [wrap(spec) for spec in READS]
