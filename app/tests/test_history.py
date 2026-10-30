"""
Offline checks for the History tab and the short chat memory: how transactions are recorded (the
assistant's and the owner's), described, settled and listed, and how the conversation starts afresh
after a transaction or is deleted on request.

Everything runs against a throwaway database, a FAKE chain and a scripted model, so it is safe to
run anywhere. The real journey -- a send on a fork landing in the history with the same hash the
chat reported -- is in test_e2e_fork.

Run: make history-test   (or: python app/tests/test_history.py)
"""
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from types import SimpleNamespace

from checks import check, finish, sign_in, set_everywhere   # first: it puts app/ on sys.path for the imports below

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"

import db                                   # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

from fastapi.testclient import TestClient   # noqa: E402
from hexbytes import HexBytes               # noqa: E402
from langchain.agents import create_agent   # noqa: E402
from langchain.agents.middleware import ToolRetryMiddleware  # noqa: E402
from langchain.tools import tool            # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from web3 import Web3                       # noqa: E402
from web3.exceptions import TimeExhausted, TransactionNotFound  # noqa: E402

import api                                  # noqa: E402
import contract_errors                      # noqa: E402
import routes.deploy  # noqa: E402
import routes.deps  # noqa: E402
import routes.history  # noqa: E402
import explorers                            # noqa: E402
import landing                              # noqa: E402
import quotes                               # noqa: E402
import signing                              # noqa: E402
import smart_wallet_agent                   # noqa: E402
import tools                                # noqa: E402
import tools.sending  # noqa: E402
import userop                               # noqa: E402
import tx_history                           # noqa: E402
from agent_context import AgentContext      # noqa: E402
from bundler import FinalUserOp, NotSent, UserOpQuote, UserOpReverted  # noqa: E402
from constants import get_router, get_v3_network  # noqa: E402
from eth_abi import encode as abi_encode    # noqa: E402
from passkey import SoftPasskey, key_id     # noqa: E402
from webauthn.helpers import base64url_to_bytes  # noqa: E402
from test_tx_movements import bundle, swap_router_call, transfer, withdrawal  # noqa: E402
from tx_movements import IN, OUT, Moved     # noqa: E402

CHAIN = 11155111
NETWORK = "sepolia-fork"
BSC = 56
BSC_NETWORK = "bsc-fork"


def _addr(n: int) -> str:
    return Web3.to_checksum_address(f"0x{n:040x}")


def _hash(n: int) -> str:
    return f"0x{n:064x}"


WALLET = _addr(0xA11CE)
ENTRY_POINT = _addr(0xE7)
OWNER = _addr(0x0E1)
SAM = _addr(0x5A3)
USDC = _addr(0x05DC)
PEPE = _addr(0x9E9E)
STRANGER = _addr(0xBAD)
ETH_SENTINEL = _addr(0)

HANDLER_ABI = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
# A bound contract with no provider: enough to encode calls and to decode them back.
WALLET_CONTRACT = Web3().eth.contract(address=WALLET, abi=HANDLER_ABI)

_db = db.get_db()
_db.execute("INSERT OR REPLACE INTO supported_tokens (chain_id, ticker, address) VALUES (?, ?, ?)", (CHAIN, "usdc", USDC))
_db.commit()


# ── A fake chain ──────────────────────────────────────────────────────────────


class _Call:
    def __init__(self, value):
        self.value = value

    def call(self, *_args, **_kwargs):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeEntryPoint:
    """getNonce and the UserOperationEvent logs, as the settle path reads them."""

    def __init__(self):
        self.nonce = 0
        self.events: dict[bytes, list] = {}   # transactionHash -> UserOperationEvent logs

    @property
    def functions(self):
        return SimpleNamespace(getNonce=lambda _wallet, _key: _Call(self.nonce))


class _EntryPointEvents:
    def __init__(self, entry_point: FakeEntryPoint):
        self.entry_point = entry_point

    def UserOperationEvent(self):  # noqa: N802 -- web3's name
        return SimpleNamespace(
            process_receipt=lambda receipt, errors=None: self.entry_point.events.get(bytes(receipt["transactionHash"]), [])
        )


class FakeEth:
    def __init__(self):
        self.txs: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.blocks: dict[int, int] = {}
        self.entry_point = FakeEntryPoint()

    def get_transaction(self, tx_hash):
        if tx_hash not in self.txs:
            raise TransactionNotFound(f"Transaction {tx_hash} not found")
        return self.txs[tx_hash]

    def get_transaction_receipt(self, tx_hash):
        if tx_hash not in self.receipts:
            raise TransactionNotFound(f"Transaction {tx_hash} not found")
        return self.receipts[tx_hash]

    def wait_for_transaction_receipt(self, tx_hash, timeout):
        if tx_hash not in self.receipts:
            raise TimeExhausted(f"Transaction {tx_hash} is not in the chain after {timeout} seconds")
        return self.receipts[tx_hash]

    def get_block(self, number):
        return {"timestamp": self.blocks[number]}

    def contract(self, address, abi):
        address = Web3.to_checksum_address(address)
        if address == WALLET:
            return SimpleNamespace(functions=SimpleNamespace(ENTRY_POINT=lambda: _Call(ENTRY_POINT)))
        if address == ENTRY_POINT:
            return SimpleNamespace(functions=self.entry_point.functions, events=_EntryPointEvents(self.entry_point))
        if address == PEPE:
            return SimpleNamespace(functions=SimpleNamespace(decimals=lambda: _Call(6)))
        return SimpleNamespace(functions=SimpleNamespace(decimals=lambda: _Call(Exception("no code"))))


class FakeW3:
    def __init__(self):
        self.eth = FakeEth()


def _owner_tx(fn_name: str, args: list, to: str = WALLET) -> dict:
    return {"to": to, "input": HexBytes(WALLET_CONTRACT.encode_abi(fn_name, args=args)), "value": 0}


def _create(owner_addr: str | None = None) -> int:
    return db.create_user(owner_addr=owner_addr)


# ── Describing an owner transaction ──────────────────────────────────────────


def test_owner_transactions_are_described_from_their_calldata():
    """
    The History tab's line for an owner transaction is read off its calldata on the server, in the
    words the controls use. Nothing the browser says is involved.
    """
    print("\n[1] owner transactions are described from their own calldata")
    user = _create(OWNER)
    db.save_contact(user, "sam", SAM)
    db.save_custom_token(user, CHAIN, PEPE, "pepe", "Pepe", 6)
    w3 = FakeW3()

    def says(tx: dict) -> str:
        return tx_history.describe_wallet_tx(w3, user, CHAIN, WALLET_CONTRACT, tx)

    in_30_days = int(time.time()) + 30 * 86_400 + 60
    v3 = get_v3_network(CHAIN)
    # One passkey this account registered, named; another the wallet could hold that it never did.
    phone_x, phone_y = b"\x01" * 32, b"\x02" * 32
    stranger_x, stranger_y = b"\x03" * 32, b"\x04" * 32
    db.save_passkey(user, key_id(phone_x, phone_y), "cred-phone", b"cose", "0x" + phone_x.hex(), "0x" + phone_y.hex(),
                    "Kani's iPhone")
    cases = [
        (_owner_tx("pause", []), "Pause the wallet"),
        (_owner_tx("unpause", []), "Unpause the wallet"),
        (_owner_tx("withdraw", [ETH_SENTINEL, 5 * 10**17, SAM]), "Withdraw 0.5 ETH to sam"),
        (_owner_tx("withdraw", [PEPE, 12_500_000, OWNER]), "Withdraw 12.5 PEPE to your owner address"),
        (_owner_tx("withdraw", [ETH_SENTINEL, 10**18, STRANGER]), f"Withdraw 1 ETH to {STRANGER[:6]}…{STRANGER[-4:]}"),
        (_owner_tx("addWatchedToken", [USDC]), "Count USDC toward the limit"),
        (_owner_tx("removeWatchedToken", [USDC]), "Stop counting USDC toward the limit"),
        (_owner_tx("setDailyLimit", [100 * 10**18]), "Set the spending limit to $100"),
        (_owner_tx("setDailyLimit", [1_250 * 10**16]), "Set the spending limit to $12.50"),
        (_owner_tx("setWindowDuration", [86_400]), "Set the spending period to 24 hours"),
        (_owner_tx("setWindowDuration", [7 * 86_400]), "Set the spending period to 7 days"),
        (_owner_tx("addSession", [phone_x, phone_y, in_30_days]), "Let “Kani's iPhone” approve payments for 30 days"),
        (_owner_tx("addSession", [stranger_x, stranger_y, in_30_days]), "Let a device approve payments for 30 days"),
        (_owner_tx("removeSession", [bytes.fromhex(key_id(phone_x, phone_y)[2:])]), "Revoke “Kani's iPhone”"),
        (_owner_tx("removeSession", [bytes.fromhex(key_id(stranger_x, stranger_y)[2:])]), "Revoke a device"),
        (_owner_tx("removeAllSessions", []), "Revoke every device"),
        (_owner_tx("addTrustedSpender", [Web3.to_checksum_address(get_router(CHAIN))]),
         "Trust the exchange router to spend from the wallet"),
        (_owner_tx("addTrustedSpender", [v3["swap_router"]]), "Trust the Uniswap V3 router to spend from the wallet"),
        (_owner_tx("addTrustedSpender", [v3["position_manager"]]), "Trust Uniswap V3 positions to spend from the wallet"),
        (_owner_tx("removeTrustedSpender", [v3["position_manager"]]), "Stop trusting Uniswap V3 positions"),
        (_owner_tx("removeTrustedSpender", [STRANGER]), f"Stop trusting {STRANGER[:6]}…{STRANGER[-4:]}"),
        (_owner_tx("setMaxOpGasCost", [10**16]), "Set the network fee cap to 0.01 ETH"),
        (_owner_tx("setSessionGasBudget", [2 * 10**16]), "Set the daily network fee budget to 0.02 ETH"),
        ({"to": WALLET, "input": HexBytes(b""), "value": 10**17}, "Add 0.1 ETH to the wallet"),
    ]
    for tx, expected in cases:
        got = says(tx)
        check(f"described as “{expected}”", got == expected, got)
    check("calldata the wallet doesn't know is still a line, not an error",
          says({"to": WALLET, "input": HexBytes(b"\xde\xad\xbe\xef"), "value": 0}) == "Call your wallet")
    on_bsc = tx_history.describe_wallet_tx(w3, user, BSC, WALLET_CONTRACT, _owner_tx("addTrustedSpender", [get_router(BSC)]))
    check("on BSC, PancakeSwap's router is the exchange router", on_bsc == "Trust the exchange router to spend from the wallet",
          on_bsc)
    elsewhere = tx_history.describe_wallet_tx(w3, user, BSC, WALLET_CONTRACT, _owner_tx("addTrustedSpender", [v3["swap_router"]]))
    check("...and Sepolia's Uniswap V3 router is nobody it knows there",
          elsewhere == f"Trust {v3['swap_router'][:6]}…{v3['swap_router'][-4:]} to spend from the wallet", elsewhere)


# ── Recording an owner transaction ───────────────────────────────────────────


def test_owner_transactions_are_recorded_once_and_settled():
    """
    The web confirms are polled, so the same hash arrives many times: it is recorded on the first
    poll that can see it, settled by the one that has the receipt, and never duplicated. Only a
    transaction sent TO the user's wallet is recorded.
    """
    print("\n[2] owner transactions: recorded once, settled by the receipt, only if sent to the wallet")
    user = _create()
    w3 = FakeW3()
    tx_hash = _hash(0x1)
    w3.eth.txs[tx_hash] = _owner_tx("pause", [])

    def rows():
        return db.get_transactions(user)

    tx_history.record_owner_tx(w3, user, CHAIN, WALLET_CONTRACT, _hash(0x99))
    check("a hash the node hasn't seen yet is not recorded", rows() == [], str(rows()))

    tx_history.record_owner_tx(w3, user, CHAIN, WALLET_CONTRACT, tx_hash)
    tx_history.record_owner_tx(w3, user, CHAIN, WALLET_CONTRACT, tx_hash.upper().replace("0X", "0x"))
    got = rows()
    check("the first poll records it as pending, once", len(got) == 1 and got[0]["status"] == "pending", str(got))
    check("with its hash and what it does",
          got[0]["tx_hash"] == tx_hash and got[0]["action"] == "Pause the wallet" and got[0]["source"] == "owner", str(got))

    w3.eth.receipts[tx_hash] = {"status": 1, "blockNumber": 7, "transactionHash": HexBytes(tx_hash)}
    w3.eth.blocks[7] = 1_790_000_000
    tx_history.record_owner_tx(w3, user, CHAIN, WALLET_CONTRACT, tx_hash, w3.eth.receipts[tx_hash])
    got = rows()
    check("the poll with the receipt settles it, at the block's time",
          len(got) == 1 and got[0]["status"] == "confirmed" and got[0]["mined_at"] == 1_790_000_000, str(got))

    w3.eth.receipts[tx_hash] = {"status": 0, "blockNumber": 7, "transactionHash": HexBytes(tx_hash)}
    tx_history.record_owner_tx(w3, user, CHAIN, WALLET_CONTRACT, tx_hash, w3.eth.receipts[tx_hash])
    check("a settled row is never rewritten", rows()[0]["status"] == "confirmed", str(rows()))

    failed = _hash(0x2)
    w3.eth.txs[failed] = _owner_tx("unpause", [])
    w3.eth.receipts[failed] = {"status": 0, "blockNumber": 7, "transactionHash": HexBytes(failed)}
    tx_history.record_owner_tx(w3, user, CHAIN, WALLET_CONTRACT, failed, w3.eth.receipts[failed])
    check("a reverted owner transaction is listed as failed", rows()[0]["status"] == "failed", str(rows()[0]))

    elsewhere = _hash(0x3)
    w3.eth.txs[elsewhere] = _owner_tx("pause", [], to=STRANGER)
    tx_history.record_owner_tx(w3, user, CHAIN, WALLET_CONTRACT, elsewhere)
    check("a transaction sent somewhere else is not written into this history",
          all(r["tx_hash"] != elsewhere for r in rows()), str(rows()))

    tx_history.record_deploy(w3, user, CHAIN, WALLET, _hash(0x4), {"status": 1, "blockNumber": 7})
    tx_history.record_deploy(w3, user, CHAIN, WALLET, _hash(0x4), {"status": 1, "blockNumber": 7})
    deploys = [r for r in rows() if r["tx_hash"] == _hash(0x4)]
    check("a deploy is recorded once, even if confirmed twice",
          len(deploys) == 1 and deploys[0]["action"] == "Create your Mitfah smart wallet", str(deploys))


