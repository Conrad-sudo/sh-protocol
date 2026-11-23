"""
The wallet's tools as AI apps see them over MCP (mcp_server.py): the same tools the web chat's
assistant calls (tools/), each wrapped once, with a description written for a model that has never
seen our system prompt.

The reads (phase 2a) and the quotes (phase 2b). What a wrapper adds to the tool it wraps:

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

A quote tool (Spec.quotes) runs as a turn of its own, as a chat message does: it holds the account's
one turn (routes.chat._chat_turn, so never beside a message the web chat is answering), counts
toward the account's message limits (QUOTE_LIMITS), and gets a fresh turn id. Whatever quote that
turn raised is offered as the web chat offers its card (signing.offer), with origin MCP, which gives
it a link token: the result, written HERE from the quote and not from the tool's chat text, carries
the /c/<token> link where the user approves it with their passkey. No tool can send. The offer voids
any other card waiting on that network, in the web chat or Telegram (one card at a time, which is
what stopped a double payment on 2026-10-22); a Telegram card it voids loses its buttons, as a
cancel's does.

get_transaction_status and cancel_transaction answer for a quote an AI app raised, by its id.

A read-only personal token (mcp_auth.READ_SCOPE) gets the reads and get_transaction_status only: the
tools that prepare or cancel are left out of its tools/list (hide_preparing_tools) and refused if it
calls one anyway (require_prepares).
"""
import asyncio
import functools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Annotated, Any

import anyio
import anyio.to_thread
from fastapi import HTTPException
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, ToolException
from limits import parse, parse_many, storage, strategies
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator, Field, create_model

import mcp_auth
import quotes
import signing
import telegram_notify
from agent_context import AgentContext
from chat_limits import CHAT_RATE_LIMIT
from constants import get_chain_display_name
from db import acting_network, cancel_sign_request, get_sign_request_by_quote, get_wallet_chains
from db.sign_requests import CANCELLED, FAILED, OPEN, SENDING, SENT
from network_config import CHAIN_NAME_BY_ID, network_name
from redact import redact
from routes.chat import _chat_turn
from routes.history import get_one_transaction
from smart_wallet_agent import _next_turn_id
from tools.positions import (
    add_to_position,
    collect_fees,
    get_positions,
    open_position,
    suggest_price_ranges,
    withdraw_from_position,
)
from tools.transfers import send_eth, transfer_erc20, wrap_eth
from tools.uniswap import (
    add_liquidity,
    get_liquidity_token_balance,
    get_lp_amounts,
    get_pool_quote,
    get_quote_in,
    get_quote_out,
    remove_liquidity,
    swap,
)
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

# Quotes count toward the same limits as messages to the assistant, per account. Counted apart from
# the web chat's (slowapi) and the bot's, as those two already are from each other (chat_limits.py).
QUOTE_LIMITS = parse_many(CHAT_RATE_LIMIT)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# A quote changes nothing on chain, but it is a step toward a transaction: apps ask the user first.
PREPARES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
CANCELS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

BUSY = "Mitfah is answering another request for this account right now. Try again in a moment."
READ_ONLY_TOKEN = (
    "This connection is read-only: it can't prepare transactions. To do that, make a token that can "
    "\"read and prepare\" in Mitfah's Controls, under Connected apps."
)

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


def require_prepares(caller: dict) -> None:
    """
    Refuses a read-only personal token a tool that prepares or cancels a transaction. The rule:
    hiding those tools from its tools/list (hide_preparing_tools) is only a courtesy to its model.

    @raises ToolError  When the caller may only read.
    """
    if not mcp_auth.prepares(caller):
        raise ToolError(READ_ONLY_TOKEN)


def count_quote(user_id: int) -> None:
    """
    Counts one quote against the account's QUOTE_LIMITS: all of them, or none when one is used up.

    @raises ToolError  When the account has made its quotes for the minute or the day.
    """
    key = ("mcp-quote", str(user_id))
    if not all(_rate_limiter.test(limit, *key) for limit in QUOTE_LIMITS):
        raise ToolError("This account has asked for too many transactions for now. Wait a while, then try again.")
    for limit in QUOTE_LIMITS:
        _rate_limiter.hit(limit, *key)


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


def _number_as_text(value):
    """
    A chain id sent as a number, as text. The schema says string, but get_account shows chain ids
    as numbers and NETWORK_ARG accepts one, so an app may well pass 11155111 rather than "11155111".
    """
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


# `network` as the SDK validates it: text, or a number taken as text. Published as a plain string.
Network = Annotated[str | None, BeforeValidator(_number_as_text)]


