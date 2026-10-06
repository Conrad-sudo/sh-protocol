"""
The services the History tab reads activity from outside Mitfah: Etherscan's API for most chains,
and NodeReal for BSC, since Etherscan charges for BSC. Nothing here knows about users or the
database. tx_history.sync_outside decides what is worth a row.

A Mitfah wallet is a contract, so it never sends a transaction of its own. What it pays out shows
up as an internal transaction or a token transfer inside somebody else's transaction. Each service
is therefore asked for three kinds of record (plain transactions, internal ones and ERC-20
transfers), and they come back as one flat list of Movements.

Both services take their key in the URL, and a `requests` error carries the URL. Every network
error is therefore replaced by an ExplorerUnavailable that names only the service, so a key never
reaches a log.
"""
import os
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import requests
from web3 import Web3

from constants import (
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
)

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
# Chains Etherscan's free tier serves. BSC is not one of them.
ETHERSCAN_CHAINS = {CHAIN_ID_MAINNET, CHAIN_ID_SEPOLIA, CHAIN_ID_ARBITRUM, CHAIN_ID_CELO}
ETHERSCAN_ACTIONS = ("txlist", "txlistinternal", "tokentx")
# Records per Etherscan page. A full page means there may be more, below its oldest block.
ETHERSCAN_PAGE = 1000

NODEREAL_URLS = {CHAIN_ID_BSC: "https://bsc-mainnet.nodereal.io/v1/{key}"}
# nr_getAssetTransfers searches at most 100,000 blocks per call.
NODEREAL_WINDOW = 100_000
# Windows searched per sync. A run stops after these, and the next run continues from there.
NODEREAL_MAX_WINDOWS = 100
# Blocks left behind the head for the next run, in case NodeReal hasn't finished indexing them.
NODEREAL_LAG = 50
NODEREAL_PAGE = 1000
NODEREAL_CATEGORIES = ["external", "internal", "20"]

# Longer than Etherscan's own query timeout (about 20s), so its "Query Timeout" answer arrives.
TIMEOUT_SECS = 30
KEY_NAMES = ("ETHERSCAN_API_KEY", "NODEREAL_API_KEY")


class ExplorerUnavailable(Exception):
    """
    The service can't be used right now: no key, rate limited, or an answer we can't read. The
    message names the service only, never the URL that holds the key, and any key that slips into
    a service's own text is masked.
    """

    def __init__(self, message: str):
        for name in KEY_NAMES:
            key = os.getenv(name, "").strip()
            if key:
                message = message.replace(key, "***")
        super().__init__(message)


class _TryAgain(Exception):
    """
    Worth one more try: the service asked us to slow down, or timed out on a busy address. Etherscan
    answers the same question at once a moment later, its cache warmed by the first. Retried once
    by _retry_once.
    """


@dataclass(frozen=True)
class Movement:
    """One movement into or out of a wallet, or one plain transaction sent to it."""

    tx_hash: str          # 0x..., lowercase
    block: int
    mined_at: int         # the block's time
    sender: str           # checksummed
    recipient: str        # checksummed
    token: str | None     # checksummed ERC-20 address; None for the chain's native coin
    amount: int           # base units
    decimals: int | None  # the token's, as the service reports them; None when it doesn't
    direct_call: bool     # a plain transaction sent to the wallet
    input: bytes | None   # a direct call's calldata; None when the service didn't send it
    succeeded: bool


class _Pace:
    """Spaces calls to one service at least `gap` seconds apart, across every thread."""

    def __init__(self, per_second: float):
        self._gap = 1 / per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self._gap
        time.sleep(at - now)


# Etherscan's free tier allows 5 calls a second per key; 4 leaves room.
_etherscan_pace = _Pace(4)
# NodeReal's free plan allows 150 compute units a second, and one nr_getAssetTransfers costs 250.
_nodereal_pace = _Pace(0.5)


def provider_for(chain_id: int) -> str | None:
    """Which service reports activity on `chain_id`, or None for a chain no service covers (anvil)."""
    if chain_id in ETHERSCAN_CHAINS:
        return "etherscan"
    if chain_id in NODEREAL_URLS:
        return "nodereal"
    return None


def movements_since(chain_id: int, wallet: str, from_block: int) -> Iterator[tuple[list[Movement], int]]:
    """
    Every movement into or out of `wallet` from `from_block` on, a batch at a time.

    Yields (movements, next_from_block) per batch. Each batch is complete up to that cursor, so the
    caller can save the cursor after recording each batch. Etherscan answers in one batch. NodeReal
    yields one batch per 100,000-block window and stops after NODEREAL_MAX_WINDOWS.

    @raises ExplorerUnavailable  On a missing key or any failed call. Batches already yielded stand.
    """
    wallet = Web3.to_checksum_address(wallet)
    provider = provider_for(chain_id)
    if provider == "etherscan":
        yield _etherscan(chain_id, wallet, from_block)
    elif provider == "nodereal":
        yield from _nodereal(chain_id, wallet, from_block)


