"""
A trading agent's trades through Mitfah: prepared here, signed by the agent with its own key, sent by
Mitfah's bundler, and accounted for when they land. The AI-app tools prepare_agent_trade and
send_agent_trade (mcp_tools.py) are the way in, for a personal access token that can read and prepare.

  1. prepare_trade  The calls -- the agent's own, or a quote one of the quote tools made -- are priced
                    as an agent op (bundler.quote_agent_op): the module's rules, the agent's own limit
                    and the wallet's checked, and the whole bundle simulated, before anything is
                    signed. The op is frozen in a sign request with origin signing.AGENT -- never a
                    card, never opened by a passkey page -- and the agent gets the EIP-712 message to
                    sign: AgentOperation(userOpHash, deadline, epoch) in AgentModule's domain.
  2. send_trade     The agent's signature comes back. It is checked here first -- the agent's own key,
                    over that exact op -- then the request is claimed, once, the op runs once more with
                    the real signature (execution, then the whole bundle) and joins the chain's next
                    bundle. Its History row is written first, as 'agent'.
  3. settle         Once it lands, off the request (landing.watch): the History row, what it charged the
                    agent accounted for (db.agents.add_relayed, so the keeper never reports it as made
                    elsewhere), and the user told on Telegram -- every trade, as they asked.

A prepared trade is single-use and short-lived: its request is claimed once, its op's nonce is spent
once on chain, and both die after TRADE_TTL_SECS (the op a little later, OP_DEADLINE_GRACE_SECS, for its
send). Mitfah never holds an agent's key and signs nothing for one; what it holds is the op the agent
signed, which can only ever run as signed.
"""
import dataclasses
import json
import logging
import secrets
import time
from dataclasses import dataclass

from langchain_core.tools import ToolException
from web3 import Web3
from web3.contract import Contract
from web3.logs import DISCARD

import agent_ops
import landing
import positions
import quotes
import signing
import telegram_notify
import tx_history
from agents import NO_MODULE, expiry_text, label, module_for_wallet, read_agent, usd
from bundler import (
    NotSent,
    SubmittedUserOp,
    UserOpReverted,
    await_user_op,
    check_bundler_funds,
    check_signed_user_op,
    estimate_agent_execution,
    pick_bundler,
    quote_agent_op,
    resolve_bundler,
    submit_user_op,
)
from constants import ETH_SENTINEL, WEI_PER_ETH, get_chain_display_name, get_native_asset_ticker
from contracts import load_entry_point, load_registry, load_session_handler, read_spending_config
from db import (
    acting_network,
    add_relayed,
    cancel_sign_request,
    claim_sign_request,
    end_sign_request,
    get_agent_row,
    get_sign_request_by_quote,
    get_supported_token_by_address,
    get_wallet_chains,
    open_sign_request,
    save_sign_build,
)
from db.sign_requests import CANCELLED, FAILED, OPEN, SENDING, SENT
from network_config import load_network_config, network_name
from parallel import read_all, start_read
from redact import redact
from tools.quoting import _check_native_headroom, _transaction_cost
from userop import OP_DEADLINE_GRACE_SECS, build_execute_calldata

log = logging.getLogger(__name__)

# How long a prepared trade waits for the agent's signature and the send. An agent signs at once, and
# the gas price and the prices behind the trade are good for minutes, as a quote's are.
TRADE_TTL_SECS = 300
# The most calls one trade may make.
MAX_CALLS = 16

NOT_FOUND = "No agent trade with that id was prepared for this account. Prepare it with prepare_agent_trade."
UNKNOWN = (
    "Mitfah couldn't tell whether that trade went out. Check get_transaction_status, or the History tab on "
    "Mitfah's website, before trying again."
)

