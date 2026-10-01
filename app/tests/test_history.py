"""
Offline checks for the History tab and the short chat memory: how transactions are recorded (the
assistant's and the owner's), described, settled and listed, and how the conversation starts afresh
after a transaction or is deleted on request.

Everything runs against a throwaway database, a FAKE chain and a scripted model, so it is safe to
run anywhere. The real journey -- a send on a fork landing in the history with the same hash the
chat reported -- is in test_e2e_fork.

Run: make history-test   (or: python app/tests/test_history.py)
"""
import os
import tempfile
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

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
from web3.exceptions import TransactionNotFound  # noqa: E402

import api                                  # noqa: E402
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


def _create(email: str) -> int:
    return db.create_user(email=email, password_hash="x")


# ── Describing an owner transaction ──────────────────────────────────────────


def test_owner_transactions_are_described_from_their_calldata():
    """
    The History tab's line for an owner transaction is read off its calldata on the server, in the
    words the controls use. Nothing the browser says is involved.
    """
    print("\n[1] owner transactions are described from their own calldata")
    user = _create("describe@example.com")
    db.save_contact(user, "sam", SAM)
    db.link_owner_addr(user, OWNER)
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
    user = _create("owner-record@example.com")
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
          len(deploys) == 1 and deploys[0]["action"] == "Create your Mitfah wallet", str(deploys))


# ── Recording the assistant's transactions ───────────────────────────────────


def _patch_confirm(w3, broadcast):
    """Stubs everything confirm_transaction calls, so only its recording is under test."""
    saved = {
        name: getattr(tools, name)
        for name in ("load_network_config", "_resolve_bundler", "load_session_handler", "load_entry_point",
                     "_prepare_user_op", "_broadcast_user_op")
    }
    saved_take = quotes.take
    op_hash = {"n": 0}

    def prepare(*_args):
        op_hash["n"] += 1
        return SimpleNamespace(user_op_hash=bytes([op_hash["n"]]) * 32, op=(WALLET, (7 << 64) | op_hash["n"]), from_block=100)

    tools.load_network_config = lambda _user_id: (w3, CHAIN, NETWORK)
    tools._resolve_bundler = lambda _w3: "bundler"
    tools.load_session_handler = lambda _user_id: SimpleNamespace(address=WALLET)
    tools.load_entry_point = lambda _user_id: None
    tools._prepare_user_op = prepare
    tools._broadcast_user_op = broadcast
    quotes.take = lambda *_args: SimpleNamespace(action="Transfer 5 USDC to 0x5A3…", key_ciphertext="vault:v1:x", quote=None)

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
    user = _create("assistant-record@example.com")
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

    restore = _patch_confirm(w3, broadcast)
    try:
        reply = tools.confirm_transaction.func(runtime, "q1")
        newest = db.get_transactions(user)[0]
        check("the row existed, pending, before the op was broadcast", outcome["recorded_before_send"])
        check("a send that went through is confirmed, with the executing transaction's hash and block time",
              newest["status"] == "confirmed" and newest["tx_hash"] == "0x" + "22" * 32
              and newest["mined_at"] == 1_790_000_123 and newest["source"] == "assistant", str(newest))
        check("described by the quote's code-written action", newest["action"] == "Transfer 5 USDC to 0x5A3…", newest["action"])
        check("the chat reply carries the same 0x-prefixed hash", "0x" + "22" * 32 in reply, reply)

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
    finally:
        restore()


# ── Settling what was left pending ───────────────────────────────────────────


def test_pending_rows_are_settled_later():
    """
    Reading the History tab finishes what was left pending: an assistant op found on chain later
    (confirmed or failed), one whose nonce another op used (dropped, it can never land), and an
    owner transaction the node no longer knows a day on (dropped).
    """
    print("\n[4] pending rows are settled when the history is read")
    user = _create("settle@example.com")
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


def _sign_up(client: TestClient, email: str) -> tuple[int, dict]:
    body = client.post("/api/auth/signup", json={"email": email, "password": "hunter2hunter2"}).json()
    return body["user_id"], {"Authorization": f"Bearer {body['access_token']}"}


