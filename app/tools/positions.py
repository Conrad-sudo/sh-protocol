"""
Uniswap V3 liquidity: price ranges to choose from, opening and adding to a position, the wallet's
positions, and taking a position or its fees out. Calldata comes from the in-repo
langchain_uniswap_v3 (toolkits.get_uniswap_tools); get_positions shows the dashboard's own reader
(positions.read_positions).

On Uniswap V3 a deposit is a position -- an NFT the position manager keeps it under -- with a price
range, and it earns fees only while the price is inside that range. Three things follow:

  - The range is the user's choice. suggest_price_ranges works out a few options and keeps them
    here, on the server; open_position(range="narrow") then deposits into that option's exact
    ticks, never into the model's own copy of its prices.
  - A position is named by its id, and anyone can mint one to any address. Every tool that takes an
    id refuses a position this wallet doesn't own, or one built from a token the user doesn't know:
    the rule the dashboard hides spam positions by.
  - They apply only where the chain trades on Uniswap V3 (_require_exchange). BSC's liquidity is
    PancakeSwap V2's LP tokens, in tools/uniswap.py.
"""
import math
import time
from dataclasses import dataclass
from typing import Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import ToolException
from web3 import Web3

from agent_context import AgentContext
from constants import get_chain_display_name
from contracts import (
    load_session_handler,
)
from positions import PositionsReadError, read_positions
from toolkits import get_uniswap_tools
from tools.common import (
    DEFAULT_SLIPPAGE_BPS,
    SENT,
    _check_slippage,
    _is_native,
    _refuse_same_token,
    _resolve,
    _token_address,
    _token_label,
    _whole,
)
from tools.quoting import _quote_plan
from tools.uniswap import _require_exchange

# How long the options of a suggest_price_ranges can be opened by name. They are centred on the
# price when they were read: a user who comes back much later should be shown today's options, not
# quoted a range around a price the market has left.
RANGE_OPTIONS_TTL_SECS = 15 * 60


@dataclass(frozen=True)
class _RangeOptions:
    """The options one suggest_price_ranges offered, and what they were worked out for."""

    pair: frozenset[str]     # the two token addresses, checksummed
    pool: str
    fee: int
    options: dict[str, dict]  # name -> the toolkit's option, exactly as it returned it
    expires: float            # time.monotonic()


# (user_id, chain_id) -> the options of the user's last suggest_price_ranges on that chain: the only
# ones open_position takes by name. One entry, so a pick always means the options the user was
# shown last; per chain, because the user runs a separate wallet and conversation on each. In
# memory, like the quotes: a restart only means suggesting again.
_range_options: dict[tuple[int, int], _RangeOptions] = {}


def _address(user_id: int, token: str) -> str:
    """_resolve, checksummed: the toolkit answers in checksummed addresses, and these are compared with them."""
    return Web3.to_checksum_address(_resolve(user_id, token))


def _price(price: float) -> str:
    """A price for people: six significant figures, thousands separated (2,850.12; 0.000351234)."""
    if price == 0 or not math.isfinite(price):
        return "0" if price == 0 else str(price)
    text = f"{price:,.{max(5 - math.floor(math.log10(abs(price))), 0)}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _amount(value: float) -> str:
    """An amount for people: up to six decimals, or six significant figures below one, never 1e-07."""
    return _whole(value) if abs(value) >= 1 else _price(value)


def _span(min_price: float, max_price: float | None, full_range: bool, base: str, quote: str) -> str:
    """A range as the user reads it: "2,850 – 3,150 USDC per ETH", or the full range."""
    if full_range or max_price is None:
        return "the full range: every price"
    return f"{_price(min_price)} – {_price(max_price)} {quote} per {base}"


