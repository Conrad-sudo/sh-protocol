"""
Offline checks for explorers.py, the History tab's view of activity outside Mitfah: how Etherscan's,
NodeReal's and Alchemy's answers are read, paged and windowed, how a rate limit is retried, and that
an API key never reaches an error or a log.

No network: `requests` is replaced by scripted answers shaped like the services' real ones. The
real services are checked, read-only, by check_explorers_live.py.

Run: make explorers-test   (or: python app/tests/test_explorers.py)
"""
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import requests

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

import explorers                   # noqa: E402
from web3 import Web3              # noqa: E402

ETHERSCAN_KEY = "ETHERSCAN-SECRET-KEY"
NODEREAL_KEY = "NODEREAL-SECRET-KEY"
ALCHEMY_KEY = "ALCHEMY-SECRET-KEY"
SEPOLIA, BSC, BASE = 11155111, 56, 8453

WALLET = Web3.to_checksum_address(f"0x{0xA11CE:040x}")
OWNER = Web3.to_checksum_address(f"0x{0x0E1:040x}")
SAM = Web3.to_checksum_address(f"0x{0x5A3:040x}")
USDC = Web3.to_checksum_address(f"0x{0x05DC:040x}")
FACTORY = Web3.to_checksum_address(f"0x{0xFAC:040x}")

# No waiting between calls in a test.
explorers._etherscan_pace = explorers._Pace(1e9)
explorers._nodereal_pace = explorers._Pace(1e9)
explorers._alchemy_pace = explorers._Pace(1e9)
explorers.time = SimpleNamespace(sleep=lambda _s: None, monotonic=explorers.time.monotonic)


def _hash(n: int) -> str:
    return f"0x{n:064x}"


