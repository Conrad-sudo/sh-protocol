"""
The wallet's Uniswap V3 positions, read one way for everyone who shows them: the dashboard, the
assistant's get_positions and the Telegram bots' daily check.

A position is an NFT the position manager holds a deposit under, so it is not a balance: what it
holds, where its price range sits and the fees it has earned all come from the chain, through the
V3 toolkit's get_positions (toolkits.get_uniswap_tools). This module adds what the app needs on
top of that:

  - Only positions worth showing. Anyone can mint a position to any address, built from tokens of
    their choosing, so a position is shown only when the user knows both of its tokens -- the rule
    the History tab hides unknown tokens by (tx_history._known_tokens): listed on that chain, or
    added by the user. A position that holds nothing and has earned nothing is left out too: one
    taken out without being burned would otherwise sit on the dashboard forever.
  - App tickers, never addresses: "eth/usdc", the native coin named as itself rather than as WETH,
    the same way the dashboard names balances and LP tokens.
  - A short cache, because each read costs about three to five RPC calls per position.

Never raises. A failed read is logged and shows as no positions: the dashboard and the daily check
both have other things to show, and must not fail for this.
"""
import logging
import math
import re
import threading
import time
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable

from constants import (
    get_exchange,
    get_native_asset_ticker,
    get_native_wrapped_ticker,
    get_v3_network,
)
from db import (
    acting_network,
    get_chain_id_from_name,
    get_custom_tokens,
    get_supported_tokens_by_chain_id,
    get_user_network,
    get_wallet_address,
)
from network_config import network_name
from parallel import start_task
from redact import redact
from toolkits import get_uniswap_tools

logger = logging.getLogger(__name__)

# The most positions shown per wallet. Each costs three to five RPC calls (its id, the position, what
# collect would pay, and its pool's price the first time that pool is seen), so twenty is already a
# read of up to ~100 calls, in about four rounds. The toolkit counts only positions of tokens the
# user knows that still hold something (its `tokens` filter, decided on the two cheap reads), so
# positions someone else sent the wallet can't crowd the real ones out of the twenty.
POSITIONS_LIMIT = 20

# How long a wallet's positions are reused before they are read again. The dashboard refetches the
# wallet every 60 seconds (web/src/hooks/useWallet.ts), so a poll always sees positions at most this
# old, while a burst of reads -- a refetch when the tab regains focus, a second tab, the assistant's
# get_positions just after the page loaded, a daily check followed by a chat -- shares one. A
# transaction the assistant sends drops them at once (forget), so its own changes show straight away.
# Per process: the API and the Telegram bots each keep their own.
CACHE_TTL_SECS = 30

# (chain_id, lowercased wallet address) -> (monotonic expiry, the token addresses the read was
# filtered by, the toolkit's rows). A read filtered by other tokens -- the user has added one since
# -- is not reused, so a position of the new token shows straight away.
_cache: dict[tuple[int, str], tuple[float, frozenset[str], list[dict]]] = {}
# Bumped by every forget(). A read that was already under way when a transaction landed would
# otherwise store what the wallet held BEFORE it, for the next CACHE_TTL_SECS.
_epoch = 0
_lock = threading.Lock()


@dataclass(frozen=True)
class _Request:
    """Everything a read needs from the database, gathered first on the caller's thread."""
    user_id: int
    chain_id: int
    account: str
    # The network to make the acting one for the read, or None when the user's requests already act
    # on chain_id (see _network_for).
    network: str | None
    # Lowercased address -> app ticker, for every token the user knows on chain_id (_known_tokens).
    known: dict[str, str]


def _key(chain_id: int, account: str) -> tuple[int, str]:
    return chain_id, account.lower()


def forget(chain_id: int, account: str) -> None:
    """
    Drops the cached positions of `account` on `chain_id`, so the next read goes to the chain.

    Called once a transaction from the wallet has landed: a deposit, a withdrawal or a fee
    collection changes its positions, and the dashboard reads the wallet again as soon as the chat
    turn ends -- it must not be shown what the wallet held before.
    """
    global _epoch
    with _lock:
        _cache.pop(_key(chain_id, account), None)
        _epoch += 1