@dataclass(frozen=True)
class Spec:
    """One wallet tool as an AI app sees it."""

    tool: BaseTool
    title: str
    description: str
    args: dict[str, str] = field(default_factory=dict)   # each argument's description, by name
    on_a_network: bool = True                             # False: the tool reads nothing per network
    quotes: bool = False                                  # True: it raises a quote (a write tool)


def _arguments_model(spec: Spec) -> type[ArgModelBase]:
    """The arguments the SDK validates a call against: the tool's own, plus `network`."""
    fields: dict[str, Any] = {}
    if spec.on_a_network:
        fields["network"] = (Network, Field(None, description=NETWORK_ARG))
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


def _run(tool: BaseTool, user_id: int, chain_id: int | None, args: dict, turn_id: int = 0) -> Any:
    """Runs a tool for `user_id` on `chain_id`, on the worker thread this is called on."""
    runtime = ToolRuntime(
        state={}, context=AgentContext(user_id=user_id, turn_id=turn_id), config={},
        stream_writer=lambda _chunk: None, tool_call_id=None, store=None,
    )
    if chain_id is None:
        return tool.func(runtime=runtime, **args)
    with acting_network(user_id, network_name(chain_id), chain_id):
        return tool.func(runtime=runtime, **args)


def _quote(tool: BaseTool, caller: dict, chain_id: int, args: dict) -> Any:
    """
    Runs a write tool as a turn of its own and offers the quote it raised, on this worker thread.

    @return  The result for the app (quote_result), or the tool's own answer when it raised no quote.
    @raises HTTPException  409 or 429 from _chat_turn while the account's turn or every slot is taken.
    """
    user_id = caller["user_id"]
    with _chat_turn(user_id):
        turn_id = _next_turn_id()
        told = _run(tool, user_id, chain_id, args, turn_id)
        offered = signing.offer(user_id, chain_id, turn_id, signing.MCP)
        if offered is None:
            # Nothing to offer: no quote, or (never yet) more than one. Leave none behind.
            for pending in quotes.raised_in(user_id, chain_id, turn_id):
                quotes.drop(user_id, pending.quote_id, chain_id)
            return told
    for request in offered.superseded:
        telegram_notify.tell(request, f"Cancelled: a newer request from {caller['name']} replaced it. Nothing was sent.")
    return quote_result(offered)


def approve_url(token: str) -> str:
    """Where a quote is approved: the confirm page for its link token, on this server's own site."""
    return f"{mcp_auth.issuer()}/c/{token}"


def quote_result(offered: signing.Offer) -> dict:
    """
    What the app gets for a quote: written here from the quote, as a card is, never from the tool's
    chat text. The approval page shows the same action, network and fees.
    """
    pending = offered.pending
    minutes = max(1, round((pending.expires_at - time.time()) / 60))
    result = {
        "status": "waiting_for_approval",
        "quote_id": pending.quote_id,
        "network": get_chain_display_name(pending.chain_id),
        "action": pending.action,
        "fees": quotes.fee_text(pending.cost),
    }
    if pending.slippage_bps is not None:
        result["slippage"] = f"{pending.slippage_bps / 100:g}%"
    if pending.charged_usd is not None:
        result["counts_toward_limit"] = f"${pending.charged_usd:,.2f}"
    result |= {
        "expires_in_minutes": minutes,
        "approve_url": approve_url(offered.token),
        "next_step": (
            "Nothing has been sent. Show the user approve_url as a link, with the action, the network and "
            f"the fees, and say it expires in {minutes} minutes. They open it and approve with their passkey "
            "(Face ID, fingerprint or PIN) on Mitfah's own page, which shows the same details. You can't send "
            "or approve it yourself. To see what happened, call get_transaction_status with the quote_id. "
            "A new quote on this network replaces this one."
        ),
    }
    return result


def wrap(spec: Spec) -> Tool:
    """The MCP tool for one wallet tool."""
    model = _arguments_model(spec)

    async def call(ctx: Context, **args: Any) -> Any:
        caller = caller_of(ctx)
        count_call(caller)
        if spec.quotes:
            require_prepares(caller)
        network = args.pop("network", None)
        chain_id = resolve_network(caller["user_id"], network) if spec.on_a_network else None
        if spec.quotes:
            count_quote(caller["user_id"])
            work = functools.partial(_quote, spec.tool, caller, chain_id, args)
        else:
            work = functools.partial(_run, spec.tool, caller["user_id"], chain_id, args)
        try:
            result = await anyio.to_thread.run_sync(work, limiter=_thread_pool())
            # The SDK sends a list as one content block per item (none for an empty list) and None as
            # nothing at all: send either as one JSON text instead, as it sends a dict.
            return json.dumps(result) if result is None or isinstance(result, (list, tuple)) else result
        except ToolException as e:
            raise ToolError(redact(e.args[0] if e.args else "The tool refused.")) from e
        except HTTPException as e:   # _chat_turn: the account's turn, or every slot, is taken
            raise ToolError(BUSY) from e
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
        annotations=PREPARES if spec.quotes else READ_ONLY,
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


