"""
Offline checks for the work that keeps the assistant quick: asking the node its chain id once,
running independent chain reads together, the wallet checks every transaction tool now runs itself
(so the agent needs no preflight_check first) -- the balance and the fee headroom among them -- and
working out a UserOp's hash locally.

Nothing here touches a chain: providers, contracts and the EntryPoint are fakes, and the hash is
checked against a vector taken from the real v0.7 EntryPoint. The on-chain side -- a paused wallet
refused, the limit enforced, the local hash matching the EntryPoint's -- is in test_e2e_fork.

Run: make speed-test   (or: python app/tests/test_speed.py)
"""
import contextvars
import threading
import time
from types import SimpleNamespace

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

from langchain_core.tools import ToolException   # noqa: E402
from web3.providers.rpc import HTTPProvider       # noqa: E402

import network_config                             # noqa: E402
import parallel                                   # noqa: E402
import tools                                      # noqa: E402
import userop                                     # noqa: E402

WEI = 10**18
USDC = "0x" + "0c" * 20
WETH = "0x" + "0e" * 20
PEPE = "0x" + "0f" * 20


def test_chain_id_is_asked_once():
    print("\n[1] the node is asked its chain id once, not before every call")
    asked = []
    original = HTTPProvider.make_request

    def node(self, method, params):
        asked.append(method)
        if method == "eth_chainId":
            return {"jsonrpc": "2.0", "id": len(asked), "result": "0xaa36a7"}
        return {"jsonrpc": "2.0", "id": len(asked), "result": "0x1"}

    HTTPProvider.make_request = node
    try:
        provider = network_config._ChainIdOnceProvider("http://127.0.0.1:1")
        answers = [provider.make_request("eth_chainId", []) for _ in range(5)]
        check("five questions, one request", asked.count("eth_chainId") == 1, str(asked))
        check("...and the node's own answer every time", all(a["result"] == "0xaa36a7" for a in answers))
        provider.make_request("eth_call", [{}, "latest"])
        provider.make_request("eth_call", [{}, "latest"])
        check("other calls still go to the node", asked.count("eth_call") == 2, str(asked))

        asked.clear()
        HTTPProvider.make_request = lambda self, method, params: (
            asked.append(method) or {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "down"}}
        )
        flaky = network_config._ChainIdOnceProvider("http://127.0.0.1:1")
        flaky.make_request("eth_chainId", [])
        flaky.make_request("eth_chainId", [])
        check("an error is not kept as the answer", asked.count("eth_chainId") == 2, str(asked))
    finally:
        HTTPProvider.make_request = original


def test_reads_run_together():
    print("\n[2] independent reads run together, in the caller's context")
    turn_network = contextvars.ContextVar("turn_network", default="saved network")
    turn_network.set("bsc-fork")

    def slow(value):
        def read():
            time.sleep(0.2)
            return value, turn_network.get(), threading.current_thread().name
        return read

    started = time.monotonic()
    results = parallel.read_all({name: slow(name) for name in ("a", "b", "c", "d", "e")})
    took = time.monotonic() - started
    check("five 0.2s reads take about 0.2s, not 1s", took < 0.5, f"{took:.2f}s")
    check("results come back under their own names", [r[0] for r in results.values()] == list("abcde"))
    check("each read sees the turn's network, not the saved one",
          all(r[1] == "bsc-fork" for r in results.values()), str(results))
    check("...on worker threads", all(r[2].startswith("chain-read") for r in results.values()))

    def boom():
        raise ValueError("execution reverted: PriceOracle_StalePrice()")

    try:
        parallel.read_all({"fine": slow("ok"), "broken": boom})
        check("a failing read is raised", False, "nothing raised")
    except ValueError as e:
        check("a failing read is raised unchanged, so the agent can name it", "StalePrice" in str(e), str(e))

    task = parallel.start_task(lambda: parallel.read_all({"x": slow(1), "y": slow(2)}))
    check("a task can wait on reads", {k: v[0] for k, v in task.result(timeout=5).items()} == {"x": 1, "y": 2})