# ── Recording the assistant's transactions ───────────────────────────────────


def _patch_send(w3, broadcast, budget_left, device_live=None):
    """
    Stubs everything a passkey-approved send calls, so only its recording is under test.

    `broadcast(user_id, prepared, bundler)` stands for sending the op and waiting for it: it runs
    when the send is waited on (bundler.await_user_op), after it was submitted.

    `budget_left` is a dict whose "value" the fake wallet's getRemainingBudget returns -- or raises,
    if it is an exception -- so a test can change it between sends. `device_live`, when given, is a
    dict whose "value" the wallet's isSessionActive answers.
    """
    saved = {
        name: getattr(tools.sending, name)
        for name in ("load_network_config", "_pick_bundler", "load_session_handler", "load_entry_point",
                     "_check_signed_user_op", "_submit_user_op", "_await_user_op")
    }
    op_hash = {"n": 0}

    def check_signed(*_args):
        op_hash["n"] += 1
        return SimpleNamespace(user_op_hash=bytes([op_hash["n"]]) * 32, op=(WALLET, (7 << 64) | op_hash["n"]), from_block=100)

    def remaining():
        if isinstance(budget_left["value"], Exception):
            raise budget_left["value"]
        return budget_left["value"]

    live = device_live if device_live is not None else {"value": True}
    wallet = SimpleNamespace(
        address=WALLET,
        functions=SimpleNamespace(
            getRemainingBudget=lambda: SimpleNamespace(call=remaining),
            isSessionActive=lambda _key_id: SimpleNamespace(call=lambda: live["value"]),
        ),
    )
    set_everywhere(tools, "load_network_config", lambda _user_id: (w3, CHAIN, NETWORK))
    set_everywhere(tools, "_pick_bundler", lambda _user_id, _wallet: nullcontext("bundler"))
    set_everywhere(tools, "load_session_handler", lambda _user_id: wallet)
    set_everywhere(tools, "load_entry_point", lambda _user_id: None)
    set_everywhere(tools, "_check_signed_user_op", check_signed)
    set_everywhere(tools, "_submit_user_op",
                   lambda user_id, prepared, bundler: SimpleNamespace(
                       w3=w3, wait=lambda: broadcast(user_id, prepared, bundler)))
    set_everywhere(tools, "_await_user_op", lambda submitted: submitted.wait())

    def restore():
        for name, value in saved.items():
            set_everywhere(tools, name, value)

    return restore


def _approved(user: int, quote_id: str, action: str = "Transfer 5 USDC to 0x5A3…", lp_pool: dict | None = None):
    """A request a passkey approved, as signing.approve hands it on: what submit_signed reads."""
    return SimpleNamespace(
        request={"id": 1, "user_id": user, "chain_id": CHAIN, "quote_id": quote_id},
        frozen={"action": action, "quote": None, "lp_pool": lp_pool, "direction": "out"},
        final=None, signature=b"", key_id="0x" + "ab" * 32,
    )


def _send(user: int, quote_id: str, **kwargs) -> str:
    """Sends an approved quote and waits for it, as confirm_signed and landing.watch do between them."""
    return tools.sending.settle_quoted(tools.sending.submit_signed(_approved(user, quote_id, **kwargs)))


def test_assistant_sends_are_recorded_before_they_go():
    """
    A send is recorded BEFORE it is broadcast and settled after, so one that outlives the wait is
    still listed. One that never executed is not listed, one that reverted on chain is, and a
    failure to record never changes what the user is told.
    """
    print("\n[3] approved sends: recorded before they go, settled after, never lost")
    user = _create()
    w3 = FakeW3()
    w3.eth.blocks[101] = 1_790_000_123
    landed = HexBytes(b"\x22" * 32)
    receipt = {"status": 1, "transactionHash": landed, "blockNumber": 101}
    outcome = {"next": "ok"}

    def broadcast(_user_id, prepared, _bundler):
        pending = [r for r in db.get_transactions(user) if r["user_op_hash"] == "0x" + prepared.user_op_hash.hex()]
        outcome["recorded_before_send"] = len(pending) == 1 and pending[0]["status"] == "pending"
        kind = outcome["next"]
        if kind == "ok":
            return landed, receipt
        if kind == "reverted":
            raise UserOpReverted("UserOperation inner call failed", landed, receipt)
        if kind == "not executed":
            raise RuntimeError("handleOps outer transaction reverted")
        raise TimeoutError("not mined in time")

    budget_left = {"value": 12345 * 10**16}   # $123.45
    device_live = {"value": True}
    restore = _patch_send(w3, broadcast, budget_left, device_live)
    try:
        reply = _send(user, "q1")
        newest = db.get_transactions(user)[0]
        check("the row existed, pending, before the op was broadcast", outcome["recorded_before_send"])
        check("a send that went through is confirmed, with the executing transaction's hash and block time",
              newest["status"] == "confirmed" and newest["tx_hash"] == "0x" + "22" * 32
              and newest["mined_at"] == 1_790_000_123 and newest["source"] == "assistant", str(newest))
        check("described by the quote's code-written action", newest["action"] == "Transfer 5 USDC to 0x5A3…", newest["action"])
        check("listed as outgoing from the start, by the quote's direction", newest["direction"] == "out", str(newest["direction"]))
        check("the reply carries the same 0x-prefixed hash", "0x" + "22" * 32 in reply, reply)
        check("...and what is left of the spending limit", "\nSpending limit left this period: $123.45\nTx hash: 0x" in reply, reply)

        outcome["next"] = "reverted"
        try:
            _send(user, "q2")
            raised = False
        except ToolException:
            raised = True
        newest = db.get_transactions(user)[0]
        check("a reverted op is a failure", raised)
        check("...but is listed as failed, with its hash", newest["status"] == "failed" and newest["tx_hash"] == "0x" + "22" * 32,
              str(newest))

        before = len(db.get_transactions(user))
        outcome["next"] = "not executed"
        try:
            _send(user, "q3")
        except ToolException:
            pass
        check("an op that never executed is not listed", len(db.get_transactions(user)) == before,
              str(db.get_transactions(user)))

        # Turned away from its bundle before anything carried it (bundler.Lane._check).
        def turned_away(*_args):
            raise NotSent("Submitting this transaction would fail: AA25 invalid account nonce. Nothing was sent.")

        submit = set_everywhere(tools, "_submit_user_op", turned_away)
        try:
            tools.sending.submit_signed(_approved(user, "q3b"))
            refused = None
        except ToolException as e:
            refused = str(e)
        finally:
            set_everywhere(tools, "_submit_user_op", submit)
        check("an op its bundle turned away fails, saying nothing was sent",
              refused is not None and "Nothing was sent" in refused, str(refused))
        check("...and is not listed", len(db.get_transactions(user)) == before, str(db.get_transactions(user)))

        # The last check before sending says AA22: which deadline ran out decides what the user hears.
        aa22 = "Submitting this transaction would fail: " + contract_errors.describe_revert_data(
            contract_errors._FAILED_OP_SELECTOR + abi_encode(["uint256", "string"], [0, "AA22 expired or not due"])
        ) + ". Nothing was sent."

        def expired(*_args):
            raise RuntimeError(aa22)

        checked = set_everywhere(tools, "_check_signed_user_op", expired)
        try:
            heard = []
            for live in (True, False):
                device_live["value"] = live
                try:
                    tools.sending.submit_signed(_approved(user, "q3c"))
                    heard.append(None)
                except ToolException as e:
                    heard.append(str(e))
        finally:
            set_everywhere(tools, "_check_signed_user_op", checked)
            device_live["value"] = True
        check("AA22 with the passkey still approved: the op's own deadline passed -- ask again",
              heard[0] == signing.EXPIRED, str(heard[0]))
        check("AA22 with the passkey's approval run out: renew the device",
              heard[1] == tools.sending.DEVICE_EXPIRED, str(heard[1]))
        check("...and neither was listed", len(db.get_transactions(user)) == before, str(db.get_transactions(user)))

        outcome["next"] = "timeout"
        try:
            _send(user, "q4")
            timed_out = False
        except TimeoutError:
            timed_out = True
        newest = db.get_transactions(user)[0]
        check("a send that outlives the wait propagates its timeout", timed_out)
        check("...and stays listed as pending, to be settled later",
              newest["status"] == "pending" and newest["tx_hash"] is None and newest["from_block"] == 100, str(newest))

        outcome["next"] = "ok"
        saved_add = tx_history.add_transaction

        def broken(*_args, **_kwargs):
            raise RuntimeError("database is locked")

        tx_history.add_transaction = broken
        try:
            reply = _send(user, "q5")
        finally:
            tx_history.add_transaction = saved_add
        check("a history that can't be written never turns a sent payment into an error",
              reply.startswith("✅ Sent —"), reply)

        budget_left["value"] = RuntimeError("rpc down")
        reply = _send(user, "q6")
        check("a budget that can't be read never turns a sent payment into an error either",
              reply.startswith("✅ Sent —") and "Spending limit" not in reply, reply)
        budget_left["value"] = -5 * 10**18
        reply = _send(user, "q7")
        check("a limit lowered below what was spent shows nothing left, not a negative figure",
              "\nSpending limit left this period: $0.00\nTx hash: 0x" in reply, reply)
    finally:
        restore()


def test_a_repeated_request_is_not_a_yes():
    """
    A typed message that repeats the request a quote was raised by, word for word, cannot have its
    card shown again: the quote is void and the agent is told to quote afresh. A reply agreeing to
    it gets the card back, and so does a one- or two-word reply even when the same words raised the
    quote. Nothing is sent either way: only the passkey sends.
    """
    print("\n[3c] a repeated request is a new request, not a yes")
    user = _create()
    w3 = FakeW3()

    def says(message: str, turn: int = 2) -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user, turn_id=turn, message=message))

    def broadcast(*_args):
        raise AssertionError("nothing may be sent here")

    restore = _patch_send(w3, broadcast, {"value": 10**18})
    try:
        asked = quotes.put(user, CHAIN, 1, "Transfer 1 USDC to sam", [], None, {}, request="Send 1 USDC to Sam.")
        try:
            tools.sending.confirm_transaction.func(says("send 1 usdc to sam"), asked.quote_id)
            check("the same request again is refused as a confirmation", False, "the card was shown again")
        except ToolException as e:
            check("the same request again (case and full stop aside) is refused as a confirmation",
                  "asked for this transaction again" in str(e) and "Nothing was sent" in str(e), str(e))
        check("...and its quote is void", asked.quote_id not in quotes._pending)

        again = quotes.put(user, CHAIN, 2, "Transfer 1 USDC to sam", [], None, {}, request="send 1 usdc to sam")
        reply = tools.sending.confirm_transaction.func(says("Yes, send it", 3), again.quote_id)
        check("a reply agreeing to the new quote gets its card back, and sends nothing",
              reply == tools.sending.CARD_SHOWN and quotes.offer(user, CHAIN, 3).quote_id == again.quote_id, reply)

        other = quotes.put(user, CHAIN, 3, "Transfer 1 USDC to sam", [], None, {}, request="Send 1 USDC to Sam.")
        reply = tools.sending.confirm_transaction.func(says("Send 2 USDC to Sam.", 4), other.quote_id)
        check("a different message is the model's to judge, not refused here", reply == tools.sending.CARD_SHOWN, reply)

        short = quotes.put(user, CHAIN, 4, "Transfer 1 USDC to sam", [], None, {}, request="yes")
        reply = tools.sending.confirm_transaction.func(says("Yes!", 5), short.quote_id)
        check("a quote raised by a \"yes\" can still be shown again for \"yes\"", reply == tools.sending.CARD_SHOWN, reply)

        unknown = quotes.put(user, CHAIN, 5, "Transfer 1 USDC to sam", [], None, {})
        reply = tools.sending.confirm_transaction.func(says("Transfer 1 USDC to sam", 6), unknown.quote_id)
        check("a quote raised without a message is never taken for a repeat", reply == tools.sending.CARD_SHOWN, reply)
    finally:
        restore()
        for pending in (again, other, short, unknown):
            quotes.drop(user, pending.quote_id)


def test_deposits_put_their_pool_on_the_dashboard():
    """
    Sending an approved liquidity deposit remembers its pool for the dashboard BEFORE the deposit
    goes out, so one that outlives the wait still shows once it lands. Any other send remembers
    nothing, and a pool that can't be saved never turns a sent deposit into an error.
    """
    print("\n[3b] a liquidity deposit puts its pool on the dashboard before it is sent")
    user = _create()
    w3 = FakeW3()
    w3.eth.blocks[101] = 1_790_000_123
    landed = HexBytes(b"\x33" * 32)
    seen = {}

    def broadcast(_user_id, _prepared, _bundler):
        seen["pools"] = db.get_lp_tokens(user, CHAIN)
        return landed, {"status": 1, "transactionHash": landed, "blockNumber": 101}

    weth = _addr(0x0E7E)
    deposit = {"action": "Add liquidity: 100 of USDC and 0.04 of ETH",
               "lp_pool": {"token_a": weth, "ticker_a": "eth", "token_b": USDC, "ticker_b": "usdc"}}
    restore = _patch_send(w3, broadcast, {"value": 10**18})
    saved_save = tools.sending._save_lp_token
    try:
        _send(user, "q1")
        check("a plain send remembers no pool", seen["pools"] == [] and db.get_lp_tokens(user, CHAIN) == [])

        reply = _send(user, "q2", **deposit)
        check("the deposit went out as usual", reply.startswith("✅ Sent —"), reply)
        check("its pool was saved before it was broadcast, in the pool's own order (lower address first)",
              [(p["token0"], p["ticker0"], p["token1"], p["ticker1"]) for p in seen["pools"]]
              == [(USDC, "usdc", weth, "eth")], str(seen["pools"]))
        check("...with nothing read about it yet", seen["pools"][0]["pair"] is None, str(seen["pools"]))

        _send(user, "q3", **deposit)
        check("a second deposit into the same pool keeps one row", len(db.get_lp_tokens(user, CHAIN)) == 1)

        def broken(*_args, **_kwargs):
            raise RuntimeError("database is locked")

        set_everywhere(tools, "_save_lp_token", broken)
        reply = _send(user, "q4", **deposit)
        check("a pool that can't be saved never turns a sent deposit into an error", reply.startswith("✅ Sent —"), reply)
    finally:
        set_everywhere(tools, "_save_lp_token", saved_save)
        restore()


