"""
The dashboard's wallet read: its settings, the passkeys it approves, and every balance it shows.
"""
from decimal import Decimal
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from langchain_erc20 import ERC20_ABI
from langchain_uniswap_v2.abis import factory_abi, pair_abi, router_abi
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ABIFunctionNotFound, ContractLogicError

from auth import get_current_user
from constants import (
    get_always_counted_ticker,
    get_native_asset_ticker,
    get_router,
)
from contracts import read_spending_config
from db import (
    add_dashboard_tokens,
    get_custom_tokens,
    get_dashboard_tokens,
    get_lp_tokens,
    get_supported_tokens_by_chain_id,
    get_user_by_id,
    get_wallet_address,
    set_lp_token_pair,
)
from parallel import read_all, start_read
from positions import start_positions_read
from redact import redact
from routes.deps import (
    USD_DECIMALS,
    _load_wallet_for_chain,
    _resolve_chain,
    _revert_reason,
)
from userop import read_sessions, session_block

router = APIRouter()


# SessionHandler.SESSION_GAS_WINDOW: how long the daily fee budget lasts before it refills.
SESSION_GAS_WINDOW_SECS = 86_400


def _unless_missing(read: Callable[[], Any]) -> Callable[[], Any]:
    """
    `read`, answering None when the wallet has no such function: one deployed before it existed.
    On chain its selector falls through to the account's fallback, which reverts; an ABI built
    before it has no entry at all. A failing RPC still raises.
    """
    def guarded():
        try:
            return read()
        except (ABIFunctionNotFound, ContractLogicError):
            return None
    return guarded


def _session_gas(budget: int | None, remaining: int | None, window_start: int | None) -> dict | None:
    """The daily fee budget as the wallet read reports it, or None for a wallet without one."""
    if budget is None or remaining is None or window_start is None:
        return None
    return {
        "budget_wei": str(budget),
        "remaining_wei": str(remaining),
        # Only while some is used: a new window starts at the next charge, not on a clock.
        "refills_at": window_start + SESSION_GAS_WINDOW_SECS if remaining < budget else None,
    }


def _ticker_map(chain_id: int) -> dict[str, str]:
    """
    Lowercased token address -> ticker, for `chain_id`.

    Chain-explicit on purpose. tools._addresses_to_tickers does the same job but resolves the chain
    from the user's SAVED network, which is not necessarily the chain being read here -- the same
    reason _load_wallet_for_chain exists.
    """
    try:
        return {t["address"].lower(): t["ticker"] for t in get_supported_tokens_by_chain_id(chain_id)}
    except ValueError:
        return {}


def _token_balances(w3: Web3, user_id: int, chain_id: int, account: str) -> list[dict]:
    """
    Balances for `account` of the native token and the tokens on the user's dashboard: the listed
    ones in dashboard_tokens, then the ones added by address, then the LP tokens of the pools the
    assistant deposited into (_lp_balances).

    Each token is fetched independently and a failure is reported per-token rather than raised: one
    token with no code (a stale row, a chain that moved a deployment) must not blank out the whole
    dashboard. Raw amounts are strings -- a uint256 of wei does not survive JSON's float64.

    Two eth_calls per listed token (decimals + balanceOf). Tokens added by address cost one: their
    decimals were read and stored when they were added. `custom` marks those -- they have no price
    and never count toward the cap. `always_counted` marks the wrapped native token (WETH, WBNB),
    which stays on the dashboard.

    The lists come from the database first, on this thread (see parallel.py); then every read goes
    out at once, and the LP rows' own reads run while they are answered. So the wait is about one
    round trip, not two per token.
    """
    always = get_always_counted_ticker(chain_id)
    listed = get_dashboard_tokens(user_id, chain_id)
    added = get_custom_tokens(user_id, chain_id)

    def erc20_read(address: str, read: Callable[[Contract], Any]):
        # The contract is bound inside the read, so a malformed address fails like any other read:
        # reported for that token, never raised.
        return start_read(lambda: read(w3.eth.contract(address=address, abi=ERC20_ABI)))

    native = start_read(lambda: w3.eth.get_balance(account))
    listed_reads = [
        (
            token,
            erc20_read(token["address"], lambda erc20: erc20.functions.decimals().call()),
            erc20_read(token["address"], lambda erc20: erc20.functions.balanceOf(account).call()),
        )
        for token in listed
    ]
    added_reads = [
        (token, erc20_read(token["address"], lambda erc20: erc20.functions.balanceOf(account).call()))
        for token in added
    ]
    lp_rows = _lp_balances(w3, user_id, chain_id, account)

    native_raw = native.result()
    balances: list[dict] = [
        {
            "ticker": get_native_asset_ticker(chain_id).lower(),
            "address": None,
            "native": True,
            "decimals": 18,
            "raw": str(native_raw),
            "amount": float(Decimal(native_raw) / Decimal(10**18)),
        }
    ]
    for token, decimals_read, balance_read in listed_reads:
        entry = {
            "ticker": token["ticker"],
            "address": token["address"],
            "native": False,
            "custom": False,
            "always_counted": token["ticker"] == always,
        }
        try:
            decimals, raw = decimals_read.result(), balance_read.result()
            entry |= {
                "decimals": decimals,
                "raw": str(raw),
                "amount": float(Decimal(raw) / Decimal(10**decimals)),
            }
        except Exception as e:  # noqa: BLE001 -- reported per token, never fatal
            entry |= {"decimals": None, "raw": None, "amount": None, "error": _revert_reason(e)}
        balances.append(entry)
    for token, balance_read in added_reads:
        entry = {
            "ticker": token["ticker"],
            "address": token["address"],
            "native": False,
            "custom": True,
            "always_counted": False,
            "name": token["name"],
        }
        try:
            raw = balance_read.result()
            entry |= {
                "decimals": token["decimals"],
                "raw": str(raw),
                "amount": float(Decimal(raw) / Decimal(10 ** token["decimals"])),
            }
        except Exception as e:  # noqa: BLE001 -- reported per token, never fatal
            entry |= {"decimals": None, "raw": None, "amount": None, "error": _revert_reason(e)}
        balances.append(entry)
    return balances + lp_rows