READS.append(Spec(
    suggest_price_ranges, "Price ranges for a position",
    "The price ranges to offer for a new Uniswap V3 position in a pair, at today's price: narrow, medium, wide "
    "and full, with how a deposit would split between the two tokens. Show the user every option and let them "
    "pick before open_position, which takes the pick by name. Read-only.",
    {"token_a": "One of the pair's tokens. " + TOKEN,
     "token_b": "The other; the native asset when left out. " + TOKEN,
     "fee": "A pool fee tier: 100, 500, 3000 or 10000. Leave it out unless the user names one: the pool "
            "with the most liquidity is used."},
))

# ── The quotes ────────────────────────────────────────────────────────────────
# Each ends with QUOTED: the app's model has to know, from the description alone, that nothing it calls
# sends anything.

QUOTED = (
    " Nothing is sent: this prepares a quote, and the result's approve_url is where the user approves it with "
    "their passkey. A newer quote on the same network replaces it."
)
CONTACT = 'A saved contact\'s name, e.g. "Sandy". Contacts are added only on Mitfah\'s website; an address can\'t be used.'
SLIPPAGE = (
    "How far the price may move against the user before it is refused, in basis points: 50 (0.5%) unless the "
    "user gives a figure; at most 1200."
)
POSITION = "The position's id, from get_positions."
WRAPPED = "Use WETH instead of ETH for the position's WETH side. Only when the user asks."