def _network_for(user_id: int, chain_id: int) -> str | None:
    """
    The network to read `chain_id` on, or None when the user's requests already act on it.

    The toolkit is built for the chain load_network_config answers for the user -- the acting
    network of a chat turn or a bot, else the one they last deployed on -- so a read of any other
    chain makes this server's network for it the acting one, as the Telegram bots do.
    """
    current = get_user_network(user_id)
    if current is not None and get_chain_id_from_name(current) == chain_id:
        return None
    return network_name(chain_id)


def _known_tokens(user_id: int, chain_id: int) -> dict[str, str]:
    """
    Lowercased address -> ticker, for the tokens the user knows on `chain_id`: the ones Mitfah lists
    and the ones they added -- the tokens the History tab shows (tx_history._known_tokens). Listed
    tokens win, as in db.resolve_token.

    The wrapped native token is named as the native coin ("eth", never "weth"), as the dashboard
    names the native side of a pool: the assistant puts native coin in and takes it back out, and the
    wrapping is a detail of the pool. It is known even where the token list somehow lacks it.
    """
    native = get_native_asset_ticker(chain_id).lower()
    wrapped = get_native_wrapped_ticker(chain_id)
    known: dict[str, str] = {}
    for token in get_supported_tokens_by_chain_id(chain_id):
        known[token["address"].lower()] = native if token["ticker"] == wrapped else token["ticker"]
    for token in get_custom_tokens(user_id, chain_id):
        known.setdefault(token["address"].lower(), token["ticker"])
    network = get_v3_network(chain_id)
    if network is not None:
        known[network["native_wrapped"].lower()] = native
    return known


def _request(user_id: int, chain_id: int | None) -> _Request | None:
    """
    The database half of a read, or None when there is nothing to read: a chain that doesn't trade
    on Uniswap V3 (BSC, Celo) or one the user has no wallet on.

    @param chain_id  The chain to read, or None for the user's current one.
    """
    if chain_id is None:
        current = get_user_network(user_id)
        chain_id = get_chain_id_from_name(current) if current else None
    if chain_id is None or get_exchange(chain_id) != "uniswap-v3":
        return None
    try:
        account = get_wallet_address(user_id, chain_id)
    except ValueError:
        return None
    return _Request(
        user_id, chain_id, account, _network_for(user_id, chain_id), _known_tokens(user_id, chain_id)
    )


def _cached(request: _Request) -> list[dict] | None:
    """The toolkit's rows for this wallet if they were read in the last CACHE_TTL_SECS, else None."""
    with _lock:
        hit = _cache.get(_key(request.chain_id, request.account))
    if hit is None or hit[0] <= time.monotonic() or hit[1] != frozenset(request.known):
        return None
    return hit[2]


def _read_chain(request: _Request) -> list[dict]:
    """
    The chain half of a read: the toolkit's rows for the wallet, which are then kept for
    CACHE_TTL_SECS -- unless a transaction landed while they were being read (forget).
    """
    with _lock:
        started = _epoch
    acting = (
        acting_network(request.user_id, request.network, request.chain_id) if request.network else nullcontext()
    )
    with acting:
        rows = get_uniswap_tools(request.user_id)["get_positions"].invoke(
            {"owner_address": request.account, "limit": POSITIONS_LIMIT, "tokens": sorted(request.known)}
        )
    with _lock:
        if _epoch == started:
            _cache[_key(request.chain_id, request.account)] = (
                time.monotonic() + CACHE_TTL_SECS, frozenset(request.known), rows
            )
    return rows


def _finite(*prices: float | None) -> bool:
    """False for a price no JSON can carry. Only a token with absurd decimals produces one."""
    return all(price is None or math.isfinite(price) for price in prices)