def transaction_block(chain_id: int, tx_hash: str) -> int | None:
    """
    The block a transaction was mined in, as NodeReal sees it: where a BSC search starts. None when
    the live chain has no such transaction, as for a wallet deployed on a fork.
    """
    receipt = _nodereal_rpc(_nodereal_url(chain_id), "eth_getTransactionReceipt", [tx_hash])
    return None if receipt is None else _int(receipt["blockNumber"])


def latest_block(chain_id: int) -> int:
    """NodeReal's latest block, less the few it may not have indexed yet."""
    return _int(_nodereal_rpc(_nodereal_url(chain_id), "eth_blockNumber", [])) - NODEREAL_LAG


def transaction_input(chain_id: int, tx_hash: str) -> bytes:
    """A transaction's calldata. Only needed for NodeReal, whose transfer list leaves it out."""
    tx = _nodereal_rpc(_nodereal_url(chain_id), "eth_getTransactionByHash", [tx_hash])
    if tx is None:
        raise ExplorerUnavailable("NodeReal does not know the transaction")
    return _bytes(tx.get("input"))


# ── Etherscan ─────────────────────────────────────────────────────────────────


def _etherscan(chain_id: int, wallet: str, from_block: int) -> tuple[list[Movement], int]:
    """
    All three record kinds from `from_block` on, newest first. Etherscan times out on a busy
    address asked for its oldest records after a recent block, but answers newest-first at once.

    A full page continues below the block it stopped in: that block's records are dropped from the
    page and read whole on the next one, so none is read twice and none is missed. The cursor
    returned is the highest block seen, and that block is searched again next time, so a record
    indexed after this search isn't skipped. The repeats are dropped by the caller, which knows
    every hash it has recorded.
    """
    key = _key("ETHERSCAN_API_KEY", "Etherscan")
    movements: list[Movement] = []
    highest = from_block
    for action in ETHERSCAN_ACTIONS:
        end = None
        while True:
            rows = _etherscan_page(chain_id, key, action, wallet, from_block, end)
            blocks = [int(row["blockNumber"]) for row in rows]
            highest = max([highest, *blocks])
            if len(rows) < ETHERSCAN_PAGE:
                movements += [m for m in (_from_etherscan(action, wallet, row) for row in rows) if m]
                break
            oldest = min(blocks)
            whole = [row for row, block in zip(rows, blocks) if block != oldest]
            if not whole:
                raise ExplorerUnavailable("Etherscan: a single block holds more records than one page")
            movements += [m for m in (_from_etherscan(action, wallet, row) for row in whole) if m]
            end = oldest
    return movements, highest


def _etherscan_page(chain_id: int, key: str, action: str, wallet: str, start: int, end: int | None) -> list[dict]:
    params = {
        "chainid": chain_id,
        "module": "account",
        "action": action,
        "address": wallet,
        "startblock": start,
        "endblock": end if end is not None else 999_999_999,
        "page": 1,
        "offset": ETHERSCAN_PAGE,
        "sort": "desc",
        "apikey": key,
    }
    def once() -> list[dict]:
        _etherscan_pace.wait()
        body = _get_json("Etherscan", lambda: requests.get(ETHERSCAN_URL, params=params, timeout=TIMEOUT_SECS))
        result = body.get("result")
        if body.get("status") == "1" and isinstance(result, list):
            return result
        if str(body.get("message", "")).startswith("No transactions found"):
            return []
        text = str(result or body.get("message", ""))
        if "rate limit" in text.lower() or "query timeout" in text.lower():
            raise _TryAgain(text[:120])
        # Etherscan's own text ("Invalid API Key", a chain the plan doesn't cover, ...).
        raise ExplorerUnavailable(f"Etherscan: {text[:120]}")

    return _retry_once("Etherscan", once)


def _from_etherscan(action: str, wallet: str, row: dict) -> Movement | None:
    if not row.get("to"):
        return None  # a contract creation, such as the wallet's own: nothing moved
    sender = Web3.to_checksum_address(row["from"])
    recipient = Web3.to_checksum_address(row["to"])
    direct_call = action == "txlist" and recipient == wallet
    failed = row.get("isError", "0") != "0" or row.get("txreceipt_status") == "0"
    return Movement(
        tx_hash=row["hash"].lower(),
        block=int(row["blockNumber"]),
        mined_at=int(row["timeStamp"]),
        sender=sender,
        recipient=recipient,
        token=Web3.to_checksum_address(row["contractAddress"]) if action == "tokentx" else None,
        amount=int(row["value"]),
        decimals=int(row["tokenDecimal"]) if action == "tokentx" and row.get("tokenDecimal") else None,
        direct_call=direct_call,
        input=_bytes(row.get("input")) if direct_call else None,
        succeeded=not failed,
    )


# ── NodeReal ──────────────────────────────────────────────────────────────────


