"""
Standalone LangChain toolkit for Uniswap V3: swaps through SwapRouter02, liquidity as positions
(NFTs) through the NonfungiblePositionManager, and reads for quotes, price ranges and positions.
Like langchain-uniswap-v2, the write tools build an execution *plan* -- ordered, account-agnostic
calls -- for an EOA to sign or a smart-contract wallet to batch; nothing here holds a key, signs or
sends.

Example (known chain, just pass a chain_id):
    from langchain_uniswap_v3 import UniswapV3Toolkit

    toolkit = UniswapV3Toolkit.for_chain(chain_id=1, tx_mode="calls")
    tools = toolkit.get_tools()

Example (explicit addresses, any chain with Uniswap's V3 periphery):
    toolkit = UniswapV3Toolkit(
        rpc_url="https://eth.llamarpc.com",
        swap_router_address="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",    # SwapRouter02
        position_manager_address="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        quoter_address="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",         # QuoterV2
    )

What differs from V2, and why the code looks the way it does:

  - A pair has several pools, one per fee tier, so every swap and every deposit first finds which
    pools exist. They are looked up with factory.getPool and never computed: the init-code hash in
    the 0.8 branch of v3-periphery's PoolAddress is wrong for the live pools.
  - SwapRouter02's structs carry no deadline, so every router write goes through its
    multicall(deadline, data): that is where deadline_secs is enforced.
  - Liquidity is a position with a price range, not a share of a pool. Prices for people are
    "quote per base" in whole tokens (USDC per ETH); the pool's own price is token1 per token0 in
    base units, and v3math converts between them.
  - Native coin is never approved. It is sent as the call's value, and the router or position
    manager wraps it; whatever was not used is paid back by refundETH inside the same multicall.
"""

from __future__ import annotations

import contextvars
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Callable, Literal, Sequence

from langchain_core.tools import ToolException, tool
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import BadFunctionCallOutput, ContractLogicError, MismatchedABI

from . import v3math
from .abis import (
    erc20_abi,
    factory_abi,
    pool_abi,
    position_manager_abi,
    quoter_abi,
    swap_router_abi,
)
from .networks import KNOWN_NETWORKS

# Basis-point denominator for slippage math (e.g. 50 bps = 0.5%).
BPS_DENOMINATOR = 10_000
DEFAULT_SLIPPAGE_BPS = 50
DEFAULT_DEADLINE_SECS = 600
DEFAULT_FEE_TIERS = (100, 500, 3000, 10000)
ZERO_ADDRESS = "0x" + "00" * 20

# Static gas-limit fallback per call role, used when a call's gas can't be estimated yet (e.g. it
# depends on an earlier, not-yet-mined call in the same plan). Override per-instance via the
# constructor's default_gas param. A first mint into a fresh tick range is the expensive one: it
# initialises two ticks, mints the NFT and writes the position.
DEFAULT_GAS = {
    "approve": 70_000,
    "approve_reset": 50_000,
    "swap": 350_000,
    "add_liquidity": 650_000,
    "remove_liquidity": 450_000,
    "collect": 250_000,
}

# Range widths offered by suggest_price_ranges: +/- this many percent around today's price. A pair
# of stablecoins trades in a hair's breadth, so its bands are tiny; anything else moves.
_STABLE_WIDTHS = (("narrow", 0.5), ("medium", 1.0), ("wide", 3.0))
_OTHER_WIDTHS = (("narrow", 5.0), ("medium", 15.0), ("wide", 50.0))

# A price printed to six significant digits can sit up to 0.05 of a tick away from the tick it came
# from (a tick is 0.01% of price). Within 0.1 of a tick a price is taken to BE that tick, so the
# prices suggest_price_ranges returns go back into open_position -- even after the model has
# rounded them -- and land on the same ticks instead of one spacing wider. Costs at most 0.001% of
# range at either end.
_TICK_SNAP = 0.1
_LN_TICK_BASE = math.log(1.0001)

# The liquidity the range options are priced with. Any value works -- only the split between the
# two tokens is read from it -- but a big one keeps the integer amounts well clear of rounding.
_SHARE_LIQUIDITY = 10**24

# A pool that did not exist a moment ago may exist now, so "no pool at this tier" is only
# remembered this long. A pool that exists never goes away, so that is remembered for good.
_MISSING_POOL_TTL_SECS = 60.0

# How many of an address's positions get_positions looks through when it is asked for only the
# positions of certain tokens. Anyone can mint a position to any address, so without the filter
# someone could hide an address's real positions behind `limit` junk ones; with it, the junk is
# skipped on two cheap reads each (its id and its position) and only real positions cost the
# collect simulation and the pool reads. It still bounds what a flood of junk can make one read cost.
_MAX_POSITIONS_SCANNED = 200

# Independent chain reads run together on this pool, so a quote or a position list costs a few
# round trips to the RPC rather than one per read. Every task is a single leaf eth_call that never
# waits on anything here, so the pool cannot deadlock however many callers share it. Every toolkit
# in the process shares it, so it is sized for many quotes at once (a quote asks each fee tier).
_READS = ThreadPoolExecutor(max_workers=64, thread_name_prefix="v3-read")


def _run(read: Callable[[], object]) -> object:
    try:
        return read()
    except Exception as err:  # noqa: BLE001 -- handed back to the caller to judge, see _gather
        return err


def _gather(reads: Sequence[Callable[[], object]]) -> list:
    """
    Runs independent reads at once and returns, in order, each one's result -- or the exception it
    raised, as a value, so the caller can skip a candidate that reverted and still fail on a
    broken connection.
    """
    if len(reads) == 1:
        return [_run(reads[0])]
    # One context copy per task: a Context can only be entered by one thread at a time.
    futures = [_READS.submit(contextvars.copy_context().run, _run, read) for read in reads]
    return [future.result() for future in futures]


def _gather_strict(reads: Sequence[Callable[[], object]]) -> list:
    """_gather, raising the first failing read's own exception (in order), unchanged."""
    results = _gather(reads)
    for result in results:
        if isinstance(result, Exception):
            raise result
    return results


# ---- pure helpers: addresses, amounts, paths ----


def _address(value: str) -> str:
    try:
        return Web3.to_checksum_address(value)
    except (ValueError, TypeError) as err:
        raise ToolException(f"{value!r} is not a valid address.") from err


def _token_order(a: str, b: str) -> tuple[str, str]:
    """(token0, token1): a pool's tokens are the pair sorted by address, as the factory sorts them."""
    return (a, b) if int(a, 16) < int(b, 16) else (b, a)


def _pool_key(a: str, b: str, fee: int) -> tuple[str, str, int]:
    token0, token1 = _token_order(a, b)
    return token0, token1, fee


def encode_path(tokens: Sequence[str], fees: Sequence[int]) -> bytes:
    """A swap path as the router and quoter read it: token (20 bytes), fee (3), token (20), ..."""
    path = bytes.fromhex(tokens[0][2:])
    for fee, token in zip(fees, tokens[1:]):
        path += int(fee).to_bytes(3, "big") + bytes.fromhex(token[2:])
    return path


def _amount_str(base_units: int, decimals: int) -> str:
    """Whole units as exact decimal text with no exponent and no trailing zeros (0.01, 2980.5)."""
    whole, fraction = divmod(base_units, 10**decimals)
    digits = str(fraction).rjust(decimals, "0").rstrip("0") if decimals else ""
    return f"{whole}.{digits}" if digits else str(whole)