QUOTES = [
    Spec(
        send_eth, "Send ETH or BNB",
        "Prepares sending the network's own asset (ETH; BNB on BNB Smart Chain) to a saved contact. Payees must be "
        "saved contacts, added only on Mitfah's website. The fees come out of the same balance, so the whole "
        "balance can't be sent." + QUOTED,
        {"recipient": CONTACT, "amount_eth": "How much to send, in whole units (1.5, not wei)."},
        quotes=True,
    ),
    Spec(
        transfer_erc20, "Send a token",
        "Prepares sending a token to a saved contact. Payees must be saved contacts, added only on Mitfah's "
        "website. For the network's own asset (ETH, BNB) use send_eth." + QUOTED,
        {"token": TOKEN, "recipient": CONTACT, "amount": "How much to send, in whole units (100 for 100 USDC)."},
        quotes=True,
    ),
    Spec(
        wrap_eth, "Wrap ETH or BNB",
        "Prepares wrapping the network's own asset into its wrapped token, 1:1 (ETH to WETH; BNB to WBNB)." + QUOTED,
        {"amount_eth": "How much to wrap, in whole units."},
        quotes=True,
    ),
    Spec(
        swap, "Swap",
        "Prepares a swap on the network's exchange (Uniswap V3; PancakeSwap V2 on BNB Smart Chain), by the best "
        "route. Give exactly one amount: amount_in when the user fixes what they spend, amount_out when they fix "
        "what they receive. \"eth\" is the native asset on either side; \"weth\" or \"wbnb\" only for the wrapped "
        "token. To deliver the output straight to a saved contact, name them in recipient." + QUOTED,
        {"token_in": "The token spent. " + TOKEN, "token_out": "The token received. " + TOKEN,
         "amount_in": "How much token_in to spend, in whole units. Leave it out when giving amount_out.",
         "amount_out": "How much token_out to receive, in whole units. Leave it out when giving amount_in.",
         "slippage_bps": SLIPPAGE,
         "recipient": "A saved contact's name, to receive token_out directly. Leave it out to keep it in the wallet."},
        quotes=True,
    ),
    Spec(
        open_position, "Open a liquidity position",
        "Prepares opening a Uniswap V3 liquidity position (Ethereum, Sepolia, Arbitrum One, Base): amount_a of "
        "token_a and what the range needs of token_b. Call suggest_price_ranges first and let the user pick: pass "
        "their pick as range, or their own min_price and max_price (quote per base, as suggest_price_ranges shows "
        "prices). Never pick a range for them. token_a can't be the native asset: if the user names only an ETH "
        "amount, ask how much of the other token." + QUOTED,
        {"token_a": "The token whose amount is fixed; not the native asset. " + TOKEN,
         "amount_a": "How much token_a to deposit, in whole units.",
         "token_b": "The pool's other token; the native asset when left out. " + TOKEN,
         "range": "The option the user picked from suggest_price_ranges.",
         "min_price": "The bottom of the user's own range, quote per base. Only without range.",
         "max_price": "The top of the user's own range, quote per base. Only without range.",
         "fee": "A pool fee tier. Leave it out: a picked range names its pool.",
         "slippage_bps": SLIPPAGE},
        quotes=True,
    ),
    Spec(
        add_to_position, "Add to a position",
        "Prepares adding to one of the wallet's Uniswap V3 positions, keeping its range: amount of token (one of "
        "the position's two, not the native asset) and what the range needs of the other." + QUOTED,
        {"position_id": POSITION, "token": "The token whose amount is fixed. " + TOKEN,
         "amount": "How much of it to deposit, in whole units.", "wrapped": WRAPPED, "slippage_bps": SLIPPAGE},
        quotes=True,
    ),
    Spec(
        withdraw_from_position, "Withdraw from a position",
        "Prepares taking a percent of one of the wallet's Uniswap V3 positions out, with all the fees it has "
        "earned; 100 closes it. Ask the user how much if they haven't said." + QUOTED,
        {"position_id": POSITION, "percent": "How much to take out: above 0, at most 100.", "wrapped": WRAPPED,
         "slippage_bps": SLIPPAGE},
        quotes=True,
    ),
    Spec(
        collect_fees, "Collect position fees",
        "Prepares collecting the fees one of the wallet's Uniswap V3 positions has earned, leaving the position "
        "as it is." + QUOTED,
        {"position_id": POSITION, "wrapped": WRAPPED},
        quotes=True,
    ),
    Spec(
        add_liquidity, "Add liquidity",
        "PancakeSwap V2 (BNB Smart Chain only): prepares depositing amount_a of token_a and the matching amount "
        "of token_b at the pool's ratio. token_a can't be the native asset." + QUOTED,
        {"token_a": "The token whose amount is fixed; not the native asset. " + TOKEN,
         "amount_a": "How much token_a to deposit, in whole units.",
         "token_b": "The pool's other token; the native asset when left out. " + TOKEN, "slippage_bps": SLIPPAGE},
        quotes=True,
    ),
    Spec(
        remove_liquidity, "Remove liquidity",
        "PancakeSwap V2 (BNB Smart Chain only): prepares burning lp_amount of a pair's liquidity tokens; both "
        "tokens come back at the pool's ratio. get_liquidity_token_balance shows how many the wallet holds." + QUOTED,
        {"token_a": "One of the pool's tokens. " + TOKEN, "lp_amount": "How many liquidity tokens to burn, in whole units.",
         "token_b": "The pool's other token; the native asset when left out. " + TOKEN, "slippage_bps": SLIPPAGE},
        quotes=True,
    ),
]


# ── After a quote: its status, and cancelling it ──────────────────────────────

QuoteId = Annotated[str, Field(description="The quote_id a quote tool returned.")]
NO_QUOTE = "No quote with that id was made through an AI app on this account."


def _app_request(user_id: int, quote_id: str) -> dict:
    """
    The sign request of a quote an AI app raised for this account, on whichever of its networks.

    @raises ToolError  When there is none: unknown, another account's, or made in the web chat or
                       Telegram (an app gets only the ids of its own quotes).
    """
    for chain_id in get_wallet_chains(user_id):
        request = get_sign_request_by_quote(user_id, chain_id, quote_id)
        if request is not None and request["origin"] == signing.MCP:
            return request
    raise ToolError(NO_QUOTE)


