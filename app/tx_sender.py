"""
Nonce-safe broadcast of transactions sent from the app's own EOAs.

telebot.py runs every user request in its own thread (asyncio.to_thread), but the app signs its
outer transactions with a small number of SHARED keys -- one bundler EOA per process in bundler.py,
one deployer/owner EOA per chain in deploy_wallet.py. Reading the nonce per-thread races: two
threads read the same value, and the second transaction either silently replaces the first (same
nonce) or is rejected as a duplicate. This never showed on a single-user Anvil run; it appears
the moment two users act at once.

Nonces are therefore handed out from a cached per-key counter under a process-wide lock instead
of being read from the node on every send. The lock covers ONE process only, which is why the API
and the Telegram bot each bundle with a key of their own (see bundler.use_bundler_key): two
processes sharing a key would each keep their own counter and hand out the same nonces.
"""

import threading
import time
from typing import Any

from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import TxReceipt

# (chain_name, address) -> the next nonce to hand out. Keyed by chain as well as address because
# one key is reused across networks (API_BUNDLER spans every fork plus live Sepolia), and
# each has its own nonce state. Guarded by _nonce_lock, which also covers the signing and
# broadcast that consume a value.
_nonce_lock = threading.Lock()
_next_nonce: dict[tuple[str, str], int] = {}

# A replacement must outbid the transaction it replaces on EVERY fee field by the node's price
# bump -- geth's default is 10% -- or it is rejected as underpriced and the original stays stuck.
# 1.25 clears that with room for nodes configured higher.
FEE_BUMP_MULTIPLIER = 1.25
# Bumps attempted before giving up. Four leaves the final attempt at ~2.4x the opening fee.
MAX_FEE_BUMPS = 4
# How long one attempt waits for inclusion before replacing itself at a higher fee. Several
# blocks on any chain here, so a transaction that is merely unlucky is not replaced needlessly.
ATTEMPT_TIMEOUT_SECS = 45
RECEIPT_POLL_INTERVAL_SECS = 2


def send_tx(
    w3: Web3, chain_name: str, account: LocalAccount, tx: dict[str, Any], send_w3: Web3 | None = None
) -> HexBytes:
    """
    Signs and broadcasts `tx` from `account`, allocating its nonce under a process-wide lock.

    Any "nonce" already present in `tx` is overwritten. The lock spans allocate -> sign ->
    broadcast so no two threads can be handed the same value, but it deliberately excludes gas
    estimation and receipt polling: callers do those on either side of this call, and so queue
    behind one eth_sendRawTransaction rather than behind each other's block confirmations.

    The counter is seeded from the node on first use and after any failure, then advanced locally.
    Re-reading it per send would not work: a transaction sitting in the mempool has not moved the
    node's count, so the next caller would be handed a nonce that is still in flight.

    @param w3          Web3 connection for the target network.
    @param chain_name  Network name; part of the counter key, since one key spans several chains.
    @param account     Local signing account. Must match the transaction's "from".
    @param tx          A fully populated transaction dict apart from "nonce".
    @param send_w3     Where to broadcast, when not `w3` -- a private RPC that keeps the transaction
                       out of the public mempool. Nonces are still read from `w3`, which cannot see
                       a privately sent transaction until it is mined: that is fine while this
                       process's counter is warm, but a restart with one still pending re-seeds
                       below it.
    @return            The broadcast transaction hash.
    """
    key = (chain_name, account.address)
    with _nonce_lock:
        for attempt in range(2):
            if key not in _next_nonce:
                # "pending" counts what is already in the mempool; web3's default of "latest"
                # would hand back a nonce one of our own unmined transactions already holds.
                _next_nonce[key] = w3.eth.get_transaction_count(account.address, "pending")

            tx["nonce"] = _next_nonce[key]
            try:
                tx_hash = _broadcast(send_w3 or w3, account, tx)
            except Exception:
                # The nonce was not consumed, and the counter may be stale for a reason not
                # visible from here -- a transaction sent from this key out of band, or a node
                # that dropped one of ours. Drop it so the retry re-seeds from the node; leaving
                # a gap would strand every later transaction from this key. A second failure is
                # the real error and surfaces to the caller.
                del _next_nonce[key]
                if attempt == 1:
                    raise
                continue

            _next_nonce[key] += 1
            return tx_hash


def _broadcast(w3: Web3, account: LocalAccount, tx: dict[str, Any]) -> HexBytes:
    """Signs `tx` as-is and puts it on the wire. The single signing path for this module."""
    signed = w3.eth.account.sign_transaction(tx, account.key)
    return w3.eth.send_raw_transaction(signed.raw_transaction)


def _bumped(value: int) -> int:
    """Raises `value` by FEE_BUMP_MULTIPLIER, always by at least 1 wei so it never stalls."""
    return max(int(value * FEE_BUMP_MULTIPLIER), value + 1)


