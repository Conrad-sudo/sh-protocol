"""
The wallet checks every transaction tool runs itself before it quotes -- balance, allowance, the
spending limit, the session key -- and preflight_check, which runs them for a question.
"""
import time

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import ToolException
from web3.contract import Contract

from agent_context import AgentContext
from constants import (
    WEI_PER_ETH,
)
from contracts import (
    load_session_handler,
)
from parallel import read_all, start_read
from tools.common import (
    RECEIVED,
    SENT,
    SESSION_EXPIRY_MARGIN_SECS,
    _get_session_keys,
    _Leg,
    _resolve_legs,
    _to_base_units,
    _whole,
)


def _wallet_checks(wallet: Contract, session_key: str, legs: list[_Leg]) -> dict:
    """
    Whether the wallet will accept a transaction, read from the chain: the pause, the session key,
    whether the wallet holds what it would send, and what the spending limit would charge for
    `legs`.

    Shared by preflight_check and every transaction tool's quote (_quote_executions), so the two can
    never disagree. Two rounds of parallel reads: the wallet's state together with each token's
    watched flag, decimals and (when it is sent) balance, then the prices, which need the first two.

    The balance check is the amount alone. The fees come on top, in the native asset, and only a
    quote knows them -- _check_native_headroom covers that half.

    The limit is charged the way SpendingLimitModule.postCheck meters it: the USD value that leaves
    the wallet minus the value that comes back, counting only the native asset and ERC20s on the
    watched list, and nothing for a net increase. So a wrap of ETH into a watched WETH costs
    nothing, a swap into a watched token costs roughly its fees and price impact, and adding
    liquidity counts both tokens deposited (the LP token that comes back has no price).

    @param wallet       The SessionHandler contract.
    @param session_key  The address of the session key the app holds.
    @param legs         From _resolve_legs. Empty for a transaction that moves nothing countable.
    @return             The figures preflight_check documents.
    @raises             A counted token's price read failing -- the wallet would refuse the
                        transaction too. A price that is only shown may fail without failing this.
    """
    first = _read_wallet_and_legs(wallet, session_key, legs)
    decimals = [18 if leg.erc20 is None else first[f"decimals{i}"] for i, leg in enumerate(legs)]
    short = _shortfalls(legs, first, decimals)
    session_active, seconds_left, expiring_imminently = _session_state(first)

    # Round two: the prices. A token is priced when the limit counts it -- the native asset always,
    # an ERC20 while it is watched -- and, when it leaves the wallet, to show what it is worth. A
    # token the user added is never priced: it has no feed.
    counted = [leg.erc20 is None or first[f"watched{i}"] for i, leg in enumerate(legs)]
    usd, unavailable = _usd_values(wallet.functions, legs, decimals, counted)
    charged, usd_value = _limit_charge(legs, usd, counted)
    remaining = first["remaining"]

    result = {
        "is_paused": first["paused"],
        "session_active": session_active and not expiring_imminently,
        "session_expires_in_secs": max(seconds_left, 0),
        "expiring_imminently": expiring_imminently,
        "enough_balance": not short,
        "within_budget": charged <= remaining,
        "usd_value": usd_value / WEI_PER_ETH if usd_value is not None else None,
        "charged_usd": charged / WEI_PER_ETH,
        "remaining_usd": remaining / WEI_PER_ETH,
    }
    if short:
        result["balance_short"] = " ".join(short)
    if unavailable:
        result["usd_value_unavailable"] = (
            f"Price data for {', '.join(t.upper() for t in unavailable)} is unavailable right now, "
            f"so its USD value can't be shown. The spending limit doesn't count it, so the "
            f"transaction doesn't need it."
        )
    return result


def _read_wallet_and_legs(wallet: Contract, session_key: str, legs: list[_Leg]) -> dict:
    """
    _wallet_checks' first round of reads, all at once: the wallet's pause, session key and remaining
    budget, and for each leg its watched flag and decimals (an ERC-20's) and, when it is sent, the
    wallet's balance of it.
    """
    functions = wallet.functions
    reads = {
        "paused": functions.paused().call,
        "active": functions.isSessionActive(session_key).call,
        "valid_until": functions.currentSessionValidUntil().call,
        "remaining": functions.getRemainingBudget().call,
    }
    for i, leg in enumerate(legs):
        if leg.erc20 is not None:
            reads[f"watched{i}"] = functions.isWatched(leg.address).call
            reads[f"decimals{i}"] = leg.erc20.functions.decimals().call
        if leg.direction == SENT:
            reads[f"balance{i}"] = (
                (lambda: wallet.w3.eth.get_balance(wallet.address))
                if leg.erc20 is None
                else leg.erc20.functions.balanceOf(wallet.address).call
            )
    return read_all(reads)


