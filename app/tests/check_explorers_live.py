"""
A read-only check of explorers.py against the REAL services, with the keys in .env: it reads a busy
public address's recent activity on Sepolia and Arbitrum (Etherscan), BSC (NodeReal) and Base
(Alchemy), and checks that the answers read into sensible movements. Confirms the offline fixtures in test_explorers.py
match what the services actually send.

Sends nothing to any chain. Uses a few dozen calls of each service's free allowance.

Run: make explorers-live   (or: python app/tests/check_explorers_live.py)
"""
import time

import requests
from dotenv import load_dotenv

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

load_dotenv()

import explorers                   # noqa: E402

# (chain_id, label, a busy public address, how many recent blocks to read)
CASES = [
    (11155111, "Sepolia, the Uniswap v2 router Mitfah uses", "0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3", 5_000),
    (42161, "Arbitrum, Circle's USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 200),
    (56, "BSC, PancakeSwap's v2 router", "0x10ED43C718714eb63d5aA57B78B54704E256024E", 400),
    (8453, "Base, the Uniswap v2 router Mitfah uses", "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24", 200),
]


def _etherscan_head(chain_id: int) -> int:
    explorers._etherscan_pace.wait()
    body = requests.get(
        explorers.ETHERSCAN_URL,
        params={"chainid": chain_id, "module": "proxy", "action": "eth_blockNumber",
                "apikey": explorers._key("ETHERSCAN_API_KEY", "Etherscan")},
        timeout=explorers.TIMEOUT_SECS,
    ).json()
    return int(body["result"], 16)


def _head(chain_id: int) -> int:
    provider = explorers.provider_for(chain_id)
    if provider == "etherscan":
        return _etherscan_head(chain_id)
    if provider == "alchemy":
        return int(explorers._alchemy_rpc(explorers._alchemy_url(chain_id), "eth_blockNumber", []), 16)
    return explorers.latest_block(chain_id)


def _show(m: explorers.Movement) -> str:
    what = "native" if m.token is None else f"token {m.token[:10]}… ({m.decimals} decimals)"
    kind = "direct call" if m.direct_call else "transfer"
    return (f"{kind}, {what}, {m.amount} base units, {m.sender[:10]}… -> {m.recipient[:10]}…, "
            f"block {m.block}, {'ok' if m.succeeded else 'FAILED'}")


def check_chain(chain_id: int, label: str, address: str, blocks: int):
    print(f"\n[{label}] ({explorers.provider_for(chain_id)}), the last {blocks} blocks")
    head = _head(chain_id)
    start = head - blocks
    began = time.monotonic()
    batches = list(explorers.movements_since(chain_id, address, start))
    took = time.monotonic() - began
    movements = [m for batch, _ in batches for m in batch]
    cursor = batches[-1][1] if batches else None

    print(f"  {len(movements)} movements in {len(batches)} batch(es), {took:.1f}s; cursor {cursor}")
    for m in movements[:3]:
        print(f"    {_show(m)}")
    check("found some activity (a busy address)", len(movements) > 0)
    check("every hash is a 32-byte 0x hash, lowercase",
          all(len(m.tx_hash) == 66 and m.tx_hash == m.tx_hash.lower() for m in movements))
    # Not checked against the head: the chain moves on while the search runs.
    check("every block is from the start on", all(m.block >= start for m in movements),
          str([m.block for m in movements if m.block < start][:3]))
    now = time.time()
    check("every time is recent and in the past", all(now - 86_400 * 7 < m.mined_at <= now + 60 for m in movements),
          str([m.mined_at for m in movements][:3]))
    check("every movement touches the address", all(address in (m.sender, m.recipient) for m in movements))
    check("amounts are whole base units", all(isinstance(m.amount, int) and m.amount >= 0 for m in movements))
    tokens = [m for m in movements if m.token is not None]
    check("token transfers carry their decimals", all(m.decimals is not None for m in tokens),
          str([_show(m) for m in tokens if m.decimals is None][:2]))
    check("the cursor moved past the start", cursor is not None and cursor >= start, str(cursor))

    if explorers.provider_for(chain_id) == "nodereal" and movements:
        sample = movements[0]
        block = explorers.transaction_block(chain_id, sample.tx_hash)
        check("NodeReal: a transaction's block is read", block == sample.block, f"{block} != {sample.block}")
        data = explorers.transaction_input(chain_id, sample.tx_hash)
        check("NodeReal: a transaction's calldata is read", isinstance(data, bytes))

    direct = next((m for m in movements if m.direct_call), None)
    if explorers.provider_for(chain_id) == "alchemy" and direct is not None:
        data = explorers.transaction_input(chain_id, direct.tx_hash)
        check("Alchemy: a direct call's calldata is read", isinstance(data, bytes) and len(data) >= 4, data[:8].hex())


if __name__ == "__main__":
    for case in CASES:
        try:
            check_chain(*case)
        except explorers.ExplorerUnavailable as e:
            check(f"{case[1]}: the service answered", False, str(e))
    finish("The live explorers read as expected.")
