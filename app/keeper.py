"""
The keeper: pays each recurring payment when it falls due.

A recurring payment is a schedule the wallet's OWNER created on the wallet's RecurringPaymentModule.
Nothing on chain runs by itself, so this process calls the module's run(wallet, id) for every
schedule that is due. The module checks the schedule and calls the wallet, which runs its session
guard, charges its fee -- the protocol fee plus the registry's KEEPER fee, which pays back the gas
this process spends -- and sends the payment through the spending-limit hook.

The keeper chooses nothing but the moment: anyone may call run(), and it pays only what the owner's
schedule says, once per period. So its key, KEEPER_KEY, can move no user's funds; it only pays gas.
Fund it on every chain it serves (`make fund` does on a fork).

    make keeper                  # every chain with a deployed protocol and a reachable RPC
    make keeper ARGS=--once      # one pass and exit (what the end-to-end test drives)

Run ONE copy: tx_sender counts this key's nonces in this process. A second copy would hand out
the same nonces, and its transactions would replace this one's.

Each pass, per chain:
  1. Every few minutes, look for schedules created outside the app (a block explorer): read each
     wallet's scheduleCount and copy any new ones into wallet.db (recurring.sync_wallet).
  2. For every live schedule wallet.db says is due, read it on chain again, simulate run(), and send
     it if it would go through. A refusal (over the limit, paused, not enough funds) is recorded
     with its reason, the owner is told once on Telegram, and the keeper tries again later, waiting
     longer each time (RETRY_DELAYS_SECS). A refused payment is not lost: it stays due, and the
     next run pays one instalment -- never a backlog (the module doesn't catch up).

"Due" is judged by the chain's latest block time, not this machine's clock: that is the time the
module checks, and on a local fork the two differ.
"""
import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

load_dotenv()

import recurring  # noqa: E402
import telegram_notify  # noqa: E402
import tx_sender  # noqa: E402
from bundler import _revert_data  # noqa: E402
from contract_errors import describe_revert_data, name_revert  # noqa: E402
from db import (  # noqa: E402
    add_transaction,
    get_due_schedules,
    get_factory_address,
    get_schedules,
    get_user_by_id,
    get_wallets_on_chain,
    init_db,
    record_failure,
    record_payment,
)
from network_config import CHAIN_NAME_BY_ID, load_network_config_by_name, network_name  # noqa: E402
from redact import install_log_redaction, redact  # noqa: E402
from tx_movements import OUT, Moved  # noqa: E402

log = logging.getLogger("keeper")

KEEPER_KEY_ENV = "KEEPER_KEY"
# Seconds between passes. A payment is due for a whole period (an hour at the shortest), so a minute
# late is nothing; the pass itself is a handful of reads per due schedule.
POLL_SECS = int(os.getenv("KEEPER_POLL_SECS", "60"))
# Seconds between looks for schedules made outside the app: one read per wallet on each chain.
SYNC_SECS = int(os.getenv("KEEPER_SYNC_SECS", "600"))
# How long to wait after a refusal before trying again, by how many refusals in a row: five minutes,
# doubling, at most six hours. The owner fixes most refusals themselves (tops up, unpauses, raises
# the limit), so retrying keeps the payment as close to on time as their fix allows.
RETRY_DELAYS_SECS = (300, 600, 1_200, 2_400, 4_800, 9_600, 19_200, 21_600)
# Gas estimate headroom: the hook's work depends on prices and balances that can move between the
# estimate and the block.
GAS_BUFFER = 1.25
# Below this many wei on a chain, every pass warns that the keeper is about to stop paying there.
LOW_BALANCE_WEI = Web3.to_wei(0.005, "ether")


def keeper_account() -> LocalAccount:
    """The keeper's key, from KEEPER_KEY. A plain EOA that only ever pays gas."""
    key = os.getenv(KEEPER_KEY_ENV)
    if not key:
        raise RuntimeError(f"{KEEPER_KEY_ENV} is not set -- the keeper has no key to send payments with.")
    return Account.from_key(key)


def served_chains() -> list[int]:
    """Every chain this deployment serves that has the protocol deployed (a factory row)."""
    chains = []
    for chain_id in sorted(CHAIN_NAME_BY_ID):
        try:
            get_factory_address(chain_id)
        except ValueError:
            continue
        chains.append(chain_id)
    return chains


