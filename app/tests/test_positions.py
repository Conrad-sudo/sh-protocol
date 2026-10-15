"""
The Uniswap V3 positions reader, offline: which positions are shown (known tokens only, nothing
empty), how each is named (app tickers, the native coin as itself, the base token first), which
chain it reads, that a failure shows as no positions, and the short cache.

No chain: the V3 toolkit's get_positions is a stand-in that returns scripted rows shaped like the
real ones (langchain_uniswap_v3.toolkit._position_row). Runs against a throwaway database.

Run: make positions-test   (or: python app/tests/test_positions.py)
"""
import logging
import os
import tempfile
import time

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

# A scratch database, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()

import db                                         # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

from web3 import Web3                             # noqa: E402

import positions                                  # noqa: E402
from constants import get_v3_network              # noqa: E402
from network_config import network_name           # noqa: E402
from seed_data import CHAINS                      # noqa: E402

SEPOLIA, ARBITRUM, BASE, BSC = 11155111, 42161, 8453, 56


def _addr(n: int) -> str:
    return Web3.to_checksum_address(f"0x{n:040x}")


WALLET = _addr(0xA11CE)
USDC = _addr(0x05DC)
LINK = _addr(0x0111)
PEPE = _addr(0x9E9E)
SPAM = _addr(0x5BA3)
ARB_USDC = _addr(0xA05DC)
POOL = _addr(0x9001)
WETH = get_v3_network(SEPOLIA)["native_wrapped"]      # 0xfFf9..., above every address here
ARB_WETH = get_v3_network(ARBITRUM)["native_wrapped"]

_conn = db.get_db()
_conn.executemany("INSERT OR REPLACE INTO chains (name, chain_id) VALUES (?, ?)", list(CHAINS.items()))
_conn.executemany(
    "INSERT OR REPLACE INTO supported_tokens (chain_id, ticker, address) VALUES (?, ?, ?)",
    # Arbitrum's list lacks WETH on purpose: the wrapped native token is known all the same.
    [(SEPOLIA, "usdc", USDC), (SEPOLIA, "link", LINK), (SEPOLIA, "weth", WETH), (ARBITRUM, "usdc", ARB_USDC)],
)
_conn.commit()


def make_user(chains=(SEPOLIA, ARBITRUM, BSC), saved="sepolia") -> int:
    """An account with a wallet on each of `chains`, last deployed on `saved`."""
    user_id = db.create_user()
    for chain_id in chains:
        db.save_wallet_address(user_id, chain_id, WALLET)
    db.save_user_network(user_id, saved)
    return user_id


def toolkit_row(token_id: int, token0: str, token1: str, base: str, *, amounts=(0, 0), fees=(0, 0),
                decimals=(18, 18), empty=False, **overrides) -> dict:
    """A get_positions row, as the V3 toolkit builds one: token0 is the lower address."""
    row = {
        "token_id": token_id, "owner": WALLET, "token0": token0, "token1": token1,
        "decimals0": decimals[0], "decimals1": decimals[1], "fee": 500, "fee_pct": 0.05, "pool": POOL,
        "tick_lower": -600, "tick_upper": 600, "tick": 0, "liquidity": "0" if empty else "123456",
        "base_token": base, "quote_token": token1 if base == token0 else token0,
        "min_price": 2850.0, "max_price": 3150.0, "full_range": False, "current_price": 3000.0, "in_range": True,
        "amount0": amounts[0] / 10 ** decimals[0], "amount1": amounts[1] / 10 ** decimals[1],
        "amount0_raw": str(amounts[0]), "amount1_raw": str(amounts[1]),
        "fees0": fees[0] / 10 ** decimals[0], "fees1": fees[1] / 10 ** decimals[1],
        "fees0_raw": str(fees[0]), "fees1_raw": str(fees[1]), "empty": empty,
    }
    return row | overrides


class FakeGetPositions:
    """The toolkit's get_positions tool: records each call, with the network it ran on."""

    def __init__(self):
        self.rows: list[dict] = []
        self.error: Exception | None = None
        self.during = None   # run inside the read, as if something happened while it was in flight
        self.calls: list[dict] = []

    def invoke(self, args: dict):
        self.calls.append(args)
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return list(self.rows)