# ── Settling what was left pending ───────────────────────────────────────────


def test_pending_rows_are_settled_later():
    """
    Reading the History tab finishes what was left pending: an assistant op found on chain later
    (confirmed or failed), one whose nonce another op used (dropped, it can never land), and an
    owner transaction the node no longer knows a day on (dropped).
    """
    print("\n[4] pending rows are settled when the history is read")
    user = _create()
    w3 = FakeW3()
    w3.eth.blocks[200] = 1_790_000_500
    nonce = (7 << 64) | 3

    def assistant_row(n: int) -> int:
        return db.add_transaction(user, CHAIN, WALLET, "assistant", f"op {n}", "pending",
                                  user_op_hash=_hash(0x100 + n), op_nonce=nonce, from_block=100)

    landed_id, reverted_id, lost_id, waiting_id = (assistant_row(n) for n in range(4))
    executed = {
        bytes.fromhex(_hash(0x100)[2:]): (HexBytes(b"\x31" * 32), True),
        bytes.fromhex(_hash(0x101)[2:]): (HexBytes(b"\x32" * 32), False),
    }
    for op_hash, (tx, success) in executed.items():
        w3.eth.entry_point.events[bytes(tx)] = [{"args": {"userOpHash": op_hash, "success": success}}]

    saved_find = tx_history.find_user_op_receipt
    tx_history.find_user_op_receipt = lambda _w3, _ep, op_hash, _from: (
        {"transactionHash": executed[op_hash][0], "blockNumber": 200, "status": 1} if op_hash in executed else None
    )
    try:
        w3.eth.entry_point.nonce = nonce        # nothing has used op 2's nonce yet
        tx_history.settle_pending(user, lambda _chain: w3)
        status = {r["id"]: r for r in db.get_transactions(user)}
        check("an op found on chain later is confirmed, with its hash and block time",
              status[landed_id]["status"] == "confirmed" and status[landed_id]["tx_hash"] == "0x" + "31" * 32
              and status[landed_id]["mined_at"] == 1_790_000_500, str(status[landed_id]))
        check("one found reverted is failed", status[reverted_id]["status"] == "failed", str(status[reverted_id]))
        check("one not found, its nonce unused, stays pending (it can still land)",
              status[lost_id]["status"] == "pending", str(status[lost_id]))

        w3.eth.entry_point.nonce = nonce + 1    # another op used the nonce
        tx_history.settle_pending(user, lambda _chain: w3)
        status = {r["id"]: r for r in db.get_transactions(user)}
        check("one not found after its nonce was used is dropped: it can never land",
              status[lost_id]["status"] == "dropped" and status[waiting_id]["status"] == "dropped", str(status[lost_id]))

        mined, stuck, gone = _hash(0x201), _hash(0x202), _hash(0x203)
        for tx_hash in (mined, stuck, gone):
            db.add_transaction(user, CHAIN, WALLET, "owner", "Pause the wallet", "pending", tx_hash=tx_hash)
        w3.eth.receipts[mined] = {"status": 1, "blockNumber": 200}
        w3.eth.txs[stuck] = {"to": WALLET}
        db.get_db().execute("UPDATE transactions SET created_at = ? WHERE tx_hash IN (?, ?)",
                            (int(time.time()) - 2 * 86_400, stuck, gone))
        db.get_db().commit()
        tx_history.settle_pending(user, lambda _chain: w3)
        by_hash = {r["tx_hash"]: r for r in db.get_transactions(user)}
        check("an owner transaction mined since is confirmed", by_hash[mined]["status"] == "confirmed", str(by_hash[mined]))
        check("one still waiting in the mempool stays pending", by_hash[stuck]["status"] == "pending", str(by_hash[stuck]))
        check("one the node no longer knows a day on is dropped", by_hash[gone]["status"] == "dropped", str(by_hash[gone]))

        unreachable = db.add_transaction(user, BSC, WALLET, "owner", "Pause the wallet", "pending", tx_hash=_hash(0x204))
        tx_history.settle_pending(user, lambda chain: w3 if chain == CHAIN else None)
        check("a chain the server can't reach is skipped, not an error",
              {r["id"]: r for r in db.get_transactions(user)}[unreachable]["status"] == "pending")
    finally:
        tx_history.find_user_op_receipt = saved_find


# ── The routes ────────────────────────────────────────────────────────────────


def make_client() -> TestClient:
    """A test client with the agent lifespan disabled."""

    @asynccontextmanager
    async def _noop(_app):
        yield

    api.app.router.lifespan_context = _noop
    api.limiter.enabled = False
    return TestClient(api.app)


def _sign_up(client: TestClient) -> tuple[int, dict]:
    body, headers, _ = sign_in(client)
    return body["user_id"], headers


def test_history_route_lists_only_your_own_newest_first():
    print("\n[5] GET /api/transactions: authenticated, per account, newest first, paged")
    c = make_client()
    me, headers = _sign_up(c)
    other, _ = _sign_up(c)
    for n in range(3):
        db.add_transaction(me, CHAIN, WALLET, "assistant", f"mine {n}", "confirmed", tx_hash=_hash(0x300 + n))
    db.add_transaction(me, BSC, WALLET, "owner", "mine on bsc", "confirmed", tx_hash=_hash(0x310))
    db.add_transaction(other, CHAIN, WALLET, "owner", "theirs", "confirmed", tx_hash=_hash(0x320))

    saved = routes.history._web3_or_none
    set_everywhere(api, "_web3_or_none", lambda _chain: None)
    try:
        check("the history needs a token", c.get("/api/transactions").status_code == 401)
        r = c.get("/api/transactions", headers=headers)
        got = r.json()
        check("readable when signed in", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        check("only this account's, newest first",
              [t["action"] for t in got["transactions"]] == ["mine on bsc", "mine 2", "mine 1", "mine 0"],
              str([t["action"] for t in got["transactions"]]))
        check("each has what the tab shows, and nothing internal",
              set(got["transactions"][0]) == {"id", "chain_id", "source", "action", "status", "tx_hash", "created_at", "mined_at", "direction"},
              str(got["transactions"][0]))
        check("no further page", got["next_before"] is None, str(got["next_before"]))

        page = c.get("/api/transactions?limit=2", headers=headers).json()
        last = page["transactions"][-1]
        check("a page stops at the limit and points at the next",
              len(page["transactions"]) == 2 and page["next_before"] == f"{last['created_at']}-{last['id']}", str(page))
        rest = c.get(f"/api/transactions?limit=2&before={page['next_before']}", headers=headers).json()
        check("the next page carries on where it stopped",
              [t["action"] for t in rest["transactions"]] == ["mine 1", "mine 0"] and rest["next_before"] is None, str(rest))

        on_bsc = c.get(f"/api/transactions?chain_id={BSC}", headers=headers).json()["transactions"]
        check("filtered to one chain", [t["action"] for t in on_bsc] == ["mine on bsc"], str(on_bsc))
        check("an unsupported chain -> 400", c.get("/api/transactions?chain_id=999999", headers=headers).status_code == 400)
        check("a limit over 100 -> 422", c.get("/api/transactions?limit=101", headers=headers).status_code == 422)
    finally:
        set_everywhere(api, "_web3_or_none", saved)


def test_deposits_from_the_fund_drawer_are_listed():
    print("\n[6] POST /api/transactions/deposit waits for a deposit sent to the wallet, and lists it")
    c = make_client()
    me, headers = _sign_up(c)
    w3 = FakeW3()
    deposit, elsewhere, unseen, failed = _hash(0x401), _hash(0x402), _hash(0x403), _hash(0x404)
    w3.eth.txs[deposit] = {"to": WALLET, "input": HexBytes(b""), "value": 2 * 10**17}
    w3.eth.txs[elsewhere] = {"to": STRANGER, "input": HexBytes(b""), "value": 10**18}
    w3.eth.txs[failed] = {"to": WALLET, "input": HexBytes(b""), "value": 10**17}
    w3.eth.receipts[failed] = {"status": 0, "blockNumber": 8}
    w3.eth.blocks[8] = 1_790_000_800

    saved = routes.deps._resolve_chain, routes.deps._load_wallet_for_chain, routes.history._web3_or_none
    set_everywhere(api, "_resolve_chain", lambda _chain: (w3, NETWORK))
    set_everywhere(api, "_load_wallet_for_chain", lambda _w3, _user, _chain: WALLET_CONTRACT)
    set_everywhere(api, "_web3_or_none", lambda _chain: w3)
    try:
        def post(tx_hash: str):
            return c.post("/api/transactions/deposit", json={"chain_id": CHAIN, "tx_hash": tx_hash}, headers=headers)

        body = {"chain_id": CHAIN, "tx_hash": deposit}
        check("following a deposit needs a token", c.post("/api/transactions/deposit", json=body).status_code == 401)
        r = post(unseen)
        check("a hash the node never saw -> 202, seen false, and nothing listed",
              r.status_code == 202 and r.json() == {"status": "pending", "tx_hash": unseen, "seen": False}
              and db.get_transactions(me) == [], f"{r.status_code} {r.text[:160]}")
        r = post(deposit)
        check("one waiting in the pool -> 202, seen true",
              r.status_code == 202 and r.json() == {"status": "pending", "tx_hash": deposit, "seen": True},
              f"{r.status_code} {r.text[:160]}")
        rows = db.get_transactions(me)
        check("listed as pending until it mines, described from the transaction",
              len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["action"] == "Add 0.2 ETH to the wallet", str(rows))
        check("...and as incoming, from its calldata", rows[0]["direction"] == "in", str(rows[0]["direction"]))

        r = post(elsewhere)
        check("a transaction to somebody else's address -> 400, and not listed",
              r.status_code == 400 and "was not sent to your wallet" in r.json()["detail"]
              and len(db.get_transactions(me)) == 1, f"{r.status_code} {r.text[:160]}")
        check("a malformed hash -> 422", post("0x12").status_code == 422)

        r = post(failed)
        check("a transfer that failed -> 400, listed as failed",
              r.status_code == 400 and "The transfer reverted" in r.json()["detail"]
              and [t["status"] for t in db.get_transactions(me) if t["tx_hash"] == failed] == ["failed"],
              f"{r.status_code} {r.text[:160]}")

        w3.eth.receipts[deposit] = {"status": 1, "blockNumber": 9}
        w3.eth.blocks[9] = 1_790_000_900
        listed = {t["tx_hash"]: t for t in c.get("/api/transactions", headers=headers).json()["transactions"]}
        check("reading the history settles it", listed[deposit]["status"] == "confirmed"
              and listed[deposit]["mined_at"] == 1_790_000_900, str(listed[deposit]))
        r = post(deposit)
        check("once mined -> 200, confirmed", r.status_code == 200
              and r.json() == {"status": "confirmed", "tx_hash": deposit}, f"{r.status_code} {r.text[:160]}")
    finally:
        set_everywhere(api, "_resolve_chain", saved[0])
        set_everywhere(api, "_load_wallet_for_chain", saved[1])
        set_everywhere(api, "_web3_or_none", saved[2])


def test_owner_confirm_says_whether_the_network_has_seen_it():
    """
    An owner confirm answers 202 for a hash with no receipt either way, but says whether the node
    has seen it at all. A hash it has never seen -- the wallet failed to broadcast it, sent it
    through another RPC, or the fork was restarted -- will never mine, and the web page stops
    waiting once it has stayed unseen for a while instead of spinning out its whole deadline.
    """
    print("\n[6b] an owner confirm says whether the network has seen the transaction")
    c = make_client()
    _, headers = _sign_up(c)
    w3 = FakeW3()
    unseen, waiting = _hash(0x501), _hash(0x502)
    w3.eth.txs[waiting] = _owner_tx("pause", [])

    saved = routes.deps._resolve_chain, routes.deps._load_wallet_for_chain
    set_everywhere(api, "_resolve_chain", lambda _chain: (w3, NETWORK))
    set_everywhere(api, "_load_wallet_for_chain", lambda _w3, _user, _chain: WALLET_CONTRACT)
    try:
        for path in ("/api/wallet/tx/confirm", "/api/wallet/session/confirm"):
            r = c.post(path, json={"chain_id": CHAIN, "tx_hash": unseen}, headers=headers)
            check(f"{path}: a hash the node never saw -> 202, seen false",
                  r.status_code == 202 and r.json() == {"status": "pending", "tx_hash": unseen, "seen": False},
                  f"{r.status_code} {r.text[:160]}")
            r = c.post(path, json={"chain_id": CHAIN, "tx_hash": waiting}, headers=headers)
            check(f"{path}: one waiting in the pool -> 202, seen true",
                  r.status_code == 202 and r.json() == {"status": "pending", "tx_hash": waiting, "seen": True},
                  f"{r.status_code} {r.text[:160]}")
    finally:
        set_everywhere(api, "_resolve_chain", saved[0])
        set_everywhere(api, "_load_wallet_for_chain", saved[1])


# Where two accounts' deploys land: the addresses /api/deploy predicted for them.
DEPLOYED = _addr(0xDE9)
DEPLOYED_TOO = _addr(0xDEA)


class FakeDeployEth(FakeEth):
    """FakeEth, plus the wallets already on chain, as /api/deploy/confirm reads them."""

    def __init__(self):
        super().__init__()
        self.wallets: dict[str, tuple[str, str, int]] = {}   # address -> (owner, passkey id, block made in)
        self.block_number = 50
        self.archive = True   # False: a node that can't read old state

    def get_code(self, address, block_identifier="latest"):
        if block_identifier != "latest" and not self.archive:
            raise ValueError("missing trie node")
        wallet = self.wallets.get(Web3.to_checksum_address(address))
        block = self.block_number if block_identifier == "latest" else block_identifier
        return b"\x60" if wallet and block >= wallet[2] else b""

    def contract(self, address, abi):
        address = Web3.to_checksum_address(address)
        if address not in self.wallets:
            return super().contract(address, abi)
        owner, key, _ = self.wallets[address]
        return SimpleNamespace(functions=SimpleNamespace(
            owner=lambda: _Call(owner),
            getSessions=lambda: _Call(([bytes.fromhex(key[2:])], [2**40])),
        ))


class FakeFactory:
    """WalletDeployed logs, behind a provider that searches at most 10 blocks at a time (Alchemy's free tier)."""

    def __init__(self):
        self.logs: list[dict] = []

    def _get_logs(self, argument_filters, from_block, to_block):
        if to_block - from_block > 10:
            raise ValueError("Under the Free tier plan, you can make eth_getLogs requests with up to a 10 block range")
        return [log for log in self.logs if log["args"]["walletAddress"] == argument_filters["walletAddress"]
                and from_block <= log["blockNumber"] <= to_block]

    @property
    def events(self):
        return SimpleNamespace(WalletDeployed=lambda: SimpleNamespace(get_logs=self._get_logs))


def test_deploy_confirm_says_whether_the_network_has_seen_it():
    """
    /api/deploy/confirm says whether the node has seen the hash, like the owner confirms -- except
    when the wallet is already at the predicted address. The user's wallet replaced the deploy (sped
    it up, say), so it mined under a hash nobody told the app. Reported as never received, the page
    would offer to start over, and a second deploy makes a second wallet; so it is finished from the
    chain instead, and listed under the hash that created it.
    """
    print("\n[6c] a deploy confirm says whether the network has seen it, and finishes a replaced deploy")
    c = make_client()
    w3 = SimpleNamespace(eth=FakeDeployEth())
    factory = FakeFactory()
    lost, waiting, sped_up = _hash(0x601), _hash(0x602), _hash(0x603)
    w3.eth.txs[waiting] = {"to": _addr(0xFAC), "input": HexBytes(b""), "value": 10**17}
    # The sped-up deploy mined in block 37, under a hash the page never learned.
    w3.eth.receipts[sped_up] = {"status": 1, "blockNumber": 37, "transactionHash": HexBytes(sped_up)}
    w3.eth.blocks[37] = 1_790_000_900
    factory.logs = [{"args": {"walletAddress": DEPLOYED}, "transactionHash": sped_up, "blockNumber": 37}]

    saved = routes.deps._resolve_chain, routes.deploy._load_factory_for_chain
    set_everywhere(api, "_resolve_chain", lambda _chain: (w3, NETWORK))
    set_everywhere(api, "_load_factory_for_chain", lambda _w3, _chain: factory)
    try:
        body, headers, account = sign_in(c)
        me, owner, key = body["user_id"], account.address, "0x" + "5e" * 32

        def confirm(tx_hash: str, predicted: str = DEPLOYED, as_headers: dict = headers, deployer: str = owner):
            return c.post("/api/deploy/confirm", headers=as_headers, json={
                "chain_id": CHAIN, "deployer": deployer, "tx_hash": tx_hash, "predicted_address": predicted,
            })

        r = confirm(lost)
        check("a hash the node never saw, and no wallet there -> 202, seen false",
              r.status_code == 202 and r.json() == {"status": "pending", "tx_hash": lost, "seen": False},
              f"{r.status_code} {r.text[:160]}")
        r = confirm(waiting)
        check("one waiting in the pool -> 202, seen true",
              r.status_code == 202 and r.json() == {"status": "pending", "tx_hash": waiting, "seen": True},
              f"{r.status_code} {r.text[:160]}")

        w3.eth.wallets[DEPLOYED] = (STRANGER, key, 37)
        r = confirm(lost)
        check("a wallet somebody else owns there finishes nothing",
              r.status_code == 202 and r.json()["seen"] is False and db.get_wallet_chains(me) == [],
              f"{r.status_code} {r.text[:160]}")

        w3.eth.wallets[DEPLOYED] = (owner, key, 37)
        r = confirm(lost)
        answer = r.json()
        check("the wallet is there under another hash -> 200, filed from the chain",
              r.status_code == 200 and answer["status"] == "deployed" and answer["wallet_address"] == DEPLOYED
              and answer["key_id"] == key and answer["session_key_authorized"] is True
              and answer["session_key_expires_at"] == 2**40,
              f"{r.status_code} {r.text[:200]}")
        check("saved as the account's wallet", db.get_wallet_address(me, CHAIN) == DEPLOYED)
        rows = db.get_transactions(me)
        check("listed under the hash that created it, found one block at a time",
              [(t["action"], t["tx_hash"], t["status"], t["mined_at"]) for t in rows]
              == [("Create your Mitfah smart wallet", sped_up, "confirmed", 1_790_000_900)], str(rows))

        # Another account, on a node that can't read old state: still filed, just not listed.
        body, other_headers, other = sign_in(c)
        w3.eth.wallets[DEPLOYED_TOO] = (other.address, key, 44)
        w3.eth.archive = False
        r = confirm(_hash(0x604), DEPLOYED_TOO, other_headers, other.address)
        check("a node without old state still files the wallet, just without the History row",
              r.status_code == 200 and db.get_wallet_address(body["user_id"], CHAIN) == DEPLOYED_TOO
              and db.get_transactions(body["user_id"]) == [], f"{r.status_code} {r.text[:200]}")
    finally:
        set_everywhere(api, "_resolve_chain", saved[0])
        set_everywhere(api, "_load_factory_for_chain", saved[1])


# ── The chat starts afresh after a transaction ───────────────────────────────


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
def confirm_transaction(quote_id: str) -> str:
    """Sends a quoted transaction (a stand-in with the real tool's name)."""
    if quote_id == "bad":
        raise ToolException("UserOp failed! tx: 0xdead")
    return f"Sent — Transfer 5 USDC to sam. Tx hash: `0x{'ab' * 32}`, Status: 1"


def _confirm_call(quote_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "confirm_transaction", "args": {"quote_id": quote_id},
                                              "id": f"call-{quote_id}-{time.time_ns()}", "type": "tool_call"}])