# Names for the functions agents call most, so the user's notice and the History tab say "approve on USDC"
# rather than a 4-byte selector. Anything else shows its selector.
_FUNCTIONS = {
    "a9059cbb": "transfer",
    "095ea7b3": "approve",
    "23b872dd": "transferFrom",
    "d0e30db0": "deposit",
    "2e1a7d4d": "withdraw",
    "04e45aaf": "exactInputSingle",
    "414bf389": "exactInputSingle",
    "b858183f": "exactInput",
    "c04b8d59": "exactInput",
    "5023b4df": "exactOutputSingle",
    "db3e2198": "exactOutputSingle",
    "ac9650d8": "multicall",
    "5ae401dc": "multicall",
    "1f0464d1": "multicall",
    "38ed1739": "swapExactTokensForTokens",
    "7ff36ab5": "swapExactETHForTokens",
    "18cbafe5": "swapExactTokensForETH",
    "617ba037": "supply",
    "69328dec": "withdraw",
    "88316456": "mint",
    "219f5d17": "increaseLiquidity",
    "0c49ccbe": "decreaseLiquidity",
    "fc6f7865": "collect",
}


# ── Preparing ──────────────────────────────────────────────────────────────────


def parse_calls(calls: list[dict]) -> list[tuple[str, int, bytes]]:
    """
    An agent's own calls as executions: [{"to", "data", "value"}] -> [(to, value in wei, data)].

    @raises ToolException  Naming the call, when one can't be read.
    """
    if not calls:
        raise ToolException("A trade needs at least one call.")
    if len(calls) > MAX_CALLS:
        raise ToolException(f"A trade makes at most {MAX_CALLS} calls; this one has {len(calls)}.")
    executions = []
    for number, call in enumerate(calls, start=1):
        try:
            to = Web3.to_checksum_address(str(call.get("to", "")).strip())
        except ValueError:
            raise ToolException(f"Call {number}: `to` must be an address (0x and 40 hex digits).")
        data = str(call.get("data") or "0x").strip()
        try:
            payload = bytes.fromhex(data[2:] if data.lower().startswith("0x") else data)
        except ValueError:
            raise ToolException(f"Call {number}: `data` must be hex.")
        try:
            value = int(str(call.get("value") or "0").strip(), 0)
        except ValueError:
            raise ToolException(f"Call {number}: `value` must be a whole number of wei, e.g. \"0\" or \"1000000000000000\".")
        if value < 0:
            raise ToolException(f"Call {number}: `value` can't be negative.")
        executions.append((to, value, payload))
    return executions


def describe_calls(chain_id: int, executions: list[tuple[str, int, bytes]]) -> str:
    """
    What an agent's own calls do, in one line written by code from the calls themselves: the function
    (by name where it is a common one) and the contract (by ticker where Mitfah lists the token).
    """
    native = get_native_asset_ticker(chain_id)
    parts = []
    for to, value, data in executions:
        token = get_supported_token_by_address(chain_id, to)
        target = token["ticker"].upper() if token else f"{to[:6]}…{to[-4:]}"
        if len(data) >= 4:
            selector = data[:4].hex()
            text = f"{_FUNCTIONS.get(selector, '0x' + selector)} on {target}"
        else:
            text = f"send to {target}"
        if value:
            text += f" with {value / WEI_PER_ETH:.10g} {native}"
        parts.append(text)
    return "Agent trade: " + "; ".join(parts)


def _quote_to_run(user_id: int, chain_id: int, quote_id: str) -> dict:
    """
    The sign request of a quote an AI app raised on this account and network, still waiting: what
    prepare_trade turns into the agent's trade.

    @raises ToolException  When there is none, or it is no longer waiting.
    """
    request = get_sign_request_by_quote(user_id, chain_id, quote_id)
    if request is None or request["origin"] != signing.MCP:
        raise ToolException(
            "No quote with that id was made through an AI app on this network. Make one with a quote tool "
            "(swap, transfer_erc20, ...), or give the calls instead."
        )
    if not signing.is_live(request):
        raise ToolException("That quote is no longer waiting: it was approved, cancelled or expired. Quote it again.")
    return request


def _refuse_unless_ready(state, config: dict, paused: bool, agent: str, chain_id: int) -> None:
    """Refuses, in words, a trade the wallet or the module would turn away whatever its calls."""
    if state is None:
        raise ToolException(
            f"{agent} is not one of this wallet's agents on {get_chain_display_name(chain_id)}. The owner adds "
            f"agents in Mitfah's Controls; get_agent_setup lists the wallet's agents."
        )
    if not state.active:
        raise ToolException(
            f"This agent expired on {expiry_text(state.valid_until)}. The owner can renew it in Mitfah's Controls."
        )
    if paused:
        raise ToolException("The owner has paused this wallet, so nothing can be sent until they unpause it.")
    if not config["nativeMetered"]:
        raise ToolException(
            f"The wallet's limit doesn't count {get_native_asset_ticker(chain_id)} right now, and agents can't "
            f"trade until it does. The owner turns that on in Mitfah's Controls."
        )


