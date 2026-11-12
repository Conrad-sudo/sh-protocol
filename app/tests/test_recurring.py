"""
Offline checks for recurring payments: the schedule table (db.recurring), reading schedules off the
module (recurring.py), the keeper's decisions (keeper.pay), and the Controls routes
(routes/recurring.py).

Everything runs against a throwaway database and a FAKE module -- a Python model of
RecurringPaymentModule's rules -- so it is safe to run anywhere. The real thing, on a fork with the
real contracts and a real keeper transaction, is test_recurring_payments in test_e2e_fork.

Run: make recurring-test   (or: python app/tests/test_recurring.py)
"""
import os
import tempfile
from types import SimpleNamespace

from checks import check, finish, set_everywhere, sign_in   # first: it puts app/ on sys.path for the imports below

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"

import db                                   # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

from eth_account import Account             # noqa: E402
from fastapi import HTTPException, status   # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402
from web3 import Web3                       # noqa: E402

import api                                  # noqa: E402
import keeper                               # noqa: E402
import recurring                            # noqa: E402
import routes.recurring                     # noqa: E402
import tx_history                           # noqa: E402
from db.recurring import CANCELLED, FINISHED, GONE, LIVE  # noqa: E402

CHAIN = 11155111
MONTH = 30 * 86_400


def _addr(n: int) -> str:
    return Web3.to_checksum_address(f"0x{n:040x}")


WALLET = _addr(0xA11CE)
MODULE = _addr(0x7EC)
PAYEE = _addr(0xBEEF)
USDC = _addr(0x05DC)
NATIVE = _addr(0)


class Revert(Exception):
    """What a fake call raises: carries revert data the way web3's ContractCustomError does."""

    def __init__(self, signature: str):
        super().__init__(signature)
        self.data = None


class _Call:
    def __init__(self, call, build=None):
        self._call, self._build = call, build

    def call(self, tx=None):
        return self._call(tx or {})

    def build_transaction(self, tx):
        self._call(tx)  # a build estimates gas, which runs the call
        return {**tx, "to": MODULE, "data": "0x", "gas": 100_000}


