"""
Uniswap V3's price and liquidity math, ported from the Solidity libraries to exact Python integers.

  - getSqrtRatioAtTick is TickMath's, constant for constant, so a tick maps to the same sqrt price
    the pool itself uses. get_tick_at_sqrt_ratio is defined by it (the greatest tick whose sqrt
    price is at or below the input), found by bisection rather than TickMath's bit tricks: the same
    answer, and much easier to check.
  - The liquidity <-> amount functions are LiquidityAmounts' (periphery), and the rounded-up amount
    deltas SqrtPriceMath's (core): what a mint actually pulls for a given liquidity.

Prices for people are "quote per base" in whole tokens (e.g. USDC per ETH), converted to and from
the pool's own price -- token1 per token0, in base units -- with each token's decimals. Those
conversions go through floats: they only choose a tick, and every amount is then worked out from
that tick exactly.
"""

from __future__ import annotations

import math

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
Q96 = 1 << 96
MAX_UINT128 = (1 << 128) - 1
_MAX_UINT256 = (1 << 256) - 1

# TickMath.getSqrtRatioAtTick's factors: bit i of |tick| multiplies by 1/sqrt(1.0001)^(2^i), Q128.
_TICK_FACTORS = (
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
)


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """sqrt(1.0001^tick) * 2^96, exactly as TickMath computes it (rounded up from Q128.128)."""
    abs_tick = abs(tick)
    if abs_tick > MAX_TICK:
        raise ValueError(f"tick {tick} is outside [{MIN_TICK}, {MAX_TICK}]")
    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if abs_tick & 0x1 else 1 << 128
    for bit, factor in _TICK_FACTORS:
        if abs_tick & bit:
            ratio = (ratio * factor) >> 128
    if tick > 0:
        ratio = _MAX_UINT256 // ratio
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """The greatest tick whose sqrt price is <= sqrt_price_x96 -- TickMath.getTickAtSqrtRatio's
    definition, by bisection over get_sqrt_ratio_at_tick."""
    if not MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO:
        raise ValueError("sqrt price is outside the range a pool can hold")
    low, high = MIN_TICK, MAX_TICK
    while low < high:
        mid = (low + high + 1) // 2
        if get_sqrt_ratio_at_tick(mid) <= sqrt_price_x96:
            low = mid
        else:
            high = mid - 1
    return low


def _mul_div(a: int, b: int, denominator: int) -> int:
    return a * b // denominator


