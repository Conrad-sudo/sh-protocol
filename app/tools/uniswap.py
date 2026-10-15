"""
Swaps on the chain's exchange -- Uniswap V3 on Ethereum, Sepolia, Arbitrum and Base, PancakeSwap V2
on BSC -- with their previews, and PancakeSwap V2's liquidity tools. Calldata comes from the in-repo
langchain_uniswap_v3 or from langchain-uniswap-v2 (toolkits.get_uniswap_tools), which name their
swap and quote tools alike, with the same arguments and summary fields, so one set of tools here
serves both. Uniswap V3's liquidity is positions, not LP tokens: tools/positions.py.
"""
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import ToolException

from agent_context import AgentContext
from constants import get_chain_display_name, get_exchange
from contracts import (
    load_session_handler,
)
from network_config import load_network_config
from toolkits import get_uniswap_tools
from tools.common import (
    BPS,
    DEFAULT_SLIPPAGE_BPS,
    RECEIVED,
    SENT,
    _check_slippage,
    _destination_note,
    _is_native,
    _keeps_output,
    _limit_note,
    _refuse_same_token,
    _resolve,
    _resolve_recipient,
    _token_label,
    _with_note,
)
from tools.quoting import _quote_plan

# How each exchange's liquidity is reached, for the refusal a liquidity tool gives on a chain that
# trades on the other. The agent normally sees only its chain's set (smart_wallet_agent's
# per-chain filter), so this is the backstop, and it points the model at the tools that do apply.
_EXCHANGE_NAMES = {"uniswap-v3": "Uniswap V3", "pancakeswap-v2": "PancakeSwap V2"}
_LIQUIDITY_HINTS = {
    "uniswap-v3": (
        "There, liquidity is a position with a price range: call suggest_price_ranges and then "
        "open_position, and get_positions, add_to_position, withdraw_from_position and "
        "collect_fees for the positions the wallet holds. The assistant can't take out Uniswap V2 "
        "LP tokens the wallet still holds there; the owner can move them to their own wallet from "
        "the web app (Dashboard -> Withdraw)."
    ),
    "pancakeswap-v2": (
        "There, liquidity is a pool's LP tokens, with no price range: use add_liquidity and "
        "remove_liquidity."
    ),
}


def _require_exchange(user_id: int, exchange: str) -> int:
    """
    Refuses a liquidity tool on a chain whose exchange isn't `exchange`, saying what to use instead.

    The two exchanges' liquidity tools build calldata for different contracts, and only one of them
    is on any chain: a V2 deposit on a V3 chain would target a router the wallet no longer trusts and
    pools that are nearly empty, and a V3 position on BSC has no position manager to go to.

    @param exchange  The exchange the tool is for: "uniswap-v3" or "pancakeswap-v2".
    @return          The user's chain id, when it trades on `exchange`.
    @raises ToolException  On any other chain.
    """
    _, chain_id, _ = load_network_config(user_id)
    actual = get_exchange(chain_id)
    if actual == exchange:
        return chain_id
    network = get_chain_display_name(chain_id)
    if actual is None:
        raise ToolException(
            f"Mitfah has no exchange on {network}, so the assistant can't provide liquidity there."
        )
    raise ToolException(
        f"{network} trades on {_EXCHANGE_NAMES[actual]}, so this tool doesn't apply there. "
        f"{_LIQUIDITY_HINTS[actual]}"
    )


def _quote_exact_input_swap(
    runtime, plan: dict, *, sold: str, sold_name: str, amount_in: float, bought: str,
    bought_name: str, slippage_bps: int, recipient: str | None,
) -> dict:
    """
    Quotes a swap of an exact input: what the router expects to come back, the least the swap will
    accept, and what the spending limit will count.

    The quote carries the router's figures itself, so the agent needs no get_quote_out first. The
    package reports only the minimum, which it derived from the router's expected output as
    expected * (1 - slippage); the expected figure is recovered from it -- exact to within a unit
    in the last decimal place -- rather than asking the router a second time.

    @param sold, bought  Tool token arguments, "eth" for the native asset.
    @param sold_name, bought_name  How to name each in the quote (a ticker, or "ETH"/"BNB").
    """
    minimum = plan["summary"]["amount_out_min"]
    expected = minimum * BPS / (BPS - slippage_bps)
    legs = [(sold, amount_in, SENT)]
    if _keeps_output(recipient):
        legs.append((bought, expected, RECEIVED))
    return _quote_plan(
        runtime,
        plan,
        details=_with_note(
            f"{sold_name} spent: {amount_in}, {bought_name} received: about {expected:.6f}, at least "
            f"{minimum:.6f} (slippage tolerance {slippage_bps / 100:g}%){_destination_note(recipient)}",
            _limit_note(runtime.context.user_id, sold, bought),
        ),
        legs=legs,
    )


