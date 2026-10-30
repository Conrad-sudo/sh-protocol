"""
Turning a plan of executions into a quote the user approves: the checks, the cost in fees, the
headroom the native asset needs, and the quote parked in quotes.py until it is confirmed.
"""
from langchain_core.tools import ToolException
from web3 import Web3

import quotes
from bundler import (
    check_bundler_funds as _check_bundler_funds,
)
from bundler import (
    quote_user_op as _quote_user_op,
)
from bundler import (
    resolve_bundler as _resolve_bundler,
)
from constants import (
    ETH_SENTINEL,
    SESSION_RENEWAL_WARNING_SECS,
    WEI_PER_ETH,
    get_chain_display_name,
    get_native_asset_ticker,
)
from contracts import (
    load_registry,
)
from network_config import load_network_config
from parallel import start_read, start_task
from tools.checks import _enforce_wallet_checks, _wallet_checks
from tools.common import (
    _APPROVAL_ROLES,
    RECEIVED,
    SENT,
    _native_units,
    _resolve_legs,
    _with_tickers,
)
from userop import build_execute_calldata


def _transaction_cost(chain_id: int, quote, fee_wei: int, native_price) -> dict:
    """
    What one quoted UserOp will cost the wallet, in the native asset and in USD.

    Two separate charges, shown separately because they behave differently. The network fee is
    what the EntryPoint takes out of the wallet's prefund to repay the bundler, and it is an
    estimate: `total` prices the gas the simulation actually burned at today's price, while `max`
    prices every gas limit in full at the op's fee cap -- the most the wallet can be charged, and
    the figure {SessionHandler-maxOpGasCost} bounds. The protocol fee is a flat amount of the
    native asset, read from the registry per call, and is exact.

    Neither counts toward the spending cap: the cap meters what the USER spends, and gas is not
    that. The wallet checks cover the cap separately (_wallet_checks).

    @param fee_wei       The protocol fee in wei, read from the registry alongside the quote's reads.
    @param native_price  A future for the USD value (18 decimals) of one whole unit of the native
                         asset. getUsdValue is linear in the amount, so this one read prices every
                         figure exactly -- it used to be three reads, one after another, after the
                         quote was done.
    @return  The figures, kept with the quote; _shown_fees picks the one the model sees.
             "usd_unavailable" replaces the USD ones when the native asset cannot be priced -- an
             unwatched-token transfer is still legal while the ETH feed is stale, so a quote in
             native units is better than refusing to quote at all.
    """
    expected_wei = quote.expected_gas_wei + fee_wei
    max_wei = quote.max_gas_wei + fee_wei

    cost = {
        "native_asset": get_native_asset_ticker(chain_id),
        "network_fee_native": round(quote.expected_gas_wei / WEI_PER_ETH, 9),
        "protocol_fee_native": round(fee_wei / WEI_PER_ETH, 9),
        "total_native": round(expected_wei / WEI_PER_ETH, 9),
        "max_total_native": round(max_wei / WEI_PER_ETH, 9),
    }
    try:
        unit = native_price.result()
    except Exception:  # noqa: BLE001 -- a paused or stale native feed, not a fault in this quote
        cost["usd_unavailable"] = (
            "The native asset's price feed is unavailable, so this cost could not be converted to "
            "USD. Quote it to the user in the native asset instead."
        )
        return cost
    # The oracle's own arithmetic, getPrice = amount * price / 10**18, so these are the figures
    # three getUsdValue reads would have returned, to the wei.
    usd = unit * expected_wei // WEI_PER_ETH
    max_usd = unit * max_wei // WEI_PER_ETH
    fee_usd = unit * fee_wei // WEI_PER_ETH
    cost["network_fee_usd"] = round((usd - fee_usd) / WEI_PER_ETH, 4)
    cost["protocol_fee_usd"] = round(fee_usd / WEI_PER_ETH, 4)
    cost["total_usd"] = round(usd / WEI_PER_ETH, 4)
    cost["max_total_usd"] = round(max_usd / WEI_PER_ETH, 4)
    return cost