class Reply:
    def __init__(self, body, status_code: int = 200):
        self.body, self.status_code = body, status_code

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeHttp:
    """Answers requests.get / requests.post from a script, and keeps every call it was sent."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return self.answer(self.calls[-1])

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return self.answer(self.calls[-1])


def _with_http(fake: FakeHttp):
    explorers.requests = SimpleNamespace(get=fake.get, post=fake.post, RequestException=requests.RequestException)


def _restore_http():
    explorers.requests = requests


def _keys(etherscan: str | None = ETHERSCAN_KEY, nodereal: str | None = NODEREAL_KEY, alchemy: str | None = ALCHEMY_KEY):
    for name, value in (("ETHERSCAN_API_KEY", etherscan), ("NODEREAL_API_KEY", nodereal), ("ALCHEMY_API_KEY", alchemy)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _all(chain_id: int, from_block: int = 0) -> list[tuple[list[explorers.Movement], int]]:
    return list(explorers.movements_since(chain_id, WALLET, from_block))


def _raises(fn) -> explorers.ExplorerUnavailable | None:
    try:
        fn()
    except explorers.ExplorerUnavailable as e:
        return e
    return None


# ── Etherscan ─────────────────────────────────────────────────────────────────


def _txlist(n: int, block: int, sender: str, value: int, input_hex: str = "0x", is_error: str = "0") -> dict:
    return {"blockNumber": str(block), "timeStamp": str(1_700_000_000 + block), "hash": _hash(n).upper().replace("0X", "0x"),
            "from": sender.lower(), "to": WALLET.lower(), "value": str(value), "input": input_hex, "isError": is_error,
            "txreceipt_status": "1" if is_error == "0" else "0", "contractAddress": ""}


def test_etherscan_reads_all_three_lists():
    print("\n[1] Etherscan: plain, internal and token records, read into movements")
    _keys()
    pages = {
        "txlist": [_txlist(1, 10, OWNER, 0, "0x8456cb59"), _txlist(2, 12, SAM, 10**17)],
        "txlistinternal": [
            # The wallet's own creation: nothing moved.
            {"blockNumber": "5", "timeStamp": "1700000005", "hash": _hash(3), "from": FACTORY.lower(), "to": "",
             "value": "0", "contractAddress": WALLET.lower(), "isError": "0", "type": "create2"},
            {"blockNumber": "14", "timeStamp": "1700000014", "hash": _hash(4), "from": WALLET.lower(), "to": SAM.lower(),
             "value": "500", "contractAddress": "", "isError": "1", "type": "call"},
        ],
        "tokentx": [{"blockNumber": "15", "timeStamp": "1700000015", "hash": _hash(5), "from": SAM.lower(),
                     "to": WALLET.lower(), "value": "2500000", "contractAddress": USDC.lower(),
                     "tokenName": "Visit claim-usdc.example", "tokenSymbol": "USDC", "tokenDecimal": "6"}],
    }

    def answer(call):
        rows = pages[call["params"]["action"]]
        return Reply({"status": "1", "message": "OK", "result": rows})

    fake = FakeHttp(answer)
    _with_http(fake)
    try:
        batches = _all(SEPOLIA)
    finally:
        _restore_http()
    check("one batch for Etherscan", len(batches) == 1, str(len(batches)))
    movements, cursor = batches[0]
    by_hash = {m.tx_hash: m for m in movements}
    check("asks for all three lists", [c["params"]["action"] for c in fake.calls] == ["txlist", "txlistinternal", "tokentx"],
          str([c["params"]["action"] for c in fake.calls]))
    check("for this chain and wallet, newest first, with the key",
          all(c["params"]["chainid"] == SEPOLIA and c["params"]["address"] == WALLET and c["params"]["sort"] == "desc"
              and c["params"]["apikey"] == ETHERSCAN_KEY for c in fake.calls))
    check("the wallet's creation is not a movement", _hash(3) not in by_hash)
    call = by_hash.get(_hash(1))
    check("a plain transaction to the wallet is a direct call, with its calldata",
          call is not None and call.direct_call and call.input == bytes.fromhex("8456cb59") and call.sender == OWNER, str(call))
    deposit = by_hash.get(_hash(2))
    check("a deposit is native, with its amount", deposit is not None and deposit.token is None and deposit.amount == 10**17
          and deposit.input == b"", str(deposit))
    failed = by_hash.get(_hash(4))
    check("a failed internal transfer is marked failed", failed is not None and not failed.succeeded and not failed.direct_call,
          str(failed))
    token = by_hash.get(_hash(5))
    check("a token transfer: its address, amount and decimals, and nothing of its name",
          token is not None and token.token == USDC and token.amount == 2_500_000 and token.decimals == 6
          and token.recipient == WALLET and token.mined_at == 1_700_000_015, str(token))
    check("hashes are lowercase", all(m.tx_hash == m.tx_hash.lower() for m in movements))
    check("the cursor is the highest block seen, searched again next time", cursor == 15, str(cursor))


def test_etherscan_pages_and_empty_answers():
    print("\n[2] Etherscan: newest first; a full page continues below its oldest block; 'No transactions found' is empty")
    _keys()
    # 600 records in block 25 and the first 400 of block 21's: a full page that stops inside block 21.
    full = [_txlist(100 + i, 25 if i < 600 else 21, SAM, 1) for i in range(explorers.ETHERSCAN_PAGE)]
    rest = [_txlist(100 + i, 21, SAM, 1) for i in range(600, 1_100)] + [_txlist(9_999, 8, SAM, 1)]

    def answer(call):
        p = call["params"]
        if p["action"] == "txlist":
            return Reply({"status": "1", "message": "OK", "result": full if p["endblock"] == 999_999_999 else rest})
        return Reply({"status": "0", "message": "No transactions found", "result": []})

    fake = FakeHttp(answer)
    _with_http(fake)
    try:
        (movements, cursor), = _all(SEPOLIA, 7)
    finally:
        _restore_http()
    txlist = [c["params"] for c in fake.calls if c["params"]["action"] == "txlist"]
    check("newest first, from the cursor", all(p["sort"] == "desc" and p["startblock"] == 7 for p in txlist))
    check("the second page ends at the block the first stopped in",
          [p["endblock"] for p in txlist] == [999_999_999, 21], str([p["endblock"] for p in txlist]))
    hashes = [m.tx_hash for m in movements]
    check("every record read once: block 21's whole on the second page, none twice",
          len(hashes) == len(set(hashes)) == 1_101, f"{len(hashes)} read, {len(set(hashes))} different")
    check("empty lists are fine, and the cursor is the highest block", cursor == 25, str(cursor))

    one_block = [_txlist(100 + i, 30, SAM, 1) for i in range(explorers.ETHERSCAN_PAGE)]
    _with_http(FakeHttp(lambda _call: Reply({"status": "1", "message": "OK", "result": one_block})))
    try:
        error = _raises(lambda: _all(SEPOLIA))
    finally:
        _restore_http()
    check("a single block too big for a page is unavailable, not a loop", error is not None, str(error))


def test_etherscan_rate_limits_and_errors():
    print("\n[3] Etherscan: a rate limit is retried once; errors name the service, never the key")
    _keys()
    answers = iter([
        Reply({"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}),
        Reply({"status": "1", "message": "OK", "result": []}),
        Reply({}, status_code=429),
        Reply({"status": "1", "message": "OK", "result": []}),
        # What Etherscan says when a busy address's query is cold; the same query answers at once after.
        Reply({"status": "0", "message": "Query Timeout occured. Please select a smaller result dataset", "result": None}),
        Reply({"status": "1", "message": "OK", "result": []}),
    ])
    fake = FakeHttp(lambda _call: next(answers))
    _with_http(fake)
    try:
        for _ in range(3):
            explorers._etherscan_page(SEPOLIA, ETHERSCAN_KEY, "txlist", WALLET, 0, None)
    finally:
        _restore_http()
    check("a rate limit (Etherscan's words or HTTP 429) or a query timeout is retried, and goes through",
          len(fake.calls) == 6, str(len(fake.calls)))

    fake = FakeHttp(lambda _call: Reply({"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}))
    _with_http(fake)
    try:
        error = _raises(lambda: _all(SEPOLIA))
    finally:
        _restore_http()
    check("twice in a row gives up", error is not None and "rate limit" in str(error) and len(fake.calls) == 2, str(error))

    fake = FakeHttp(lambda _call: Reply({"status": "0", "message": "NOTOK",
                                         "result": f"Invalid API Key (#err2)|{ETHERSCAN_KEY}"}))
    _with_http(fake)
    try:
        error = _raises(lambda: _all(SEPOLIA))
    finally:
        _restore_http()
    check("Etherscan's own error text is passed on", error is not None and "Invalid API Key" in str(error), str(error))
    check("...with any key in it masked", error is not None and ETHERSCAN_KEY not in str(error), str(error))

    def unreachable(call):
        raise requests.ConnectionError(f"Max retries exceeded with url: /v2/api?apikey={ETHERSCAN_KEY}")

    _with_http(FakeHttp(unreachable))
    try:
        error = _raises(lambda: _all(SEPOLIA))
    finally:
        _restore_http()
    check("a network error names the service only", error is not None and str(error) == "Etherscan unreachable", str(error))
    check("...and doesn't carry the URL as its cause", error is not None and error.__cause__ is None and error.__suppress_context__)

    _with_http(FakeHttp(lambda _call: Reply(ValueError("not json"))))
    try:
        error = _raises(lambda: _all(SEPOLIA))
    finally:
        _restore_http()
    check("an answer that isn't JSON is unavailable, not a crash", error is not None, str(error))

    _keys(etherscan=None)
    error = _raises(lambda: _all(SEPOLIA))
    check("no key: unavailable, saying which setting is missing", error is not None and "ETHERSCAN_API_KEY" in str(error), str(error))
    _keys()


# ── NodeReal ──────────────────────────────────────────────────────────────────


def _transfer(n: int, block: int, category: str, sender: str, recipient: str, value: int, status: int = 1, **extra) -> dict:
    return {"category": category, "blockNum": hex(block), "from": sender.lower(), "to": recipient.lower(), "value": hex(value),
            "asset": "BNB", "hash": _hash(n), "blockTimeStamp": 1_700_000_000 + block, "receiptsStatus": status, **extra}


def test_nodereal_windows_sides_and_pages():
    print("\n[4] NodeReal: 100,000-block windows, both sides of each, pages followed, cursor per window")
    _keys()
    head = 250_000 + explorers.NODEREAL_LAG

    def answer(call):
        body = call["json"]
        if body["method"] == "eth_blockNumber":
            return Reply({"jsonrpc": "2.0", "id": 1, "result": hex(head)})
        p = body["params"][0]
        start = int(p["fromBlock"], 16)
        if start == 0 and "toAddress" in p and "pageKey" not in p:
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"pageKey": "page-2", "transfers": [
                _transfer(1, 10, "external", OWNER, WALLET, 0),
                _transfer(2, 11, "20", SAM, WALLET, 2_500_000, contractAddress=USDC.lower(), decimal="0x6"),
            ]}})
        if start == 0 and "toAddress" in p:
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"pageKey": "", "transfers": [
                _transfer(3, 12, "internal", SAM, WALLET, 10**18)]}})
        if start == 100_000 and "fromAddress" in p:
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": [
                _transfer(4, 100_005, "internal", WALLET, SAM, 7, status=0)]}})
        return Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": []}})

    fake = FakeHttp(answer)
    _with_http(fake)
    try:
        batches = _all(BSC, 0)
    finally:
        _restore_http()
    searches = [c["json"]["params"][0] for c in fake.calls if c["json"]["method"] == "nr_getAssetTransfers"]
    windows = sorted({(int(p["fromBlock"], 16), int(p["toBlock"], 16)) for p in searches})
    check("windows of at most 100,000 blocks, up to the head less the lag",
          windows == [(0, 99_999), (100_000, 199_999), (200_000, 250_000)], str(windows))
    check("each window searched from the wallet and to it",
          all({side for p in searches if int(p["fromBlock"], 16) == w[0] for side in ("fromAddress", "toAddress") if side in p}
              == {"fromAddress", "toAddress"} for w in windows))
    check("zero values are asked for (the owner's calls carry none), with the three categories",
          all(p["excludeZeroValue"] is False and p["category"] == ["external", "internal", "20"] for p in searches))
    check("the key is in the URL", all(c["url"] == f"https://bsc-mainnet.nodereal.io/v1/{NODEREAL_KEY}" for c in fake.calls))
    check("a page key is followed", any(p.get("pageKey") == "page-2" for p in searches))
    check("one batch per window, each with the block the next search starts from",
          [cursor for _, cursor in batches] == [100_000, 200_000, 250_001], str([cursor for _, cursor in batches]))
    by_hash = {m.tx_hash: m for movements, _ in batches for m in movements}
    owner_call = by_hash.get(_hash(1))
    check("an external transaction to the wallet is a direct call, its calldata read later",
          owner_call is not None and owner_call.direct_call and owner_call.input is None and owner_call.amount == 0, str(owner_call))
    token = by_hash.get(_hash(2))
    check("a token transfer: hex amount and decimals read",
          token is not None and token.token == USDC and token.amount == 2_500_000 and token.decimals == 6 and token.block == 11,
          str(token))
    internal = by_hash.get(_hash(3))
    check("an internal transfer is native", internal is not None and internal.token is None and internal.amount == 10**18
          and internal.mined_at == 1_700_000_012, str(internal))
    failed = by_hash.get(_hash(4))
    check("a failed receipt is marked failed", failed is not None and not failed.succeeded, str(failed))

    saved = explorers.NODEREAL_MAX_WINDOWS
    explorers.NODEREAL_MAX_WINDOWS = 2
    _with_http(FakeHttp(answer))
    try:
        capped = _all(BSC, 0)
    finally:
        _restore_http()
        explorers.NODEREAL_MAX_WINDOWS = saved
    check("a run stops after the window cap; the next continues from its cursor",
          [cursor for _, cursor in capped] == [100_000, 200_000], str([cursor for _, cursor in capped]))

    _with_http(FakeHttp(answer))
    try:
        caught_up = _all(BSC, head)
    finally:
        _restore_http()
    check("nothing to search when already at the head", caught_up == [], str(caught_up))


def test_nodereal_lookups_limits_and_errors():
    print("\n[5] NodeReal: block and calldata lookups, rate limits, errors without the key")
    _keys()

    def answer(call):
        body = call["json"]
        if body["method"] == "eth_getTransactionReceipt":
            known = body["params"][0] == _hash(1)
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"blockNumber": "0x309"} if known else None})
        if body["method"] == "eth_getTransactionByHash":
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"input": "0x8456cb59"}})
        return Reply({"jsonrpc": "2.0", "id": 1, "result": hex(1_000)})

    _with_http(FakeHttp(answer))
    try:
        check("the block a transaction was mined in", explorers.transaction_block(BSC, _hash(1)) == 0x309)
        check("None for a transaction the live chain doesn't have", explorers.transaction_block(BSC, _hash(2)) is None)
        check("a transaction's calldata", explorers.transaction_input(BSC, _hash(1)) == bytes.fromhex("8456cb59"))
        check("the head, less the blocks it may not have indexed", explorers.latest_block(BSC) == 1_000 - explorers.NODEREAL_LAG)
    finally:
        _restore_http()

    answers = iter([
        Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit exceeded"}}),
        Reply({"jsonrpc": "2.0", "id": 1, "result": hex(1_000)}),
    ])
    fake = FakeHttp(lambda _call: next(answers))
    _with_http(fake)
    try:
        explorers.latest_block(BSC)
    finally:
        _restore_http()
    check("a rate limit is retried once", len(fake.calls) == 2)

    _with_http(FakeHttp(lambda _call: Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": f"bad key {NODEREAL_KEY}"}})))
    try:
        error = _raises(lambda: explorers.latest_block(BSC))
    finally:
        _restore_http()
    check("an error names the service, with the key masked",
          error is not None and str(error).startswith("NodeReal") and NODEREAL_KEY not in str(error), str(error))

    def unreachable(call):
        raise requests.Timeout(f"HTTPSConnectionPool: /v1/{NODEREAL_KEY} timed out")

    _with_http(FakeHttp(unreachable))
    try:
        error = _raises(lambda: _all(BSC))
    finally:
        _restore_http()
    check("a network error names the service only", error is not None and str(error) == "NodeReal unreachable", str(error))

    _with_http(FakeHttp(lambda _call: Reply({}, status_code=500)))
    try:
        error = _raises(lambda: _all(BSC))
    finally:
        _restore_http()
    check("an HTTP error says its status", error is not None and "500" in str(error), str(error))

    _keys(nodereal=None)
    error = _raises(lambda: _all(BSC))
    check("no key: unavailable, saying which setting is missing", error is not None and "NODEREAL_API_KEY" in str(error), str(error))
    _keys()


# ── Alchemy ───────────────────────────────────────────────────────────────────


def _iso(block: int) -> str:
    """The block's time as Alchemy writes it, e.g. "2023-11-14T22:13:30.000Z"."""
    return datetime.fromtimestamp(1_700_000_000 + block, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _alchemy_transfer(n: int, block: int, category: str, sender: str, recipient: str | None, raw_value: int,
                      token: str | None = None, decimals: int | None = 18) -> dict:
    # `value` is Alchemy's float, already divided by the decimals; only rawContract is exact.
    return {"blockNum": hex(block), "uniqueId": f"{_hash(n)}:{category}", "hash": _hash(n), "from": sender.lower(),
            "to": recipient.lower() if recipient else None, "value": raw_value / 10 ** (decimals or 18),
            "asset": "USDC" if token else "ETH", "category": category,
            "rawContract": {"value": hex(raw_value), "address": token.lower() if token else None,
                            "decimal": hex(decimals) if decimals is not None else None},
            "metadata": {"blockTimestamp": _iso(block)}}


def test_alchemy_both_sides_and_pages():
    print("\n[6] Alchemy: the whole range in one batch, both sides, pages followed, exact amounts")
    _keys()

    def answer(call):
        p = call["json"]["params"][0]
        if "toAddress" in p and "pageKey" not in p:
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"pageKey": "page-2", "transfers": [
                _alchemy_transfer(1, 10, "external", OWNER, WALLET, 0),
                _alchemy_transfer(2, 11, "erc20", SAM, WALLET, 2_500_000, token=USDC, decimals=6),
            ]}})
        if "toAddress" in p:
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": [
                _alchemy_transfer(3, 12, "internal", SAM, WALLET, 10**18)]}})
        return Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": [
            # The wallet's own creation by the factory: nothing moved.
            _alchemy_transfer(4, 5, "internal", FACTORY, None, 0),
            _alchemy_transfer(5, 100_005, "internal", WALLET, SAM, 7),
        ]}})

    fake = FakeHttp(answer)
    _with_http(fake)
    try:
        batches = _all(BASE, 0)
    finally:
        _restore_http()
    searches = [c["json"]["params"][0] for c in fake.calls]
    check("one batch, from the cursor to the head", len(batches) == 1 and all(
        p["fromBlock"] == "0x0" and p["toBlock"] == "latest" for p in searches), str([(p["fromBlock"], p["toBlock"]) for p in searches]))
    check("searched from the wallet and to it",
          {side for p in searches for side in ("fromAddress", "toAddress") if p.get(side) == WALLET} == {"fromAddress", "toAddress"})
    check("zero values and the block's time asked for, with the three categories",
          all(p["excludeZeroValue"] is False and p["withMetadata"] is True and p["category"] == ["external", "internal", "erc20"]
              for p in searches))
    check("Alchemy's own method, with the key in the URL",
          all(c["json"]["method"] == "alchemy_getAssetTransfers" and c["url"] == f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"
              for c in fake.calls))
    check("a page key is followed", any(p.get("pageKey") == "page-2" for p in searches))
    movements, cursor = batches[0]
    by_hash = {m.tx_hash: m for m in movements}
    owner_call = by_hash.get(_hash(1))
    check("an external transaction to the wallet is a direct call, its calldata read later",
          owner_call is not None and owner_call.direct_call and owner_call.input is None and owner_call.amount == 0, str(owner_call))
    token = by_hash.get(_hash(2))
    check("a token transfer: exact base units and decimals from rawContract, not the rounded float",
          token is not None and token.token == USDC and token.amount == 2_500_000 and token.decimals == 6 and token.block == 11,
          str(token))
    internal = by_hash.get(_hash(3))
    check("an internal transfer is native, its time read from the metadata",
          internal is not None and internal.token is None and internal.decimals is None and internal.amount == 10**18
          and internal.mined_at == 1_700_000_012, str(internal))
    check("the wallet's creation is not a movement", _hash(4) not in by_hash)
    out = by_hash.get(_hash(5))
    check("a payment out of the wallet, marked succeeded (Alchemy lists no failed transfer)",
          out is not None and out.sender == WALLET and out.recipient == SAM and out.succeeded and not out.direct_call, str(out))
    check("the cursor is the highest block seen, searched again next time", cursor == 100_005, str(cursor))

    _with_http(FakeHttp(lambda _call: Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": []}})))
    try:
        (nothing, cursor), = _all(BASE, 4_242)
    finally:
        _restore_http()
    check("nothing new: no movements, and the cursor stays where it was", nothing == [] and cursor == 4_242, str(cursor))


def test_alchemy_lookups_limits_and_errors():
    print("\n[7] Alchemy: calldata lookups, rate limits, errors without the key")
    _keys()

    def answer(call):
        body = call["json"]
        if body["method"] == "eth_getTransactionByHash":
            known = body["params"][0] == _hash(1)
            return Reply({"jsonrpc": "2.0", "id": 1, "result": {"input": "0x8456cb59"} if known else None})
        return Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": []}})

    fake = FakeHttp(answer)
    _with_http(fake)
    try:
        data = explorers.transaction_input(BASE, _hash(1))
        error = _raises(lambda: explorers.transaction_input(BASE, _hash(2)))
    finally:
        _restore_http()
    check("a transaction's calldata, from Alchemy", data == bytes.fromhex("8456cb59")
          and fake.calls[0]["url"] == f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}", str(data))
    check("a transaction Alchemy doesn't know is unavailable", error is not None and "Alchemy" in str(error), str(error))

    answers = iter([
        Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": 429, "message": "Your app has exceeded its compute units per "
                                                    "second capacity. If you have retries enabled, you can safely ignore this message."}}),
        Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": []}}),
        Reply({}, status_code=429),
        Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": []}}),
    ])
    fake = FakeHttp(lambda _call: next(answers))
    _with_http(fake)
    try:
        _all(BASE)
    finally:
        _restore_http()
    check("a rate limit (Alchemy's code 429 or HTTP 429) is retried once, and goes through", len(fake.calls) == 4, str(len(fake.calls)))

    _with_http(FakeHttp(lambda _call: Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": f"Must be authenticated! {ALCHEMY_KEY}"}})))
    try:
        error = _raises(lambda: _all(BASE))
    finally:
        _restore_http()
    check("an error names the service, with the key masked",
          error is not None and str(error).startswith("Alchemy") and ALCHEMY_KEY not in str(error), str(error))

    def unreachable(call):
        raise requests.ConnectionError(f"Max retries exceeded with url: /v2/{ALCHEMY_KEY}")

    _with_http(FakeHttp(unreachable))
    try:
        error = _raises(lambda: _all(BASE))
    finally:
        _restore_http()
    check("a network error names the service only", error is not None and str(error) == "Alchemy unreachable", str(error))

    _with_http(FakeHttp(lambda _call: Reply({}, status_code=401)))
    try:
        error = _raises(lambda: _all(BASE))
    finally:
        _restore_http()
    check("a refused key says its status", error is not None and "401" in str(error), str(error))

    _keys(alchemy=None)
    error = _raises(lambda: _all(BASE))
    check("no key: unavailable, saying which setting is missing", error is not None and "ALCHEMY_API_KEY" in str(error), str(error))
    _keys()


def test_which_service_covers_which_chain():
    print("\n[8] which service covers which chain")
    check("Etherscan for Ethereum, Sepolia, Arbitrum and Celo",
          all(explorers.provider_for(c) == "etherscan" for c in (1, 11155111, 42161, 42220)))
    check("NodeReal for BSC", explorers.provider_for(56) == "nodereal")
    check("Alchemy for Base", explorers.provider_for(8453) == "alchemy")
    check("nothing for a local anvil chain", explorers.provider_for(31337) is None)
    check("a chain nothing covers yields nothing", list(explorers.movements_since(31337, WALLET, 0)) == [])


if __name__ == "__main__":
    test_etherscan_reads_all_three_lists()
    test_etherscan_pages_and_empty_answers()
    test_etherscan_rate_limits_and_errors()
    test_nodereal_windows_sides_and_pages()
    test_nodereal_lookups_limits_and_errors()
    test_alchemy_both_sides_and_pages()
    test_alchemy_lookups_limits_and_errors()
    test_which_service_covers_which_chain()
    finish("All explorer checks passed.")
