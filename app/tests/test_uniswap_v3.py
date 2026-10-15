"""
Offline checks for app/langchain_uniswap_v3, the Uniswap V3 toolkit: its math against Uniswap's own
Solidity, its address table against script/Constants.s.sol, the price-range and deposit helpers, and
the shape of every plan it builds (call order, approvals and their resets, the deadline multicall,
native coin in and out, burn at 100%).

No node and no network. The plans are built by the real toolkit against a FakeChain: a web3
provider that answers eth_call by decoding the calldata with the real ABIs and calling a Python
handler, so the reads (getPool, QuoterV2, slot0, positions, collect, ...) go through the same
encode/decode path as against a node, in the same parallel rounds. The calldata the toolkit writes
is decoded back with the ABIs and checked argument by argument.

Run: make uniswap-v3-test   (or: python app/tests/test_uniswap_v3.py)
"""
from checks import check, finish   # first: it puts app/ on sys.path for the imports below

import math                                    # noqa: E402
import re                                      # noqa: E402
import threading                               # noqa: E402
import time                                    # noqa: E402
from fractions import Fraction                 # noqa: E402
from pathlib import Path                       # noqa: E402
from unittest import mock                      # noqa: E402

from eth_abi import encode                     # noqa: E402
from eth_utils.abi import collapse_if_tuple    # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402
from web3 import Web3                          # noqa: E402
from web3.providers import JSONBaseProvider    # noqa: E402

from langchain_uniswap_v3 import DEFAULT_GAS, KNOWN_NETWORKS, UniswapV3Toolkit  # noqa: E402
from langchain_uniswap_v3 import toolkit as tk  # noqa: E402
from langchain_uniswap_v3 import v3math as m   # noqa: E402
from langchain_uniswap_v3.abis import (        # noqa: E402
    erc20_abi, factory_abi, pool_abi, position_manager_abi, quoter_abi, swap_router_abi,
)

REPO = Path(__file__).resolve().parent.parent.parent


# ── Uniswap's own numbers ─────────────────────────────────────────────────────
#
# One line per datum, printed by a forge script that imports Uniswap's TickMath, SqrtPriceMath and
# LiquidityAmounts straight from lib/ (v3-core and v3-periphery, 0.8 branches) and runs them on
# solc 0.8.33:
#   SQRT|tick|getSqrtRatioAtTick(tick)
#   TICKAT|sqrtPriceX96|getTickAtSqrtRatio(sqrtPriceX96)        (at a tick's own price and one wei either side)
#   LIQ|name|sqrtPrice|tickLower|tickUpper|amount0|amount1|liquidity|
#       getLiquidityForAmounts|getAmountsForLiquidity (2 values)|what a mint of `liquidity` pulls, rounded up (2 values)
# To refresh them, run the same library calls under `forge script` (read-only) and paste the lines.
SOLIDITY_REFERENCE = """
SQRT|0|79228162514264337593543950336
  TICKAT|79228162514264337593543950336|0
  TICKAT|79228162514264337593543950335|-1
  TICKAT|79228162514264337593543950337|0
  SQRT|1|79232123823359799118286999568
  TICKAT|79232123823359799118286999568|1
  TICKAT|79232123823359799118286999567|0
  TICKAT|79232123823359799118286999569|1
  SQRT|-1|79224201403219477170569942574
  TICKAT|79224201403219477170569942574|-1
  TICKAT|79224201403219477170569942573|-2
  TICKAT|79224201403219477170569942575|-1
  SQRT|2|79236085330515764027303304732
  TICKAT|79236085330515764027303304732|2
  TICKAT|79236085330515764027303304731|1
  TICKAT|79236085330515764027303304733|2
  SQRT|-2|79220240490215316061937756561
  TICKAT|79220240490215316061937756561|-2
  TICKAT|79220240490215316061937756560|-3
  TICKAT|79220240490215316061937756562|-2
  SQRT|3|79240047035742135098198828268
  TICKAT|79240047035742135098198828268|3
  TICKAT|79240047035742135098198828267|2
  TICKAT|79240047035742135098198828269|3
  SQRT|10|79267784519130042428790663799
  TICKAT|79267784519130042428790663799|10
  TICKAT|79267784519130042428790663798|9
  TICKAT|79267784519130042428790663800|10
  SQRT|-10|79188560314459151373725315960
  TICKAT|79188560314459151373725315960|-10
  TICKAT|79188560314459151373725315959|-11
  TICKAT|79188560314459151373725315961|-10
  SQRT|60|79466191966197645195421774833
  TICKAT|79466191966197645195421774833|60
  TICKAT|79466191966197645195421774832|59
  TICKAT|79466191966197645195421774834|60
  SQRT|-60|78990846045029531151608375686
  TICKAT|78990846045029531151608375686|-60
  TICKAT|78990846045029531151608375685|-61
  TICKAT|78990846045029531151608375687|-60
  SQRT|100|79625275426524748796330556128
  TICKAT|79625275426524748796330556128|100
  TICKAT|79625275426524748796330556127|99
  TICKAT|79625275426524748796330556129|100
  SQRT|-100|78833030112140176575862854579
  TICKAT|78833030112140176575862854579|-100
  TICKAT|78833030112140176575862854578|-101
  TICKAT|78833030112140176575862854580|-100
  SQRT|200|80024378775772204256025656563
  TICKAT|80024378775772204256025656563|200
  TICKAT|80024378775772204256025656562|199
  TICKAT|80024378775772204256025656564|200
  SQRT|1000|83290069058676223003182343270
  TICKAT|83290069058676223003182343270|1000
  TICKAT|83290069058676223003182343269|999
  TICKAT|83290069058676223003182343271|1000
  SQRT|-1000|75364347830767020784054125655
  TICKAT|75364347830767020784054125655|-1000
  TICKAT|75364347830767020784054125654|-1001
  TICKAT|75364347830767020784054125656|-1000
  SQRT|10000|130621891405341611593710811006
  TICKAT|130621891405341611593710811006|10000
  TICKAT|130621891405341611593710811005|9999
  TICKAT|130621891405341611593710811007|10000
  SQRT|-10000|48055510970269007215549348797
  TICKAT|48055510970269007215549348797|-10000
  TICKAT|48055510970269007215549348796|-10001
  TICKAT|48055510970269007215549348798|-10000
  SQRT|12345|146870458338965608271414022015
  TICKAT|146870458338965608271414022015|12345
  TICKAT|146870458338965608271414022014|12344
  TICKAT|146870458338965608271414022016|12345
  SQRT|-12345|42739035517269358503607398648
  TICKAT|42739035517269358503607398648|-12345
  TICKAT|42739035517269358503607398647|-12346
  TICKAT|42739035517269358503607398649|-12345
  SQRT|100000|11755562826496067164730007768450
  TICKAT|11755562826496067164730007768450|100000
  TICKAT|11755562826496067164730007768449|99999
  TICKAT|11755562826496067164730007768451|100000
  SQRT|-100000|533968626430936354154228408
  TICKAT|533968626430936354154228408|-100000
  TICKAT|533968626430936354154228407|-100001
  TICKAT|533968626430936354154228409|-100000
  SQRT|196238|1445175406321390753211676069269095
  TICKAT|1445175406321390753211676069269095|196238
  TICKAT|1445175406321390753211676069269094|196237
  TICKAT|1445175406321390753211676069269096|196238
  SQRT|-196238|4343487792505876584670170
  TICKAT|4343487792505876584670170|-196238
  TICKAT|4343487792505876584670169|-196239
  TICKAT|4343487792505876584670171|-196238
  SQRT|276324|79228057781537899283318961129827820
  TICKAT|79228057781537899283318961129827820|276324
  TICKAT|79228057781537899283318961129827819|276323
  TICKAT|79228057781537899283318961129827821|276324
  SQRT|-276324|79228267247129223624114
  TICKAT|79228267247129223624114|-276324
  TICKAT|79228267247129223624113|-276325
  TICKAT|79228267247129223624115|-276324
  SQRT|500000|5697689776495288729098254600827762987878
  TICKAT|5697689776495288729098254600827762987878|500000
  TICKAT|5697689776495288729098254600827762987877|499999
  TICKAT|5697689776495288729098254600827762987879|500000
  SQRT|-500000|1101692437043807371
  TICKAT|1101692437043807371|-500000
  TICKAT|1101692437043807370|-500001
  TICKAT|1101692437043807372|-500000
  SQRT|887271|1461373636630004318706518188784493106690254656249
  TICKAT|1461373636630004318706518188784493106690254656249|887271
  TICKAT|1461373636630004318706518188784493106690254656248|887270
  TICKAT|1461373636630004318706518188784493106690254656250|887271
  SQRT|887272|1461446703485210103287273052203988822378723970342
  TICKAT|1461446703485210103287273052203988822378723970341|887271
  SQRT|-887272|4295128739
  TICKAT|4295128739|-887272
  TICKAT|4295128740|-887272
  TICKAT|4295128739|-887272
  TICKAT|4295128740|-887272
  TICKAT|79228162514264337593543950335|-1
  TICKAT|79228162514264337593543950336|0
  TICKAT|79228162514264337593543950337|0
  TICKAT|4339505179874779672736325173|-58095
  TICKAT|25054144837504793750611689472|-23028
  TICKAT|1461446703485210103287273052203988822378723970341|887271
  LIQ|inrange_tick0|79228162514264337593543950336|-100|100|1000000000000000000|1000000000000000000|1000000000000000000|200510416479002803287|4987272070749096|4987272070749096|4987272070749097|4987272070749097
  LIQ|inrange_offtick|79231000000000000000000000000|-100|100|1234567890123456789|987654321098765432|500000000000000000|196623009546617610103|2475729624148750|2511543087902438|2475729624148751|2511543087902439
  LIQ|below|77272108795590369356373805297|-100|100|1000000000000000000|1000000000000000000|1000000000000000000|100004583272051203256|9999541693800299|0|9999541693800300|0
  LIQ|above|81233731461783161732293370115|-100|100|1000000000000000000|1000000000000000000|1000000000000000000|100004583272051203256|0|9999541693800299|0|9999541693800300
  LIQ|edge_lower|78833030112140176575862854579|-100|100|1000000000000000000|1000000000000000000|1000000000000000000|100004583272051203256|9999541693800299|0|9999541693800300|0
  LIQ|edge_upper|79625275426524748796330556128|-100|100|1000000000000000000|1000000000000000000|1000000000000000000|100004583272051203256|0|9999541693800299|0|9999541693800300
  LIQ|weth_usdc|4343500138184777819238060|-197000|-195500|1000000000000000000|3000000000|123456789012345678|1463769123497065|81571572592568459463|253025126088|81571572592568459464|253025126089
  LIQ|usdc_weth|1445175406321890753211676069269095|195000|196500|3000000000|1000000000000000000|98765432109876543|913401041238688|70464794536|108129318503883018984|70464794537|108129318503883018985
  LIQ|stable|79228162514264337593543950336|-50|50|1000000000|1000000000|4321000000000|400520207989|10788469380|10788469380|10788469381|10788469381
  LIQ|full60|4339505179874779672736325173|-887220|887220|1000000000000000000|3000000000000|7000000000000|54772255750516|127801930084538|383405790253|127801930084539|383405790254
"""