def prepare_trade(
    user_id: int, chain_id: int, agent: str, *, calls: list[dict] | None = None, quote_id: str | None = None,
    app: str | None = None,
) -> dict:
    """
    Prices a trade for one of the wallet's agents and freezes the op it will sign.

    @param agent     The agent's address: one of the wallet's agents, active.
    @param calls     The agent's own calls, [{"to", "data", "value"}], in order. Or:
    @param quote_id  A quote an AI app raised on this account and network, still waiting. It becomes the
                     agent's trade: its approve link stops working, so it can't also be sent with the
                     passkey.
    @param app       What the connection is called, kept with the trade.
    @return          The trade to sign: its id, what it does, its fees, and the typed data.
    @raises ToolException  When it can't be prepared, saying why. Nothing is signed or sent.
    """
    if (calls is None) == (quote_id is None):
        raise ToolException("Give the trade as `calls`, or as `quote_id`: one of the two.")
    try:
        agent = Web3.to_checksum_address(str(agent).strip())
    except ValueError:
        raise ToolException("`agent` must be the agent's address (0x and 40 hex digits).")
    with acting_network(user_id, network_name(chain_id), chain_id):
        source = None
        if quote_id is not None:
            source = _quote_to_run(user_id, chain_id, quote_id)
            frozen = signing.thaw(source)
            executions = [
                (Web3.to_checksum_address(c["to"]), int(c["value"]), bytes.fromhex(c["data"][2:]))
                for c in frozen["calls"]
            ]
            plan = {k: frozen.get(k) for k in ("action", "lp_pool", "direction", "slippage_bps", "charged_usd")}
        else:
            executions = parse_calls(calls)
            plan = {
                "action": describe_calls(chain_id, executions), "lp_pool": None,
                "direction": "out" if any(value for _, value, _ in executions) else None,
                "slippage_bps": None, "charged_usd": None,
            }

        # The database first, on this thread (see parallel.py); then the chain, all at once.
        w3, _, _ = load_network_config(user_id)
        bundler = resolve_bundler()
        wallet, entry_point, calldata = build_execute_calldata(user_id, executions)
        registry = load_registry(user_id)
        module = module_for_wallet(w3, wallet)
        if module is None:
            raise ToolException(NO_MODULE)
        native_value = sum(value for _, value, _ in executions)
        reads = {
            "state": lambda: read_agent(module, wallet.address, agent),
            "config": lambda: read_spending_config(wallet),
            "paused": wallet.functions.paused().call,
            "fee": registry.functions.getFee().call,
            "bundler_balance": lambda: w3.eth.get_balance(bundler.address),
        }
        if native_value:
            reads["balance"] = lambda: w3.eth.get_balance(wallet.address)
            reads["deposit"] = entry_point.functions.balanceOf(wallet.address).call
        native_price = start_read(lambda: wallet.functions.getUsdValue(ETH_SENTINEL, WEI_PER_ETH).call())
        first = read_all(reads)
        state = first["state"]
        _refuse_unless_ready(state, first["config"], first["paused"], agent, chain_id)

        expires_at = int(time.time()) + TRADE_TTL_SECS
        deadline = expires_at + OP_DEADLINE_GRACE_SECS
        try:
            quote, final = quote_agent_op(
                user_id, wallet, entry_point, module, calldata, agent, state.epoch, deadline, bundler,
            )
        except RuntimeError as e:
            raise ToolException(str(e))
        if native_value:
            _check_native_headroom(chain_id, native_value, first["fee"], first["balance"], first["deposit"], quote)
        try:
            check_bundler_funds(user_id, quote, bundler, balance=first["bundler_balance"])
        except RuntimeError as e:
            raise ToolException(str(e))
        cost = _transaction_cost(chain_id, quote, first["fee"], native_price)
        digest = agent_ops.operation_digest(module.address, chain_id, final.user_op_hash, deadline, state.epoch)

        trade_id = secrets.token_urlsafe(9)
        frozen_trade = {
            **plan,
            "calls": [{"to": to, "value": value, "data": "0x" + data.hex()} for to, value, data in executions],
            "cost": cost,
            "quote": dataclasses.asdict(quote),
            "agent": agent,
            "epoch": state.epoch,
            "deadline": deadline,
            "module": module.address,
            "wallet": wallet.address,
            "app": app,
        }
        request = open_sign_request(trade_id, user_id, chain_id, signing.AGENT, json.dumps(frozen_trade), expires_at)
        save_sign_build(request["id"], secrets.token_urlsafe(16), signing.op_json(final), "0x" + digest.hex())
        if source is not None:
            # The quote becomes this trade. Its link must die first, or both could be sent.
            if not cancel_sign_request(source["id"]):
                cancel_sign_request(request["id"])
                raise ToolException(
                    "That quote was approved, cancelled or expired while this trade was prepared. Nothing was "
                    "sent: quote it again."
                )
            quotes.drop(user_id, quote_id, chain_id)

    row = get_agent_row(chain_id, wallet.address, agent)
    result = {
        "status": "waiting_for_signature",
        "quote_id": trade_id,
        "network": get_chain_display_name(chain_id),
        "agent": agent,
        "agent_name": row["name"] if row else None,
        "action": plan["action"],
        "fees": quotes.fee_text(cost),
    }
    if plan["slippage_bps"] is not None:
        result["slippage"] = f"{plan['slippage_bps'] / 100:g}%"
    if plan["charged_usd"] is not None:
        result["counts_toward_limit"] = f"${plan['charged_usd']:,.2f}"
    sender, nonce, _init_code, call_data, gas_limits, pre_verification_gas, gas_fees, _paymaster, _ = final.op
    result |= {
        "agent_limit_left": usd(state.remaining),
        "expires_in_minutes": TRADE_TTL_SECS // 60,
        "typed_data": agent_ops.typed_data(module.address, chain_id, final.user_op_hash, deadline, state.epoch),
        "digest": "0x" + digest.hex(),
        # The op itself, so the agent can check what it signs rather than trust this server: userOpHash is
        # EntryPoint v0.7's hash of these fields, and callData decodes to the calls.
        "user_op": {
            "sender": sender,
            "nonce": hex(nonce),
            "initCode": "0x",
            "callData": "0x" + call_data.hex(),
            "accountGasLimits": "0x" + gas_limits.hex(),
            "preVerificationGas": hex(pre_verification_gas),
            "gasFees": "0x" + gas_fees.hex(),
            "paymasterAndData": "0x",
            "entryPoint": entry_point.address,
            "chainId": chain_id,
        },
        "next_step": (
            "Nothing has been signed or sent. Sign typed_data with the agent's own key as EIP-712 typed data "
            "(eth_signTypedData_v4, or eth_account's sign_typed_data(full_message=typed_data)); `digest` is the "
            "hash that signs. Then call send_agent_trade with this quote_id and the 65-byte signature as hex, "
            f"within {TRADE_TTL_SECS // 60} minutes. To check what you sign, recompute typed_data's userOpHash "
            "from user_op (EntryPoint v0.7's getUserOpHash) and decode its callData: executeUserOp's selector, "
            "then the wallet's execute() of the calls. Never send the agent's key to Mitfah or anyone."
        ),
    }
    return result