def _range_status(row: dict, base: str, quote: str) -> str:
    """
    Where today's price sits against a position's range, and what that means for it. Below the
    range a position holds only the base token, above it only the quote token, and either way it
    earns nothing until the price comes back.

    @param row  Anything with in_range, current_price, min_price and full_range: an open_position
                summary, or a position row.
    """
    now = f"{base} is at {_price(row['current_price'])} {quote} now"
    if row["in_range"]:
        return f"{now}, inside the range, so the position earns fees for as long as the price stays in it"
    only = base if row["full_range"] or row["current_price"] < row["min_price"] else quote
    return (
        f"{now}, outside the range, so the position holds only {only} and earns no fees until the price "
        f"comes back into it"
    )


def _invoke(package_tool, args: dict, names: dict[str, str]):
    """
    Runs a package tool, naming addresses in its refusals the way the user knows them.

    The toolkit speaks addresses ("0xA11CE... needs 2.5 of 0x1c7D... for this deposit"): they mean
    nothing to the user, and a model handed one tends to read it out.

    @param names  Address -> name: the tokens involved, and the wallet ("this wallet").
    """
    try:
        return package_tool.invoke(args)
    except ToolException as e:
        text = str(e.args[0]) if e.args else str(e)
        for address, name in names.items():
            text = text.replace(address, name)
        raise ToolException(text) from e


def _owned_position(user_id: int, uniswap: dict, position_id: int, wallet: str) -> dict:
    """
    The position's row (the toolkit's get_position), after refusing one this wallet doesn't own or
    one built from a token the user doesn't know.

    Ownership is the toolkit's check, made before it reads anything else about the position. The
    token rule is this app's: anyone can mint a position to any address, out of tokens of their
    choosing, so the assistant treats a position it can't name both tokens of as spam, as the
    dashboard does -- it never deposits into one, and never takes one out on the user's behalf.

    @raises ToolException  If the position doesn't exist, isn't this wallet's, or holds a token
                           that is neither listed nor added.
    """
    try:
        position = _invoke(
            uniswap["get_position"],
            {"token_id": position_id, "owner_address": wallet},
            {wallet: "this wallet"},
        )
    except ToolException as e:
        raise ToolException(
            f"{e.args[0]} The assistant can only act on this wallet's own positions: call get_positions "
            f"to see them. Nothing was quoted."
        ) from e
    for address in (position["token0"], position["token1"]):
        try:
            _token_address(user_id, address)
        except ToolException:
            raise ToolException(
                f"Position #{position_id} holds a token this wallet doesn't know ({address}), so the "
                f"assistant won't touch it: anyone can send a wallet a position, and one built from "
                f"unknown tokens is treated as spam. If the user knows the token, they can add it by "
                f"its address in the web app (Dashboard -> Balances -> Add token). Nothing was quoted."
            ) from None
    return position


def _position_names(user_id: int, position: dict, native: bool) -> dict[str, str]:
    """
    Address -> name for a position's two tokens: the wrapped native token as the native coin (ETH)
    when that is what goes in or comes out, else by its own ticker (WETH); any other by its ticker.
    """
    wrapped = _address(user_id, "eth")
    return {
        address: _token_label(user_id, "eth") if native and address == wrapped else _token_label(user_id, address)
        for address in (position["token0"], position["token1"])
    }