def test_math_against_solidity():
    print("v3math against Uniswap's Solidity")
    check("getSqrtRatioAtTick(0) is 2**96", m.get_sqrt_ratio_at_tick(0) == 2**96)
    check("MIN_TICK -> MIN_SQRT_RATIO", m.get_sqrt_ratio_at_tick(m.MIN_TICK) == m.MIN_SQRT_RATIO)
    check("MAX_TICK -> MAX_SQRT_RATIO", m.get_sqrt_ratio_at_tick(m.MAX_TICK) == m.MAX_SQRT_RATIO)
    check("getTickAtSqrtRatio(MIN_SQRT_RATIO) is MIN_TICK", m.get_tick_at_sqrt_ratio(m.MIN_SQRT_RATIO) == m.MIN_TICK)
    check("getTickAtSqrtRatio(MAX_SQRT_RATIO - 1) is MAX_TICK - 1",
          m.get_tick_at_sqrt_ratio(m.MAX_SQRT_RATIO - 1) == m.MAX_TICK - 1)
    for bad in (m.MIN_SQRT_RATIO - 1, m.MAX_SQRT_RATIO):
        try:
            m.get_tick_at_sqrt_ratio(bad)
            check(f"getTickAtSqrtRatio({bad}) is refused", False)
        except ValueError:
            check(f"getTickAtSqrtRatio({bad}) is refused", True)
    try:
        m.get_sqrt_ratio_at_tick(m.MAX_TICK + 1)
        check("a tick past MAX_TICK is refused", False)
    except ValueError:
        check("a tick past MAX_TICK is refused", True)

    ticks = (0, 1, -1, 7, -7, 60, -60, 12345, -12345, 196238, -196238, 500000, -500000, m.MAX_TICK, m.MIN_TICK)
    check("every tick's own price maps back to it",
          all(m.get_tick_at_sqrt_ratio(m.get_sqrt_ratio_at_tick(t)) == t for t in ticks if t != m.MAX_TICK))
    check("prices strictly between two ticks map to the lower one",
          all(m.get_tick_at_sqrt_ratio((m.get_sqrt_ratio_at_tick(t) + m.get_sqrt_ratio_at_tick(t + 1)) // 2) == t
              for t in (-500000, -12345, -1, 0, 1, 196238, 500000)))

    # The factor table itself, read back from the Solidity source.
    source = REPO / "lib" / "v3-core" / "contracts" / "libraries" / "TickMath.sol"
    if source.exists():
        text = source.read_text()
        pairs = [(int(b, 16), int(f, 16)) for b, f in
                 re.findall(r"if \(absTick & (0x[0-9a-fA-F]+) != 0\) ratio = \(ratio \* (0x[0-9a-fA-F]+)\) >> 128;", text)]
        first = int(re.search(r"absTick & 0x1 != 0\s*\?\s*(0x[0-9a-fA-F]+)", text).group(1), 16)
        check("TickMath's 19 factors are lib/v3-core's, constant for constant",
              len(pairs) == 19 and pairs == list(m._TICK_FACTORS))
        check("TickMath's first factor is lib/v3-core's", first == 0xFFFCB933BD6FAD37AA2D162D1A594001)
    else:
        print("  SKIP  lib/v3-core is not checked out: TickMath's factor table not compared")

    sqrt_ok = tick_ok = liq_ok = 0
    failed = []
    for line in SOLIDITY_REFERENCE.strip().splitlines():
        parts = line.strip().split("|")
        if parts[0] == "SQRT":
            sqrt_ok += m.get_sqrt_ratio_at_tick(int(parts[1])) == int(parts[2])
            if m.get_sqrt_ratio_at_tick(int(parts[1])) != int(parts[2]):
                failed.append(line)
        elif parts[0] == "TICKAT":
            tick_ok += m.get_tick_at_sqrt_ratio(int(parts[1])) == int(parts[2])
            if m.get_tick_at_sqrt_ratio(int(parts[1])) != int(parts[2]):
                failed.append(line)
        elif parts[0] == "LIQ":
            _, name, price, lo, hi, a0, a1, liq, l, x0, x1, m0, m1 = parts
            price, lo, hi, a0, a1, liq, l, x0, x1, m0, m1 = map(int, (price, lo, hi, a0, a1, liq, l, x0, x1, m0, m1))
            sa, sb = m.get_sqrt_ratio_at_tick(lo), m.get_sqrt_ratio_at_tick(hi)
            got = (m.liquidity_for_amounts(price, sa, sb, a0, a1), m.amounts_for_liquidity(price, sa, sb, liq),
                   m.mint_amounts(price, sa, sb, liq))
            if got == (l, (x0, x1), (m0, m1)):
                liq_ok += 1
            else:
                failed.append(f"{name}: {got}")
    counts = SOLIDITY_REFERENCE.count("SQRT|"), SOLIDITY_REFERENCE.count("TICKAT|"), SOLIDITY_REFERENCE.count("LIQ|")
    check(f"getSqrtRatioAtTick matches Solidity at {counts[0]} ticks", sqrt_ok == counts[0], str(failed[:3]))
    check(f"getTickAtSqrtRatio matches Solidity at {counts[1]} prices", tick_ok == counts[1], str(failed[:3]))
    check(f"LiquidityAmounts and SqrtPriceMath match Solidity in {counts[2]} scenarios", liq_ok == counts[2], str(failed[:3]))

    # Rounding direction, from Solidity's numbers for 1e18 liquidity in [-100, 100] at tick 0: a mint
    # pulls one wei more than a withdrawal of the same liquidity gives.
    sa, sb = m.get_sqrt_ratio_at_tick(-100), m.get_sqrt_ratio_at_tick(100)
    check("a mint rounds up and a withdrawal rounds down, each by Solidity's figure",
          m.mint_amounts(2**96, sa, sb, 10**18) == (4987272070749097, 4987272070749097)
          and m.amounts_for_liquidity(2**96, sa, sb, 10**18) == (4987272070749096, 4987272070749096))

    check("full_range_ticks(60) stays inside the valid ticks and on the spacing",
          m.full_range_ticks(60) == (-887220, 887220))
    check("full_range_ticks(1) is MIN_TICK..MAX_TICK", m.full_range_ticks(1) == (m.MIN_TICK, m.MAX_TICK))
    check("floor_tick / ceil_tick round toward -inf / +inf on negatives",
          m.floor_tick(-1, 10) == -10 and m.ceil_tick(-1, 10) == 0 and m.floor_tick(11, 10) == 10 and m.ceil_tick(11, 10) == 20
          and m.floor_tick(20, 10) == 20 and m.ceil_tick(20, 10) == 20)
    check("tick_at_raw_price of an infinite price is MAX_TICK (a zero base-token1 price)",
          m.tick_at_raw_price(math.inf) == m.MAX_TICK and m.tick_at_raw_price(0) == m.MIN_TICK)
    # 3000 USDC per ETH with ETH the base and token0 (WETH 18, USDC 6): raw 3000e6/1e18.
    raw = m.raw_from_human(3000.0, True, 18, 6)
    check("raw_from_human / human_price are inverses, both orientations",
          abs(m.human_price(raw, True, 18, 6) - 3000.0) < 1e-9
          and abs(m.human_price(m.raw_from_human(3000.0, False, 6, 18), False, 6, 18) - 3000.0) < 1e-9)


# ── Address table ─────────────────────────────────────────────────────────────


def test_address_table():
    print("KNOWN_NETWORKS against script/Constants.s.sol")
    constants = dict(re.findall(r"address constant (\w+)\s*=\s*(0x[0-9a-fA-F]{40});",
                                (REPO / "script" / "Constants.s.sol").read_text()))
    prefix = {1: "MNT", 11155111: "SPO", 42161: "ARB", 8453: "BASE"}
    check("the table covers exactly mainnet, Sepolia, Arbitrum and Base", set(KNOWN_NETWORKS) == set(prefix))
    for chain_id, network in KNOWN_NETWORKS.items():
        p = prefix[chain_id]
        for key, constant in (("swap_router", f"{p}_UNISWAP_V3_SWAP_ROUTER_02"),
                              ("quoter", f"{p}_UNISWAP_V3_QUOTER_V2"),
                              ("position_manager", f"{p}_UNISWAP_V3_POSITION_MANAGER"),
                              ("native_wrapped", f"{p}_WETH")):
            sol = constants.get(constant)
            check(f"chain {chain_id} {key} is {constant}",
                  sol is not None and Web3.to_checksum_address(sol) == network[key], f"{sol} vs {network[key]}")
            check(f"chain {chain_id} {key} is written in its EIP-55 checksum form",
                  Web3.is_checksum_address(network[key]))
    check("mainnet and Arbitrum share the router, quoter and position manager",
          all(KNOWN_NETWORKS[1][k] == KNOWN_NETWORKS[42161][k] for k in ("swap_router", "quoter", "position_manager")))


# ── Pure helpers: orientation, ticks, prices, deposits, range options ────────

WETH_ = "0x4200000000000000000000000000000000000006"
USDC_ = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DAI_ = "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb"
LINK_ = "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196"


def test_orientation():
    print("orient: which token is the quote")
    stable = frozenset({USDC_, DAI_})
    check("ETH/USDC: the stablecoin is the quote (USDC per ETH)", tk.orient(WETH_, USDC_, WETH_, stable) == (WETH_, USDC_))
    check("... whichever of the two is token0", tk.orient(USDC_, WETH_, WETH_, stable) == (WETH_, USDC_))
    check("LINK/WETH: no stablecoin, so WETH is the quote (WETH per LINK)", tk.orient(WETH_, LINK_, WETH_, stable) == (LINK_, WETH_))
    check("... whichever of the two is token0", tk.orient(LINK_, WETH_, WETH_, stable) == (LINK_, WETH_))
    check("two stablecoins: token0 is the base", tk.orient(DAI_, USDC_, WETH_, stable) == (DAI_, USDC_))
    check("neither a stablecoin nor WETH: token0 is the base", tk.orient(WETH_, LINK_, DAI_, frozenset()) == (WETH_, LINK_))
    check("a stablecoin beats WETH: WETH/USDC has USDC as quote even when listed the other way",
          tk.orient(USDC_, WETH_, WETH_, stable) == (WETH_, USDC_))
    check("with no stablecoin list, WETH is the quote (USDC per ... becomes WETH per USDC)",
          tk.orient(WETH_, USDC_, WETH_, frozenset()) == (USDC_, WETH_))


def test_ticks_and_prices():
    print("ticks_for_prices / prices_for_ticks")
    # WETH (token0, 18) / USDC (token1, 6) at spacing 10, base = WETH: USDC per ETH rises with the tick.
    lo, hi = tk.ticks_for_prices(2850.0, 3150.0, True, 18, 6, 10)
    check("ticks are on the spacing and ordered", lo % 10 == 0 and hi % 10 == 0 and lo < hi)
    low, high, full = tk.prices_for_ticks(lo, hi, True, 18, 6, 10)
    check("the range covers the prices asked for (lower rounded down, upper rounded up)", low <= 2850.0 and high >= 3150.0)
    check("... and is no wider than one spacing (0.1%) either side", low > 2850 * 0.998 and high < 3150 * 1.002, f"{low} {high}")
    check("not full range", full is False)

    # USDC (token0, 6) / WETH (token1, 18), base = WETH (token1): the price falls as the tick rises,
    # so the MIN price is the UPPER tick.
    lo2, hi2 = tk.ticks_for_prices(2850.0, 3150.0, False, 6, 18, 10)
    low2, high2, _ = tk.prices_for_ticks(lo2, hi2, False, 6, 18, 10)
    check("base = token1: the range still covers the prices asked for", low2 <= 2850.0 and high2 >= 3150.0, f"{low2} {high2}")
    check("base = token1: min_price comes from the upper tick",
          abs(low2 - m.human_price(m.raw_price_at_tick(hi2), False, 6, 18)) < 1e-9 * low2
          and abs(high2 - m.human_price(m.raw_price_at_tick(lo2), False, 6, 18)) < 1e-9 * high2)
    check("the two orientations describe mirror-image ticks (raw prices are reciprocals), to within a spacing",
          abs(lo2 + hi) <= 10 and abs(hi2 + lo) <= 10 and lo < 0 < hi2, f"{lo} {hi} {lo2} {hi2}")

    # A price range that falls inside one tick spacing is widened by one spacing, never left empty.
    a, b = tk.ticks_for_prices(3000.0, 3000.0001, True, 18, 6, 60)
    check("a range narrower than a spacing is widened to one", b - a >= 60 and a % 60 == 0 and b % 60 == 0)
    a, b = tk.ticks_for_prices(3000.0, 3000.0, True, 18, 6, 10)
    check("min_price == max_price still gives lower < upper", a < b)

    # Clamped to the ticks a pool can hold, and flagged as the full range.
    lo3, hi3 = tk.ticks_for_prices(1e-60, 1e60, True, 18, 6, 60)
    check("an absurdly wide range clamps to the full range for the spacing", (lo3, hi3) == m.full_range_ticks(60))
    check("prices_for_ticks calls that full range: min 0.0, no max",
          tk.prices_for_ticks(lo3, hi3, True, 18, 6, 60) == (0.0, None, True))
    check("full range is flagged for base = token1 too", tk.prices_for_ticks(lo3, hi3, False, 6, 18, 60) == (0.0, None, True))
    lo4, hi4 = tk.ticks_for_prices(1e-30, 1e60, False, 6, 18, 200)
    check("a (near-)zero and a huge price in base = token1 also clamp to the ends", lo4 == m.full_range_ticks(200)[0] and hi4 == m.full_range_ticks(200)[1])

    # The prices a range prints go back in and land on the same ticks, exactly and at six digits.
    check("every tick range survives price -> ticks -> price -> ticks",
          all(tk.ticks_for_prices(*tk.prices_for_ticks(a, a + w, base0, d0, d1, s)[:2], base0, d0, d1, s) == (a, a + w)
              for base0, d0, d1, s in ((True, 18, 6, 10), (False, 6, 18, 10), (True, 18, 6, 60), (False, 18, 18, 200), (True, 6, 6, 1))
              for a in (s * k for k in (-3000, -1234, -7, 0, 5, 800, 2500)) for w in (s, 5 * s, 200 * s)))
    rounded = lambda x: float(f"{x:.6g}")  # noqa: E731
    check("... even when the prices were printed to six significant digits",
          all(tk.ticks_for_prices(rounded(low_), rounded(high_), base0, d0, d1, s) == (a, a + w)
              for base0, d0, d1, s in ((True, 18, 6, 10), (False, 6, 18, 10), (True, 18, 6, 60))
              for a in (s * k for k in (-14000, -3000, 500)) for w in (50 * s, 400 * s)
              for low_, high_, _ in [tk.prices_for_ticks(a, a + w, base0, d0, d1, s)]))


def test_deposit_amounts():
    print("deposit_amounts")
    s_lo, s_hi = m.get_sqrt_ratio_at_tick(-1000), m.get_sqrt_ratio_at_tick(1000)
    price = m.get_sqrt_ratio_at_tick(37)
    liq, a0, a1 = tk.deposit_amounts(price, -1000, 1000, True, 10**18, token_a="A", token_b="B")
    check("in range: amount_a alone fixes the liquidity", liq == m.liquidity_for_amount0(price, s_hi, 10**18))
    check("in range: the amounts are what the pool pulls for that liquidity, rounded up",
          (a0, a1) == m.mint_amounts(price, s_lo, s_hi, liq) and a0 <= 10**18)
    liq1, b0, b1 = tk.deposit_amounts(price, -1000, 1000, False, 10**18, token_a="A", token_b="B")
    check("in range, amount_a is token1: liquidity from amount1, and token1 is not exceeded",
          liq1 == m.liquidity_for_amount1(s_lo, price, 10**18) and b1 <= 10**18)
    check("minting those amounts as desired yields the same liquidity (what the position manager will compute)",
          m.liquidity_for_amounts(price, s_lo, s_hi, a0, a1) == liq and m.liquidity_for_amounts(price, s_lo, s_hi, b0, b1) == liq1)

    # The range wholly above the price holds only token0: token0 in, token1 refused.
    above = m.get_sqrt_ratio_at_tick(-2000)
    liq, a0, a1 = tk.deposit_amounts(above, -1000, 1000, True, 10**18, token_a="A", token_b="B")
    check("price below the range, depositing token0: single-sided, amount1 is 0", a1 == 0 and a0 > 0 and liq > 0)
    try:
        tk.deposit_amounts(above, -1000, 1000, False, 10**18, token_a="TOKEN1", token_b="TOKEN0")
        check("price below the range, depositing token1: refused", False)
    except ToolException as err:
        check("price below the range, depositing token1: refused, naming the token to deposit instead",
              "TOKEN0" in str(err) and "below" in str(err) and "instead" in str(err), str(err))
    below = m.get_sqrt_ratio_at_tick(2000)
    liq, a0, a1 = tk.deposit_amounts(below, -1000, 1000, False, 10**18, token_a="A", token_b="B")
    check("price above the range, depositing token1: single-sided, amount0 is 0", a0 == 0 and a1 > 0)
    try:
        tk.deposit_amounts(below, -1000, 1000, True, 10**18, token_a="TOKEN0", token_b="TOKEN1")
        check("price above the range, depositing token0: refused", False)
    except ToolException as err:
        check("price above the range, depositing token0: refused, naming the token to deposit instead",
              "TOKEN1" in str(err) and "above" in str(err), str(err))
    try:
        # At tick -200000 one wei of token0 is worth a few hundredths of a unit of liquidity.
        tk.deposit_amounts(m.get_sqrt_ratio_at_tick(-200000), -200100, -199900, True, 1, token_a="A", token_b="B")
        check("a dust amount that buys no liquidity is refused", False)
    except ToolException as err:
        check("a dust amount that buys no liquidity is refused", "too small" in str(err), str(err))
    # The boundary: the price exactly at the lower tick is "below" (token0 only), as the pool treats it.
    edge = m.get_sqrt_ratio_at_tick(-1000)
    liq, a0, a1 = tk.deposit_amounts(edge, -1000, 1000, True, 10**18, token_a="A", token_b="B")
    check("price exactly at the lower tick: token0 only", a1 == 0 and a0 > 0)


def test_deposit_minimums():
    """The minimums must be achievable: the position manager, handed the desired amounts, has to pull at least them."""
    print("deposit minimums against the position manager's own arithmetic")
    import random
    w = World(8453, positions=False)
    toolkit = w.toolkit()
    dai, usdc = w.addr["DAI"], w.addr["USDC"]

    # A case seen against the LIVE Base position manager (USDC/DAI, token_a = USDC = token1, 10 USDC, slippage 0): the
    # position manager floors a product mid-sum and mints one unit of liquidity below the one the DAI amount was derived
    # from, so it pulls 2521 wei less DAI than that amount. A minimum set at the derived amount reverted on chain.
    stable = tk._Pool("0x" + "ab" * 20, dai, usdc, 100, 79171004539182755172116, -276339, 1, 0)
    dep = toolkit._plan_deposit(stable, -276389, -276288, usdc, 10.0, (18, 6), use_native=False, slippage_bps=0)
    check("live regression: the DAI amount offered is the one derived for 10 USDC", dep.amount0 == 9999035545929254029 and dep.amount1 == 10_000_000, str(dep))
    check("live regression: the minimums are what the real position manager pulls (9999035545929251508 DAI), not the derived amount",
          (dep.liquidity, dep.min0, dep.min1) == (3965406102060690, 9999035545929251508, 10_000_000), str(dep))
    check("mint_outcome is that position manager's own result",
          tk.mint_outcome(79171004539182755172116, -276389, -276288, 9999035545929254029, 10_000_000) == (3965406102060690, 9999035545929251508, 10_000_000))

    # Property: over many random pools and ranges, with slippage 0, what the position manager pulls always covers the minimums
    # and never exceeds what was offered -- in both token orders, in range and on either side of it.
    rnd = random.Random(2026)
    bad, tested = [], 0
    for _ in range(4000):
        d0, d1 = rnd.choice([(18, 6), (6, 18), (18, 18), (8, 18), (6, 6)])
        spacing = rnd.choice([1, 10, 60, 200])
        kmax = 887272 // spacing
        price_tick = rnd.randint(-250000, 250000)
        lo = spacing * rnd.randint(max(-kmax, price_tick // spacing - 300), min(kmax - 2, price_tick // spacing + 300))
        hi = lo + spacing * rnd.randint(1, 400)
        if hi > spacing * kmax:
            continue
        sqrt_price = m.get_sqrt_ratio_at_tick(price_tick) + rnd.randint(0, 10**6)
        if not m.MIN_SQRT_RATIO <= sqrt_price < m.MAX_SQRT_RATIO:
            continue
        pool = tk._Pool("0x" + "cd" * 20, w.addr["DAI"], w.addr["USDC"], 500, sqrt_price, price_tick, spacing, 0)
        a_is_token0 = rnd.random() < 0.5
        token_a = pool.token0 if a_is_token0 else pool.token1
        amount = float(f"{rnd.randint(100000, 999999)}e{rnd.randint(-8, 4)}")
        try:
            dep = toolkit._plan_deposit(pool, lo, hi, token_a, amount, (d0, d1), use_native=False, slippage_bps=rnd.choice([0, 0, 1, 50]))
        except ToolException:
            continue      # the range holds only the other token, or the amount buys nothing
        tested += 1
        liq, pulled0, pulled1 = tk.mint_outcome(sqrt_price, lo, hi, dep.amount0, dep.amount1)
        # And it buys all the liquidity token_a could: exactly what was derived for it when token_a is token0 (the same
        # formula), or a hair under it -- the position manager's mid-sum floor, parts in 10**17 -- when token_a is token1.
        derived = tk.deposit_amounts(sqrt_price, lo, hi, a_is_token0, dep.amount0 if a_is_token0 else dep.amount1,
                                     token_a=token_a, token_b="B")[0]
        if not (liq > 0 and dep.min0 <= pulled0 <= dep.amount0 and dep.min1 <= pulled1 <= dep.amount1
                and 0 <= derived - liq <= (0 if a_is_token0 else derived // 10**8 + 2)):
            bad.append((d0, d1, spacing, price_tick, lo, hi, a_is_token0, amount, dep, (liq, pulled0, pulled1)))
    check(f"{tested} random deposits: the position manager always pulls at least the minimums and at most the offer", not bad and tested > 2000, str(bad[:1]))


def test_range_options():
    print("range_options")
    for label, base0, d0, d1, spacing in (("WETH/USDC-like, base = token0", True, 18, 6, 10),
                                           ("USDC/WETH-like, base = token1", False, 6, 18, 10),
                                           ("same decimals, spacing 60", True, 18, 18, 60)):
        raw = m.raw_from_human(3000.0 if d0 != d1 else 15.0, base0, d0, d1)
        tick = m.tick_at_raw_price(raw) + 3
        sqrt_price = m.get_sqrt_ratio_at_tick(tick) + 12345
        current = m.human_price(m.raw_price_from_sqrt(sqrt_price), base0, d0, d1)
        options = tk.range_options(sqrt_price, spacing, base0, d0, d1, stable_pair=False)
        check(f"{label}: narrow, medium, wide, full", [o["name"] for o in options] == ["narrow", "medium", "wide", "full"])
        check(f"{label}: widths are 5, 15, 50 and None for full", [o["width_pct"] for o in options] == [5.0, 15.0, 50.0, None])
        check(f"{label}: only the last is full range", [o["full_range"] for o in options] == [False, False, False, True])
        check(f"{label}: ticks sit on the spacing and are ordered",
              all(o["tick_lower"] % spacing == 0 and o["tick_upper"] % spacing == 0 and o["tick_lower"] < o["tick_upper"] for o in options))
        banded = options[:3]
        check(f"{label}: min < current < max for every band", all(o["min_price"] < current < o["max_price"] for o in banded),
              str([(o["min_price"], o["max_price"], current) for o in banded]))
        check(f"{label}: bands widen from narrow to wide",
              banded[0]["min_price"] > banded[1]["min_price"] > banded[2]["min_price"]
              and banded[0]["max_price"] < banded[1]["max_price"] < banded[2]["max_price"])
        step = 1.0001 ** spacing   # one tick spacing, in price
        check(f"{label}: each band covers its +/- width around today's price, rounded outwards by less than one spacing",
              all(current * (1 - o["width_pct"] / 100) / step * (1 - 1e-9) < o["min_price"] <= current * (1 - o["width_pct"] / 100) * (1 + 1e-9)
                  and current * (1 + o["width_pct"] / 100) * (1 - 1e-9) <= o["max_price"] < current * (1 + o["width_pct"] / 100) * step * (1 + 1e-9)
                  for o in banded),
              str([(o["width_pct"], o["min_price"], o["max_price"]) for o in banded]))
        full = options[3]
        check(f"{label}: full range has min 0.0 and no max", full["min_price"] == 0.0 and full["max_price"] is None)
        check(f"{label}: full range is the widest the spacing allows",
              (full["tick_lower"], full["tick_upper"]) == m.full_range_ticks(spacing))
        check(f"{label}: shares add up to 100 and are in 0..100",
              all(abs(o["base_share_pct"] + o["quote_share_pct"] - 100) < 0.11 and 0 <= o["base_share_pct"] <= 100 for o in options))
        check(f"{label}: full range splits about 50/50 by value", abs(full["base_share_pct"] - 50) < 0.2, str(full["base_share_pct"]))
        check(f"{label}: a centred band splits about 50/50 too, never lopsided",
              all(35 < o["base_share_pct"] < 65 for o in banded), str([o["base_share_pct"] for o in banded]))

    stable = tk.range_options(m.get_sqrt_ratio_at_tick(5), 1, True, 6, 6, stable_pair=True)
    check("a stablecoin pair is offered 0.5 / 1 / 3 percent", [o["width_pct"] for o in stable] == [0.5, 1.0, 3.0, None])
    cur = m.human_price(m.raw_price_from_sqrt(m.get_sqrt_ratio_at_tick(5)), True, 6, 6)
    check("... and min < current < max for them", all(o["min_price"] < cur < o["max_price"] for o in stable[:3]))
    check("... the narrow band is within ~0.5% (spacing 1 is 0.01% a tick)",
          stable[0]["max_price"] / cur - 1 < 0.0052 and 1 - stable[0]["min_price"] / cur < 0.0052)

    # The suggested band's printed prices feed straight back into ticks_for_prices, same ticks.
    options = tk.range_options(m.get_sqrt_ratio_at_tick(-197000), 10, True, 18, 6, stable_pair=False)
    check("an option's min and max, passed back to ticks_for_prices, give that option's ticks",
          all(tk.ticks_for_prices(o["min_price"], o["max_price"], True, 18, 6, 10) == (o["tick_lower"], o["tick_upper"])
              for o in options[:3]))


def test_formatting():
    print("text helpers")
    check("_amount_str is exact and plain", tk._amount_str(10**16, 18) == "0.01" and tk._amount_str(2_980_123_456, 6) == "2980.123456"
          and tk._amount_str(5, 0) == "5" and tk._amount_str(10**18, 18) == "1" and tk._amount_str(1, 18) == "0.000000000000000001")
    check("_price_str keeps six significant digits, no exponent",
          tk._price_str(2987.123456) == "2987.12" and tk._price_str(0.000123456789) == "0.000123457"
          and tk._price_str(1e-7) == "0.0000001" and tk._price_str(1234567.0) == "1234570" and tk._price_str(0.0) == "0")
    check("_fee_str reads the fee tier as a percent",
          [tk._fee_str(f) for f in (100, 500, 3000, 10000)] == ["0.01%", "0.05%", "0.3%", "1%"])
    check("_to_base_units is exact for tiny and huge amounts",
          UniswapV3Toolkit._to_base_units(1e-07, 18) == 10**11 and UniswapV3Toolkit._to_base_units(0.1, 18) == 10**17
          and UniswapV3Toolkit._to_base_units(123456789012345678901.0, 0) == 123456789012345680000)
    try:
        UniswapV3Toolkit._to_base_units(float("nan"), 18)
        check("a nan amount is refused", False)
    except ToolException:
        check("a nan amount is refused", True)
    check("encode_path is token | fee (3 bytes) | token",
          tk.encode_path([USDC_, WETH_, DAI_], [500, 3000])
          == bytes.fromhex(USDC_[2:]) + (500).to_bytes(3, "big") + bytes.fromhex(WETH_[2:]) + (3000).to_bytes(3, "big") + bytes.fromhex(DAI_[2:])
          and len(tk.encode_path([USDC_, WETH_, DAI_], [500, 3000])) == 66)


# ── A fake chain: web3 provider that answers eth_call from Python handlers ───


class Revert(Exception):
    """What a handler raises for a call that reverts on chain."""


def _ok(result):
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def reverting(args, sender):
    """A handler for a call that always reverts."""
    raise Revert()


def raising(error):
    """A function that raises `error` whatever it is called with."""
    def fail(*args, **kwargs):
        raise error
    return fail


class FakeChain(JSONBaseProvider):
    """
    A web3 provider for tests. eth_call is decoded with the real ABI of the contract at `to`, handed
    to the handler registered for that function as (decoded_args, sender), and the result is
    ABI-encoded back, so the toolkit's reads run through the very encode/decode path they use
    against a node. `latency` makes each call take that long, so a test can see how many ran at
    once; `calls` logs every eth_call.
    """

    def __init__(self, chain_id: int, latency: float = 0.0):
        super().__init__()
        self.chain_id = chain_id
        self.latency = latency
        self.contracts: dict = {}
        self.handlers: dict = {}
        self.balances: dict = {}
        self.calls: list = []
        self.inflight = 0
        self.max_inflight = 0
        self.rpc_log: list = []
        self.estimate_gas = lambda tx: 120_000
        self.nonce = 7
        self._lock = threading.Lock()
        self._w3 = Web3()

    def add_contract(self, address: str, abi) -> None:
        address = Web3.to_checksum_address(address)
        self.contracts[address] = self._w3.eth.contract(address=address, abi=abi)

    def on(self, address: str, fn_name: str, handler) -> None:
        self.handlers[(Web3.to_checksum_address(address), fn_name)] = handler

    def count(self, fn_name: str) -> int:
        return sum(1 for _, name, _ in self.calls if name == fn_name)

    def make_request(self, method, params):
        with self._lock:
            self.rpc_log.append(method)
        if method == "web3_clientVersion":
            return _ok("fake-chain/1.0")
        if method == "eth_chainId":
            return _ok(hex(self.chain_id))
        if method == "eth_getBalance":
            return _ok(hex(self.balances.get(Web3.to_checksum_address(params[0]), 0)))
        if method == "eth_getCode":
            return _ok("0x60" if Web3.to_checksum_address(params[0]) in self.contracts else "0x")
        if method == "eth_getTransactionCount":
            return _ok(hex(self.nonce))
        if method == "eth_estimateGas":
            try:
                return _ok(hex(self.estimate_gas(params[0])))
            except Revert:
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted: STF", "data": "0x"}}
        if method == "eth_maxPriorityFeePerGas":
            return _ok(hex(10**9))
        if method == "eth_gasPrice":
            return _ok(hex(3 * 10**9))
        if method == "eth_getBlockByNumber":
            return _ok({"number": "0x10", "baseFeePerGas": hex(2 * 10**9), "timestamp": hex(1_800_000_000),
                        "hash": "0x" + "11" * 32, "parentHash": "0x" + "22" * 32, "transactions": []})
        if method == "eth_call":
            return self._eth_call(params[0])
        raise AssertionError(f"the toolkit made an RPC call this fake does not know: {method}")

    def _eth_call(self, tx):
        to = Web3.to_checksum_address(tx["to"])
        data = tx.get("data") or tx.get("input")
        contract = self.contracts.get(to)
        if contract is None:
            return _ok("0x")      # what a node answers for a call to an address with no code
        fn, args = contract.decode_function_input(data)
        handler = self.handlers.get((to, fn.fn_name))
        if handler is None:
            raise AssertionError(f"no handler for {fn.fn_name} on {to}")
        with self._lock:
            self.calls.append((to, fn.fn_name, args))
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.latency:
                time.sleep(self.latency)
            result = handler(args, tx.get("from"))
        except Revert:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted", "data": "0x"}}
        finally:
            with self._lock:
                self.inflight -= 1
        types = [collapse_if_tuple(o) for o in fn.abi["outputs"]]
        values = result if len(types) != 1 else (result,)
        return _ok("0x" + encode(types, list(values)).hex())


# ── A world on top of it: one chain's Uniswap V3, with pools, balances and positions ──

TOKENS = {
    8453: {"WETH": ("0x4200000000000000000000000000000000000006", 18),
           "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
           "DAI": ("0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", 18),
           "LINK": ("0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196", 18)},
    1: {"WETH": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),
        "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        "DAI": ("0x6B175474E89094C44Da98b954EedeAC495271d0F", 18),
        "LINK": ("0x514910771AF9Ca656af840dff83E8264EcF986CA", 18)},
}
# Tokens that exist on both, only here: one whose decimals() reverts, one with a pool that has no price.
BROKEN = Web3.to_checksum_address("0x000000000000000000000000000000000000b0b0")
UNPRICED = Web3.to_checksum_address("0x000000000000000000000000000000000000c0c0")
OTHER = Web3.to_checksum_address("0x00000000000000000000000000000000000a11ce")
WALLET = Web3.to_checksum_address("0x0000000000000000000000000000000000c0ffee")
RECIPIENT = Web3.to_checksum_address("0x00000000000000000000000000000000000dad00")
FACTORY = Web3.to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")


def _pool_address(token0: str, token1: str, fee: int) -> str:
    return Web3.to_checksum_address(Web3.keccak(text=f"pool|{token0}|{token1}|{fee}")[12:])


class World:
    """
    One chain's V3 as the fake chain serves it. Prices are human, token1 per token0 of the sorted
    pair; amounts are priced with Fractions, so every quote is exact and the tests can recompute
    what the toolkit should have derived from it.
    """

    def __init__(self, chain_id: int, latency: float = 0.0, positions: bool = True):
        self.chain_id = chain_id
        self.net = KNOWN_NETWORKS[chain_id]
        self.router, self.pm, self.quoter = self.net["swap_router"], self.net["position_manager"], self.net["quoter"]
        self.weth = self.net["native_wrapped"]
        self.addr = {name: a for name, (a, _) in TOKENS[chain_id].items()}
        self.decimals = {a: d for _, (a, d) in TOKENS[chain_id].items()}
        self.decimals[BROKEN] = None      # decimals() reverts
        self.decimals[UNPRICED] = 18
        self.stable = {self.addr["USDC"], self.addr["DAI"]}
        self.chain = FakeChain(chain_id, latency)
        # (token0, token1, fee) -> pool dict
        self.pools: dict = {}
        # (first token, second token, fee, price of the second per the first, liquidity, spacing, a cap on
        # how much of the FIRST token the pool can take or give in one swap -- past it the quote reverts)
        self._add_pool("WETH", "USDC", 500, price=3000.0, liquidity=5 * 10**17, spacing=10, cap=1000.0)
        self._add_pool("WETH", "USDC", 3000, price=3003.0, liquidity=10**17, spacing=60, cap=10.0)
        self._add_pool("LINK", "WETH", 3000, price=0.005, liquidity=2 * 10**17, spacing=60, cap=100000.0)
        self._add_pool("DAI", "USDC", 100, price=1.0001, liquidity=7 * 10**17, spacing=1, cap=10**7)
        self._add_pool("DAI", "WETH", 500, price=1 / 3000.0, liquidity=10**17, spacing=10, cap=10**7)
        self._add_pool_addr(BROKEN, self.weth, 3000, price=1.0, liquidity=10**15, spacing=60, cap=10**6)
        self._add_pool_addr(UNPRICED, self.weth, 500, price=1.0, liquidity=0, spacing=10, cap=10**6, initialized=False)
        self.positions: dict = {}
        self.balances = {
            self.addr["USDC"]: 1_000_000 * 10**6, self.addr["DAI"]: 1_000_000 * 10**18,
            self.addr["LINK"]: 100_000 * 10**18, self.weth: 1000 * 10**18,
        }
        self.chain.balances[WALLET] = 50 * 10**18
        self._register()
        if positions:
            self._add_positions()

    # -- pools --

    def _add_pool(self, name_a, name_b, fee, **kw):
        self._add_pool_addr(self.addr[name_a], self.addr[name_b], fee, **kw)

    def _add_pool_addr(self, a, b, fee, *, price, liquidity, spacing, cap, initialized=True):
        """price is the human price of b per a, as listed; the pool stores it per token0 (flipped if
        the sort puts b first). cap limits how much of a the pool can take or give."""
        token0, token1 = (a, b) if int(a, 16) < int(b, 16) else (b, a)
        if (token0, token1) != (a, b):
            price = 1 / price
        d0, d1 = self.decimals.get(token0) or 18, self.decimals.get(token1) or 18
        raw = price * 10 ** (d1 - d0)
        sqrt_price = int(math.sqrt(raw) * 2**96) if initialized else 0
        tick = int(math.floor(math.log(raw) / math.log(1.0001))) if initialized else 0
        self.pools[(token0, token1, fee)] = {
            "address": _pool_address(token0, token1, fee), "price": price, "liquidity": liquidity,
            "spacing": spacing, "cap_token": a, "cap": cap, "sqrt_price": sqrt_price, "tick": tick,
            "token0": token0, "token1": token1, "fee": fee,
        }

    def pool(self, a, b, fee):
        key = (a, b, fee) if int(a, 16) < int(b, 16) else (b, a, fee)
        return self.pools[key]

    # -- the amounts a quote works out (exact) --

    def _dec(self, token):
        d = self.decimals.get(token)
        if d is None:
            raise Revert()
        return d

    def _check_cap(self, pool, token_in, amount_in, amount_out):
        """Revert when the swap moves more of the pool's capped token than the pool can."""
        raw = amount_in if token_in == pool["cap_token"] else amount_out
        if Fraction(raw, 10 ** self._dec(pool["cap_token"])) > pool["cap"]:
            raise Revert()

    def quote_in(self, token_in, token_out, amount_in, fee):
        """QuoterV2.quoteExactInputSingle's amountOut, or Revert."""
        key = (token_in, token_out, fee) if int(token_in, 16) < int(token_out, 16) else (token_out, token_in, fee)
        pool = self.pools.get(key)
        if pool is None or pool["sqrt_price"] == 0:
            raise Revert()
        d0, d1 = self._dec(pool["token0"]), self._dec(pool["token1"])
        rate = Fraction(pool["price"]).limit_denominator(10**9) * Fraction(10) ** (d1 - d0)   # token1_raw per token0_raw
        keep = 1 - Fraction(fee, 10**6)
        out = int(amount_in * rate * keep) if token_in == pool["token0"] else int(amount_in / rate * keep)
        self._check_cap(pool, token_in, amount_in, out)
        if out <= 0:
            raise Revert()
        return out

    def quote_out(self, token_in, token_out, amount_out, fee):
        """QuoterV2.quoteExactOutputSingle's amountIn, or Revert."""
        key = (token_in, token_out, fee) if int(token_in, 16) < int(token_out, 16) else (token_out, token_in, fee)
        pool = self.pools.get(key)
        if pool is None or pool["sqrt_price"] == 0:
            raise Revert()
        d0, d1 = self._dec(pool["token0"]), self._dec(pool["token1"])
        rate = Fraction(pool["price"]).limit_denominator(10**9) * Fraction(10) ** (d1 - d0)
        keep = 1 - Fraction(fee, 10**6)
        if token_in == pool["token0"]:
            amount_in = math.ceil(Fraction(amount_out) / (rate * keep))
        else:
            amount_in = math.ceil(Fraction(amount_out) * rate / keep)
        self._check_cap(pool, token_in, amount_in, amount_out)
        return amount_in

    @staticmethod
    def _split_path(path: bytes):
        tokens, fees = [Web3.to_checksum_address(path[:20])], []
        i = 20
        while i < len(path):
            fees.append(int.from_bytes(path[i:i + 3], "big"))
            tokens.append(Web3.to_checksum_address(path[i + 3:i + 23]))
            i += 23
        return tokens, fees

    # -- wiring the fake chain --

    def _register(self):
        c = self.chain
        for address, abi in ((self.router, swap_router_abi), (self.pm, position_manager_abi),
                             (self.quoter, quoter_abi), (FACTORY, factory_abi)):
            c.add_contract(address, abi)
        for address in self.decimals:
            c.add_contract(address, erc20_abi)
        for pool in self.pools.values():
            c.add_contract(pool["address"], pool_abi)
        for contract in (self.router, self.pm, self.quoter):
            c.on(contract, "factory", lambda a, s: FACTORY)
            c.on(contract, "WETH9", lambda a, s: self.weth)
        c.on(FACTORY, "getPool", self._get_pool)
        for pool in self.pools.values():
            c.on(pool["address"], "slot0", lambda a, s, p=pool: (p["sqrt_price"], p["tick"], 0, 1, 1, 0, True))
            c.on(pool["address"], "liquidity", lambda a, s, p=pool: p["liquidity"])
            c.on(pool["address"], "tickSpacing", lambda a, s, p=pool: p["spacing"])
        for token in self.decimals:
            c.on(token, "decimals", lambda a, s, t=token: self._dec(t))
            c.on(token, "balanceOf", lambda a, s, t=token: self.balances.get(t, 0) if a["account"] == WALLET else 0)
        c.on(self.quoter, "quoteExactInputSingle", lambda a, s: self._q_in_single(a["params"]))
        c.on(self.quoter, "quoteExactOutputSingle", lambda a, s: self._q_out_single(a["params"]))
        c.on(self.quoter, "quoteExactInput", lambda a, s: self._q_in_path(a["path"], a["amountIn"]))
        c.on(self.quoter, "quoteExactOutput", lambda a, s: self._q_out_path(a["path"], a["amountOut"]))
        c.on(self.pm, "balanceOf", lambda a, s: sum(1 for p in self.positions.values() if p["owner"] == a["owner"]))
        c.on(self.pm, "tokenOfOwnerByIndex", self._token_of_owner)
        c.on(self.pm, "ownerOf", self._owner_of)
        c.on(self.pm, "positions", self._positions)
        c.on(self.pm, "collect", self._collect)

    def _get_pool(self, a, s):
        t0, t1 = (a["tokenA"], a["tokenB"]) if int(a["tokenA"], 16) < int(a["tokenB"], 16) else (a["tokenB"], a["tokenA"])
        pool = self.pools.get((t0, t1, a["fee"]))
        return pool["address"] if pool else "0x" + "00" * 20

    def _q_in_single(self, p):
        out = self.quote_in(p["tokenIn"], p["tokenOut"], p["amountIn"], p["fee"])
        return out, 2**96, 1, 90_000

    def _q_out_single(self, p):
        amount_in = self.quote_out(p["tokenIn"], p["tokenOut"], p["amount"], p["fee"])
        return amount_in, 2**96, 1, 90_000

    def _q_in_path(self, path, amount_in):
        tokens, fees = self._split_path(path)
        amount = amount_in
        for i, fee in enumerate(fees):
            amount = self.quote_in(tokens[i], tokens[i + 1], amount, fee)
        return amount, [2**96] * len(fees), [1] * len(fees), 200_000

    def _q_out_path(self, path, amount_out):
        tokens, fees = self._split_path(path)   # reversed: tokens[0] is the token received
        amount = amount_out
        for i, fee in enumerate(fees):
            amount = self.quote_out(tokens[i + 1], tokens[i], amount, fee)
        return amount, [2**96] * len(fees), [1] * len(fees), 200_000

    # -- positions --

    def _owner_of(self, a, s):
        position = self.positions.get(a["tokenId"])
        if position is None:
            raise Revert()
        return position["owner"]

    def _positions(self, a, s):
        p = self.positions.get(a["tokenId"])
        if p is None:
            raise Revert()
        return (0, "0x" + "00" * 20, p["token0"], p["token1"], p["fee"], p["tick_lower"], p["tick_upper"],
                p["liquidity"], 0, 0, p["owed0"], p["owed1"])

    def _token_of_owner(self, a, s):
        ids = [i for i, p in self.positions.items() if p["owner"] == a["owner"]]
        if a["index"] >= len(ids):
            raise Revert()
        return ids[a["index"]]

    def _collect(self, a, sender):
        p = self.positions.get(a["params"]["tokenId"])
        if p is None or sender is None or Web3.to_checksum_address(sender) != p["owner"]:
            raise Revert()      # "Not approved"
        return p["fees0"], p["fees1"]

    def _add_positions(self):
        weth, usdc = self.addr["WETH"], self.addr["USDC"]
        main = self.pool(weth, usdc, 500)
        mid = (main["tick"] // 10) * 10
        t0, t1 = (weth, usdc) if int(weth, 16) < int(usdc, 16) else (usdc, weth)
        full3000 = (-887220, 887220)
        link_pool = self.pool(self.addr["LINK"], weth, 3000)
        lt0, lt1 = link_pool["token0"], link_pool["token1"]
        lmid = (link_pool["tick"] // 60) * 60
        spam0, spam1 = sorted([BROKEN, weth], key=lambda token: int(token, 16))
        stable_pool = self.pool(self.addr["DAI"], self.addr["USDC"], 100)
        dai0, dai1, stable_tick = stable_pool["token0"], stable_pool["token1"], stable_pool["tick"]
        self.positions = {
            # in range, with fees
            101: dict(owner=WALLET, token0=t0, token1=t1, fee=500, tick_lower=mid - 1000, tick_upper=mid + 1000,
                      liquidity=123_456_789_012_345_678, owed0=0, owed1=0, fees0=1_500_000, fees1=2_000_000_000_000_000),
            # above the price: out of range
            102: dict(owner=WALLET, token0=t0, token1=t1, fee=500, tick_lower=mid + 500, tick_upper=mid + 1500,
                      liquidity=987_654_321_098_765, owed0=0, owed1=0, fees0=0, fees1=0),
            # full range at spacing 60
            103: dict(owner=WALLET, token0=t0, token1=t1, fee=3000, tick_lower=full3000[0], tick_upper=full3000[1],
                      liquidity=555_000_000_000_000, owed0=0, owed1=0, fees0=0, fees1=0),
            # nothing left at all
            104: dict(owner=WALLET, token0=t0, token1=t1, fee=500, tick_lower=mid - 100, tick_upper=mid + 100,
                      liquidity=0, owed0=0, owed1=0, fees0=0, fees1=0),
            # LINK/WETH: no stablecoin, so WETH is the quote and LINK the base
            106: dict(owner=WALLET, token0=lt0, token1=lt1, fee=3000, tick_lower=lmid - 600, tick_upper=lmid + 600,
                      liquidity=777_000_000_000_000_000, owed0=10**15, owed1=10**14, fees0=2 * 10**15, fees1=3 * 10**14),
            # liquidity gone but fees left to collect
            107: dict(owner=WALLET, token0=t0, token1=t1, fee=500, tick_lower=mid - 100, tick_upper=mid + 100,
                      liquidity=0, owed0=10**6, owed1=10**15, fees0=10**6, fees1=10**15),
            # spam: a token whose decimals() reverts
            105: dict(owner=WALLET, token0=spam0, token1=spam1, fee=3000, tick_lower=-600, tick_upper=600,
                      liquidity=10**12, owed0=0, owed1=0, fees0=0, fees1=0),
            # DAI/USDC, spacing 1: the price sits exactly on one end of the range
            108: dict(owner=WALLET, token0=dai0, token1=dai1, fee=100, tick_lower=stable_tick - 50, tick_upper=stable_tick,
                      liquidity=10**15, owed0=0, owed1=0, fees0=0, fees1=0),
            109: dict(owner=WALLET, token0=dai0, token1=dai1, fee=100, tick_lower=stable_tick, tick_upper=stable_tick + 50,
                      liquidity=10**15, owed0=0, owed1=0, fees0=0, fees1=0),
            # someone else's
            201: dict(owner=OTHER, token0=t0, token1=t1, fee=500, tick_lower=mid - 1000, tick_upper=mid + 1000,
                      liquidity=10**15, owed0=0, owed1=0, fees0=10, fees1=10),
        }

    # -- the toolkit --

    def toolkit(self, **kw) -> UniswapV3Toolkit:
        kw.setdefault("tx_mode", "calls")
        kw.setdefault("stable_tokens", sorted(self.stable))
        with mock.patch.object(Web3, "HTTPProvider", lambda uri: self.chain):
            return UniswapV3Toolkit.for_chain(self.chain_id, rpc_url="http://fake.invalid/secret-key", **kw)

    def tools(self, **kw) -> dict:
        return {t.name: t for t in self.toolkit(**kw).get_tools()}


# ── Decoding what the toolkit wrote ───────────────────────────────────────────

_W3 = Web3()
ROUTER_ABI = _W3.eth.contract(abi=swap_router_abi)
PM_ABI = _W3.eth.contract(abi=position_manager_abi)
ERC20_ABI = _W3.eth.contract(abi=erc20_abi)


def decode(contract, data):
    fn, args = contract.decode_function_input(data)
    return fn.fn_name, args


def decode_multicall(call, contract):
    """(outer args, [(function name, args), ...]) of a router or position-manager multicall."""
    name, outer = decode(contract, call["data"])
    assert name == "multicall", name
    return outer, [decode(contract, bytes(d)) for d in outer["data"]]


def roles(plan):
    return [c["role"] for c in plan["calls"]]


def approval(call):
    name, args = decode(ERC20_ABI, call["data"])
    assert name == "approve", name
    return Web3.to_checksum_address(call["to"]), args["spender"], args["amount"]


def raises(label, fn, contains=None):
    try:
        fn()
    except ToolException as err:
        check(label, contains is None or contains in str(err), f"message was: {err}")
    except Exception as err:  # noqa: BLE001
        check(label, False, f"raised {type(err).__name__}: {err}")
    else:
        check(label, False, "did not raise")


# ── The constructor ───────────────────────────────────────────────────────────


def test_constructor():
    print("constructor")
    w = World(8453, positions=False)
    toolkit = w.toolkit()
    check("it reads WETH9 off the router", toolkit.native_wrapped_address == w.weth)
    check("it reads the factory off the deployment", toolkit.factory.address == FACTORY)
    check("it reads the chain id", toolkit._chain_id == 8453)
    slow = World(8453, latency=0.02, positions=False)
    slow.toolkit()
    check("the constructor's reads run together (one round, not seven)", slow.chain.max_inflight >= 4, str(slow.chain.max_inflight))
    check("calls mode defaults to resetting residual approvals", toolkit.reset_residual_approvals is True)
    check("eoa mode defaults to not resetting them", w.toolkit(tx_mode="eoa").reset_residual_approvals is False)
    check("reset_residual_approvals can be set against the mode", w.toolkit(tx_mode="eoa", reset_residual_approvals=True).reset_residual_approvals is True)
    check("default_gas has every role the plans use",
          {"approve", "approve_reset", "swap", "add_liquidity", "remove_liquidity", "collect"} <= set(DEFAULT_GAS))
    check("stable_tokens are kept checksummed", toolkit.stable_tokens == frozenset(w.stable))

    # A deployment that disagrees with itself is refused.
    def build(w2, **kw):
        with mock.patch.object(Web3, "HTTPProvider", lambda uri: w2.chain):
            return UniswapV3Toolkit.for_chain(8453, rpc_url="http://fake.invalid/secret-key", **kw)

    other_factory = Web3.to_checksum_address("0x00000000000000000000000000000000000f4c70")
    w2 = World(8453, positions=False)
    w2.chain.on(w2.pm, "factory", lambda a, s: other_factory)
    try:
        build(w2)
        check("a position manager on another factory is refused", False)
    except ValueError as err:
        check("a position manager on another factory is refused, naming all three", "different V3 factories" in str(err) and other_factory in str(err), str(err))
    w3 = World(8453, positions=False)
    w3.chain.on(w3.quoter, "factory", lambda a, s: other_factory)
    try:
        build(w3)
        check("a quoter on another factory is refused", False)
    except ValueError:
        check("a quoter on another factory is refused", True)
    w4 = World(8453, positions=False)
    w4.chain.on(w4.pm, "WETH9", lambda a, s: USDC_)
    try:
        build(w4)
        check("a position manager with another WETH9 is refused", False)
    except ValueError as err:
        check("a position manager with another WETH9 is refused", "different WETH9" in str(err), str(err))
    try:
        build(World(8453, positions=False), native_wrapped_address=USDC_)
        check("a native_wrapped_address that is not the router's WETH9 is refused", False)
    except ValueError as err:
        check("a native_wrapped_address that is not the router's WETH9 is refused", "WETH9" in str(err), str(err))
    w5 = World(8453, positions=False)
    w5.chain.on(w5.router, "factory", reverting)
    try:
        build(w5)
        check("a router that reverts on factory() is refused", False)
    except ValueError as err:
        check("a router that reverts on factory() is refused with a clear message", "factory()" in str(err), str(err))
    class DeadChain(FakeChain):
        def make_request(self, method, params):
            raise ConnectionError("the node is down")

    dead = DeadChain(8453)
    try:
        with mock.patch.object(Web3, "HTTPProvider", lambda uri: dead):
            UniswapV3Toolkit.for_chain(8453, rpc_url="http://fake.invalid/secret-key")
        check("an unreachable RPC raises ConnectionError", False)
    except ConnectionError as err:
        check("an unreachable RPC raises ConnectionError, without leaking the URL", "secret-key" not in str(err) and "fake.invalid" not in str(err), str(err))
    try:
        UniswapV3Toolkit.for_chain(56, rpc_url="http://x")
        check("a chain with no V3 entry is refused", False)
    except ValueError as err:
        check("a chain with no V3 entry is refused", "56" in str(err), str(err))
    try:
        w.toolkit(tx_mode="bundled")
        check("an unknown tx_mode is refused", False)
    except ValueError:
        check("an unknown tx_mode is refused", True)
    with mock.patch.object(Web3, "HTTPProvider", lambda uri: w.chain):
        t = UniswapV3Toolkit.for_chain(8453, rpc_url="http://x", native_wrapped_address=w.weth, fee_tiers=(500,), tx_mode="calls")
    check("fee_tiers and overrides pass through for_chain", t.fee_tiers == (500,))


def test_tool_names():
    print("tool names")
    names = [t.name for t in World(8453, positions=False).toolkit().get_tools()]
    expected = ["get_quote_in", "get_quote_out", "swap_exact_tokens_for_tokens", "swap_tokens_for_exact_tokens",
                "swap_exact_eth_for_tokens", "swap_eth_for_exact_tokens", "swap_exact_tokens_for_eth",
                "swap_tokens_for_exact_eth", "suggest_price_ranges", "open_position", "add_to_position",
                "withdraw_from_position", "collect_fees", "get_positions", "get_position"]
    check("the toolkit offers exactly the contract's 15 tools", sorted(names) == sorted(expected), str(sorted(set(names) ^ set(expected))))
    check("... and no approve_token or is_*_sufficient", not any(n.startswith("is_") or n == "approve_token" for n in names))
    check("every tool has a description", all(t.description for t in World(8453, positions=False).toolkit().get_tools()))


def test_tool_signatures():
    print("tool arguments, in the contract's order and with its defaults")
    R = "REQUIRED"
    swap_tail = [("from_address", R), ("recipient", None), ("slippage_bps", 50), ("deadline_secs", 600), ("nonce", None)]
    expected = {
        "get_quote_in": [("token_in", R), ("token_out", R), ("amount_out", R)],
        "get_quote_out": [("token_in", R), ("token_out", R), ("amount_in", R)],
        "swap_exact_tokens_for_tokens": [("token_in", R), ("token_out", R), ("amount_in", R)] + swap_tail,
        "swap_tokens_for_exact_tokens": [("token_in", R), ("token_out", R), ("amount_out", R)] + swap_tail,
        "swap_exact_eth_for_tokens": [("token_out", R), ("amount_in", R)] + swap_tail,
        "swap_eth_for_exact_tokens": [("token_out", R), ("amount_out", R)] + swap_tail,
        "swap_exact_tokens_for_eth": [("token_in", R), ("amount_in", R)] + swap_tail,
        "swap_tokens_for_exact_eth": [("token_in", R), ("amount_out", R)] + swap_tail,
        "suggest_price_ranges": [("token_a", R), ("token_b", R), ("fee", None)],
        "open_position": [("token_a", R), ("token_b", R), ("amount_a", R), ("from_address", R), ("min_price", None), ("max_price", None),
                          ("full_range", False), ("fee", None), ("use_native", False), ("recipient", None), ("slippage_bps", 50),
                          ("deadline_secs", 600), ("nonce", None)],
        "add_to_position": [("token_id", R), ("token_a", R), ("amount_a", R), ("from_address", R), ("use_native", False), ("slippage_bps", 50),
                            ("deadline_secs", 600), ("nonce", None)],
        "withdraw_from_position": [("token_id", R), ("percent", R), ("from_address", R), ("recipient", None), ("native_out", False),
                                   ("slippage_bps", 50), ("deadline_secs", 600), ("nonce", None)],
        "collect_fees": [("token_id", R), ("from_address", R), ("recipient", None), ("native_out", False), ("nonce", None)],
        "get_positions": [("owner_address", R), ("limit", 50), ("tokens", None)],
        "get_position": [("token_id", R), ("owner_address", None)],
    }
    tools = {t.name: t for t in World(8453, positions=False).toolkit().get_tools()}
    for name, want in expected.items():
        fields = tools[name].args_schema.model_fields
        got = [(k, R if v.is_required() else v.default) for k, v in fields.items()]
        check(f"{name}: arguments and defaults", got == want, f"{got}")


# ── Quotes and routing ────────────────────────────────────────────────────────


def test_routing(chain_id: int):
    w = World(chain_id, positions=False)
    tools = w.tools()
    A = w.addr
    print(f"quotes and routing (chain {chain_id})")

    # Direct: WETH -> USDC, the 0.05% pool beats the 0.3% one.
    res = tools["get_quote_out"].invoke({"token_in": A["WETH"], "token_out": A["USDC"], "amount_in": 1.0})
    best = max(w.quote_in(A["WETH"], A["USDC"], 10**18, f) for f in (500, 3000))
    check("get_quote_out WETH->USDC: the best tier's amount, direct", res["amount_out"] == best / 10**6 and res["fees"] == [500]
          and res["path"] == [A["WETH"], A["USDC"]] and res["amount_in"] == 1.0, str(res))
    check("... and the pools that exist were looked up in one round, then quoted together",
          w.chain.count("getPool") >= 4 and w.chain.count("quoteExactInputSingle") == 2, f"{w.chain.count('getPool')} {w.chain.count('quoteExactInputSingle')}")
    calls_before = w.chain.count("getPool")
    tools["get_quote_out"].invoke({"token_in": A["WETH"], "token_out": A["USDC"], "amount_in": 1.0})
    check("a pool that exists is remembered: the second quote looks up only the pools that did not exist",
          w.chain.count("getPool") - calls_before < calls_before, f"{w.chain.count('getPool') - calls_before} vs {calls_before}")
    res = tools["get_quote_out"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 3000.0})
    best = max(w.quote_in(A["USDC"], A["WETH"], 3000 * 10**6, f) for f in (500, 3000))
    check("get_quote_out USDC->WETH", res["amount_out"] == best / 10**18 and res["fees"] == [500], str(res))

    # Exact output, direct.
    res = tools["get_quote_in"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 0.5})
    best = min(w.quote_out(A["USDC"], A["WETH"], 5 * 10**17, f) for f in (500, 3000))
    check("get_quote_in USDC->WETH: the cheapest tier", res["amount_in"] == best / 10**6 and res["fees"] == [500] and res["amount_out"] == 0.5, str(res))

    # A tier whose pool can't fill the swap is skipped, not fatal: 20 WETH is past the 0.3% pool's cap.
    res = tools["get_quote_out"].invoke({"token_in": A["WETH"], "token_out": A["USDC"], "amount_in": 20.0})
    check("a tier that reverts is skipped while another can fill", res["fees"] == [500] and res["amount_out"] > 0, str(res))
    raises("no route can fill 10 million WETH: a clear ToolException",
           lambda: tools["get_quote_out"].invoke({"token_in": A["WETH"], "token_out": A["USDC"], "amount_in": 1e7}),
           "No Uniswap V3 liquidity path")

    # A hop: LINK -> USDC has no pool of its own, so it goes through WETH.
    res = tools["get_quote_out"].invoke({"token_in": A["LINK"], "token_out": A["USDC"], "amount_in": 100.0})
    weth_out = w.quote_in(A["LINK"], A["WETH"], 100 * 10**18, 3000)
    leg2 = max((500, 3000), key=lambda fee: w.quote_in(A["WETH"], A["USDC"], weth_out, fee))
    expected = w.quote_in(A["WETH"], A["USDC"], weth_out, leg2)
    check("a pair with no pool of its own routes through WETH, taking the best tier of each leg",
          res["path"] == [A["LINK"], w.weth, A["USDC"]] and res["fees"] == [3000, leg2] and res["amount_out"] == expected / 10**6, str(res))
    rounds = [name for _, name, _ in w.chain.calls if name in ("quoteExactInputSingle", "quoteExactInput")]
    check("... leg 1 was quoted alone (one pool), then the full path per second-leg tier",
          rounds.count("quoteExactInput") == 2, str(rounds))
    res = tools["get_quote_in"].invoke({"token_in": A["LINK"], "token_out": A["USDC"], "amount_out": 1500.0})
    check("exact output through WETH: the same two pools, in swap order", res["path"] == [A["LINK"], w.weth, A["USDC"]] and len(res["fees"]) == 2, str(res))
    paths = [bytes(a["path"]) for _, name, a in w.chain.calls if name == "quoteExactOutput"]
    check("... and the path it quoted is read backwards, token_out first",
          paths and all(p[:20] == bytes.fromhex(A["USDC"][2:]) and p[-20:] == bytes.fromhex(A["LINK"][2:]) for p in paths), str(paths[:1]))

    # DAI -> USDC has a direct pool (0.01%) and a hop through WETH: the direct one is far better here.
    res = tools["get_quote_out"].invoke({"token_in": A["DAI"], "token_out": A["USDC"], "amount_in": 1000.0})
    check("a direct pool that fills better than the hop is used", res["path"] == [A["DAI"], A["USDC"]] and res["fees"] == [100], str(res))

    # A tie goes to the direct pool: one pool instead of two is less gas and less to move before the swap lands.
    t = World(chain_id, positions=False).toolkit()
    label = dict(label_in="DAI", label_out="USDC")
    t._quote_in_single = lambda token_in, token_out, amount_in, fee: 1000
    t._quote_in_path = lambda path, amount_in: 1000
    route = t._route_out(10**18, A["DAI"], A["USDC"], **label)
    check("exact in, a tie: the direct pool wins", route.path == [A["DAI"], A["USDC"]] and route.fees == [100] and route.amount == 1000, str(route))
    t._quote_in_path = lambda path, amount_in: 1001
    route = t._route_out(10**18, A["DAI"], A["USDC"], **label)
    check("exact in, a hop better by one unit wins", route.path == [A["DAI"], w.weth, A["USDC"]] and route.amount == 1001, str(route))
    t._quote_out_single = lambda token_in, token_out, amount_out, fee: 1000
    t._quote_out_path = lambda path, amount_out: 1000
    route = t._route_in(10**18, A["DAI"], A["USDC"], **label)
    check("exact out, a tie: the direct pool wins", route.path == [A["DAI"], A["USDC"]] and route.fees == [100] and route.amount == 1000, str(route))
    t._quote_out_path = lambda path, amount_out: 999
    route = t._route_in(10**18, A["DAI"], A["USDC"], **label)
    check("exact out, a hop cheaper by one unit wins", route.path == [A["DAI"], w.weth, A["USDC"]] and route.amount == 999, str(route))
    t._quote_in_single = raising(ConnectionError("down"))
    try:
        t._route_out(10**18, A["DAI"], A["USDC"], **label)
        check("a failure that is not a revert is not swallowed as an unroutable pool", False)
    except ConnectionError:
        check("a failure that is not a revert is not swallowed as an unroutable pool", True)

    # The same-token and bad-amount refusals.
    raises("the same token in and out is refused", lambda: tools["get_quote_out"].invoke({"token_in": A["USDC"], "token_out": A["USDC"], "amount_in": 1.0}), "same token")
    raises("a zero amount is refused", lambda: tools["get_quote_out"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 0.0}), "greater than zero")
    raises("a malformed address is refused", lambda: tools["get_quote_out"].invoke({"token_in": "0x123", "token_out": A["WETH"], "amount_in": 1.0}), "not a valid address")
    raises("a token with no pool at all is refused", lambda: tools["get_quote_out"].invoke({"token_in": UNPRICED, "token_out": A["USDC"], "amount_in": 1.0}), "No Uniswap V3 liquidity path")
    raises("an address that is not a token is refused", lambda: tools["get_quote_out"].invoke({"token_in": OTHER, "token_out": A["USDC"], "amount_in": 1.0}), "does not look like an ERC-20")

    # Parallelism: with a slow node the independent quotes still overlap.
    slow = World(chain_id, latency=0.02, positions=False)
    slow.tools()["get_quote_out"].invoke({"token_in": A["LINK"], "token_out": A["USDC"], "amount_in": 100.0})
    check("independent reads run at the same time (more than one eth_call in flight)", slow.chain.max_inflight >= 3, str(slow.chain.max_inflight))


# ── Swaps ─────────────────────────────────────────────────────────────────────


def _deadline_ok(deadline, secs=600):
    return time.time() <= deadline <= time.time() + secs + 5 and deadline >= time.time() + secs - 30


def test_swaps(chain_id: int):
    w = World(chain_id, positions=False)
    tools = w.tools()
    A = w.addr
    print(f"swap plans (chain {chain_id})")
    router = w.router

    # -- token in, exact input, one hop --
    plan = tools["swap_exact_tokens_for_tokens"].invoke(
        {"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 3000.0, "from_address": WALLET})
    out = max(w.quote_in(A["USDC"], A["WETH"], 3000 * 10**6, f) for f in (500, 3000))
    minimum = out * 9950 // 10000
    check("exact in: [approve, swap] with no reset", roles(plan) == ["approve", "swap"], str(roles(plan)))
    check("exact in: transactions is None in calls mode, and the chain id is set", plan["transactions"] is None and plan["chain_id"] == chain_id)
    token, spender, amount = approval(plan["calls"][0])
    check("exact in: approves the router for exactly amount_in", (token, spender, amount) == (A["USDC"], router, 3000 * 10**6))
    swap = plan["calls"][1]
    outer, inner = decode_multicall(swap, ROUTER_ABI)
    check("exact in: the swap goes to SwapRouter02 as multicall(deadline, ...) with no value",
          swap["to"] == router and swap["value"] == 0 and _deadline_ok(outer["deadline"]), str(outer["deadline"]))
    check("exact in: one inner call, exactInputSingle", [n for n, _ in inner] == ["exactInputSingle"])
    p = inner[0][1]["params"]
    check("exact in: the params are token pair, fee, recipient, amount, minimum, no price limit",
          (p["tokenIn"], p["tokenOut"], p["fee"], p["recipient"], p["amountIn"], p["amountOutMinimum"], p["sqrtPriceLimitX96"])
          == (A["USDC"], A["WETH"], 500, WALLET, 3000 * 10**6, minimum, 0), str(p))
    check("exact in: the summary has exactly the contract's keys",
          list(plan["summary"]) == ["token_in", "token_out", "amount_in", "amount_out_min", "path", "fees"], str(list(plan["summary"])))
    check("exact in: the summary's figures", plan["summary"]["amount_in"] == 3000.0 and plan["summary"]["amount_out_min"] == minimum / 10**18
          and plan["summary"]["path"] == [A["USDC"], A["WETH"]] and plan["summary"]["fees"] == [500])
    check("exact in: the action line names the amounts, the tokens and the pool",
          all(s in swap["description"] for s in ("Swap 3000 of", A["USDC"], A["WETH"], "0.05% pool")), swap["description"])
    check("exact in: every call is tagged with a role and a description", all(c["role"] and c["description"] for c in plan["calls"]))

    # recipient and slippage
    plan = tools["swap_exact_tokens_for_tokens"].invoke(
        {"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 3000.0, "from_address": WALLET,
         "recipient": RECIPIENT, "slippage_bps": 100, "deadline_secs": 60})
    outer, inner = decode_multicall(plan["calls"][1], ROUTER_ABI)
    p = inner[0][1]["params"]
    check("a recipient and slippage are honoured", p["recipient"] == RECIPIENT and p["amountOutMinimum"] == out * 9900 // 10000, str(p))
    check("deadline_secs sets the multicall's deadline", _deadline_ok(outer["deadline"], 60), str(outer["deadline"]))

    # -- token in, exact output --
    plan = tools["swap_tokens_for_exact_tokens"].invoke(
        {"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 0.5, "from_address": WALLET})
    cost = min(w.quote_out(A["USDC"], A["WETH"], 5 * 10**17, f) for f in (500, 3000))
    maximum = cost * 10050 // 10000
    check("exact out: [approve, swap, approve 0]", roles(plan) == ["approve", "swap", "approve_reset"], str(roles(plan)))
    check("exact out: approves amount_in_max, then resets to 0",
          approval(plan["calls"][0])[1:] == (router, maximum) and approval(plan["calls"][2])[1:] == (router, 0))
    outer, inner = decode_multicall(plan["calls"][1], ROUTER_ABI)
    p = inner[0][1]["params"]
    check("exact out: exactOutputSingle with amountOut and amountInMaximum",
          inner[0][0] == "exactOutputSingle" and (p["tokenIn"], p["tokenOut"], p["fee"], p["recipient"], p["amountOut"], p["amountInMaximum"])
          == (A["USDC"], A["WETH"], 500, WALLET, 5 * 10**17, maximum), str(p))
    check("exact out: carries the deadline and no value", _deadline_ok(outer["deadline"]) and plan["calls"][1]["value"] == 0)
    check("exact out: summary keys", list(plan["summary"]) == ["token_in", "token_out", "amount_out", "amount_in_max", "path", "fees"])
    check("exact out: summary figures", plan["summary"]["amount_out"] == 0.5 and plan["summary"]["amount_in_max"] == maximum / 10**6)
    plain = World(chain_id, positions=False).tools(reset_residual_approvals=False)
    plan = plain["swap_tokens_for_exact_tokens"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 0.5, "from_address": WALLET})
    check("exact out with reset_residual_approvals off: no trailing approve", roles(plan) == ["approve", "swap"])

    # -- hops --
    plan = tools["swap_exact_tokens_for_tokens"].invoke(
        {"token_in": A["LINK"], "token_out": A["USDC"], "amount_in": 100.0, "from_address": WALLET})
    outer, inner = decode_multicall(plan["calls"][1], ROUTER_ABI)
    name, args = inner[0]
    tokens, fees = World._split_path(bytes(args["params"]["path"]))
    check("exact in through WETH: exactInput with the path LINK | fee | WETH | fee | USDC",
          name == "exactInput" and tokens == [A["LINK"], w.weth, A["USDC"]] and fees == plan["summary"]["fees"] and fees[0] == 3000, str((name, tokens, fees)))
    weth_leg = w.quote_in(A["LINK"], w.weth, 100 * 10**18, fees[0])
    hop_out = w.quote_in(w.weth, A["USDC"], weth_leg, fees[1])
    check("... recipient, amount in and minimum (0.5% under the whole path's quote)",
          args["params"]["recipient"] == WALLET and args["params"]["amountIn"] == 100 * 10**18
          and args["params"]["amountOutMinimum"] == hop_out * 9950 // 10000, str(args["params"]))
    check("... the description says so", "through the wrapped native token" in plan["calls"][1]["description"], plan["calls"][1]["description"])
    plan = tools["swap_tokens_for_exact_tokens"].invoke(
        {"token_in": A["LINK"], "token_out": A["USDC"], "amount_out": 1500.0, "from_address": WALLET})
    outer, inner = decode_multicall(plan["calls"][1], ROUTER_ABI)
    name, args = inner[0]
    tokens, fees = World._split_path(bytes(args["params"]["path"]))
    check("exact out through WETH: exactOutput with the path read backwards (USDC | fee | WETH | fee | LINK)",
          name == "exactOutput" and tokens == [A["USDC"], w.weth, A["LINK"]] and fees == plan["summary"]["fees"][::-1], str((name, tokens, fees)))
    check("... the summary's path and fees stay in swap order",
          plan["summary"]["path"] == [A["LINK"], w.weth, A["USDC"]] and roles(plan) == ["approve", "swap", "approve_reset"])

    # -- native in --
    plan = tools["swap_exact_eth_for_tokens"].invoke({"token_out": A["USDC"], "amount_in": 0.25, "from_address": WALLET})
    out = max(w.quote_in(w.weth, A["USDC"], 25 * 10**16, f) for f in (500, 3000))
    check("native in, exact in: one call, no approval", roles(plan) == ["swap"] and len(plan["calls"]) == 1, str(roles(plan)))
    swap = plan["calls"][0]
    outer, inner = decode_multicall(swap, ROUTER_ABI)
    p = inner[0][1]["params"]
    check("native in, exact in: the amount rides as value and the deadline is in the multicall",
          swap["value"] == 25 * 10**16 and _deadline_ok(outer["deadline"]))
    check("native in, exact in: tokenIn is WETH9, no refundETH", [n for n, _ in inner] == ["exactInputSingle"] and p["tokenIn"] == w.weth
          and p["amountIn"] == 25 * 10**16 and p["amountOutMinimum"] == out * 9950 // 10000 and p["recipient"] == WALLET)
    check("native in, exact in: summary keys", list(plan["summary"]) == ["token_out", "amount_in", "amount_out_min", "path", "fees"])
    check("native in, exact in: the action line speaks of the native asset", "native asset" in swap["description"], swap["description"])
    plan = tools["swap_eth_for_exact_tokens"].invoke({"token_out": A["USDC"], "amount_out": 750.0, "from_address": WALLET})
    cost = min(w.quote_out(w.weth, A["USDC"], 750 * 10**6, f) for f in (500, 3000))
    maximum = cost * 10050 // 10000
    outer, inner = decode_multicall(plan["calls"][0], ROUTER_ABI)
    check("native in, exact out: one call with value = amountInMax", roles(plan) == ["swap"] and plan["calls"][0]["value"] == maximum, str(plan["calls"][0]["value"]))
    check("native in, exact out: [exactOutputSingle, refundETH] so the unused value comes back",
          [n for n, _ in inner] == ["exactOutputSingle", "refundETH"] and inner[0][1]["params"]["tokenIn"] == w.weth
          and inner[0][1]["params"]["amountInMaximum"] == maximum and inner[0][1]["params"]["recipient"] == WALLET)
    check("native in, exact out: summary keys", list(plan["summary"]) == ["token_out", "amount_out", "amount_in_max", "path", "fees"])
    plan = tools["swap_exact_eth_for_tokens"].invoke({"token_out": A["LINK"], "amount_in": 1.0, "from_address": WALLET})
    outer, inner = decode_multicall(plan["calls"][0], ROUTER_ABI)
    check("native in through a pool that exists only for WETH: single hop", inner[0][0] == "exactInputSingle" and plan["summary"]["fees"] == [3000])

    # -- native out --
    plan = tools["swap_exact_tokens_for_eth"].invoke({"token_in": A["USDC"], "amount_in": 3000.0, "from_address": WALLET, "recipient": RECIPIENT})
    out = max(w.quote_in(A["USDC"], w.weth, 3000 * 10**6, f) for f in (500, 3000))
    minimum = out * 9950 // 10000
    check("native out, exact in: [approve, swap], no reset", roles(plan) == ["approve", "swap"], str(roles(plan)))
    outer, inner = decode_multicall(plan["calls"][1], ROUTER_ABI)
    p = inner[0][1]["params"]
    check("native out, exact in: swaps to the router itself, then unwrapWETH9(min, recipient) in the same multicall",
          [n for n, _ in inner] == ["exactInputSingle", "unwrapWETH9"] and p["recipient"] == router and p["tokenOut"] == w.weth
          and inner[1][1] == {"amountMinimum": minimum, "recipient": RECIPIENT}, str(inner))
    check("native out, exact in: no value, deadline set", plan["calls"][1]["value"] == 0 and _deadline_ok(outer["deadline"]))
    check("native out, exact in: summary keys", list(plan["summary"]) == ["token_in", "amount_in", "amount_out_min", "path", "fees"])
    plan = tools["swap_tokens_for_exact_eth"].invoke({"token_in": A["USDC"], "amount_out": 0.5, "from_address": WALLET})
    cost = min(w.quote_out(A["USDC"], w.weth, 5 * 10**17, f) for f in (500, 3000))
    maximum = cost * 10050 // 10000
    outer, inner = decode_multicall(plan["calls"][1], ROUTER_ABI)
    check("native out, exact out: [approve, swap, approve 0]", roles(plan) == ["approve", "swap", "approve_reset"], str(roles(plan)))
    check("native out, exact out: exactOutputSingle to the router, then unwrapWETH9(exactly amount_out, wallet)",
          [n for n, _ in inner] == ["exactOutputSingle", "unwrapWETH9"] and inner[0][1]["params"]["recipient"] == router
          and inner[0][1]["params"]["amountInMaximum"] == maximum and inner[1][1] == {"amountMinimum": 5 * 10**17, "recipient": WALLET}, str(inner))
    check("native out, exact out: summary keys", list(plan["summary"]) == ["token_in", "amount_out", "amount_in_max", "path", "fees"])
    plan = tools["swap_exact_tokens_for_eth"].invoke({"token_in": A["LINK"], "amount_in": 100.0, "from_address": WALLET})
    outer, inner = decode_multicall(plan["calls"][1], ROUTER_ABI)
    check("native out of LINK: one hop into WETH (a pool for it exists), then unwrapWETH9",
          [n for n, _ in inner] == ["exactInputSingle", "unwrapWETH9"] and plan["summary"]["path"] == [A["LINK"], w.weth])

    # -- refusals --
    raises("selling WETH for native coin is refused (same asset)",
           lambda: tools["swap_exact_tokens_for_eth"].invoke({"token_in": w.weth, "amount_in": 1.0, "from_address": WALLET}), "same")
    raises("buying WETH with native coin is refused (same asset)",
           lambda: tools["swap_exact_eth_for_tokens"].invoke({"token_out": w.weth, "amount_in": 1.0, "from_address": WALLET}), "same")
    raises("a balance short of the swap is refused with the shortfall",
           lambda: tools["swap_exact_tokens_for_tokens"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 1_200_000.0, "from_address": WALLET}), "short by")
    raises("native balance short of the swap is refused",
           lambda: tools["swap_exact_eth_for_tokens"].invoke({"token_out": A["USDC"], "amount_in": 500.0, "from_address": WALLET}), "native asset")
    raises("exact-out balance is checked against the maximum, not the quote",
           lambda: tools["swap_tokens_for_exact_tokens"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 900.0, "from_address": WALLET}), "short by")
    raises("slippage of 100% or more is refused",
           lambda: tools["swap_exact_tokens_for_tokens"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 1.0, "from_address": WALLET, "slippage_bps": 10000}), "slippage_bps")
    raises("a deadline of zero seconds is refused (it would already have passed)",
           lambda: tools["swap_exact_tokens_for_tokens"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 1.0, "from_address": WALLET, "deadline_secs": 0}), "deadline_secs")
    raises("a negative slippage is refused",
           lambda: tools["swap_exact_tokens_for_tokens"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 1.0, "from_address": WALLET, "slippage_bps": -1}), "slippage_bps")
    nopre = World(chain_id, positions=False).tools(preflight=False)
    plan = nopre["swap_exact_tokens_for_tokens"].invoke({"token_in": A["USDC"], "token_out": A["WETH"], "amount_in": 1_200_000.0, "from_address": WALLET})
    check("preflight=False builds the plan without the balance check", roles(plan) == ["approve", "swap"])

    # -- EOA mode: the same calls, rendered as transactions with sequential nonces and one fee snapshot --
    eoa = World(chain_id, positions=False)
    eoa_tools = eoa.tools(tx_mode="eoa", reset_residual_approvals=True)
    plan = eoa_tools["swap_tokens_for_exact_tokens"].invoke(
        {"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 0.5, "from_address": WALLET})
    txs = plan["transactions"]
    check("eoa mode renders one transaction per call, nonces following the account's pending count",
          txs is not None and [t["nonce"] for t in txs] == [7, 8, 9] and [t["to"] for t in txs] == [c["to"] for c in plan["calls"]],
          str(txs and [t["nonce"] for t in txs]))
    check("eoa mode: each transaction carries its call's value and data, from the sender",
          all(t["data"] == c["data"] and t["value"] == c["value"] and t["from"] == WALLET for t, c in zip(txs, plan["calls"])))
    check("eoa mode: fees are EIP-1559 and the same on every transaction",
          all(t["maxFeePerGas"] == 2 * 2 * 10**9 + 10**9 and t["maxPriorityFeePerGas"] == 10**9 and t["chainId"] == chain_id for t in txs))
    check("eoa mode: gas is the estimate with the 1.25 buffer", all(t["gas_estimated"] and t["gas"] == int(120_000 * 1.25) for t in txs),
          str([(t["gas"], t["gas_estimated"]) for t in txs]))
    check("eoa mode, no reset by default: [approve, swap] only",
          [t["nonce"] for t in World(chain_id, positions=False).tools(tx_mode="eoa")["swap_tokens_for_exact_tokens"].invoke(
              {"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 0.5, "from_address": WALLET})["transactions"]] == [7, 8])
    # A swap cannot be simulated before its approval is mined: it falls back to the role's default gas,
    # but the FIRST call of a plan has no such excuse.
    def swaps_revert(tx):
        if tx["to"] == eoa.router:
            raise Revert()
        return 120_000

    eoa.chain.estimate_gas = swaps_revert
    plan = eoa_tools["swap_tokens_for_exact_tokens"].invoke(
        {"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 0.5, "from_address": WALLET})
    txs = plan["transactions"]
    check("eoa mode: a swap that can't be simulated yet gets the default swap gas, flagged as not estimated",
          txs[1]["gas"] == DEFAULT_GAS["swap"] and txs[1]["gas_estimated"] is False and txs[0]["gas_estimated"] is True, str([(t["gas"], t["gas_estimated"]) for t in txs]))
    eoa.chain.estimate_gas = raising(Revert())
    raises("eoa mode: a first call that would revert is fatal", lambda: eoa_tools["swap_tokens_for_exact_tokens"].invoke(
        {"token_in": A["USDC"], "token_out": A["WETH"], "amount_out": 0.5, "from_address": WALLET}), "would revert")
    eoa.chain.estimate_gas = lambda tx: 120_000
    plan = eoa_tools["swap_exact_eth_for_tokens"].invoke({"token_out": A["USDC"], "amount_in": 0.25, "from_address": WALLET, "nonce": 40})
    check("eoa mode: an explicit nonce is used, and native value is carried",
          [t["nonce"] for t in plan["transactions"]] == [40] and plan["transactions"][0]["value"] == 25 * 10**16)


# ── Price ranges ──────────────────────────────────────────────────────────────


def test_suggest(chain_id: int):
    w = World(chain_id, positions=False)
    tools = w.tools()
    A = w.addr
    print(f"suggest_price_ranges (chain {chain_id})")
    res = tools["suggest_price_ranges"].invoke({"token_a": A["WETH"], "token_b": A["USDC"]})
    main = w.pool(A["WETH"], A["USDC"], 500)
    check("it picks the tier with the most active liquidity", res["fee"] == 500 and res["pool"] == main["address"] and res["fee_pct"] == 0.05, str(res["fee"]))
    check("ETH/USDC reads USDC per ETH", res["base_token"] == A["WETH"] and res["quote_token"] == A["USDC"])
    check("the current price is the pool's price", abs(res["current_price"] - 3000.0) / 3000.0 < 1e-9, str(res["current_price"]))
    check("it is not a stable pair", res["stable_pair"] is False)
    check("its keys are the contract's", list(res) == ["pool", "fee", "fee_pct", "base_token", "quote_token", "current_price", "stable_pair", "options"], str(list(res)))
    check("the options' keys are the contract's",
          all(list(o) == ["name", "width_pct", "full_range", "min_price", "max_price", "tick_lower", "tick_upper", "base_share_pct", "quote_share_pct"] for o in res["options"]))
    bands = res["options"][:3]
    check("narrow/medium/wide are 5/15/50 %, then full", [o["width_pct"] for o in res["options"]] == [5.0, 15.0, 50.0, None])
    check("min < current < max, ticks on the pool's spacing of 10",
          all(o["min_price"] < res["current_price"] < o["max_price"] and o["tick_lower"] % 10 == 0 and o["tick_upper"] % 10 == 0 for o in bands))
    check("the medium band is 15% either side of 3000", 2550 * 0.998 < bands[1]["min_price"] <= 2550 and 3450 <= bands[1]["max_price"] < 3450 * 1.002,
          f"{bands[1]['min_price']} {bands[1]['max_price']}")
    check("full range: min 0.0, max None", res["options"][3]["min_price"] == 0.0 and res["options"][3]["max_price"] is None and res["options"][3]["full_range"])

    res = tools["suggest_price_ranges"].invoke({"token_a": A["USDC"], "token_b": A["WETH"], "fee": 3000})
    check("a fee tier can be named, and the token order does not matter",
          res["fee"] == 3000 and res["base_token"] == A["WETH"] and abs(res["current_price"] - 3003.0) / 3003.0 < 1e-9)
    check("the 0.3% pool's spacing of 60 is used", all(o["tick_lower"] % 60 == 0 and o["tick_upper"] % 60 == 0 for o in res["options"]))

    res = tools["suggest_price_ranges"].invoke({"token_a": A["LINK"], "token_b": A["WETH"]})
    check("LINK/WETH reads WETH per LINK", res["base_token"] == A["LINK"] and res["quote_token"] == w.weth and abs(res["current_price"] - 0.005) < 1e-12, str(res["current_price"]))
    check("... and min < current < max whichever way the pool is sorted",
          all(o["min_price"] < res["current_price"] < o["max_price"] for o in res["options"][:3]))

    res = tools["suggest_price_ranges"].invoke({"token_a": A["DAI"], "token_b": A["USDC"]})
    check("DAI/USDC is a stable pair with 0.5 / 1 / 3 % bands",
          res["stable_pair"] is True and [o["width_pct"] for o in res["options"]] == [0.5, 1.0, 3.0, None] and res["fee"] == 100)
    check("... its prices hug 1", all(0.95 < o["min_price"] < 1.0 < o["max_price"] < 1.05 for o in res["options"][:3]), str(res["options"][:3]))

    raises("a pair with no pool is refused, saying it never creates one",
           lambda: tools["suggest_price_ranges"].invoke({"token_a": A["USDC"], "token_b": A["LINK"]}), "never creates a pool")
    raises("a fee tier with no pool is refused",
           lambda: tools["suggest_price_ranges"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "fee": 10000}), "1% fee tier")
    raises("a pool that was never initialised is refused",
           lambda: tools["suggest_price_ranges"].invoke({"token_a": UNPRICED, "token_b": w.weth}), "never initialised")
    raises("the same token twice is refused", lambda: tools["suggest_price_ranges"].invoke({"token_a": A["USDC"], "token_b": A["USDC"]}), "same token")
    raises("a nonsense fee is refused", lambda: tools["suggest_price_ranges"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "fee": 0}), "not a pool fee")


# ── open_position / add_to_position ───────────────────────────────────────────


def test_open_position(chain_id: int):
    w = World(chain_id, positions=False)
    tools = w.tools()
    A = w.addr
    print(f"open_position (chain {chain_id})")
    pool = w.pool(A["WETH"], A["USDC"], 500)
    token0, token1 = pool["token0"], pool["token1"]
    weth_is_token0 = token0 == A["WETH"]

    res = tools["suggest_price_ranges"].invoke({"token_a": A["WETH"], "token_b": A["USDC"]})
    medium = res["options"][1]
    plan = tools["open_position"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET,
                                          "min_price": medium["min_price"], "max_price": medium["max_price"]})
    check("mint: [approve, approve, mint, reset, reset]", roles(plan) == ["approve", "approve", "add_liquidity", "approve_reset", "approve_reset"], str(roles(plan)))
    check("mint: the option's prices come back as the option's own ticks",
          (plan["summary"]["tick_lower"], plan["summary"]["tick_upper"]) == (medium["tick_lower"], medium["tick_upper"]), str(plan["summary"]))
    name, args = decode(PM_ABI, plan["calls"][2]["data"])
    p = args["params"]
    check("mint: goes straight to the position manager, no value", name == "mint" and plan["calls"][2]["to"] == w.pm and plan["calls"][2]["value"] == 0)
    check("mint: tokens sorted, fee, ticks, recipient",
          (p["token0"], p["token1"], p["fee"], p["tickLower"], p["tickUpper"], p["recipient"])
          == (token0, token1, 500, medium["tick_lower"], medium["tick_upper"], WALLET), str(p))
    check("mint: the deadline is in the params", _deadline_ok(p["deadline"]), str(p["deadline"]))
    a0, a1 = p["amount0Desired"], p["amount1Desired"]
    amount_weth, amount_usdc = (a0, a1) if weth_is_token0 else (a1, a0)
    pa, pb = plan["summary"]["min_price"], plan["summary"]["max_price"]
    usdc_per_eth = (math.sqrt(3000.0) - math.sqrt(pa)) / (1 / math.sqrt(3000.0) - 1 / math.sqrt(pb))   # textbook V3, in floats
    check("mint: amount_a is exactly what was asked, token_b follows from the range",
          amount_weth == 10**18 and abs(amount_usdc / 10**6 / usdc_per_eth - 1) < 1e-6, f"{amount_weth} {amount_usdc} {usdc_per_eth}")
    sqrt_lo, sqrt_hi = m.get_sqrt_ratio_at_tick(p["tickLower"]), m.get_sqrt_ratio_at_tick(p["tickUpper"])
    check("mint: the position manager will compute the liquidity we derived",
          m.liquidity_for_amounts(pool["sqrt_price"], sqrt_lo, sqrt_hi, a0, a1) == m.liquidity_for_amount0(pool["sqrt_price"], sqrt_hi, a0)
          if weth_is_token0 else
          m.liquidity_for_amounts(pool["sqrt_price"], sqrt_lo, sqrt_hi, a0, a1) == m.liquidity_for_amount1(sqrt_lo, pool["sqrt_price"], a1))
    min0, min1 = p["amount0Min"], p["amount1Min"]
    check("mint: each minimum is the desired amount less 0.5%, and never above what the mint takes",
          min0 == min(a0 * 9950 // 10000, m.mint_amounts(pool["sqrt_price"], sqrt_lo, sqrt_hi, m.liquidity_for_amounts(pool["sqrt_price"], sqrt_lo, sqrt_hi, a0, a1))[0])
          and min1 == min(a1 * 9950 // 10000, m.mint_amounts(pool["sqrt_price"], sqrt_lo, sqrt_hi, m.liquidity_for_amounts(pool["sqrt_price"], sqrt_lo, sqrt_hi, a0, a1))[1]), f"{min0} {min1}")
    approvals = [approval(plan["calls"][0]), approval(plan["calls"][1])]
    check("mint: each token the position manager pulls is approved for its desired amount, to the position manager",
          sorted(approvals) == sorted([(A["WETH"], w.pm, amount_weth), (A["USDC"], w.pm, amount_usdc)]), str(approvals))
    check("mint: ... and each approval is reset to 0 afterwards",
          sorted((approval(c)[0], approval(c)[2]) for c in plan["calls"][3:]) == sorted([(A["WETH"], 0), (A["USDC"], 0)]))
    summary = plan["summary"]
    check("mint: the summary has exactly the contract's keys",
          list(summary) == ["pool", "fee", "fee_pct", "base_token", "quote_token", "tick_lower", "tick_upper", "min_price", "max_price",
                            "full_range", "current_price", "in_range", "token_a", "token_b", "amount_a", "amount_b", "amount_a_min",
                            "amount_b_min", "native_value"], str(list(summary)))
    check("mint: the summary's figures",
          summary["pool"] == pool["address"] and summary["fee"] == 500 and summary["fee_pct"] == 0.05 and summary["base_token"] == A["WETH"]
          and summary["quote_token"] == A["USDC"] and summary["full_range"] is False and summary["in_range"] is True
          and summary["token_a"] == A["WETH"] and summary["token_b"] == A["USDC"] and summary["amount_a"] == 1.0
          and summary["amount_b"] == amount_usdc / 10**6 and summary["native_value"] == 0.0
          and summary["amount_a_min"] == (min0 if weth_is_token0 else min1) / 10**18 and summary["amount_b_min"] == (min1 if weth_is_token0 else min0) / 10**6, str(summary))
    check("mint: min_price / max_price are the ticks' prices", summary["min_price"] <= medium["min_price"] * 1.000001 and summary["max_price"] >= medium["max_price"] * 0.999999
          and summary["min_price"] == medium["min_price"] and summary["max_price"] == medium["max_price"])
    check("mint: the action line names the pool, amounts and prices",
          all(s in plan["calls"][2]["description"] for s in ("Open a Uniswap V3 position in the", "0.05% pool", A["WETH"], A["USDC"], "per")), plan["calls"][2]["description"])

    # token_b as the amount_a side: the other token is asked for, the maths mirrors
    plan2 = tools["open_position"].invoke({"token_a": A["USDC"], "token_b": A["WETH"], "amount_a": 1500.0, "from_address": WALLET,
                                           "min_price": medium["min_price"], "max_price": medium["max_price"], "fee": 500})
    p2 = decode(PM_ABI, plan2["calls"][2]["data"])[1]["params"]
    usdc_desired, weth_desired = (p2["amount1Desired"], p2["amount0Desired"]) if weth_is_token0 else (p2["amount0Desired"], p2["amount1Desired"])
    check("depositing the quote token as amount_a: same pool and ticks, USDC exactly as asked, WETH worked out",
          (p2["token0"], p2["token1"], p2["tickLower"], p2["tickUpper"]) == (token0, token1, medium["tick_lower"], medium["tick_upper"])
          and usdc_desired == 1500 * 10**6 and 0 < weth_desired < 10**18, str(p2))
    check("... and the summary calls USDC token_a, WETH token_b",
          (plan2["summary"]["token_a"], plan2["summary"]["token_b"], plan2["summary"]["amount_a"]) == (A["USDC"], A["WETH"], 1500.0)
          and plan2["summary"]["amount_b"] == weth_desired / 10**18)

    # Native coin: the WETH side is the call's value, refunded by refundETH, never approved
    plan = tools["open_position"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET, "use_native": True,
                                          "min_price": medium["min_price"], "max_price": medium["max_price"]})
    check("use_native: [approve the other token, multicall{value}, reset it]", roles(plan) == ["approve", "add_liquidity", "approve_reset"], str(roles(plan)))
    check("use_native: only USDC is approved", approval(plan["calls"][0])[0] == A["USDC"] and approval(plan["calls"][2])[0] == A["USDC"])
    call = plan["calls"][1]
    outer, inner = decode_multicall(call, PM_ABI)
    check("use_native: the WETH side is the value", call["value"] == 10**18 and plan["summary"]["native_value"] == 1.0, str(call["value"]))
    check("use_native: multicall([mint, refundETH]) on the position manager", call["to"] == w.pm and [n for n, _ in inner] == ["mint", "refundETH"])
    check("use_native: the mint is the same mint", inner[0][1]["params"]["token0"] == token0 and inner[0][1]["params"]["fee"] == 500
          and inner[0][1]["params"]["tickLower"] == medium["tick_lower"])
    plan = tools["open_position"].invoke({"token_a": A["USDC"], "token_b": A["WETH"], "amount_a": 1500.0, "from_address": WALLET, "use_native": True,
                                          "min_price": medium["min_price"], "max_price": medium["max_price"]})
    inner_p = decode_multicall(plan["calls"][1], PM_ABI)[1][0][1]["params"]
    weth_desired = inner_p["amount0Desired"] if weth_is_token0 else inner_p["amount1Desired"]
    check("use_native with token_a the other token: the WETH amount it works out is the value",
          plan["calls"][1]["value"] == weth_desired and approval(plan["calls"][0])[0] == A["USDC"], str((plan["calls"][1]["value"], weth_desired)))
    raises("use_native on a pool without WETH is refused",
           lambda: tools["open_position"].invoke({"token_a": A["DAI"], "token_b": A["USDC"], "amount_a": 10.0, "from_address": WALLET, "use_native": True,
                                                  "full_range": True}), "use_native")

    # Full range
    plan = tools["open_position"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET, "full_range": True, "fee": 3000})
    p = decode(PM_ABI, plan["calls"][2]["data"])[1]["params"]
    check("full range: the widest ticks for the 0.3% pool's spacing of 60", (p["tickLower"], p["tickUpper"]) == (-887220, 887220), str(p))
    check("full range: min_price 0.0, max_price None, full_range True",
          (plan["summary"]["min_price"], plan["summary"]["max_price"], plan["summary"]["full_range"]) == (0.0, None, True))
    check("full range: the action line says so", "full price range" in plan["calls"][2]["description"])
    check("full range with the 0.3% pool: its fee tier was named", p["fee"] == 3000 and plan["summary"]["fee"] == 3000)

    # Single-sided: a range wholly above the price takes only token0 (here the pair's first token)
    # "Above today's price" in quote-per-base terms is above in tick terms only when the base (WETH) is token0.
    above = (3300.0, 3600.0)
    plan = tools["open_position"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET,
                                          "min_price": above[0], "max_price": above[1]})
    if weth_is_token0:
        check("a range above today's price, depositing the base (token0): single-sided, no approval for the other token",
              roles(plan) == ["approve", "add_liquidity", "approve_reset"] and plan["summary"]["amount_b"] == 0.0 and plan["summary"]["in_range"] is False
              and approval(plan["calls"][0])[0] == A["WETH"], str((roles(plan), plan["summary"]["amount_b"])))
        check("... and the action line warns that it is out of range", "out of range" in plan["calls"][1]["description"], plan["calls"][1]["description"])
        raises("a range above today's price needs only WETH: depositing USDC is refused, naming WETH",
               lambda: tools["open_position"].invoke({"token_a": A["USDC"], "token_b": A["WETH"], "amount_a": 1500.0, "from_address": WALLET,
                                                      "min_price": above[0], "max_price": above[1]}), A["WETH"])
        raises("... the message says to deposit it instead",
               lambda: tools["open_position"].invoke({"token_a": A["USDC"], "token_b": A["WETH"], "amount_a": 1500.0, "from_address": WALLET,
                                                      "min_price": above[0], "max_price": above[1]}), "instead")
    else:
        # base = token1 (USDC token0, WETH token1): a USD-price range above today is BELOW in tick terms: only token1 (WETH) is held
        check("a range above today's price (base = token1): the position holds only WETH, so depositing WETH is single-sided",
              roles(plan) == ["approve", "add_liquidity", "approve_reset"] and plan["summary"]["amount_b"] == 0.0 and plan["summary"]["in_range"] is False
              and approval(plan["calls"][0])[0] == A["WETH"], str((roles(plan), plan["summary"]["amount_b"])))
        raises("... and depositing USDC for it is refused, naming WETH",
               lambda: tools["open_position"].invoke({"token_a": A["USDC"], "token_b": A["WETH"], "amount_a": 1500.0, "from_address": WALLET,
                                                      "min_price": above[0], "max_price": above[1]}), A["WETH"])
    below = (2000.0, 2400.0)
    plan = tools["open_position"].invoke({"token_a": A["USDC"], "token_b": A["WETH"], "amount_a": 1500.0, "from_address": WALLET,
                                          "min_price": below[0], "max_price": below[1]})
    check("a range below today's price holds only USDC whichever way the pool is sorted: depositing USDC is single-sided",
          plan["summary"]["amount_b"] == 0.0 and plan["summary"]["in_range"] is False and approval(plan["calls"][0])[0] == A["USDC"]
          and roles(plan) == ["approve", "add_liquidity", "approve_reset"], str((roles(plan), plan["summary"]["amount_b"])))

    # Zero slippage must still be buildable: minimums are capped at what the mint takes
    plan = tools["open_position"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET,
                                          "min_price": medium["min_price"], "max_price": medium["max_price"], "slippage_bps": 0})
    p = decode(PM_ABI, plan["calls"][2]["data"])[1]["params"]
    sa, sb = m.get_sqrt_ratio_at_tick(p["tickLower"]), m.get_sqrt_ratio_at_tick(p["tickUpper"])
    liq = m.liquidity_for_amounts(pool["sqrt_price"], sa, sb, p["amount0Desired"], p["amount1Desired"])
    used = m.mint_amounts(pool["sqrt_price"], sa, sb, liq)
    check("slippage 0: the minimums equal what the mint will take, so it can succeed", (p["amount0Min"], p["amount1Min"]) == used and used[0] <= p["amount0Desired"] and used[1] <= p["amount1Desired"], f"{used} vs {p}")

    # recipient, preflight, refusals
    plan = tools["open_position"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET, "recipient": RECIPIENT,
                                          "min_price": medium["min_price"], "max_price": medium["max_price"]})
    check("recipient receives the position NFT", decode(PM_ABI, plan["calls"][2]["data"])[1]["params"]["recipient"] == RECIPIENT)
    raises("both full_range and prices is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET, "full_range": True, "min_price": 1.0, "max_price": 2.0}), "not both")
    raises("no range at all is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET}), "full_range")
    raises("only one bound is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET, "min_price": 2000.0}), "max_price")
    raises("min above max is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET, "min_price": 3500.0, "max_price": 2500.0}), "below max_price")
    raises("a zero min_price is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1.0, "from_address": WALLET, "min_price": 0.0, "max_price": 2500.0}), "above zero")
    raises("a pair with no pool is refused (never creates one)", lambda: tools["open_position"].invoke(
        {"token_a": A["USDC"], "token_b": A["LINK"], "amount_a": 1.0, "from_address": WALLET, "full_range": True}), "never creates a pool")
    raises("an uninitialised pool is refused", lambda: tools["open_position"].invoke(
        {"token_a": UNPRICED, "token_b": w.weth, "amount_a": 1.0, "from_address": WALLET, "full_range": True}), "never initialised")
    raises("a zero amount is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 0.0, "from_address": WALLET, "full_range": True}), "greater than zero")
    raises("a balance short of the deposit is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 5000.0, "from_address": WALLET, "min_price": medium["min_price"], "max_price": medium["max_price"]}), "short by")
    raises("the same token twice is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["WETH"], "amount_a": 1.0, "from_address": WALLET, "full_range": True}), "same token")
    raises("an amount too small to buy any liquidity is refused", lambda: tools["open_position"].invoke(
        {"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 1e-15, "from_address": WALLET, "min_price": medium["min_price"], "max_price": medium["max_price"]}), "too small")
    p = decode(PM_ABI, tools["open_position"].invoke({"token_a": A["WETH"], "token_b": A["USDC"], "amount_a": 0.01, "from_address": WALLET,
                                                    "min_price": 2999.0, "max_price": 3001.0})["calls"][2]["data"])[1]["params"]
    check("a range narrower than one spacing is widened to one", p["tickUpper"] - p["tickLower"] >= 10 and p["tickLower"] % 10 == 0 and p["tickUpper"] % 10 == 0, str(p))


def test_add_to_position(chain_id: int):
    w = World(chain_id)
    tools = w.tools()
    A = w.addr
    print(f"add_to_position (chain {chain_id})")
    pool = w.pool(A["WETH"], A["USDC"], 500)
    weth_is_token0 = pool["token0"] == A["WETH"]
    position = w.positions[101]

    plan = tools["add_to_position"].invoke({"token_id": 101, "token_a": A["WETH"], "amount_a": 0.5, "from_address": WALLET})
    check("increase: [approve, approve, increaseLiquidity, reset, reset]", roles(plan) == ["approve", "approve", "add_liquidity", "approve_reset", "approve_reset"], str(roles(plan)))
    name, args = decode(PM_ABI, plan["calls"][2]["data"])
    p = args["params"]
    check("increase: increaseLiquidity on the position manager, no value", name == "increaseLiquidity" and plan["calls"][2]["to"] == w.pm and plan["calls"][2]["value"] == 0)
    a0, a1 = p["amount0Desired"], p["amount1Desired"]
    amount_weth, amount_usdc = (a0, a1) if weth_is_token0 else (a1, a0)
    check("increase: the position's id, WETH as asked, USDC from the position's range",
          p["tokenId"] == 101 and amount_weth == 5 * 10**17 and amount_usdc > 0 and _deadline_ok(p["deadline"]), str(p))
    check("increase: minimums below the desired amounts", 0 < p["amount0Min"] <= a0 and 0 < p["amount1Min"] <= a1)
    sa, sb = m.get_sqrt_ratio_at_tick(position["tick_lower"]), m.get_sqrt_ratio_at_tick(position["tick_upper"])
    expected_b = m.mint_amounts(pool["sqrt_price"], sa, sb, m.liquidity_for_amount0(pool["sqrt_price"], sb, 5 * 10**17) if weth_is_token0
                                else m.liquidity_for_amount1(sa, pool["sqrt_price"], 5 * 10**17))
    check("increase: the other token's amount is what the position's own range needs", (a1 if weth_is_token0 else a0) == expected_b[1 if weth_is_token0 else 0])
    check("increase: approvals go to the position manager for the desired amounts",
          sorted([approval(plan["calls"][0]), approval(plan["calls"][1])]) == sorted([(A["WETH"], w.pm, amount_weth), (A["USDC"], w.pm, amount_usdc)]))
    s = plan["summary"]
    check("increase: the summary has exactly the contract's keys",
          list(s) == ["token_id", "token_a", "token_b", "amount_a", "amount_b", "amount_a_min", "amount_b_min", "native_value", "base_token",
                      "quote_token", "min_price", "max_price", "full_range", "in_range"], str(list(s)))
    check("increase: the summary's figures", s["token_id"] == 101 and s["token_a"] == A["WETH"] and s["token_b"] == A["USDC"] and s["amount_a"] == 0.5
          and s["amount_b"] == amount_usdc / 10**6 and s["in_range"] is True and s["full_range"] is False and s["native_value"] == 0.0
          and s["base_token"] == A["WETH"] and s["quote_token"] == A["USDC"] and s["min_price"] < 3000.0 < s["max_price"], str(s))

    plan = tools["add_to_position"].invoke({"token_id": 101, "token_a": A["WETH"], "amount_a": 0.5, "from_address": WALLET, "use_native": True})
    outer, inner = decode_multicall(plan["calls"][1], PM_ABI)
    check("increase with use_native: multicall{value}([increaseLiquidity, refundETH]), only USDC approved",
          roles(plan) == ["approve", "add_liquidity", "approve_reset"] and [n for n, _ in inner] == ["increaseLiquidity", "refundETH"]
          and plan["calls"][1]["value"] == 5 * 10**17 and approval(plan["calls"][0])[0] == A["USDC"] and plan["summary"]["native_value"] == 0.5)

    plan = tools["add_to_position"].invoke({"token_id": 101, "token_a": A["USDC"], "amount_a": 1500.0, "from_address": WALLET})
    p = decode(PM_ABI, plan["calls"][2]["data"])[1]["params"]
    check("increase with the other token as token_a", (p["amount1Desired"] if weth_is_token0 else p["amount0Desired"]) == 1500 * 10**6 and plan["summary"]["token_b"] == A["WETH"])

    plan = tools["add_to_position"].invoke({"token_id": 103, "token_a": A["WETH"], "amount_a": 0.1, "from_address": WALLET})
    check("a full-range position: summary says full_range, min 0.0, max None",
          plan["summary"]["full_range"] is True and plan["summary"]["min_price"] == 0.0 and plan["summary"]["max_price"] is None and plan["summary"]["in_range"] is True)
    plan = tools["add_to_position"].invoke({"token_id": 102, "token_a": A["WETH"] if weth_is_token0 else A["USDC"], "amount_a": 0.1 if weth_is_token0 else 100.0, "from_address": WALLET})
    check("an out-of-range position can be topped up with the one token it holds",
          plan["summary"]["in_range"] is False and plan["summary"]["amount_b"] == 0.0 and roles(plan) == ["approve", "add_liquidity", "approve_reset"])
    raises("an out-of-range position refuses the token it does not hold, naming the other",
           lambda: tools["add_to_position"].invoke({"token_id": 102, "token_a": A["USDC"] if weth_is_token0 else A["WETH"],
                                                    "amount_a": 100.0 if weth_is_token0 else 0.1, "from_address": WALLET}), "instead")

    raises("someone else's position is refused", lambda: tools["add_to_position"].invoke({"token_id": 201, "token_a": A["WETH"], "amount_a": 0.1, "from_address": WALLET}), "is owned by")
    raises("a position that does not exist is refused", lambda: tools["add_to_position"].invoke({"token_id": 999, "token_a": A["WETH"], "amount_a": 0.1, "from_address": WALLET}), "does not exist")
    raises("a token that is not in the position is refused", lambda: tools["add_to_position"].invoke({"token_id": 101, "token_a": A["DAI"], "amount_a": 1.0, "from_address": WALLET}), "not one of position #101")
    raises("a balance short of the increase is refused", lambda: tools["add_to_position"].invoke({"token_id": 101, "token_a": A["WETH"], "amount_a": 5000.0, "from_address": WALLET}), "short by")


# ── withdraw_from_position / collect_fees ─────────────────────────────────────


def test_withdraw_and_collect(chain_id: int):
    w = World(chain_id)
    tools = w.tools()
    A = w.addr
    print(f"withdraw_from_position and collect_fees (chain {chain_id})")
    pool = w.pool(A["WETH"], A["USDC"], 500)
    t0, t1 = pool["token0"], pool["token1"]
    d0, d1 = w.decimals[t0], w.decimals[t1]
    position = w.positions[101]
    L = position["liquidity"]
    sa, sb = m.get_sqrt_ratio_at_tick(position["tick_lower"]), m.get_sqrt_ratio_at_tick(position["tick_upper"])

    plan = tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 50, "from_address": WALLET})
    check("withdraw 50%: one multicall, no approval", roles(plan) == ["remove_liquidity"] and len(plan["calls"]) == 1, str(roles(plan)))
    call = plan["calls"][0]
    outer, inner = decode_multicall(call, PM_ABI)
    check("withdraw 50%: to the position manager, no value", call["to"] == w.pm and call["value"] == 0)
    check("withdraw 50%: decreaseLiquidity then collect, and no burn", [n for n, _ in inner] == ["decreaseLiquidity", "collect"], str([n for n, _ in inner]))
    dec = inner[0][1]["params"]
    expected = m.amounts_for_liquidity(pool["sqrt_price"], sa, sb, L * 50 // 100)
    check("withdraw 50%: half the liquidity, minimums 0.5% under the expected share, and a deadline",
          dec["tokenId"] == 101 and dec["liquidity"] == L * 50 // 100 and dec["amount0Min"] == expected[0] * 9950 // 10000
          and dec["amount1Min"] == expected[1] * 9950 // 10000 and _deadline_ok(dec["deadline"]), str(dec))
    col = inner[1][1]["params"]
    check("withdraw 50%: collect everything owed, to the wallet", col == {"tokenId": 101, "recipient": WALLET, "amount0Max": 2**128 - 1, "amount1Max": 2**128 - 1}, str(col))
    s = plan["summary"]
    fees0, fees1 = position["fees0"], position["fees1"]
    check("withdraw 50%: the summary has exactly the contract's keys",
          list(s) == ["token_id", "percent", "token0", "token1", "expected0", "expected1", "amount0_min", "amount1_min", "fees0", "fees1", "burns"], str(list(s)))
    check("withdraw 50%: the summary's figures",
          s["token_id"] == 101 and s["percent"] == 50 and (s["token0"], s["token1"]) == (t0, t1) and s["burns"] is False
          and s["expected0"] == expected[0] / 10**d0 and s["expected1"] == expected[1] / 10**d1
          and s["amount0_min"] == dec["amount0Min"] / 10**d0 and s["amount1_min"] == dec["amount1Min"] / 10**d1
          and s["fees0"] == fees0 / 10**d0 and s["fees1"] == fees1 / 10**d1, str(s))
    check("withdraw 50%: the action line gives both the share and the fees",
          all(x in call["description"] for x in ("Withdraw 50% of Uniswap V3 position #101", "unclaimed fees", t0, t1)), call["description"])
    check("the unclaimed fees were simulated from the owner (collect with from=owner)", w.chain.count("collect") >= 1)

    plan = tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 100, "from_address": WALLET})
    outer, inner = decode_multicall(plan["calls"][0], PM_ABI)
    check("withdraw 100%: decreaseLiquidity(all), collect, burn", [n for n, _ in inner] == ["decreaseLiquidity", "collect", "burn"], str([n for n, _ in inner]))
    check("withdraw 100%: exactly L is removed and the NFT burned", inner[0][1]["params"]["liquidity"] == L and inner[2][1] == {"tokenId": 101} and plan["summary"]["burns"] is True)
    check("withdraw 100%: still no approval", roles(plan) == ["remove_liquidity"])
    plan = tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 33.3, "from_address": WALLET})
    dec = decode_multicall(plan["calls"][0], PM_ABI)[1][0][1]["params"]
    check("a fractional percent is exact integer math", dec["liquidity"] == L * 333 // 1000, str(dec["liquidity"]))
    plan = tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 50, "from_address": WALLET, "recipient": RECIPIENT, "slippage_bps": 200})
    outer, inner = decode_multicall(plan["calls"][0], PM_ABI)
    check("recipient and slippage on a withdrawal", inner[1][1]["params"]["recipient"] == RECIPIENT and inner[0][1]["params"]["amount0Min"] == expected[0] * 9800 // 10000)

    # Native out: collect to the position manager, then unwrap the WETH side and sweep the other
    plan = tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 100, "from_address": WALLET, "native_out": True, "recipient": RECIPIENT})
    outer, inner = decode_multicall(plan["calls"][0], PM_ABI)
    check("native_out: [decrease, collect to address(0), unwrapWETH9, sweepToken, burn]",
          [n for n, _ in inner] == ["decreaseLiquidity", "collect", "unwrapWETH9", "sweepToken", "burn"], str([n for n, _ in inner]))
    check("native_out: collect pays the position manager itself (recipient 0)", inner[1][1]["params"]["recipient"] == "0x" + "00" * 20)
    check("native_out: unwrapWETH9(0, recipient)", inner[2][1] == {"amountMinimum": 0, "recipient": RECIPIENT})
    check("native_out: sweepToken(the other token, 0, recipient)", inner[3][1] == {"token": A["USDC"], "amountMinimum": 0, "recipient": RECIPIENT}, str(inner[3][1]))
    plan = tools["withdraw_from_position"].invoke({"token_id": 106, "percent": 25, "from_address": WALLET, "native_out": True})
    outer, inner = decode_multicall(plan["calls"][0], PM_ABI)
    check("native_out on a LINK/WETH position: LINK is the token swept, whichever of the two sorts first",
          inner[3][1] == {"token": A["LINK"], "amountMinimum": 0, "recipient": WALLET} and inner[2][1] == {"amountMinimum": 0, "recipient": WALLET}, str(inner))
    # A position whose liquidity is gone but whose fees are not: only collect (and burn at 100%)
    plan = tools["withdraw_from_position"].invoke({"token_id": 107, "percent": 100, "from_address": WALLET})
    outer, inner = decode_multicall(plan["calls"][0], PM_ABI)
    check("withdraw 100% of a position with no liquidity left but fees: collect and burn, no decreaseLiquidity",
          [n for n, _ in inner] == ["collect", "burn"], str([n for n, _ in inner]))
    raises("withdraw 50% of a position with no liquidity left points to collect_fees",
           lambda: tools["withdraw_from_position"].invoke({"token_id": 107, "percent": 50, "from_address": WALLET}), "collect_fees")
    raises("an empty position (no liquidity, no fees) has nothing to withdraw",
           lambda: tools["withdraw_from_position"].invoke({"token_id": 104, "percent": 100, "from_address": WALLET}), "already empty")
    raises("a percent that rounds to zero liquidity is refused",
           lambda: tools["withdraw_from_position"].invoke({"token_id": 102, "percent": 1e-15, "from_address": WALLET}), "too little liquidity")
    raises("0% is refused", lambda: tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 0, "from_address": WALLET}), "above 0")
    raises("more than 100% is refused", lambda: tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 101, "from_address": WALLET}), "at most 100")
    raises("someone else's position is refused", lambda: tools["withdraw_from_position"].invoke({"token_id": 201, "percent": 50, "from_address": WALLET}), "is owned by")
    raises("a position that does not exist is refused", lambda: tools["withdraw_from_position"].invoke({"token_id": 999, "percent": 50, "from_address": WALLET}), "does not exist")

    # -- collect_fees --
    plan = tools["collect_fees"].invoke({"token_id": 101, "from_address": WALLET})
    check("collect_fees: one multicall with role collect, no approval", roles(plan) == ["collect"] and len(plan["calls"]) == 1)
    outer, inner = decode_multicall(plan["calls"][0], PM_ABI)
    check("collect_fees: multicall([collect]) paying the wallet", [n for n, _ in inner] == ["collect"]
          and inner[0][1]["params"] == {"tokenId": 101, "recipient": WALLET, "amount0Max": 2**128 - 1, "amount1Max": 2**128 - 1}
          and plan["calls"][0]["to"] == w.pm and plan["calls"][0]["value"] == 0)
    s = plan["summary"]
    check("collect_fees: the summary has exactly the contract's keys, and the fees", list(s) == ["token_id", "token0", "token1", "fees0", "fees1"]
          and s["fees0"] == fees0 / 10**d0 and s["fees1"] == fees1 / 10**d1 and (s["token0"], s["token1"]) == (t0, t1), str(s))
    check("collect_fees: the action line says what it collects", "Collect" in plan["calls"][0]["description"] and "unclaimed fees" in plan["calls"][0]["description"])
    plan = tools["collect_fees"].invoke({"token_id": 101, "from_address": WALLET, "native_out": True, "recipient": RECIPIENT})
    outer, inner = decode_multicall(plan["calls"][0], PM_ABI)
    check("collect_fees native_out: [collect to address(0), unwrapWETH9(0, to), sweepToken(other, 0, to)]",
          [n for n, _ in inner] == ["collect", "unwrapWETH9", "sweepToken"] and inner[0][1]["params"]["recipient"] == "0x" + "00" * 20
          and inner[1][1] == {"amountMinimum": 0, "recipient": RECIPIENT} and inner[2][1] == {"token": A["USDC"], "amountMinimum": 0, "recipient": RECIPIENT}, str(inner))
    plan_lower = tools["collect_fees"].invoke({"token_id": 101, "from_address": WALLET.lower()})
    check("a lowercase from_address owns the position all the same (addresses are compared checksummed)",
          plan_lower["summary"]["fees0"] == fees0 / 10**d0 and
          tools["withdraw_from_position"].invoke({"token_id": 101, "percent": 10, "from_address": WALLET.lower()})["summary"]["token_id"] == 101)
    raises("collect_fees on a position with no fees is refused", lambda: tools["collect_fees"].invoke({"token_id": 102, "from_address": WALLET}), "no fees")
    raises("collect_fees on someone else's position is refused", lambda: tools["collect_fees"].invoke({"token_id": 201, "from_address": WALLET}), "is owned by")
    raises("collect_fees on a position that does not exist is refused", lambda: tools["collect_fees"].invoke({"token_id": 999, "from_address": WALLET}), "does not exist")
    raises("native_out on a pair with no WETH is refused", lambda: _no_weth_position(chain_id, collect=True), "wrapped native")
    raises("... for a withdrawal too", lambda: _no_weth_position(chain_id, collect=False), "wrapped native")


def _no_weth_position(chain_id, *, collect: bool):
    """native_out on a DAI/USDC position: neither token is WETH, so there is nothing to unwrap."""
    w = World(chain_id)
    dai, usdc = w.addr["DAI"], w.addr["USDC"]
    t0, t1 = (dai, usdc) if int(dai, 16) < int(usdc, 16) else (usdc, dai)
    w.positions[301] = dict(owner=WALLET, token0=t0, token1=t1, fee=100, tick_lower=-100, tick_upper=100, liquidity=10**15, owed0=0, owed1=0, fees0=10, fees1=10)
    tools = w.tools()
    if collect:
        return tools["collect_fees"].invoke({"token_id": 301, "from_address": WALLET, "native_out": True})
    return tools["withdraw_from_position"].invoke({"token_id": 301, "percent": 50, "from_address": WALLET, "native_out": True})


# ── get_positions / get_position ──────────────────────────────────────────────


def test_positions(chain_id: int):
    w = World(chain_id)
    tools = w.tools()
    A = w.addr
    print(f"get_positions and get_position (chain {chain_id})")
    pool = w.pool(A["WETH"], A["USDC"], 500)
    t0, t1 = pool["token0"], pool["token1"]
    weth_is_token0 = t0 == A["WETH"]
    d0, d1 = w.decimals[t0], w.decimals[t1]

    rows = tools["get_positions"].invoke({"owner_address": WALLET})
    by_id = {r["token_id"]: r for r in rows}
    check("every readable position of the wallet is listed, the spam one and the other owner's left out",
          sorted(by_id) == [101, 102, 103, 104, 106, 107, 108, 109], str(sorted(by_id)))
    row = by_id[101]
    check("a row has exactly the contract's keys",
          list(row) == ["token_id", "owner", "token0", "token1", "decimals0", "decimals1", "fee", "fee_pct", "pool", "tick_lower", "tick_upper", "tick",
                        "liquidity", "base_token", "quote_token", "min_price", "max_price", "full_range", "current_price", "in_range", "amount0", "amount1",
                        "amount0_raw", "amount1_raw", "fees0", "fees1", "fees0_raw", "fees1_raw", "empty"], str(list(row)))
    p = w.positions[101]
    check("row 101: identity", row["owner"] == WALLET and (row["token0"], row["token1"]) == (t0, t1) and (row["decimals0"], row["decimals1"]) == (d0, d1)
          and row["fee"] == 500 and row["fee_pct"] == 0.05 and row["pool"] == pool["address"], str(row))
    check("row 101: ticks and the pool's tick", (row["tick_lower"], row["tick_upper"], row["tick"]) == (p["tick_lower"], p["tick_upper"], pool["tick"]))
    check("row 101: liquidity is a string", row["liquidity"] == str(p["liquidity"]) and isinstance(row["liquidity"], str))
    check("row 101: ETH is the base, USDC the quote", (row["base_token"], row["quote_token"]) == (A["WETH"], A["USDC"]))
    check("row 101: the current price is the pool's", abs(row["current_price"] - 3000.0) / 3000.0 < 1e-9)
    low = m.human_price(1.0001 ** p["tick_lower"], weth_is_token0, d0, d1)
    high = m.human_price(1.0001 ** p["tick_upper"], weth_is_token0, d0, d1)
    check("row 101: min and max are the ticks' prices in quote per base, whichever token sorts first",
          abs(row["min_price"] - min(low, high)) < 1e-6 * row["min_price"] and abs(row["max_price"] - max(low, high)) < 1e-6 * row["max_price"], f"{row['min_price']} {row['max_price']} {low} {high}")
    check("row 101: in range, not full range", row["in_range"] is True and row["full_range"] is False)
    sa, sb = m.get_sqrt_ratio_at_tick(p["tick_lower"]), m.get_sqrt_ratio_at_tick(p["tick_upper"])
    amounts = m.amounts_for_liquidity(pool["sqrt_price"], sa, sb, p["liquidity"])
    check("row 101: amounts are what the liquidity holds, raw as strings and whole as floats",
          (row["amount0_raw"], row["amount1_raw"]) == (str(amounts[0]), str(amounts[1])) and row["amount0"] == amounts[0] / 10**d0 and row["amount1"] == amounts[1] / 10**d1)
    check("row 101: unclaimed fees come from the simulated collect", (row["fees0_raw"], row["fees1_raw"]) == (str(p["fees0"]), str(p["fees1"]))
          and row["fees0"] == p["fees0"] / 10**d0 and row["fees1"] == p["fees1"] / 10**d1)
    check("row 101: not empty", row["empty"] is False)
    check("a range whose upper tick IS the pool's tick is out of range (the pool treats tick >= upper as above it); "
          "one whose lower tick is the pool's tick is in range",
          by_id[108]["in_range"] is False and by_id[109]["in_range"] is True and by_id[108]["tick"] == by_id[108]["tick_upper"]
          and by_id[109]["tick"] == by_id[109]["tick_lower"], str((by_id[108]["in_range"], by_id[109]["in_range"])))
    check("row 102 (above the price): out of range", by_id[102]["in_range"] is False and by_id[102]["tick"] < by_id[102]["tick_lower"])
    check("row 102: it holds only token0, no fees", by_id[102]["amount1_raw"] == "0" and by_id[102]["amount0_raw"] != "0" and by_id[102]["fees0_raw"] == "0")
    full = by_id[103]
    check("row 103 (full range): min 0.0, max None, full_range", full["min_price"] == 0.0 and full["max_price"] is None and full["full_range"] is True and full["in_range"] is True)
    check("row 104 (nothing left): empty", by_id[104]["empty"] is True and by_id[104]["liquidity"] == "0" and by_id[104]["amount0_raw"] == "0")
    check("row 107 (no liquidity, fees waiting): not empty", by_id[107]["empty"] is False and by_id[107]["liquidity"] == "0" and by_id[107]["fees1_raw"] == str(10**15))
    link = by_id[106]
    check("row 106 (LINK/WETH): LINK is the base, WETH the quote", (link["base_token"], link["quote_token"]) == (A["LINK"], w.weth))
    check("row 106: price is WETH per LINK", abs(link["current_price"] - 0.005) < 1e-12 and link["min_price"] < 0.005 < link["max_price"], str(link))
    check("row 106: fees use the simulated collect, not the booked amounts", link["fees0_raw"] == str(2 * 10**15) and link["fees1_raw"] == str(3 * 10**14))

    one = tools["get_position"].invoke({"token_id": 101})
    check("get_position returns the same row as get_positions", one == row, str(one))
    one = tools["get_position"].invoke({"token_id": 101, "owner_address": WALLET})
    check("get_position with the owner named is the same", one == row)
    check("get_position works for someone else's position when no owner is named", tools["get_position"].invoke({"token_id": 201})["owner"] == OTHER)
    raises("get_position with the wrong owner named is refused", lambda: tools["get_position"].invoke({"token_id": 101, "owner_address": OTHER}), "is owned by")
    raises("get_position for a position that does not exist is refused", lambda: tools["get_position"].invoke({"token_id": 999}), "does not exist")
    raises("get_position for the spam position raises (a single id is not swallowed)", lambda: tools["get_position"].invoke({"token_id": 105}), "decimals")

    check("an address with no positions gets an empty list", tools["get_positions"].invoke({"owner_address": RECIPIENT}) == [])
    check("limit caps how many are read", len(tools["get_positions"].invoke({"owner_address": WALLET, "limit": 2})) == 2)
    ids = lambda **kw: [r["token_id"] for r in tools["get_positions"].invoke({"owner_address": WALLET, **kw})]
    check("tokens keeps only positions of those tokens that still hold something (104 empty, 105/106/108/109 other tokens)",
          ids(tokens=[A["WETH"], A["USDC"]]) == [101, 102, 103, 107], str(ids(tokens=[A["WETH"], A["USDC"]])))
    check("... with limit counting only the positions kept", ids(tokens=[A["WETH"], A["USDC"]], limit=2) == [101, 102])
    check("... so positions listed later aren't crowded out by the ones skipped",
          ids(tokens=[A["DAI"], A["USDC"]], limit=2) == [108, 109] and ids(limit=2) == [101, 102],
          str(ids(tokens=[A["DAI"], A["USDC"]], limit=2)))
    check("... and addresses in any case are accepted", ids(tokens=[A["DAI"].lower(), A["USDC"].lower()]) == [108, 109])
    check("... and no known tokens means no positions", ids(tokens=[]) == [])
    raises("a malformed token in tokens is refused",
           lambda: tools["get_positions"].invoke({"owner_address": WALLET, "tokens": ["nope"]}), "not a valid address")
    raises("a limit below 1 is refused", lambda: tools["get_positions"].invoke({"owner_address": WALLET, "limit": 0}), "at least 1")
    raises("a malformed owner is refused", lambda: tools["get_positions"].invoke({"owner_address": "nope"}), "not a valid address")

    # The reads are batched: a few rounds, not a few per position
    w2 = World(chain_id, latency=0.01)
    w2.tools()["get_positions"].invoke({"owner_address": WALLET})
    check("the position reads overlap (many eth_calls in flight at once)", w2.chain.max_inflight >= 4, str(w2.chain.max_inflight))

    # When the fee simulation is refused, the booked fees stand in
    w3 = World(chain_id)
    w3.chain.on(w3.pm, "collect", reverting)
    rows = w3.tools()["get_positions"].invoke({"owner_address": WALLET})
    row = {r["token_id"]: r for r in rows}[106]
    check("if collect cannot be simulated, the fees the position manager has booked are shown", (row["fees0_raw"], row["fees1_raw"]) == (str(10**15), str(10**14)), str(row))
    raises("... but collect_fees refuses rather than guess", lambda: w3.tools()["collect_fees"].invoke({"token_id": 106, "from_address": WALLET}), "would revert")


def test_concurrent_callers():
    """Many tool calls at once share the read pool: all finish, none deadlocks, none sees another's data."""
    print("concurrent callers")
    from concurrent.futures import ThreadPoolExecutor
    w = World(8453, latency=0.003)
    tools = w.tools()
    A = w.addr
    expected_quote = tools["get_quote_out"].invoke({"token_in": A["LINK"], "token_out": A["USDC"], "amount_in": 100.0})
    expected_rows = tools["get_positions"].invoke({"owner_address": WALLET})

    def work(i):
        if i % 2:
            return tools["get_quote_out"].invoke({"token_in": A["LINK"], "token_out": A["USDC"], "amount_in": 100.0}) == expected_quote
        return tools["get_positions"].invoke({"owner_address": WALLET}) == expected_rows

    with ThreadPoolExecutor(max_workers=24) as callers:
        futures = [callers.submit(work, i) for i in range(48)]
        results = [f.result(timeout=120) for f in futures]
    check("48 overlapping quotes and position lists all finish with the same answers", all(results))


def main():
    test_math_against_solidity()
    test_address_table()
    test_orientation()
    test_ticks_and_prices()
    test_deposit_amounts()
    test_deposit_minimums()
    test_range_options()
    test_formatting()
    test_constructor()
    test_tool_names()
    test_tool_signatures()
    test_concurrent_callers()
    for chain_id in (8453, 1):      # Base sorts WETH first; mainnet sorts USDC first
        test_routing(chain_id)
        test_swaps(chain_id)
        test_suggest(chain_id)
        test_open_position(chain_id)
        test_add_to_position(chain_id)
        test_withdraw_and_collect(chain_id)
        test_positions(chain_id)
    finish("All Uniswap V3 toolkit checks passed.")


if __name__ == "__main__":
    main()
