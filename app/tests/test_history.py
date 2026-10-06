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
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

from checks import check, finish, sign_in   # first: it puts app/ on sys.path for the imports below

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
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from web3 import Web3                       # noqa: E402
from web3.exceptions import TimeExhausted, TransactionNotFound  # noqa: E402

import api                                  # noqa: E402
import explorers                            # noqa: E402
import quotes                               # noqa: E402
import smart_wallet_agent                   # noqa: E402
import tools                                # noqa: E402
import tx_history                           # noqa: E402
from agent_context import AgentContext      # noqa: E402
from bundler import UserOpReverted          # noqa: E402
from constants import get_router            # noqa: E402

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
        (_owner_tx("addSession", [_addr(0x5E55), in_30_days]), "Turn the assistant on for 30 days"),
        (_owner_tx("removeSession", []), "Turn the assistant off"),
        (_owner_tx("addTrustedSpender", [Web3.to_checksum_address(get_router(CHAIN))]),
         "Trust the exchange router to spend from the wallet"),
        (_owner_tx("removeTrustedSpender", [STRANGER]), f"Stop trusting {STRANGER[:6]}…{STRANGER[-4:]}"),
        (_owner_tx("setMaxOpGasCost", [10**16]), "Set the network fee cap to 0.01 ETH"),
        ({"to": WALLET, "input": HexBytes(b""), "value": 10**17}, "Add 0.1 ETH to the wallet"),
    ]
    for tx, expected in cases:
        got = says(tx)
        check(f"described as “{expected}”", got == expected, got)
    check("calldata the wallet doesn't know is still a line, not an error",
          says({"to": WALLET, "input": HexBytes(b"\xde\xad\xbe\xef"), "value": 0}) == "Call your wallet")


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


def _patch_confirm(w3, broadcast, budget_left):
    """
    Stubs everything confirm_transaction calls, so only its recording is under test.

    `budget_left` is a dict whose "value" the fake wallet's getRemainingBudget returns -- or raises,
    if it is an exception -- so a test can change it between sends.
    """
    saved = {
        name: getattr(tools, name)
        for name in ("load_network_config", "_resolve_bundler", "load_session_handler", "load_entry_point",
                     "_prepare_user_op", "_broadcast_user_op", "_get_session_keys")
    }
    saved_take = quotes.take
    op_hash = {"n": 0}

    def prepare(*_args):
        op_hash["n"] += 1
        return SimpleNamespace(user_op_hash=bytes([op_hash["n"]]) * 32, op=(WALLET, (7 << 64) | op_hash["n"]), from_block=100)

    tools.load_network_config = lambda _user_id: (w3, CHAIN, NETWORK)
    tools._resolve_bundler = lambda _w3: "bundler"
    def remaining():
        if isinstance(budget_left["value"], Exception):
            raise budget_left["value"]
        return budget_left["value"]

    wallet = SimpleNamespace(
        address=WALLET,
        functions=SimpleNamespace(getRemainingBudget=lambda: SimpleNamespace(call=remaining)),
    )
    tools.load_session_handler = lambda _user_id: wallet
    tools._get_session_keys = lambda _user_id: ("0xkey", "vault:v1:x")
    tools.load_entry_point = lambda _user_id: None
    tools._prepare_user_op = prepare
    tools._broadcast_user_op = broadcast
    quotes.take = lambda *_args: SimpleNamespace(action="Transfer 5 USDC to 0x5A3…", quote=None, lp_pool=None)

    def restore():
        for name, value in saved.items():
            setattr(tools, name, value)
        quotes.take = saved_take

    return restore