def _use_scripted_agent(replies: list):
    # The production failure handling, so a failed send becomes an error ToolMessage, as it does live.
    smart_wallet_agent.agent = create_agent(
        ScriptedModel(messages=iter(replies)), tools=[confirm_transaction], checkpointer=InMemorySaver(),
        context_schema=AgentContext,
        middleware=[ToolRetryMiddleware(max_retries=0, on_failure=smart_wallet_agent._tool_failure_message)],
    )


def _thread(user: int, chain: int = CHAIN) -> list:
    config = {"configurable": {"thread_id": smart_wallet_agent.thread_id(user, chain)}}
    return smart_wallet_agent.agent.get_state(config).values.get("messages", [])


# What the approval page's finish writes into the conversation for a send (confirm_signed).
SENDING = "Sending — Transfer 5 USDC to sam. Waiting for the network to confirm it."
# A priced op for quotes that get a sign request: the request freezes it.
PRICED = UserOpQuote(
    calldata="0x1234", call_gas=100_000, call_gas_used=80_000, verification_gas=200_000,
    verification_gas_used=150_000, pre_verification_gas=60_000, max_fee=2 * 10**9, tip=10**9,
    base_fee=10**9, outer_gas=500_000, simulated_gas=None, chain_name=NETWORK,
)


def test_the_chat_starts_afresh_after_a_transaction():
    """
    After a transaction goes out -- a card approved with a passkey, which confirm_signed writes into
    the conversation and marks -- the next turn starts a fresh conversation carrying only the last
    exchange's text. A failed send, or a quote still waiting for an answer, keeps it; a history
    over the size limit clears it too.
    """
    print("\n[7] the conversation starts afresh after a transaction, carrying only the last exchange")
    original = smart_wallet_agent.agent
    user = _create()
    confirm = smart_wallet_agent.CONFIRM_BUTTON
    try:
        _use_scripted_agent([
            AIMessage(content="Here's a quote."),
            AIMessage(content="Your balance is 20 USDC."),
            AIMessage(content="Still here."),
        ])
        smart_wallet_agent.chat(user, CHAIN, "send 5 usdc to sam", NETWORK)
        smart_wallet_agent._record_button(user, CHAIN, confirm, SENDING, sent=True)
        check("the approval is added to the conversation, which stays whole until the next turn",
              len(_thread(user)) == 4, str(len(_thread(user))))

        smart_wallet_agent.chat(user, CHAIN, "what's my balance?", NETWORK)
        thread = _thread(user)
        check("the next turn starts afresh: the last exchange, then the new one",
              [(type(m).__name__, m.content) for m in thread] == [
                  ("HumanMessage", confirm), ("AIMessage", SENDING),
                  ("HumanMessage", "what's my balance?"), ("AIMessage", "Your balance is 20 USDC."),
              ], str([(type(m).__name__, m.content) for m in thread]))
        check("the carried messages are marked, and carry no tool traffic",
              all(m.additional_kwargs.get("carried_over") for m in thread[:2]) and not thread[1].tool_calls)
        history = smart_wallet_agent.get_history(user, CHAIN, 50)
        check("the web chat sees where it was cleared, the tap still shown as a button's",
              history[0] == {"role": "user", "text": confirm, "carried_over": True, "button": True}
              and "carried_over" not in history[2], str(history))

        smart_wallet_agent.chat(user, CHAIN, "thanks", NETWORK)
        check("with no new transaction it doesn't clear again", len(_thread(user)) == 6, str(len(_thread(user))))

        failed_user = _create()
        _use_scripted_agent([AIMessage(content="ok")])
        smart_wallet_agent._record_button(failed_user, CHAIN, confirm, "That didn't go through: not enough USDC", sent=False)
        smart_wallet_agent.chat(failed_user, CHAIN, "why?", NETWORK)
        check("a failed send keeps the conversation, so the user can ask why",
              len(_thread(failed_user)) == 4 and _thread(failed_user)[0].content == confirm, str(len(_thread(failed_user))))

        waiting_user = _create()
        _use_scripted_agent([AIMessage(content="Sent.")])
        smart_wallet_agent._record_button(waiting_user, CHAIN, confirm, SENDING, sent=True)
        pending = quotes.put(waiting_user, CHAIN, 1, "Transfer half to tim", [], None, {})
        try:
            smart_wallet_agent.chat(waiting_user, CHAIN, "and the rest?", NETWORK)
            check("a quote still waiting for an answer keeps the conversation",
                  _thread(waiting_user)[0].content == confirm and len(_thread(waiting_user)) == 4,
                  str(len(_thread(waiting_user))))
        finally:
            quotes.drop(waiting_user, pending.quote_id)

        long_user = _create()
        _use_scripted_agent([AIMessage(content="x" * 400), AIMessage(content="short")])
        saved_limit = smart_wallet_agent.HISTORY_TOKEN_LIMIT
        smart_wallet_agent.HISTORY_TOKEN_LIMIT = 50
        try:
            smart_wallet_agent.chat(long_user, CHAIN, "tell me a lot", NETWORK)
            smart_wallet_agent.chat(long_user, CHAIN, "and now?", NETWORK)
        finally:
            smart_wallet_agent.HISTORY_TOKEN_LIMIT = saved_limit
        check("a conversation over the size limit starts afresh too, with no transaction",
              [m.content for m in _thread(long_user)] == ["tell me a lot", "x" * 400, "and now?", "short"]
              and _thread(long_user)[0].additional_kwargs.get("carried_over"), str([m.content[:12] for m in _thread(long_user)]))
    finally:
        smart_wallet_agent.agent = original