def _quote_exact_output_swap(
    runtime, plan: dict, *, sold: str, sold_name: str, bought: str, bought_name: str,
    amount_out: float, slippage_bps: int, recipient: str | None,
) -> dict:
    """
    Quotes a swap for an exact output: what the router expects it to cost, the most the swap will
    pay, and what the spending limit will count. The exact-output twin of _quote_exact_input_swap:
    the package derived its maximum as expected * (1 + slippage), and the expected input is
    recovered from that.
    """
    maximum = plan["summary"]["amount_in_max"]
    expected = maximum * BPS / (BPS + slippage_bps)
    legs = [(sold, expected, SENT)]
    if _keeps_output(recipient):
        legs.append((bought, amount_out, RECEIVED))
    return _quote_plan(
        runtime,
        plan,
        details=_with_note(
            f"{bought_name} received: {amount_out}, {sold_name} spent: about {expected:.6f}, at most "
            f"{maximum:.6f} (slippage tolerance {slippage_bps / 100:g}%){_destination_note(recipient)}",
            _limit_note(runtime.context.user_id, sold, bought),
        ),
        legs=legs,
    )


"""
 /*//////////////////////////////////////////////////////////////
                         EXCHANGE TOOLS
//////////////////////////////////////////////////////////////*/
"""
@tool
def get_quote_in(runtime: ToolRuntime[AgentContext], token_in: str, token_out: str, amount_out: float) -> dict:
    """
    Returns how much of token_in is required to receive an exact amount of token_out on the
    chain's exchange (Uniswap V3; PancakeSwap V2 on BSC). It prices the direct pool -- on Uniswap
    V3, at every fee tier -- and the route through the chain's wrapped-native token (WETH, or WBNB
    on BSC), and returns the cheaper.

    Use this tool to answer a question about the cost of acquiring a specific amount of a token
    (e.g. "How much USDC do I need to buy exactly 100 DAI?"). When the user wants the swap itself,
    call `swap` straight away instead: its quote already carries these figures.

    Args:
        token_in: The ticker of the token being spent (e.g. "usdc"), or "eth" for the native asset.
        token_out: The ticker of the token being received (e.g. "dai"), or "eth" for the native asset.
        amount_out: The exact amount of token_out to receive, in whole units (e.g. 100 for 100 DAI).

    Returns:
        A dict with:
          - amount_in (float): required token_in in whole units (e.g. 101.5 for 101.5 USDC)
          - amount_out (float): the requested token_out amount in whole units
          - path (list[str]): the token address path used for the quote
          - fees (list[int]): on Uniswap V3, each hop's pool fee tier

        When presenting to the user, show only amount_in and amount_out. Never expose path or fees.
    """
    user_id = runtime.context.user_id
    print("Running get_quote_in")
    return get_uniswap_tools(user_id)["get_quote_in"].invoke(
        {
            "token_in": _resolve(user_id, token_in),
            "token_out": _resolve(user_id, token_out),
            "amount_out": amount_out,
        }
    )