# ── Sending ────────────────────────────────────────────────────────────────────


@dataclass
class AgentSending:
    """An agent's trade on the wire, waiting to land: what settle needs."""

    user_id: int
    chain_id: int
    wallet: str
    agent: str
    epoch: int
    name: str | None
    action: str
    record: int | None          # its History row
    module: Contract
    submitted: SubmittedUserOp

    @property
    def text(self) -> str:
        return f"Sending — {self.action}"


def find_trade(user_id: int, quote_id: str) -> dict:
    """
    The sign request of one of the account's agent trades, on whichever of its networks.

    @raises ToolException  When there is none.
    """
    for chain_id in get_wallet_chains(user_id):
        request = get_sign_request_by_quote(user_id, chain_id, quote_id)
        if request is not None and request["origin"] == signing.AGENT:
            return request
    raise ToolException(NOT_FOUND)


def _not_waiting(request: dict) -> str:
    """Why a trade can no longer be sent, by its request's state."""
    status = request["status"]
    if status == OPEN:
        return "That trade expired before it was sent. Nothing was sent: prepare it again."
    if status in (SENDING, SENT):
        return "That trade was already sent; a trade is sent once. get_transaction_status says how it went."
    if status == CANCELLED:
        return "That trade was cancelled. Nothing was sent."
    if status == FAILED:
        return f"That trade already failed: {request['outcome']}. Nothing was sent; prepare it again."
    return "That trade can't be sent."