def _nodereal(chain_id: int, wallet: str, from_block: int) -> Iterator[tuple[list[Movement], int]]:
    """
    One batch per 100,000-block window, from `from_block` up to a little behind the live head.

    The head is NodeReal's own, not our RPC's, so this also works while the app runs on a fork.
    NodeReal can't match an address on either side, so each window takes two searches: transfers
    from the wallet and transfers to it. Zero values are kept, since the owner's calls on the
    wallet carry none. tx_history drops the ones that matter to nobody.
    """
    url = _nodereal_url(chain_id)
    end = latest_block(chain_id)
    start = from_block
    for _ in range(NODEREAL_MAX_WINDOWS):
        if start > end:
            return
        stop = min(start + NODEREAL_WINDOW - 1, end)
        movements: list[Movement] = []
        for side in ("fromAddress", "toAddress"):
            movements += _nodereal_window(url, wallet, start, stop, side)
        start = stop + 1
        yield movements, start


def _nodereal_window(url: str, wallet: str, start: int, stop: int, side: str) -> list[Movement]:
    params = {
        "category": NODEREAL_CATEGORIES,
        "fromBlock": hex(start),
        "toBlock": hex(stop),
        side: wallet,
        "order": "asc",
        "excludeZeroValue": False,
        "maxCount": hex(NODEREAL_PAGE),
    }
    movements: list[Movement] = []
    seen_keys: set[str] = set()
    while True:
        result = _nodereal_rpc(url, "nr_getAssetTransfers", [params]) or {}
        transfers = result.get("transfers") or []
        movements += [m for m in (_from_nodereal(wallet, t) for t in transfers) if m]
        page_key = result.get("pageKey") or result.get("PageKey")
        if not transfers or not page_key or page_key in seen_keys:
            return movements
        seen_keys.add(page_key)
        params = params | {"pageKey": page_key}


def _from_nodereal(wallet: str, t: dict) -> Movement | None:
    if not t.get("to"):
        return None  # a contract creation: nothing moved
    category = t.get("category")
    sender = Web3.to_checksum_address(t["from"])
    recipient = Web3.to_checksum_address(t["to"])
    is_token = category == "20"
    status = t.get("receiptsStatus")
    return Movement(
        tx_hash=t["hash"].lower(),
        block=_int(t["blockNum"]),
        mined_at=_int(t.get("blockTimeStamp") or t.get("blockTimestamp") or 0),
        sender=sender,
        recipient=recipient,
        token=Web3.to_checksum_address(t["contractAddress"]) if is_token else None,
        amount=_int(t.get("value") or 0),
        decimals=_int(t["decimal"]) if is_token and t.get("decimal") not in (None, "") else None,
        direct_call=category == "external" and recipient == wallet,
        input=None,  # not in the transfer list; transaction_input reads it when it is needed
        succeeded=status is None or _int(status) == 1,
    )


def _nodereal_url(chain_id: int) -> str:
    if chain_id not in NODEREAL_URLS:
        raise ExplorerUnavailable(f"NodeReal does not serve chain {chain_id}")
    return NODEREAL_URLS[chain_id].format(key=_key("NODEREAL_API_KEY", "NodeReal"))


def _nodereal_rpc(url: str, method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

    def once():
        _nodereal_pace.wait()
        body = _get_json("NodeReal", lambda: requests.post(url, json=payload, timeout=TIMEOUT_SECS))
        error = body.get("error")
        if error is None:
            return body.get("result")
        message = str(error.get("message", error)) if isinstance(error, dict) else str(error)
        if "limit" in message.lower():
            raise _TryAgain(message[:120])
        raise ExplorerUnavailable(f"NodeReal: {message[:120]}")

    return _retry_once("NodeReal", once)


# ── Shared ────────────────────────────────────────────────────────────────────


def _key(env_name: str, service: str) -> str:
    key = os.getenv(env_name, "").strip()
    if not key:
        raise ExplorerUnavailable(f"{service}: no API key ({env_name} is not set)")
    return key


def _retry_once(service: str, call):
    """Runs `call`, and once more after a pause if the service says to try again."""
    try:
        return call()
    except _TryAgain:
        time.sleep(2)
    try:
        return call()
    except _TryAgain as e:
        raise ExplorerUnavailable(f"{service}: {e}") from None


def _get_json(service: str, send) -> dict:
    """Sends a request and reads its JSON, with every failure stripped of the URL (and the key in it)."""
    try:
        response = send()
    except requests.RequestException:
        raise ExplorerUnavailable(f"{service} unreachable") from None
    if response.status_code == 429:
        raise _TryAgain("rate limited")
    if response.status_code != 200:
        raise ExplorerUnavailable(f"{service} answered HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise ExplorerUnavailable(f"{service} sent an answer that isn't JSON") from None
    if not isinstance(body, dict):
        raise ExplorerUnavailable(f"{service} sent an answer we can't read")
    return body


def _int(value) -> int:
    """An int from an int, a 0x hex string or a decimal string, as these services mix them."""
    if isinstance(value, int):
        return value
    text = str(value)
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def _bytes(value) -> bytes:
    if not value:
        return b""
    return bytes.fromhex(str(value).removeprefix("0x"))