def _shortfalls(legs: list[_Leg], first: dict, decimals: list[int]) -> list[str]:
    """One sentence for each leg the wallet would send but doesn't hold enough of."""
    short = []
    for i, leg in enumerate(legs):
        if leg.direction == SENT and first[f"balance{i}"] < _to_base_units(leg.amount, decimals[i]):
            short.append(
                f"Not enough {leg.label or leg.token}: the wallet holds "
                f"{_whole(first[f'balance{i}'] / 10 ** decimals[i])}, and this needs {_whole(leg.amount)}."
            )
    return short


def _session_state(first: dict) -> tuple[bool, int, bool]:
    """
    (whether the session key is live, seconds until it expires, whether that is too soon to use it).

    A key that is live NOW but expires while this transaction is being built, signed and mined
    would fail with AA22 after the user has already agreed to it. Refuse a little early instead,
    and say why -- the contract's own comparison stays exact, the margin lives here.
    """
    session_active = first["active"]
    seconds_left = first["valid_until"] - int(time.time())
    return session_active, seconds_left, session_active and seconds_left < SESSION_EXPIRY_MARGIN_SECS


def _usd_values(functions, legs: list[_Leg], decimals: list[int], counted: list[bool]) -> tuple[dict, list[str]]:
    """
    _wallet_checks' second round: the USD value of each leg the limit counts, or that is sent and
    has a feed, all at once. Returns ({leg index: value}, the tokens whose shown-only price failed).

    @raises  A counted leg's price failing: the wallet would refuse the transaction too.
    """
    prices = {}
    for i, leg in enumerate(legs):
        if counted[i] or (leg.direction == SENT and not leg.unlisted):
            base_units = _to_base_units(leg.amount, decimals[i])
            prices[i] = start_read(
                lambda address=leg.address, base_units=base_units: functions.getUsdValue(address, base_units).call()
            )
    usd, unavailable = {}, []
    for i, price in prices.items():
        try:
            usd[i] = price.result()
        except Exception:
            # Only shown, not counted: a transfer of a token the limit ignores is still legal while
            # its feed is down, so it goes ahead without a USD figure. A counted price is another
            # matter -- the wallet would refuse the transaction -- so that failure is raised.
            if counted[i]:
                raise
            unavailable.append(legs[i].label or legs[i].token)
    return usd, unavailable


def _limit_charge(legs: list[_Leg], usd: dict, counted: list[bool]) -> tuple[int, int | None]:
    """
    (what the spending limit would charge, the USD value sent or None where a price is missing),
    both with 18 decimals. Charged as postCheck meters it: the counted value that leaves minus the
    counted value that comes back, never below zero.
    """
    sent = [i for i, leg in enumerate(legs) if leg.direction == SENT]
    charged = sum(usd[i] for i in sent if counted[i]) - sum(
        usd[i] for i, leg in enumerate(legs) if leg.direction == RECEIVED and counted[i]
    )
    usd_value = sum(usd[i] for i in sent) if sent and all(i in usd for i in sent) else None
    return max(charged, 0), usd_value


def _enforce_wallet_checks(checks: dict, priced: bool) -> dict:
    """
    Refuses a quote the wallet would reject anyway, saying why -- or returns the figures to show.

    @param checks  From _wallet_checks.
    @param priced  Whether the transaction moves something the spending limit could count. If not,
                   only the pause and the key are checked, and no USD figures are returned.
    @raises ToolException  If the wallet is paused, its session key is no longer live (or is about
                           to run out), it doesn't hold what the transaction sends, or the
                           transaction would go over the spending limit.
    """
    renew = (
        "The owner can renew it with one transaction from their own wallet in the web app "
        "(Controls → Renew). Nothing was sent."
    )
    if checks["is_paused"]:
        raise ToolException(
            "The owner has paused this wallet, so it can't send anything until they unpause it in "
            "the web app (Controls). Nothing was sent."
        )
    if checks["expiring_imminently"]:
        raise ToolException(
            f"The assistant's session key runs out in under a minute -- too soon to send a "
            f"transaction with it. {renew}"
        )
    if not checks["session_active"]:
        raise ToolException(
            f"The assistant's session key is no longer active (it has run out or been turned off), "
            f"so it can't send anything. {renew}"
        )
    # Before the limit: with too little to send, what the limit would count is beside the point.
    if not checks.get("enough_balance", True):
        raise ToolException(f"{checks['balance_short']} Nothing was sent.")
    figures = {"session_expires_in_secs": checks["session_expires_in_secs"]}
    if not priced:
        return figures
    remaining = max(checks["remaining_usd"], 0)
    if not checks["within_budget"]:
        raise ToolException(
            f"This would count ${checks['charged_usd']:,.2f} toward the spending limit, but only "
            f"${remaining:,.2f} is left in this period. Nothing was sent. The owner can raise the "
            f"limit in the web app, or the user can wait for the period to roll over."
        )
    figures.update(
        usd_value=checks["usd_value"],
        charged_usd=checks["charged_usd"],
        remaining_usd=remaining,
    )
    if "usd_value_unavailable" in checks:
        figures["usd_value_unavailable"] = checks["usd_value_unavailable"]
    return figures