@tool
def get_quote_out(runtime: ToolRuntime[AgentContext], token_in: str, token_out: str, amount_in: float) -> dict:
    """
    Returns how much of token_out will be received when spending an exact amount of token_in on
    the chain's exchange (Uniswap V3; PancakeSwap V2 on BSC). It prices the direct pool -- on
    Uniswap V3, at every fee tier -- and the route through the chain's wrapped-native token (WETH,
    or WBNB on BSC), and returns the better.

    Use this tool to answer a question about how much they'd receive for a given spend
    (e.g. "How much DAI will I get for 100 USDC?"). When the user wants the swap itself, call
    `swap` straight away instead: its quote already carries these figures.

    Args:
        token_in: The ticker of the token being spent (e.g. "usdc"), or "eth" for the native asset.
        token_out: The ticker of the token being received (e.g. "dai"), or "eth" for the native asset.
        amount_in: The exact amount of token_in to spend, in whole units (e.g. 100 for 100 USDC).

    Returns:
        A dict with:
          - amount_in (float): the token_in amount in whole units
          - amount_out (float): expected token_out in whole units (e.g. 99.2 for 99.2 DAI)
          - path (list[str]): the token address path used for the quote
          - fees (list[int]): on Uniswap V3, each hop's pool fee tier

        When presenting to the user, show only amount_in and amount_out. Never expose path or fees.
    """
    user_id = runtime.context.user_id
    print("Running get_quote_out")
    return get_uniswap_tools(user_id)["get_quote_out"].invoke(
        {
            "token_in": _resolve(user_id, token_in),
            "token_out": _resolve(user_id, token_out),
            "amount_in": amount_in,
        }
    )


@tool
def get_liquidity_token_balance(
    runtime: ToolRuntime[AgentContext], token_a: str, token_b: str = "eth"
) -> float:
    """
    Retrieves the smart wallet's balance of PancakeSwap V2 liquidity tokens for a given pair (BSC).

    Use this tool when the user wants to check how much liquidity they have provided to a pool,
    or before removing liquidity when they haven't said how much.
    The tool identifies the correct pair based on the two token tickers and returns the wallet's balance
    of that pair's liquidity tokens in whole units (not base units).

    Args:
        token_a: The ticker symbol of the first token in the pair (e.g. "dai").
        token_b: The ticker symbol of the second token in the pair. Defaults to the native asset
                 (ETH, or BNB on BSC), whose pool is the one add_liquidity uses by default.

    Returns:
        The wallet's balance of liquidity tokens for the specified pair, in whole units (e.g. 10.5).
    """
    user_id = runtime.context.user_id
    print("Running get_liquidity_token_balance")
    _require_exchange(user_id, "pancakeswap-v2")
    # The native asset's pool is the wrapped-native one: _resolve maps "eth" there.
    return get_uniswap_tools(user_id)["get_liquidity_token_balance"].invoke(
        {
            "owner_address": load_session_handler(user_id).address,
            "token_a": _resolve(user_id, token_a),
            "token_b": _resolve(user_id, token_b),
        }
    )


@tool
def get_pool_quote(runtime: ToolRuntime[AgentContext], token_a: str, token_b: str, amount_a: float) -> dict:
    """
    Returns the proportional token_b amount required to match a given token_a deposit in a
    PancakeSwap V2 pool (BSC), using live pool reserves and router.quote().

    Use this tool when the user wants to preview how much of the second token they need to
    provide before adding liquidity (e.g. "How much ETH do I need to pair with 2500 DAI?").
    add_liquidity derives this amount itself, so this tool is for previewing only.

    Args:
        token_a: The ticker of the first token (e.g. "dai").
        token_b: The ticker of the second token, or "eth" for the native asset (ETH, or BNB on
                 BSC).
        amount_a: The amount of token_a to deposit, in whole units (e.g. 2500 for 2500 DAI).

    Returns:
        A dict with:
          - amount_a (float): token_a deposit in whole units
          - amount_b_desired (float): required token_b in whole units
    """
    user_id = runtime.context.user_id
    print("Running get_pool_quote")
    _require_exchange(user_id, "pancakeswap-v2")
    return get_uniswap_tools(user_id)["get_pool_quote"].invoke(
        {
            "token_a": _resolve(user_id, token_a),
            "token_b": _resolve(user_id, token_b),
            "amount_a": amount_a,
        }
    )


