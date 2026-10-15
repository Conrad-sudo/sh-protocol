"""
Offline checks for the assistant's Uniswap V3 liquidity tools (tools/positions.py) and the toolset
each chain gets: what the tools refuse before building anything, that a picked range hands the
toolkit the suggested option's exact prices and fee, what a deposit counts toward the spending limit,
how the quotes read, and which liquidity tools the model is shown on each chain.

The toolkit's tools, the positions reader and the quoting step are stand-ins and the database is a
throwaway file, so this runs anywhere. The toolkit itself is test_uniswap_v3; the real journey on a
fork is test_e2e_fork.

Run: make position-tools-test   (or: python app/tests/test_position_tools.py)
"""
import asyncio
import os
import tempfile
from types import SimpleNamespace

from checks import check, finish, set_everywhere   # first: it puts app/ on sys.path for the imports below

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
# smart_wallet_agent builds its model client at import; nothing here ever calls it.
os.environ.setdefault("DEEPSEEK_API_KEY", "offline-test-placeholder")

import db                                   # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

from langchain.agents.middleware import ModelRequest  # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402
from web3 import Web3                       # noqa: E402

import smart_wallet_agent                   # noqa: E402
import tools                                # noqa: E402
import tools.positions                      # noqa: E402
import tools.uniswap                        # noqa: E402
from agent_context import AgentContext      # noqa: E402
from positions import PositionsReadError    # noqa: E402
from tools.common import SENT               # noqa: E402

SEPOLIA, BSC, CELO = 11155111, 56, 42220
NETWORKS = {SEPOLIA: "sepolia-fork", BSC: "bsc-fork", CELO: "celo"}


def _addr(n: int) -> str:
    return Web3.to_checksum_address(f"0x{n:040x}")


WALLET = _addr(0xA11CE)
STRANGER = _addr(0x5BAD)
USDC = _addr(0x05DC)
DAI = _addr(0x0DA1)
WETH = _addr(0x0E7E)
WBNB = _addr(0x0B4B)
PEPE = _addr(0x9E9E)    # added by the user
SHIB = _addr(0x5B1B)    # known to nobody
POOL = _addr(0x9001)    # ETH/USDC 0.05%
OTHER_POOL = _addr(0x9002)

_db = db.get_db()
for chain_id, ticker, address in (
    (SEPOLIA, "usdc", USDC), (SEPOLIA, "dai", DAI), (SEPOLIA, "weth", WETH),
    (BSC, "usdc", USDC), (BSC, "wbnb", WBNB),
):
    _db.execute("INSERT OR REPLACE INTO supported_tokens (chain_id, ticker, address) VALUES (?, ?, ?)",
                (chain_id, ticker, address))
for chain_id, name in NETWORKS.items():
    _db.execute("INSERT OR REPLACE INTO chains (name, chain_id) VALUES (?, ?)", (name, chain_id))
_db.commit()

UID = db.create_user()
db.save_custom_token(UID, SEPOLIA, PEPE, "pepe", "Pepe", 18)
RUNTIME = SimpleNamespace(context=SimpleNamespace(user_id=UID, turn_id=1))

# What the toolkit's suggest_price_ranges answers for ETH/USDC: prices in USDC per ETH, each band's
# ends the prices of the ticks it rounds to (deliberately not round numbers).
OPTIONS = [
    {"name": "narrow", "width_pct": 5.0, "full_range": False, "min_price": 2849.87312, "max_price": 3150.21877,
     "tick_lower": -199_000, "tick_upper": -198_000, "base_share_pct": 50.1, "quote_share_pct": 49.9},
    {"name": "medium", "width_pct": 15.0, "full_range": False, "min_price": 2549.5021, "max_price": 3449.98763,
     "tick_lower": -200_100, "tick_upper": -197_100, "base_share_pct": 50.3, "quote_share_pct": 49.7},
    {"name": "wide", "width_pct": 50.0, "full_range": False, "min_price": 1499.91, "max_price": 4500.4402,
     "tick_lower": -205_400, "tick_upper": -194_400, "base_share_pct": 52.0, "quote_share_pct": 48.0},
    {"name": "full", "width_pct": None, "full_range": True, "min_price": 0.0, "max_price": None,
     "tick_lower": -887_270, "tick_upper": 887_270, "base_share_pct": 50.0, "quote_share_pct": 50.0},
]


def _position(token0, token1, owner=WALLET, base=None, **extra) -> dict:
    """A row like the toolkit's get_position."""
    row = {
        "token_id": 0, "owner": owner, "token0": token0, "token1": token1, "fee": 500, "fee_pct": 0.05,
        "base_token": base or token1, "quote_token": token0 if (base or token1) == token1 else token1,
        "min_price": 2849.87312, "max_price": 3150.21877, "full_range": False, "current_price": 3000.0,
        "in_range": True, "amount0": 1.0, "amount1": 1.0, "fees0": 0.5, "fees1": 0.0002,
    }
    return {**row, **extra}