def test_assistant_sends_are_recorded_before_they_go():
    """
    A send is recorded BEFORE it is broadcast and settled after, so one that outlives the wait is
    still listed. One that never executed is not listed, one that reverted on chain is, and a
    failure to record never changes what the tool tells the user.
    """
    print("\n[3] the assistant's sends: recorded before they go, settled after, never lost")
    user = _create()
    runtime = SimpleNamespace(context=AgentContext(user_id=user, turn_id=2))
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
    restore = _patch_confirm(w3, broadcast, budget_left)
    try:
        reply = tools.confirm_transaction.func(runtime, "q1")
        newest = db.get_transactions(user)[0]
        check("the row existed, pending, before the op was broadcast", outcome["recorded_before_send"])
        check("a send that went through is confirmed, with the executing transaction's hash and block time",
              newest["status"] == "confirmed" and newest["tx_hash"] == "0x" + "22" * 32
              and newest["mined_at"] == 1_790_000_123 and newest["source"] == "assistant", str(newest))
        check("described by the quote's code-written action", newest["action"] == "Transfer 5 USDC to 0x5A3…", newest["action"])
        check("the chat reply carries the same 0x-prefixed hash", "0x" + "22" * 32 in reply, reply)
        check("...and what is left of the spending limit, so the agent needn't ask for it",
              reply.endswith("Spending limit left this period: $123.45"), reply)

        outcome["next"] = "reverted"
        try:
            tools.confirm_transaction.func(runtime, "q2")
            raised = False
        except ToolException:
            raised = True
        newest = db.get_transactions(user)[0]
        check("a reverted op still fails the tool", raised)
        check("...but is listed as failed, with its hash", newest["status"] == "failed" and newest["tx_hash"] == "0x" + "22" * 32,
              str(newest))

        before = len(db.get_transactions(user))
        outcome["next"] = "not executed"
        try:
            tools.confirm_transaction.func(runtime, "q3")
        except ToolException:
            pass
        check("an op that never executed is not listed", len(db.get_transactions(user)) == before,
              str(db.get_transactions(user)))

        outcome["next"] = "timeout"
        try:
            tools.confirm_transaction.func(runtime, "q4")
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
            reply = tools.confirm_transaction.func(runtime, "q5")
        finally:
            tx_history.add_transaction = saved_add
        check("a history that can't be written never turns a sent payment into an error",
              reply.startswith("Sent —"), reply)

        budget_left["value"] = RuntimeError("rpc down")
        reply = tools.confirm_transaction.func(runtime, "q6")
        check("a budget that can't be read never turns a sent payment into an error either",
              reply.startswith("Sent —") and "Spending limit" not in reply, reply)
        budget_left["value"] = -5 * 10**18
        reply = tools.confirm_transaction.func(runtime, "q7")
        check("a limit lowered below what was spent shows nothing left, not a negative figure",
              reply.endswith("Spending limit left this period: $0.00"), reply)
    finally:
        restore()