tool = FakeGetPositions()
# The network each toolkit was built for: what load_network_config answers inside get_uniswap_tools.
built: list[tuple[int, str]] = []
tools_returned = {"get_positions": tool}


def fake_get_uniswap_tools(user_id: int) -> dict:
    built.append((user_id, db.get_user_network(user_id)))
    return tools_returned


positions.get_uniswap_tools = fake_get_uniswap_tools


def reset(rows=()):
    positions._cache.clear()
    tool.rows, tool.error, tool.during = list(rows), None, None
    tool.calls.clear()
    built.clear()


# ETH/USDC: USDC (token0) is the stablecoin, so it is the quote and WETH (token1) the base.
ETH_USDC = toolkit_row(
    11, USDC, WETH, WETH, decimals=(6, 18), amounts=(1_500 * 10**6, 2 * 10**18), fees=(3 * 10**6, 10**15),
)
# LINK/WETH: no stablecoin, so WETH is the quote and LINK (token0) the base.
LINK_ETH = toolkit_row(
    12, LINK, WETH, LINK, amounts=(40 * 10**18, 10**17), min_price=0.004, max_price=0.006, current_price=0.0049,
)


def test_row_shape():
    print("\n[1] each position is named by app tickers, the native coin as itself, the base token first")
    user = make_user()
    reset([ETH_USDC, LINK_ETH])
    rows = positions.read_positions(user, SEPOLIA)
    check("both positions are shown", [r["id"] for r in rows] == [11, 12], str(rows))
    check("the row is exactly what the dashboard reads", rows and rows[0] == {
        "id": 11,
        "pair": "eth/usdc",
        "base": "eth",
        "quote": "usdc",
        "fee_pct": 0.05,
        "min_price": 2850.0,
        "max_price": 3150.0,
        "full_range": False,
        "current_price": 3000.0,
        "in_range": True,
        # The base is token1 here, so its amounts come first.
        "amounts": [{"ticker": "eth", "decimals": 18, "raw": str(2 * 10**18)},
                    {"ticker": "usdc", "decimals": 6, "raw": str(1_500 * 10**6)}],
        "fees": [{"ticker": "eth", "decimals": 18, "raw": str(10**15)},
                 {"ticker": "usdc", "decimals": 6, "raw": str(3 * 10**6)}],
    }, str(rows[:1]))
    link = rows[1] if len(rows) > 1 else {}
    check("WETH is named as the native coin, never as weth",
          link.get("pair") == "link/eth" and link.get("quote") == "eth"
          and "weth" not in str(rows), str(link))
    check("a base that is token0 keeps token0's amounts first",
          [a["ticker"] for a in link.get("amounts", [])] == ["link", "eth"]
          and link["amounts"][0]["raw"] == str(40 * 10**18), str(link.get("amounts")))
    asked = tool.calls[0] if tool.calls else {}
    check("the toolkit is asked for this wallet, at most POSITIONS_LIMIT positions",
          len(tool.calls) == 1 and asked.get("owner_address") == WALLET
          and asked.get("limit") == positions.POSITIONS_LIMIT and positions.POSITIONS_LIMIT == 20,
          str(tool.calls))
    check("... of the tokens the user knows only, so junk can't crowd real positions out of the limit",
          {USDC.lower(), WETH.lower()} <= set(asked.get("tokens", [])) and SPAM.lower() not in asked.get("tokens", []),
          str(asked.get("tokens")))