def _web3(chain_id: int) -> tuple[Web3, str]:
    """(Web3, network name) for a chain, checking the RPC really is that chain."""
    chain_name = network_name(chain_id)
    w3, _ = load_network_config_by_name(chain_name)
    if w3.eth.chain_id != chain_id:
        raise RuntimeError(f"the RPC for {chain_name} answers as chain {w3.eth.chain_id}")
    return w3, chain_name


def _reason(exc: Exception) -> str:
    """Why a simulated or sent run() failed, in the contract's own words where it gave any."""
    data = _revert_data(exc)
    if data:
        return describe_revert_data(data)
    text = redact(str(exc).split("\n")[0].strip()) or exc.__class__.__name__
    return name_revert(text) or text


def _retry_delay(failures: int) -> int:
    return RETRY_DELAYS_SECS[min(failures, len(RETRY_DELAYS_SECS)) - 1]


def _tell_owner(user_id: int, chain_id: int, text: str):
    """Best effort: a Telegram message to the owner, if they linked a chat. Never raises."""
    try:
        user = get_user_by_id(user_id)
        if user and user.get("telegram_chat_id"):
            telegram_notify.send(user["telegram_chat_id"], chain_id, text)
    except Exception:  # noqa: BLE001 -- the payment is already decided; a message is a courtesy
        log.warning("Could not tell user %s on Telegram", user_id)


# ── One schedule ────────────────────────────────────────────────────────────────


def pay(w3: Web3, chain_name: str, chain_id: int, account: LocalAccount, row: dict, now: int) -> str:
    """
    Tries to pay one due schedule. Re-reads it on chain first, since wallet.db may be behind (the
    owner cancelled it from a block explorer, someone else ran it).

    @return  "paid", "refused", "not_due" or "gone".
    """
    module = recurring.module_contract(w3, row["module"])
    wallet, schedule_id, user_id = row["wallet"], row["schedule_id"], row["user_id"]
    terms, live = recurring.read_schedule(module, wallet, schedule_id)
    saved = recurring.save_terms(user_id, chain_id, wallet, module.address, schedule_id, terms, live)
    if not live:
        return "gone"
    if terms["next_due"] > now:
        return "not_due"

    described = recurring.describe(w3, user_id, chain_id, saved)
    fn = module.functions.run(wallet, schedule_id)
    try:
        fn.call({"from": account.address})
        tx = fn.build_transaction({"from": account.address, "chainId": chain_id})
    except Exception as exc:  # noqa: BLE001 -- a revert, named; anything else is reported the same way
        return _refused(chain_id, saved, described, _reason(exc), now)

    tx["gas"] = int(tx["gas"] * GAS_BUFFER)
    receipt = tx_sender.send_and_confirm(w3, chain_name, account, tx)
    tx_hash = Web3.to_hex(receipt["transactionHash"])
    if receipt["status"] != 1:
        return _refused(chain_id, saved, described, "the payment reverted when it was sent", now)

    mined_at = w3.eth.get_block(receipt["blockNumber"])["timestamp"]
    executed = recurring.events_in(module, receipt)["RecurringPaymentExecuted"]
    next_due = executed[0]["args"]["nextDue"] if executed else 0
    record_payment(saved["id"], tx_hash, mined_at, next_due or None)

    token = None if int(terms["token"], 16) == 0 else terms["token"]
    add_transaction(
        user_id, chain_id, wallet, "recurring",
        f"Recurring payment: {described['amount_text']} {described['ticker']} to {described['recipient_name']}",
        "confirmed", tx_hash=tx_hash, mined_at=mined_at, direction=OUT,
        movements=[Moved(OUT, token, described["ticker"], terms["amount"], described["decimals"], terms["recipient"])],
    )
    when = (
        f" Next payment {time.strftime('%d %b %Y, %H:%M UTC', time.gmtime(next_due))}." if next_due
        else " That was the last one."
    )
    _tell_owner(user_id, chain_id, f"Paid {described['amount_text']} {described['ticker']} to "
                                   f"{described['recipient_name']} (recurring payment).{when}")
    log.info("chain %s: paid schedule %s of %s in %s", chain_id, schedule_id, wallet, tx_hash)
    return "paid"