@tool
def get_lp_amounts(runtime: ToolRuntime[AgentContext], token_a: str, token_b: str, lp_amount: float) -> dict:
    """
    Returns the expected token amounts redeemable by burning a given amount of PancakeSwap V2 LP
    tokens (BSC), derived from live reserves using the proportional share formula
    (liquidity × reserve / totalSupply).

    Use this tool when the user wants to preview how much they'll receive before removing
    liquidity (e.g. "How much DAI and ETH will I get back for 0.5 LP tokens?").
    remove_liquidity derives these amounts itself, so this tool is for previewing only.

    Args:
        token_a: The ticker of the first token in the pair (e.g. "dai").
        token_b: The ticker of the second token in the pair, or "eth" for the native asset (ETH,
                 or BNB on BSC).
        lp_amount: The amount of LP tokens to burn, in whole units (e.g. 0.5).

    Returns:
        A dict with:
          - expected_a (float): expected token_a return in whole units
          - expected_b (float): expected token_b return in whole units
    """
    user_id = runtime.context.user_id
    print("Running get_lp_amounts")
    _require_exchange(user_id, "pancakeswap-v2")
    return get_uniswap_tools(user_id)["get_lp_amounts"].invoke(
        {
            "token_a": _resolve(user_id, token_a),
            "token_b": _resolve(user_id, token_b),
            "lp_amount": lp_amount,
        }
    )