def _action_of(plan: dict) -> str:
    """
    What a plan does, in one line, taken from the calls themselves.

    The descriptions are written by the package that built the calldata, from the same arguments,
    so they describe the bytes that will actually be sent rather than the model's account of them.
    That is the whole point of showing them: the user is comparing the agent's summary against
    something the agent did not write. Approvals are dropped -- they are plumbing the wallet
    forces, never the thing the user is agreeing to -- unless they are all there is.

    By role, dropping the approvals rather than keeping an "action" role: the ERC-20 and ERC-8004
    packages call the main step "action", but the Uniswap one calls it "swap", "add_liquidity" or
    "remove_liquidity", and keeping only "action" put every approval back into those quotes.
    """
    calls = plan["calls"]
    described = [
        c["description"] for c in calls
        if c.get("role") not in _APPROVAL_ROLES and c.get("description")
    ]
    if not described:
        described = [c.get("description", f"Call {c['to']}") for c in calls]
    return "; ".join(described)


def _quote_executions(
    runtime, executions: list, action: str, legs: list | None = None, lp_pool: dict | None = None,
    slippage_bps: int | None = None,
) -> dict:
    """
    Checks a set of executions, prices them as one UserOperation, and parks it for the user to approve.

    The half of every write that runs BEFORE the user agrees. It builds the calldata the
    transaction will carry, prices the whole operation against the chain with no user key
    (bundler.quote_user_op), and stores it under an id. No signature is made and nothing is sent:
    once the turn is over the quote goes on a card, and only the user's passkey can approve it.

    It also checks the wallet will accept the transaction at all -- the checks preflight_check
    makes: not paused, a passkey approved to confirm it, and, when `legs` says what moves, within
    the spending limit -- and refuses with the reason if not. Those used to be a separate tool the
    agent had to call first, a whole model call per transaction; here they run alongside the
    quote's own reads, so they cost no extra time either.

    A single call goes out as an ERC-7579 single execution; anything longer is batched, which is
    not an optimisation -- SpendingLimitModule reverts any transaction that leaves an allowance
    standing, so [approve, spend, (reset)] has to land atomically.

    No key is an argument, nor read: the server holds none. The user approves the quote on their
    own device, with whichever of their passkeys the wallet approves.

    @param runtime         The tool's ToolRuntime: carries the user and the conversation turn.
    @param executions      [(target_address, value_wei, calldata_bytes), ...], in order.
    @param action          What this does, in English, composed by code -- never by the model.
    @param legs            What moves, as [(token, amount, SENT or RECEIVED), ...]: what the
                           spending limit will count and the USD value to show. None for a write
                           that moves nothing the limit could count (a registry write, removing
                           liquidity), which only needs the pause and the key checked.
    @param lp_pool         A liquidity deposit's pool (see _lp_pool), for the send to put on the
                           dashboard. None for anything else.
    @param slippage_bps    A swap's or a liquidity change's slippage tolerance, for its card. None
                           for anything else.
    @return                The quote to show the user. NOTHING HAS BEEN SENT.
    @raises ToolException  If the wallet is paused, no passkey is approved on it (or every approval
                           is about to run out), it doesn't hold what the transaction sends (or
                           can't pay the fees on top in the native asset), the transaction would go
                           over the spending limit or would fail, the gas price exceeds what the
                           wallet allows, or the service's bundler could not pay to submit it.
    """
    user_id = runtime.context.user_id
    # The database first, on this thread (see parallel.py).
    w3, chain_id, _ = load_network_config(user_id)
    bundler = _resolve_bundler()
    session_handler, entry_point, calldata = build_execute_calldata(user_id, executions)
    resolved = _resolve_legs(user_id, legs or [])
    registry = load_registry(user_id)
    # What the calls carry in the native asset. The fee and the gas come out of the same balance.
    native_value = sum(value for _, value, _ in executions)

    # Then the chain, all at once: the wallet checks, the quote's own rounds of reads, and the
    # figures its cost needs.
    checks = start_task(lambda: _wallet_checks(session_handler, resolved))
    fee = start_read(registry.functions.getFee().call)
    native_price = start_read(
        lambda: session_handler.functions.getUsdValue(ETH_SENTINEL, WEI_PER_ETH).call()
    )
    bundler_balance = start_read(lambda: w3.eth.get_balance(bundler.address))
    if native_value:
        native_balance = start_read(lambda: w3.eth.get_balance(session_handler.address))
        deposit = start_read(entry_point.functions.balanceOf(session_handler.address).call)
    try:
        quote, failure = _quote_user_op(user_id, session_handler, entry_point, calldata, bundler), None
    except RuntimeError as e:
        quote, failure = None, e
    # A failed check is the clearer reason, so it wins: a paused wallet fails the simulation too,
    # but "the owner has paused this wallet" is what the user needs to hear.
    figures = _enforce_wallet_checks(checks.result(), priced=legs is not None)
    if native_value:
        _check_native_headroom(
            chain_id, native_value, fee.result(), native_balance.result(), deposit.result(), quote
        )
    if failure is not None:
        raise ToolException(str(failure))
    try:
        _check_bundler_funds(user_id, quote, bundler, balance=bundler_balance.result())
    except RuntimeError as e:
        raise ToolException(str(e))

    pending = quotes.put(
        user_id=user_id,
        chain_id=chain_id,
        turn_id=runtime.context.turn_id,
        action=action,
        calls=[{"to": to, "value": value, "data": "0x" + data.hex()} for to, value, data in executions],
        quote=quote,
        cost=_transaction_cost(chain_id, quote, fee.result(), native_price),
        lp_pool=lp_pool,
        direction=_intended_direction(native_value, legs),
        request=runtime.context.message,
        slippage_bps=slippage_bps,
        charged_usd=figures.get("charged_usd"),
    )

    return {
        "status": "NOT SENT — quoted only, waiting for the user to approve it",
        "quote_id": pending.quote_id,
        # The user has a wallet on several chains, so the quote says which one it would move money
        # on -- from the chain it was priced against, not from anything the model said.
        "network": get_chain_display_name(chain_id),
        "action": pending.action,
        "destinations": [c["to"] for c in pending.calls],
        **_shown_figures(figures),
        **_shown_fees(pending.cost),
        "expires_in_seconds": quotes.QUOTE_TTL_SECONDS,
        "next_step": (
            "Show the user, ONE FACT PER LINE as in your instructions' example, never a paragraph: "
            "`action` and the `network`, then what its `details` say, what it is worth "
            "(`usd_value`) and counts toward the spending limit (`charged_usd`) where the quote has "
            "them, the fees as the one figure given, and last \"Tap Confirm and approve with your "
            "passkey\". Plain words, never field names. Then STOP. A Confirm card for this quote appears with "
            "your reply: the user sends it by tapping Confirm and approving with their passkey "
            "(Face ID, fingerprint or PIN). You cannot send it. If they decline or change anything, "
            "call cancel_transaction and start again."
        ),
    }