def _refused(chain_id: int, row: dict, described: dict, reason: str, now: int) -> str:
    """Records a refusal and when to try again; tells the owner on the first of a run of them."""
    failures = (row.get("failures") or 0) + 1
    record_failure(row["id"], reason, now + _retry_delay(failures))
    if failures == 1:
        _tell_owner(
            row["user_id"], chain_id,
            f"The recurring payment of {described['amount_text']} {described['ticker']} to "
            f"{described['recipient_name']} couldn't go through: {reason}. It will be tried again; "
            "you can see it, and cancel it, under Controls in the web app.",
        )
    log.info("chain %s: schedule %s of %s refused: %s", chain_id, row["schedule_id"], row["wallet"], reason)
    return "refused"


# ── One chain ───────────────────────────────────────────────────────────────────


def sync_chain(w3: Web3, chain_id: int) -> int:
    """
    Copies into wallet.db any schedule on `chain_id` the app hasn't seen: one made outside the app.
    One read per wallet, plus the new schedules' own.

    @return  How many wallets had schedules read.
    """
    synced = 0
    for user_id, wallet in get_wallets_on_chain(chain_id):
        try:
            contract = w3.eth.contract(address=Web3.to_checksum_address(wallet),
                                       abi=recurring.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
            if not w3.eth.get_code(contract.address):
                continue  # a stale row: a fork restarted since
            module = recurring.module_for_wallet(w3, chain_id, contract)
            if module is None:
                continue
            known = {r["schedule_id"] for r in get_schedules(user_id, chain_id) if r["wallet"] == contract.address}
            # Ids only ever grow, so a count past the highest known id means new schedules. Live ones
            # already known are read again anyway when they fall due (pay).
            if module.functions.scheduleCount(contract.address).call() > max(known, default=0):
                recurring.sync_wallet(user_id, chain_id, contract.address, module)
                synced += 1
        except Exception as exc:  # noqa: BLE001 -- one wallet must not stop the others
            log.warning("chain %s: could not read the schedules of %s: %s", chain_id, wallet, redact(exc))
    return synced


def run_once(chain_id: int, account: LocalAccount, sync: bool = False) -> dict[str, int]:
    """
    One pass over one chain: optionally look for new schedules, then pay every due one.

    @return  How many schedules came out each way: {"paid", "refused", "not_due", "gone", "error"}.
    """
    w3, chain_name = _web3(chain_id)
    if sync:
        sync_chain(w3, chain_id)
    outcome = {"paid": 0, "refused": 0, "not_due": 0, "gone": 0, "error": 0}
    now = w3.eth.get_block("latest")["timestamp"]
    due = get_due_schedules(chain_id, now)
    if due and w3.eth.get_balance(account.address) < LOW_BALANCE_WEI:
        log.warning("chain %s: the keeper %s is low on gas money", chain_id, account.address)
    for row in due:
        try:
            outcome[pay(w3, chain_name, chain_id, account, row, now)] += 1
        except Exception as exc:  # noqa: BLE001 -- an RPC failure or a stuck send: try the next one
            outcome["error"] += 1
            log.warning("chain %s: schedule %s of %s: %s", chain_id, row["schedule_id"], row["wallet"], redact(exc))
    return outcome


def main():
    parser = argparse.ArgumentParser(description="Pays recurring payments when they fall due.")
    parser.add_argument("--once", action="store_true", help="one pass over every chain, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    install_log_redaction()
    init_db()
    account = keeper_account()
    log.info("keeper %s", account.address)

    last_sync: dict[int, float] = {}
    # Why each chain was last skipped, so a chain that stays down (a fork not running) is reported
    # once, not every pass.
    skipped: dict[int, str] = {}
    while True:
        for chain_id in served_chains():
            sync = time.monotonic() - last_sync.get(chain_id, float("-inf")) >= SYNC_SECS
            try:
                outcome = run_once(chain_id, account, sync=sync)
            except Exception as exc:  # noqa: BLE001 -- a chain whose RPC is down waits for the next pass
                reason = redact(exc)
                if skipped.get(chain_id) != reason:
                    log.warning("chain %s skipped: %s", chain_id, reason)
                skipped[chain_id] = reason
                continue
            if chain_id in skipped:
                log.info("chain %s answering again", chain_id)
                del skipped[chain_id]
            if sync:
                last_sync[chain_id] = time.monotonic()
            if any(outcome[k] for k in ("paid", "refused", "error")):
                log.info("chain %s: %s", chain_id, outcome)
        if args.once:
            return
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    sys.exit(main())