class FakeV3:
    """
    Stands in for the V3 toolkit's tools: records what each was asked and answers as the toolkit
    would, including its refusals for a position that doesn't exist or isn't the caller's.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.positions = {
            7: _position(USDC, WETH),                      # USDC is token0, WETH token1: ETH/USDC
            8: _position(WETH, PEPE, base=PEPE),           # built from a token the user added
            9: _position(USDC, WETH, owner=STRANGER),      # someone else's
            10: _position(SHIB, WETH),                     # a token nobody knows: spam
        }
        self.ticks = None   # set to make open_position land on other ticks than asked
        self.refusal = None  # set to make the next write refuse with this text
        self.current = 3000.0

    def __getitem__(self, name):
        def invoke(args):
            self.calls.append((name, dict(args)))
            if self.refusal and name not in ("get_position", "suggest_price_ranges"):
                raise ToolException(self.refusal)
            return getattr(self, name)(**args)
        return SimpleNamespace(invoke=invoke)

    def called(self, name) -> list[dict]:
        return [args for called, args in self.calls if called == name]

    @staticmethod
    def _plan(summary):
        return {"calls": [{"to": POOL, "value": 0, "data": "0x", "role": "add_liquidity", "description": "x"}],
                "transactions": None, "chain_id": SEPOLIA, "summary": summary}

    def suggest_price_ranges(self, token_a, token_b, fee=None):
        pair = {token_a, token_b}
        return {
            "pool": POOL if pair == {USDC, WETH} else OTHER_POOL, "fee": fee or 500, "fee_pct": (fee or 500) / 10_000,
            "base_token": WETH if WETH in pair else min(pair), "quote_token": USDC if USDC in pair else max(pair),
            "current_price": self.current, "stable_pair": False, "options": [dict(o) for o in OPTIONS],
        }

    def open_position(self, token_a, token_b, amount_a, from_address, min_price=None, max_price=None,
                      full_range=False, fee=None, use_native=False, slippage_bps=50):
        # The toolkit's own rule: the suggested prices land on the suggested ticks.
        ticks = next(((o["tick_lower"], o["tick_upper"]) for o in OPTIONS
                      if (full_range and o["full_range"]) or (o["min_price"], o["max_price"]) == (min_price, max_price)),
                     (-1, 1))
        low, high = (0.0, None) if full_range else (min_price, max_price)
        return self._plan({
            "pool": POOL if {token_a, token_b} == {USDC, WETH} and (fee or 500) == 500 else OTHER_POOL,
            "fee": fee or 500, "fee_pct": (fee or 500) / 10_000, "base_token": WETH, "quote_token": USDC,
            "tick_lower": (self.ticks or ticks)[0], "tick_upper": (self.ticks or ticks)[1],
            "min_price": low, "max_price": high, "full_range": full_range, "current_price": self.current,
            "in_range": full_range or low <= self.current < high, "token_a": token_a, "token_b": token_b,
            "amount_a": amount_a, "amount_b": 0.0331, "amount_a_min": amount_a * 0.995, "amount_b_min": 0.0329,
            "native_value": 0.0331 if use_native else 0.0,
        })

    def get_position(self, token_id, owner_address=None):
        if token_id not in self.positions:
            raise ToolException(f"Position #{token_id} does not exist.")
        row = {**self.positions[token_id], "token_id": token_id}
        if owner_address is not None and row["owner"] != owner_address:
            raise ToolException(f"Position #{token_id} is owned by {row['owner']}, not by {owner_address}.")
        return row

    def add_to_position(self, token_id, token_a, amount_a, from_address, use_native=False, slippage_bps=50):
        row = self.positions[token_id]
        return self._plan({
            "token_id": token_id, "token_a": token_a,
            "token_b": row["token1"] if token_a == row["token0"] else row["token0"],
            "amount_a": amount_a, "amount_b": 0.0166, "amount_a_min": amount_a * 0.995, "amount_b_min": 0.0165,
            "native_value": 0.0166 if use_native else 0.0, "base_token": row["base_token"],
            "quote_token": row["quote_token"], "min_price": row["min_price"], "max_price": row["max_price"],
            "full_range": row["full_range"], "in_range": row["in_range"],
        })

    def withdraw_from_position(self, token_id, percent, from_address, native_out=False, slippage_bps=50):
        row = self.positions[token_id]
        return self._plan({
            "token_id": token_id, "percent": percent, "token0": row["token0"], "token1": row["token1"],
            "expected0": 1500.0 * percent / 100, "expected1": 0.5 * percent / 100,
            "amount0_min": 1492.5 * percent / 100, "amount1_min": 0.4975 * percent / 100,
            "fees0": 3.2, "fees1": 0.001, "burns": percent == 100,
        })

    def collect_fees(self, token_id, from_address, native_out=False):
        row = self.positions[token_id]
        return self._plan({"token_id": token_id, "token0": row["token0"], "token1": row["token1"],
                           "fees0": 3.2, "fees1": 0.001})


state = {"chain": SEPOLIA, "v3": FakeV3(), "quotes": [], "positions": [], "read": []}


def _quote_plan(runtime, plan, details="", legs=None, lp_pool=None):
    """Stands in for tools.quoting._quote_plan: records what would be priced and parked."""
    state["quotes"].append({"plan": plan, "details": details, "legs": legs, "lp_pool": lp_pool})
    return {"status": "NOT SENT", "details": details}


def _read_positions(user_id, chain_id=None, *, strict=False):
    state["read"].append((user_id, chain_id))
    state["strict"] = strict
    if isinstance(state["positions"], Exception):
        raise state["positions"]
    return state["positions"]


_ORIGINALS = {
    name: set_everywhere(tools, name, value) for name, value in (
        ("load_network_config", lambda _uid: (None, state["chain"], NETWORKS[state["chain"]])),
        ("load_session_handler", lambda _uid: SimpleNamespace(address=WALLET)),
        ("get_uniswap_tools", lambda _uid: state["v3"]),
        ("_quote_plan", _quote_plan),
        ("read_positions", _read_positions),
    )
}


def _fresh(chain: int = SEPOLIA) -> FakeV3:
    """A new stand-in toolkit, no cached range options, acting on `chain`."""
    state["chain"], state["v3"] = chain, FakeV3()
    state["quotes"].clear()
    tools.positions._range_options.clear()
    return state["v3"]


def attempt(tool, **kwargs) -> str:
    """The refusal's text, or "ok" if the tool returned."""
    try:
        tool.func(RUNTIME, **kwargs)
    except ToolException as e:
        return str(e)
    return "ok"