def test_approvals_are_sent_and_recorded():
    """
    A card approved with a passkey is sent by confirm_signed with no model call, on the request's
    own network. It answers once the transaction is on the wire and waits for it to land in the
    background (landing.py). The tap and its first outcome go into the conversation, one that went
    out starts it afresh on the next turn, and the sign request is closed with the outcome. Cancel,
    /cancel and the approval page's Cancel discard, and are recorded the same way.
    """
    print("\n[7b] an approved card: sent with no model call, recorded in the conversation and its request")
    original_agent = smart_wallet_agent.agent
    original_send = smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted
    user, other = _create(), _create()
    sends = []

    def fake_submit(approved):
        request, action = approved.request, approved.frozen["action"]
        sends.append((request["user_id"], request["chain_id"], db.get_user_network(request["user_id"]), action))
        if action == "fails":
            raise ToolException("UserOp failed! tx: 0xdead")
        if action == "unknown":
            raise ConnectionError("the node hung up")
        return SimpleNamespace(user_id=request["user_id"], chain_id=request["chain_id"], action=action, record=None,
                               text=f"Sending — {action}. Waiting for the network to confirm it.")

    def fake_settle(sending):
        if sending.action == "reverts":
            raise ToolException("UserOp failed! tx: 0xbeef")
        if sending.action == "times out":
            raise TimeoutError("no receipt in 120 s")
        return f"Sent — {sending.action}. Tx hash: `0x{'ab' * 32}`"

    def approve(action: str, turn_id: int = 1):
        """A card offered on the web for `action`, approved: what signing.approve hands confirm_signed."""
        pending = quotes.put(user, CHAIN, turn_id, action, [], PRICED, {})
        request = signing.open_request(pending, signing.WEB).request
        return SimpleNamespace(request=request, frozen=signing.thaw(request), final=None, signature=b"",
                               key_id="0x" + "ab" * 32), pending

    def status_of(request: dict) -> str:
        return db.get_sign_request(request["id"])["status"]

    def last_two(user_id: int) -> list[tuple]:
        return [(type(m).__name__, m.content) for m in _thread(user_id)[-2:]]

    try:
        _use_scripted_agent([AIMessage(content="Transfer 5 USDC to sam. Fees: $0.30. Tap Confirm to send it."),
                             AIMessage(content="Your balance is 20 USDC.")])
        smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted = fake_submit, fake_settle
        reply, turn_id = smart_wallet_agent.chat_turn(user, CHAIN, "send 5 usdc to sam", NETWORK)
        check("a turn gives its id with its reply", turn_id is not None and reply.startswith("Transfer 5 USDC"), reply)
        approved, pending = approve("Transfer 5 USDC to sam", turn_id)
        check("raised_in finds the quote the turn raised",
              [p.quote_id for p in quotes.raised_in(user, CHAIN, turn_id)] == [pending.quote_id])
        check("and nothing for another turn, network or account",
              quotes.raised_in(user, CHAIN, turn_id + 1) == [] and quotes.raised_in(user, BSC, turn_id) == []
              and quotes.raised_in(other, CHAIN, turn_id) == [])

        confirmed = smart_wallet_agent.confirm_signed(approved)
        check("it sends exactly what was approved, on the request's own network, and answers before it lands",
              sends == [(user, CHAIN, routes.deps._network_name(CHAIN), "Transfer 5 USDC to sam")]
              and confirmed.status == "sent"
              and confirmed.text == "Sending — Transfer 5 USDC to sam. Waiting for the network to confirm it.",
              f"{sends} {confirmed}")
        check("what it says once it lands comes from the background wait",
              confirmed.landed is not None and confirmed.landed.result(timeout=5).startswith("Sent — Transfer 5 USDC"),
              str(confirmed))
        check("the tap and its outcome are in the conversation",
              last_two(user) == [("HumanMessage", "✅ Confirm"), ("AIMessage", confirmed.text)], str(last_two(user)))
        check("...and the landing writes nothing more there", len(_thread(user)) == 4, str(len(_thread(user))))
        check("marked as a transaction that went out", _thread(user)[-1].additional_kwargs.get("sent_transaction") is True)
        check("its request is closed as sent, with what the user was told",
              status_of(approved.request) == "sent" and db.get_sign_request(approved.request["id"])["outcome"] == confirmed.text)
        check("and its quote is gone from the store", pending.quote_id not in quotes._pending)

        smart_wallet_agent.chat(user, CHAIN, "what's my balance?", NETWORK)
        check("the next turn starts afresh, carrying the approval's exchange",
              [(type(m).__name__, m.content) for m in _thread(user)] == [
                  ("HumanMessage", "✅ Confirm"), ("AIMessage", confirmed.text),
                  ("HumanMessage", "what's my balance?"), ("AIMessage", "Your balance is 20 USDC."),
              ], str([(type(m).__name__, m.content[:20]) for m in _thread(user)]))

        failing, _ = approve("fails")
        confirmed = smart_wallet_agent.confirm_signed(failing)
        check("a send that can't go out says so, waits for nothing, and isn't marked as gone out",
              confirmed.status == "failed" and confirmed.text == "That didn't go through: UserOp failed! tx: 0xdead"
              and confirmed.reason == "UserOp failed! tx: 0xdead" and confirmed.landed is None
              and confirmed.transaction_id is None
              and not _thread(user)[-1].additional_kwargs.get("sent_transaction"), str(confirmed))
        check("...and its request is closed as failed", status_of(failing.request) == "failed")
        unknown, _ = approve("unknown")
        confirmed = smart_wallet_agent.confirm_signed(unknown)
        check("one whose broadcast is in doubt says to check History before trying again",
              confirmed.status == "unknown" and confirmed.text == smart_wallet_agent.UNKNOWN_OUTCOME
              and last_two(user) == [("HumanMessage", "✅ Confirm"), ("AIMessage", confirmed.text)], str(confirmed))
        reverts, _ = approve("reverts")
        landed = smart_wallet_agent.confirm_signed(reverts).landed
        check("one that went out and then failed says so once it lands",
              landed.result(timeout=5) == "That didn't go through: UserOp failed! tx: 0xbeef")
        stuck, _ = approve("times out")
        landed = smart_wallet_agent.confirm_signed(stuck).landed
        check("one that outlives the wait says to check History before trying again",
              landed.result(timeout=5) == smart_wallet_agent.UNKNOWN_OUTCOME)

        doomed, doomed_quote = approve("Transfer 1 USDC to sam")
        check("Cancel on another network discards nothing", smart_wallet_agent.cancel_quote(user, BSC, doomed_quote.quote_id) is None)
        check("nor another account's", smart_wallet_agent.cancel_quote(other, CHAIN, doomed_quote.quote_id) is None)
        cancelled = smart_wallet_agent.cancel_quote(user, CHAIN, doomed_quote.quote_id)
        check("the account's own Cancel discards it, closes its request, and records it",
              cancelled["id"] == doomed.request["id"] and status_of(doomed.request) == "cancelled"
              and not quotes.has_pending(user, CHAIN)
              and last_two(user) == [("HumanMessage", "✖️ Cancel"), ("AIMessage", "Cancelled. Nothing was sent.")],
              str(last_two(user)))
        check("a second Cancel finds nothing", smart_wallet_agent.cancel_quote(user, CHAIN, doomed_quote.quote_id) is None)
        bare = quotes.put(user, CHAIN, turn_id, "never offered", [], None, {})
        check("a quote that never had a card is cancelled all the same",
              smart_wallet_agent.cancel_quote(user, CHAIN, bare.quote_id) == {})

        from_page, _ = approve("cancelled on its page")
        check("the approval page's Cancel closes its request and records it",
              smart_wallet_agent.cancel_signed(from_page.request) and status_of(from_page.request) == "cancelled"
              and last_two(user) == [("HumanMessage", "✖️ Cancel"), ("AIMessage", "Cancelled. Nothing was sent.")])
        check("...once", not smart_wallet_agent.cancel_signed(from_page.request))

        one, _ = approve("one")
        quotes.put(user, CHAIN, turn_id, "two", [], None, {})
        kept = quotes.put(user, BSC, turn_id, "on BSC", [], None, {})
        gone = smart_wallet_agent.cancel_quotes(user, CHAIN)
        check("/cancel drops every quote on the network in use, and only there, with its request",
              sorted(bool(r) for r in gone) == [False, True] and status_of(one.request) == "cancelled"
              and not quotes.has_pending(user, CHAIN) and quotes.has_pending(user, BSC), str(gone))
        check("and records it", last_two(user) == [("HumanMessage", "/cancel"), ("AIMessage", "Cancelled. Nothing was sent.")],
              str(last_two(user)))
        size = len(_thread(user))
        check("with nothing to drop, it records nothing",
              smart_wallet_agent.cancel_quotes(user, CHAIN) == [] and len(_thread(user)) == size)
        quotes.drop(user, kept.quote_id)
    finally:
        smart_wallet_agent.agent = original_agent
        smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted = original_send


def test_web_quote_cards():
    """
    The web chat's quote card: a turn that raised exactly one quote offers it, GET /api/chat/quote
    shows it (after a reload too), and the user's passkey approves it through the one sign flow --
    /api/sign/start builds the op and its challenge, /api/sign/finish checks the approval and sends
    -- for the token's own account and the chain named, once. It answers once the transaction is on
    the wire, with the History row GET /api/transactions/{id} follows, and runs on a tap's slot, so
    it goes through while every chat slot is taken. Cancel discards it.
    """
    print("\n[7c] the web chat's card: offered by the turn, approved by a passkey, checked on the way")
    import routes.chat
    original_agent, original_turn = smart_wallet_agent.agent, routes.chat.chat_turn
    original_send = smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted
    original_build = {name: getattr(signing, name) for name in
                      ("load_session_handler", "load_entry_point", "resolve_bundler", "finalize_user_op")}
    saved_env = {name: os.environ.get(name) for name in ("WEBAUTHN_RP_ID", "WEBAUTHN_ORIGINS")}
    os.environ["WEBAUTHN_RP_ID"], os.environ["WEBAUTHN_ORIGINS"] = "localhost", "http://localhost:3000"
    c = make_client()
    me, headers = _sign_up(c)
    other, other_headers = _sign_up(c)
    phone = SoftPasskey(rp_id="localhost", origin="http://localhost:3000")
    db.save_passkey(me, phone.key_id, phone.credential_id_b64, phone.cose_public_key(),
                    "0x" + phone.qx.hex(), "0x" + phone.qy.hex(), "Test phone")
    sends, to_quote, built = [], [], []
    land = threading.Event()
    wallet_keys = {"keys": [(bytes.fromhex(phone.key_id[2:]), 2**40)]}

    def fake_finalize(user_id, wallet, entry_point, quote, bundler, op_deadline):
        """The op the approval signs: its hash a fresh challenge each time, as a fresh nonce makes it."""
        built.append(op_deadline)
        op = (WALLET, len(built), b"", bytes.fromhex(quote.calldata[2:]), b"\x00" * 32, 60_000, b"\x00" * 32, b"", b"")
        return FinalUserOp(op, secrets.token_bytes(32), 2 * 10**9, 10**9)

    def fake_submit(approved):
        action = approved.frozen["action"]
        sends.append((approved.request["user_id"], approved.request["chain_id"],
                      db.get_user_network(approved.request["user_id"]), action, approved.key_id))
        record = db.add_transaction(approved.request["user_id"], CHAIN, WALLET, "assistant", action, "pending",
                                    user_op_hash="0x" + approved.final.user_op_hash.hex())
        return SimpleNamespace(user_id=approved.request["user_id"], chain_id=CHAIN, action=action, record=record,
                               text=f"Sending — {action}. Waiting for the network to confirm it.")

    def fake_settle(sending):
        land.wait(timeout=10)
        db.settle_transaction(sending.record, "confirmed", "0x" + "ab" * 32, 1_790_000_000)
        return f"Sent — {sending.action}. Tx hash: `0x{'ab' * 32}`"

    def quoting_turn(user_id, chain_id, text, network):
        # The real turn, then the quotes a write tool would have raised in it.
        reply, turn_id = original_turn(user_id, chain_id, text, network)
        for action in to_quote:
            quotes.put(user_id, chain_id, turn_id, action, [], PRICED, {"total_usd": 0.3512})
        return reply, turn_id

    def card(headers=headers, chain=CHAIN):
        r = c.get(f"/api/chat/quote?chain_id={chain}", headers=headers)
        return r.json()["quote"] if r.status_code == 200 else r.status_code

    def ask(text: str, *actions: str) -> int:
        to_quote[:] = actions
        return c.post("/api/chat", json={"chain_id": CHAIN, "message": text}, headers=headers).status_code

    def start(quote_id: str, chain=CHAIN, headers=headers):
        return c.post("/api/sign/start", json={"chain_id": chain, "quote_id": quote_id}, headers=headers)

    def finish(sign_id: str, credential: dict, headers=headers):
        return c.post("/api/sign/finish", json={"sign_id": sign_id, "credential": credential}, headers=headers)

    def approval(started: dict, **kwargs) -> dict:
        return phone.assertion(base64url_to_bytes(started["challenge"]), **kwargs)

    def cancel(quote_id: str, headers=headers):
        return c.post("/api/chat/quote/cancel", json={"chain_id": CHAIN, "quote_id": quote_id}, headers=headers)

    def history() -> list:
        return c.get(f"/api/chat/history?chain_id={CHAIN}", headers=headers).json()["messages"]

    try:
        _use_scripted_agent([AIMessage(content=f"reply {n}") for n in range(14)])
        smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted = fake_submit, fake_settle
        signing.load_session_handler = lambda _user: SimpleNamespace(
            address=WALLET, functions=SimpleNamespace(getSessions=lambda: _Call(tuple(zip(*wallet_keys["keys"])) or ([], []))))
        signing.load_entry_point = lambda _user: None
        signing.resolve_bundler = lambda: "bundler"
        signing.finalize_user_op = fake_finalize
        routes.chat.chat_turn = quoting_turn

        check("the card needs a token", c.get(f"/api/chat/quote?chain_id={CHAIN}").status_code == 401)
        check("an unsupported chain -> 400", card(chain=999999) == 400)
        check("no quote, no card", card() is None)

        r = c.post("/api/chat", json={"chain_id": CHAIN, "message": "send 5 usdc to sam"}, headers=headers)
        to_quote[:] = []
        check("a chat reply is unchanged", r.status_code == 200 and r.json() == {"reply": "reply 0"}, r.text[:160])
        check("with no quote raised, still no card", card() is None)

        check("a turn that raised one quote", ask("send 5 usdc to sam", "Transfer 5 USDC to sam") == 200)
        shown = card()
        pending = quotes._pending.get(shown["quote_id"]) if isinstance(shown, dict) else None
        check("offers it on a card written from the stored quote",
              pending is not None and shown == {
                  "quote_id": pending.quote_id, "chain_id": CHAIN, "action": "Transfer 5 USDC to sam",
                  "fees": "$0.35", "expires_at": int(pending.created_at + quotes.QUOTE_TTL_SECONDS)}, str(shown))
        check("only to its own account, on its own chain", card(other_headers) is None and card(chain=BSC) is None)
        request = db.get_sign_request_by_quote(me, CHAIN, pending.quote_id)
        check("with a sign request that froze it, offered on the web, with no link token",
              request["origin"] == "web" and request["token_hash"] is None and request["status"] == "open")

        check("a malformed quote id -> 422", start("../x").status_code == 422)
        check("another account's start -> 404", start(pending.quote_id, headers=other_headers).status_code == 404)
        check("so is one naming another chain", start(pending.quote_id, chain=BSC).status_code == 404)
        check("a start needs a token", c.post("/api/sign/start", json={"chain_id": CHAIN, "quote_id": pending.quote_id})
              .status_code == 401)

        wallet_keys["keys"] = []
        r = start(pending.quote_id)
        check("with no passkey of the account's approved on the wallet -> 409, saying so",
              r.status_code == 409 and r.json()["detail"] == signing.NO_PASSKEY, f"{r.status_code} {r.text[:160]}")
        wallet_keys["keys"] = [(bytes.fromhex(phone.key_id[2:]), int(time.time()) + 30)]
        check("...nor with one about to run out", start(pending.quote_id).status_code == 409)
        wallet_keys["keys"] = [(bytes.fromhex(phone.key_id[2:]), 2**40)]

        r = start(pending.quote_id)
        started = r.json() if r.status_code == 200 else {}
        check("a start answers what the browser asks the passkey with",
              r.status_code == 200 and started["rp_id"] == "localhost" and started["user_verification"] == "required"
              and started["allow_credentials"] == [{"type": "public-key", "id": phone.credential_id_b64}]
              and started["summary"]["action"] == "Transfer 5 USDC to sam" and started["summary"]["chain_id"] == CHAIN
              and started["expires_at"] == request["expires_at"], f"{r.status_code} {r.text[:300]}")
        check("its op carries its own deadline: the request's expiry plus time to send",
              built[-1] == request["expires_at"] + userop.OP_DEADLINE_GRACE_SECS, str(built))
        again = start(pending.quote_id).json()
        check("each start builds afresh, under a new id and challenge",
              again["sign_id"] != started["sign_id"] and again["challenge"] != started["challenge"])
        r = finish(started["sign_id"], approval(started))
        check("an approval of an older build -> 409, nothing sent", r.status_code == 409 and sends == [],
              f"{r.status_code} {r.text[:160]}")
        started = again

        r = finish(started["sign_id"], approval(started, user_verified=False))
        check("an approval without Face ID or a PIN -> 400, nothing sent", r.status_code == 400 and sends == [],
              f"{r.status_code} {r.text[:160]}")
        r = finish(started["sign_id"], approval(started, origin="https://evil.example"))
        check("one made on another site -> 400", r.status_code == 400 and sends == [], f"{r.status_code} {r.text[:160]}")
        stranger = SoftPasskey(credential_id=phone.credential_id, rp_id="localhost", origin="http://localhost:3000")
        r = finish(started["sign_id"], stranger.assertion(base64url_to_bytes(started["challenge"])))
        check("one by another key under the same credential id -> 400", r.status_code == 400 and sends == [],
              f"{r.status_code} {r.text[:160]}")
        r = finish(started["sign_id"], phone.assertion(secrets.token_bytes(32)))
        check("one over another challenge -> 400", r.status_code == 400 and sends == [], f"{r.status_code} {r.text[:160]}")
        r = finish(started["sign_id"], approval(started), headers=other_headers)
        check("another account's finish -> 409", r.status_code == 409 and sends == [], f"{r.status_code} {r.text[:160]}")
        with routes.chat._chat_turn(me):
            r = finish(started["sign_id"], approval(started))
        check("one while a message is being answered -> 409", r.status_code == 409 and "still answering" in r.text,
              f"{r.status_code} {r.text[:160]}")
        check("the request is still open after all of them", db.get_sign_request(request["id"])["status"] == "open")

        # Every chat slot taken by other people's turns: a tap has slots of its own.
        taken = [routes.chat._chat_slots.acquire(blocking=False) for _ in range(routes.chat.CHAT_MAX_CONCURRENT)]
        try:
            r = finish(started["sign_id"], approval(started))
        finally:
            for _ in filter(None, taken):
                routes.chat._chat_slots.release()
        sent_row = r.json().get("transaction_id") if r.status_code == 200 else None
        check("the account's own approval sends exactly the quote, on the chain's network, though every chat slot is taken",
              all(taken) and r.status_code == 200
              and r.json() == {"status": "sent",
                               "outcome": "Sending — Transfer 5 USDC to sam. Waiting for the network to confirm it.",
                               "transaction_id": sent_row}
              and isinstance(sent_row, int)
              and sends == [(me, CHAIN, routes.deps._network_name(CHAIN), "Transfer 5 USDC to sam", phone.key_id)],
              f"{r.status_code} {r.text[:160]} {sends}")
        row = c.get(f"/api/transactions/{sent_row}", headers=headers)
        check("it answered before landing: its History row is pending, and waited on",
              row.status_code == 200 and row.json()["status"] == "pending" and landing.is_watched(sent_row)
              and row.json()["action"] == "Transfer 5 USDC to sam", f"{row.status_code} {row.text[:160]}")
        check("another account can't read it", c.get(f"/api/transactions/{sent_row}", headers=other_headers).status_code == 404)
        check("the History tab's token list is still its own route",
              c.get("/api/transactions/tokens", headers=headers).status_code == 200)
        land.set()
        deadline = time.time() + 5
        while landing.is_watched(sent_row) and time.time() < deadline:
            time.sleep(0.02)
        row = c.get(f"/api/transactions/{sent_row}", headers=headers).json()
        check("once it lands, the same row is confirmed, with its hash",
              row["status"] == "confirmed" and row["tx_hash"] == "0x" + "ab" * 32 and not landing.is_watched(sent_row),
              str(row))
        orphan = db.add_transaction(me, CHAIN, WALLET, "assistant", "Lost wait", "pending", user_op_hash="0x" + "cd" * 32)
        settled, saved_settle = [], tx_history.settle_one

        def settle_from_chain(found, _web3_for):
            settled.append(found["id"])
            db.settle_transaction(found["id"], "confirmed", "0x" + "ef" * 32, 1_790_000_001)

        tx_history.settle_one = settle_from_chain
        try:
            row = c.get(f"/api/transactions/{orphan}", headers=headers).json()
        finally:
            tx_history.settle_one = saved_settle
        check("a pending row nobody here waits on (a wait lost to a restart) is settled from the chain when read",
              settled == [orphan] and row["status"] == "confirmed", f"{settled} {row}")
        check("the approval and its outcome are in the history, marked as a button's",
              history()[-2:] == [{"role": "user", "text": "✅ Confirm", "button": True},
                                 {"role": "assistant", "text": r.json()["outcome"], "button": True}], str(history()[-2:]))
        check("the card is gone, and its request closed as sent",
              card() is None and db.get_sign_request(request["id"])["status"] == "sent")
        check("a second approval sends nothing", finish(started["sign_id"], approval(started)).status_code == 409
              and start(pending.quote_id).status_code == 404 and len(sends) == 1)

        check("the next turn starts afresh", ask("thanks") == 200)
        check("carrying the approval's exchange, still marked as one",
              history()[0] == {"role": "user", "text": "✅ Confirm", "carried_over": True, "button": True}
              and history()[1]["button"] is True, str(history()[:2]))

        ask("two things", "one", "two")
        check("two quotes from one turn: no card, the user says which in words", card() is None)
        quotes.drop_all(me, CHAIN)

        ask("first", "Transfer 1 USDC to sam")
        first = card()
        ask("second", "Transfer 2 USDC to sam")
        second = card()
        check("the newest offer is the card", second["action"] == "Transfer 2 USDC to sam", str(second))
        check("and it voided the earlier one: its passkey can no longer approve it (2026-10-22, the old card came "
              "back once the new one was dealt with, and could pay twice)",
              first["quote_id"] not in quotes._pending and start(first["quote_id"]).status_code == 404)
        check("another account's Cancel -> 409, and keeps it",
              cancel(second["quote_id"], headers=other_headers).status_code == 409 and second["quote_id"] in quotes._pending)
        r = cancel(second["quote_id"])
        check("Cancel discards it", r.status_code == 200 and r.json() == {"outcome": smart_wallet_agent.CANCELLED_REPLY}
              and second["quote_id"] not in quotes._pending, f"{r.status_code} {r.text[:160]}")
        check("its passkey can no longer approve it", start(second["quote_id"]).status_code == 404)
        check("and is recorded as a button's",
              history()[-2:] == [{"role": "user", "text": "✖️ Cancel", "button": True},
                                 {"role": "assistant", "text": smart_wallet_agent.CANCELLED_REPLY, "button": True}],
              str(history()[-2:]))
        check("no earlier card comes back", card() is None, str(card()))
        check("a second Cancel -> 409", cancel(second["quote_id"]).status_code == 409)

        ask("third", "Transfer 3 USDC to sam")
        third = card()

        def failed_turn(user_id, chain_id, text, network):
            quotes.put(user_id, chain_id, 99_999, "half-quoted", [], PRICED, {"total_usd": 1})
            return "Sorry, something went wrong while handling that. Please try again.", None

        routes.chat.chat_turn = failed_turn
        ask("send 3 usdc to sam")
        check("a turn that failed offers nothing, whatever it raised, and voids nothing", card() == third, str(card()))

        check("clearing the chat takes the card with it",
              c.delete(f"/api/chat/history?chain_id={CHAIN}", headers=headers).status_code == 200 and card() is None)
    finally:
        land.set()
        smart_wallet_agent.agent, routes.chat.chat_turn = original_agent, original_turn
        smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted = original_send
        for name, value in original_build.items():
            setattr(signing, name, value)
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        quotes.drop_all(me, CHAIN)