def _shown_figures(figures: dict) -> dict:
    """
    The wallet checks' figures (_enforce_wallet_checks) that a quote shows the model: what the
    transaction is worth and what it counts toward the limit. What is left of the limit is not
    among them -- the sent transaction's outcome reports it, and a refusal states it when the
    transaction doesn't fit -- and the passkeys' time left is there only once the last approval runs
    out within SESSION_RENEWAL_WARNING_SECS. A model handed a figure reads it out, and every figure
    read out makes the quote longer to read on a phone.
    """
    shown = {k: v for k, v in figures.items() if k not in ("remaining_usd", "session_expires_in_secs")}
    if figures["session_expires_in_secs"] < SESSION_RENEWAL_WARNING_SECS:
        shown["session_expires_in_secs"] = figures["session_expires_in_secs"]
    return shown


def _shown_fees(cost: dict) -> dict:
    """
    The fees as a quote shows them to the model: one figure, the network fee and Mitfah's together.
    A model handed the parts reads every one of them out, so the breakdown and the ceiling stay in
    the stored quote (PendingTransaction.cost) for the code that needs them.

    @param cost  _transaction_cost's figures.
    @return      {"fees_usd"}, or, when the native asset can't be priced, {"fees"} in the native
                 asset with "usd_unavailable" saying why.
    """
    if "usd_unavailable" in cost:
        native = f"{cost['total_native']:.8f}".rstrip("0").rstrip(".")
        return {"fees": f"{native} {cost['native_asset']}", "usd_unavailable": cost["usd_unavailable"]}
    return {"fees_usd": cost["total_usd"]}