P = tools.positions
WRITES = ("open_position", "add_to_position", "withdraw_from_position", "collect_fees")


def test_refusals():
    print("\n[1] what the position tools refuse before building anything")
    v3 = _fresh()
    for label, tool, kwargs in (
        ("suggest_price_ranges", P.suggest_price_ranges, {"token_a": SHIB}),
        ("open_position, as token_a", P.open_position, {"token_a": SHIB, "amount_a": 1, "min_price": 1, "max_price": 2}),
        ("open_position, as token_b", P.open_position,
         {"token_a": "usdc", "amount_a": 1, "token_b": "shib", "min_price": 1, "max_price": 2}),
        ("add_to_position", P.add_to_position, {"position_id": 7, "token": SHIB, "amount": 1}),
    ):
        outcome = attempt(tool, **kwargs)
        check(f"{label}: a token nobody added is refused", "not a token this wallet knows" in outcome, outcome[:160])
    check("...without asking the toolkit for anything", not v3.calls, str(v3.calls))

    for label, tool, kwargs in (
        ("open_position", P.open_position, {"token_a": "usdc", "amount_a": 1, "min_price": 1, "max_price": 2}),
        ("add_to_position", P.add_to_position, {"position_id": 7, "token": "usdc", "amount": 1}),
        ("withdraw_from_position", P.withdraw_from_position, {"position_id": 7, "percent": 50}),
    ):
        outcome = attempt(tool, **kwargs, slippage_bps=1201)
        check(f"{label}: a slippage over 12% is refused", "outside what this wallet allows" in outcome, outcome[:160])
    check("...before anything is read", not v3.calls, str(v3.calls))

    outcome = attempt(P.open_position, token_a="usdc", amount_a=1, token_b="usdc", min_price=1, max_price=2)
    check("a position in a token with itself is refused", "two different tokens" in outcome, outcome[:160])
    outcome = attempt(P.suggest_price_ranges, token_a="weth", token_b="eth")
    check("ETH and WETH are one token in a pool too", "same token here" in outcome, outcome[:160])

    outcome = attempt(P.open_position, token_a="eth", amount_a=1, token_b="usdc", range="narrow")
    check("open_position: the native amount can't be the fixed one",
          "Name how much USDC to deposit first" in outcome and "Nothing was quoted" in outcome, outcome[:200])
    outcome = attempt(P.add_to_position, position_id=7, token="eth", amount=1)
    check("add_to_position: nor there", "Name how much of the position's other token" in outcome, outcome[:200])

    outcome = attempt(P.add_to_position, position_id=7, token="dai", amount=1)
    check("add_to_position: a token that isn't one of the position's is refused",
          "isn't one of position #7's tokens (USDC/ETH)" in outcome, outcome[:200])

    # Ownership and spam.
    outcome = attempt(P.withdraw_from_position, position_id=9, percent=50)
    check("a position another address owns is refused, in plain words",
          "is owned by" in outcome and "not by this wallet" in outcome and "get_positions" in outcome
          and WALLET not in outcome, outcome[:240])
    outcome = attempt(P.collect_fees, position_id=99)
    check("one that doesn't exist says so", "does not exist" in outcome and "get_positions" in outcome, outcome[:200])
    for tool, kwargs in ((P.add_to_position, {"token": "eth", "amount": 1}),
                         (P.add_to_position, {"token": "weth", "amount": 1}),
                         (P.withdraw_from_position, {"percent": 50}),
                         (P.collect_fees, {})):
        outcome = attempt(tool, position_id=10, **kwargs)
        if "Name how much" in outcome:   # the native refusal comes first, and is right too
            continue
        check(f"{tool.name}: a position built from an unknown token is treated as spam",
              "treated as spam" in outcome and SHIB in outcome and "Add token" in outcome, outcome[:240])
    check("...and nothing is built for any of them", not any(v3.called(name) for name in WRITES), str(v3.calls))
    outcome = attempt(P.withdraw_from_position, position_id=8, percent=50)
    check("a position built from a token the user added is theirs to take out", outcome == "ok", outcome[:200])