class FakeModule:
    """RecurringPaymentModule's rules, as Python: enough for sync_wallet and keeper.pay."""

    def __init__(self):
        self.address = MODULE
        self.now = 1_000_000
        self.count: dict[str, int] = {}
        self.schedules: dict[tuple[str, int], dict] = {}
        self.generation: dict[str, int] = {}
        self.refuse: str | None = None
        self.ran: list[tuple[str, int, int, int]] = []

    def create(self, wallet: str, token: str, recipient: str, amount: int, interval: int, first_due=None, end=0):
        sid = self.count.get(wallet, 0) + 1
        self.count[wallet] = sid
        self.schedules[(wallet, sid)] = dict(token=token, next_due=first_due or self.now, end=end, recipient=recipient,
                                             interval=interval, generation=self.generation.get(wallet, 0), amount=amount)
        return sid

    def _get(self, wallet, sid):
        s = self.schedules.get((wallet, sid))
        if s is None:
            return (NATIVE, 0, 0, NATIVE, 0, 0, 0), False
        live = s["amount"] != 0 and s["generation"] == self.generation.get(wallet, 0)
        return (s["token"], s["next_due"], s["end"], s["recipient"], s["interval"], s["generation"], s["amount"]), live

    def _run(self, wallet, sid):
        (_t, due, end, _r, interval, _g, _a), live = self._get(wallet, sid)
        if not live:
            raise Revert(f"RecurringPayment_UnknownSchedule({wallet},{sid})")
        if self.now < due:
            raise Revert(f"RecurringPayment_NotDue({due})")
        if self.refuse:
            raise Revert(self.refuse)

    def pay(self, wallet, sid):
        """What the mined run() did: the next due date on the grid, or 0 when it was the last."""
        s = self.schedules[(wallet, sid)]
        due = s["next_due"]
        nxt = due + ((self.now - due) // s["interval"] + 1) * s["interval"]
        if s["end"] and nxt > s["end"]:
            del self.schedules[(wallet, sid)]
            nxt = 0
        else:
            s["next_due"] = nxt
        self.ran.append((wallet, sid, due, nxt))
        return due, nxt

    @property
    def functions(self):
        return SimpleNamespace(
            scheduleCount=lambda wallet: _Call(lambda tx: self.count.get(wallet, 0)),
            getSchedule=lambda wallet, sid: _Call(lambda tx: self._get(wallet, sid)),
            run=lambda wallet, sid: _Call(lambda tx: self._run(wallet, sid)),
            # What the routes build: they only hand it to _prepare_owner_tx, which the checks replace.
            createSchedule=lambda *args: ("createSchedule", args),
            cancelSchedule=lambda *args: ("cancelSchedule", args),
        )


class FakeW3:
    to_checksum_address = staticmethod(Web3.to_checksum_address)

    def __init__(self, module: FakeModule):
        self.module = module
        self.eth = SimpleNamespace(
            chain_id=CHAIN,
            get_block=lambda n: {"timestamp": module.now, "number": 1},
            get_balance=lambda a: 10**18,
        )


def _fresh_module() -> FakeModule:
    return FakeModule()


# ── 1. The table ────────────────────────────────────────────────────────────────


def test_table():
    print("\n[1] the schedule table keeps what the chain says, and the keeper's own fields apart")
    common = dict(token=USDC, recipient=PAYEE, amount=10**30, interval_secs=MONTH, end_at=0)
    row = db.upsert_schedule(1, CHAIN, WALLET, MODULE, 1, next_due=100, status=LIVE, **common)
    check("a new schedule is recorded live", row["status"] == LIVE and row["schedule_id"] == 1)
    check("an amount past 64 bits survives (text)", int(row["amount"]) == 10**30)

    db.record_failure(row["id"], "over the limit", retry_at=500)
    check("a refusal is due again only after retry_at", db.get_due_schedules(CHAIN, 400) == []
          and [r["id"] for r in db.get_due_schedules(CHAIN, 500)] == [row["id"]])
    again = db.upsert_schedule(1, CHAIN, WALLET, MODULE, 1, next_due=100, status=LIVE, **common)
    check("a refresh from the chain keeps the keeper's refusal", again["last_error"] == "over the limit"
          and again["failures"] == 1)

    db.record_payment(row["id"], "0xabc", ran_at=600, next_due=700)
    paid = db.get_schedule(CHAIN, WALLET, 1)
    check("a payment moves the due date and clears the refusal",
          paid["next_due"] == 700 and paid["last_error"] is None and paid["failures"] == 0 and paid["retry_at"] is None
          and paid["last_tx"] == "0xabc")
    check("and it isn't due before then", db.get_due_schedules(CHAIN, 699) == [])

    db.record_payment(row["id"], "0xdef", ran_at=700, next_due=None)
    check("the last payment finishes it", db.get_schedule(CHAIN, WALLET, 1)["status"] == FINISHED)
    db.upsert_schedule(1, CHAIN, WALLET, MODULE, 1, next_due=0, status=CANCELLED, **common)
    check("a finished schedule stays finished whatever a refresh says",
          db.get_schedule(CHAIN, WALLET, 1)["status"] == FINISHED)

    db.upsert_schedule(1, CHAIN, WALLET, MODULE, 2, next_due=50, status=LIVE, **common)
    check("live schedules are listed first", [r["schedule_id"] for r in db.get_schedules(1, CHAIN)][0] == 2)


# ── 2. Reading the module ───────────────────────────────────────────────────────


def test_sync():
    print("\n[2] sync_wallet copies the chain's schedules, and works out what became of the gone ones")
    module = _fresh_module()
    wallet = _addr(0xC0FFEE)
    a = module.create(wallet, USDC, PAYEE, 5, MONTH)
    b = module.create(wallet, USDC, PAYEE, 6, MONTH, end=module.now + MONTH)
    c = module.create(wallet, USDC, PAYEE, 7, MONTH)
    rows = {r["schedule_id"]: r for r in recurring.sync_wallet(2, CHAIN, wallet, module)}
    check("all three are on record, live", {a, b, c} <= set(rows) and all(r["status"] == LIVE for r in rows.values()))

    module.schedules[(wallet, a)]["amount"] = 0  # cancelled: the module zeroes it
    module.now += 2 * MONTH                      # b's last due date came and went ...
    module.pay(wallet, b)                        # ... and someone else ran it, deleting it
    rows = {r["schedule_id"]: r for r in recurring.sync_wallet(2, CHAIN, wallet, module)}
    check("a zeroed schedule whose end hadn't come was cancelled", rows[a]["status"] == CANCELLED, rows[a]["status"])

    module.generation[wallet] = 1  # the owner uninstalled the module: c keeps its terms but isn't live
    rows = {r["schedule_id"]: r for r in recurring.sync_wallet(2, CHAIN, wallet, module)}
    check("a schedule wiped by an uninstall is gone", rows[c]["status"] == GONE, rows[c]["status"])

    never = module.create(_addr(0xD00D), USDC, PAYEE, 1, MONTH)
    module.schedules[(_addr(0xD00D), never)]["amount"] = 0
    check("one cancelled before it was ever seen isn't recorded",
          recurring.sync_wallet(3, CHAIN, _addr(0xD00D), module) == [])


def test_wording():
    print("\n[3] schedules are worded like the History tab")
    check("weeks", recurring.interval_text(604_800) == "every week" and recurring.interval_text(1_209_600) == "every 2 weeks")
    check("days", recurring.interval_text(MONTH) == "every 30 days" and recurring.interval_text(86_400) == "every day")
    check("hours", recurring.interval_text(3_600) == "every hour" and recurring.interval_text(21_600) == "every 6 hours")
    row = dict(schedule_id=1, token=NATIVE, recipient=PAYEE, amount="1500000000000000000", interval_secs=MONTH,
               next_due=1, end_at=0, status=LIVE, last_tx=None, last_run_at=None, last_error=None, retry_at=None)
    described = recurring.describe(FakeW3(_fresh_module()), 1, CHAIN, row)
    check("a native payment reads in whole units", described["summary"].startswith("1.5 ETH to 0x0000") and
          described["summary"].endswith("every 30 days"), described["summary"])


# ── 3. The keeper ───────────────────────────────────────────────────────────────


class Keeper:
    """keeper.pay with the chain swapped out: sends 'mine' at once, Telegram messages are kept."""

    def __init__(self, module: FakeModule):
        self.module, self.w3, self.told, self.sent = module, FakeW3(module), [], []
        keeper.recurring.module_contract = lambda w3, address: module
        keeper.tx_sender.send_and_confirm = self._send
        keeper._tell_owner = lambda user_id, chain_id, text: self.told.append(text)
        recurring.events_in = self._events
        self._last = None

    def _send(self, w3, chain_name, account, tx):
        self.sent.append(tx)
        wallet, sid = self._next
        due, nxt = self.module.pay(wallet, sid)
        self._last = SimpleNamespace(args={"account": wallet, "id": sid, "dueAt": due, "nextDue": nxt},
                                     address=MODULE)
        return {"status": 1, "transactionHash": bytes(32), "blockNumber": 1}

    def _events(self, module, receipt):
        executed = [{"args": self._last.args, "address": MODULE}] if self._last else []
        return {"ScheduleCreated": [], "ScheduleCancelled": [], "RecurringPaymentExecuted": executed}

    def pay(self, row: dict) -> str:
        self._next = (row["wallet"], row["schedule_id"])
        return keeper.pay(self.w3, "sepolia-fork", CHAIN, Account.create(), row, self.module.now)


def test_keeper():
    print("\n[4] the keeper pays what is due, once, and backs off a refusal")
    module = _fresh_module()
    k = Keeper(module)
    wallet = _addr(0xFACE)
    sid = module.create(wallet, NATIVE, PAYEE, 10**17, MONTH)
    row = recurring.sync_wallet(4, CHAIN, wallet, module)[0]

    check("a due schedule is paid", k.pay(row) == "paid" and len(k.sent) == 1)
    after = db.get_schedule(CHAIN, wallet, sid)
    check("its next due date is the module's", after["next_due"] == module.now + MONTH, str(after["next_due"]))
    check("the owner is told", k.told and k.told[-1].startswith("Paid 0.1 ETH to"), str(k.told))
    history = db.get_db().execute("SELECT * FROM transactions WHERE user_id = 4").fetchall()
    check("the History tab lists it as a recurring payment, going out",
          len(history) == 1 and history[0]["source"] == "recurring" and history[0]["direction"] == "out"
          and history[0]["movements_read"] == 1, str([dict(h) for h in history]))

    check("not due yet: nothing is sent", k.pay(after) == "not_due" and len(k.sent) == 1)

    module.now += MONTH
    module.refuse = "SpendingLimitModule_BudgetExceeded(1,2)"
    row = db.get_schedule(CHAIN, wallet, sid)
    check("a refused payment is not sent", k.pay(row) == "refused" and len(k.sent) == 1)
    refused = db.get_schedule(CHAIN, wallet, sid)
    check("its reason is kept, and it waits five minutes",
          "BudgetExceeded" in (refused["last_error"] or "") and refused["retry_at"] == module.now + 300
          and refused["failures"] == 1, str(refused))
    told = len(k.told)
    k.pay(refused)
    again = db.get_schedule(CHAIN, wallet, sid)
    check("a second refusal waits twice as long, and the owner isn't told twice",
          again["retry_at"] == module.now + 600 and again["failures"] == 2 and len(k.told) == told)
    check("it stays due all the while", again["next_due"] <= module.now)

    module.refuse = None
    module.now += 3 * MONTH  # long overdue
    k.pay(db.get_schedule(CHAIN, wallet, sid))
    caught_up = db.get_schedule(CHAIN, wallet, sid)
    check("long overdue: one payment, and the next date is in the future",
          len(k.sent) == 2 and caught_up["next_due"] > module.now and caught_up["failures"] == 0)

    module.schedules[(wallet, sid)]["amount"] = 0  # the owner cancelled it from a block explorer
    check("a schedule cancelled behind the app's back is dropped, not sent",
          k.pay(db.get_schedule(CHAIN, wallet, sid)) == "gone" and len(k.sent) == 2
          and db.get_schedule(CHAIN, wallet, sid)["status"] == CANCELLED)

    last = module.create(wallet, NATIVE, PAYEE, 1, MONTH, end=module.now)
    row = recurring.sync_wallet(4, CHAIN, wallet, module)
    k.pay(db.get_schedule(CHAIN, wallet, last))
    check("a schedule's last payment finishes it", db.get_schedule(CHAIN, wallet, last)["status"] == FINISHED
          and "last one" in k.told[-1], k.told[-1])
    check("retries back off to six hours at most", keeper._retry_delay(1) == 300 and keeper._retry_delay(50) == 21_600)


# ── 4. The routes ───────────────────────────────────────────────────────────────


def _client():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop(_app):
        yield

    api.app.router.lifespan_context = _noop
    api.limiter.enabled = False
    return TestClient(api.app)


def test_routes():
    print("\n[5] the Controls routes check what they can before any chain call")
    c = _client()
    _, headers, owner = sign_in(c)
    user_id = db.get_user_by_owner_addr(owner.address)["id"]
    module = _fresh_module()
    wallet_addr = _addr(0xABCD)
    db.save_wallet_address(user_id, CHAIN, wallet_addr)
    wallet = SimpleNamespace(address=wallet_addr)
    w3 = FakeW3(module)
    calls = []

    set_everywhere(api, "_resolve_chain", lambda chain_id: (w3, "sepolia-fork"))
    routes.recurring._load_wallet_for_chain = lambda w3_, uid, chain_id: wallet
    routes.recurring._fee_per_payment = lambda wallet_: 123
    installed = {"module": module}
    routes.recurring.recurring.module_for_wallet = lambda w3_, chain_id, wallet_: installed["module"]

    def fake_prepare(w3_, owner_, fn, chain_id):
        calls.append(fn)
        return {"tx": {"to": MODULE}}

    routes.recurring._prepare_owner_tx = fake_prepare

    good = {"chain_id": CHAIN, "token": "eth", "amount": "0.25", "to": PAYEE, "interval_secs": MONTH}
    r = c.post("/api/wallet/recurring/prepare", headers=headers, json=good)
    check("a good request is built for the owner to sign", r.status_code == 200, r.text[:200])
    name, args = calls[-1]
    check("as createSchedule on the module, in base units, with no start or end",
          name == "createSchedule" and args == (wallet_addr, NATIVE, PAYEE, 25 * 10**16, MONTH, 0, 0), str(args))

    r = c.post("/api/wallet/recurring/prepare", headers=headers, json={**good, "interval_secs": 60})
    check("an interval under an hour is refused", r.status_code == 422, str(r.status_code))
    r = c.post("/api/wallet/recurring/prepare", headers=headers, json={**good, "to": "not an address"})
    check("a bad address is refused", r.status_code == 400 and "valid address" in r.text, r.text[:120])
    r = c.post("/api/wallet/recurring/prepare", headers=headers, json={**good, "to": wallet_addr})
    check("the wallet can't pay itself", r.status_code == 400 and "itself" in r.text, r.text[:120])
    r = c.post("/api/wallet/recurring/prepare", headers=headers,
               json={**good, "first_due": 2_000_000_000, "end_at": 1_900_000_000})
    check("an end before the first payment is refused", r.status_code == 400 and "end date" in r.text, r.text[:120])
    r = c.post("/api/wallet/recurring/prepare", headers=headers, json={**good, "amount": "0"})
    check("a zero amount is refused", r.status_code == 422)

    installed["module"] = None
    r = c.post("/api/wallet/recurring/prepare", headers=headers, json=good)
    check("a wallet without the module is told so", r.status_code == 400 and "no recurring payments module" in r.text,
          r.text[:160])
    r = c.get(f"/api/wallet/{CHAIN}/recurring", headers=headers)
    check("and lists as unavailable", r.status_code == 200 and r.json()["available"] is False, r.text[:160])
    installed["module"] = module

    sid = module.create(wallet_addr, NATIVE, PAYEE, 10**18, MONTH)
    r = c.get(f"/api/wallet/{CHAIN}/recurring", headers=headers)
    body = r.json()
    check("the list reads the chain and words each schedule",
          r.status_code == 200 and body["available"] and body["schedules"][0]["schedule_id"] == sid
          and body["schedules"][0]["summary"].startswith("1 ETH to") and body["fee_per_payment_wei"] == "123",
          r.text[:300])

    r = c.post("/api/wallet/recurring/cancel/prepare", headers=headers, json={"chain_id": CHAIN, "schedule_id": sid})
    check("a cancel is built as cancelSchedule on the module",
          r.status_code == 200 and calls[-1] == ("cancelSchedule", (wallet_addr, sid)), str(calls[-1]))

    other = Account.create()
    w3.eth.wait_for_transaction_receipt = lambda h, timeout: {"to": other.address, "status": 1, "blockNumber": 1}
    set_everywhere(api, "_tx_seen", lambda w3_, h: True)
    r = c.post("/api/wallet/recurring/confirm", headers=headers, json={"chain_id": CHAIN, "tx_hash": "0x" + "11" * 32})
    check("a confirm for a transaction to some other contract is refused",
          r.status_code == 400 and "not sent to your wallet's recurring payments" in r.text, r.text[:160])


if __name__ == "__main__":
    test_table()
    test_sync()
    test_wording()
    test_keeper()
    test_routes()
    finish("All recurring payment checks passed.")