def test_deleting_the_chat():
    print("\n[8] DELETE /api/chat/history: per chain or all, refused mid-turn, quotes go with it")
    original = smart_wallet_agent.agent
    c = make_client()
    me, headers = _sign_up(c)
    other, _ = _sign_up(c)
    try:
        _use_scripted_agent([AIMessage(content=f"reply {n}") for n in range(4)])
        smart_wallet_agent.chat(me, CHAIN, "hi on sepolia", NETWORK)
        smart_wallet_agent.chat(me, BSC, "hi on bsc", BSC_NETWORK)
        smart_wallet_agent.chat(other, CHAIN, "theirs", NETWORK)
        quote = quotes.put(me, CHAIN, 1, "Transfer 1 USDC to sam", [], None, {})
        db.add_transaction(me, CHAIN, WALLET, "assistant", "kept", "confirmed", tx_hash=_hash(0x501))

        check("deleting needs a token", c.delete(f"/api/chat/history?chain_id={CHAIN}").status_code == 401)
        check("an unsupported chain -> 400",
              c.delete("/api/chat/history?chain_id=999999", headers=headers).status_code == 400)

        with smart_wallet_agent._turn_running(smart_wallet_agent.thread_id(me, CHAIN)):
            r = c.delete(f"/api/chat/history?chain_id={CHAIN}", headers=headers)
        check("refused with 409 while the assistant is answering there", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
        check("...and nothing was deleted", len(_thread(me)) == 2)

        r = c.delete(f"/api/chat/history?chain_id={CHAIN}", headers=headers)
        check("deletes one chain's conversation", r.status_code == 200 and _thread(me) == [], f"{r.status_code} {r.text[:120]}")
        check("its quotes go with it", not quotes.has_pending(me, CHAIN) and quote.quote_id not in quotes._pending)
        check("the other chain's conversation is kept", len(_thread(me, BSC)) == 2)
        check("another account's is untouched", len(_thread(other)) == 2)
        check("the transaction history is kept", [t["action"] for t in db.get_transactions(me)] == ["kept"])

        with smart_wallet_agent._turn_running(smart_wallet_agent.thread_id(me, BSC)):
            r = c.delete("/api/chat/history", headers=headers)
        check("deleting every chain is refused if any is busy", r.status_code == 409 and len(_thread(me, BSC)) == 2)
        r = c.delete("/api/chat/history", headers=headers)
        check("with no chain named, every chain's conversation goes",
              r.status_code == 200 and _thread(me, BSC) == [] and CHAIN in r.json()["chain_ids"], f"{r.status_code} {r.text[:160]}")
    finally:
        smart_wallet_agent.agent = original


# ── Activity outside Mitfah ──────────────────────────────────────────────────

OUT_OWNER = _addr(0x0A8)
SPAM = _addr(0x5BA3)        # a token Mitfah doesn't list: airdrop spam
LP_PAIR = _addr(0x1B1B)


def _move(n: int, *, sender: str, recipient: str, amount: int, token: str | None = None, decimals: int | None = None,
          block: int = 100, direct_call: bool = False, input: bytes | None = None, succeeded: bool = True,
          mined_at: int = 1_700_000_000) -> explorers.Movement:
    return explorers.Movement(
        tx_hash=_hash(n), block=block, mined_at=mined_at, sender=sender, recipient=recipient, token=token,
        amount=amount, decimals=decimals, direct_call=direct_call, input=input, succeeded=succeeded,
    )


class FakeExplorer:
    """Stands in for explorers.movements_since: scripted batches, and where each search started."""

    def __init__(self, batches=(), error: Exception | None = None):
        self.batches = list(batches)
        self.error = error
        self.starts: list[int] = []

    def __call__(self, chain_id, wallet, from_block):
        self.starts.append(from_block)
        yield from self.batches
        if self.error is not None:
            raise self.error


class _OutsideEth:
    """Every wallet answers ENTRY_POINT; no token answers decimals."""

    def contract(self, address, abi):
        return SimpleNamespace(functions=SimpleNamespace(
            ENTRY_POINT=lambda: _Call(ENTRY_POINT), decimals=lambda: _Call(Exception("no code"))))


OUTSIDE_W3 = SimpleNamespace(eth=_OutsideEth())


@contextmanager
def _explorer(fake: FakeExplorer):
    saved = explorers.movements_since
    explorers.movements_since = fake
    try:
        yield
    finally:
        explorers.movements_since = saved


def _outside(user: int, wallet: str) -> dict[str, str]:
    """The wallet's outside rows: {hash: action}."""
    return {r["tx_hash"]: r["action"] for r in db.get_transactions(user) if r["source"] == "outside" and r["wallet"] == wallet}


def test_outside_activity_is_recorded_and_worded():
    """
    Activity that didn't go through Mitfah is listed from what it moved, in the History tab's words.
    Spam and noise stay out: tokens Mitfah doesn't know, zero amounts (address poisoning), failed
    movements, and calls that moved nothing. Searching the same blocks again adds nothing.
    """
    print("\n[11] outside activity: recorded once, worded from what moved, spam left out")
    user = _create(OUT_OWNER)
    wallet = _addr(0x0A7)
    db.save_custom_token(user, CHAIN, PEPE, "pepe", "Pepe", 6)
    db.save_lp_token(user, CHAIN, USDC, "usdc", ETH_SENTINEL, "eth")
    pool = db.get_lp_tokens(user, CHAIN)[0]
    db.set_lp_token_pair(user, CHAIN, pool["token0"], pool["token1"], LP_PAIR, 6, 18)
    pause = bytes(HexBytes(WALLET_CONTRACT.encode_abi("pause", args=[])))
    movements = [
        _move(1, sender=SAM, recipient=wallet, amount=100_000_000, token=USDC, decimals=6),
        _move(2, sender=STRANGER, recipient=wallet, amount=10**24, token=SPAM, decimals=18),
        _move(3, sender=STRANGER, recipient=wallet, amount=0, token=USDC, decimals=6),
        _move(4, sender=wallet, recipient=STRANGER, amount=0, token=USDC, decimals=6),
        _move(5, sender=STRANGER, recipient=wallet, amount=0, direct_call=True, input=b"\x12\x34\x56\x78"),
        _move(6, sender=OUT_OWNER, recipient=wallet, amount=0, direct_call=True, input=pause),
        _move(7, sender=OUT_OWNER, recipient=wallet, amount=2 * 10**17, direct_call=True, input=b""),
        # The owner's own UserOperation, sent from another app: the fee and the transfer, one line.
        _move(8, sender=wallet, recipient=ENTRY_POINT, amount=3 * 10**14),
        _move(8, sender=wallet, recipient=SAM, amount=5_000_000, token=USDC, decimals=6),
        _move(9, sender=wallet, recipient=SAM, amount=10**18, succeeded=False),
        # An owner call the app has no words for, which moved something: worded by what moved.
        _move(10, sender=OUT_OWNER, recipient=wallet, amount=0, direct_call=True, input=b"\xde\xad\xbe\xef"),
        _move(10, sender=wallet, recipient=SAM, amount=10**18),
        _move(11, sender=STRANGER, recipient=wallet, amount=12_500_000, token=PEPE),
        _move(12, sender=wallet, recipient=STRANGER, amount=5 * 10**17, token=LP_PAIR),
        _move(13, sender=OUT_OWNER, recipient=wallet, amount=0, direct_call=True, input=b""),
    ]
    fake = FakeExplorer([(movements, 500)])
    with _explorer(fake):
        tx_history.sync_outside(user, CHAIN, wallet, OUTSIDE_W3)

    expected = {
        _hash(1): f"Received 100 USDC from {SAM}",
        _hash(6): "Pause the wallet",
        _hash(7): "Add 0.2 ETH to the wallet",
        _hash(8): f"Paid 0.0003 ETH in network fees · Sent 5 USDC to {SAM}",
        _hash(10): f"Sent 1 ETH to {SAM}",
        _hash(11): f"Received 12.5 PEPE from {STRANGER}",
        _hash(12): f"Sent 0.5 ETH/USDC LP to {STRANGER}",
    }
    got = _outside(user, wallet)
    for tx_hash, action in expected.items():
        check(f"listed as “{action}”", got.get(tx_hash) == action, str(got.get(tx_hash)))
    check("nothing else: unknown tokens, zero amounts, failures, and calls that moved nothing are left out",
          set(got) == set(expected), str(sorted(set(got) - set(expected))))
    rows = [r for r in db.get_transactions(user) if r["source"] == "outside"]
    check("each is confirmed, with the time its block was mined",
          all(r["status"] == "confirmed" and r["mined_at"] == 1_700_000_000 for r in rows), str(rows[:1]))
    check("where the next search starts is saved", db.get_history_sync(CHAIN, wallet)["next_block"] == 500,
          str(db.get_history_sync(CHAIN, wallet)))

    with _explorer(fake):
        tx_history.sync_outside(user, CHAIN, wallet, OUTSIDE_W3)
    check("the next search starts there", fake.starts == [0, 500], str(fake.starts))
    check("searching the same blocks again adds nothing", len(_outside(user, wallet)) == len(expected),
          str(len(_outside(user, wallet))))


def test_outside_rows_never_repeat_mitfahs_own():
    """
    A transaction Mitfah already lists for this wallet is never listed again as outside activity,
    even when the explorer search ran first. A payment between two Mitfah users is still outside
    activity for the one who received it.
    """
    print("\n[12] outside activity: Mitfah's own transactions are never listed twice")
    payer, payee = _create(_addr(0x0B1)), _create(_addr(0x0B2))
    payer_wallet, payee_wallet = _addr(0x0B3), _addr(0x0B4)
    paid = _hash(0xB00)
    db.add_transaction(payer, CHAIN, payer_wallet, "assistant", "Send 1 USDC", "confirmed",
                       tx_hash=paid, user_op_hash=_hash(0xB01))
    payment = _move(0xB00, sender=payer_wallet, recipient=payee_wallet, amount=10**6, token=USDC, decimals=6)

    with _explorer(FakeExplorer([([payment], 10)])):
        tx_history.sync_outside(payer, CHAIN, payer_wallet, OUTSIDE_W3)
        tx_history.sync_outside(payee, CHAIN, payee_wallet, OUTSIDE_W3)
    check("the payer's own send is not listed again", _outside(payer, payer_wallet) == {}, str(_outside(payer, payer_wallet)))
    check("the payee sees it as received", _outside(payee, payee_wallet) == {paid: f"Received 1 USDC from {payer_wallet}"},
          str(_outside(payee, payee_wallet)))

    # The search runs while an assistant send is still pending, before its row knows its hash.
    pending = db.add_transaction(payer, CHAIN, payer_wallet, "assistant", "Send 2 USDC", "pending", user_op_hash=_hash(0xB02))
    early, owner_early = _hash(0xB03), _hash(0xB04)
    batch = [
        _move(0xB03, sender=payer_wallet, recipient=payee_wallet, amount=2 * 10**6, token=USDC, decimals=6),
        _move(0xB04, sender=STRANGER, recipient=payer_wallet, amount=10**17),
    ]
    with _explorer(FakeExplorer([(batch, 20)])):
        tx_history.sync_outside(payer, CHAIN, payer_wallet, OUTSIDE_W3)
        tx_history.sync_outside(payee, CHAIN, payee_wallet, OUTSIDE_W3)
    check("before the send settles, the search lists it", set(_outside(payer, payer_wallet)) == {early, owner_early},
          str(_outside(payer, payer_wallet)))
    db.settle_transaction(pending, "confirmed", early, 1_700_000_100)
    check("once it settles with that hash, the outside copy goes", set(_outside(payer, payer_wallet)) == {owner_early},
          str(_outside(payer, payer_wallet)))
    db.add_transaction(payer, CHAIN, payer_wallet, "owner", "Add 0.1 ETH to the wallet", "confirmed", tx_hash=owner_early)
    check("an owner transaction recorded later replaces its outside copy too", _outside(payer, payer_wallet) == {},
          str(_outside(payer, payer_wallet)))
    check("the payee's rows are their own and stay", set(_outside(payee, payee_wallet)) == {paid, early},
          str(_outside(payee, payee_wallet)))


def test_outside_search_where_it_starts():
    """Etherscan searches by address from block 0. A BSC search (NodeReal, by block range) starts
    where the wallet was created, or from now for a wallet the live chain doesn't know."""
    print("\n[13] outside activity: where a wallet's first search starts")
    user = _create(_addr(0x0C1))
    eth_wallet, bsc_wallet, fork_wallet = _addr(0x0C2), _addr(0x0C3), _addr(0x0C4)
    db.add_transaction(user, BSC, bsc_wallet, "owner", "Create your Mitfah smart wallet", "confirmed", tx_hash=_hash(0xC00))
    db.add_transaction(user, BSC, fork_wallet, "owner", "Create your Mitfah smart wallet", "confirmed", tx_hash=_hash(0xC01))
    saved = explorers.transaction_block, explorers.latest_block
    explorers.transaction_block = lambda _chain, tx_hash: 777 if tx_hash == _hash(0xC00) else None
    explorers.latest_block = lambda _chain: 999
    try:
        for chain, wallet, start in ((CHAIN, eth_wallet, 0), (BSC, bsc_wallet, 777), (BSC, fork_wallet, 999)):
            fake = FakeExplorer()
            with _explorer(fake):
                tx_history.sync_outside(user, chain, wallet, OUTSIDE_W3)
            check(f"chain {chain}: the first search starts at block {start}", fake.starts == [start], str(fake.starts))
    finally:
        explorers.transaction_block, explorers.latest_block = saved


class _HeldPool:
    """Takes the background searches without running them, so a test can run them when it likes."""

    def __init__(self):
        self.jobs: list = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)