def test_wrong_chain():
    print("\n[2] each exchange's liquidity tools refuse on a chain that trades on the other, or on none")
    v2_tools = [
        (tools.uniswap.add_liquidity, {"token_a": "usdc", "amount_a": 1}),
        (tools.uniswap.remove_liquidity, {"token_a": "usdc", "lp_amount": 1}),
        (tools.uniswap.get_pool_quote, {"token_a": "usdc", "token_b": "eth", "amount_a": 1}),
        (tools.uniswap.get_lp_amounts, {"token_a": "usdc", "token_b": "eth", "lp_amount": 1}),
        (tools.uniswap.get_liquidity_token_balance, {"token_a": "usdc"}),
    ]
    v3_tools = [
        (P.suggest_price_ranges, {"token_a": "usdc"}),
        (P.open_position, {"token_a": "usdc", "amount_a": 1, "range": "narrow"}),
        (P.add_to_position, {"position_id": 7, "token": "usdc", "amount": 1}),
        (P.get_positions, {}),
        (P.withdraw_from_position, {"position_id": 7, "percent": 50}),
        (P.collect_fees, {"position_id": 7}),
    ]
    v3 = _fresh(SEPOLIA)
    for tool, kwargs in v2_tools:
        outcome = attempt(tool, **kwargs)
        check(f"Sepolia: {tool.name} refuses, pointing at the V3 tools and the dashboard's Withdraw",
              "trades on Uniswap V3" in outcome and "suggest_price_ranges" in outcome
              and "Dashboard -> Withdraw" in outcome, outcome[:240])
    v3 = _fresh(BSC)
    for tool, kwargs in v3_tools:
        outcome = attempt(tool, **kwargs)
        check(f"BSC: {tool.name} refuses, pointing at add_liquidity",
              "trades on PancakeSwap V2" in outcome and "add_liquidity" in outcome, outcome[:240])
    v3 = _fresh(CELO)
    for tool, kwargs in v2_tools + v3_tools:
        outcome = attempt(tool, **kwargs)
        check(f"Celo: {tool.name} refuses: no exchange there", "no exchange on Celo" in outcome, outcome[:200])
    check("none of them reached the toolkit or a quote", not v3.calls and not state["quotes"], str(v3.calls))
    check("...nor the positions reader", not state["read"], str(state["read"]))