def _price_str(price: float) -> str:
    """A price to six significant digits as plain decimal text (no exponent)."""
    if price == 0 or not math.isfinite(price):
        return str(price) if price else "0"
    exact = Decimal(repr(price))
    rounded = exact.quantize(Decimal(1).scaleb(exact.adjusted() - 5), rounding=ROUND_HALF_EVEN)
    text = format(rounded, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fee_pct(fee: int) -> float:
    """A pool's fee in percent: the fee is in hundredths of a basis point, so 500 is 0.05%."""
    return fee / 10_000


def _fee_str(fee: int) -> str:
    return f"{_fee_pct(fee):g}%"


# ---- pure helpers: prices, ticks and deposits ----


def orient(token0: str, token1: str, native_wrapped: str, stable_tokens) -> tuple[str, str]:
    """
    Which of a pool's tokens is the base and which the quote, so a price reads "quote per base":

      1. if exactly one token is a stablecoin, it is the quote   (ETH/USDC: USDC per ETH);
      2. otherwise, if exactly one is the wrapped native token, it is the quote   (LINK/WETH);
      3. otherwise the base is token0 and the quote is token1.

    @return  (base, quote).
    """
    stable0, stable1 = token0 in stable_tokens, token1 in stable_tokens
    if stable0 != stable1:
        return (token1, token0) if stable0 else (token0, token1)
    native0, native1 = token0 == native_wrapped, token1 == native_wrapped
    if native0 != native1:
        return (token1, token0) if native0 else (token0, token1)
    return token0, token1


def _tick_float(raw_price: float) -> float:
    if raw_price <= 0:
        return float(v3math.MIN_TICK)
    if math.isinf(raw_price):
        return float(v3math.MAX_TICK)
    return math.log(raw_price) / _LN_TICK_BASE


def ticks_for_prices(
    min_price: float,
    max_price: float,
    base_is_token0: bool,
    decimals0: int,
    decimals1: int,
    tick_spacing: int,
) -> tuple[int, int]:
    """
    The tick range that covers min_price..max_price (quote per base, whole tokens): the lower tick
    rounded down and the upper rounded up to the pool's tick spacing, so the range is never
    narrower than asked, clamped to the ticks a pool can hold. When the base is token1 the prices
    run the other way round the pool's own price, so the lower price is the upper tick.

    @return  (tick_lower, tick_upper), tick_lower < tick_upper.
    """
    ticks = sorted(
        _tick_float(v3math.raw_from_human(price, base_is_token0, decimals0, decimals1))
        for price in (min_price, max_price)
    )

    def snapped(tick: float) -> float:
        nearest = round(tick)
        return nearest if abs(tick - nearest) < _TICK_SNAP else tick

    lowest, highest = v3math.full_range_ticks(tick_spacing)
    lower = max(lowest, v3math.floor_tick(math.floor(snapped(ticks[0])), tick_spacing))
    upper = min(highest, v3math.ceil_tick(math.ceil(snapped(ticks[1])), tick_spacing))
    if lower >= upper:  # the two prices share one tick spacing's width: widen by one
        if lower + tick_spacing <= highest:
            upper = lower + tick_spacing
        else:
            lower = upper - tick_spacing
    return lower, upper


def prices_for_ticks(
    tick_lower: int,
    tick_upper: int,
    base_is_token0: bool,
    decimals0: int,
    decimals1: int,
    tick_spacing: int,
) -> tuple[float, float | None, bool]:
    """
    The (min_price, max_price, full_range) a tick range stands for, in quote per base. The widest
    range the spacing allows is "full range": it starts at 0 and has no top, so max_price is None.
    """
    lowest, highest = v3math.full_range_ticks(tick_spacing)
    if tick_lower <= lowest and tick_upper >= highest:
        return 0.0, None, True
    prices = [
        v3math.human_price(v3math.raw_price_at_tick(tick), base_is_token0, decimals0, decimals1)
        for tick in (tick_lower, tick_upper)
    ]
    return min(prices), max(prices), False


def deposit_amounts(
    sqrt_price: int,
    tick_lower: int,
    tick_upper: int,
    a_is_token0: bool,
    amount_a: int,
    *,
    token_a: str,
    token_b: str,
) -> tuple[int, int, int]:
    """
    The liquidity amount_a buys in this range at today's price, and what the position manager
    will pull for it: (liquidity, amount0, amount1) in base units, the amounts rounded up as the
    pool rounds them. amount_a alone fixes the liquidity; the other token's amount follows.

    @raises ToolException  If the range, at today's price, holds only the other token -- the
                           deposit can't be made with token_a, and the message says which token to
                           deposit instead -- or if amount_a is too small to buy any liquidity.
    """
    sqrt_lower = v3math.get_sqrt_ratio_at_tick(tick_lower)
    sqrt_upper = v3math.get_sqrt_ratio_at_tick(tick_upper)
    # A range wholly above the price holds only token0 (it is all still to be sold as the price
    # rises into it); one wholly below holds only token1.
    if sqrt_price <= sqrt_lower and not a_is_token0:
        side = "below"
    elif sqrt_price >= sqrt_upper and a_is_token0:
        side = "above"
    else:
        side = None
    if side is not None:
        raise ToolException(
            f"At today's price this range holds only {token_b}: the current price is {side} the "
            f"range, so a deposit of {token_a} can't be used. Deposit {token_b} instead (give it "
            f"as token_a), or choose a range that contains the current price."
        )

    if sqrt_price <= sqrt_lower:  # only token0 is needed, and token_a is it
        liquidity = v3math.liquidity_for_amount0(sqrt_lower, sqrt_upper, amount_a)
    elif sqrt_price >= sqrt_upper:  # only token1 is needed, and token_a is it
        liquidity = v3math.liquidity_for_amount1(sqrt_lower, sqrt_upper, amount_a)
    elif a_is_token0:
        liquidity = v3math.liquidity_for_amount0(sqrt_price, sqrt_upper, amount_a)
    else:
        liquidity = v3math.liquidity_for_amount1(sqrt_lower, sqrt_price, amount_a)
    if liquidity <= 0:
        raise ToolException(f"{token_a} amount is too small to buy any liquidity in this range.")
    amount0, amount1 = v3math.mint_amounts(sqrt_price, sqrt_lower, sqrt_upper, liquidity)
    return liquidity, amount0, amount1


def mint_outcome(
    sqrt_price: int,
    tick_lower: int,
    tick_upper: int,
    amount0_desired: int,
    amount1_desired: int,
) -> tuple[int, int, int]:
    """
    What the position manager makes of the amounts it is offered: (liquidity, pulled0, pulled1).
    It works the liquidity out itself, on chain -- the smaller of what each token alone would buy
    (LiquidityAmounts.getLiquidityForAmounts) -- and the pool then pulls that liquidity's amounts,
    rounded up.

    This is not always the liquidity deposit_amounts started from. The position manager floors a
    product in the middle of its sum (the token0 side, sqrtP * sqrtB / 2**96), so when token_a is
    the pool's token1 and the token0 amount is derived from it, it can land a hair lower -- a few
    parts in 10**17 or less, which is thousands of wei at 18 decimals. The minimums have to come
    from this outcome, or a slippage of zero would revert for want of those wei (seen against the
    live position manager on a stablecoin pool).
    """
    sqrt_lower = v3math.get_sqrt_ratio_at_tick(tick_lower)
    sqrt_upper = v3math.get_sqrt_ratio_at_tick(tick_upper)
    liquidity = v3math.liquidity_for_amounts(
        sqrt_price, sqrt_lower, sqrt_upper, amount0_desired, amount1_desired
    )
    pulled0, pulled1 = v3math.mint_amounts(sqrt_price, sqrt_lower, sqrt_upper, liquidity)
    return liquidity, pulled0, pulled1


def range_options(
    sqrt_price: int,
    tick_spacing: int,
    base_is_token0: bool,
    decimals0: int,
    decimals1: int,
    stable_pair: bool,
) -> list[dict]:
    """
    The price ranges to offer for a pool at its current price: three bands around it (narrow,
    medium, wide -- tiny ones for a stablecoin pair) and the full range. Each band's ends are the
    prices of the ticks it actually rounds to, and base_share_pct / quote_share_pct say how a
    deposit would split between the two tokens by value at today's price.
    """
    current = v3math.human_price(
        v3math.raw_price_from_sqrt(sqrt_price), base_is_token0, decimals0, decimals1
    )
    options = []
    for name, width in (*(_STABLE_WIDTHS if stable_pair else _OTHER_WIDTHS), ("full", None)):
        if width is None:
            tick_lower, tick_upper = v3math.full_range_ticks(tick_spacing)
        else:
            tick_lower, tick_upper = ticks_for_prices(
                current * (1 - width / 100),
                current * (1 + width / 100),
                base_is_token0,
                decimals0,
                decimals1,
                tick_spacing,
            )
        min_price, max_price, full = prices_for_ticks(
            tick_lower, tick_upper, base_is_token0, decimals0, decimals1, tick_spacing
        )
        amount0, amount1 = v3math.amounts_for_liquidity(
            sqrt_price,
            v3math.get_sqrt_ratio_at_tick(tick_lower),
            v3math.get_sqrt_ratio_at_tick(tick_upper),
            _SHARE_LIQUIDITY,
        )
        whole0, whole1 = amount0 / 10**decimals0, amount1 / 10**decimals1
        base_whole, quote_whole = (whole0, whole1) if base_is_token0 else (whole1, whole0)
        base_value = base_whole * current
        total = base_value + quote_whole
        base_share = round(100 * base_value / total, 1) if total else 0.0
        options.append(
            {
                "name": name,
                "width_pct": width,
                "full_range": full,
                "min_price": min_price,
                "max_price": max_price,
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "base_share_pct": base_share,
                "quote_share_pct": round(100 - base_share, 1),
            }
        )
    return options


@dataclass(frozen=True)
class _Pool:
    """One pool's state at the moment it was read."""

    address: str
    token0: str
    token1: str
    fee: int
    sqrt_price: int
    tick: int
    spacing: int
    liquidity: int  # the active liquidity at the current tick; 0 when it was not read


@dataclass(frozen=True)
class _Route:
    """A swap route: the tokens in swap order, the fee of each hop, and the quote that chose it."""

    path: list[str]
    fees: list[int]
    amount: int  # the quoted amount out for an exact-input route, the amount in for exact-output


@dataclass(frozen=True)
class _Deposit:
    """What a mint or increase will ask for: desired amounts, their minimums, and the native part."""

    liquidity: int
    amount0: int
    amount1: int
    min0: int
    min1: int
    native_side: int | None  # 0 or 1: the token that is paid as native coin, if any
    native_value: int


class UniswapV3Toolkit:
    """
    Bind once to an RPC endpoint, SwapRouter02, the position manager and QuoterV2, then call
    get_tools() to get LangChain tools scoped to that deployment. All tool arguments are plain
    contract addresses and amounts -- no ticker registry or wallet state is required.

    get_tools() returns two kinds of tools:

    - Read-only tools that only make RPC calls: get_quote_in and get_quote_out (the best route
      across the pair's fee tiers, directly or through the wrapped native token),
      suggest_price_ranges (the ranges worth offering for a pair), and get_positions /
      get_position (an owner's positions, with current amounts, unclaimed fees and whether each
      is in range).
    - Write tools (the six swap_* tools, open_position, add_to_position,
      withdraw_from_position and collect_fees) that build and return an execution *plan* -- an
      ordered list of account-agnostic calls (any required approval, then the action, then an
      approval reset where needed), plus the same calls rendered as unsigned EOA transactions
      when tx_mode="eoa" (the default). No write tool ever holds a private key, signs anything,
      or submits anything to the network.

    Every write tool includes its own approval call(s) in the plan, sized to what that call
    actually needs. tx_mode="calls" returns plans with calls only and makes zero nonce/gas/fee
    RPC calls, for a smart-contract wallet's own batch executor. All amount-based write tools take
    slippage_bps (basis points, default 50) and derive their minimums or maximums from a live
    on-chain quote or pool price, and deadline_secs (default 600) for the plan's on-chain expiry
    -- both are always explicit tool arguments, never silently omitted.
    """

    def __init__(
        self,
        rpc_url: str,
        swap_router_address: str,
        position_manager_address: str,
        quoter_address: str,
        native_wrapped_address: str | None = None,
        *,
        tx_mode: Literal["eoa", "calls"] = "eoa",
        estimate_gas: bool = True,
        gas_buffer: float = 1.25,
        default_gas: dict[str, int] | None = None,
        reset_residual_approvals: bool | None = None,
        preflight: bool = True,
        fee_tiers: Sequence[int] = DEFAULT_FEE_TIERS,
        stable_tokens: Sequence[str] = (),
    ):
        """
        Raises:
            ConnectionError: If the RPC endpoint does not answer. (The URL is left out of the
                message: it usually carries an API key.)
            ValueError: If the router, position manager and quoter are not one deployment -- they
                must report the same factory() and the same WETH9() -- or if native_wrapped_address
                is given and is not that WETH9. It refuses to start rather than build calldata for
                the wrong contract.
        """
        if tx_mode not in ("eoa", "calls"):
            raise ValueError(f"tx_mode must be 'eoa' or 'calls', not {tx_mode!r}")
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        # No CCIP-Read: a token reverting with OffchainLookup would otherwise have web3 fetch any URL
        # it names, from the server (see network_config._ChainIdOnceProvider).
        self.w3.provider.global_ccip_read_enabled = False
        if not self.w3.is_connected():
            raise ConnectionError("Could not connect to the RPC endpoint.")

        self.router: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(swap_router_address), abi=swap_router_abi
        )
        self.position_manager: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(position_manager_address), abi=position_manager_abi
        )
        self.quoter: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(quoter_address), abi=quoter_abi
        )

        # The deployment's own account of itself, all at once: the three contracts must agree on
        # the factory (so pools, quotes and positions are one set) and on the wrapped native token
        # (so native-coin swaps and deposits wrap what the pools hold).
        contracts = (self.router, self.position_manager, self.quoter)
        try:
            read = _gather_strict(
                [c.functions.factory().call for c in contracts]
                + [c.functions.WETH9().call for c in contracts]
                + [lambda: self.w3.eth.chain_id]
            )
        except (ContractLogicError, BadFunctionCallOutput) as err:
            raise ValueError(
                "The router, position manager or quoter did not answer factory() / WETH9(): "
                "check each address is the right Uniswap V3 contract for this chain."
            ) from err
        factories = [Web3.to_checksum_address(a) for a in read[:3]]
        wrapped = [Web3.to_checksum_address(a) for a in read[3:6]]
        if len(set(factories)) != 1:
            raise ValueError(
                "The router, position manager and quoter report different V3 factories "
                f"(router {factories[0]}, position manager {factories[1]}, quoter {factories[2]}): "
                "they are not one Uniswap V3 deployment."
            )
        if len(set(wrapped)) != 1:
            raise ValueError(
                "The router, position manager and quoter report different WETH9 addresses "
                f"(router {wrapped[0]}, position manager {wrapped[1]}, quoter {wrapped[2]})."
            )
        if (
            native_wrapped_address is not None
            and Web3.to_checksum_address(native_wrapped_address) != wrapped[0]
        ):
            raise ValueError(
                f"native_wrapped_address {Web3.to_checksum_address(native_wrapped_address)} is "
                f"not the router's WETH9() {wrapped[0]}; the router wraps and unwraps only its "
                "own."
            )
        self.factory: Contract = self.w3.eth.contract(address=factories[0], abi=factory_abi)
        # Always the router's own WETH9: the native-coin tools swap and unwrap through it, and the
        # routes that hop between two tokens hop through it.
        self.native_wrapped_address: str = wrapped[0]
        self._chain_id: int = int(read[6])

        # "eoa": write tools also render signable transactions (nonce, gas, fees). "calls": write
        # tools return only the account-agnostic calls list -- zero nonce/gas/fee RPC calls -- for
        # a smart-contract wallet's own batch executor.
        self.tx_mode = tx_mode
        self.estimate_gas = estimate_gas
        self.gas_buffer = gas_buffer
        self.default_gas = {**DEFAULT_GAS, **(default_gas or {})}

        # Whether an approve-then-act call sequence appends a trailing approve(spender, 0) after
        # actions that may pull less than they were approved for (exact-output swaps, and every
        # mint or increase). An EOA pays gas for that extra transaction, so it defaults to off; a
        # batching account pays nothing extra for it (and some enforce it), so it defaults to on
        # -- unless the caller states a preference explicitly.
        self.reset_residual_approvals = (
            reset_residual_approvals
            if reset_residual_approvals is not None
            else (tx_mode == "calls")
        )
        # Whether write tools check, before building, that the sender holds what the plan pulls,
        # raising ToolException if not -- the safety net eth_estimateGas used to provide by
        # accident before plans could be built ahead of a standing allowance.
        self.preflight = preflight

        self.fee_tiers = tuple(int(fee) for fee in fee_tiers)
        if not self.fee_tiers:
            raise ValueError("fee_tiers must name at least one fee tier")
        # Stablecoins decide which side of a pair is the quote (see orient).
        self.stable_tokens = frozenset(Web3.to_checksum_address(a) for a in stable_tokens)

        self._erc20_cache: dict[str, Contract] = {}
        self._pool_contract_cache: dict[str, Contract] = {}
        self._decimals_cache: dict[str, int] = {}
        self._spacing_cache: dict[str, int] = {}
        # (token0, token1, fee) -> (pool address or None, when it was looked up)
        self._pools: dict[tuple[str, str, int], tuple[str | None, float]] = {}

    @classmethod
    def for_chain(
        cls, chain_id: int, rpc_url: str | None = None, **overrides
    ) -> UniswapV3Toolkit:
        """
        Convenience constructor for chains in KNOWN_NETWORKS -- looks up the router, position
        manager, quoter and wrapped-native addresses for chain_id so callers only need to supply a
        chain_id.

        Args:
            chain_id: The EVM chain ID (1 Ethereum, 11155111 Sepolia, 42161 Arbitrum, 8453 Base).
            rpc_url: Optional RPC endpoint override. If omitted, falls back to KNOWN_NETWORKS'
                public endpoint for that chain -- fine for prototyping, but rate-limited; pass
                your own for production.
            **overrides: Passed through to the main constructor (e.g. tx_mode, estimate_gas,
                reset_residual_approvals, preflight, fee_tiers, stable_tokens).
                swap_router_address, position_manager_address, quoter_address and
                native_wrapped_address may be passed here too, each overriding the registry's
                value for this chain -- the escape hatch for a stale entry, or a fork reusing a
                known chain id.

        Raises:
            ValueError: If chain_id has no entry in KNOWN_NETWORKS, or if neither rpc_url nor
                KNOWN_NETWORKS has one for this chain. Use the main constructor with explicit
                addresses for any other chain.
        """
        network = KNOWN_NETWORKS.get(chain_id)
        if network is None:
            raise ValueError(
                f"No known Uniswap V3 deployment registered for chain_id {chain_id}. Known "
                f"chain_ids: {sorted(KNOWN_NETWORKS)}. For any other chain, instantiate "
                f"UniswapV3Toolkit(...) directly with explicit addresses."
            )
        resolved_rpc_url = rpc_url or network["rpc_url"]
        if resolved_rpc_url is None:
            raise ValueError(
                f"No public rpc_url is known for chain_id {chain_id} ('{network['name']}'). "
                f"Pass rpc_url explicitly: UniswapV3Toolkit.for_chain({chain_id}, rpc_url=...)."
            )
        # setdefault, not explicit keywords: passing these positionally *and* splatting
        # **overrides raises TypeError the moment a caller overrides one, which would close the
        # only escape hatch for a stale registry entry.
        overrides.setdefault("swap_router_address", network["swap_router"])
        overrides.setdefault("position_manager_address", network["position_manager"])
        overrides.setdefault("quoter_address", network["quoter"])
        overrides.setdefault("native_wrapped_address", network["native_wrapped"])
        return cls(rpc_url=resolved_rpc_url, **overrides)

    # ---- chain reads: each one a single eth_call, so a caller can run several together ----

    def _erc20(self, address: str) -> Contract:
        address = Web3.to_checksum_address(address)
        if address not in self._erc20_cache:
            self._erc20_cache[address] = self.w3.eth.contract(address=address, abi=erc20_abi)
        return self._erc20_cache[address]

    def _decimals(self, address: str) -> int:
        address = Web3.to_checksum_address(address)
        if address not in self._decimals_cache:
            try:
                self._decimals_cache[address] = int(
                    self._erc20(address).functions.decimals().call()
                )
            except (ContractLogicError, BadFunctionCallOutput) as err:
                raise ToolException(
                    f"{address} does not look like an ERC-20 token: decimals() failed."
                ) from err
        return self._decimals_cache[address]

    def _decimals_of(self, *addresses: str) -> list[int]:
        """The decimals of each address, in order. The ones not read before are read together."""
        checked = [Web3.to_checksum_address(a) for a in addresses]
        missing = [a for a in dict.fromkeys(checked) if a not in self._decimals_cache]
        if missing:
            _gather_strict([lambda a=a: self._decimals(a) for a in missing])
        return [self._decimals_cache[a] for a in checked]

    @staticmethod
    def _to_base_units(amount: float, decimals: int) -> int:
        try:
            exact = Fraction(str(amount)) * 10**decimals  # exact: no float, no rounding context
        except (ValueError, ZeroDivisionError) as err:
            raise ToolException(f"{amount!r} is not a valid amount.") from err
        return int(exact)

    def _token_balance(self, token_address: str, owner_address: str) -> int:
        return (
            self._erc20(token_address)
            .functions.balanceOf(Web3.to_checksum_address(owner_address))
            .call()
        )

    def _native_balance(self, owner_address: str) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(owner_address))

    def _pool_contract(self, address: str) -> Contract:
        if address not in self._pool_contract_cache:
            self._pool_contract_cache[address] = self.w3.eth.contract(
                address=address, abi=pool_abi
            )
        return self._pool_contract_cache[address]

    def _read_pool(self, key: tuple[str, str, int]) -> str | None:
        """factory.getPool: the pool's address, or None when there is no pool."""
        address = self.factory.functions.getPool(*key).call()
        return None if int(address, 16) == 0 else Web3.to_checksum_address(address)

    def _read_slot0(self, pool: str) -> tuple[int, int]:
        slot0 = self._pool_contract(pool).functions.slot0().call()
        return int(slot0[0]), int(slot0[1])  # sqrtPriceX96, tick

    def _tick_spacing(self, pool: str) -> int:
        if pool not in self._spacing_cache:  # a pool's spacing never changes
            self._spacing_cache[pool] = int(self._pool_contract(pool).functions.tickSpacing().call())
        return self._spacing_cache[pool]

    def _read_liquidity(self, pool: str) -> int:
        return int(self._pool_contract(pool).functions.liquidity().call())

    def _read_owner_of(self, token_id: int) -> str:
        try:
            return Web3.to_checksum_address(
                self.position_manager.functions.ownerOf(token_id).call()
            )
        except ContractLogicError as err:
            raise ToolException(f"Position #{token_id} does not exist.") from err

    def _read_position(self, token_id: int) -> dict:
        try:
            raw = self.position_manager.functions.positions(token_id).call()
        except ContractLogicError as err:
            raise ToolException(f"Position #{token_id} does not exist.") from err
        return {
            "token0": Web3.to_checksum_address(raw[2]),
            "token1": Web3.to_checksum_address(raw[3]),
            "fee": int(raw[4]),
            "tick_lower": int(raw[5]),
            "tick_upper": int(raw[6]),
            "liquidity": int(raw[7]),
            "owed0": int(raw[10]),
            "owed1": int(raw[11]),
        }

    def _read_collectable(self, token_id: int, owner: str) -> tuple[int, int]:
        """
        What collect() would pay this position's owner right now: the fees it has earned, even the
        ones the pool has not yet credited to it, plus anything already owed. The standard trick:
        an eth_call of collect from the owner pays out nothing and changes nothing.
        """
        collected = self.position_manager.functions.collect(
            (token_id, owner, v3math.MAX_UINT128, v3math.MAX_UINT128)
        ).call({"from": owner})
        return int(collected[0]), int(collected[1])

    # ---- pools ----

    def _find_pools(
        self, triples: Sequence[tuple[str, str, int]]
    ) -> dict[tuple[str, str, int], str | None]:
        """
        Which pools exist among (token_a, token_b, fee) triples: {(token0, token1, fee): address,
        or None}. The ones not looked up recently are looked up together, in one round of getPool
        reads; an address found is remembered for good.
        """
        keys = [_pool_key(a, b, fee) for a, b, fee in triples]
        now = time.monotonic()
        todo = []
        for key in dict.fromkeys(keys):
            cached = self._pools.get(key)
            if cached is None or (cached[0] is None and now - cached[1] >= _MISSING_POOL_TTL_SECS):
                todo.append(key)
        if todo:
            addresses = _gather_strict([lambda key=key: self._read_pool(key) for key in todo])
            stamp = time.monotonic()
            for key, address in zip(todo, addresses):
                self._pools[key] = (address, stamp)
        return {key: self._pools[key][0] for key in keys}

    def _pool_states(
        self, pools: Sequence[str], *, with_liquidity: bool = False
    ) -> dict[str, tuple[int, int, int, int]]:
        """{pool: (sqrt_price, tick, tick_spacing, active_liquidity)}, all pools read together.
        Liquidity is 0 unless with_liquidity."""
        pools = list(dict.fromkeys(pools))
        reads = [lambda p=p: self._read_slot0(p) for p in pools]
        reads += [lambda p=p: self._tick_spacing(p) for p in pools]
        if with_liquidity:
            reads += [lambda p=p: self._read_liquidity(p) for p in pools]
        results = _gather_strict(reads)
        count = len(pools)
        return {
            pool: (
                results[i][0],
                results[i][1],
                results[count + i],
                results[2 * count + i] if with_liquidity else 0,
            )
            for i, pool in enumerate(pools)
        }

    def _select_pool(self, token_a: str, token_b: str, fee: int | None) -> _Pool:
        """
        The pool a deposit or a range suggestion should use: the one at `fee`, or -- the fee not
        given -- the tier whose pool has the most active liquidity, because that is where the fees
        are. Never creates a pool.

        @raises ToolException  If there is no pool, or it has no price yet (created, not initialised).
        """
        if fee is not None and not 0 < fee < 1_000_000:
            raise ToolException(f"fee {fee} is not a pool fee (e.g. 500 for 0.05%).")
        tiers = [fee] if fee is not None else list(self.fee_tiers)
        found = self._find_pools([(token_a, token_b, tier) for tier in tiers])
        existing = [
            (tier, found[_pool_key(token_a, token_b, tier)])
            for tier in tiers
            if found[_pool_key(token_a, token_b, tier)] is not None
        ]
        if not existing:
            where = f"the {_fee_str(fee)} fee tier" if fee is not None else (
                "any of the fee tiers " + ", ".join(_fee_str(t) for t in tiers)
            )
            raise ToolException(
                f"No Uniswap V3 pool exists for {token_a} / {token_b} at {where}. This tool "
                f"never creates a pool."
            )
        states = self._pool_states(
            [pool for _, pool in existing], with_liquidity=len(existing) > 1
        )
        token0, token1 = _token_order(token_a, token_b)
        pools = [
            _Pool(pool, token0, token1, tier, *states[pool][:2], states[pool][2], states[pool][3])
            for tier, pool in existing
        ]
        best = pools[0]
        for pool in pools[1:]:
            if pool.liquidity > best.liquidity:
                best = pool
        if best.sqrt_price == 0:
            raise ToolException(
                f"The {_fee_str(best.fee)} pool for {token_a} / {token_b} exists but has no price "
                f"yet (it was never initialised), so nothing can be deposited into it."
            )
        return best

    # ---- routing ----
    #
    # A route is a direct pool at one fee tier, or two pools through the wrapped native token. No
    # other hop tokens: that matches what V2 offered, and every extra hop is another fee and
    # another place the price can move before the swap lands. Pools that don't exist are dropped
    # after one round of getPool reads; the rest are quoted with QuoterV2 and the best fill wins.
    #
    # Exact input quotes the direct tiers and the first leg's tiers together, then -- with the best
    # first-leg tier fixed -- every second-leg tier along the whole path, so a hop costs two
    # rounds, not one per tier pair. Exact output is the mirror image, starting from the output
    # side. The winning quote comes back with its route, so callers never quote twice: a swap's
    # limit and its path are derived from one decision, and a second quote could straddle a price
    # move and cap the input using one route while executing another.

    def _quote_in_single(self, token_in: str, token_out: str, amount_in: int, fee: int) -> int:
        return self.quoter.functions.quoteExactInputSingle(
            (token_in, token_out, amount_in, fee, 0)
        ).call()[0]

    def _quote_in_path(self, path: bytes, amount_in: int) -> int:
        return self.quoter.functions.quoteExactInput(path, amount_in).call()[0]

    def _quote_out_single(self, token_in: str, token_out: str, amount_out: int, fee: int) -> int:
        return self.quoter.functions.quoteExactOutputSingle(
            (token_in, token_out, amount_out, fee, 0)
        ).call()[0]

    def _quote_out_path(self, reversed_path: bytes, amount_out: int) -> int:
        return self.quoter.functions.quoteExactOutput(reversed_path, amount_out).call()[0]

    def _hop_pairs(self, token_in: str, token_out: str) -> list[tuple[str, str]]:
        """The pairs whose pools a swap could use: the direct one, and -- unless one token already
        is the wrapped native token -- each leg of the hop through it."""
        wrapped = self.native_wrapped_address
        pairs = [(token_in, token_out)]
        if wrapped not in (token_in, token_out):
            pairs += [(token_in, wrapped), (wrapped, token_out)]
        return pairs

    def _existing_tiers(self, pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], list[int]]:
        """{pair: the fee tiers with a pool, in fee_tiers order}, one round of getPool reads."""
        found = self._find_pools([(a, b, fee) for a, b in pairs for fee in self.fee_tiers])
        return {
            (a, b): [fee for fee in self.fee_tiers if found[_pool_key(a, b, fee)] is not None]
            for a, b in pairs
        }

    @staticmethod
    def _best_quote(
        jobs: Sequence, outcomes: Sequence, kind: str, *, better: Callable[[int, int], bool]
    ) -> tuple[int, int] | None:
        """The (fee, amount) of the best successful quote among `kind` jobs, or None. A quote that
        reverted is skipped -- that pool cannot fill this swap; anything else is a real failure
        and is raised. On a tie the first tier wins."""
        top = None
        for (job_kind, fee), outcome in zip(jobs, outcomes):
            if job_kind != kind:
                continue
            if isinstance(outcome, ContractLogicError):
                continue
            if isinstance(outcome, Exception):
                raise outcome
            if outcome > 0 and (top is None or better(outcome, top[1])):
                top = (fee, outcome)
        return top

    def _route_out(
        self, amount_in: int, token_in: str, token_out: str, *, label_in: str, label_out: str
    ) -> _Route:
        """The route returning the most token_out for amount_in, with the quote that chose it.
        Raises if no route can fill it."""
        wrapped = self.native_wrapped_address
        pairs = self._hop_pairs(token_in, token_out)
        tiers = self._existing_tiers(pairs)
        direct = tiers[pairs[0]]
        leg1, leg2 = (tiers[pairs[1]], tiers[pairs[2]]) if len(pairs) == 3 else ([], [])

        # Round 1: every direct tier, and every first-leg tier (when a second leg exists at all).
        jobs = [("direct", fee) for fee in direct]
        if leg1 and leg2:
            jobs += [("leg1", fee) for fee in leg1]
        outcomes = _gather(
            [
                (lambda fee=fee: self._quote_in_single(token_in, token_out, amount_in, fee))
                if kind == "direct"
                else (lambda fee=fee: self._quote_in_single(token_in, wrapped, amount_in, fee))
                for kind, fee in jobs
            ]
        ) if jobs else []
        best_direct = self._best_quote(jobs, outcomes, "direct", better=lambda a, b: a > b)
        best_leg1 = self._best_quote(jobs, outcomes, "leg1", better=lambda a, b: a > b)

        # Round 2: with the best first leg fixed, every second-leg tier along the whole path.
        hop = None
        if best_leg1 is not None:
            first_fee = best_leg1[0]
            hop_jobs = [("leg2", fee) for fee in leg2]
            hop_outcomes = _gather(
                [
                    lambda fee=fee: self._quote_in_path(
                        encode_path([token_in, wrapped, token_out], [first_fee, fee]), amount_in
                    )
                    for _, fee in hop_jobs
                ]
            )
            best_leg2 = self._best_quote(hop_jobs, hop_outcomes, "leg2", better=lambda a, b: a > b)
            if best_leg2 is not None:
                hop = _Route(
                    [token_in, wrapped, token_out], [first_fee, best_leg2[0]], best_leg2[1]
                )

        # One pool beats two on a tie: less gas, and less that can move before the swap lands.
        if best_direct is not None and (hop is None or best_direct[1] >= hop.amount):
            return _Route([token_in, token_out], [best_direct[0]], best_direct[1])
        if hop is not None:
            return hop
        raise self._no_route(label_in, label_out)

    def _route_in(
        self, amount_out: int, token_in: str, token_out: str, *, label_in: str, label_out: str
    ) -> _Route:
        """The route needing the least token_in to deliver amount_out, with the quote that chose
        it -- the exact-output mirror of _route_out. Raises if no route can fill it."""
        wrapped = self.native_wrapped_address
        pairs = self._hop_pairs(token_in, token_out)
        tiers = self._existing_tiers(pairs)
        direct = tiers[pairs[0]]
        leg1, leg2 = (tiers[pairs[1]], tiers[pairs[2]]) if len(pairs) == 3 else ([], [])

        # Round 1: every direct tier, and every output-side tier (wrapped native -> token_out).
        jobs = [("direct", fee) for fee in direct]
        if leg1 and leg2:
            jobs += [("leg2", fee) for fee in leg2]
        outcomes = _gather(
            [
                (lambda fee=fee: self._quote_out_single(token_in, token_out, amount_out, fee))
                if kind == "direct"
                else (lambda fee=fee: self._quote_out_single(wrapped, token_out, amount_out, fee))
                for kind, fee in jobs
            ]
        ) if jobs else []
        best_direct = self._best_quote(jobs, outcomes, "direct", better=lambda a, b: a < b)
        best_leg2 = self._best_quote(jobs, outcomes, "leg2", better=lambda a, b: a < b)

        # Round 2: with the best output side fixed, every input-side tier along the whole path,
        # which exact-output quotes and swaps read backwards: token_out first.
        hop = None
        if best_leg2 is not None:
            second_fee = best_leg2[0]
            hop_jobs = [("leg1", fee) for fee in leg1]
            hop_outcomes = _gather(
                [
                    lambda fee=fee: self._quote_out_path(
                        encode_path([token_out, wrapped, token_in], [second_fee, fee]), amount_out
                    )
                    for _, fee in hop_jobs
                ]
            )
            best_leg1 = self._best_quote(hop_jobs, hop_outcomes, "leg1", better=lambda a, b: a < b)
            if best_leg1 is not None:
                hop = _Route(
                    [token_in, wrapped, token_out], [best_leg1[0], second_fee], best_leg1[1]
                )

        if best_direct is not None and (hop is None or best_direct[1] <= hop.amount):
            return _Route([token_in, token_out], [best_direct[0]], best_direct[1])
        if hop is not None:
            return hop
        raise self._no_route(label_in, label_out)

    @staticmethod
    def _no_route(label_in: str, label_out: str) -> ToolException:
        """The one place this message is worded, so every failed route search reports alike."""
        return ToolException(
            f"No Uniswap V3 liquidity path found for {label_in} -> {label_out}. No pool, direct or "
            f"through the wrapped native token, can fill this amount."
        )

    # ---- plans ----

    def _encode(self, contract: Contract, fn_name: str, args: list) -> bytes:
        """One call's calldata as bytes, for nesting inside a multicall."""
        try:
            return bytes.fromhex(
                contract.encode_abi(abi_element_identifier=fn_name, args=args)[2:]
            )
        except (ValueError, MismatchedABI) as err:
            raise ToolException(f"Could not encode {fn_name} call: {err}") from err

    def _call(
        self,
        contract: Contract,
        fn_name: str,
        args: list,
        *,
        value: int = 0,
        role: str,
        description: str,
    ) -> dict:
        """
        Builds one account-agnostic call: (to, value, data) plus metadata. Pure ABI encoding --
        makes no RPC request, so it works identically whether the caller is an EOA or a
        smart-contract wallet.
        """
        try:
            data = contract.encode_abi(abi_element_identifier=fn_name, args=args)
        except (ValueError, MismatchedABI) as err:
            raise ToolException(f"Could not encode {fn_name} call: {err}") from err
        return {
            "to": Web3.to_checksum_address(contract.address),
            "value": int(value),
            "data": data,
            "role": role,
            "description": description,
        }

    def _plan(
        self, calls: list[dict], *, from_address: str, summary: dict, nonce: int | None = None
    ) -> dict:
        """
        Assembles an execution plan from an ordered list of account-agnostic calls. calls is
        always populated; transactions is only rendered in "eoa" tx_mode (see _render_eoa) and is
        None in "calls" mode, where a smart-contract wallet's own batch executor consumes calls
        directly.
        """
        plan = {
            "calls": calls,
            "transactions": None,
            "chain_id": self._chain_id,
            "summary": summary,
        }
        if self.tx_mode == "eoa":
            plan["transactions"] = self._render_eoa(calls, from_address, nonce)
        return plan

    def _fee_params(self) -> dict:
        """
        EIP-1559 fee fields when the chain's latest block reports a base fee, otherwise legacy
        gasPrice. Read once per plan and reused across every transaction in it, so a multi-call
        plan carries one consistent fee snapshot instead of a fresh (and possibly inconsistent)
        one per call.
        """
        latest_block = self.w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")
        if base_fee is None:
            return {"gasPrice": self.w3.eth.gas_price}
        max_priority_fee = self.w3.eth.max_priority_fee
        return {
            "maxFeePerGas": base_fee * 2 + max_priority_fee,
            "maxPriorityFeePerGas": max_priority_fee,
        }

    def _default_gas(self, call: dict) -> int:
        """Static fallback gas limit for a call whose gas could not be estimated yet (see
        _gas_for), keyed by the call's role."""
        return self.default_gas.get(call["role"], 300_000)

    def _gas_for(
        self, call: dict, sender: str, *, has_pending_prerequisite: bool
    ) -> tuple[int, bool]:
        """
        Returns (gas_limit, was_estimated).

        A call whose prerequisite approval is earlier in this same plan cannot be simulated yet --
        the allowance is not on-chain until that earlier call is mined. For those, fall back to a
        static limit instead of failing the whole build. The first call in a plan has no such
        excuse, so its revert is still fatal and surfaces to the caller.
        """
        if not self.estimate_gas:
            return self._default_gas(call), False
        try:
            estimate = self.w3.eth.estimate_gas(
                {
                    "from": sender,
                    "to": call["to"],
                    "value": call["value"],
                    "data": call["data"],
                }
            )
            return int(estimate * self.gas_buffer), True
        except (ContractLogicError, ValueError) as err:
            if has_pending_prerequisite:
                return self._default_gas(call), False
            raise ToolException(f"Transaction would revert: {err}") from err

    def _render_eoa(
        self, calls: list[dict], from_address: str, nonce: int | None = None
    ) -> list[dict]:
        """Renders calls as unsigned, signable EOA transactions with sequential nonces and one
        shared fee snapshot."""
        sender = Web3.to_checksum_address(from_address)
        base_nonce = (
            nonce
            if nonce is not None
            else self.w3.eth.get_transaction_count(sender, "pending")
        )
        fees = self._fee_params()

        txs = []
        for index, call in enumerate(calls):
            gas, estimated = self._gas_for(call, sender, has_pending_prerequisite=index > 0)
            txs.append(
                {
                    "from": sender,
                    "to": call["to"],
                    "value": call["value"],
                    "data": call["data"],
                    "nonce": base_nonce + index,
                    "chainId": self._chain_id,
                    "gas": gas,
                    "gas_estimated": estimated,
                    **fees,
                }
            )
        return txs

    def _approval_calls(
        self, spender: str, approvals: list[tuple[Contract, int, str]]
    ) -> tuple[list[dict], list[dict]]:
        """
        Builds the approve() call(s) a set of token pulls needs. Returns (leading, trailing):
        leading always has one approve call per approval, in order; trailing has a matching
        approve(spender, 0) for each approval, but only when reset_residual_approvals is enabled
        -- it is the caller's job to decide whether to append trailing at all (skip it entirely
        for a call that always pulls its exact approved amount, since a reset after that is a
        guaranteed no-op).
        """
        spender = Web3.to_checksum_address(spender)
        leading, trailing = [], []
        for token, amount_base, label in approvals:
            leading.append(
                self._call(
                    token,
                    "approve",
                    [spender, amount_base],
                    role="approve",
                    description=f"Approve {spender} to spend {label}",
                )
            )
            if self.reset_residual_approvals:
                trailing.append(
                    self._call(
                        token,
                        "approve",
                        [spender, 0],
                        role="approve_reset",
                        description=f"Clear residual {label} allowance for {spender}",
                    )
                )
        return leading, trailing

    @staticmethod
    def _deadline(deadline_secs: int) -> int:
        if deadline_secs < 1:
            raise ToolException(f"deadline_secs must be at least 1, not {deadline_secs}.")
        return int(time.time()) + deadline_secs

    @staticmethod
    def _check_slippage(slippage_bps: int) -> None:
        if not 0 <= slippage_bps < BPS_DENOMINATOR:
            raise ToolException(
                f"slippage_bps must be between 0 and {BPS_DENOMINATOR - 1}, not {slippage_bps}."
            )

    # ---- balance checks ----
    #
    # Neither reads an allowance: they only answer "does this address hold enough", the question
    # gas estimation used to answer by accident before write tools could build a plan ahead of a
    # standing approval.

    def _require_token_balance(
        self, token_address: str, amount_base: int, owner_address: str, *, decimals: int, purpose: str
    ) -> None:
        """Preflight check for write tools: raises ToolException naming the held amount, the
        required amount, and the shortfall, when owner_address doesn't hold enough of
        token_address for purpose."""
        balance = self._token_balance(token_address, owner_address)
        if balance < amount_base:
            raise ToolException(
                f"{owner_address} needs {amount_base / 10**decimals} of {token_address} for "
                f"{purpose} but only holds {balance / 10**decimals} -- short by "
                f"{(amount_base - balance) / 10**decimals}."
            )

    def _require_native_balance(
        self, amount_base: int, owner_address: str, *, purpose: str
    ) -> None:
        """Native-asset equivalent of _require_token_balance."""
        balance = self._native_balance(owner_address)
        if balance < amount_base:
            raise ToolException(
                f"{owner_address} needs {amount_base / 10**18} of the native asset for "
                f"{purpose} but only holds {balance / 10**18} -- short by "
                f"{(amount_base - balance) / 10**18}."
            )

    def _require_funds(
        self,
        token_address: str,
        amount_base: int,
        owner_address: str,
        *,
        decimals: int,
        native: bool,
        purpose: str,
    ) -> None:
        if native:
            self._require_native_balance(amount_base, owner_address, purpose=purpose)
        else:
            self._require_token_balance(
                token_address, amount_base, owner_address, decimals=decimals, purpose=purpose
            )

    # ---- swaps ----

    def _swap_leg_args(self, route: _Route, recipient: str, amount: int, limit: int, *, exact_in: bool):
        """The router function and arguments that run `route`: the Single form for one hop, the
        path form for two. An exact-output path is read backwards, token_out first."""
        if len(route.fees) == 1:
            params = (route.path[0], route.path[1], route.fees[0], recipient, amount, limit, 0)
            return ("exactInputSingle" if exact_in else "exactOutputSingle"), [params]
        if exact_in:
            return "exactInput", [(encode_path(route.path, route.fees), recipient, amount, limit)]
        backwards = encode_path(route.path[::-1], route.fees[::-1])
        return "exactOutput", [(backwards, recipient, amount, limit)]

    @staticmethod
    def _route_note(route: _Route) -> str:
        if len(route.fees) == 1:
            return f"Uniswap V3, {_fee_str(route.fees[0])} pool"
        return (
            f"Uniswap V3, through the wrapped native token: {_fee_str(route.fees[0])} pool, "
            f"then {_fee_str(route.fees[1])} pool"
        )

    def _swap_exact_in(
        self,
        *,
        token_in: str | None,
        token_out: str | None,
        amount_in: float,
        from_address: str,
        recipient: str | None,
        slippage_bps: int,
        deadline_secs: int,
        nonce: int | None,
        native_in: bool,
        native_out: bool,
        keys: tuple[str, ...],
    ) -> dict:
        """
        The exact-input swap behind three tools. token_in / token_out are None on the native
        side. Native coin in is the call's value (the router wraps it); native coin out is
        swapped to the router itself and unwrapped to the recipient by unwrapWETH9, all inside one
        multicall. An exact-input swap pulls exactly what it was approved, so nothing is reset.
        """
        self._check_slippage(slippage_bps)
        wrapped = self.native_wrapped_address
        sold = wrapped if native_in else _address(token_in)
        bought = wrapped if native_out else _address(token_out)
        if sold == bought:
            raise ToolException("The token being sold and the token being bought are the same.")
        decimals_in, decimals_out = self._decimals_of(sold, bought)
        amount_in_base = self._to_base_units(amount_in, decimals_in)
        if amount_in_base <= 0:
            raise ToolException("amount_in must be greater than zero.")
        if self.preflight:
            self._require_funds(
                sold, amount_in_base, from_address,
                decimals=decimals_in, native=native_in, purpose="this swap",
            )
        route = self._route_out(
            amount_in_base, sold, bought,
            label_in="the native asset" if native_in else sold,
            label_out="the native asset" if native_out else bought,
        )
        amount_out_min = route.amount * (BPS_DENOMINATOR - slippage_bps) // BPS_DENOMINATOR
        to = Web3.to_checksum_address(recipient or from_address)
        # Native out: the swap pays the router, which unwraps to the recipient.
        swap_to = self.router.address if native_out else to
        fn_name, args = self._swap_leg_args(route, swap_to, amount_in_base, amount_out_min, exact_in=True)
        inner = [self._encode(self.router, fn_name, args)]
        if native_out:
            inner.append(self._encode(self.router, "unwrapWETH9", [amount_out_min, to]))

        leading, _trailing = (
            ([], []) if native_in
            else self._approval_calls(
                self.router.address,
                [(self._erc20(sold), amount_in_base, f"{_amount_str(amount_in_base, decimals_in)} of {sold}")],
            )
        )
        spent = "the native asset" if native_in else sold
        got = "the native asset" if native_out else bought
        action = self._call(
            self.router,
            "multicall",
            [self._deadline(deadline_secs), inner],
            value=amount_in_base if native_in else 0,
            role="swap",
            description=(
                f"Swap {_amount_str(amount_in_base, decimals_in)} of {spent} for at least "
                f"{_amount_str(amount_out_min, decimals_out)} of {got} ({self._route_note(route)})"
            ),
        )
        full = {
            "token_in": sold,
            "token_out": bought,
            "amount_in": amount_in,
            "amount_out_min": amount_out_min / 10**decimals_out,
            "path": route.path,
            "fees": route.fees,
        }
        return self._plan(
            [*leading, action],
            from_address=from_address,
            summary={key: full[key] for key in keys},
            nonce=nonce,
        )

    def _swap_exact_out(
        self,
        *,
        token_in: str | None,
        token_out: str | None,
        amount_out: float,
        from_address: str,
        recipient: str | None,
        slippage_bps: int,
        deadline_secs: int,
        nonce: int | None,
        native_in: bool,
        native_out: bool,
        keys: tuple[str, ...],
    ) -> dict:
        """
        The exact-output swap behind three tools. The input is capped at the live quote plus
        slippage, and the swap usually pulls less, so a token's approval is reset afterwards and a
        native coin's unused value is paid back by refundETH inside the multicall.
        """
        self._check_slippage(slippage_bps)
        wrapped = self.native_wrapped_address
        sold = wrapped if native_in else _address(token_in)
        bought = wrapped if native_out else _address(token_out)
        if sold == bought:
            raise ToolException("The token being sold and the token being bought are the same.")
        decimals_in, decimals_out = self._decimals_of(sold, bought)
        amount_out_base = self._to_base_units(amount_out, decimals_out)
        if amount_out_base <= 0:
            raise ToolException("amount_out must be greater than zero.")
        route = self._route_in(
            amount_out_base, sold, bought,
            label_in="the native asset" if native_in else sold,
            label_out="the native asset" if native_out else bought,
        )
        amount_in_max = route.amount * (BPS_DENOMINATOR + slippage_bps) // BPS_DENOMINATOR
        if self.preflight:
            self._require_funds(
                sold, amount_in_max, from_address,
                decimals=decimals_in, native=native_in, purpose="this swap",
            )
        to = Web3.to_checksum_address(recipient or from_address)
        swap_to = self.router.address if native_out else to
        fn_name, args = self._swap_leg_args(route, swap_to, amount_out_base, amount_in_max, exact_in=False)
        inner = [self._encode(self.router, fn_name, args)]
        if native_out:
            inner.append(self._encode(self.router, "unwrapWETH9", [amount_out_base, to]))
        if native_in:
            inner.append(self._encode(self.router, "refundETH", []))

        leading, trailing = (
            ([], []) if native_in
            else self._approval_calls(
                self.router.address,
                [(self._erc20(sold), amount_in_max, f"up to {_amount_str(amount_in_max, decimals_in)} of {sold}")],
            )
        )
        spent = "the native asset" if native_in else sold
        got = "the native asset" if native_out else bought
        action = self._call(
            self.router,
            "multicall",
            [self._deadline(deadline_secs), inner],
            value=amount_in_max if native_in else 0,
            role="swap",
            description=(
                f"Swap up to {_amount_str(amount_in_max, decimals_in)} of {spent} for exactly "
                f"{_amount_str(amount_out_base, decimals_out)} of {got} ({self._route_note(route)})"
            ),
        )
        full = {
            "token_in": sold,
            "token_out": bought,
            "amount_out": amount_out,
            "amount_in_max": amount_in_max / 10**decimals_in,
            "path": route.path,
            "fees": route.fees,
        }
        return self._plan(
            [*leading, action, *trailing],
            from_address=from_address,
            summary={key: full[key] for key in keys},
            nonce=nonce,
        )

    # ---- liquidity ----

    def _plan_deposit(
        self,
        pool: _Pool,
        tick_lower: int,
        tick_upper: int,
        token_a: str,
        amount_a: float,
        decimals: tuple[int, int],
        *,
        use_native: bool,
        slippage_bps: int,
    ) -> _Deposit:
        """
        Works out a deposit of amount_a of token_a into [tick_lower, tick_upper): the other
        token's amount, each token's minimum, and which token (if any) is paid as native coin.

        The minimums are each desired amount less the slippage -- but never above what the position
        manager will actually pull (see mint_outcome), which is a few wei under the desired amount
        of token_a, and for the other token can even sit a hair under the amount derived for it.
        Without that cap a slippage of zero could never succeed.

        @param decimals  (decimals0, decimals1) of the pool's tokens.
        """
        self._check_slippage(slippage_bps)
        a_is_token0 = token_a == pool.token0
        token_b = pool.token1 if a_is_token0 else pool.token0
        amount_a_base = self._to_base_units(amount_a, decimals[0 if a_is_token0 else 1])
        if amount_a_base <= 0:
            raise ToolException("amount_a must be greater than zero.")
        _, derived0, derived1 = deposit_amounts(
            pool.sqrt_price, tick_lower, tick_upper, a_is_token0, amount_a_base,
            token_a=token_a, token_b=token_b,
        )
        desired0 = amount_a_base if a_is_token0 else derived0
        desired1 = derived1 if a_is_token0 else amount_a_base
        # What the position manager will really do with those, which the minimums are set against.
        liquidity, used0, used1 = mint_outcome(
            pool.sqrt_price, tick_lower, tick_upper, desired0, desired1
        )
        if liquidity <= 0:
            raise ToolException(f"{token_a} amount is too small to buy any liquidity in this range.")
        native_side = None
        if use_native:
            wrapped = self.native_wrapped_address
            if wrapped not in (pool.token0, pool.token1):
                raise ToolException(
                    "use_native only applies to a pool that has the wrapped native token "
                    f"({wrapped}) as one of its tokens."
                )
            native_side = 0 if pool.token0 == wrapped else 1

        def floor(desired: int, used: int) -> int:
            return min(desired * (BPS_DENOMINATOR - slippage_bps) // BPS_DENOMINATOR, used)

        return _Deposit(
            liquidity=liquidity,
            amount0=desired0,
            amount1=desired1,
            min0=floor(desired0, used0),
            min1=floor(desired1, used1),
            native_side=native_side,
            native_value=(
                0 if native_side is None else (desired0 if native_side == 0 else desired1)
            ),
        )

    def _deposit_calls(
        self,
        pool: _Pool,
        deposit: _Deposit,
        decimals: tuple[int, int],
        *,
        fn_name: str,
        args: list,
        description: str,
        from_address: str,
    ) -> list[dict]:
        """
        The plan's calls for a mint or an increase: an approval for each token the position
        manager will pull (never the native coin, which rides as value), the action, then each
        approval reset -- the position manager takes only what the pool needs, which is at most
        what was approved. With native coin the action is multicall([action, refundETH]) so the
        part of the value not used comes straight back.
        """
        if self.preflight:
            for side, token, amount, dec in (
                (0, pool.token0, deposit.amount0, decimals[0]),
                (1, pool.token1, deposit.amount1, decimals[1]),
            ):
                if amount > 0:
                    self._require_funds(
                        token, amount, from_address, decimals=dec,
                        native=deposit.native_side == side, purpose="this deposit",
                    )
        pulls = [
            (self._erc20(token), amount, f"{_amount_str(amount, dec)} of {token}")
            for side, token, amount, dec in (
                (0, pool.token0, deposit.amount0, decimals[0]),
                (1, pool.token1, deposit.amount1, decimals[1]),
            )
            if amount > 0 and deposit.native_side != side
        ]
        leading, trailing = self._approval_calls(self.position_manager.address, pulls)
        if deposit.native_side is None:
            action = self._call(
                self.position_manager, fn_name, args, role="add_liquidity", description=description
            )
        else:
            inner = [
                self._encode(self.position_manager, fn_name, args),
                self._encode(self.position_manager, "refundETH", []),
            ]
            action = self._call(
                self.position_manager,
                "multicall",
                [inner],
                value=deposit.native_value,
                role="add_liquidity",
                description=description,
            )
        return [*leading, action, *trailing]

    def _owned_position(
        self, token_id: int, from_address: str, *, with_fees: bool = False
    ) -> tuple[dict, tuple[int, int] | None]:
        """
        The position, after checking that from_address owns it -- the position manager would let
        anyone top one up and only the owner take one out, and neither is what a wallet means by
        "my position". With with_fees, what collect would pay out right now comes back too, read
        in the same round as the owner and the position (it is simulated from from_address, so an
        address that does not own the position fails the ownership check first).

        @return  (position, fees): fees is (fees0, fees1) in base units, or None.
        @raises ToolException  If the position does not exist or is not from_address's, or -- with
                               with_fees -- if the position manager refuses to simulate collect,
                               in which case the real collect would revert too.
        """
        owner = Web3.to_checksum_address(from_address)
        reads = [lambda: self._read_owner_of(token_id), lambda: self._read_position(token_id)]
        if with_fees:
            reads.append(lambda: self._read_collectable(token_id, owner))
        results = _gather(reads)
        for result in results[:2]:
            if isinstance(result, Exception):
                raise result
        actual, position = results[0], results[1]
        if actual != owner:
            raise ToolException(f"Position #{token_id} is owned by {actual}, not by {from_address}.")
        if not with_fees:
            return position, None
        fees = results[2]
        if isinstance(fees, ContractLogicError):
            raise ToolException(
                f"Could not work out the fees position #{token_id} has earned: collect would "
                f"revert for it ({fees})."
            ) from fees
        if isinstance(fees, Exception):
            raise fees
        return position, fees

    def _position_pool(self, position: dict) -> _Pool:
        """The state of the pool a position lives in."""
        key = (position["token0"], position["token1"], position["fee"])
        address = self._find_pools([key])[_pool_key(*key)]
        if address is None:
            raise ToolException(
                f"The pool of position {position['token0']} / {position['token1']} "
                f"({_fee_str(position['fee'])}) was not found on this chain."
            )
        sqrt_price, tick, spacing, _ = self._pool_states([address])[address]
        return _Pool(address, key[0], key[1], key[2], sqrt_price, tick, spacing, 0)

    def _collect_calls(
        self, token_id: int, tokens: tuple[str, str], recipient: str, native_out: bool
    ) -> list[bytes]:
        """
        The inner calls that pay a position's owed tokens out: collect, and -- for native_out --
        collect to the position manager itself (recipient address(0) means "me") followed by
        unwrapWETH9 and sweepToken, which pass the wrapped native token's share on as native coin
        and the other token as itself. Both take the contract's whole balance (minimum 0), so
        fees that accrue before the plan lands are paid out too.
        """
        pm = self.position_manager
        collect_to = ZERO_ADDRESS if native_out else recipient
        calls = [
            self._encode(
                pm, "collect", [(token_id, collect_to, v3math.MAX_UINT128, v3math.MAX_UINT128)]
            )
        ]
        if native_out:
            wrapped = self.native_wrapped_address
            if wrapped not in tokens:
                raise ToolException(
                    "native_out needs one of the position's two tokens to be the wrapped native "
                    f"token ({wrapped})."
                )
            other = tokens[1] if tokens[0] == wrapped else tokens[0]
            calls.append(self._encode(pm, "unwrapWETH9", [0, recipient]))
            calls.append(self._encode(pm, "sweepToken", [other, 0, recipient]))
        return calls

    # ---- positions ----

    def _position_row(
        self,
        token_id: int,
        owner: str,
        position: dict,
        fees: tuple[int, int],
        pool: _Pool,
        decimals: tuple[int, int],
    ) -> dict:
        """One position as a row: where its range sits, whether the price is inside it, what it
        holds and what it has earned."""
        decimals0, decimals1 = decimals
        base, quote = orient(
            position["token0"], position["token1"], self.native_wrapped_address, self.stable_tokens
        )
        base_is_token0 = base == position["token0"]
        min_price, max_price, full_range = prices_for_ticks(
            position["tick_lower"], position["tick_upper"], base_is_token0, decimals0, decimals1,
            pool.spacing,
        )
        amount0, amount1 = v3math.amounts_for_liquidity(
            pool.sqrt_price,
            v3math.get_sqrt_ratio_at_tick(position["tick_lower"]),
            v3math.get_sqrt_ratio_at_tick(position["tick_upper"]),
            position["liquidity"],
        )
        return {
            "token_id": token_id,
            "owner": owner,
            "token0": position["token0"],
            "token1": position["token1"],
            "decimals0": decimals0,
            "decimals1": decimals1,
            "fee": position["fee"],
            "fee_pct": _fee_pct(position["fee"]),
            "pool": pool.address,
            "tick_lower": position["tick_lower"],
            "tick_upper": position["tick_upper"],
            "tick": pool.tick,
            "liquidity": str(position["liquidity"]),
            "base_token": base,
            "quote_token": quote,
            "min_price": min_price,
            "max_price": max_price,
            "full_range": full_range,
            "current_price": v3math.human_price(
                v3math.raw_price_from_sqrt(pool.sqrt_price), base_is_token0, decimals0, decimals1
            ),
            "in_range": position["tick_lower"] <= pool.tick < position["tick_upper"],
            "amount0": amount0 / 10**decimals0,
            "amount1": amount1 / 10**decimals1,
            "amount0_raw": str(amount0),
            "amount1_raw": str(amount1),
            "fees0": fees[0] / 10**decimals0,
            "fees1": fees[1] / 10**decimals1,
            "fees0_raw": str(fees[0]),
            "fees1_raw": str(fees[1]),
            "empty": position["liquidity"] == 0 and fees == (0, 0),
        }

    def _position_rows(
        self, owners: dict[int, str], *, strict: bool, read: dict[int, dict] | None = None
    ) -> list[dict]:
        """
        Rows for these positions ({token_id: owner}), reading everything together in a few rounds:
        the positions and what each would collect; then the pools and the tokens' decimals; then
        the pools' prices.

        Anyone can mint a position to any address, built from tokens of their choosing -- a token
        whose decimals() reverts, say -- so a position that can't be read is left out of a list
        (strict=False) rather than failing the whole list. A single position asked for by id
        (strict=True) raises instead.

        @param read  Positions the caller already read ({token_id: _read_position's dict}), which
                     are not read again.
        """
        ids = list(owners)
        if read is None:
            results = _gather(
                [lambda t=t: self._read_position(t) for t in ids]
                + [lambda t=t: self._read_collectable(t, owners[t]) for t in ids]
            )
            positions, collectables = results[: len(ids)], results[len(ids):]
        else:
            positions = [read[t] for t in ids]
            collectables = _gather([lambda t=t: self._read_collectable(t, owners[t]) for t in ids])
        readable: dict[int, dict] = {}
        for token_id, position in zip(ids, positions):
            if isinstance(position, Exception):
                if strict or not isinstance(position, (ToolException, ContractLogicError, BadFunctionCallOutput)):
                    raise position
                continue
            readable[token_id] = position
        fees: dict[int, tuple[int, int]] = {}
        for token_id, collectable in zip(ids, collectables):
            if token_id not in readable:
                continue
            if isinstance(collectable, Exception):
                if not isinstance(collectable, (ContractLogicError, BadFunctionCallOutput)):
                    raise collectable
                # The simulation was refused (a token that blocks the owner, say): the fees the
                # position manager has already booked are the best that can be said.
                collectable = (readable[token_id]["owed0"], readable[token_id]["owed1"])
            fees[token_id] = collectable
        if not readable:
            return []

        tokens = list(dict.fromkeys(t for p in readable.values() for t in (p["token0"], p["token1"])))
        triples = list(dict.fromkeys((p["token0"], p["token1"], p["fee"]) for p in readable.values()))
        missing = [t for t in tokens if t not in self._decimals_cache]
        decimals_results = _gather([lambda t=t: self._decimals(t) for t in missing])
        pools = self._find_pools(triples)
        states = self._pool_states([a for a in pools.values() if a is not None])

        rows = []
        for token_id, position in readable.items():
            key = _pool_key(position["token0"], position["token1"], position["fee"])
            address = pools[key]
            failed = [r for t, r in zip(missing, decimals_results)
                      if isinstance(r, Exception) and t in (position["token0"], position["token1"])]
            if address is None or failed:
                if strict:
                    if failed:
                        raise failed[0]
                    raise ToolException(
                        f"The pool of position #{token_id} was not found on this chain."
                    )
                continue
            sqrt_price, tick, spacing, _ = states[address]
            pool = _Pool(address, key[0], key[1], key[2], sqrt_price, tick, spacing, 0)
            try:
                rows.append(
                    self._position_row(
                        token_id, owners[token_id], position, fees[token_id], pool,
                        (self._decimals_cache[position["token0"]], self._decimals_cache[position["token1"]]),
                    )
                )
            except (ArithmeticError, ValueError):  # a malicious token's absurd decimals
                if strict:
                    raise
        return rows

    # ---- LangChain tools ----

    def get_tools(self) -> list:
        """
        Returns LangChain tools bound to this toolkit's RPC/router/position-manager
        configuration. Pass these directly into an agent's tools=[...] list.
        """
        toolkit = self

        @tool
        def get_quote_in(token_in: str, token_out: str, amount_out: float) -> dict:
            """
            Returns how much of token_in is required to receive an exact amount of token_out on
            Uniswap V3. Quotes the direct pool at every fee tier that exists and the route
            through the wrapped native token, with QuoterV2, and returns whichever needs the least
            token_in; the returned path and fees say which one that was.

            Use this tool when the user wants to know the cost of acquiring a specific amount of
            a token (e.g. "how much USDC do I need to buy exactly 100 DAI?").

            Args:
                token_in: Contract address of the token being spent.
                token_out: Contract address of the token being received.
                amount_out: The exact amount of token_out to receive, in whole units (e.g. 100
                    for 100 tokens, adjusted for that token's decimals internally).

            Returns:
                A dict with:
                  - amount_in (float): required token_in in whole units.
                  - amount_out (float): the requested token_out amount, echoed back.
                  - path (list[str]): the token address path used for the quote, in swap order.
                  - fees (list[int]): the fee tier of each hop (500 is 0.05%).
            """
            token_in, token_out = _address(token_in), _address(token_out)
            if token_in == token_out:
                raise ToolException("token_in and token_out are the same token.")
            decimals_in, decimals_out = toolkit._decimals_of(token_in, token_out)
            amount_out_base = toolkit._to_base_units(amount_out, decimals_out)
            if amount_out_base <= 0:
                raise ToolException("amount_out must be greater than zero.")
            route = toolkit._route_in(
                amount_out_base, token_in, token_out, label_in=token_in, label_out=token_out
            )
            return {
                "amount_in": route.amount / 10**decimals_in,
                "amount_out": amount_out,
                "path": route.path,
                "fees": route.fees,
            }

        @tool
        def get_quote_out(token_in: str, token_out: str, amount_in: float) -> dict:
            """
            Returns how much of token_out will be received when spending an exact amount of
            token_in on Uniswap V3. Quotes the direct pool at every fee tier that exists and the
            route through the wrapped native token, with QuoterV2, and returns whichever pays
            the most token_out; the returned path and fees say which one that was.

            Use this tool when the user wants to know how much they'll receive for a given spend
            (e.g. "how much DAI will I get for 100 USDC?").

            Args:
                token_in: Contract address of the token being spent.
                token_out: Contract address of the token being received.
                amount_in: The exact amount of token_in to spend, in whole units (e.g. 100 for
                    100 tokens, adjusted for that token's decimals internally).

            Returns:
                A dict with:
                  - amount_in (float): the token_in amount, echoed back.
                  - amount_out (float): expected token_out in whole units.
                  - path (list[str]): the token address path used for the quote, in swap order.
                  - fees (list[int]): the fee tier of each hop (500 is 0.05%).
            """
            token_in, token_out = _address(token_in), _address(token_out)
            if token_in == token_out:
                raise ToolException("token_in and token_out are the same token.")
            decimals_in, decimals_out = toolkit._decimals_of(token_in, token_out)
            amount_in_base = toolkit._to_base_units(amount_in, decimals_in)
            if amount_in_base <= 0:
                raise ToolException("amount_in must be greater than zero.")
            route = toolkit._route_out(
                amount_in_base, token_in, token_out, label_in=token_in, label_out=token_out
            )
            return {
                "amount_in": amount_in,
                "amount_out": route.amount / 10**decimals_out,
                "path": route.path,
                "fees": route.fees,
            }

        @tool
        def swap_exact_tokens_for_tokens(
            token_in: str,
            token_out: str,
            amount_in: float,
            from_address: str,
            recipient: str | None = None,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that swaps an exact amount of token_in for token_out through
            SwapRouter02, on the best route (see get_quote_out). The swap is the router's
            exactInputSingle (one pool) or exactInput (two), wrapped in its
            multicall(deadline, ...) so the deadline is enforced.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- the required approval first, then the swap. The router pulls
            exactly the approved amount, so nothing is reset afterwards. Execute all of them, in
            order; if your account supports batching, execute them atomically in one transaction.
            In EOA mode `transactions` holds the same calls rendered as unsigned, signable
            transactions with sequential nonces already assigned; in calls mode it is None.
            `summary` holds the amounts in whole units and is safe to show the user. This tool
            never signs or broadcasts anything.

            Args:
                token_in: Contract address of the token being sold.
                token_out: Contract address of the token being bought.
                amount_in: The exact amount of token_in to sell, in whole units.
                from_address: The address that will sign this transaction -- also the recipient
                    of token_out unless recipient is set.
                recipient: Optional address to receive token_out. Defaults to from_address.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). A
                    live quote is used to derive amountOutMinimum as the quoted output reduced by
                    this percentage. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the swap reverts if not yet mined.
                    Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used. The approval call ahead of the swap gets this nonce; the swap gets the
                    next one.
            """
            return toolkit._swap_exact_in(
                token_in=token_in, token_out=token_out, amount_in=amount_in,
                from_address=from_address, recipient=recipient, slippage_bps=slippage_bps,
                deadline_secs=deadline_secs, nonce=nonce, native_in=False, native_out=False,
                keys=("token_in", "token_out", "amount_in", "amount_out_min", "path", "fees"),
            )

        @tool
        def swap_tokens_for_exact_tokens(
            token_in: str,
            token_out: str,
            amount_out: float,
            from_address: str,
            recipient: str | None = None,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that swaps as much of token_in as needed for an exact amount of
            token_out through SwapRouter02 (exactOutputSingle or exactOutput inside
            multicall(deadline, ...)). The actual amount pulled is derived from a live quote plus
            slippage_bps and may be less than the approved amountInMaximum -- when
            reset_residual_approvals is enabled (see the constructor) the plan's last call resets
            any resulting residual approval to zero.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- the required approval first, then the swap, then an approval
            reset where the router may pull less than it was approved for. Execute all of them,
            in order; if your account supports batching, execute them atomically in one
            transaction. In EOA mode `transactions` holds the same calls rendered as unsigned,
            signable transactions with sequential nonces already assigned; in calls mode it is
            None. `summary` holds the amounts in whole units and is safe to show the user. This
            tool never signs or broadcasts anything.

            Args:
                token_in: Contract address of the token being sold.
                token_out: Contract address of the token being bought.
                amount_out: The exact amount of token_out to receive, in whole units.
                from_address: The address that will sign this transaction -- also the recipient
                    of token_out unless recipient is set.
                recipient: Optional address to receive token_out. Defaults to from_address.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). A
                    live quote is used to derive amountInMaximum as the quoted input increased by
                    this percentage. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the swap reverts if not yet mined.
                    Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used; each call in the plan gets the next sequential nonce.
            """
            return toolkit._swap_exact_out(
                token_in=token_in, token_out=token_out, amount_out=amount_out,
                from_address=from_address, recipient=recipient, slippage_bps=slippage_bps,
                deadline_secs=deadline_secs, nonce=nonce, native_in=False, native_out=False,
                keys=("token_in", "token_out", "amount_out", "amount_in_max", "path", "fees"),
            )

        @tool
        def swap_exact_eth_for_tokens(
            token_out: str,
            amount_in: float,
            from_address: str,
            recipient: str | None = None,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that swaps an exact amount of the chain's native asset (ETH, etc.) for
            token_out through SwapRouter02. "ETH" in the tool name is a generic internal label --
            this works identically on every supported network. The native amount is sent as the
            multicall's value and the router wraps it.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- here, just the one swap call, with the native-asset amount
            set as its value (no approval is needed to send native value). Execute it; if your
            account supports batching it can be included in a larger atomic batch alongside other
            calls. In EOA mode `transactions` holds the same call rendered as an unsigned,
            signable transaction; in calls mode it is None. `summary` holds the amounts in whole
            units and is safe to show the user. This tool never signs or broadcasts anything.

            Args:
                token_out: Contract address of the token being bought.
                amount_in: The exact amount of the native asset to spend, in whole units (e.g.
                    1.5).
                from_address: The address that will sign this transaction and send the
                    native-asset value -- also the recipient of token_out unless recipient is
                    set.
                recipient: Optional address to receive token_out. Defaults to from_address.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). A
                    live quote is used to derive amountOutMinimum as the quoted output reduced by
                    this percentage. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the swap reverts if not yet mined.
                    Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used.
            """
            return toolkit._swap_exact_in(
                token_in=None, token_out=token_out, amount_in=amount_in,
                from_address=from_address, recipient=recipient, slippage_bps=slippage_bps,
                deadline_secs=deadline_secs, nonce=nonce, native_in=True, native_out=False,
                keys=("token_out", "amount_in", "amount_out_min", "path", "fees"),
            )

        @tool
        def swap_eth_for_exact_tokens(
            token_out: str,
            amount_out: float,
            from_address: str,
            recipient: str | None = None,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that swaps however much of the chain's native asset (ETH, etc.) is
            needed for an exact amount of token_out through SwapRouter02. The derived
            amountInMaximum is sent as the multicall's value and the router's refundETH, in the
            same multicall, pays back whatever was not used. "ETH" in the tool name is a generic
            internal label -- this works identically on every supported network.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- here, just the one swap call, with the derived amountInMax
            set as its value (no approval is needed to send native value; unused value is
            refunded on-chain). Execute it; if your account supports batching it can be included
            in a larger atomic batch alongside other calls. In EOA mode `transactions` holds the
            same call rendered as an unsigned, signable transaction; in calls mode it is None.
            `summary` holds the amounts in whole units and is safe to show the user. This tool
            never signs or broadcasts anything.

            Args:
                token_out: Contract address of the token being bought.
                amount_out: The exact amount of token_out to receive, in whole units.
                from_address: The address that will sign this transaction and send the
                    native-asset value -- also the recipient of token_out unless recipient is
                    set.
                recipient: Optional address to receive token_out. Defaults to from_address.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). A
                    live quote is used to derive amountInMaximum (the call's value) as the quoted
                    input increased by this percentage. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the swap reverts if not yet mined.
                    Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used.
            """
            return toolkit._swap_exact_out(
                token_in=None, token_out=token_out, amount_out=amount_out,
                from_address=from_address, recipient=recipient, slippage_bps=slippage_bps,
                deadline_secs=deadline_secs, nonce=nonce, native_in=True, native_out=False,
                keys=("token_out", "amount_out", "amount_in_max", "path", "fees"),
            )

        @tool
        def swap_exact_tokens_for_eth(
            token_in: str,
            amount_in: float,
            from_address: str,
            recipient: str | None = None,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that swaps an exact amount of token_in for the chain's native asset
            (ETH, etc.) through SwapRouter02. The swap pays the wrapped native token to the router
            itself and unwrapWETH9, in the same multicall, sends it on to the recipient as native
            coin. "ETH" in the tool name is a generic internal label -- this works identically on
            every supported network.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- the required approval first, then the swap. The router pulls
            exactly the approved amount, so nothing is reset afterwards. Execute all of them, in
            order; if your account supports batching, execute them atomically in one transaction.
            In EOA mode `transactions` holds the same calls rendered as unsigned, signable
            transactions with sequential nonces already assigned; in calls mode it is None.
            `summary` holds the amounts in whole units and is safe to show the user. This tool
            never signs or broadcasts anything.

            Args:
                token_in: Contract address of the token being sold.
                amount_in: The exact amount of token_in to sell, in whole units.
                from_address: The address that will sign this transaction -- also the recipient
                    of the native asset unless recipient is set.
                recipient: Optional address to receive the native asset. Defaults to
                    from_address.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). A
                    live quote is used to derive the minimum received as the quoted output
                    reduced by this percentage. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the swap reverts if not yet mined.
                    Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used. The approval call ahead of the swap gets this nonce; the swap gets the
                    next one.
            """
            return toolkit._swap_exact_in(
                token_in=token_in, token_out=None, amount_in=amount_in,
                from_address=from_address, recipient=recipient, slippage_bps=slippage_bps,
                deadline_secs=deadline_secs, nonce=nonce, native_in=False, native_out=True,
                keys=("token_in", "amount_in", "amount_out_min", "path", "fees"),
            )

        @tool
        def swap_tokens_for_exact_eth(
            token_in: str,
            amount_out: float,
            from_address: str,
            recipient: str | None = None,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that swaps however much of token_in is needed for an exact amount of
            the chain's native asset (ETH, etc.) through SwapRouter02: the swap pays the wrapped
            native token to the router and unwrapWETH9 sends exactly amount_out on as native
            coin. "ETH" in the tool/parameter names is a generic internal label -- this works
            identically on every supported network. The actual amount pulled is derived from a
            live quote plus slippage_bps and may be less than the approved amountInMaximum --
            when reset_residual_approvals is enabled (see the constructor) the plan's last call
            resets any resulting residual approval to zero.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- the required approval first, then the swap, then an approval
            reset where the router may pull less than it was approved for. Execute all of them,
            in order; if your account supports batching, execute them atomically in one
            transaction. In EOA mode `transactions` holds the same calls rendered as unsigned,
            signable transactions with sequential nonces already assigned; in calls mode it is
            None. `summary` holds the amounts in whole units and is safe to show the user. This
            tool never signs or broadcasts anything.

            Args:
                token_in: Contract address of the token being sold.
                amount_out: The exact amount of the native asset to receive, in whole units (e.g.
                    1.5).
                from_address: The address that will sign this transaction -- also the recipient
                    of the native asset unless recipient is set.
                recipient: Optional address to receive the native asset. Defaults to
                    from_address.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). A
                    live quote is used to derive amountInMaximum as the quoted input increased by
                    this percentage. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the swap reverts if not yet mined.
                    Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used; each call in the plan gets the next sequential nonce.
            """
            return toolkit._swap_exact_out(
                token_in=token_in, token_out=None, amount_out=amount_out,
                from_address=from_address, recipient=recipient, slippage_bps=slippage_bps,
                deadline_secs=deadline_secs, nonce=nonce, native_in=False, native_out=True,
                keys=("token_in", "amount_out", "amount_in_max", "path", "fees"),
            )

        @tool
        def suggest_price_ranges(token_a: str, token_b: str, fee: int | None = None) -> dict:
            """
            Returns the price ranges worth offering for a Uniswap V3 pool, at its current price:
            three bands around it -- narrow, medium and wide (+/-0.5%, 1% and 3% for a pair of
            stablecoins, +/-5%, 15% and 50% for anything else) -- and the full range. A position
            only earns fees while the price is inside its range, so a narrow range earns more per
            dollar and goes out of range sooner.

            Use this tool before open_position: show the user the options and let them pick one
            (or give their own bounds), then pass the option's min_price and max_price on. Prices
            are always "quote per base" in whole tokens (e.g. USDC per ETH). Each band's min and
            max are the prices of the ticks it actually rounds to on this pool, and the shares say
            how a deposit would split between the two tokens, by value, at today's price.

            Args:
                token_a: Contract address of the first token.
                token_b: Contract address of the second token.
                fee: Optional pool fee tier (500 for 0.05%, 3000 for 0.3%, ...). If omitted, the
                    tier whose pool has the most active liquidity is used. This tool never
                    creates a pool: it raises if none exists.

            Returns:
                A dict with:
                  - pool (str): the pool's address.
                  - fee (int) and fee_pct (float): the pool's fee tier, and in percent.
                  - base_token and quote_token (str): the two token addresses; prices are quote
                    per base.
                  - current_price (float): today's price, quote per base.
                  - stable_pair (bool): whether both tokens are stablecoins.
                  - options (list[dict]): each with name ("narrow", "medium", "wide" or "full"),
                    width_pct (float, None for full), full_range (bool), min_price (float, 0.0
                    for full), max_price (float, None for full), tick_lower, tick_upper,
                    base_share_pct and quote_share_pct (floats that sum to 100).
            """
            token_a, token_b = _address(token_a), _address(token_b)
            if token_a == token_b:
                raise ToolException("token_a and token_b are the same token.")
            decimals = toolkit._decimals_of(*_token_order(token_a, token_b))
            pool = toolkit._select_pool(token_a, token_b, fee)
            base, quote = orient(
                pool.token0, pool.token1, toolkit.native_wrapped_address, toolkit.stable_tokens
            )
            base_is_token0 = base == pool.token0
            stable_pair = pool.token0 in toolkit.stable_tokens and pool.token1 in toolkit.stable_tokens
            return {
                "pool": pool.address,
                "fee": pool.fee,
                "fee_pct": _fee_pct(pool.fee),
                "base_token": base,
                "quote_token": quote,
                "current_price": v3math.human_price(
                    v3math.raw_price_from_sqrt(pool.sqrt_price), base_is_token0,
                    decimals[0], decimals[1],
                ),
                "stable_pair": stable_pair,
                "options": range_options(
                    pool.sqrt_price, pool.spacing, base_is_token0, decimals[0], decimals[1],
                    stable_pair,
                ),
            }

        @tool
        def open_position(
            token_a: str,
            token_b: str,
            amount_a: float,
            from_address: str,
            min_price: float | None = None,
            max_price: float | None = None,
            full_range: bool = False,
            fee: int | None = None,
            use_native: bool = False,
            recipient: str | None = None,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that opens a Uniswap V3 liquidity position: it deposits amount_a of
            token_a, and however much of token_b the price range needs at today's price, into the
            pool for this pair, and the position manager mints the position as an NFT. The range
            is min_price..max_price (quote per base, whole tokens -- see suggest_price_ranges), or
            the full range; it is rounded outwards to the pool's tick spacing. The pool is the
            one at `fee`, or the fee tier with the most active liquidity. This never creates a
            pool, and refuses when the range, at today's price, needs only the other token: the
            message says which token to deposit instead. The pool takes at most the amounts
            computed here, so each approval is reset afterwards when reset_residual_approvals is
            enabled (see the constructor).

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- an approval for each token that is pulled, then the mint,
            then the approval resets. With use_native the native-coin side is sent as the mint's
            value (no approval) in multicall([mint, refundETH]), so what the pool does not use
            comes straight back. Execute all of them, in order; if your account supports
            batching, execute them atomically in one transaction. In EOA mode `transactions`
            holds the same calls rendered as unsigned, signable transactions with sequential
            nonces already assigned; in calls mode it is None. `summary` holds the amounts in
            whole units and is safe to show the user. This tool never signs or broadcasts
            anything.

            Args:
                token_a: Contract address of the first token to deposit.
                token_b: Contract address of the second token to deposit.
                amount_a: The amount of token_a to deposit, in whole units. The amount of token_b
                    follows from the range and today's price.
                from_address: The address that will sign this transaction and that holds both
                    tokens -- also the owner of the new position unless recipient is set.
                min_price: The bottom of the range, in quote per base (see suggest_price_ranges).
                    Required unless full_range.
                max_price: The top of the range, in quote per base. Required unless full_range.
                full_range: Provide liquidity across every price. Do not pass prices with it.
                fee: Optional pool fee tier (500 for 0.05%, 3000 for 0.3%, ...). If omitted, the
                    pool with the most active liquidity is used.
                use_native: Pay whichever of the two tokens is the wrapped native token as
                    native coin (ETH, etc.) instead of as the wrapped token.
                recipient: Optional address to receive the position NFT. Defaults to
                    from_address.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%).
                    Applied downward to both amount minimums. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the mint reverts if not yet mined.
                    Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used; each call in the plan gets the next sequential nonce.
            """
            token_a, token_b = _address(token_a), _address(token_b)
            if token_a == token_b:
                raise ToolException("token_a and token_b are the same token.")
            if full_range and (min_price is not None or max_price is not None):
                raise ToolException(
                    "Pass either full_range or min_price and max_price, not both."
                )
            if not full_range:
                if min_price is None or max_price is None:
                    raise ToolException(
                        "Give min_price and max_price (quote per base, whole tokens), or set "
                        "full_range."
                    )
                if not 0 < min_price < max_price:
                    raise ToolException(
                        "min_price must be above zero and below max_price."
                    )
            toolkit._check_slippage(slippage_bps)
            token0, token1 = _token_order(token_a, token_b)
            decimals = tuple(toolkit._decimals_of(token0, token1))
            pool = toolkit._select_pool(token_a, token_b, fee)
            base, quote = orient(
                pool.token0, pool.token1, toolkit.native_wrapped_address, toolkit.stable_tokens
            )
            base_is_token0 = base == pool.token0
            if full_range:
                tick_lower, tick_upper = v3math.full_range_ticks(pool.spacing)
            else:
                tick_lower, tick_upper = ticks_for_prices(
                    min_price, max_price, base_is_token0, decimals[0], decimals[1], pool.spacing
                )
            deposit = toolkit._plan_deposit(
                pool, tick_lower, tick_upper, token_a, amount_a, decimals,
                use_native=use_native, slippage_bps=slippage_bps,
            )
            low, high, is_full = prices_for_ticks(
                tick_lower, tick_upper, base_is_token0, decimals[0], decimals[1], pool.spacing
            )
            current = v3math.human_price(
                v3math.raw_price_from_sqrt(pool.sqrt_price), base_is_token0, decimals[0], decimals[1]
            )
            in_range = tick_lower <= pool.tick < tick_upper
            a_is_token0 = token_a == pool.token0
            amount_a_base, amount_b_base = (
                (deposit.amount0, deposit.amount1) if a_is_token0 else (deposit.amount1, deposit.amount0)
            )
            min_a, min_b = (deposit.min0, deposit.min1) if a_is_token0 else (deposit.min1, deposit.min0)
            dec_a, dec_b = (decimals[0], decimals[1]) if a_is_token0 else (decimals[1], decimals[0])
            to = Web3.to_checksum_address(recipient or from_address)
            span = (
                "across the full price range" if is_full
                else f"priced from {_price_str(low)} to {_price_str(high)} {quote} per {base}"
            )
            # What the user should know before agreeing: a range the price is already outside of
            # earns nothing until the price comes back, and a native-coin deposit is paid as value.
            out_of_range_note = (
                "" if in_range
                else " (out of range at today's price, so it earns nothing until the price moves in)"
            )
            native_note = "; the wrapped native token is paid as native coin" if use_native else ""
            description = (
                f"Open a Uniswap V3 position in the {pool.token0}/{pool.token1} "
                f"{_fee_str(pool.fee)} pool: {_amount_str(amount_a_base, dec_a)} of {token_a} and "
                f"{_amount_str(amount_b_base, dec_b)} of {token_b}, {span}"
                f"{out_of_range_note}{native_note}"
            )
            calls = toolkit._deposit_calls(
                pool, deposit, decimals,
                fn_name="mint",
                args=[
                    (
                        pool.token0, pool.token1, pool.fee, tick_lower, tick_upper,
                        deposit.amount0, deposit.amount1, deposit.min0, deposit.min1,
                        to, toolkit._deadline(deadline_secs),
                    )
                ],
                description=description,
                from_address=from_address,
            )
            return toolkit._plan(
                calls,
                from_address=from_address,
                summary={
                    "pool": pool.address,
                    "fee": pool.fee,
                    "fee_pct": _fee_pct(pool.fee),
                    "base_token": base,
                    "quote_token": quote,
                    "tick_lower": tick_lower,
                    "tick_upper": tick_upper,
                    "min_price": low,
                    "max_price": high,
                    "full_range": is_full,
                    "current_price": current,
                    "in_range": in_range,
                    "token_a": token_a,
                    "token_b": token_b,
                    "amount_a": amount_a,
                    "amount_b": amount_b_base / 10**dec_b,
                    "amount_a_min": min_a / 10**dec_a,
                    "amount_b_min": min_b / 10**dec_b,
                    "native_value": deposit.native_value / 10**18,
                },
                nonce=nonce,
            )

        @tool
        def add_to_position(
            token_id: int,
            token_a: str,
            amount_a: float,
            from_address: str,
            use_native: bool = False,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that adds liquidity to an existing Uniswap V3 position through the
            position manager's increaseLiquidity: amount_a of token_a, and however much of the
            position's other token its range needs at today's price. The position keeps its range;
            it must belong to from_address. Refuses when the range, at today's price, needs only
            the other token (the message says which to deposit instead). The pool takes at most
            the amounts computed here, so each approval is reset afterwards when
            reset_residual_approvals is enabled (see the constructor).

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- an approval for each token that is pulled, then the
            increase, then the approval resets. With use_native the native-coin side is sent as
            value (no approval) in multicall([increaseLiquidity, refundETH]). Execute all of them,
            in order; if your account supports batching, execute them atomically in one
            transaction. In EOA mode `transactions` holds the same calls rendered as unsigned,
            signable transactions with sequential nonces already assigned; in calls mode it is
            None. `summary` holds the amounts in whole units and is safe to show the user. This
            tool never signs or broadcasts anything.

            Args:
                token_id: The position's NFT id.
                token_a: Contract address of the position's token to deposit (one of its two).
                amount_a: The amount of token_a to deposit, in whole units. The amount of the
                    other token follows from the position's range and today's price.
                from_address: The address that will sign this transaction, owns the position and
                    holds both tokens.
                use_native: Pay whichever of the position's tokens is the wrapped native token as
                    native coin (ETH, etc.) instead of as the wrapped token.
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%).
                    Applied downward to both amount minimums. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the increase reverts if not yet
                    mined. Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used; each call in the plan gets the next sequential nonce.
            """
            token_a = _address(token_a)
            position, _ = toolkit._owned_position(token_id, from_address)
            if token_a not in (position["token0"], position["token1"]):
                raise ToolException(
                    f"{token_a} is not one of position #{token_id}'s tokens "
                    f"({position['token0']}, {position['token1']})."
                )
            decimals = tuple(toolkit._decimals_of(position["token0"], position["token1"]))
            pool = toolkit._position_pool(position)
            tick_lower, tick_upper = position["tick_lower"], position["tick_upper"]
            deposit = toolkit._plan_deposit(
                pool, tick_lower, tick_upper, token_a, amount_a, decimals,
                use_native=use_native, slippage_bps=slippage_bps,
            )
            base, quote = orient(
                pool.token0, pool.token1, toolkit.native_wrapped_address, toolkit.stable_tokens
            )
            base_is_token0 = base == pool.token0
            low, high, is_full = prices_for_ticks(
                tick_lower, tick_upper, base_is_token0, decimals[0], decimals[1], pool.spacing
            )
            a_is_token0 = token_a == pool.token0
            token_b = pool.token1 if a_is_token0 else pool.token0
            amount_a_base, amount_b_base = (
                (deposit.amount0, deposit.amount1) if a_is_token0 else (deposit.amount1, deposit.amount0)
            )
            min_a, min_b = (deposit.min0, deposit.min1) if a_is_token0 else (deposit.min1, deposit.min0)
            dec_a, dec_b = (decimals[0], decimals[1]) if a_is_token0 else (decimals[1], decimals[0])
            native_note = "; the wrapped native token is paid as native coin" if use_native else ""
            description = (
                f"Add {_amount_str(amount_a_base, dec_a)} of {token_a} and "
                f"{_amount_str(amount_b_base, dec_b)} of {token_b} to Uniswap V3 position "
                f"#{token_id} ({pool.token0}/{pool.token1} {_fee_str(pool.fee)} pool)"
                f"{native_note}"
            )
            calls = toolkit._deposit_calls(
                pool, deposit, decimals,
                fn_name="increaseLiquidity",
                args=[
                    (
                        token_id, deposit.amount0, deposit.amount1, deposit.min0, deposit.min1,
                        toolkit._deadline(deadline_secs),
                    )
                ],
                description=description,
                from_address=from_address,
            )
            return toolkit._plan(
                calls,
                from_address=from_address,
                summary={
                    "token_id": token_id,
                    "token_a": token_a,
                    "token_b": token_b,
                    "amount_a": amount_a,
                    "amount_b": amount_b_base / 10**dec_b,
                    "amount_a_min": min_a / 10**dec_a,
                    "amount_b_min": min_b / 10**dec_b,
                    "native_value": deposit.native_value / 10**18,
                    "base_token": base,
                    "quote_token": quote,
                    "min_price": low,
                    "max_price": high,
                    "full_range": is_full,
                    "in_range": tick_lower <= pool.tick < tick_upper,
                },
                nonce=nonce,
            )

        @tool
        def withdraw_from_position(
            token_id: int,
            percent: float,
            from_address: str,
            recipient: str | None = None,
            native_out: bool = False,
            slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
            deadline_secs: int = DEFAULT_DEADLINE_SECS,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that takes percent of a Uniswap V3 position's liquidity out, together
            with all of the fees it has earned, in one position-manager multicall:
            decreaseLiquidity, then collect. At 100% it also burns the (now empty) position NFT.
            The position must belong to from_address. The amounts back are the share of the
            position's tokens at today's price plus its unclaimed fees; the slippage minimums
            guard the share only.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- here, just the one multicall. No approval is needed: the
            position's owner is calling its own position manager. With native_out the collected
            tokens go to the position manager first, then unwrapWETH9 pays the wrapped native
            token's share to the recipient as native coin and sweepToken pays the other token.
            In EOA mode `transactions` holds the same call rendered as an unsigned, signable
            transaction; in calls mode it is None. `summary` holds the amounts in whole units and
            is safe to show the user; the total that comes back is expected + fees. This tool
            never signs or broadcasts anything.

            Args:
                token_id: The position's NFT id.
                percent: The share of the position's liquidity to remove, above 0 and at most 100.
                    100 removes it all and burns the NFT.
                from_address: The address that will sign this transaction and owns the position.
                recipient: Optional address to receive the tokens. Defaults to from_address.
                native_out: Receive the wrapped native token's share as native coin (ETH, etc.).
                slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%).
                    Applied downward to both amount minimums. Defaults to 50 bps.
                deadline_secs: Seconds from now after which the withdrawal reverts if not yet
                    mined. Defaults to 600 (10 minutes).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used.
            """
            toolkit._check_slippage(slippage_bps)
            try:
                share = Fraction(str(percent))
            except (ValueError, ZeroDivisionError) as err:
                raise ToolException(f"{percent!r} is not a valid percent.") from err
            if not 0 < share <= 100:
                raise ToolException("percent must be above 0 and at most 100.")
            position, fees = toolkit._owned_position(token_id, from_address, with_fees=True)
            token0, token1 = position["token0"], position["token1"]
            liquidity = position["liquidity"]
            decimals = tuple(toolkit._decimals_of(token0, token1))
            pool = toolkit._position_pool(position)
            # Exactly all of it at 100% -- never "percent of liquidity" rounded -- so the position
            # is left with none and can be burned. Integer math: liquidity can exceed 2**128.
            removing = (
                liquidity if share == 100
                else liquidity * share.numerator // (100 * share.denominator)
            )
            burns = share == 100
            if removing == 0:
                if liquidity == 0 and not burns:
                    raise ToolException(
                        f"Position #{token_id} has no liquidity left. Use collect_fees to take "
                        f"its fees, or withdraw 100% to take them and burn the position."
                    )
                if liquidity > 0:
                    raise ToolException(
                        f"Position #{token_id} holds too little liquidity to remove {percent}% "
                        f"of it."
                    )
                if fees == (0, 0):
                    raise ToolException(f"Position #{token_id} is already empty: nothing to withdraw.")
            expected = v3math.amounts_for_liquidity(
                pool.sqrt_price,
                v3math.get_sqrt_ratio_at_tick(position["tick_lower"]),
                v3math.get_sqrt_ratio_at_tick(position["tick_upper"]),
                removing,
            )
            min0 = expected[0] * (BPS_DENOMINATOR - slippage_bps) // BPS_DENOMINATOR
            min1 = expected[1] * (BPS_DENOMINATOR - slippage_bps) // BPS_DENOMINATOR
            to = Web3.to_checksum_address(recipient or from_address)

            inner = []
            if removing > 0:
                inner.append(
                    toolkit._encode(
                        toolkit.position_manager,
                        "decreaseLiquidity",
                        [(token_id, removing, min0, min1, toolkit._deadline(deadline_secs))],
                    )
                )
            inner += toolkit._collect_calls(token_id, (token0, token1), to, native_out)
            if burns:
                inner.append(toolkit._encode(toolkit.position_manager, "burn", [token_id]))
            shown = f"{percent:g}" if isinstance(percent, float) else str(percent)
            burn_note = ", then burn the empty position" if burns else ""
            native_note = "; the wrapped native token comes back as native coin" if native_out else ""
            description = (
                f"Withdraw {shown}% of Uniswap V3 position #{token_id} "
                f"({token0}/{token1} {_fee_str(position['fee'])} pool): about "
                f"{_amount_str(expected[0], decimals[0])} of {token0} and "
                f"{_amount_str(expected[1], decimals[1])} of {token1}, plus unclaimed fees of "
                f"{_amount_str(fees[0], decimals[0])} of {token0} and "
                f"{_amount_str(fees[1], decimals[1])} of {token1}{burn_note}{native_note}"
            )
            action = toolkit._call(
                toolkit.position_manager,
                "multicall",
                [inner],
                role="remove_liquidity",
                description=description,
            )
            return toolkit._plan(
                [action],
                from_address=from_address,
                summary={
                    "token_id": token_id,
                    "percent": percent,
                    "token0": token0,
                    "token1": token1,
                    "expected0": expected[0] / 10 ** decimals[0],
                    "expected1": expected[1] / 10 ** decimals[1],
                    "amount0_min": min0 / 10 ** decimals[0],
                    "amount1_min": min1 / 10 ** decimals[1],
                    "fees0": fees[0] / 10 ** decimals[0],
                    "fees1": fees[1] / 10 ** decimals[1],
                    "burns": burns,
                },
                nonce=nonce,
            )

        @tool
        def collect_fees(
            token_id: int,
            from_address: str,
            recipient: str | None = None,
            native_out: bool = False,
            nonce: int | None = None,
        ) -> dict:
            """
            Builds a plan that collects the fees a Uniswap V3 position has earned, leaving its
            liquidity in place: a position-manager multicall of collect. The position must belong
            to from_address. Refuses when there is nothing to collect. Fees are what a position
            earns while the price is inside its range, so a position that is out of range earns
            none until the price comes back.

            Returns an execution plan: `calls` is the ordered list of contract calls this
            operation consists of -- here, just the one multicall. No approval is needed. With
            native_out the fees go to the position manager first, then unwrapWETH9 pays the
            wrapped native token's share to the recipient as native coin and sweepToken pays the
            other token. In EOA mode `transactions` holds the same call rendered as an unsigned,
            signable transaction; in calls mode it is None. `summary` holds the amounts in whole
            units and is safe to show the user. This tool never signs or broadcasts anything.

            Args:
                token_id: The position's NFT id.
                from_address: The address that will sign this transaction and owns the position.
                recipient: Optional address to receive the fees. Defaults to from_address.
                native_out: Receive the wrapped native token's share as native coin (ETH, etc.).
                nonce: Optional starting nonce for the plan (EOA mode only; ignored in calls
                    mode). If omitted, the current pending transaction count for from_address is
                    used.
            """
            position, fees = toolkit._owned_position(token_id, from_address, with_fees=True)
            token0, token1 = position["token0"], position["token1"]
            decimals = tuple(toolkit._decimals_of(token0, token1))
            if fees == (0, 0):
                raise ToolException(f"Position #{token_id} has no fees to collect.")
            to = Web3.to_checksum_address(recipient or from_address)
            native_note = "; the wrapped native token comes back as native coin" if native_out else ""
            description = (
                f"Collect {_amount_str(fees[0], decimals[0])} of {token0} and "
                f"{_amount_str(fees[1], decimals[1])} of {token1} in unclaimed fees from Uniswap "
                f"V3 position #{token_id} ({token0}/{token1} {_fee_str(position['fee'])} pool)"
                f"{native_note}"
            )
            action = toolkit._call(
                toolkit.position_manager,
                "multicall",
                [toolkit._collect_calls(token_id, (token0, token1), to, native_out)],
                role="collect",
                description=description,
            )
            return toolkit._plan(
                [action],
                from_address=from_address,
                summary={
                    "token_id": token_id,
                    "token0": token0,
                    "token1": token1,
                    "fees0": fees[0] / 10 ** decimals[0],
                    "fees1": fees[1] / 10 ** decimals[1],
                },
                nonce=nonce,
            )

        @tool
        def get_positions(
            owner_address: str, limit: int = 50, tokens: list[str] | None = None
        ) -> list[dict]:
            """
            Lists the Uniswap V3 positions an address owns: for each, its pool, its price range
            (quote per base, whole tokens), the current price, whether the price is inside the
            range (in_range -- only then does it earn fees), what it holds right now and the fees
            it has earned and not yet collected.

            Use this tool when the user wants to see their liquidity positions, or to find the
            id of one before adding to it, withdrawing from it or collecting its fees. Anyone can
            send anyone a position, so without `tokens` the rows include whatever the address
            holds; positions that cannot be read (a token that misbehaves) are left out.

            Args:
                owner_address: The address whose positions to list.
                limit: The most positions to return, in the order the position manager lists
                    them. Defaults to 50.
                tokens: Optional token addresses. If given, only positions whose two tokens are
                    both among them, and that still hold liquidity or fees, are returned -- and
                    `limit` counts only those, so positions someone else sent the address can't
                    crowd the real ones out. Up to the first 200 positions are looked through.

            Returns:
                A list of rows, each a dict with token_id, owner, token0, token1, decimals0,
                decimals1, fee, fee_pct, pool, tick_lower, tick_upper, tick, liquidity (str),
                base_token, quote_token, min_price, max_price (None for full range), full_range,
                current_price, in_range, amount0, amount1, amount0_raw (str), amount1_raw (str),
                fees0, fees1, fees0_raw (str), fees1_raw (str) and empty (no liquidity and no
                fees).
            """
            owner = _address(owner_address)
            if limit < 1:
                raise ToolException("limit must be at least 1.")
            wanted = None if tokens is None else {_address(t) for t in tokens}
            count = int(toolkit.position_manager.functions.balanceOf(owner).call())
            if count == 0:
                return []
            scanned = min(count, limit if wanted is None else max(limit, _MAX_POSITIONS_SCANNED))
            ids = [
                int(token_id)
                for token_id in _gather_strict(
                    [
                        lambda i=i: toolkit.position_manager.functions.tokenOfOwnerByIndex(owner, i).call()
                        for i in range(scanned)
                    ]
                )
            ]
            if wanted is None:
                return toolkit._position_rows({token_id: owner for token_id in ids}, strict=False)
            # The filter runs on positions() alone. With no liquidity a position earns nothing more,
            # so what it books (owed0/owed1) is exactly what collect would pay: "empty" is decided
            # here without the collect simulation.
            kept: dict[int, dict] = {}
            for token_id, position in zip(ids, _gather([lambda t=t: toolkit._read_position(t) for t in ids])):
                if isinstance(position, Exception):
                    if not isinstance(position, (ToolException, ContractLogicError, BadFunctionCallOutput)):
                        raise position
                    continue
                if position["token0"] not in wanted or position["token1"] not in wanted:
                    continue
                if position["liquidity"] == 0 and position["owed0"] == 0 and position["owed1"] == 0:
                    continue
                kept[token_id] = position
                if len(kept) == limit:
                    break
            return toolkit._position_rows(
                {token_id: owner for token_id in kept}, strict=False, read=kept
            )

        @tool
        def get_position(token_id: int, owner_address: str | None = None) -> dict:
            """
            Returns one Uniswap V3 position by its NFT id, as a row like get_positions': its pool,
            price range (quote per base, whole tokens), the current price, whether the price is
            inside the range, what it holds right now and the fees it has earned and not yet
            collected.

            Args:
                token_id: The position's NFT id.
                owner_address: Optional. If given, the position must belong to this address
                    (otherwise this raises); if omitted, the position's current owner is used.

            Returns:
                One row: a dict with token_id, owner, token0, token1, decimals0, decimals1, fee,
                fee_pct, pool, tick_lower, tick_upper, tick, liquidity (str), base_token,
                quote_token, min_price, max_price (None for full range), full_range,
                current_price, in_range, amount0, amount1, amount0_raw (str), amount1_raw (str),
                fees0, fees1, fees0_raw (str), fees1_raw (str) and empty (no liquidity and no
                fees).
            """
            owner = toolkit._read_owner_of(token_id)
            if owner_address is not None and owner != _address(owner_address):
                raise ToolException(
                    f"Position #{token_id} is owned by {owner}, not by {owner_address}."
                )
            rows = toolkit._position_rows({token_id: owner}, strict=True)
            if not rows:  # strict mode raises on every failure, so this is never reached
                raise ToolException(f"Position #{token_id} could not be read.")
            return rows[0]

        return [
            get_quote_in,
            get_quote_out,
            swap_exact_tokens_for_tokens,
            swap_tokens_for_exact_tokens,
            swap_exact_eth_for_tokens,
            swap_eth_for_exact_tokens,
            swap_exact_tokens_for_eth,
            swap_tokens_for_exact_eth,
            suggest_price_ranges,
            open_position,
            add_to_position,
            withdraw_from_position,
            collect_fees,
            get_positions,
            get_position,
        ]