def test_deposits_put_their_pool_on_the_dashboard():
    """
    Confirming a liquidity deposit remembers its pool for the dashboard BEFORE the deposit goes out,
    so one that outlives the wait still shows once it lands. Any other send remembers nothing, and a
    pool that can't be saved never turns a sent deposit into an error.
    """
    print("\n[3b] a liquidity deposit puts its pool on the dashboard before it is sent")
    user = _create()
    runtime = SimpleNamespace(context=AgentContext(user_id=user, turn_id=2))
    w3 = FakeW3()
    w3.eth.blocks[101] = 1_790_000_123
    landed = HexBytes(b"\x33" * 32)
    seen = {}

    def broadcast(_user_id, _prepared, _bundler):
        seen["pools"] = db.get_lp_tokens(user, CHAIN)
        return landed, {"status": 1, "transactionHash": landed, "blockNumber": 101}

    weth = _addr(0x0E7E)
    deposit = SimpleNamespace(action="Add liquidity: 100 of USDC and 0.04 of ETH", quote=None,
                              lp_pool={"token_a": weth, "ticker_a": "eth", "token_b": USDC, "ticker_b": "usdc"})
    restore = _patch_confirm(w3, broadcast, {"value": 10**18})
    saved_save = tools._save_lp_token
    try:
        reply = tools.confirm_transaction.func(runtime, "q1")
        check("a plain send remembers no pool", seen["pools"] == [] and db.get_lp_tokens(user, CHAIN) == [])

        quotes.take = lambda *_args: deposit
        reply = tools.confirm_transaction.func(runtime, "q2")
        check("the deposit went out as usual", reply.startswith("Sent —"), reply)
        check("its pool was saved before it was broadcast, in the pool's own order (lower address first)",
              [(p["token0"], p["ticker0"], p["token1"], p["ticker1"]) for p in seen["pools"]]
              == [(USDC, "usdc", weth, "eth")], str(seen["pools"]))
        check("...with nothing read about it yet", seen["pools"][0]["pair"] is None, str(seen["pools"]))

        tools.confirm_transaction.func(runtime, "q3")
        check("a second deposit into the same pool keeps one row", len(db.get_lp_tokens(user, CHAIN)) == 1)

        def broken(*_args, **_kwargs):
            raise RuntimeError("database is locked")

        tools._save_lp_token = broken
        reply = tools.confirm_transaction.func(runtime, "q4")
        check("a pool that can't be saved never turns a sent deposit into an error", reply.startswith("Sent —"), reply)
    finally:
        tools._save_lp_token = saved_save
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

    saved = api._web3_or_none
    api._web3_or_none = lambda _chain: None   # no chain to settle against here
    try:
        check("the history needs a token", c.get("/api/transactions").status_code == 401)
        r = c.get("/api/transactions", headers=headers)
        got = r.json()
        check("readable when signed in", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        check("only this account's, newest first",
              [t["action"] for t in got["transactions"]] == ["mine on bsc", "mine 2", "mine 1", "mine 0"],
              str([t["action"] for t in got["transactions"]]))
        check("each has what the tab shows, and nothing internal",
              set(got["transactions"][0]) == {"id", "chain_id", "source", "action", "status", "tx_hash", "created_at", "mined_at"},
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
        api._web3_or_none = saved


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

    saved = api._resolve_chain, api._load_wallet_for_chain, api._web3_or_none
    api._resolve_chain = lambda _chain: (w3, NETWORK)
    api._load_wallet_for_chain = lambda _w3, _user, _chain: WALLET_CONTRACT
    api._web3_or_none = lambda _chain: w3
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
        api._resolve_chain, api._load_wallet_for_chain, api._web3_or_none = saved


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

    saved = api._resolve_chain, api._load_wallet_for_chain
    api._resolve_chain = lambda _chain: (w3, NETWORK)
    api._load_wallet_for_chain = lambda _w3, _user, _chain: WALLET_CONTRACT
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
        api._resolve_chain, api._load_wallet_for_chain = saved


# Where two accounts' deploys land: the addresses /api/deploy predicted for them.
DEPLOYED = _addr(0xDE9)
DEPLOYED_TOO = _addr(0xDEA)


class FakeDeployEth(FakeEth):
    """FakeEth, plus the wallets already on chain, as /api/deploy/confirm reads them."""

    def __init__(self):
        super().__init__()
        self.wallets: dict[str, tuple[str, str, int]] = {}   # address -> (owner, session key, block made in)
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
            isSessionActive=lambda k: _Call(k == key),
            currentSessionValidUntil=lambda: _Call(1_800_000_000),
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

    saved = api._resolve_chain, api._load_factory_for_chain
    api._resolve_chain = lambda _chain: (w3, NETWORK)
    api._load_factory_for_chain = lambda _w3, _chain: factory
    try:
        body, headers, account = sign_in(c)
        me, owner, key = body["user_id"], account.address, _addr(0x5E55)
        db.save_pending_session_key(me, CHAIN, DEPLOYED, key, "vault:v1:test")

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
              and answer["session_key"] == key and answer["session_key_authorized"] is True,
              f"{r.status_code} {r.text[:200]}")
        check("saved as the account's wallet, its key promoted out of pending",
              db.get_wallet_address(me, CHAIN) == DEPLOYED and db.get_pending_session_key(me, CHAIN, DEPLOYED) is None)
        rows = db.get_transactions(me)
        check("listed under the hash that created it, found one block at a time",
              [(t["action"], t["tx_hash"], t["status"], t["mined_at"]) for t in rows]
              == [("Create your Mitfah smart wallet", sped_up, "confirmed", 1_790_000_900)], str(rows))

        # Another account, on a node that can't read old state: still filed, just not listed.
        body, other_headers, other = sign_in(c)
        db.save_pending_session_key(body["user_id"], CHAIN, DEPLOYED_TOO, key, "vault:v1:test")
        w3.eth.wallets[DEPLOYED_TOO] = (other.address, key, 44)
        w3.eth.archive = False
        r = confirm(_hash(0x604), DEPLOYED_TOO, other_headers, other.address)
        check("a node without old state still files the wallet, just without the History row",
              r.status_code == 200 and db.get_wallet_address(body["user_id"], CHAIN) == DEPLOYED_TOO
              and db.get_transactions(body["user_id"]) == [], f"{r.status_code} {r.text[:200]}")
    finally:
        api._resolve_chain, api._load_factory_for_chain = saved


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


def test_the_chat_starts_afresh_after_a_transaction():
    """
    After a transaction goes out, the next turn starts a fresh conversation carrying only the last
    exchange's text. A failed send, or a quote still waiting for an answer, keeps it; a history
    over the size limit clears it too.
    """
    print("\n[7] the conversation starts afresh after a transaction, carrying only the last exchange")
    original = smart_wallet_agent.agent
    user = _create()
    try:
        _use_scripted_agent([
            AIMessage(content="Here's a quote."),
            _confirm_call("q1"), AIMessage(content="Sent 5 USDC to sam. Anything else?"),
            AIMessage(content="Your balance is 20 USDC."),
            AIMessage(content="Still here."),
        ])
        smart_wallet_agent.chat(user, CHAIN, "send 5 usdc to sam", NETWORK)
        smart_wallet_agent.chat(user, CHAIN, "yes", NETWORK)
        check("the turn that sent keeps its whole conversation", len(_thread(user)) == 6, str(len(_thread(user))))

        smart_wallet_agent.chat(user, CHAIN, "what's my balance?", NETWORK)
        thread = _thread(user)
        check("the next turn starts afresh: the last exchange, then the new one",
              [(type(m).__name__, m.content) for m in thread] == [
                  ("HumanMessage", "yes"), ("AIMessage", "Sent 5 USDC to sam. Anything else?"),
                  ("HumanMessage", "what's my balance?"), ("AIMessage", "Your balance is 20 USDC."),
              ], str([(type(m).__name__, m.content) for m in thread]))
        check("the carried messages are marked, and carry no tool traffic",
              all(m.additional_kwargs.get("carried_over") for m in thread[:2]) and not thread[1].tool_calls)
        history = smart_wallet_agent.get_history(user, CHAIN, 50)
        check("the web chat sees where it was cleared",
              history[0] == {"role": "user", "text": "yes", "carried_over": True}
              and "carried_over" not in history[2], str(history))

        smart_wallet_agent.chat(user, CHAIN, "thanks", NETWORK)
        check("with no new transaction it doesn't clear again", len(_thread(user)) == 6, str(len(_thread(user))))

        failed_user = _create()
        _use_scripted_agent([_confirm_call("bad"), AIMessage(content="That didn't go through."), AIMessage(content="ok")])
        smart_wallet_agent.chat(failed_user, CHAIN, "yes", NETWORK)
        smart_wallet_agent.chat(failed_user, CHAIN, "why?", NETWORK)
        check("a failed send keeps the conversation, so the user can ask why",
              len(_thread(failed_user)) == 6 and _thread(failed_user)[0].content == "yes", str(len(_thread(failed_user))))

        waiting_user = _create()
        _use_scripted_agent([
            _confirm_call("q1"), AIMessage(content="Swapped. Here's the quote for sending half to Tim."),
            AIMessage(content="Sent."),
        ])
        smart_wallet_agent.chat(waiting_user, CHAIN, "yes", NETWORK)
        pending = quotes.put(waiting_user, CHAIN, 1, "Transfer half to tim", [], None, {})
        try:
            smart_wallet_agent.chat(waiting_user, CHAIN, "yes", NETWORK)
            check("a quote still waiting for an answer keeps the conversation",
                  _thread(waiting_user)[0].content == "yes" and len(_thread(waiting_user)) == 6, str(len(_thread(waiting_user))))
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

    pool, saved_pool, saved_web3 = _HeldPool(), tx_history._outside_pool, api._web3_or_none
    tx_history._outside_pool = pool
    api._web3_or_none = lambda _chain: None
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
        tx_history._outside_pool, api._web3_or_none = saved_pool, saved_web3



if __name__ == "__main__":
    try:
        test_owner_transactions_are_described_from_their_calldata()
        test_owner_transactions_are_recorded_once_and_settled()
        test_assistant_sends_are_recorded_before_they_go()
        test_deposits_put_their_pool_on_the_dashboard()
        test_pending_rows_are_settled_later()
        test_history_route_lists_only_your_own_newest_first()
        test_deposits_from_the_fund_drawer_are_listed()
        test_owner_confirm_says_whether_the_network_has_seen_it()
        test_deploy_confirm_says_whether_the_network_has_seen_it()
        test_the_chat_starts_afresh_after_a_transaction()
        test_deleting_the_chat()
        test_outside_activity_is_recorded_and_worded()
        test_outside_rows_never_repeat_mitfahs_own()
        test_outside_search_where_it_starts()
        test_outside_searches_run_in_the_background_once_a_minute()
        test_history_lists_by_time()
    finally:
        os.unlink(_tmp_db.name)
    finish("All history checks passed.")