def test_range_cache():
    print("\n[3] a picked range is the suggested option, exactly, and only for the pair it was suggested for")
    v3 = _fresh()
    outcome = attempt(P.open_position, token_a="usdc", amount_a=100, range="narrow")
    check("a range name with no suggestion is refused, saying to suggest first",
          "call suggest_price_ranges" in outcome and "Nothing was quoted" in outcome, outcome[:200])

    shown = P.suggest_price_ranges.func(RUNTIME, token_a="usdc")
    check("the suggestion reads as the user would: the pair and today's price",
          shown["pair"] == "ETH/USDC" and shown["current_price"] == "3,000 USDC per ETH" and shown["pool_fee"] == "0.05%",
          str({k: shown[k] for k in ("pair", "current_price", "pool_fee")}))
    check("...each option's prices, width and split",
          shown["options"][0] == {"range": "narrow", "prices": "2,849.87 – 3,150.22 USDC per ETH",
                                  "width": "±5% around today's price", "deposit_split": "50.1% ETH, 49.9% USDC"}
          and shown["options"][3]["prices"] == "the full range: every price", str(shown["options"]))
    check("...and no address anywhere in it", not any(a in str(shown) for a in (USDC, WETH, POOL)), str(shown)[:200])
    check("the toolkit was asked about USDC and WETH, the native asset's pool",
          v3.called("suggest_price_ranges") == [{"token_a": USDC, "token_b": WETH, "fee": None}],
          str(v3.called("suggest_price_ranges")))

    for name in ("narrow", "medium", "wide"):
        option = next(o for o in OPTIONS if o["name"] == name)
        P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, range=name)
        args = v3.called("open_position")[-1]
        check(f"'{name}': the toolkit gets the option's exact prices and its pool's fee",
              (args["min_price"], args["max_price"], args["full_range"], args["fee"])
              == (option["min_price"], option["max_price"], False, 500), str(args))
    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, range="full")
    args = v3.called("open_position")[-1]
    check("'full': the full range, at that pool's fee",
          (args["min_price"], args["max_price"], args["full_range"], args["fee"]) == (None, None, True, 500), str(args))
    check("...the native asset paid as itself", args["use_native"] is True and args["token_b"] == WETH, str(args))
    P.open_position.func(RUNTIME, token_b="usdc", amount_a=100, token_a="weth", range="narrow")
    check("a pick works whichever way round the pair is named, and WETH is paid as WETH",
          v3.called("open_position")[-1]["use_native"] is False, str(v3.called("open_position")[-1]))

    for label, kwargs, expected in (
        ("a range for another pair", {"token_a": "dai", "amount_a": 100, "range": "narrow"}, "call suggest_price_ranges"),
        ("a range for a pool at another fee", {"token_a": "usdc", "amount_a": 100, "range": "narrow", "fee": 3000},
         "worked out for the 0.05% pool"),
        ("a range and bounds together", {"token_a": "usdc", "amount_a": 100, "range": "narrow", "min_price": 1,
                                         "max_price": 2}, "not both"),
        ("neither a range nor bounds", {"token_a": "usdc", "amount_a": 100}, "Say which range"),
        ("only one bound", {"token_a": "usdc", "amount_a": 100, "min_price": 1}, "Say which range"),
    ):
        outcome = attempt(P.open_position, **kwargs)
        check(f"{label} is refused", expected in outcome and "Nothing was quoted" in outcome, outcome[:200])

    v3.ticks = (-1, 1)
    outcome = attempt(P.open_position, token_a="usdc", amount_a=100, range="narrow")
    check("a pick the toolkit would put on other ticks is refused", "no longer matches" in outcome, outcome[:200])
    v3.ticks = None

    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, min_price=2900, max_price=3100)
    args = v3.called("open_position")[-1]
    check("the user's own bounds go through as given, in the suggested pool",
          (args["min_price"], args["max_price"], args["full_range"], args["fee"]) == (2900, 3100, False, 500), str(args))
    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, min_price=2900, max_price=3100, fee=3000)
    check("...or in the pool the user named", v3.called("open_position")[-1]["fee"] == 3000)

    P.suggest_price_ranges.func(RUNTIME, token_a="dai")
    outcome = attempt(P.open_position, token_a="usdc", amount_a=100, range="narrow")
    check("only the last suggestion counts: ETH/USDC's options are gone once DAI's are shown",
          "call suggest_price_ranges" in outcome, outcome[:200])
    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, min_price=2900, max_price=3100)
    check("...and the user's own bounds for ETH/USDC fall back to the deepest pool",
          v3.called("open_position")[-1]["fee"] is None, str(v3.called("open_position")[-1]))

    P.suggest_price_ranges.func(RUNTIME, token_a="usdc")
    key = (UID, SEPOLIA)
    cached = P._range_options[key]
    P._range_options[key] = P._RangeOptions(cached.pair, cached.pool, cached.fee, cached.options, expires=0.0)
    outcome = attempt(P.open_position, token_a="usdc", amount_a=100, range="narrow")
    check("options older than 15 minutes are refused, asking for today's",
          "more than 15 minutes old" in outcome, outcome[:200])

    _fresh(SEPOLIA)
    P.suggest_price_ranges.func(RUNTIME, token_a="usdc")
    tools.positions._range_options[(UID + 1, SEPOLIA)] = tools.positions._range_options.pop((UID, SEPOLIA))
    outcome = attempt(P.open_position, token_a="usdc", amount_a=100, range="narrow")
    check("one user's options are never another's", "call suggest_price_ranges" in outcome, outcome[:200])


