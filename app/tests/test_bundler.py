"""
Offline checks for what a UserOp is charged for posting its data to Ethereum: Base's L1 fee (an OP
Stack fee in wei, priced by the GasPriceOracle predeploy) and Arbitrum's (gas units, from
NodeInterface), each turned into preVerificationGas on the live chain only, never on a fork.

No network: the node is a fake that answers the two oracles with fixed figures. Against real Base
handleOps receipts, the pricing repaid 1.20-1.22x of the L1 fee actually charged, the cushion
L1_GAS_BUFFER is there to give.

Also how the bundler's transactions go out (tx_sender): sending returns as soon as the node has
one, so the wait can run elsewhere (landing.py), and the wait replaces a stalled one at a higher fee.

And how ops share them (bundler.Lane): alone when the lane is idle, together when they pile up --
one per wallet, within the op and gas caps, at the lowest fee among them, with one wait per
bundle -- an op that would sink the bundle refused on its own, and an op whose bundle reverted
without running it sent again.

And how a process's pool of bundler keys shares the ops out (bundler.pick_bundler): each to the
least busy key, so a stuck one is passed over, and a wallet's ops to one key, so they land in order.

Run: make bundler-test   (or: python app/tests/test_bundler.py)
"""
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace

import rlp
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_account import Account
from eth_utils import keccak
from hexbytes import HexBytes
from web3.exceptions import ContractCustomError, TransactionNotFound

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

import bundler                     # noqa: E402
import tx_sender                   # noqa: E402

ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
# Stands in for a handleOps transaction's calldata: zero and non-zero bytes, as ABI encoding has.
HANDLE_OPS = keccak(text="handleOps")[:4] + bytes(100) + b"\x01" * 200
BASE_PVG = bundler._intrinsic_gas(HANDLE_OPS) + bundler.ENTRY_POINT_OVERHEAD_GAS
GWEI = 10**9


class FakeNode:
    """Answers eth_call for GasPriceOracle.getL1Fee and NodeInterface.gasEstimateL1Component, and keeps every call."""

    def __init__(self, l1_fee_wei: int = 0, arbitrum_l1_gas: int = 0):
        self.l1_fee_wei, self.arbitrum_l1_gas = l1_fee_wei, arbitrum_l1_gas
        self.calls: list[dict] = []
        self.eth = SimpleNamespace(call=self._call)

    def _call(self, tx: dict) -> bytes:
        self.calls.append(tx)
        if tx["to"] == bundler.OP_GAS_PRICE_ORACLE:
            return abi_encode(["uint256"], [self.l1_fee_wei])
        if tx["to"] == bundler.ARB_NODE_INTERFACE:
            return abi_encode(["uint64", "uint256", "uint256"], [self.arbitrum_l1_gas, 0, 0])
        raise AssertionError(f"unexpected call to {tx['to']}")


ENTRY_POINT_CONTRACT = SimpleNamespace(
    address=ENTRY_POINT,
    encode_abi=lambda abi_element_identifier, args: "0x" + HANDLE_OPS.hex(),
)


def _pvg(chain_name: str, node: FakeNode, gas_price: int) -> int:
    return bundler._pre_verification_gas(node, chain_name, ENTRY_POINT_CONTRACT, (), "0x" + "be" * 20, gas_price)