def _mul_div_up(a: int, b: int, denominator: int) -> int:
    return -(-(a * b) // denominator)


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


# ---- LiquidityAmounts (periphery) ----


def liquidity_for_amount0(sqrt_a: int, sqrt_b: int, amount0: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    intermediate = _mul_div(sqrt_a, sqrt_b, Q96)
    return _mul_div(amount0, intermediate, sqrt_b - sqrt_a)


def liquidity_for_amount1(sqrt_a: int, sqrt_b: int, amount1: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    return _mul_div(amount1, Q96, sqrt_b - sqrt_a)


def liquidity_for_amounts(sqrt_price: int, sqrt_a: int, sqrt_b: int, amount0: int, amount1: int) -> int:
    """The most liquidity amount0 and amount1 can both pay for -- what the position manager mints."""
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    if sqrt_price <= sqrt_a:
        return liquidity_for_amount0(sqrt_a, sqrt_b, amount0)
    if sqrt_price < sqrt_b:
        return min(
            liquidity_for_amount0(sqrt_price, sqrt_b, amount0),
            liquidity_for_amount1(sqrt_a, sqrt_price, amount1),
        )
    return liquidity_for_amount1(sqrt_a, sqrt_b, amount1)


def amount0_for_liquidity(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    return _mul_div(liquidity << 96, sqrt_b - sqrt_a, sqrt_b) // sqrt_a


def amount1_for_liquidity(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    return _mul_div(liquidity, sqrt_b - sqrt_a, Q96)


def amounts_for_liquidity(sqrt_price: int, sqrt_a: int, sqrt_b: int, liquidity: int) -> tuple[int, int]:
    """What `liquidity` holds at sqrt_price, rounded down -- about what withdrawing it returns."""
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    if sqrt_price <= sqrt_a:
        return amount0_for_liquidity(sqrt_a, sqrt_b, liquidity), 0
    if sqrt_price < sqrt_b:
        return (
            amount0_for_liquidity(sqrt_price, sqrt_b, liquidity),
            amount1_for_liquidity(sqrt_a, sqrt_price, liquidity),
        )
    return 0, amount1_for_liquidity(sqrt_a, sqrt_b, liquidity)


# ---- SqrtPriceMath (core): what a mint pulls, rounded up ----


def amount0_delta_up(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    return -(-_mul_div_up(liquidity << 96, sqrt_b - sqrt_a, sqrt_b) // sqrt_a)


def amount1_delta_up(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    return _mul_div_up(liquidity, sqrt_b - sqrt_a, Q96)


def mint_amounts(sqrt_price: int, sqrt_a: int, sqrt_b: int, liquidity: int) -> tuple[int, int]:
    """What minting `liquidity` at sqrt_price pulls of token0 and token1 (the pool rounds up)."""
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    if sqrt_price <= sqrt_a:
        return amount0_delta_up(sqrt_a, sqrt_b, liquidity), 0
    if sqrt_price < sqrt_b:
        return amount0_delta_up(sqrt_price, sqrt_b, liquidity), amount1_delta_up(sqrt_a, sqrt_price, liquidity)
    return 0, amount1_delta_up(sqrt_a, sqrt_b, liquidity)


# ---- ticks and prices ----


def full_range_ticks(tick_spacing: int) -> tuple[int, int]:
    """The widest usable range: MIN_TICK and MAX_TICK, each moved inward onto the spacing."""
    return -(MAX_TICK // tick_spacing) * tick_spacing, (MAX_TICK // tick_spacing) * tick_spacing


def floor_tick(tick: int, tick_spacing: int) -> int:
    return (tick // tick_spacing) * tick_spacing


def ceil_tick(tick: int, tick_spacing: int) -> int:
    return -((-tick) // tick_spacing) * tick_spacing


def raw_price_at_tick(tick: int) -> float:
    """token1 per token0, in base units, at `tick`."""
    return 1.0001**tick


def raw_price_from_sqrt(sqrt_price_x96: int) -> float:
    """token1 per token0, in base units, from a pool's sqrtPriceX96."""
    return (sqrt_price_x96 / Q96) ** 2


def tick_at_raw_price(raw_price: float) -> int:
    """The tick at or just below a token1-per-token0 price in base units, clamped to the valid range."""
    if raw_price <= 0:
        return MIN_TICK
    if raw_price == math.inf:  # raw_from_human returns inf for a zero quote-per-base price
        return MAX_TICK
    tick = math.floor(math.log(raw_price) / math.log(1.0001))
    return max(MIN_TICK, min(MAX_TICK, tick))


def human_price(raw_price: float, base_is_token0: bool, decimals0: int, decimals1: int) -> float:
    """
    A pool price (token1 per token0, base units) as quote per base in whole tokens.

    @param base_is_token0  Whether the token being priced is the pool's token0.
    """
    per_token0 = raw_price * 10 ** (decimals0 - decimals1)  # whole token1 per whole token0
    if base_is_token0:
        return per_token0
    return 1 / per_token0 if per_token0 else math.inf


def raw_from_human(price: float, base_is_token0: bool, decimals0: int, decimals1: int) -> float:
    """The inverse of human_price: quote per base in whole tokens, as token1 per token0 in base units."""
    if base_is_token0:
        return price * 10 ** (decimals1 - decimals0)
    if price <= 0:
        return math.inf
    return (1 / price) * 10 ** (decimals1 - decimals0)