def send_trade(user_id: int, quote_id: str, signature: str) -> dict:
    """
    Sends an agent's prepared trade with its signature, and returns once it is on the wire; the wait
    for it to land runs in the background (settle).

    @param signature  The agent's 65-byte ECDSA signature of the trade's typed data, hex.
    @raises ToolException  When it isn't sent, saying why: not the agent's signature (the trade stays
                           open for the right one), no longer waiting, or it would fail now.
    """
    request = find_trade(user_id, quote_id)
    if not signing.is_live(request):
        raise ToolException(_not_waiting(request))
    frozen = signing.thaw(request)
    final = signing.built_op(request)
    agent = frozen["agent"]
    try:
        ecdsa = agent_ops.normalize_ecdsa(signature)
        signer = agent_ops.recover_agent(bytes.fromhex(request["challenge"][2:]), ecdsa)
    except ValueError as e:
        raise ToolException(f"That signature can't be used: {e}. Nothing was sent.")
    if signer != agent:
        raise ToolException(
            f"That signature was made by {signer}, not by the agent ({agent}). Sign this trade's typed_data with "
            f"the agent's own key. Nothing was sent."
        )
    packed = agent_ops.pack_signature(agent, frozen["epoch"], frozen["deadline"], ecdsa)
    chain_id = request["chain_id"]
    try:
        network = network_name(chain_id)
    except ValueError:
        raise ToolException("Mitfah doesn't serve that trade's network right now. Nothing was sent.")
    if not claim_sign_request(request["id"], request["sign_id"]):
        raise ToolException(_not_waiting(find_trade(user_id, quote_id)))

    with acting_network(user_id, network, chain_id):
        try:
            sending = _submit(user_id, chain_id, frozen, final, packed)
        except ToolException as e:
            end_sign_request(request["id"], FAILED, redact(e.args[0] if e.args else "it would fail"))
            raise
        except Exception:
            log.exception("Agent trade %s: the send failed (user %s, chain %s)", quote_id, user_id, chain_id)
            end_sign_request(request["id"], SENT, UNKNOWN)
            raise ToolException(UNKNOWN)
        end_sign_request(request["id"], SENT, sending.text, sending.record)
        landing.watch(sending.record, lambda: settle(sending))
    return {
        "status": "sending",
        "quote_id": quote_id,
        "network": get_chain_display_name(chain_id),
        "agent": agent,
        "action": frozen["action"],
        "user_op_hash": "0x" + final.user_op_hash.hex(),
        "next_step": (
            "It is on its way. Call get_transaction_status with the quote_id to follow it to confirmed: a few "
            "seconds, or about a minute on Ethereum. The user is told of every trade."
        ),
    }


def _submit(user_id: int, chain_id: int, frozen: dict, final, packed: bytes) -> AgentSending:
    """
    Checks the signed op once more and puts it on the wire. The acting network must be the trade's.

    First the agent and the execution, at once: an agent removed (or removed and added again) or expired
    since it was prepared is refused in words, and a trade that would now fail -- its limit spent by
    another trade meanwhile, a price that moved -- by the module's or the wallet's own error. Then the
    whole bundle with the real signature, which sizes the outer transaction.

    @raises ToolException  If it would fail, or its bundle turned it away: nothing went out.
    """
    w3, _, _ = load_network_config(user_id)
    wallet = load_session_handler(user_id)
    entry_point = load_entry_point(user_id)
    module = module_for_wallet(w3, wallet)
    if module is None:
        raise ToolException(NO_MODULE)
    agent, epoch = frozen["agent"], frozen["epoch"]
    signed = final.op[:-1] + (packed,)
    row = get_agent_row(chain_id, wallet.address, agent)
    name = row["name"] if row else None
    with pick_bundler(user_id, wallet.address) as bundler:
        state = start_read(lambda: read_agent(module, wallet.address, agent))
        execution = start_read(lambda: estimate_agent_execution(w3, entry_point, wallet, signed))
        current = state.result()
        if current is None or current.epoch != epoch:
            raise ToolException("The owner removed this agent since the trade was prepared. Nothing was sent.")
        if not current.active:
            raise ToolException("This agent expired since the trade was prepared. Nothing was sent.")
        try:
            execution.result()
            prepared = check_signed_user_op(user_id, entry_point, frozen["quote"], final, packed, bundler)
        except RuntimeError as e:
            raise ToolException(str(e))
        record = tx_history.start_assistant_tx(
            user_id, chain_id, wallet.address, frozen["action"], prepared, frozen["direction"],
            source="agent", agent=agent,
        )
        try:
            submitted = submit_user_op(user_id, prepared, bundler)
        except NotSent as e:
            tx_history.discard_assistant_tx(record)
            raise ToolException(str(e))
    return AgentSending(user_id, chain_id, wallet.address, agent, epoch, name, frozen["action"], record, module, submitted)