def test_base_l1_fee():
    print("\n[1] Base: the L1 fee, in wei, becomes preVerificationGas at the price the EntryPoint repays")
    fee, price = 7 * GWEI, 6_000_000  # what a 1-2 KB handleOps costs on Base, at ~0.006 gwei
    node = FakeNode(l1_fee_wei=fee)
    pvg = _pvg("base", node, price)
    l1_gas = -(-fee // price)
    check("the fee as gas at that price, rounded up, then buffered",
          pvg == BASE_PVG + int(l1_gas * bundler.L1_GAS_BUFFER), f"{pvg - BASE_PVG} != {int(l1_gas * bundler.L1_GAS_BUFFER)}")
    repaid = (pvg - BASE_PVG) * price
    check("repaid at that price, it covers the fee with the buffer's cushion, to within one gas",
          fee * bundler.L1_GAS_BUFFER - price <= repaid <= fee * bundler.L1_GAS_BUFFER + 2 * price, f"{repaid / fee:.4f}x")

    check("one call, to the GasPriceOracle predeploy's getL1Fee",
          len(node.calls) == 1 and node.calls[0]["to"] == "0x420000000000000000000000000000000000000F"
          and node.calls[0]["data"].startswith("0x" + keccak(text="getL1Fee(bytes)")[:4].hex()), str(node.calls))
    (priced,) = abi_decode(["bytes"], bytes.fromhex(node.calls[0]["data"][10:]))
    fields = rlp.decode(priced[1:])
    check("what it prices is an unsigned EIP-1559 transaction to the EntryPoint carrying the handleOps calldata",
          priced[:1] == b"\x02" and len(fields) == 9 and fields[5] == bytes.fromhex(ENTRY_POINT[2:])
          and fields[7] == HANDLE_OPS and fields[8] == [], str(fields)[:200])

    node = FakeNode(l1_fee_wei=price + 1)
    check("a fee just over one gas's worth is two gas, never rounded down",
          _pvg("base", node, price) == BASE_PVG + int(2 * bundler.L1_GAS_BUFFER))
    node = FakeNode(l1_fee_wei=fee)
    check("a zero gas price is no division by zero", _pvg("base", node, 0) > BASE_PVG)


def test_forks_and_other_chains():
    print("\n[2] only the live L2s pay an L1 charge; forks and L1s don't")
    for name in ("base-fork", "arbitrum-fork", "mainnet", "sepolia-fork", "bsc"):
        node = FakeNode(l1_fee_wei=7 * GWEI, arbitrum_l1_gas=5_000)
        check(f"{name}: calldata and fixed costs only, and no oracle asked", _pvg(name, node, 6_000_000) == BASE_PVG
              and node.calls == [], f"{_pvg(name, FakeNode(), 6_000_000) - BASE_PVG} extra")

    node = FakeNode(arbitrum_l1_gas=5_000)
    pvg = _pvg("arbitrum", node, 10**7)
    check("Arbitrum: NodeInterface's gas units, buffered, whatever the gas price",
          pvg == BASE_PVG + int(5_000 * bundler.L1_GAS_BUFFER) and node.calls[0]["to"] == bundler.ARB_NODE_INTERFACE,
          str(pvg - BASE_PVG))


class FakeChain:
    """
    A node for tx_sender: takes raw transactions, and mines one only once `mine_after` broadcasts
    have reached it -- so a test can make the first one stall.
    """

    def __init__(self, mine_after: int = 1):
        self.mine_after, self.raw, self.receipt_asks = mine_after, [], 0
        self.eth = SimpleNamespace(
            account=Account,
            get_transaction_count=lambda _address, _block: 7,
            send_raw_transaction=self._send,
            get_transaction_receipt=self._receipt,
            get_block=lambda _block: {"baseFeePerGas": GWEI},
        )

    def _send(self, raw: bytes) -> HexBytes:
        self.raw.append(raw)
        return HexBytes(keccak(raw))

    def _receipt(self, tx_hash):
        self.receipt_asks += 1
        if len(self.raw) < self.mine_after or HexBytes(tx_hash) != HexBytes(keccak(self.raw[-1])):
            raise TransactionNotFound("not yet")
        return {"status": 1, "transactionHash": HexBytes(tx_hash)}

    def sent(self, i: int) -> dict:
        """The i-th raw transaction's nonce and fees."""
        fields = rlp.decode(self.raw[i][1:])
        return {"nonce": int.from_bytes(fields[1], "big"), "tip": int.from_bytes(fields[2], "big"),
                "cap": int.from_bytes(fields[3], "big")}


def _tx() -> dict:
    return {"to": ENTRY_POINT, "value": 0, "gas": 300_000, "maxFeePerGas": 3 * GWEI,
            "maxPriorityFeePerGas": GWEI, "chainId": 1, "data": "0x"}


def test_sending_and_waiting():
    print("\n[3] tx_sender: sending returns at once, nonces run ahead, and the wait replaces a stalled one")
    account = Account.create()
    saved = tx_sender.ATTEMPT_TIMEOUT_SECS, tx_sender.RECEIPT_POLL_INTERVAL_SECS
    tx_sender.ATTEMPT_TIMEOUT_SECS, tx_sender.RECEIPT_POLL_INTERVAL_SECS = 0.05, 0.01
    tx_sender._next_nonce.clear()
    try:
        node = FakeChain()
        first = tx_sender.send(node, "test-chain", account, _tx())
        second = tx_sender.send(node, "test-chain", account, _tx())
        check("sending puts it on the wire and returns, without waiting for a receipt",
              len(node.raw) == 2 and node.receipt_asks == 0 and len(first.hashes) == 1, str(node.receipt_asks))
        check("a second send before the first lands takes the next nonce",
              (node.sent(0)["nonce"], node.sent(1)["nonce"]) == (7, 8), str([node.sent(0), node.sent(1)]))
        receipt = tx_sender.confirm(second)
        check("the wait returns the receipt of the one it sent", receipt["transactionHash"] == second.hashes[0])

        tx_sender._next_nonce.clear()
        node = FakeChain(mine_after=2)
        stalled = tx_sender.send(node, "test-chain", account, _tx())
        receipt = tx_sender.confirm(stalled)
        replaced, original = node.sent(1), node.sent(0)
        check("a stalled one is replaced at the same nonce, every fee raised, and the replacement is what lands",
              len(stalled.hashes) == 2 and replaced["nonce"] == original["nonce"]
              and replaced["tip"] > original["tip"] and replaced["cap"] > original["cap"]
              and receipt["transactionHash"] == stalled.hashes[1], f"{original} -> {replaced}")
    finally:
        tx_sender.ATTEMPT_TIMEOUT_SECS, tx_sender.RECEIPT_POLL_INTERVAL_SECS = saved
        tx_sender._next_nonce.clear()


# ── Bundles ───────────────────────────────────────────────────────────────────

FAILED_OP = keccak(text="FailedOp(uint256,string)")[:4]
ACCOUNT = Account.create()


def _wallet(n: int) -> str:
    return "0x" + f"{n:040x}"


class FakeEntryPoint:
    """
    The EntryPoint, as far as a Lane uses it. A fake op is (wallet, id), and handleOps calldata is
    just the ids, one byte each -- so the node can tell which ops a simulation or a broadcast carried.
    """

    address = ENTRY_POINT

    def __init__(self):
        self.functions = SimpleNamespace(handleOps=self._handle_ops)

    @staticmethod
    def _data(ops) -> str:
        return "0x" + bytes(op[1] for op in ops).hex()

    def encode_abi(self, abi_element_identifier, args):
        return self._data(args[0])

    def _handle_ops(self, ops, beneficiary):
        data = self._data(ops)
        return SimpleNamespace(build_transaction=lambda params: {"to": ENTRY_POINT, "value": 0, "data": data, **params})


class FakeBundleNode:
    """
    A node for a Lane: simulates handleOps (an op whose id is in `failing` fails validation, as
    FailedOp naming its index), and takes raw transactions -- holding each until `gate` is set, so
    a test can keep the lane busy while ops pile up behind it.
    """

    def __init__(self, base_fee: int = GWEI, balance: int = 10**18, failing=(), gas_per_op: int = 100_000):
        self.base_fee, self.balance, self.failing, self.gas_per_op = base_fee, balance, set(failing), gas_per_op
        self.estimates: list[list[int]] = []
        self.raw: list[bytes] = []
        self.gate, self.holding = threading.Event(), threading.Event()
        self.gate.set()
        self.eth = SimpleNamespace(
            account=Account,
            get_transaction_count=lambda _address, _block: 0,
            send_raw_transaction=self._send,
            estimate_gas=self._estimate,
            get_block=lambda _block: {"baseFeePerGas": self.base_fee},
            get_balance=lambda _address: self.balance,
        )

    def _estimate(self, tx: dict, block_identifier=None) -> int:
        ids = list(bytes.fromhex(tx["data"][2:]))
        self.estimates.append(ids)
        for index, op_id in enumerate(ids):
            if op_id in self.failing:
                data = FAILED_OP + abi_encode(["uint256", "string"], [index, "AA25 invalid account nonce"])
                raise ContractCustomError("execution reverted", data="0x" + data.hex())
        return self.gas_per_op * len(ids)

    def _send(self, raw: bytes) -> HexBytes:
        self.holding.set()
        self.gate.wait()
        self.raw.append(raw)
        return HexBytes(keccak(raw))

    def sent(self) -> list[dict]:
        """Every transaction sent: the op ids it carried, its fees and its gas."""
        out = []
        for raw in self.raw:
            fields = rlp.decode(raw[1:])
            out.append({"ops": list(fields[7]), "tip": int.from_bytes(fields[2], "big"),
                        "cap": int.from_bytes(fields[3], "big"), "gas": int.from_bytes(fields[4], "big")})
        return out


def _prepared(wallet: int, op_id: int, outer_gas: int = 300_000, cap: int = 3 * GWEI, tip: int = GWEI):
    return bundler.PreparedUserOp(
        op=(_wallet(wallet), op_id), user_op_hash=bytes([op_id]) * 32, outer_gas=outer_gas,
        fees={"maxFeePerGas": cap, "maxPriorityFeePerGas": tip}, from_block=0,
    )


def _until(condition, what: str, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while not condition():
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


class Bundles:
    """
    One test's lane on a fake node, with tx_sender.confirm counted rather than run: the wait is
    tx_sender's business (test [3]); what matters here is how many there are.
    """

    def __init__(self, chain_name: str = "test-chain", **node_args):
        self.node = FakeBundleNode(**node_args)
        self.lane = bundler.Lane(self.node, chain_name, 1, FakeEntryPoint(), ACCOUNT)
        self.pool = ThreadPoolExecutor(max_workers=64)
        self.confirms: list = []

    def __enter__(self):
        tx_sender._next_nonce.clear()
        self.saved_confirm = bundler.confirm

        def counted(sent):
            self.confirms.append(sent)
            return {"status": 1, "transactionHash": sent.hashes[0]}

        bundler.confirm = counted
        return self

    def __exit__(self, *_exc):
        self.node.gate.set()
        self.pool.shutdown(wait=True)
        bundler.confirm = self.saved_confirm
        tx_sender._next_nonce.clear()

    def submit(self, prepared) -> Future:
        return self.pool.submit(self.lane.submit, prepared)

    def piled_up(self, held, waiting: list) -> list[Future]:
        """
        Sends `held` and holds the lane in its broadcast while `waiting` queue up behind it, then
        lets it go. Returns the futures of `held`, then of each of `waiting`.
        """
        self.node.gate.clear()
        self.node.holding.clear()
        first = self.submit(held)
        _until(self.node.holding.is_set, "the lane to start sending")
        rest = [self.submit(p) for p in waiting]
        _until(lambda: len(self.lane._queue) == len(waiting), "the ops to queue")
        self.node.gate.set()
        return [first] + rest


def test_bundle_shape():
    print("\n[4] bundles: alone when the lane is idle, together when ops pile up behind it")
    with Bundles() as b:
        bundle = b.submit(_prepared(1, 1, outer_gas=250_000, cap=4 * GWEI, tip=2 * GWEI)).result(timeout=3)
        sent = b.node.sent()
        check("an op that finds the lane idle goes out at once, alone, unsimulated (prepare_user_op just did that)",
              [s["ops"] for s in sent] == [[1]] and b.node.estimates == [], str(sent))
        check("...at its own fees and its own outer gas, as before bundling",
              (sent[0]["cap"], sent[0]["tip"], sent[0]["gas"]) == (4 * GWEI, 2 * GWEI, 250_000), str(sent[0]))
        check("...with one wait, whose receipt its op reads", bundle.landed.result(timeout=3)["status"] == 1
              and len(b.confirms) == 1)

    with Bundles() as b:
        futures = b.piled_up(_prepared(1, 1), [
            _prepared(2, 2, cap=5 * GWEI, tip=GWEI),
            _prepared(3, 3, cap=3 * GWEI, tip=2 * GWEI),
            _prepared(2, 4),                               # wallet 2 again
            _prepared(4, 5, cap=4 * GWEI, tip=GWEI + GWEI // 2),
        ])
        bundles = [f.result(timeout=3) for f in futures]
        sent = b.node.sent()
        check("ops that piled up while the lane was busy go out together, one per wallet, in order",
              [s["ops"] for s in sent] == [[1], [2, 3, 5], [4]], str([s["ops"] for s in sent]))
        check("...simulated together first", b.node.estimates[0] == [2, 3, 5], str(b.node.estimates))
        check("...priced at the lowest cap and the lowest tip among them",
              (sent[1]["cap"], sent[1]["tip"]) == (3 * GWEI, GWEI), str(sent[1]))
        check("...with the simulated gas plus the usual headroom", sent[1]["gas"] == int(3 * 100_000 * bundler.OUTER_GAS_BUFFER),
              str(sent[1]["gas"]))
        check("the ops of one bundle share one transaction and one wait",
              bundles[1] is bundles[2] is bundles[4] and len(b.confirms) == 3, f"{len(b.confirms)} waits")
        check("the wallet's second op waits for the next bundle", bundles[3] is not bundles[1] and sent[2]["ops"] == [4])


def test_bundle_caps():
    print("\n[5] bundles: at most BUNDLE_MAX_OPS ops, and under the chain's per-transaction gas limit")
    check("Ethereum, Sepolia, BSC and Base: 2^24 gas a transaction (EIP-7825)",
          all(bundler.bundle_gas_cap(name) == 2**24 for name in ("mainnet", "sepolia", "bsc", "base", "anvil")))
    check("Arbitrum allows 32M, so the bundle cap holds there: 20M",
          bundler.bundle_gas_cap("arbitrum") == 20_000_000 == bundler.BUNDLE_MAX_GAS)
    check("a fork is held to its live chain's limit",
          bundler.bundle_gas_cap("base-fork") == 2**24 and bundler.bundle_gas_cap("arbitrum-fork") == 20_000_000)
    check("40 ops a bundle", bundler.BUNDLE_MAX_OPS == 40)

    with Bundles() as b:
        b.piled_up(_prepared(100, 100), [_prepared(n, n) for n in range(1, 46)])
        _until(lambda: len(b.node.raw) == 3, "three bundles")
        sizes = [len(s["ops"]) for s in b.node.sent()]
        check("45 ops piled up go out as 40, then 5", sizes == [1, 40, 5], str(sizes))

    with Bundles() as b:
        b.piled_up(_prepared(100, 100), [_prepared(n, n, outer_gas=6_000_000) for n in range(1, 6)])
        _until(lambda: len(b.node.raw) == 4, "four bundles")
        sizes = [len(s["ops"]) for s in b.node.sent()]
        check("ops of 6M gas each: two a bundle under 2^24 (a third would be 18M)", sizes == [1, 2, 2, 1], str(sizes))


def test_bundle_refusals():
    print("\n[6] bundles: an op that would sink the bundle is refused on its own; the rest still go")
    with Bundles(failing={3}) as b:
        futures = b.piled_up(_prepared(1, 1), [_prepared(2, 2), _prepared(3, 3), _prepared(4, 4)])
        try:
            futures[2].result(timeout=3)
            refused = None
        except bundler.NotSent as e:
            refused = str(e)
        check("the op the EntryPoint rejects is refused, by reason, with nothing sent",
              refused is not None and "AA25" in refused and "Nothing was sent" in refused, str(refused))
        futures[1].result(timeout=3)  # refused before the rest of its bundle was even broadcast
        check("...and the others go out together without it",
              [s["ops"] for s in b.node.sent()] == [[1], [2, 4]] and b.node.estimates == [[2, 3, 4], [2, 4]],
              f"{[s['ops'] for s in b.node.sent()]} after {b.node.estimates}")
        check("...in a bundle both of their futures name", futures[1].result() is futures[3].result())

    with Bundles(base_fee=4 * GWEI) as b:
        futures = b.piled_up(_prepared(1, 1, cap=9 * GWEI), [
            _prepared(2, 2, cap=5 * GWEI), _prepared(3, 3, cap=3 * GWEI), _prepared(4, 4, cap=6 * GWEI),
        ])
        [f.result(timeout=3) for f in futures]
        sent = b.node.sent()
        check("an op whose cap the base fee has passed doesn't hold the bundle's price down: it goes alone, after",
              [s["ops"] for s in sent] == [[1], [2, 4], [3]] and sent[1]["cap"] == 5 * GWEI and sent[2]["cap"] == 3 * GWEI,
              str(sent))

    with Bundles(balance=10**12) as b:
        futures = b.piled_up(_prepared(1, 1), [_prepared(2, 2), _prepared(3, 3)])
        errors = []
        for f in futures[1:]:
            try:
                f.result(timeout=3)
            except bundler.NotSent as e:
                errors.append(str(e))
        check("a bundle the bundler can't afford, on top of the one in flight, is refused to every op in it",
              len(errors) == 2 and all("top it up" in e and "Nothing was sent" in e for e in errors)
              and len(b.node.raw) == 1, str(errors))


def test_resending():
    print("\n[7] an op whose bundle reverted without running it goes out again; one a rival ran is reported as run")
    reverted = {"status": 0, "transactionHash": HexBytes(b"\x01" * 32), "blockNumber": 9}
    mined = {"status": 1, "transactionHash": HexBytes(b"\x02" * 32), "blockNumber": 10}
    rival = {"status": 1, "transactionHash": HexBytes(b"\x03" * 32), "blockNumber": 9}

    def bundle_with(receipt=None, error=None):
        landed = Future()
        landed.set_exception(error) if error else landed.set_result(receipt)
        return bundler.Bundle(tx={}, sent=None, landed=landed)

    def run(first, later, landed_elsewhere=None, check_alone=None):
        submits, estimates = [], []

        def submit(prepared):
            submits.append(prepared)
            return later.pop(0)

        def estimate(_w3, tx, what):
            estimates.append(tx["data"])
            if check_alone:
                raise check_alone
            return 100_000

        saved = bundler.find_user_op_receipt, bundler._estimate, bundler._replay_revert
        bundler.find_user_op_receipt = lambda *_args: landed_elsewhere
        bundler._estimate, bundler._replay_revert = estimate, lambda *_args: "AA25 invalid account nonce"
        try:
            submitted = bundler.SubmittedUserOp(
                _prepared(1, 1), None, FakeEntryPoint(), SimpleNamespace(bundler=ACCOUNT, submit=submit), first
            )
            try:
                return bundler._receipt_that_ran(submitted), submits, estimates
            except Exception as e:  # noqa: BLE001 -- the test reads it
                return e, submits, estimates
        finally:
            bundler.find_user_op_receipt, bundler._estimate, bundler._replay_revert = saved

    got, submits, estimates = run(bundle_with(reverted), [bundle_with(mined)])
    check("checked on its own, then sent again, and the bundle that ran it is what's returned",
          got is mined and len(submits) == 1 and len(estimates) == 1, f"{got} {len(submits)} {len(estimates)}")

    got, submits, _ = run(bundle_with(reverted), [], landed_elsewhere=rival)
    check("one a rival landed is reported as the rival's, and not sent again", got is rival and submits == [], str(got))

    got, submits, _ = run(bundle_with(reverted), [bundle_with(reverted) for _ in range(bundler.MAX_RESENDS)])
    check(f"after {bundler.MAX_RESENDS} more reverted bundles it fails, saying why",
          isinstance(got, RuntimeError) and "reverted" in str(got) and len(submits) == bundler.MAX_RESENDS, str(got))

    got, submits, _ = run(bundle_with(reverted), [], check_alone=RuntimeError("Submitting this transaction would fail"))
    check("one that fails on its own is the one that sank the bundle: its user hears why, and it isn't sent again",
          isinstance(got, RuntimeError) and "would fail" in str(got) and submits == [], str(got))

    got, submits, _ = run(bundle_with(error=TimeoutError("not mined")), [])
    check("a stalled bundle is not resent behind itself: the timeout stands", isinstance(got, TimeoutError) and submits == [],
          str(got))
    got, _, _ = run(bundle_with(error=TimeoutError("not mined")), [], landed_elsewhere=rival)
    check("...unless a rival ran the op meanwhile", got is rival, str(got))


POOL = [Account.from_key(keccak(text=f"pool key {n}")) for n in range(1, 4)]


class Pool:
    """
    A process with three bundler keys, as pick_bundler sees one with API_BUNDLER_1..3: a Lane per
    key on one fake node. A bundle's wait returns only while `released` is set, so a test can hold
    bundles in flight, as a stuck transaction is.
    """

    def __init__(self):
        self.node = FakeBundleNode()
        self.entry_point = FakeEntryPoint()
        self.released = threading.Event()
        self.released.set()

    def __enter__(self):
        tx_sender._next_nonce.clear()
        self.saved = {name: getattr(bundler, name)
                      for name in ("bundler_keys", "load_network_config", "load_entry_point", "confirm")}

        def held_confirm(sent):
            self.released.wait()
            return {"status": 1, "transactionHash": sent.hashes[0]}

        bundler.bundler_keys = lambda: POOL
        bundler.load_network_config = lambda _user_id: (self.node, 1, "pool-chain")
        bundler.load_entry_point = lambda user_id: self.entry_point
        bundler.confirm = held_confirm
        return self

    def __exit__(self, *_exc):
        self.released.set()
        for name, value in self.saved.items():
            setattr(bundler, name, value)
        for key in [key for key in bundler._lanes if key[0] == "pool-chain"]:
            del bundler._lanes[key]
        tx_sender._next_nonce.clear()

    def loads(self) -> list[int]:
        return [lane.load() for lane in bundler._lanes_for(0, POOL)]


def test_bundler_pool():
    print("\n[8] the bundler key pool: each op to the least busy key, a wallet's ops to one key")
    keys = {f"POOL_TEST_{n}": keccak(text=f"env key {n}").hex() for n in (1, 2, 4)}
    saved_prefix = bundler._bundler_key_env
    os.environ.update(keys)
    try:
        bundler._bundler_key_env = "POOL_TEST"
        accounts = [a.address for a in bundler.bundler_keys()]
        check("a process's keys are <prefix>_1, _2, ... up to the first number not set",
              accounts == [Account.from_key(keys[f"POOL_TEST_{n}"]).address for n in (1, 2)], str(accounts))
        check("...and quotes are simulated from the first", bundler.resolve_bundler().address == accounts[0])
        bundler._bundler_key_env = "POOL_TEST_NONE"
        try:
            bundler.bundler_keys()
            check("a process with no key at all is refused", False, "it got keys")
        except RuntimeError as e:
            check("a process with no key at all is refused, naming the key it looked for",
                  "POOL_TEST_NONE_1 is not set" in str(e), str(e))
    finally:
        bundler._bundler_key_env = saved_prefix
        for prefix in ("POOL_TEST", "POOL_TEST_NONE"):
            bundler._bundler_keys.pop(prefix, None)
        for name in keys:
            del os.environ[name]

    addresses = [a.address for a in POOL]
    with Pool() as p:
        with ExitStack() as held:
            picked = [held.enter_context(bundler.pick_bundler(0, _wallet(w))).address for w in (1, 2, 3)]
            check("ops picked together go to different keys, the first key first", picked == addresses, str(picked))
            again = held.enter_context(bundler.pick_bundler(0, _wallet(2))).address
            check("a wallet's next op goes with the key its last one is held on, though others are as busy",
                  again == addresses[1])
            fourth = held.enter_context(bundler.pick_bundler(0, _wallet(4))).address
            check("a new wallet goes to the least busy key", fourth == addresses[0], fourth)
        check("each key is let go of once the op is done with it", p.loads() == [0, 0, 0], str(p.loads()))

        barrier = threading.Barrier(120)

        def pick(wallet: int) -> str:
            with bundler.pick_bundler(0, _wallet(wallet)) as account:
                barrier.wait(timeout=10)  # every pick held at once, as Confirms tapped together are
                return account.address

        with ThreadPoolExecutor(max_workers=120) as pool:
            chosen = list(pool.map(pick, range(1, 121)))
        counts = [chosen.count(address) for address in addresses]
        check("120 ops picked at once spread 40 / 40 / 40 over the three keys", counts == [40, 40, 40], str(counts))

        p.released.clear()  # from here, a bundle on the wire stays in flight, as a stuck one does
        with bundler.pick_bundler(0, _wallet(1)) as account:
            bundler.submit_user_op(0, _prepared(1, 1), account)
        check("a bundle in flight keeps its key busy after its op is on the wire", p.loads() == [1, 0, 0], str(p.loads()))
        with bundler.pick_bundler(0, _wallet(2)) as account:
            check("so the next wallet's op goes through another key, not behind it", account.address == addresses[1])
            bundler.submit_user_op(0, _prepared(2, 2), account)
        with bundler.pick_bundler(0, _wallet(1)) as account:
            check("...while the first wallet's next op stays with the key its last one is stuck on",
                  account.address == addresses[0])
        senders = [Account.recover_transaction(raw) for raw in p.node.raw]
        check("each key sent its own handleOps", senders == addresses[:2], str(senders))
        p.released.set()
        _until(lambda: p.loads() == [0, 0, 0], "the bundles to land and let go of their keys")
        check("a bundle that lands lets go of its key", True)


if __name__ == "__main__":
    test_base_l1_fee()
    test_forks_and_other_chains()
    test_sending_and_waiting()
    test_bundle_shape()
    test_bundle_caps()
    test_bundle_refusals()
    test_resending()
    test_bundler_pool()
    finish("All bundler checks passed.")
