"""
The wallet checks every transaction tool runs itself before it quotes -- balance, the spending
limit, the pause, and whether any of the user's passkeys is approved to confirm it -- and
preflight_check, which runs them for a question.
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
    read_spending_config,
)
from parallel import read_all, start_read
from tools.common import (
    RECEIVED,
    SENT,
    SESSION_EXPIRY_MARGIN_SECS,
    _Leg,
    _resolve_legs,
    _snap_to_balance,
    _to_base_units,
    _whole,
)
from userop import read_sessions


def _wallet_checks(wallet: Contract, legs: list[_Leg]) -> dict:
    """
    Whether the wallet will accept a transaction, read from the chain: the pause, whether a passkey
    is approved to confirm it, whether the wallet holds what it would send, and what the spending
    limit would charge for `legs`.

    Shared by preflight_check and every transaction tool's quote (_quote_executions), so the two can
    never disagree. Two rounds of parallel reads: the wallet's state together with each token's
    watched flag, decimals and (when it is sent) balance, then the prices, which need the first two.

    The balance check is the amount alone. The fees come on top, in the native asset, and only a
    quote knows them -- _check_native_headroom covers that half.

    The limit is charged the way SpendingLimitModule.postCheck meters it: the USD value that leaves
    the wallet minus the value that comes back, counting only ERC20s on the watched list and, if the
    owner has it counted, the native asset; and nothing for a net increase. So with both counted a
    wrap of ETH into WETH costs nothing, a swap into a watched token costs roughly its fees and
    price impact, and adding liquidity counts both tokens deposited (the LP token that comes back
    has no price). With the native asset NOT counted it is left out both ways: sending it costs
    nothing, and selling a counted token for it costs the full amount sold.

    @param wallet       The SessionHandler contract.
    @param legs         From _resolve_legs. Empty for a transaction that moves nothing countable.
    @return             The figures preflight_check documents.
    @raises             A counted token's price read failing -- the wallet would refuse the
                        transaction too. A price that is only shown may fail without failing this.
    """
    first = _read_wallet_and_legs(wallet, legs)
    decimals = [18 if leg.erc20 is None else first[f"decimals{i}"] for i, leg in enumerate(legs)]
    amounts = _base_amounts(legs, first, decimals)
    short = _shortfalls(legs, first, decimals, amounts)
    over_token_limits = _over_token_limits(legs, first, decimals, amounts)
    session_active, seconds_left, expiring_imminently = _session_state(first)

    # Round two: the prices. A token is priced when the limit counts it -- the native asset while
    # the owner has it counted, an ERC20 while it is watched -- and, when it leaves the wallet, to
    # show what it is worth. A token the user added is never priced: it has no feed.
    counted = [
        first["native_metered"] if leg.erc20 is None else first[f"watched{i}"] for i, leg in enumerate(legs)
    ]
    usd, unavailable = _usd_values(wallet.functions, legs, amounts, counted)
    charged, usd_value = _limit_charge(legs, usd, counted)
    remaining = first["remaining"]

    result = {
        "is_paused": first["paused"],
        "session_active": session_active and not expiring_imminently,
        "session_expires_in_secs": max(seconds_left, 0),
        "expiring_imminently": expiring_imminently,
        "enough_balance": not short,
        "within_budget": charged <= remaining,
        "within_token_limits": not over_token_limits,
        "usd_value": usd_value / WEI_PER_ETH if usd_value is not None else None,
        "charged_usd": charged / WEI_PER_ETH,
        "remaining_usd": remaining / WEI_PER_ETH,
    }
    if short:
        result["balance_short"] = " ".join(short)
    if over_token_limits:
        result["token_limit_short"] = " ".join(over_token_limits)
    if unavailable:
        result["usd_value_unavailable"] = (
            f"Price data for {', '.join(t.upper() for t in unavailable)} is unavailable right now, "
            f"so its USD value can't be shown. The spending limit doesn't count it, so the "
            f"transaction doesn't need it."
        )
    return result


def _read_wallet_and_legs(wallet: Contract, legs: list[_Leg]) -> dict:
    """
    _wallet_checks' first round of reads, all at once: the wallet's pause, the passkeys it approves
    (getSessions) and its remaining budget, and for each leg its watched flag and decimals (an
    ERC-20's) and, when it is sent, the wallet's balance of it and the limit on it in its own units.
    Whether the native asset counts is read only when a leg is the native asset.
    """
    functions = wallet.functions
    reads = {
        "paused": functions.paused().call,
        "sessions": lambda: read_sessions(wallet),
        "remaining": functions.getRemainingBudget().call,
    }
    if any(leg.erc20 is None for leg in legs):
        # By field name, never position: see contracts.read_spending_config.
        reads["native_metered"] = lambda: read_spending_config(wallet)["nativeMetered"]
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
            if leg.erc20 is not None:
                reads[f"cap{i}"] = functions.getTokenCap(leg.address).call
    return read_all(reads)


def _base_amounts(legs: list[_Leg], first: dict, decimals: list[int]) -> list[int]:
    """
    Each leg's amount in base units. An ERC-20 the wallet sends is settled against its balance
    first (_snap_to_balance): "send all my LINK" arrives as the balance read back through a float, a
    few base units off it, and the tool that built the transaction settled it the same way. The
    native asset never settles -- sending all of it would leave nothing for the fees.
    """
    amounts = []
    for i, leg in enumerate(legs):
        amount = _to_base_units(leg.amount, decimals[i])
        if leg.direction == SENT and leg.erc20 is not None:
            amount = _snap_to_balance(amount, first[f"balance{i}"])
        amounts.append(amount)
    return amounts


def _shortfalls(legs: list[_Leg], first: dict, decimals: list[int], amounts: list[int]) -> list[str]:
    """One sentence for each leg the wallet would send but doesn't hold enough of."""
    short = []
    for i, leg in enumerate(legs):
        if leg.direction == SENT and first[f"balance{i}"] < amounts[i]:
            short.append(
                f"Not enough {leg.label or leg.token}: the wallet holds "
                f"{_whole(first[f'balance{i}'] / 10 ** decimals[i])}, and this needs {_whole(leg.amount)}."
            )
    return short


def _over_token_limits(legs: list[_Leg], first: dict, decimals: list[int], amounts: list[int]) -> list[str]:
    """
    One sentence for each leg the wallet would send past the limit the owner set on that token in its
    own units. Only what is sent counts: buying a token doesn't use its limit, and a limit is charged
    on the balance drop whatever the price was.
    """
    over = []
    for i, leg in enumerate(legs):
        if leg.direction != SENT or leg.erc20 is None:
            continue
        capped, limit, remaining = first[f"cap{i}"]
        if capped and amounts[i] > remaining:
            name = leg.label or leg.token
            scale = 10 ** decimals[i]
            over.append(
                f"This would use {_whole(leg.amount)} {name}, but only {_whole(remaining / scale)} of the "
                f"{_whole(limit / scale)} {name} limit the owner set is left in this period."
            )
    return over


def _session_state(first: dict) -> tuple[bool, int, bool]:
    """
    (whether any passkey is approved on the wallet now, seconds until the last of them runs out,
    whether that is too soon to use one).

    Any approved passkey can confirm the transaction, so the wallet can take one until the
    longest-lived approval runs out. One that is live NOW but runs out while this transaction is
    being built, approved and mined would fail with AA22 after the user has already agreed to it.
    Refuse a little early instead, and say why -- the contract's own comparison stays exact, the
    margin lives here.
    """
    now = int(time.time())
    active = [session for session in first["sessions"] if session.active(now)]
    seconds_left = max((session.valid_until - now for session in active), default=0)
    session_active = bool(active)
    return session_active, seconds_left, session_active and seconds_left < SESSION_EXPIRY_MARGIN_SECS


def _usd_values(functions, legs: list[_Leg], amounts: list[int], counted: list[bool]) -> tuple[dict, list[str]]:
    """
    _wallet_checks' second round: the USD value of each leg the limit counts, or that is sent and
    has a feed, all at once. Returns ({leg index: value}, the tokens whose shown-only price failed).

    @raises  A counted leg's price failing: the wallet would refuse the transaction too.
    """
    prices = {}
    for i, leg in enumerate(legs):
        if counted[i] or (leg.direction == SENT and not leg.unlisted):
            base_units = amounts[i]
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
    @raises ToolException  If the wallet is paused, no passkey is approved on it (or every approval
                           is about to run out), it doesn't hold what the transaction sends, or the
                           transaction would go over the spending limit or a token's own limit.
    """
    renew = (
        "The owner can approve or renew a passkey with one transaction from their own wallet in the "
        "web app (Controls → Devices). Nothing was sent."
    )
    if checks["is_paused"]:
        raise ToolException(
            "The owner has paused this wallet, so it can't send anything until they unpause it in "
            "the web app (Controls). Nothing was sent."
        )
    if checks["expiring_imminently"]:
        raise ToolException(
            f"Every passkey approved on this wallet runs out in under a minute -- too soon to "
            f"confirm a transaction with one. {renew}"
        )
    if not checks["session_active"]:
        raise ToolException(
            f"No passkey is approved on this wallet any more (they have run out or been removed), "
            f"so no transaction can be confirmed. {renew}"
        )
    # Before the limit: with too little to send, what the limit would count is beside the point.
    if not checks.get("enough_balance", True):
        raise ToolException(f"{checks['balance_short']} Nothing was sent.")
    if not checks.get("within_token_limits", True):
        raise ToolException(
            f"{checks['token_limit_short']} Nothing was sent. The owner can change that limit in the web "
            f"app (Controls), or the user can wait for the period to roll over."
        )
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
    whether a passkey is approved to confirm it, whether the wallet holds the amount, the spending
    limit, and the USD value, in one call.

    You do NOT need this before a transaction: every transaction tool (send_eth, transfer_erc20,
    swap, wrap_eth, add_liquidity, ...) runs these same checks itself, refuses with the reason if
    one fails, and puts the figures in its quote. Use this for questions -- "could I send $500 of
    ETH today?", "how much of my limit would this use?" -- where nothing should be quoted.

    The budget check charges what the wallet's spending cap will actually charge: the value that
    leaves the wallet minus the value that comes back, counting only what the owner has counted
    (ERC20s on the watched list, and the native asset if get_wallet_status says native_counts). So
    a swap into a watched token costs roughly its fees and price impact, and with the native asset
    counted a wrap costs nothing. With it not counted, sending the native asset costs nothing and
    selling a counted token for it costs the full amount sold.

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
          - "session_active" (bool): True if at least one of the user's passkeys is approved on
            the wallet, and not about to run out ("session_expires_in_secs" says how long the
            longest-lived approval has left). The user confirms every transaction with one.
          - "enough_balance" (bool): True if the wallet holds `amount` of `token`. When False,
            "balance_short" says what it holds. Fees come on top, in the native asset, so sending
            nearly all of the native asset can still fail.
          - "within_budget" (bool): True if `charged_usd` fits the remaining USD budget.
          - "within_token_limits" (bool): True unless this would send a token past the limit the
            owner set on it in its own units. When False, "token_limit_short" says what is left.
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
        "enough_balance", "within_budget" and "within_token_limits" are all True.
    """
    user_id = runtime.context.user_id
    print("Running preflight_check")
    if (token_received is None) != (amount_received is None):
        raise ToolException("Pass token_received and amount_received together, or neither.")

    legs = [(token, amount, SENT)]
    if token_received is not None:
        legs.append((token_received, amount_received, RECEIVED))
    return _wallet_checks(load_session_handler(user_id), _resolve_legs(user_id, legs))