class FakeWallet:
    """
    The SessionHandler reads _wallet_checks makes: $1 USDC (6 dec), $2000 WETH/ETH (18 dec). Its
    contract functions are its own methods, as on a web3 Contract's `.functions`.
    """

    def __init__(self, watched=(), paused=False, active=True, valid_for=10**6, remaining=100, broken=(),
                 native=10 * WEI):
        self.watched, self.broken = set(watched), set(broken)
        self.state = {"paused": paused, "active": active, "valid_until": int(time.time()) + valid_for,
                      "remaining": remaining * WEI}
        self.functions = self
        self.address = "0x" + "a1" * 20
        self.w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=lambda _address: native))

    def _call(self, value):
        return SimpleNamespace(call=lambda: value)

    def paused(self):
        return self._call(self.state["paused"])

    def isSessionActive(self, _key):
        return self._call(self.state["active"])

    def currentSessionValidUntil(self):
        return self._call(self.state["valid_until"])

    def getRemainingBudget(self):
        return self._call(self.state["remaining"])

    def isWatched(self, address):
        return self._call(address in self.watched)

    def getUsdValue(self, address, amount):
        def read():
            if address in self.broken:
                raise ValueError("execution reverted: PriceOracle_StalePrice()")
            return amount * 10**12 if address == USDC else amount * 2000
        return SimpleNamespace(call=read)


def erc20(address, decimals, balance):
    return SimpleNamespace(address=address, functions=SimpleNamespace(
        decimals=lambda: SimpleNamespace(call=lambda: decimals),
        balanceOf=lambda _owner: SimpleNamespace(call=lambda: balance)))


def leg(token, amount, direction, address=None, decimals=18, unlisted=False, balance=10**30):
    """One leg; an ERC20 one holds `balance` base units (plenty, unless a check says otherwise)."""
    contract = None if address is None else erc20(address, decimals, balance)
    return tools._Leg(token, amount, direction, address or tools.ETH_SENTINEL, contract, unlisted, token.upper())