def test_spam_and_empty_are_hidden():
    print("\n[2] only positions holding something, in tokens the user knows, are shown")
    user = make_user()
    db.save_custom_token(user, SEPOLIA, PEPE, "pepe", "Pepe", 18)
    spam = toolkit_row(21, SPAM, WETH, SPAM, amounts=(10**24, 0))
    spam_quote = toolkit_row(22, USDC, SPAM, SPAM, amounts=(0, 10**24))
    added = toolkit_row(23, PEPE, WETH, PEPE, amounts=(10**21, 10**16))
    empty = toolkit_row(24, USDC, WETH, WETH, decimals=(6, 18), empty=True)
    # Taken out but not collected: no liquidity, fees still owed. Still worth showing.
    fees_only = toolkit_row(25, USDC, WETH, WETH, decimals=(6, 18), fees=(5 * 10**6, 0), liquidity="0")
    reset([spam, spam_quote, added, empty, fees_only])
    rows = positions.read_positions(user, SEPOLIA)
    ids = [r["id"] for r in rows]
    check("a position with a token the user doesn't know is hidden, whichever side it is on",
          21 not in ids and 22 not in ids, str(ids))
    check("a token the user added counts as known", 23 in ids
          and next(r for r in rows if r["id"] == 23)["pair"] == "pepe/eth", str(rows))
    check("a position with nothing in it and nothing earned is hidden", 24 not in ids, str(ids))
    check("one with only fees left is shown", 25 in ids, str(ids))

    other = make_user()   # never added PEPE
    reset([added])
    check("another user, who never added the token, doesn't see it", positions.read_positions(other, SEPOLIA) == [])

    reset([toolkit_row(26, ARB_USDC, ARB_WETH, ARB_WETH, decimals=(6, 18), amounts=(10**6, 10**15))])
    rows = positions.read_positions(user, ARBITRUM)
    check("the wrapped native token is known even where the token list lacks it",
          [r["pair"] for r in rows] == ["eth/usdc"], str(rows))

    reset([toolkit_row(27, USDC, WETH, WETH, decimals=(6, 18), amounts=(1, 1), current_price=float("inf"))])
    check("a price no JSON can carry hides the row instead of breaking the page",
          positions.read_positions(user, SEPOLIA) == [])


def test_which_chain():
    print("\n[3] the read is of the chain asked for, and nothing at all where there are no V3 positions")
    user = make_user(saved="sepolia")
    reset([ETH_USDC])
    check("no chain: the user's current one", [r["id"] for r in positions.read_positions(user)] == [11])
    check("built for the network they act on, not switched", built == [(user, "sepolia")], str(built))

    reset([toolkit_row(31, ARB_USDC, ARB_WETH, ARB_WETH, decimals=(6, 18), amounts=(1, 1))])
    rows = positions.read_positions(user, ARBITRUM)
    check("another chain: read on this server's network for it",
          [r["id"] for r in rows] == [31] and built == [(user, network_name(ARBITRUM))], str(built))
    check("and the user's own network is left as it was", db.get_user_network(user) == "sepolia")

    reset([ETH_USDC])
    check("BSC trades on PancakeSwap V2: no positions", positions.read_positions(user, BSC) == [])
    check("Celo has no exchange: none", positions.read_positions(user, 42220) == [])
    check("a chain without a wallet: none", positions.read_positions(user, BASE) == [])
    bsc_user = make_user(chains=(BSC,), saved="bsc")
    check("a user acting on BSC: none", positions.read_positions(bsc_user) == [])
    check("and the toolkit was never asked", tool.calls == [] and built == [], f"{tool.calls} {built}")


def test_failures_show_as_nothing():
    print("\n[4] a failed read is logged without the RPC's URL and shows as no positions")
    user = make_user()
    secret_url = "https://eth-sepolia.g.alchemy.com/v2/SECRETKEY1234567890abcdef"
    logged: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    handler = Collect()
    positions.logger.addHandler(handler)
    try:
        reset([ETH_USDC])
        tool.error = ConnectionError(f"Max retries exceeded with url: {secret_url}")
        try:
            rows = positions.read_positions(user, SEPOLIA)
            raised = None
        except Exception as e:   # noqa: BLE001
            rows, raised = None, e
        check("it doesn't raise, and shows nothing", raised is None and rows == [], repr(raised))
        check("it is logged", len(logged) == 1 and "ConnectionError" in logged[0], str(logged))
        check("with neither the key nor the URL", logged and "SECRETKEY" not in logged[0]
              and "https://" not in logged[0], str(logged))

        tool.error = None
        check("a failed read isn't kept: the next one goes to the chain",
              [r["id"] for r in positions.read_positions(user, SEPOLIA)] == [11] and len(tool.calls) == 2,
              str(tool.calls))

        reset([ETH_USDC])
        tools_returned.pop("get_positions")
        try:
            check("a toolkit without get_positions (the V2 one) shows nothing",
                  positions.read_positions(user, SEPOLIA) == [])
        finally:
            tools_returned["get_positions"] = tool

        reset([ETH_USDC])
        tool.error = RuntimeError("node down")
        future = positions.start_positions_read(user, SEPOLIA)
        check("the dashboard's overlapped read never raises either", future.result(timeout=5) == [])

        reset([ETH_USDC])
        tool.error = ConnectionError(f"Max retries exceeded with url: {secret_url}")
        try:
            positions.read_positions(user, SEPOLIA, strict=True)
            raised = None
        except positions.PositionsReadError as e:
            raised = e
        check("strict (the assistant's read) raises instead of answering no positions",
              raised is not None and "Couldn't read" in str(raised), repr(raised))
        check("... with a message that carries neither the key nor the URL",
              raised is not None and "SECRETKEY" not in str(raised) and "https://" not in str(raised), str(raised))
        tool.error = None
    finally:
        positions.logger.removeHandler(handler)