@tool
def swap(
    runtime: ToolRuntime[AgentContext],
    token_in: str,
    token_out: str,
    amount_in: float | None = None,
    amount_out: float | None = None,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    recipient: str | None = None,
):
    """
    Swaps one token for another on the chain's exchange: Uniswap V3 on Ethereum, Sepolia,
    Arbitrum and Base, PancakeSwap V2 on BSC. On Uniswap V3 it takes the best of the pair's fee
    tiers, directly or through the wrapped native token.

    Give exactly ONE amount -- whichever the user fixed:
      - amount_in when they say what they SPEND ("swap 0.1 ETH for USDC"). The swap spends exactly
        that and receives what the pool gives, no less than the slippage allows.
      - amount_out when they say what they RECEIVE ("buy 100 USDC with ETH"). The swap receives
        exactly that and spends what it costs, no more than the slippage allows.

    Use "eth" (or "bnb") for the chain's native asset, on either side; use "weth"/"wbnb" only when
    the user means the wrapped token. The router approval a swap needs is granted and used up in
    the same transaction, so there is never a separate approval.

    Args:
        token_in: The token to spend: a ticker (e.g. "usdc"), or "eth" for the native asset.
        token_out: The token to receive: a ticker (e.g. "link"), or "eth" for the native asset.
        amount_in: How much token_in to spend, in whole units (e.g. 0.1). Leave unset when giving
                   amount_out.
        amount_out: How much token_out to receive, in whole units (e.g. 100). Leave unset when
                    giving amount_in.
        slippage_bps: How far the price may move against the user before the swap is refused, in
                      basis points: 50 (0.5%) by default, at most 1200 (12%). Use the figure the
                      user gave, if any.
        recipient: Optional. The name of a saved contact to receive token_out directly, when the
                   user asks to swap and send in one go (e.g. "swap 1 ETH for USDC and send it to
                   Sandy"). The swap itself delivers it — do NOT follow up with a transfer. Must
                   be a saved contact, added in the web app — you cannot add one here. Pass "me"
                   or omit it to keep the output in the wallet.

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running swap")
    if (amount_in is None) == (amount_out is None):
        raise ToolException(
            "Give exactly one amount: amount_in for what the user spends, or amount_out for what "
            "they receive. Nothing was quoted."
        )
    _refuse_same_token(user_id, token_in, token_out, "there is nothing to swap")
    native_in, native_out = _is_native(token_in), _is_native(token_out)
    exact_in = amount_in is not None

    # One of the router's six swap functions, picked by which side is the native asset and which
    # amount the user fixed. The native side names no token: the router wraps or unwraps it itself.
    if native_in:
        name = "swap_exact_eth_for_tokens" if exact_in else "swap_eth_for_exact_tokens"
        args = {"token_out": _resolve(user_id, token_out)}
    elif native_out:
        name = "swap_exact_tokens_for_eth" if exact_in else "swap_tokens_for_exact_eth"
        args = {"token_in": _resolve(user_id, token_in)}
    else:
        name = "swap_exact_tokens_for_tokens" if exact_in else "swap_tokens_for_exact_tokens"
        args = {"token_in": _resolve(user_id, token_in), "token_out": _resolve(user_id, token_out)}
    if exact_in:
        args["amount_in"] = amount_in
    else:
        args["amount_out"] = amount_out
    plan = get_uniswap_tools(user_id)[name].invoke(
        {
            **args,
            "from_address": load_session_handler(user_id).address,
            "recipient": _resolve_recipient(user_id, recipient),
            "slippage_bps": _check_slippage(slippage_bps),
        }
    )

    sides = dict(
        sold="eth" if native_in else token_in,
        sold_name=_token_label(user_id, token_in),
        bought="eth" if native_out else token_out,
        bought_name=_token_label(user_id, token_out),
        slippage_bps=slippage_bps,
        recipient=recipient,
    )
    if exact_in:
        return _quote_exact_input_swap(runtime, plan, amount_in=amount_in, **sides)
    return _quote_exact_output_swap(runtime, plan, amount_out=amount_out, **sides)


@tool
def add_liquidity(
    runtime: ToolRuntime[AgentContext],
    token_a: str,
    amount_a: float,
    token_b: str = "eth",
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Adds liquidity to a PancakeSwap V2 pool (BSC): deposits `amount_a` of token_a and the matching
    amount of token_b, worked out from the pool's current ratio.

    token_b defaults to the native asset (ETH, or BNB on BSC), which is deposited as it is — nothing
    needs wrapping first. Pass "weth"/"wbnb" only when the user wants to deposit the wrapped token,
    or another ticker for a pool without the native asset.

    The fixed amount is always token_a's, and token_a can't be the native asset. If the user names
    only an amount of the native asset ("add 0.5 ETH of liquidity with DAI"), ask how much of the
    other token they want to deposit instead; get_pool_quote shows how much ETH an amount of a token
    pairs with.

    The router approvals the deposit needs are granted and used up in the same transaction, so
    there is never a separate approval. Both deposits count toward the spending limit where it
    counts the token (the native asset always), because the LP tokens that come back have no price.
    Once sent, the LP tokens show on the user's dashboard in the web app.

    Args:
        token_a: The token whose amount is fixed (e.g. "dai"). Not the native asset.
        amount_a: How much token_a to deposit, in whole units (e.g. 2500 for 2500 DAI).
        token_b: The pool's other token. Defaults to "eth", the native asset.
        slippage_bps: How far the pool's ratio may move before the deposit is refused, in basis
                      points: 50 (0.5%) by default, at most 1200 (12%).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running add_liquidity")
    _require_exchange(user_id, "pancakeswap-v2")
    _refuse_same_token(user_id, token_a, token_b, "a pool needs two different tokens")
    if _is_native(token_a):
        # The package's add_liquidity_eth takes the token's amount and works out the native one
        # from the pool, never the other way round.
        native, other = _token_label(user_id, token_a), _token_label(user_id, token_b)
        raise ToolException(
            f"Name how much {other} to deposit first: the pool works out the {native} that goes with "
            f"it, so the {native} amount can't be the one that is fixed. get_pool_quote shows how "
            f"much {native} an amount of {other} pairs with. Nothing was quoted."
        )
    address_a, address_b = _resolve(user_id, token_a), _resolve(user_id, token_b)
    from_address = load_session_handler(user_id).address
    uniswap = get_uniswap_tools(user_id)
    if _is_native(token_b):
        plan = uniswap["add_liquidity_eth"].invoke(
            {
                "token": address_a,
                "amount_token": amount_a,
                "from_address": from_address,
                "slippage_bps": _check_slippage(slippage_bps),
            }
        )
        summary = plan["summary"]
        amount_b, a_min, b_min = (
            summary["amount_eth_desired"], summary["amount_token_min"], summary["amount_eth_min"]
        )
    else:
        plan = uniswap["add_liquidity"].invoke(
            {
                "token_a": address_a,
                "token_b": address_b,
                "amount_a": amount_a,
                "from_address": from_address,
                "slippage_bps": _check_slippage(slippage_bps),
            }
        )
        summary = plan["summary"]
        amount_b, a_min, b_min = summary["amount_b_desired"], summary["amount_a_min"], summary["amount_b_min"]

    # Both deposits leave the wallet and the LP token that comes back has no price, so the limit
    # counts both: the native asset always, a token while it is watched.
    return _quote_plan(
        runtime,
        plan,
        details=(
            f"{_token_label(user_id, token_a)} deposited: {amount_a} (at least {a_min:.6f}), "
            f"{_token_label(user_id, token_b)} deposited: about {amount_b:.6f} (at least {b_min:.6f}), "
            f"slippage tolerance {slippage_bps / 100:g}%"
        ),
        legs=[(token_a, amount_a, SENT), (token_b, amount_b, SENT)],
        lp_pool=_lp_pool(user_id, address_a, address_b),
    )


def _lp_pool(user_id: int, address_a: str, address_b: str) -> dict:
    """
    The pool a deposit goes into, for confirm_transaction to put on the dashboard (db.save_lp_token).

    Each token is named the way the user knows it, and the wrapped native token as the native asset
    itself (ETH, BNB): that is what goes in and comes back out by default, and a deposit of WETH
    lands in the same pool, so it gets the same name.

    @param address_a, address_b  The pool's tokens, as _resolve returned them.
    """
    wrapped = _resolve(user_id, "eth")

    def ticker(address: str) -> str:
        return _token_label(user_id, "eth" if address == wrapped else address).lower()

    return {"token_a": address_a, "ticker_a": ticker(address_a), "token_b": address_b, "ticker_b": ticker(address_b)}


@tool
def remove_liquidity(
    runtime: ToolRuntime[AgentContext],
    token_a: str,
    lp_amount: float,
    token_b: str = "eth",
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Removes liquidity from a PancakeSwap V2 pool (BSC): burns `lp_amount` of the pool's LP tokens,
    and both of the pool's tokens come back to the wallet in its current proportions.

    token_b defaults to the native asset (ETH, or BNB on BSC), which comes back as it is. Pass
    "weth"/"wbnb" only when the user wants the wrapped token back, or another ticker for a pool
    without the native asset. If the user hasn't said how much to remove,
    get_liquidity_token_balance shows how many LP tokens the wallet holds.

    It counts nothing toward the spending limit: value only comes back. The router approval for the
    LP tokens is granted and used up in the same transaction.

    Args:
        token_a: One of the pool's tokens (e.g. "dai").
        lp_amount: How many LP tokens to burn, in whole units (e.g. 0.5).
        token_b: The pool's other token. Defaults to "eth", the native asset.
        slippage_bps: How far below the expected amounts the returns may fall before the removal is
                      refused, in basis points: 50 (0.5%) by default, at most 1200 (12%).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running remove_liquidity")
    # On a Uniswap V3 chain this refuses, and says how the owner gets old V2 LP tokens out instead.
    _require_exchange(user_id, "pancakeswap-v2")
    _refuse_same_token(user_id, token_a, token_b, "there is no such pool")
    # A pool has no first token: "my ETH/DAI liquidity" and "my DAI/ETH liquidity" are the same.
    if _is_native(token_a):
        token_a, token_b = token_b, token_a
    from_address = load_session_handler(user_id).address
    uniswap = get_uniswap_tools(user_id)
    # The plan's approval is on the pair's own LP token, which the oracle does not price. It clears
    # SpendingLimitModule only because the router is a trusted spender (auto-trusted at wallet
    # deploy), and the removal pulls exactly the approved amount.
    if _is_native(token_b):
        plan = uniswap["remove_liquidity_eth"].invoke(
            {
                "token": _resolve(user_id, token_a),
                "lp_amount": lp_amount,
                "from_address": from_address,
                "slippage_bps": _check_slippage(slippage_bps),
            }
        )
        summary = plan["summary"]
        expected_a, expected_b = summary["expected_token"], summary["expected_eth"]
        a_min, b_min = summary["amount_token_min"], summary["amount_eth_min"]
    else:
        plan = uniswap["remove_liquidity"].invoke(
            {
                "token_a": _resolve(user_id, token_a),
                "token_b": _resolve(user_id, token_b),
                "lp_amount": lp_amount,
                "from_address": from_address,
                "slippage_bps": _check_slippage(slippage_bps),
            }
        )
        summary = plan["summary"]
        expected_a, expected_b = summary["expected_a"], summary["expected_b"]
        a_min, b_min = summary["amount_a_min"], summary["amount_b_min"]

    # Nothing it moves counts toward the limit, so no legs: only the pause and the key are checked.
    return _quote_plan(
        runtime,
        plan,
        details=(
            f"{_token_label(user_id, token_a)} returned: about {expected_a:.6f} (at least {a_min:.6f}), "
            f"{_token_label(user_id, token_b)} returned: about {expected_b:.6f} (at least {b_min:.6f}), "
            f"slippage tolerance {slippage_bps / 100:g}%"
        ),
    )