def _intended_direction(native_value: int, legs: list | None) -> str | None:
    """
    The History tab's group for a quoted transaction, before anything has moved: 'out' if it sends
    the native coin or a leg it sends, 'in' if it only receives, None if the quote can't say (it
    has no legs). Once it confirms, its receipt decides (tx_history.read_movements).
    """
    directions = {direction for _, _, direction in legs or []}
    if native_value or SENT in directions:
        return "out"
    return "in" if RECEIVED in directions else None


def _quote_plan(
    runtime, plan: dict, details: str = "", legs: list | None = None, lp_pool: dict | None = None,
    slippage_bps: int | None = None,
) -> dict:
    """Checks and prices a package execution plan and parks it for approval. See {_quote_executions}.

    @param details       Extra facts to put in front of the user before they approve -- the
                         slippage bounds a swap will accept, where its output goes. These used to
                         be printed beside the receipt, which was too late to be of any use.
    @param legs          What moves, for the spending-limit check. See {_quote_executions}.
    @param lp_pool       A liquidity deposit's pool. See {_quote_executions}.
    @param slippage_bps  The slippage tolerance, for the card. See {_quote_executions}.
    """
    executions = [
        (Web3.to_checksum_address(call["to"]), call["value"], bytes.fromhex(call["data"][2:]))
        for call in plan["calls"]
    ]
    action = _with_tickers(runtime.context.user_id, _action_of(plan))
    quoted = _quote_executions(runtime, executions, action, legs, lp_pool, slippage_bps)
    if details:
        quoted["details"] = details
    return quoted


def _check_native_headroom(chain_id: int, value: int, fee: int, balance: int, deposit: int, quote) -> None:
    """
    Refuses a transaction whose native asset can't cover what it sends AND what it costs.

    One balance pays for three things, in this order: the gas the EntryPoint asks for up front (what
    the wallet's deposit there doesn't cover), Mitfah's fee, then the value the calls carry. The
    amount alone can fit while the three together don't. So "send all my ETH" used to be refused
    as a bare "FailedCall()", and an amount a little smaller passed the quote and then failed on
    chain, after the user had agreed, charging them the gas.

    @param value    The native value the calls carry, in wei.
    @param fee      The protocol fee, in wei.
    @param balance  The wallet's native balance, in wei.
    @param deposit  The wallet's deposit at the EntryPoint, which pays the gas before its balance does.
    @param quote    The quote, or None when quoting failed. The gas is unknown then, so only the fee
                    is counted -- and the arithmetic alone says whether that already doesn't fit.
    @raises ToolException  If the balance can't cover it all.
    """
    gas = max(quote.max_gas_wei - deposit, 0) if quote is not None else 0
    if value + fee + gas <= balance:
        return
    ticker = get_native_asset_ticker(chain_id)
    held = f"The wallet holds {_native_units(balance)} {ticker}"
    if value > balance:
        raise ToolException(f"{held}, not the {_native_units(value)} this needs. Nothing was sent.")
    if quote is None:
        raise ToolException(
            f"{held}. Using {_native_units(value)} of it leaves too little for the fees. Nothing was "
            f"sent. Try a smaller amount."
        )
    most = max(balance - fee - gas, 0)
    raise ToolException(
        f"{held}. This uses {_native_units(value)}, and the fees can take up to "
        f"{_native_units(fee + gas)} more, so there isn't enough. Nothing was sent. The most it can "
        f"use now is about {_native_units(most - most % 10**10)} {ticker}."
    )