def test_overlapped_read():
    print("\n[5] the dashboard's overlapped read answers what read_positions does")
    user = make_user()
    reset([ETH_USDC, LINK_ETH])
    future = positions.start_positions_read(user, SEPOLIA)
    rows = future.result(timeout=5)
    check("the same rows", [r["id"] for r in rows] == [11, 12], str(rows))
    other = make_user(saved="arbitrum")
    reset([ETH_USDC])
    future = positions.start_positions_read(other, SEPOLIA)
    check("on the chain asked for, from a worker thread",
          [r["id"] for r in future.result(timeout=5)] == [11] and built == [(other, network_name(SEPOLIA))],
          str(built))
    check("on BSC, nothing", positions.start_positions_read(user, BSC).result(timeout=5) == [])


def test_cache():
    print("\n[6] positions are reused for a short while, and dropped when a transaction lands")
    user = make_user()
    reset([ETH_USDC])
    first = positions.read_positions(user, SEPOLIA)
    second = positions.read_positions(user, SEPOLIA)
    check("a second read within the TTL doesn't touch the chain",
          first == second and len(tool.calls) == 1, str(tool.calls))
    check("the TTL suits the dashboard's 60-second refetch", 0 < positions.CACHE_TTL_SECS < 60)

    positions.read_positions(user, ARBITRUM)
    check("each chain is kept apart", len(tool.calls) == 2, str(tool.calls))

    # A read is filtered by the tokens the user knows, so it is only reused while they still do.
    adder = make_user()
    reset([toolkit_row(61, PEPE, WETH, PEPE, amounts=(1, 1))])
    check("before a token is known, its position is hidden",
          positions.read_positions(adder, SEPOLIA) == [] and len(tool.calls) == 1)
    db.save_custom_token(adder, SEPOLIA, PEPE, "pepe", "Pepe", 18)
    check("once added, it shows straight away: the read filtered without it isn't reused",
          [r["id"] for r in positions.read_positions(adder, SEPOLIA)] == [61] and len(tool.calls) == 2
          and PEPE.lower() in tool.calls[1]["tokens"], str(tool.calls))

    reset([ETH_USDC])
    positions.read_positions(user, SEPOLIA)
    positions.forget(SEPOLIA, WALLET.lower())
    positions.read_positions(user, SEPOLIA)
    check("forget() drops them, whatever the address's case", len(tool.calls) == 2, str(tool.calls))

    key = positions._key(SEPOLIA, WALLET)
    positions._cache[key] = (time.monotonic() - 1, *positions._cache[key][1:])
    positions.read_positions(user, SEPOLIA)
    check("once the TTL is up they are read again", len(tool.calls) == 3, str(tool.calls))

    reset([ETH_USDC])
    tool.during = lambda: positions.forget(SEPOLIA, WALLET)   # a transaction lands mid-read
    positions.read_positions(user, SEPOLIA)
    tool.during = None
    positions.read_positions(user, SEPOLIA)
    check("a read under way when a transaction lands isn't kept", len(tool.calls) == 2, str(tool.calls))


if __name__ == "__main__":
    try:
        test_row_shape()
        test_spam_and_empty_are_hidden()
        test_which_chain()
        test_failures_show_as_nothing()
        test_overlapped_read()
        test_cache()
    finally:
        os.unlink(_tmp_db.name)

    finish("All positions reader checks passed.")