# Every Uniswap V2 pair's LP token has 18 decimals, fixed in the pair contract -- PancakeSwap's too.
_LP_DECIMALS = 18


# A wallet holding less than 1/_LP_DUST of a pool holds none, as far as the dashboard goes. "Remove
# all" passes the amount as a float, so it can leave a few wei behind -- at most ~2e-16 of what was
# held, so always far below this. No real position is this small: in a $1B pool it is $0.001.
_LP_DUST = 10**12


# Each chain's V2 factory, read off its router once: a router's factory never changes.
_v2_factories: dict[int, str] = {}


def _v2_factory(w3: Web3, chain_id: int):
    """
    The V2 factory behind the router the wallets on `chain_id` trust -- found the way the assistant's
    toolkit finds it (router.factory(), see toolkits.get_uniswap_tools), so both see the same pools.
    """
    if chain_id not in _v2_factories:
        router = w3.eth.contract(address=get_router(chain_id), abi=router_abi)
        _v2_factories[chain_id] = router.functions.factory().call()
    return w3.eth.contract(address=_v2_factories[chain_id], abi=factory_abi)


def _read_new_pools(w3: Web3, user_id: int, chain_id: int, pools: list[dict]):
    """
    Finds each pool's address and its two tokens' decimals, all at once, and saves them, so the
    dashboard reads them only the first time it shows a pool. A pool with no address yet (the
    deposit that would create it hasn't landed) is left for a later read.

    @param pools  Rows of db.get_lp_tokens with no `pair` yet. Each is updated in place.
    """
    factory = _v2_factory(w3, chain_id)
    reads = {}
    for i, pool in enumerate(pools):
        reads[i, "pair"] = factory.functions.getPair(pool["token0"], pool["token1"]).call
        for side in ("0", "1"):
            token = w3.eth.contract(address=pool["token" + side], abi=ERC20_ABI)
            reads[i, "decimals" + side] = token.functions.decimals().call
    found = read_all(reads)
    for i, pool in enumerate(pools):
        if int(found[i, "pair"], 16) == 0:
            continue
        pool |= {"pair": found[i, "pair"], "decimals0": found[i, "decimals0"], "decimals1": found[i, "decimals1"]}
        set_lp_token_pair(
            user_id, chain_id, pool["token0"], pool["token1"], pool["pair"], pool["decimals0"], pool["decimals1"]
        )


def _known_pools(w3: Web3, user_id: int, chain_id: int) -> list[dict]:
    """
    The pools the assistant deposited into (db.lp_tokens) whose address is known, reading it off the
    chain the first time (_read_new_pools). One that can't be read, or doesn't exist yet, is left
    out and tried again on the next read.
    """
    pools = get_lp_tokens(user_id, chain_id)
    new = [pool for pool in pools if pool["pair"] is None]
    if new:
        try:
            _read_new_pools(w3, user_id, chain_id, new)
        except Exception as e:  # noqa: BLE001 -- tried again on the next read
            print(f"Could not read the new pools for user {user_id} on chain {chain_id}: {redact(repr(e))}")
    return [pool for pool in pools if pool["pair"] is not None]