def _chosen_range(
    user_id: int, chain_id: int, pair: frozenset[str], range_name: str | None,
    min_price: float | None, max_price: float | None, fee: int | None,
) -> tuple[dict, dict | None, _RangeOptions | None]:
    """
    The range arguments for the toolkit's open_position: a suggested option's, or the user's own.

    A named option passes the toolkit that option's prices exactly as the toolkit returned them,
    and its pool's fee, so the toolkit lands on the very ticks the user was shown: it takes a price
    within a tenth of a tick of one to be that tick. They come from this module's cache, never from
    the model, which would carry over rounded copies -- and could carry over another pair's.

    @return  (min_price / max_price / full_range / fee for the toolkit, the option or None for the
             user's own bounds, the cached suggestion or None).
    @raises ToolException  If a name is given with no current suggestion for this pair, or with a
                           fee other than the one it was worked out for; or if neither a name nor
                           both bounds are given.
    """
    cached = _range_options.get((user_id, chain_id))
    fresh = cached is not None and cached.pair == pair and cached.expires > time.monotonic()
    if range_name is None:
        if min_price is None or max_price is None:
            raise ToolException(
                "Say which range: range= one of the options suggest_price_ranges gave (call it first "
                "and let the user pick), or min_price and max_price for bounds the user named "
                "themselves. Nothing was quoted."
            )
        default_fee = cached.fee if fresh else None
        return {"min_price": min_price, "max_price": max_price, "full_range": False,
                "fee": fee if fee is not None else default_fee}, None, None
    if min_price is not None or max_price is not None:
        raise ToolException(
            "Give either range (one of the suggested options) or min_price and max_price (the user's "
            "own bounds), not both. Nothing was quoted."
        )
    if cached is not None and cached.pair == pair and not fresh:
        raise ToolException(
            f"The range options for this pair are more than {RANGE_OPTIONS_TTL_SECS // 60} minutes old, "
            f"and prices move: call suggest_price_ranges again and show the user today's options. "
            f"Nothing was quoted."
        )
    if not fresh or range_name not in cached.options:
        raise ToolException(
            f"There are no range options for this pair to take '{range_name}' from: call "
            f"suggest_price_ranges for these two tokens first, show the user its options, and use "
            f"the one they pick. Nothing was quoted."
        )
    if fee is not None and fee != cached.fee:
        raise ToolException(
            f"The '{range_name}' range was worked out for the {cached.fee / 10_000:g}% pool, not the "
            f"{fee / 10_000:g}% one: leave fee unset, or call suggest_price_ranges with fee={fee} for "
            f"that pool's options. Nothing was quoted."
        )
    option = cached.options[range_name]
    if option["full_range"]:
        bounds = {"min_price": None, "max_price": None, "full_range": True}
    else:
        bounds = {"min_price": option["min_price"], "max_price": option["max_price"], "full_range": False}
    return {**bounds, "fee": cached.fee}, option, cached


"""
 /*//////////////////////////////////////////////////////////////
                       UNISWAP V3 POSITIONS
//////////////////////////////////////////////////////////////*/
"""
@tool
def suggest_price_ranges(
    runtime: ToolRuntime[AgentContext], token_a: str, token_b: str = "eth", fee: int | None = None
) -> dict:
    """
    Works out the price ranges to offer for a new Uniswap V3 position in a pair, at today's price:
    narrow, medium and wide bands around it, and the full range. A position earns fees only while
    the price is inside its range: a narrow range earns more while it lasts and goes out of range
    sooner.

    Call this first whenever the user wants to provide liquidity, show them every option, and let
    them pick one (or give their own lower and upper price) before calling open_position. The
    options are kept on the server: open_position(range="<name>") deposits into exactly the range
    shown.

    Args:
        token_a: One of the pair's tokens (e.g. "usdc").
        token_b: The other. Defaults to "eth", the native asset, as open_position does.
        fee: Optional pool fee tier: 100 (0.01%), 500 (0.05%), 3000 (0.3%) or 10000 (1%). Leave it
             unset unless the user names one: the pool with the most liquidity is used.

    Returns:
        The pair, its pool's fee, today's price and each option: its name, its price range, how wide
        it is, and how a deposit splits between the two tokens by value at today's price.
    """
    user_id = runtime.context.user_id
    print("Running suggest_price_ranges")
    chain_id = _require_exchange(user_id, "uniswap-v3")
    _refuse_same_token(user_id, token_a, token_b, "a pool needs two different tokens")
    address_a, address_b = _address(user_id, token_a), _address(user_id, token_b)
    names = {address_a: _token_label(user_id, token_a), address_b: _token_label(user_id, token_b)}
    found = _invoke(
        get_uniswap_tools(user_id)["suggest_price_ranges"],
        {"token_a": address_a, "token_b": address_b, "fee": fee},
        names,
    )
    _range_options[(user_id, chain_id)] = _RangeOptions(
        pair=frozenset((address_a, address_b)),
        pool=found["pool"],
        fee=found["fee"],
        options={option["name"]: option for option in found["options"]},
        expires=time.monotonic() + RANGE_OPTIONS_TTL_SECS,
    )
    base, quote = names[found["base_token"]], names[found["quote_token"]]
    return {
        "pair": f"{base}/{quote}",
        "pool_fee": f"{found['fee_pct']:g}%",
        "current_price": f"{_price(found['current_price'])} {quote} per {base}",
        "options": [
            {
                "range": option["name"],
                "prices": _span(option["min_price"], option["max_price"], option["full_range"], base, quote),
                "width": "every price" if option["full_range"] else f"±{option['width_pct']:g}% around today's price",
                "deposit_split": f"{option['base_share_pct']:g}% {base}, {option['quote_share_pct']:g}% {quote}",
            }
            for option in found["options"]
        ],
        "next_step": (
            "Show the user every option: its name, its prices, today's price and how a deposit "
            "splits. Say in plain words that a narrower range earns more fees while the price stays "
            "inside it but leaves it sooner, and that outside its range a position earns nothing and "
            "holds only one of the two tokens. Then STOP and let the user pick one, or name their own "
            "lower and upper price. Never pick for them. Then call open_position with range=<their "
            "pick>, or min_price and max_price for their own bounds. If the user ALREADY named one of "
            "these ranges and an amount, that is their pick: call open_position now, in this turn, "
            "and show its quote instead of the options."
        ),
    }