# ── Settling ───────────────────────────────────────────────────────────────────


def _executed(sending: AgentSending, receipt) -> dict | None:
    """This trade's AgentExecuted event out of its receipt: what it charged the agent. None if absent."""
    user_op_hash = sending.submitted.prepared.user_op_hash
    for event in sending.module.events.AgentExecuted().process_receipt(receipt, errors=DISCARD):
        args = event["args"]
        if bytes(args["userOpHash"]) == user_op_hash and args["account"] == sending.wallet:
            return dict(args)
    return None


def settle(sending: AgentSending) -> str:
    """
    Waits for an agent's trade to land, then: its History row, what it charged the agent accounted for
    (so the keeper doesn't report it as made elsewhere), and the user told on Telegram. Runs on a
    landing thread, so it never raises: what it returns is what the user was told.
    """
    who = f"Your agent {label(sending.name, sending.agent)}"
    w3 = sending.submitted.w3
    try:
        try:
            tx_hash, receipt = await_user_op(sending.submitted)
        except UserOpReverted as e:
            tx_history.finish_assistant_tx(sending.record, w3, e.receipt, succeeded=False)
            text = (f"{who}'s trade failed on chain: {redact(e)}. The wallet paid its gas.\n"
                    f"Tx: {Web3.to_hex(e.tx_hash)}")
            telegram_notify.tell_user(sending.user_id, sending.chain_id, text)
            return text
        except RuntimeError as e:
            tx_history.discard_assistant_tx(sending.record)
            text = f"{who}'s trade didn't go through: {redact(e)}. Nothing was sent."
            telegram_notify.tell_user(sending.user_id, sending.chain_id, text)
            return text

        charged_line = ""
        executed = _executed(sending, receipt)
        if executed is not None:
            charged = executed["chargedUsd"] + executed["feeUsd"]
            try:
                after = read_agent(sending.module, sending.wallet, sending.agent, block_identifier=receipt["blockNumber"])
            except Exception as e:  # noqa: BLE001 -- the trade landed; the account and the notice still go out
                log.warning("Agent trade landed, but its record couldn't be read: %s", redact(e))
                after = None
            if after is not None:
                add_relayed(sending.chain_id, sending.wallet, sending.agent, sending.epoch, after.window_start, charged)
                charged_line = (f"\nIt counted {usd(charged)} toward its limit: {usd(after.remaining)} of its "
                                f"{usd(after.limit)} left this period.")
        # After the account, so the keeper never sees this trade landed and unaccounted for.
        tx_history.finish_assistant_tx(sending.record, w3, receipt, receipt["status"] == 1)
        positions.forget(sending.chain_id, sending.wallet)
        text = f"🤖 {who} traded: {sending.action}{charged_line}\nTx: {Web3.to_hex(tx_hash)}"
        telegram_notify.tell_user(sending.user_id, sending.chain_id, text)
        return text
    except TimeoutError:
        # Neither landed nor refused in time: its History row stays pending, and the History tab settles
        # it from the chain later (tx_history.settle_pending).
        return f"{who}'s trade is still on its way."
    except Exception:  # noqa: BLE001 -- a landing thread must not raise
        log.exception("Settling an agent trade failed (user %s, chain %s)", sending.user_id, sending.chain_id)
        return UNKNOWN