def _lp_balances(w3: Web3, user_id: int, chain_id: int, account: str) -> list[dict]:
    """
    The wallet's LP tokens: one row for each pool the assistant deposited into (db.lp_tokens) that
    the wallet still holds some of. A pool it has taken everything out of isn't shown; the next
    deposit brings it back.

    `underlying` is what the LP tokens hold right now: the wallet's share of each of the pool's
    tokens (balance x reserve / totalSupply), which is about what taking it all out would return.
    A few wei left behind by a "remove all" count as nothing (_LP_DUST).
    Rows are named like "eth/usdc lp", the native asset's side first, and `underlying` follows the
    same order.

    Three reads per pool, all at once, plus a round the first time a pool is shown (_read_new_pools).
    A pool that can't be read is reported, like any other balance, never raised. An LP token has no
    price and the assistant can't send one (tools._token_address), so it is neither `custom` nor
    counted; `lp` marks it.
    """
    pools = _known_pools(w3, user_id, chain_id)
    reads = {}
    for pool in pools:
        pair = w3.eth.contract(address=pool["pair"], abi=pair_abi)
        reads[pool["pair"]] = [
            start_read(read)
            for read in (pair.functions.balanceOf(account).call, pair.functions.totalSupply().call,
                         pair.functions.getReserves().call)
        ]

    native = get_native_asset_ticker(chain_id).lower()
    rows = []
    for pool in pools:
        sides = [(pool["ticker0"], pool["decimals0"]), (pool["ticker1"], pool["decimals1"])]
        order = (1, 0) if pool["ticker1"] == native else (0, 1)
        entry = {
            "ticker": "/".join(sides[i][0] for i in order) + " lp",
            "address": pool["pair"],
            "native": False,
            "custom": False,
            "always_counted": False,
            "lp": True,
        }
        try:
            balance, supply, (reserve0, reserve1, _) = (read.result() for read in reads[pool["pair"]])
        except Exception as e:  # noqa: BLE001 -- reported per pool, never fatal
            rows.append(entry | {"decimals": None, "raw": None, "amount": None, "underlying": None,
                                 "error": _revert_reason(e)})
            continue
        if balance == 0 or balance * _LP_DUST < supply:
            continue
        held = (balance * reserve0 // supply, balance * reserve1 // supply)
        rows.append(entry | {
            "decimals": _LP_DECIMALS,
            "raw": str(balance),
            "amount": float(Decimal(balance) / Decimal(10**_LP_DECIMALS)),
            "underlying": [
                {"ticker": sides[i][0], "decimals": sides[i][1], "raw": str(held[i])} for i in order
            ],
        })
    return rows


def _show_counted_tokens(user_id: int, chain_id: int, watched: list[str]):
    """
    Puts every token the limit covers on the dashboard. Run on every wallet read, so it covers the
    deploy's picks, tokens counted later in Controls (or on chain directly), and wallets made before
    the dashboard list existed. The wrapped native token goes on too: it always counts, even on an
    older wallet that doesn't count it yet. A watched address Mitfah doesn't list is skipped -- it
    has no ticker to show (Controls shows it by address).

    @param watched  The wallet's watchedTokens, as read from the chain.
    """
    tickers = _ticker_map(chain_id)
    always = get_always_counted_ticker(chain_id)
    add_dashboard_tokens(
        user_id,
        chain_id,
        [tickers[a.lower()] for a in watched if a.lower() in tickers]
        + ([always] if always in tickers.values() else []),
    )


@router.get("/api/wallet/{chain_id}")
def get_wallet_state(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The read half of the wallet API: everything a dashboard needs, in one call.

    Every other /api/wallet route is a WRITE (prepare -> sign -> confirm), so until this existed a
    front end could change the wallet but not display it -- the only way to read cap, spend or
    paused state was to ask the agent in /api/chat and parse prose.

    Authenticated but NOT gated on `_require_owner`. That check exists so an owner ACTION has an
    address to build a transaction for; reading costs nothing and the wallet row is already keyed by
    the caller's own user_id, so demanding an owner address first would only lock out an account
    from Telegram-only days, whose wallet was created outside the browser flow. `is_owner` reports
    whether the address the account signs in as owns the wallet, so a UI can decide whether to offer
    the owner controls.

    **Reading the session block.** The wallet approves up to `max_keys` passkeys -- one per device --
    each until its own deadline, and lists them all (getSessions), so `session.keys` is the whole
    truth about which devices can confirm the assistant's transactions. Each key carries the name
    the user gave its device here, or null for a key this account never registered (granted on
    chain directly), which the owner may still want to remove. `active` says whether the wallet
    accepts it now, and `needs_renewal` turns true while it still works, so a UI can prompt the
    owner before that device stops working rather than after.

    **Reading the chain.** In two rounds of reads that go out together (parallel.py), rather than
    one call after another: first everything the wallet says about itself, its passkeys included;
    then, once the database has caught up with that, every balance at once. Two round trips after
    the chain and wallet checks (one more the first time a new pool is shown), where there used to
    be about ten plus two per token. On a Uniswap V3 chain the positions take about four rounds of
    their own (positions.read_positions); they need nothing else here, so they start before the
    first round and run beside both.

    @param chain_id  The chain to read. Must be one this deployment serves.
    @return          Wallet address, owner, paused state, the spending cap and window, session-key
                     status, the loosening knobs, the balances of the tokens on the dashboard, and
                     the wallet's Uniswap V3 positions (empty on a chain that doesn't trade there).
    @raises HTTPException 404 if this account has no wallet on `chain_id`.
    """
    # The wallet-row lookup comes FIRST, before _resolve_chain dials anything. Two reasons, the
    # same ones that put the authorization check ahead of the chain resolution in the deploy
    # handlers: a caller with no wallet on this chain should cost no RPC, and the honest answer for
    # them is 404 rather than a 400 about chain configuration they cannot act on. Pure SQLite, so
    # the repeated lookup inside _load_wallet_for_chain below is free.
    try:
        get_wallet_address(user_id, chain_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"You have no wallet on chain {chain_id}.")

    w3, chain_name = _resolve_chain(chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, chain_id)
    # First out, last in: the longest read here, and it waits on nothing below. Never raises.
    positions = start_positions_read(user_id, chain_id)

    reads = wallet.functions
    state = read_all({
        # By field name, never position: see contracts.read_spending_config.
        "cfg": lambda: read_spending_config(wallet),
        "remaining": reads.getRemainingBudget().call,
        # Every passkey the wallet approves, with its deadline: the list is the whole truth.
        "sessions": lambda: read_sessions(wallet),
        "owner": reads.owner().call,
        "paused": reads.paused().call,
        "max_op_gas_cost": reads.maxOpGasCost().call,
        "allowlist_enabled": reads.sessionAllowlistEnabled().call,
        "gas_budget": _unless_missing(lambda: reads.sessionGasBudget().call()),
        "gas_remaining": _unless_missing(lambda: reads.sessionGasRemaining().call()),
        "gas_window_start": _unless_missing(lambda: reads.sessionGasWindowStart().call()),
    })
    cfg = state["cfg"]
    tickers = _ticker_map(chain_id)
    _show_counted_tokens(user_id, chain_id, cfg["watchedTokens"])

    on_chain_owner = state["owner"]
    bound_owner = get_user_by_id(user_id)["owner_addr"]

    # The second round: every balance at once.
    balances = _token_balances(w3, user_id, chain_id, wallet.address)

    return {
        "chain_id": chain_id,
        "chain_name": chain_name,
        "address": wallet.address,
        "owner": on_chain_owner,
        # False means the UI should not offer the owner controls: this account has not proved it
        # holds the key those transactions must be signed with.
        "is_owner": bool(bound_owner) and bound_owner == on_chain_owner,
        "paused": state["paused"],
        "spending": {
            "hook_installed": cfg["installed"],
            "daily_limit_usd": cfg["dailyLimitUsd"] / USD_DECIMALS,
            "spent_usd": cfg["spentInWindow"] / USD_DECIMALS,
            "remaining_usd": state["remaining"] / USD_DECIMALS,
            "window_hours": cfg["windowDuration"] / 3600,
            "window_start": cfg["windowStart"],
            # Watched ERC20s are what the cap meters. The native asset is ALWAYS metered and is
            # deliberately absent here -- see SpendingLimitModule. Unknown addresses fall back to
            # the raw address rather than being dropped.
            "watched_tokens": [
                {"ticker": tickers.get(a.lower()), "address": w3.to_checksum_address(a)} for a in cfg["watchedTokens"]
            ],
        },
        # The passkeys the wallet approves, read off the chain, named from the account's own
        # registrations (userop.session_block).
        "session": session_block(user_id, state["sessions"]),
        # The loosening knobs, grouped so a UI can present them as such.
        "limits": {
            "max_op_gas_cost_wei": str(state["max_op_gas_cost"]),
            # None on a wallet deployed before the daily fee budget existed.
            "session_gas": _session_gas(state["gas_budget"], state["gas_remaining"], state["gas_window_start"]),
            "allowlist_enabled": state["allowlist_enabled"],
            "trusted_spenders": [w3.to_checksum_address(s) for s in cfg["trustedSpenders"]],
        },
        "balances": balances,
        # Uniswap V3 positions are NFTs, not balances. V2 LP tokens stay in `balances` (_lp_balances).
        "positions": positions.result(),
    }