def test_quotes():
    print("\n[4] what each write counts toward the limit, and how its quote reads")
    v3 = _fresh()
    P.suggest_price_ranges.func(RUNTIME, token_a="usdc")
    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, range="narrow")
    quoted = state["quotes"][-1]
    check("a deposit counts both tokens as sent: the position that comes back has no price",
          quoted["legs"] == [("usdc", 100, SENT), ("eth", 0.0331, SENT)], str(quoted["legs"]))
    check("...and names no LP pool: positions are read off the chain", quoted["lp_pool"] is None)
    details = quoted["details"]
    for label, text in (
        ("the range, as the user reads it", "Range: 2,849.87 – 3,150.22 USDC per ETH (the narrow option)"),
        ("today's price, inside the range", "ETH is at 3,000 USDC now, inside the range"),
        ("both amounts and their minimums", "USDC deposited: 100 (at least 99.5), ETH deposited: about 0.0331 (at least 0.0329)"),
        ("the slippage", "slippage tolerance 0.5%"),
        ("that fees aren't compounded", "not added to the position"),
    ):
        check(f"open_position's details: {label}", text in details, details)
    check("...and no address", not any(a in details for a in (USDC, WETH, WALLET)), details)

    v3.current = 3300.0
    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, min_price=2900, max_price=3100)
    details = state["quotes"][-1]["details"]
    check("a range the price is above: out of range, holding only USDC, earning nothing",
          "outside the range, so the position holds only USDC and earns no fees" in details
          and "(the user's own bounds)" in details, details)
    v3.current = 2700.0
    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, min_price=2900, max_price=3100)
    check("...one it is below: holding only ETH", "holds only ETH" in state["quotes"][-1]["details"],
          state["quotes"][-1]["details"])
    v3.current = 3000.0

    P.open_position.func(RUNTIME, token_a="usdc", amount_a=100, token_b="weth", min_price=2900, max_price=3100)
    check("a deposit of WETH counts WETH, not the native asset",
          state["quotes"][-1]["legs"] == [("usdc", 100, SENT), ("weth", 0.0331, SENT)], str(state["quotes"][-1]["legs"]))

    P.add_to_position.func(RUNTIME, position_id=7, token="usdc", amount=50)
    quoted, args = state["quotes"][-1], v3.called("add_to_position")[-1]
    check("add_to_position pays the position's WETH side as ETH", args["use_native"] is True and args["token_a"] == USDC,
          str(args))
    check("...and counts both deposits", quoted["legs"] == [("usdc", 50, SENT), ("eth", 0.0166, SENT)], str(quoted["legs"]))
    check("...its details name the position, its range and both amounts",
          "position #7 (ETH/USDC)" in quoted["details"] and "2,849.87 – 3,150.22 USDC per ETH" in quoted["details"]
          and "ETH deposited: about 0.0166" in quoted["details"], quoted["details"])
    P.add_to_position.func(RUNTIME, position_id=7, token="usdc", amount=50, wrapped=True)
    quoted, args = state["quotes"][-1], v3.called("add_to_position")[-1]
    check("...or as WETH when asked", args["use_native"] is False and quoted["legs"][1] == (WETH, 0.0166, SENT)
          and "WETH deposited" in quoted["details"], str(quoted["legs"]))

    P.withdraw_from_position.func(RUNTIME, position_id=7, percent=50)
    quoted, args = state["quotes"][-1], v3.called("withdraw_from_position")[-1]
    check("a withdrawal counts nothing: value only comes back", quoted["legs"] is None, str(quoted["legs"]))
    check("...its ETH side comes back as ETH", args["native_out"] is True and args["from_address"] == WALLET, str(args))
    check("...its details: the share, both amounts with their minimums, and the fees, base first",
          "Takes 50% of position #7 (ETH/USDC) out: about 0.25 ETH and 750 USDC (at least 0.24875 ETH and 746.25 USDC), "
          "plus the fees it has earned, 0.001 ETH and 3.2 USDC" in quoted["details"]
          and "burned" not in quoted["details"], quoted["details"])
    P.withdraw_from_position.func(RUNTIME, position_id=7, percent=100, wrapped=True)
    quoted, args = state["quotes"][-1], v3.called("withdraw_from_position")[-1]
    check("...all of it closes the position, and WETH comes back as WETH when asked",
          "burned" in quoted["details"] and args["native_out"] is False and "WETH" in quoted["details"], quoted["details"])

    P.collect_fees.func(RUNTIME, position_id=7)
    quoted, args = state["quotes"][-1], v3.called("collect_fees")[-1]
    check("collecting fees counts nothing, and pays ETH as ETH", quoted["legs"] is None and args["native_out"] is True,
          str(args))
    check("...its details say what is collected", "0.001 ETH and 3.2 USDC" in quoted["details"]
          and "stays as it is" in quoted["details"], quoted["details"])

    v3.refusal = f"{WALLET} needs 0.5 of {WETH} for this deposit but only holds 0.1 -- short by 0.4."
    outcome = attempt(P.open_position, token_a="usdc", amount_a=100, range="narrow")
    check("the toolkit's own refusals name tokens and the wallet as the user knows them",
          outcome == "this wallet needs 0.5 of ETH for this deposit but only holds 0.1 -- short by 0.4.", outcome)