def _shown(request: _Request, rows: list[dict]) -> list[dict]:
    """
    The toolkit's rows as the app shows them: only the positions that hold or have earned something
    and whose two tokens the user knows, each named by app tickers with the base token first.

    The toolkit already decides which token is the base and which the quote (prices are quote per
    base: USDC per ETH, WETH per LINK); token0 and token1 are just the pool's address order, so
    either can be the base, and the amounts follow the base.
    """
    shown = []
    for row in rows:
        tickers = (request.known.get(row["token0"].lower()), request.known.get(row["token1"].lower()))
        if row["empty"] or None in tickers:
            continue
        if not _finite(row["min_price"], row["max_price"], row["current_price"]):
            continue
        sides = [
            (tickers[0], row["decimals0"], row["amount0_raw"], row["fees0_raw"]),
            (tickers[1], row["decimals1"], row["amount1_raw"], row["fees1_raw"]),
        ]
        if row["base_token"] != row["token0"]:
            sides.reverse()
        base, quote = sides[0][0], sides[1][0]
        shown.append({
            "id": int(row["token_id"]),
            "pair": f"{base}/{quote}",
            "base": base,
            "quote": quote,
            "fee_pct": row["fee_pct"],
            "min_price": row["min_price"],
            "max_price": row["max_price"],
            "full_range": row["full_range"],
            "current_price": row["current_price"],
            "in_range": row["in_range"],
            # Raw amounts are strings: a uint256 doesn't survive JSON's float64.
            "amounts": [{"ticker": t, "decimals": d, "raw": str(amount)} for t, d, amount, _ in sides],
            "fees": [{"ticker": t, "decimals": d, "raw": str(fees)} for t, d, _, fees in sides],
        })
    return shown


def _log_failure(user_id: int, chain_id: int | None, error: Exception) -> None:
    # Masked, and no URL at all: web3's errors quote the RPC's URL, key and all.
    text = re.sub(r"https?://\S+", "<rpc>", redact(repr(error)))
    logger.warning("Could not read the Uniswap V3 positions of user %s on chain %s: %s", user_id, chain_id, text)


class PositionsReadError(Exception):
    """A strict read failed. Its message is safe to show: no URL, no key, no web3 detail."""


def _failed(user_id: int, chain_id: int | None, error: Exception, strict: bool) -> list[dict]:
    _log_failure(user_id, chain_id, error)
    if strict:
        raise PositionsReadError(
            "Couldn't read this wallet's Uniswap V3 positions right now. Try again in a moment."
        ) from error
    return []


def _begin(user_id: int, chain_id: int | None, strict: bool = False) -> Callable[[], list[dict]]:
    """
    Starts a read: the database half now, on the caller's thread (see parallel.py), and returns the
    chain half -- a call that answers the rows and never raises (unless strict). When nothing needs
    reading (no V3, no wallet, a fresh cache entry), the call just hands back the answer.
    """
    try:
        request = _request(user_id, chain_id)
        if request is None:
            return lambda: []
        rows = _cached(request)
        if rows is not None:
            shown = _shown(request, rows)
            return lambda: shown
    except Exception as e:  # noqa: BLE001 -- shown as no positions unless strict
        # Bound now: Python unbinds `e` when the except block ends, before the lambda runs.
        return lambda error=e: _failed(user_id, chain_id, error, strict)

    def finish() -> list[dict]:
        try:
            return _shown(request, _read_chain(request))
        except Exception as e:  # noqa: BLE001 -- shown as no positions unless strict
            return _failed(user_id, request.chain_id, e, strict)

    return finish


def read_positions(user_id: int, chain_id: int | None = None, *, strict: bool = False) -> list[dict]:
    """
    The user's Uniswap V3 positions on `chain_id`, as the dashboard, the assistant and the Telegram
    check show them. Blocking (chain reads).

    @param user_id   The application user ID.
    @param chain_id  The chain to read, or None for the user's current one.
    @return          One row per position worth showing, in the position manager's order:
                     {id, pair ("eth/usdc"), base, quote, fee_pct, min_price, max_price (None for
                     full range), full_range, current_price, in_range, amounts, fees}. Prices are
                     quote per base in whole tokens; amounts and fees are [{ticker, decimals, raw
                     (str)}], base first. [] on a chain that doesn't trade on Uniswap V3, without a
                     wallet there, or when the read fails (logged).
    @param strict    Raise PositionsReadError instead of answering [] when the read fails: for the
                     assistant, which must not tell the user they have no positions because an RPC
                     call failed. The dashboard and Telegram have other things to show, so they don't.
    """
    return _begin(user_id, chain_id, strict)()


def start_positions_read(user_id: int, chain_id: int | None = None) -> Future:
    """
    read_positions, for a caller with reads of its own to overlap: the database lookups run now, on
    this thread, and the chain reads as a task (parallel.py). The future's result is read_positions'
    answer; it never raises.

    A positions read takes more rounds of reads than anything else on the dashboard, so the wallet
    read starts it first and collects it last.
    """
    return start_task(_begin(user_id, chain_id))