def test_wallet_checks():
    print("\n[3] the wallet checks every transaction tool now runs itself")
    S, R = tools.SENT, tools.RECEIVED
    usdc = lambda amount, d: leg("usdc", amount, d, USDC, 6)   # noqa: E731
    weth = lambda amount, d: leg("weth", amount, d, WETH, 18)  # noqa: E731

    out = tools._wallet_checks(FakeWallet(), "0xkey", [leg("eth", 0.01, S)])
    check("a native send counts in full", out["charged_usd"] == 20 and out["usd_value"] == 20, str(out))
    check("...and fits $100", out["within_budget"] and not out["is_paused"] and out["session_active"], str(out))

    out = tools._wallet_checks(FakeWallet(watched={USDC, WETH}), "0xkey", [usdc(10, S), weth(0.01, S)])
    check("adding liquidity counts BOTH deposits (the LP token has no price)",
          out["charged_usd"] == 30 and out["usd_value"] == 30, str(out))

    out = tools._wallet_checks(FakeWallet(watched={WETH}), "0xkey", [leg("eth", 0.01, S), weth(0.01, R)])
    check("a wrap into watched WETH counts nothing", out["charged_usd"] == 0 and out["usd_value"] == 20, str(out))

    out = tools._wallet_checks(FakeWallet(watched=set()), "0xkey", [leg("eth", 0.01, S), usdc(19, R)])
    check("a swap into an unwatched token counts the ETH paid", out["charged_usd"] == 20, str(out))

    out = tools._wallet_checks(FakeWallet(), "0xkey", [leg("pepe", 1000, S, PEPE, 18, unlisted=True)])
    check("a token the user added: no price, nothing counted", out["usd_value"] is None and out["charged_usd"] == 0,
          str(out))

    out = tools._wallet_checks(FakeWallet(broken={USDC}), "0xkey", [usdc(10, S)])
    check("an unwatched token's price failing doesn't fail the checks (it is only shown)",
          out["usd_value"] is None and out["charged_usd"] == 0 and "usd_value_unavailable" in out, str(out))
    try:
        tools._wallet_checks(FakeWallet(watched={USDC}, broken={USDC}), "0xkey", [usdc(10, S)])
        check("a counted token's price failing fails the checks", False, "nothing raised")
    except ValueError as e:
        check("a counted token's price failing fails the checks, by name", "StalePrice" in str(e), str(e))

    out = tools._wallet_checks(FakeWallet(remaining=10), "0xkey", [leg("eth", 0.01, S)])
    check("$20 doesn't fit $10", out["within_budget"] is False, str(out))
    out = tools._wallet_checks(FakeWallet(valid_for=30), "0xkey", [])
    check("a key with 30s left is reported as about to run out",
          out["expiring_imminently"] and not out["session_active"], str(out))

    out = tools._wallet_checks(FakeWallet(native=WEI // 100), "0xkey", [leg("eth", 0.02, S)])
    check("sending more of the native asset than the wallet holds is caught, with both figures",
          out["enough_balance"] is False
          and out["balance_short"] == "Not enough ETH: the wallet holds 0.01, and this needs 0.02.", str(out))
    out = tools._wallet_checks(FakeWallet(), "0xkey", [leg("usdc", 10, S, USDC, 6, balance=5 * 10**6)])
    check("...and of a token, counted in its own decimals",
          out["enough_balance"] is False and "holds 5, and this needs 10" in out["balance_short"], str(out))
    out = tools._wallet_checks(FakeWallet(), "0xkey", [leg("eth", 0.01, S), usdc(19, R)])
    check("what only comes back isn't balance-checked", out["enough_balance"] is True, str(out))
    out = tools._wallet_checks(FakeWallet(native=WEI // 100), "0xkey", [leg("eth", 0.01, S)])
    check("exactly the balance is enough (the fees are the quote's to check)", out["enough_balance"] is True,
          str(out))


def test_quote_refusals():
    print("\n[4] a quote the wallet would reject is refused, with the reason")
    base = {"is_paused": False, "session_active": True, "expiring_imminently": False, "within_budget": True,
            "session_expires_in_secs": 3600, "usd_value": 20.0, "charged_usd": 20.0, "remaining_usd": 80.0}

    def refusal(**changes) -> str:
        try:
            tools._enforce_wallet_checks({**base, **changes}, priced=True)
            return ""
        except ToolException as e:
            return str(e)

    check("paused: says so, and where to unpause", "paused" in refusal(is_paused=True)
          and "Controls" in refusal(is_paused=True), refusal(is_paused=True))
    message = refusal(session_active=False)
    check("a dead key: says so, and how to renew", "no longer active" in message and "Renew" in message, message)
    message = refusal(session_active=False, expiring_imminently=True)
    check("a key about to run out: says that instead", "under a minute" in message, message)
    message = refusal(within_budget=False, charged_usd=150.0, remaining_usd=80.0)
    check("over the limit: what it would count and what is left",
          "$150.00" in message and "$80.00" in message and "Nothing was sent" in message, message)
    check("every refusal says nothing was sent", all("Nothing was sent" in refusal(**c) for c in (
        {"is_paused": True}, {"session_active": False}, {"within_budget": False})))
    short = "Not enough ETH: the wallet holds 0.01, and this needs 0.02."
    message = refusal(enough_balance=False, balance_short=short, within_budget=False)
    check("too little to send: says what the wallet holds, ahead of the limit",
          message == f"{short} Nothing was sent.", message)

    shown = tools._enforce_wallet_checks(base, priced=True)
    check("a passing quote carries the figures to show",
          shown == {"session_expires_in_secs": 3600, "usd_value": 20.0, "charged_usd": 20.0, "remaining_usd": 80.0},
          str(shown))
    shown = tools._enforce_wallet_checks({**base, "within_budget": False}, priced=False)
    check("a write that moves nothing countable checks only the pause and the key",
          shown == {"session_expires_in_secs": 3600}, str(shown))
    shown = tools._enforce_wallet_checks({**base, "remaining_usd": -5.0}, priced=True)
    check("a limit lowered below what was spent shows nothing left, not a negative figure",
          shown["remaining_usd"] == 0, str(shown))


def test_native_headroom():
    print("\n[5] the native asset has to cover what is sent AND the fees")
    milli = WEI // 1000   # 0.001 ETH

    def refusal(value, fee, balance, deposit=0, max_gas=None) -> str:
        quote = None if max_gas is None else SimpleNamespace(max_gas_wei=max_gas)
        try:
            tools._check_native_headroom(11155111, value, fee, balance, deposit, quote)
            return ""
        except ToolException as e:
            return str(e)

    check("an amount the fees fit beside goes through", refusal(500 * milli, milli, WEI, max_gas=2 * milli) == "")
    check("gas the EntryPoint deposit already covers doesn't count",
          refusal(999 * milli, milli, WEI, deposit=2 * milli, max_gas=2 * milli) == "")
    message = refusal(1000 * milli, milli, WEI)
    check("the whole balance, when the quote itself failed: too little left for the fees",
          "holds 1 ETH" in message and "leaves too little for the fees" in message
          and "Nothing was sent" in message, message)
    message = refusal(999 * milli, milli // 10, WEI, deposit=milli // 2, max_gas=2 * milli)
    check("a quote that passed: what the fees can take, and the most it can use instead",
          "up to 0.0016" in message and "most it can use now is about 0.9984 ETH" in message, message)
    message = refusal(2 * WEI, milli, WEI)
    check("more than the wallet holds: says so plainly", "not the 2 this needs" in message, message)


def test_user_op_hash():
    print("\n[6] a UserOp's hash, worked out locally")
    entry_point = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
    op = ("0x1111111111111111111111111111111111111111", (5 << 64) | 7, b"", bytes.fromhex("1234abcd"),
          ((100_000 << 128) | 200_000).to_bytes(32, "big"), 50_000,
          ((10**9 << 128) | 3 * 10**9).to_bytes(32, "big"), b"", b"")
    # From EntryPoint v0.7's own getUserOpHash, on a Sepolia fork.
    reference = "c5bc65be264591e10aa2cdfbaf19c92ec53ce7ef002223bdd1f456585dd2c466"
    check("matches EntryPoint v0.7", userop._v07_user_op_hash(op, entry_point, 11155111).hex() == reference)
    check("the signature is not part of it",
          userop._v07_user_op_hash(op[:-1] + (b"\x01" * 65,), entry_point, 11155111).hex() == reference)
    check("the chain is", userop._v07_user_op_hash(op, entry_point, 1).hex() != reference)

    asked = []

    def ep(answer):
        return SimpleNamespace(address=entry_point, functions=SimpleNamespace(
            getUserOpHash=lambda _op: SimpleNamespace(call=lambda: asked.append(1) or answer)))

    userop._local_hash_matches.clear()
    agreeing = ep(bytes.fromhex(reference))
    first = userop.hash_user_op(op, agreeing, 11155111)
    second = userop.hash_user_op(op, agreeing, 11155111)
    check("the first op on a chain is checked against the EntryPoint, the rest are not",
          first.hex() == second.hex() == reference and len(asked) == 1, f"asked {len(asked)} times")

    asked.clear()
    userop._local_hash_matches.clear()
    other = ep(b"\x22" * 32)
    answers = [userop.hash_user_op(op, other, 11155111) for _ in range(3)]
    check("an EntryPoint that hashes differently is asked every time, and believed",
          all(a == b"\x22" * 32 for a in answers) and len(asked) == 3, f"asked {len(asked)} times")
    userop._local_hash_matches.clear()


if __name__ == "__main__":
    test_chain_id_is_asked_once()
    test_reads_run_together()
    test_wallet_checks()
    test_quote_refusals()
    test_native_headroom()
    test_user_op_hash()
    finish("All speed checks passed.")