def transaction_status(user_id: int, quote_id: str) -> dict:
    """
    Where a quote stands, from its sign request and, once sent, its History row (settled from the
    chain if nothing is waiting on it, as the History tab does).
    """
    request = _app_request(user_id, quote_id)
    result = {
        "quote_id": quote_id,
        "network": get_chain_display_name(request["chain_id"]),
        "action": signing.thaw(request)["action"],
    }
    status, now = request["status"], int(time.time())
    if status == OPEN and request["expires_at"] >= now:
        return result | {"status": "waiting_for_approval", "expires_in_seconds": request["expires_at"] - now}
    if status == OPEN:
        return result | {"status": "expired", "note": "It was never approved, so nothing was sent."}
    if status == SENDING:
        return result | {"status": "sending", "note": "The user approved it; it is being sent now."}
    if status == CANCELLED:
        return result | {"status": "cancelled", "note": "Nothing was sent."}
    if status == FAILED:
        return result | {"status": "failed", "reason": request["outcome"], "note": "Nothing was sent."}
    if status != SENT:
        return result | {"status": status}
    # The request's outcome is what the page said as it sent ("Sending — ..."): returned only when
    # there is no History row to say more, as when the broadcast left it unknown whether it went out.
    result["status"] = "sent"
    if request["transaction_id"] is None:
        return result | {"outcome": request["outcome"]}
    try:
        row = get_one_transaction(request["transaction_id"], user_id=user_id)
    except HTTPException:
        return result | {"outcome": request["outcome"]}
    result["tx_hash"] = row["tx_hash"]
    if row["status"] == "confirmed":
        result["status"] = "confirmed"
    elif row["status"] == "failed":
        result |= {"status": "failed", "note": "It was sent but failed on the network."}
    elif row["status"] == "dropped":
        result |= {"status": "failed", "note": "It never made it into a block, and now never will. Nothing was sent."}
    else:
        result["note"] = "On its way: not yet in a block. Check again in a few seconds."
    return result


def cancel_quote(user_id: int, quote_id: str) -> dict:
    """Cancels a quote an AI app raised, if it still waits: its link stops working."""
    request = _app_request(user_id, quote_id)
    quotes.drop(user_id, quote_id, request["chain_id"])
    if cancel_sign_request(request["id"]):
        return {"quote_id": quote_id, "status": "cancelled", "note": "Its approval link no longer works. Nothing was sent."}
    return transaction_status(user_id, quote_id) | {"note": "It was no longer waiting, so there was nothing to cancel."}


async def get_transaction_status(ctx: Context, quote_id: QuoteId) -> dict:
    caller = caller_of(ctx)
    count_call(caller)
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(transaction_status, caller["user_id"], quote_id), limiter=_thread_pool(),
        )
    except ToolError:
        raise
    except Exception as e:
        log.exception("MCP get_transaction_status failed (connection %s)", caller["connection_id"])
        raise ToolError(UNEXPECTED) from e


async def cancel_transaction(ctx: Context, quote_id: QuoteId) -> dict:
    caller = caller_of(ctx)
    count_call(caller)
    require_prepares(caller)
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(cancel_quote, caller["user_id"], quote_id), limiter=_thread_pool(),
        )
    except ToolError:
        raise
    except Exception as e:
        log.exception("MCP cancel_transaction failed (connection %s)", caller["connection_id"])
        raise ToolError(UNEXPECTED) from e


# What a read-only token may not call, and doesn't see in its tools/list.
PREPARING_TOOLS = frozenset({spec.tool.name for spec in QUOTES} | {"cancel_transaction"})


async def hide_preparing_tools(ctx, call_next):
    """
    Server middleware (mcp_server.py): leaves PREPARING_TOOLS out of a read-only caller's tools/list,
    so its model isn't offered tools it would only be refused (require_prepares). Every other
    message, and every other caller, passes through as it is.
    """
    result = await call_next(ctx)
    if ctx.method != "tools/list" or not isinstance(result, dict):
        return result
    request = ctx.request
    caller = request.scope.get("state", {}).get(CALLER_KEY) if request is not None else None
    if caller is None or mcp_auth.prepares(caller):
        return result
    return {**result, "tools": [tool for tool in result.get("tools", []) if tool.get("name") not in PREPARING_TOOLS]}


def wallet_tools() -> list[Tool]:
    """Every wallet tool an AI app gets: the reads, the quotes, and the two that follow a quote."""
    return [
        *(wrap(spec) for spec in READS),
        *(wrap(spec) for spec in QUOTES),
        Tool.from_function(
            get_transaction_status, title="Transaction status", annotations=READ_ONLY,
            description=(
                "What happened to a quote this app prepared, by its quote_id: waiting_for_approval (and for how "
                "long), sending, sent (on its way), confirmed (in a block, with tx_hash), failed (with the reason), "
                "cancelled or expired. Read-only."
            ),
        ),
        Tool.from_function(
            cancel_transaction, title="Cancel a quote", annotations=CANCELS,
            description=(
                "Cancels a quote that is still waiting for approval, by its quote_id: its approve_url stops "
                "working. Use it when the user changes their mind. Nothing was sent, so nothing is reversed."
            ),
        ),
    ]