def test_get_positions():
    print("\n[5] get_positions shows the dashboard's rows, readably")
    _fresh()
    state["read"].clear()
    state["positions"] = [{
        "id": 7, "pair": "eth/usdc", "base": "eth", "quote": "usdc", "fee_pct": 0.05,
        "min_price": 2849.87312, "max_price": 3150.21877, "full_range": False, "current_price": 3420.5,
        "in_range": False,
        "amounts": [{"ticker": "eth", "decimals": 18, "raw": "0"}, {"ticker": "usdc", "decimals": 6, "raw": "1500250000"}],
        "fees": [{"ticker": "eth", "decimals": 18, "raw": str(10**15)}, {"ticker": "usdc", "decimals": 6, "raw": "3200000"}],
    }]
    shown = P.get_positions.func(RUNTIME)
    check("it reads the positions of the chain the turn acts on", state["read"] == [(UID, SEPOLIA)], str(state["read"]))
    row = shown["positions"][0]
    check("each row: id, pair, fee, range and today's price",
          (row["position_id"], row["pair"], row["pool_fee"], row["range"], row["current_price"])
          == (7, "ETH/USDC", "0.05%", "2,849.87 – 3,150.22 USDC per ETH", "3,420.5 USDC per ETH"), str(row))
    check("...whether it is in range, and what that means",
          row["in_range"] is False and "outside the range, so the position holds only USDC" in row["status"], row["status"])
    check("...what it holds and what it has earned",
          row["holds"] == "0 ETH + 1,500.25 USDC" and row["unclaimed_fees"] == "0.001 ETH + 3.2 USDC", str(row))
    check("...and a note on ranges and fees", "earns no fees" in shown["note"] and "collect_fees" in shown["note"])
    state["positions"] = []
    shown = P.get_positions.func(RUNTIME)
    check("no positions says so", shown == {"positions": [], "note": "No positions found for this wallet on Sepolia."},
          str(shown))
    check("the read is strict: a failed read is never shown as no positions", state.get("strict") is True)
    state["positions"] = PositionsReadError("Couldn't read this wallet's Uniswap V3 positions right now.")
    refusal = attempt(P.get_positions)
    check("a failed read is reported as one", refusal.startswith("Couldn't read"), refusal)
    state["positions"] = []


def test_toolset_per_chain():
    print("\n[6] the model is shown only its chain's liquidity tools")
    every = tools.get_tools()
    names = {t.name for t in every}
    v3, v2 = tools.LIQUIDITY_TOOLS["uniswap-v3"], tools.LIQUIDITY_TOOLS["pancakeswap-v2"]
    check("every name in LIQUIDITY_TOOLS is a registered tool", (v3 | v2) <= names, str((v3 | v2) - names))
    check("the six V3 tools are registered",
          v3 == {"suggest_price_ranges", "open_position", "add_to_position", "get_positions",
                 "withdraw_from_position", "collect_fees"}, str(sorted(v3)))
    check("no name is in both sets: the tool node needs them unique", not (v3 & v2))
    others = names - v3 - v2

    original = smart_wallet_agent.load_network_config
    try:
        for chain_id, expected in ((1, v3), (11155111, v3), (42161, v3), (8453, v3), (56, v2), (42220, set()),
                                   (31337, set())):
            smart_wallet_agent.load_network_config = lambda _uid, c=chain_id: (None, c, "x")
            shown = {t.name for t in smart_wallet_agent._tools_for_chain(every, UID)}
            check(f"chain {chain_id}: {'its liquidity tools' if expected else 'no liquidity tools'}, and every other tool",
                  shown == others | expected, str(sorted(shown ^ (others | expected))))

        def no_network(_uid):
            raise ValueError("No network configured")
        smart_wallet_agent.load_network_config = no_network
        shown = {t.name for t in smart_wallet_agent._tools_for_chain(every, UID)}
        check("a user with no network gets no liquidity tools", shown == others, str(sorted(shown ^ others)))

        # Through the middleware itself, both ways the agent is run.
        smart_wallet_agent.load_network_config = lambda _uid: (None, 56, "bsc-fork")
        middleware = smart_wallet_agent.ChainLiquidityTools()
        request = ModelRequest(model=None, messages=[], tools=every,
                               runtime=SimpleNamespace(context=AgentContext(user_id=UID, turn_id=1)))
        seen = {}

        def handler(req):
            seen["sync"] = {t.name for t in req.tools}
            return "response"

        async def ahandler(req):
            seen["async"] = {t.name for t in req.tools}
            return "response"

        result = middleware.wrap_model_call(request, handler)
        asyncio.run(middleware.awrap_model_call(request, ahandler))
        check("the middleware narrows the model's tools when the agent is invoked",
              result == "response" and seen.get("sync") == others | v2, str(sorted(seen.get("sync", set()) ^ (others | v2))))
        check("...and when it is awaited", seen.get("async") == others | v2)
        check("...without changing the request it was given", len(request.tools) == len(every))
    finally:
        smart_wallet_agent.load_network_config = original

    captured = {}
    create_agent = smart_wallet_agent.create_agent
    smart_wallet_agent.create_agent = lambda **kwargs: captured.update(kwargs)
    try:
        smart_wallet_agent.init_agent()
    finally:
        smart_wallet_agent.create_agent = create_agent
    check("the agent is built with every tool and the per-chain filter",
          len(captured.get("tools", [])) == len(every)
          and any(isinstance(m, smart_wallet_agent.ChainLiquidityTools) for m in captured.get("middleware", [])),
          str(captured.get("middleware")))