def test_outside_searches_run_in_the_background_once_a_minute():
    """Reading the History tab starts a search in the background, never twice at once for a wallet,
    and at most once a minute. A failing explorer is logged without its key and tried a minute later."""
    print("\n[14] outside activity: background searches, once a minute, failures logged safely")
    user = _create(_addr(0x0D1))
    wallet = _addr(0x0D2)
    pool, saved_pool = _HeldPool(), tx_history._outside_pool
    tx_history._outside_pool = pool
    no_web3 = lambda _chain: None  # noqa: E731
    try:
        wallets = {CHAIN: wallet, 31337: wallet}  # anvil: no explorer covers it
        check("a search starts, and the tab is told one is running",
              tx_history.start_outside_sync(user, wallets, no_web3) is True and len(pool.jobs) == 1, str(pool.jobs))
        check("a second read while it runs starts no second search",
              tx_history.start_outside_sync(user, wallets, no_web3) is True and len(pool.jobs) == 1)
        with _explorer(FakeExplorer([([_move(0xD00, sender=SAM, recipient=wallet, amount=10**17)], 30)])):
            pool.run_all()
        check("it records what it found", list(_outside(user, wallet).values()) == [f"Received 0.1 ETH from {SAM}"],
              str(_outside(user, wallet)))
        check("within the minute, nothing starts and nothing is running",
              tx_history.start_outside_sync(user, wallets, no_web3) is False and pool.jobs == [])

        db.get_db().execute("UPDATE history_sync SET synced_at = synced_at - 120 WHERE wallet = ?", (wallet,))
        db.get_db().commit()
        check("a minute later it searches again", tx_history.start_outside_sync(user, wallets, no_web3) is True
              and len(pool.jobs) == 1)
        os.environ["ETHERSCAN_API_KEY"] = "SECRET-KEY-123"
        logged: list[str] = []
        handler = _ListHandler(logged)
        tx_history.log.addHandler(handler)
        try:
            with _explorer(FakeExplorer(error=explorers.ExplorerUnavailable("Etherscan: bad key SECRET-KEY-123"))):
                pool.run_all()
        finally:
            tx_history.log.removeHandler(handler)
        check("a failed search is logged", any("Etherscan" in line for line in logged), str(logged))
        check("...without the key", not any("SECRET-KEY-123" in line for line in logged), str(logged))
        check("...keeps the cursor it had", db.get_history_sync(CHAIN, wallet)["next_block"] == 30)
        check("...and isn't tried again within the minute",
              tx_history.start_outside_sync(user, wallets, no_web3) is False and pool.jobs == [])
    finally:
        tx_history._outside_pool = saved_pool


class _ListHandler(logging.Handler):
    def __init__(self, lines: list[str]):
        super().__init__()
        self.lines = lines

    def emit(self, record):
        self.lines.append(self.format(record))


def test_history_lists_by_time():
    """Outside rows are recorded long after they mined, so the tab orders by time, not by id, and
    its pages carry on from a (time, id) cursor. The first read says whether a search is running."""
    print("\n[15] GET /api/transactions: newest first by time, paged by a (time, id) cursor")
    c = make_client()
    me, headers = _sign_up(c)
    wallet = _addr(0x0E9)
    db.add_transaction(me, CHAIN, wallet, "assistant", "sent today", "confirmed", tx_hash=_hash(0xE01), mined_at=1_700_000_300)
    db.add_transaction(me, CHAIN, wallet, "outside", "received last week", "confirmed", tx_hash=_hash(0xE02), mined_at=1_700_000_100)
    db.add_transaction(me, CHAIN, wallet, "outside", "received yesterday", "confirmed", tx_hash=_hash(0xE03), mined_at=1_700_000_200)
    db.add_transaction(me, CHAIN, wallet, "assistant", "still pending", "pending", user_op_hash=_hash(0xE04))

    pool, saved_pool, saved_web3 = _HeldPool(), tx_history._outside_pool, routes.history._web3_or_none
    tx_history._outside_pool = pool
    set_everywhere(api, "_web3_or_none", lambda _chain: None)
    try:
        page = c.get("/api/transactions?limit=2", headers=headers).json()
        check("newest first by time, the pending one on top",
              [t["action"] for t in page["transactions"]] == ["still pending", "sent today"], str(page))
        check("no wallet, so no search", page["syncing"] is False and pool.jobs == [], str(page))
        rest = c.get(f"/api/transactions?limit=2&before={page['next_before']}", headers=headers).json()
        check("the next page carries on by time",
              [t["action"] for t in rest["transactions"]] == ["received yesterday", "received last week"]
              and rest["next_before"] is None, str(rest))
        check("an outside row says so", rest["transactions"][0]["source"] == "outside")
        check("a malformed cursor -> 422", c.get("/api/transactions?before=12", headers=headers).status_code == 422)

        db.save_wallet_address(me, CHAIN, wallet)
        first = c.get("/api/transactions", headers=headers).json()
        check("with a wallet, the first read starts a search and says so", first["syncing"] is True and len(pool.jobs) == 1,
              str(first["syncing"]))
        c.get(f"/api/transactions?before={page['next_before']}", headers=headers)
        check("a later page starts none", len(pool.jobs) == 1)
    finally:
        tx_history._outside_pool, routes.history._web3_or_none = saved_pool, saved_web3



# ── Filters: incoming, outgoing, dates, amounts, addresses, tokens ───────────