def _bump_fees(w3: Web3, tx: dict[str, Any]) -> None:
    """
    Raises `tx`'s fee fields in place so it can replace itself at its existing nonce.

    EIP-1559 needs BOTH fields bumped: a replacement is judged on each independently, so lifting
    only the cap leaves it underpriced and silently rejected.
    """
    if "gasPrice" in tx:
        tx["gasPrice"] = _bumped(tx["gasPrice"])
        return

    tip = _bumped(tx["maxPriorityFeePerGas"])
    # Floor the cap against the CURRENT base fee rather than only scaling the old one. A stuck
    # transaction is usually stuck because the base fee climbed past its cap, and a bump computed
    # from that stale cap can land right back underneath it -- burning an attempt for nothing.
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    tx["maxPriorityFeePerGas"] = tip
    tx["maxFeePerGas"] = max(_bumped(tx["maxFeePerGas"]), 2 * base_fee + tip)


def _await_receipt(w3: Web3, tx_hashes: list[HexBytes], timeout: float) -> TxReceipt | None:
    """
    Polls every hash broadcast for this nonce until one has a receipt or `timeout` elapses.

    All of them are checked, not just the newest: a replacement races the transaction it replaces,
    and either can be the one that gets mined. Returns None on timeout so the caller can bump.
    """
    deadline = time.time() + timeout
    while True:
        for tx_hash in tx_hashes:
            try:
                return w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                continue
        if time.time() >= deadline:
            return None
        time.sleep(RECEIPT_POLL_INTERVAL_SECS)


def send_and_confirm(
    w3: Web3,
    chain_name: str,
    account: LocalAccount,
    tx: dict[str, Any],
    send_w3: Web3 | None = None,
) -> TxReceipt:
    """
    Broadcasts `tx` and returns its receipt, replacing it at a higher fee if it stalls.

    web3's wait_for_transaction_receipt only waits. A transaction whose fee cap the base fee has
    since overtaken will never be mined however long it is waited on, so an unbounded wait hangs
    the calling thread for good -- and the caller is a user request. Here each attempt gets
    ATTEMPT_TIMEOUT_SECS, after which the transaction replaces itself at its own nonce with
    bumped fees, up to MAX_FEE_BUMPS times.

    Only the OUTER transaction is re-signed, with the account key held here. Any ERC-4337 UserOp
    in its calldata is untouched and its session-key signature stays valid, because the EntryPoint
    prices reimbursement purely from the UserOp's own gas fields -- `tx.gasprice` never enters
    that calculation. The gap between the two is the bundler's own subsidy: bumping buys inclusion
    with gas the account will not repay, which is the intended trade at these amounts.

    @param w3          Web3 connection for the target network.
    @param chain_name  Network name, forwarded to send_tx for nonce bookkeeping.
    @param account     Local signing account. Must match the transaction's "from".
    @param tx          Transaction dict, fully populated apart from "nonce". EIP-1559 fee fields
                       are bumped when present, otherwise a legacy "gasPrice" is.
    @param send_w3     Optional private RPC to broadcast (and re-broadcast) through; see send_tx.
                       Receipts are always polled from `w3`.
    @return            The mined transaction receipt, whichever broadcast won.
    @raises TimeoutError if no attempt is mined. The nonce stays allocated on purpose: the
            transactions are still live in the mempool and one may yet be included, so rolling
            the counter back would hand the same nonce out twice.
    """
    tx_hashes = [send_tx(w3, chain_name, account, tx, send_w3)]
    rebroadcast_error: Exception | None = None

    for attempt in range(MAX_FEE_BUMPS + 1):
        receipt = _await_receipt(w3, tx_hashes, ATTEMPT_TIMEOUT_SECS)
        if receipt is not None:
            return receipt
        if attempt == MAX_FEE_BUMPS:
            break

        _bump_fees(w3, tx)
        print(
            f"[tx_sender] nonce {tx['nonce']} unconfirmed after {ATTEMPT_TIMEOUT_SECS}s — "
            f"replacing at a higher fee (bump {attempt + 1}/{MAX_FEE_BUMPS})"
        )
        try:
            tx_hashes.append(_broadcast(send_w3 or w3, account, tx))
        except Exception as exc:
            # "already known" and "nonce too low" both mean a transaction we already sent is
            # pending or mined, so the hashes in hand are still the right things to poll. A
            # genuine error (funds too low for the raised cap) is kept for the timeout message
            # rather than raised here, since an earlier broadcast may still land.
            rebroadcast_error = exc

    raise TimeoutError(
        f"transaction from {account.address} at nonce {tx['nonce']} was not mined after "
        f"{MAX_FEE_BUMPS + 1} attempts over ~{(MAX_FEE_BUMPS + 1) * ATTEMPT_TIMEOUT_SECS}s "
        f"(hashes: {', '.join(h.hex() for h in tx_hashes)})"
        + (f"; last replacement failed: {rebroadcast_error}" if rebroadcast_error else "")
        + ". The nonce stays held — later sends from this key queue behind it until one is mined."
    )