def test_toolkit_wiring():
    print("\n[7] get_uniswap_tools builds the V3 toolkit on a V3 chain and the V2 one on BSC, once per user and chain")
    import toolkits
    from constants import get_router, get_v3_network

    built = []

    class StandIn:
        """Records how a toolkit was built; its tools are just names."""
        names: tuple = ()

        def __init__(self, **kwargs):
            self.kwargs, self.w3 = kwargs, SimpleNamespace(provider="its own")
            built.append(self)

        def get_tools(self):
            return [SimpleNamespace(name=name) for name in self.names]

    class V3(StandIn):
        names = ("swap_exact_tokens_for_tokens", "suggest_price_ranges", "get_positions", "get_position")

    class V2(StandIn):
        names = ("swap_exact_tokens_for_tokens", "add_liquidity", "approve_token")

    app_w3 = SimpleNamespace(provider=SimpleNamespace(endpoint_uri="http://node.invalid"))
    chain = {"id": SEPOLIA}
    db.save_user_network(UID, NETWORKS[BSC])   # what V2's token map reads the listed tokens by
    original = toolkits.load_network_config, toolkits.UniswapV3Toolkit, toolkits.UniswapV2Toolkit
    toolkits.load_network_config = lambda _uid: (app_w3, chain["id"], NETWORKS[chain["id"]])
    toolkits.UniswapV3Toolkit, toolkits.UniswapV2Toolkit = V3, V2
    toolkits.invalidate_toolkits(UID)
    try:
        found = toolkits.get_uniswap_tools(UID)
        v3 = built[-1]
        network = get_v3_network(SEPOLIA)
        check("Sepolia gets the Uniswap V3 toolkit, with every tool the dashboard and the agent use",
              isinstance(v3, V3) and set(found) == set(V3.names), str(sorted(found)))
        check("...bound to the chain's own SwapRouter02, position manager, quoter and WETH, over the app's RPC",
              {k: v3.kwargs[k] for k in ("swap_router_address", "position_manager_address", "quoter_address",
                                         "native_wrapped_address", "rpc_url")}
              == {"swap_router_address": network["swap_router"], "position_manager_address": network["position_manager"],
                  "quoter_address": network["quoter"], "native_wrapped_address": network["native_wrapped"],
                  "rpc_url": "http://node.invalid"}, str(v3.kwargs))
        check("...in calls mode, resetting what an approval leaves",
              v3.kwargs["tx_mode"] == "calls" and v3.kwargs["reset_residual_approvals"] is True, str(v3.kwargs))
        check("...with the chain's listed stablecoins: USDC and DAI, not WETH and not the user's PEPE",
              sorted(v3.kwargs["stable_tokens"]) == sorted([USDC, DAI]), str(v3.kwargs["stable_tokens"]))
        check("...sharing the app's provider", v3.w3.provider is app_w3.provider)
        toolkits.get_uniswap_tools(UID)
        check("...and built once", len(built) == 1, str(len(built)))

        chain["id"] = BSC
        found = toolkits.get_uniswap_tools(UID)
        v2 = built[-1]
        check("BSC gets the V2 toolkit on PancakeSwap's router, as before",
              isinstance(v2, V2) and v2.kwargs["router_address"] == get_router(BSC)
              and v2.kwargs["native_wrapped_address"] == WBNB, str(v2.kwargs))
        check("...with the standing-approval tools withheld", "approve_token" not in found, str(sorted(found)))

        toolkits.invalidate_toolkits(UID)
        chain["id"] = SEPOLIA
        toolkits.get_uniswap_tools(UID)
        check("invalidating the user's toolkits drops the V3 one too", len(built) == 3 and isinstance(built[-1], V3),
              str(len(built)))
    finally:
        toolkits.load_network_config, toolkits.UniswapV3Toolkit, toolkits.UniswapV2Toolkit = original
        toolkits.invalidate_toolkits(UID)


if __name__ == "__main__":
    try:
        test_refusals()
        test_wrong_chain()
        test_range_cache()
        test_quotes()
        test_get_positions()
        test_toolset_per_chain()
        test_toolkit_wiring()
    finally:
        for name, value in _ORIGINALS.items():
            set_everywhere(tools, name, value)
        os.unlink(_tmp_db.name)

    finish("All position-tool checks passed.")