def test_history_filters():
    """
    The History tab's groups and filters, searched on the server. The money filters must all hold
    for ONE movement, and with a group chosen that movement must go that way.
    """
    print("\n[16] filters: in / out / changes, dates, amount, address, token -- the money ones on one movement")
    c = make_client()
    me, headers = _sign_up(c)
    wallet, pair, router = _addr(0xF11), _addr(0xF12), _addr(0xF13)

    def row(n: int, action: str, direction: str, moved: list, mined_at: int):
        db.add_transaction(me, CHAIN, wallet, "assistant", action, "confirmed", tx_hash=_hash(0xF00 + n),
                           mined_at=mined_at, direction=direction, movements=moved)

    row(1, "received 100 USDC", "in", [Moved(IN, USDC, "USDC", 100_000_000, 6, SAM)], 1_700_100_000)
    row(2, "sent 5 USDC", "out", [Moved(OUT, USDC, "USDC", 5_000_000, 6, SAM)], 1_700_200_000)
    row(3, "sent 100 ETH", "out", [Moved(OUT, None, "ETH", 100 * 10**18, 18, STRANGER)], 1_700_300_000)
    row(4, "swapped 100 USDC for 0.03 ETH", "out",
        [Moved(OUT, USDC, "USDC", 100_000_000, 6, pair), Moved(IN, None, "ETH", 3 * 10**16, 18, router)], 1_700_400_000)
    row(5, "paused", "none", [], 1_700_500_000)
    db.add_transaction(me, CHAIN, wallet, "assistant", "sending", "pending", user_op_hash=_hash(0xF06), direction="out")

    saved = routes.history._web3_or_none
    set_everywhere(api, "_web3_or_none", lambda _chain: None)
    try:
        def listed(query: str) -> list[str]:
            r = c.get(f"/api/transactions?{query}", headers=headers)
            return [t["action"] for t in r.json()["transactions"]] if r.status_code == 200 else [f"HTTP {r.status_code}"]

        check("each row says its group", {t["action"]: t["direction"] for t in
              c.get("/api/transactions", headers=headers).json()["transactions"]}["paused"] == "none")
        check("incoming: only what came in", listed("direction=in") == ["received 100 USDC"], str(listed("direction=in")))
        check("outgoing: sends and swaps, a pending send on top",
              listed("direction=out") == ["sending", "swapped 100 USDC for 0.03 ETH", "sent 100 ETH", "sent 5 USDC"],
              str(listed("direction=out")))
        check("wallet changes: what moved nothing", listed("direction=none") == ["paused"], str(listed("direction=none")))

        check("dates: from inclusive, to exclusive",
              listed("since=1700200000&until=1700400000") == ["sent 100 ETH", "sent 5 USDC"],
              str(listed("since=1700200000&until=1700400000")))
        check("at least 100 of something",
              listed("min_amount=100") == ["swapped 100 USDC for 0.03 ETH", "sent 100 ETH", "received 100 USDC"],
              str(listed("min_amount=100")))
        check("exactly 5", listed("min_amount=5&max_amount=5") == ["sent 5 USDC"], str(listed("min_amount=5&max_amount=5")))
        check("exactly 0.03, in whole units", listed("min_amount=0.03&max_amount=0.03") == ["swapped 100 USDC for 0.03 ETH"],
              str(listed("min_amount=0.03&max_amount=0.03")))
        check("a token, any case",
              listed("token=usdc") == ["swapped 100 USDC for 0.03 ETH", "sent 5 USDC", "received 100 USDC"],
              str(listed("token=usdc")))
        check("an address: the other side", listed(f"address={SAM.lower()}") == ["sent 5 USDC", "received 100 USDC"],
              str(listed(f"address={SAM.lower()}")))
        check("an address: or the token's contract",
              listed(f"address={USDC}") == ["swapped 100 USDC for 0.03 ETH", "sent 5 USDC", "received 100 USDC"],
              str(listed(f"address={USDC}")))
        check("amount and token on ONE movement: the swap's 100 is USDC, not ETH",
              listed("token=ETH&min_amount=100") == ["sent 100 ETH"], str(listed("token=ETH&min_amount=100")))
        check("outgoing ETH: the swap's ETH came in, so it isn't",
              listed("direction=out&token=ETH") == ["sent 100 ETH"], str(listed("direction=out&token=ETH")))

        page = c.get("/api/transactions?token=usdc&limit=2", headers=headers).json()
        rest = c.get(f"/api/transactions?token=usdc&limit=2&before={page['next_before']}", headers=headers).json()
        check("a filtered list pages like the full one",
              [t["action"] for t in page["transactions"]] == ["swapped 100 USDC for 0.03 ETH", "sent 5 USDC"]
              and [t["action"] for t in rest["transactions"]] == ["received 100 USDC"] and rest["next_before"] is None,
              f"{page} / {rest}")

        tokens = c.get("/api/transactions/tokens", headers=headers)
        check("the token filter offers what was moved", tokens.json() == {"tokens": ["ETH", "USDC"]}, tokens.text)
        check("per chain", c.get(f"/api/transactions/tokens?chain_id={BSC}", headers=headers).json() == {"tokens": []})
        check("tokens need a token", c.get("/api/transactions/tokens").status_code == 401)
        for bad in ("direction=sideways", "address=0x12", "min_amount=-1", "since=-5", "token=" + "x" * 41):
            check(f"{bad[:24]} -> 422", c.get(f"/api/transactions?{bad}", headers=headers).status_code == 422)
    finally:
        set_everywhere(api, "_web3_or_none", saved)


def test_movements_are_read_after_a_transaction_settles():
    """
    A settled Mitfah transaction's movements are read the next time the History tab is read: an
    owner call from its calldata, an assistant op from its receipt. A failed one keeps the group it
    was recorded with and moved nothing; one the node no longer has is marked read and left alone.
    """
    print("\n[17] movements: read when the history is read, a few at a time, each row once")
    owner = _addr(0x17E)
    user = _create(owner)
    w3 = FakeW3()
    deposit, failed_withdraw, sent, failed_send, gone, pending = (_hash(0x1700 + n) for n in range(6))
    w3.eth.txs[deposit] = {"to": WALLET, "from": owner, "input": HexBytes(b""), "value": 2 * 10**17}
    w3.eth.txs[failed_withdraw] = {**_owner_tx("withdraw", [USDC, 5_000_000, SAM]), "from": owner}
    usdc_transfer = Web3.keccak(text="transfer(address,uint256)")[:4] + SAM_AMOUNT
    w3.eth.txs[sent] = bundle([(USDC, 0, usdc_transfer)])
    w3.eth.receipts[sent] = {"status": 1, "logs": [transfer(USDC, WALLET, SAM, 5_000_000)]}
    w3.eth.txs[failed_send] = bundle([(USDC, 0, usdc_transfer)])

    def add(source, action, status, tx_hash, direction=None):
        return db.add_transaction(user, CHAIN, WALLET, source, action, status, tx_hash=tx_hash, direction=direction)

    ids = {
        "deposit": add("owner", "Add 0.2 ETH to the wallet", "confirmed", deposit, "in"),
        "failed_withdraw": add("owner", "Withdraw 5 USDC to sam", "failed", failed_withdraw, "out"),
        "sent": db.add_transaction(user, CHAIN, WALLET, "assistant", "Transfer 5 USDC", "confirmed",
                                   tx_hash=sent, user_op_hash=_hash(0x1710)),
        "failed_send": db.add_transaction(user, CHAIN, WALLET, "assistant", "Transfer 5 USDC", "failed",
                                          tx_hash=failed_send, user_op_hash=_hash(0x1711), direction="out"),
        "gone": add("owner", "Add 1 ETH to the wallet", "confirmed", gone, "in"),
        "pending": db.add_transaction(user, CHAIN, WALLET, "assistant", "Transfer 1 USDC", "pending",
                                      user_op_hash=_hash(0x1712), direction="out"),
    }

    def state(name: str) -> tuple:
        row = next(r for r in db.get_transactions(user) if r["id"] == ids[name])
        return row["direction"], row["movements_read"], db.get_movements(ids[name])

    saved_batch = tx_history.MOVEMENTS_BATCH
    tx_history.MOVEMENTS_BATCH = 2
    try:
        tx_history.read_movements(user, lambda _chain: w3)
        check("a few per read, newest first", state("gone")[1] == 1 and state("failed_send")[1] == 1
              and state("sent")[1] == 0, f"{state('gone')} {state('failed_send')} {state('sent')}")
    finally:
        tx_history.MOVEMENTS_BATCH = saved_batch
    tx_history.read_movements(user, lambda _chain: w3)

    direction, _, moved = state("deposit")
    check("a deposit: in, from the owner, 0.2 ETH",
          direction == "in" and moved == [{"direction": "in", "token": None, "ticker": "ETH", "amount": str(2 * 10**17),
                                           "amount_value": 0.2, "counterparty": owner}], str(moved))
    check("a failed withdrawal: still outgoing, moved nothing", state("failed_withdraw")[0::2] == ("out", []),
          str(state("failed_withdraw")))
    direction, _, moved = state("sent")
    check("an assistant send: out, read from its receipt",
          direction == "out" and [(m["direction"], m["token"], m["amount"], m["counterparty"]) for m in moved]
          == [("out", USDC, "5000000", SAM)], f"{direction} {moved}")
    check("a failed send: still outgoing, moved nothing", state("failed_send")[0::2] == ("out", []), str(state("failed_send")))
    check("one the node no longer has: marked read, its group kept", state("gone") == ("in", 1, []), str(state("gone")))
    check("a pending one waits until it settles", state("pending")[:2] == ("out", 0), str(state("pending")))
    check("nothing is read twice", db.get_unread_movement_rows(user, 50) == [], str(db.get_unread_movement_rows(user, 50)))


SAM_AMOUNT = bytes(12) + bytes.fromhex(SAM[2:]) + (5_000_000).to_bytes(32, "big")


def test_v3_swaps_are_read_with_the_eth_that_came_back():
    """
    The reader is given every exchange contract on the op's chain, so the ETH Uniswap V3's router
    unwraps for the wallet -- which leaves no log of its own -- is read back as coming in.
    """
    print("\n[17b] movements: the ETH a Uniswap V3 swap pays the wallet is read back, from the router")
    user = _create(_addr(0x17B))
    weth, pool = _addr(0x0E7E), _addr(0x9A13)
    # The chain's wrapped native token, known to this user alone, so no other check sees it.
    db.save_custom_token(user, CHAIN, weth, "weth", "Wrapped Ether", 18)
    router = get_v3_network(CHAIN)["swap_router"]
    swap = swap_router_call("multicall", [2**32, [
        swap_router_call("exactInputSingle", [(USDC, weth, 500, router, 100_000_000, 1, 0)]),
        swap_router_call("unwrapWETH9", [1, WALLET]),
    ]])
    w3 = FakeW3()
    tx_hash = _hash(0x17B0)
    w3.eth.txs[tx_hash] = bundle([(router, 0, swap)])
    w3.eth.receipts[tx_hash] = {"status": 1, "logs": [
        transfer(USDC, WALLET, pool, 100_000_000),
        transfer(weth, pool, router, 3 * 10**16),
        withdrawal(router, 3 * 10**16, weth),
    ]}
    tx_id = db.add_transaction(user, CHAIN, WALLET, "assistant", "Swap 100 USDC for ETH", "confirmed",
                               tx_hash=tx_hash, user_op_hash=_hash(0x17B1))
    tx_history.read_movements(user, lambda _chain: w3)
    moved = [(m["direction"], m["ticker"], m["amount"], m["counterparty"]) for m in db.get_movements(tx_id)]
    check("out the USDC to the pool, in the ETH from the Uniswap V3 router",
          moved == [("out", "USDC", "100000000", pool), ("in", "ETH", str(3 * 10**16), router)], str(moved))


def test_outside_rows_carry_their_movements():
    print("\n[18] outside rows are recorded with what they moved, and their group")
    owner = _addr(0x18E)
    user = _create(owner)
    wallet = _addr(0x0F7)
    pause = bytes(HexBytes(WALLET_CONTRACT.encode_abi("pause", args=[])))
    movements = [
        _move(0x1801, sender=SAM, recipient=wallet, amount=100_000_000, token=USDC, decimals=6),
        _move(0x1802, sender=wallet, recipient=SAM, amount=10**18),
        _move(0x1803, sender=owner, recipient=wallet, amount=0, direct_call=True, input=pause),
    ]
    with _explorer(FakeExplorer([(movements, 500)])):
        tx_history.sync_outside(user, CHAIN, wallet, OUTSIDE_W3)
    rows = {r["tx_hash"]: r for r in db.get_transactions(user)}
    received, sent, paused = rows[_hash(0x1801)], rows[_hash(0x1802)], rows[_hash(0x1803)]
    check("received: in, with the USDC and who sent it",
          received["direction"] == "in" and received["movements_read"] == 1
          and [(m["ticker"], m["amount_value"], m["counterparty"]) for m in db.get_movements(received["id"])]
          == [("USDC", 100.0, SAM)], str(db.get_movements(received["id"])))
    check("sent: out", sent["direction"] == "out" and db.get_movements(sent["id"])[0]["counterparty"] == SAM)
    check("a pause from the explorer: a wallet change", paused["direction"] == "none" and db.get_movements(paused["id"]) == [])


def test_old_databases_are_migrated():
    """A transactions table from before the filters gains its columns. Its outside rows are searched
    for again, so they come back with their movements; Mitfah's own rows stay, to be read later."""
    print("\n[19] an older database: columns added, outside rows searched for again, Mitfah's kept")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            chain_id INTEGER NOT NULL, wallet TEXT NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL,
            status TEXT NOT NULL, tx_hash TEXT, user_op_hash TEXT, op_nonce TEXT, from_block INTEGER,
            created_at INTEGER NOT NULL, mined_at INTEGER);
        CREATE TABLE history_sync (chain_id INTEGER NOT NULL, wallet TEXT NOT NULL, next_block INTEGER,
            synced_at INTEGER NOT NULL, PRIMARY KEY (chain_id, wallet));
        INSERT INTO transactions (user_id, chain_id, wallet, source, action, status, created_at)
            VALUES (1, 1, 'w', 'assistant', 'mine', 'confirmed', 1), (1, 1, 'w', 'outside', 'theirs', 'confirmed', 1);
        INSERT INTO history_sync VALUES (1, 'w', 500, 1);
    """)
    db.schema._migrate_transaction_direction(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    rows = [dict(r) for r in conn.execute("SELECT source, direction, movements_read FROM transactions").fetchall()]
    check("direction and movements_read added", {"direction", "movements_read"} <= set(cols), str(cols))
    check("Mitfah's row kept, unread; the outside one gone",
          rows == [{"source": "assistant", "direction": None, "movements_read": 0}], str(rows))
    check("every explorer search starts over", conn.execute("SELECT COUNT(*) FROM history_sync").fetchone()[0] == 0)
    db.schema._migrate_transaction_direction(conn)
    check("running it again changes nothing", conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1)



if __name__ == "__main__":
    try:
        test_owner_transactions_are_described_from_their_calldata()
        test_owner_transactions_are_recorded_once_and_settled()
        test_assistant_sends_are_recorded_before_they_go()
        test_a_repeated_request_is_not_a_yes()
        test_deposits_put_their_pool_on_the_dashboard()
        test_pending_rows_are_settled_later()
        test_history_route_lists_only_your_own_newest_first()
        test_deposits_from_the_fund_drawer_are_listed()
        test_owner_confirm_says_whether_the_network_has_seen_it()
        test_deploy_confirm_says_whether_the_network_has_seen_it()
        test_the_chat_starts_afresh_after_a_transaction()
        test_approvals_are_sent_and_recorded()
        test_web_quote_cards()
        test_deleting_the_chat()
        test_outside_activity_is_recorded_and_worded()
        test_outside_rows_never_repeat_mitfahs_own()
        test_outside_search_where_it_starts()
        test_outside_searches_run_in_the_background_once_a_minute()
        test_history_lists_by_time()
        test_history_filters()
        test_movements_are_read_after_a_transaction_settles()
        test_v3_swaps_are_read_with_the_eth_that_came_back()
        test_outside_rows_carry_their_movements()
        test_old_databases_are_migrated()
    finally:
        os.unlink(_tmp_db.name)
    finish("All history checks passed.")