@tool
def open_position(
    runtime: ToolRuntime[AgentContext],
    token_a: str,
    amount_a: float,
    token_b: str = "eth",
    range: Literal["narrow", "medium", "wide", "full"] | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    fee: int | None = None,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Opens a Uniswap V3 liquidity position: deposits `amount_a` of token_a and the amount of token_b
    the range needs at today's price. The position is an NFT the wallet holds; it earns fees only
    while the price is inside its range, and they are not added to it -- collect_fees claims them.

    The range is the user's pick from suggest_price_ranges: pass range="narrow", "medium", "wide"
    or "full", and the position gets exactly the range they were shown (call suggest_price_ranges
    first; a name without its options is refused). Or pass min_price and max_price, in the same
    "quote per base" prices suggest_price_ranges shows (e.g. USDC per ETH), for bounds the user
    named themselves. Never choose a range for them.

    token_b defaults to the native asset (ETH), which is deposited as it is. The fixed amount is
    always token_a's, and token_a can't be the native asset: if the user names only an ETH amount,
    ask how much of the other token instead. Both deposits count toward the spending limit where it
    counts the token (the native asset always), because the position has no price. The approvals
    are granted and used up in the same transaction.

    Args:
        token_a: The token whose amount is fixed (e.g. "usdc"). Not the native asset.
        amount_a: How much token_a to deposit, in whole units (e.g. 1000).
        token_b: The pool's other token. Defaults to "eth", the native asset.
        range: The suggested option the user picked: "narrow", "medium", "wide" or "full".
        min_price: The bottom of the user's own range, quote per base. Only without range.
        max_price: The top of the user's own range, quote per base. Only without range.
        fee: Optional pool fee tier (500 for 0.05%, ...). Leave it unset: a picked range already
             names its pool.
        slippage_bps: How far the price may move before the deposit is refused, in basis points:
                      50 (0.5%) by default, at most 1200 (12%).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running open_position")
    chain_id = _require_exchange(user_id, "uniswap-v3")
    _refuse_same_token(user_id, token_a, token_b, "a pool needs two different tokens")
    if _is_native(token_a):
        native, other = _token_label(user_id, token_a), _token_label(user_id, token_b)
        raise ToolException(
            f"Name how much {other} to deposit first: the position works out the {native} that goes "
            f"with it, so the {native} amount can't be the one that is fixed. suggest_price_ranges "
            f"shows how a deposit splits between the two at each range. Nothing was quoted."
        )
    address_a, address_b = _address(user_id, token_a), _address(user_id, token_b)
    slippage_bps = _check_slippage(slippage_bps)
    bounds, option, cached = _chosen_range(
        user_id, chain_id, frozenset((address_a, address_b)), range, min_price, max_price, fee
    )
    wallet = load_session_handler(user_id).address
    names = {address_a: _token_label(user_id, token_a), address_b: _token_label(user_id, token_b),
             wallet: "this wallet"}
    plan = _invoke(
        get_uniswap_tools(user_id)["open_position"],
        {
            "token_a": address_a,
            "token_b": address_b,
            "amount_a": amount_a,
            "from_address": wallet,
            **bounds,
            # The native side is paid as value and wrapped by the position manager: nothing to wrap first.
            "use_native": _is_native(token_b),
            "slippage_bps": slippage_bps,
        },
        names,
    )
    summary = plan["summary"]
    # The toolkit snaps a suggested price to the tick it came from, so a picked option lands on its
    # own ticks; anything else means the pool is not the one the options were worked out for.
    if option is not None and (
        summary["pool"] != cached.pool
        or (summary["tick_lower"], summary["tick_upper"]) != (option["tick_lower"], option["tick_upper"])
    ):
        raise ToolException(
            f"The '{range}' range no longer matches its pool: call suggest_price_ranges again and show "
            f"the user today's options. Nothing was quoted."
        )

    base, quote = names[summary["base_token"]], names[summary["quote_token"]]
    picked = f"the {range} option" if range else "the user's own bounds"
    return _quote_plan(
        runtime,
        plan,
        details=(
            f"Range: {_span(summary['min_price'], summary['max_price'], summary['full_range'], base, quote)} "
            f"({picked}), in the {summary['fee_pct']:g}% fee pool. {_range_status(summary, base, quote)}. "
            f"{names[address_a]} deposited: {amount_a} (at least {_amount(summary['amount_a_min'])}), "
            f"{names[address_b]} deposited: about {_amount(summary['amount_b'])} (at least "
            f"{_amount(summary['amount_b_min'])}), slippage tolerance {slippage_bps / 100:g}%. The fees "
            f"it earns are not added to the position: they wait in it until they are collected"
        ),
        # Both deposits leave the wallet and the position NFT that comes back has no price, so the
        # limit counts both: the native asset always, a token while it is watched. No lp_pool: the
        # dashboard reads positions off the chain (positions.py), not from the LP-token table.
        legs=[(token_a, amount_a, SENT), (token_b, summary["amount_b"], SENT)],
    )


@tool
def add_to_position(
    runtime: ToolRuntime[AgentContext],
    position_id: int,
    token: str,
    amount: float,
    wrapped: bool = False,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Adds liquidity to one of the wallet's Uniswap V3 positions, keeping its range: deposits
    `amount` of `token` (one of the position's two) and the amount of the other the range needs at
    today's price. get_positions lists the wallet's positions and their ids.

    The fixed amount can't be the native asset: if the user names only an ETH amount, ask how much
    of the other token instead. The position's ETH side is paid in ETH unless `wrapped` is set.
    Both deposits count toward the spending limit, as for open_position.

    Args:
        position_id: The position's id, from get_positions.
        token: The token whose amount is fixed (e.g. "usdc"): one of the position's two, not the
               native asset.
        amount: How much of it to deposit, in whole units.
        wrapped: Pay the position's WETH side as WETH instead of ETH. Only when the user asks.
        slippage_bps: How far the price may move before the deposit is refused, in basis points:
                      50 (0.5%) by default, at most 1200 (12%).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running add_to_position")
    _require_exchange(user_id, "uniswap-v3")
    if _is_native(token):
        native = _token_label(user_id, token)
        raise ToolException(
            f"Name how much of the position's other token to add first: the position works out the "
            f"{native} that goes with it, so the {native} amount can't be the one that is fixed. "
            f"Nothing was quoted."
        )
    address = _address(user_id, token)
    slippage_bps = _check_slippage(slippage_bps)
    uniswap = get_uniswap_tools(user_id)
    wallet = load_session_handler(user_id).address
    position = _owned_position(user_id, uniswap, position_id, wallet)
    if address not in (position["token0"], position["token1"]):
        pair = "/".join(_position_names(user_id, position, native=True).values())
        raise ToolException(
            f"{_token_label(user_id, token)} isn't one of position #{position_id}'s tokens ({pair}). "
            f"Nothing was quoted."
        )
    other = position["token1"] if address == position["token0"] else position["token0"]
    use_native = other == _address(user_id, "eth") and not wrapped
    names = {**_position_names(user_id, position, native=use_native), address: _token_label(user_id, token)}
    plan = _invoke(
        uniswap["add_to_position"],
        {
            "token_id": position_id,
            "token_a": address,
            "amount_a": amount,
            "from_address": wallet,
            "use_native": use_native,
            "slippage_bps": slippage_bps,
        },
        {**names, wallet: "this wallet"},
    )
    summary = plan["summary"]
    base, quote = names[summary["base_token"]], names[summary["quote_token"]]
    return _quote_plan(
        runtime,
        plan,
        details=(
            f"Adds to position #{position_id} ({base}/{quote}), whose range stays "
            f"{_span(summary['min_price'], summary['max_price'], summary['full_range'], base, quote)}; it "
            f"is {'inside' if summary['in_range'] else 'outside'} that range now. {names[address]} "
            f"deposited: {amount} (at least {_amount(summary['amount_a_min'])}), {names[other]} "
            f"deposited: about {_amount(summary['amount_b'])} (at least {_amount(summary['amount_b_min'])}), "
            f"slippage tolerance {slippage_bps / 100:g}%"
        ),
        legs=[(token, amount, SENT), ("eth" if use_native else other, summary["amount_b"], SENT)],
    )


@tool
def get_positions(runtime: ToolRuntime[AgentContext]) -> dict:
    """
    Lists the wallet's Uniswap V3 positions on this network: for each, its id, its pair and pool
    fee, its price range, today's price, whether the price is inside the range, what it holds now
    and the fees it has earned and not yet collected.

    Use it when the user asks about their liquidity or positions, and to find a position's id
    before add_to_position, withdraw_from_position or collect_fees. Positions built from a token the
    user doesn't know are left out (anyone can send a wallet one), as on the dashboard.

    Returns:
        {"positions": [...], "note": ...}. Show the figures in plain words, never the field names.
    """
    user_id = runtime.context.user_id
    print("Running get_positions")
    chain_id = _require_exchange(user_id, "uniswap-v3")
    try:
        rows = read_positions(user_id, chain_id, strict=True)
    except PositionsReadError as e:
        raise ToolException(str(e))
    if not rows:
        return {
            "positions": [],
            "note": f"No positions found for this wallet on {get_chain_display_name(chain_id)}.",
        }

    def holding(sides: list[dict]) -> str:
        return " + ".join(f"{_amount(int(s['raw']) / 10 ** s['decimals'])} {s['ticker'].upper()}" for s in sides)

    positions = []
    for row in rows:
        base, quote = row["base"].upper(), row["quote"].upper()
        positions.append({
            "position_id": row["id"],
            "pair": f"{base}/{quote}",
            "pool_fee": f"{row['fee_pct']:g}%",
            "range": _span(row["min_price"], row["max_price"], row["full_range"], base, quote),
            "current_price": f"{_price(row['current_price'])} {quote} per {base}",
            "in_range": row["in_range"],
            "status": _range_status(row, base, quote),
            "holds": holding(row["amounts"]),
            "unclaimed_fees": holding(row["fees"]),
        })
    return {
        "positions": positions,
        "note": (
            "Out of range, a position earns no fees and holds only one of its two tokens until the "
            "price comes back. Fees wait in the position until collect_fees claims them; "
            "withdraw_from_position takes a percent of it out, with its fees, and 100% closes it."
        ),
    }


@tool
def withdraw_from_position(
    runtime: ToolRuntime[AgentContext],
    position_id: int,
    percent: float,
    wrapped: bool = False,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Takes a percent of one of the wallet's Uniswap V3 positions out, together with all the fees it
    has earned. 100 takes it all and closes the position (its NFT is burned). If the user hasn't
    said how much, ask; get_positions shows what each position holds.

    It counts nothing toward the spending limit: value only comes back. The position's ETH side
    comes back as ETH unless `wrapped` is set.

    Args:
        position_id: The position's id, from get_positions.
        percent: How much of it to take out, above 0 and at most 100.
        wrapped: Receive the position's WETH side as WETH instead of ETH. Only when the user asks.
        slippage_bps: How far below the expected amounts the returns may fall before the withdrawal
                      is refused, in basis points: 50 (0.5%) by default, at most 1200 (12%).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running withdraw_from_position")
    _require_exchange(user_id, "uniswap-v3")
    slippage_bps = _check_slippage(slippage_bps)
    uniswap = get_uniswap_tools(user_id)
    wallet = load_session_handler(user_id).address
    position = _owned_position(user_id, uniswap, position_id, wallet)
    native_out = _address(user_id, "eth") in (position["token0"], position["token1"]) and not wrapped
    names = _position_names(user_id, position, native=native_out)
    plan = _invoke(
        uniswap["withdraw_from_position"],
        {
            "token_id": position_id,
            "percent": percent,
            "from_address": wallet,
            "native_out": native_out,
            "slippage_bps": slippage_bps,
        },
        {**names, wallet: "this wallet"},
    )
    summary = plan["summary"]
    sides = [
        (names[summary["token0"]], summary["expected0"], summary["amount0_min"], summary["fees0"]),
        (names[summary["token1"]], summary["expected1"], summary["amount1_min"], summary["fees1"]),
    ]
    if position["base_token"] != summary["token0"]:
        sides.reverse()
    (base, expected_b, min_b, fees_b), (quote, expected_q, min_q, fees_q) = sides
    closes = " The position is then closed: its NFT is burned." if summary["burns"] else ""
    # Nothing it moves counts toward the limit, so no legs: only the pause and the key are checked.
    return _quote_plan(
        runtime,
        plan,
        details=(
            f"Takes {percent:g}% of position #{position_id} ({base}/{quote}) out: about "
            f"{_amount(expected_b)} {base} and {_amount(expected_q)} {quote} (at least {_amount(min_b)} "
            f"{base} and {_amount(min_q)} {quote}), plus the fees it has earned, {_amount(fees_b)} {base} "
            f"and {_amount(fees_q)} {quote}. Slippage tolerance {slippage_bps / 100:g}%.{closes}"
        ),
    )


@tool
def collect_fees(runtime: ToolRuntime[AgentContext], position_id: int, wrapped: bool = False):
    """
    Collects the fees one of the wallet's Uniswap V3 positions has earned, leaving the position as
    it is. A position earns fees only while the price is inside its range, and they are never added
    back into it: this claims them. Refused when there is nothing to collect.

    It counts nothing toward the spending limit. The ETH side comes back as ETH unless `wrapped` is
    set.

    Args:
        position_id: The position's id, from get_positions.
        wrapped: Receive the position's WETH side as WETH instead of ETH. Only when the user asks.

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running collect_fees")
    _require_exchange(user_id, "uniswap-v3")
    uniswap = get_uniswap_tools(user_id)
    wallet = load_session_handler(user_id).address
    position = _owned_position(user_id, uniswap, position_id, wallet)
    native_out = _address(user_id, "eth") in (position["token0"], position["token1"]) and not wrapped
    names = _position_names(user_id, position, native=native_out)
    plan = _invoke(
        uniswap["collect_fees"],
        {"token_id": position_id, "from_address": wallet, "native_out": native_out},
        {**names, wallet: "this wallet"},
    )
    summary = plan["summary"]
    fees = {summary["token0"]: summary["fees0"], summary["token1"]: summary["fees1"]}
    base, quote = position["base_token"], position["quote_token"]
    return _quote_plan(
        runtime,
        plan,
        details=(
            f"Collects the fees position #{position_id} ({names[base]}/{names[quote]}) has earned: "
            f"{_amount(fees[base])} {names[base]} and {_amount(fees[quote])} {names[quote]}. The "
            f"position itself stays as it is"
        ),
    )