def test_history_route_lists_only_your_own_newest_first():
    print("\n[5] GET /api/transactions: authenticated, per account, newest first, paged")
    c = make_client()
    me, headers = _sign_up(c, "history-route@example.com")
    other, _ = _sign_up(c, "history-other@example.com")
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
        check("a page stops at the limit and points at the next",
              len(page["transactions"]) == 2 and page["next_before"] == page["transactions"][-1]["id"], str(page))
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
    print("\n[6] POST /api/transactions/deposit lists a deposit sent to the wallet, and settles it later")
    c = make_client()
    me, headers = _sign_up(c, "deposit@example.com")
    w3 = FakeW3()
    deposit, elsewhere = _hash(0x401), _hash(0x402)
    w3.eth.txs[deposit] = {"to": WALLET, "input": HexBytes(b""), "value": 2 * 10**17}
    w3.eth.txs[elsewhere] = {"to": STRANGER, "input": HexBytes(b""), "value": 10**18}

    saved = api._resolve_chain, api._load_wallet_for_chain, api._web3_or_none
    api._resolve_chain = lambda _chain: (w3, NETWORK)
    api._load_wallet_for_chain = lambda _w3, _user, _chain: WALLET_CONTRACT
    api._web3_or_none = lambda _chain: w3
    try:
        body = {"chain_id": CHAIN, "tx_hash": deposit}
        check("reporting a deposit needs a token", c.post("/api/transactions/deposit", json=body).status_code == 401)
        r = c.post("/api/transactions/deposit", json=body, headers=headers)
        check("a deposit is accepted", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        rows = db.get_transactions(me)
        check("listed as pending until it mines, described from the transaction",
              len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["action"] == "Add 0.2 ETH to the wallet", str(rows))

        c.post("/api/transactions/deposit", json={"chain_id": CHAIN, "tx_hash": elsewhere}, headers=headers)
        check("a transaction to somebody else's address is not listed", len(db.get_transactions(me)) == 1)
        check("a malformed hash -> 422",
              c.post("/api/transactions/deposit", json={"chain_id": CHAIN, "tx_hash": "0x12"}, headers=headers).status_code == 422)

        w3.eth.receipts[deposit] = {"status": 1, "blockNumber": 9}
        w3.eth.blocks[9] = 1_790_000_900
        listed = c.get("/api/transactions", headers=headers).json()["transactions"]
        check("reading the history settles it", listed[0]["status"] == "confirmed" and listed[0]["mined_at"] == 1_790_000_900,
              str(listed[0]))
    finally:
        api._resolve_chain, api._load_wallet_for_chain, api._web3_or_none = saved


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
    user = _create("fresh@example.com")
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

        failed_user = _create("fresh-failed@example.com")
        _use_scripted_agent([_confirm_call("bad"), AIMessage(content="That didn't go through."), AIMessage(content="ok")])
        smart_wallet_agent.chat(failed_user, CHAIN, "yes", NETWORK)
        smart_wallet_agent.chat(failed_user, CHAIN, "why?", NETWORK)
        check("a failed send keeps the conversation, so the user can ask why",
              len(_thread(failed_user)) == 6 and _thread(failed_user)[0].content == "yes", str(len(_thread(failed_user))))

        waiting_user = _create("fresh-quote@example.com")
        _use_scripted_agent([
            _confirm_call("q1"), AIMessage(content="Swapped. Here's the quote for sending half to Tim."),
            AIMessage(content="Sent."),
        ])
        smart_wallet_agent.chat(waiting_user, CHAIN, "yes", NETWORK)
        pending = quotes.put(waiting_user, CHAIN, 1, "Transfer half to tim", [], "vault:v1:x", None, {})
        try:
            smart_wallet_agent.chat(waiting_user, CHAIN, "yes", NETWORK)
            check("a quote still waiting for an answer keeps the conversation",
                  _thread(waiting_user)[0].content == "yes" and len(_thread(waiting_user)) == 6, str(len(_thread(waiting_user))))
        finally:
            quotes.drop(waiting_user, pending.quote_id)

        long_user = _create("fresh-long@example.com")
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
    me, headers = _sign_up(c, "delete-chat@example.com")
    other, _ = _sign_up(c, "delete-other@example.com")
    try:
        _use_scripted_agent([AIMessage(content=f"reply {n}") for n in range(4)])
        smart_wallet_agent.chat(me, CHAIN, "hi on sepolia", NETWORK)
        smart_wallet_agent.chat(me, BSC, "hi on bsc", BSC_NETWORK)
        smart_wallet_agent.chat(other, CHAIN, "theirs", NETWORK)
        quote = quotes.put(me, CHAIN, 1, "Transfer 1 USDC to sam", [], "vault:v1:x", None, {})
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


if __name__ == "__main__":
    try:
        test_owner_transactions_are_described_from_their_calldata()
        test_owner_transactions_are_recorded_once_and_settled()
        test_assistant_sends_are_recorded_before_they_go()
        test_pending_rows_are_settled_later()
        test_history_route_lists_only_your_own_newest_first()
        test_deposits_from_the_fund_drawer_are_listed()
        test_the_chat_starts_afresh_after_a_transaction()
        test_deleting_the_chat()
    finally:
        os.unlink(_tmp_db.name)
    finish("All history checks passed.")