@tool
def preflight_check(
    runtime: ToolRuntime[AgentContext],
    token: str,
    amount: float,
    token_received: str | None = None,
    amount_received: float | None = None,
) -> dict:
    """
    Answers "could the wallet make this transaction right now?" without making it: pause state,
    session validity, whether the wallet holds the amount, the spending limit, and the USD value,
    in one call.

    You do NOT need this before a transaction: every transaction tool (send_eth, transfer_erc20,
    swap, wrap_eth, add_liquidity, ...) runs these same checks itself, refuses with the reason if
    one fails, and puts the figures in its quote. Use this for questions -- "could I send $500 of
    ETH today?", "how much of my limit would this use?" -- where nothing should be quoted.

    The budget check charges what the wallet's spending cap will actually charge: the value that
    leaves the wallet minus the value that comes back, counting only metered tokens (the native
    asset, and ERC20s on the watched list). So a wrap of ETH into a watched WETH costs nothing, and
    a swap into a watched token costs roughly its fees and price impact.

    Args:
        token: The ticker of the token leaving the wallet (e.g. "usdc"), or "eth"/"bnb" for the
               native asset. For a swap, the token being SOLD; for a wrap, "eth"/"bnb".
        amount: How much of `token` leaves the wallet, in whole units (e.g. 100 for 100 USDC).
        token_received: For a swap or wrap only, the ticker of the token that comes back to the
               wallet (the token bought, or the wrapped-native ticker for a wrap). Leave unset for a
               transfer, a native send, or a swap whose output goes to someone else.
        amount_received: How much of `token_received` comes back, in whole units: the quote's
               amount for a swap, the same amount for a wrap. Pass it together with `token_received`.

    Returns:
        A dict with:
          - "is_paused" (bool): True if the owner has paused the wallet. A paused wallet rejects
            every transaction until the owner unpauses it in the web app.
          - "session_active" (bool): True if the wallet's session key is authorized, and not about
            to run out ("session_expires_in_secs" says how long it has left).
          - "enough_balance" (bool): True if the wallet holds `amount` of `token`. When False,
            "balance_short" says what it holds. Fees come on top, in the native asset, so sending
            nearly all of the native asset can still fail.
          - "within_budget" (bool): True if `charged_usd` fits the remaining USD budget.
          - "usd_value" (float | None): The USD value of `amount` of `token` at the current price.
            None for a token the user added themselves: Mitfah has no price for it, so never state
            or estimate one. Also None, with "usd_value_unavailable" saying why, when the price of
            a token the limit doesn't count can't be read right now.
          - "charged_usd" (float): What the transaction would count toward the spending limit. For
            a swap it is an estimate: the wallet is charged on what actually arrives, which can be
            a little less if the price moves. Buying a token the user added with a counted token
            charges the full amount paid, since what comes back has no price.
          - "remaining_usd" (float): The budget left in the current window.
        The transaction would go through only if "is_paused" is False and "session_active",
        "enough_balance" and "within_budget" are all True.
    """
    user_id = runtime.context.user_id
    print("Running preflight_check")
    if (token_received is None) != (amount_received is None):
        raise ToolException("Pass token_received and amount_received together, or neither.")

    session_key, _ = _get_session_keys(user_id)
    legs = [(token, amount, SENT)]
    if token_received is not None:
        legs.append((token_received, amount_received, RECEIVED))
    return _wallet_checks(load_session_handler(user_id), session_key, _resolve_legs(user_id, legs))
