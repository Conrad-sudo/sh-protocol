"""
End-to-end API tests against a running local fork -- Sepolia unless another is named.

This covers the half of the API that no offline test can reach: the transactions themselves, and
the eth_call simulations that are the entire reason the prepare endpoints exist. A signed-in user
is walked through the real journey -- sign up, prove an address with SIWE, deploy a wallet, then
drive every owner action -- with each on-chain effect asserted afterwards. The agent's side is
covered too: ERC20 transfers approved with the account's passkey and sent through the self-bundler,
with what each one really cost written to COST_REPORT_PATH. The passkey is a soft one
(passkey.SoftPasskey), made through the API as the web app makes one, and approving as a phone does.

A brand-new account is used rather than the harness's user 1, because the deploy endpoints can only
be covered by a wallet this API actually created.

Requires (see the plan, Phase 7):
    make sepolia-fork                    (or make arbitrum-fork, ...; started with --hardfork osaka)
    make setup-test ARGS=sepolia-fork    (the same fork name)

Run: make e2e-test [ARGS=arbitrum-fork]   (or: python app/tests/test_e2e_fork.py [arbitrum-fork])
"""
import itertools
import json
import math
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

from dotenv import load_dotenv

from checks import add_contact, check, finish, sign_in   # first: it puts app/ on sys.path for the imports below
import db  # noqa: E402

load_dotenv()
os.environ["COOKIE_SECURE"] = "0"

from eth_account import Account                       # noqa: E402
from eth_utils import keccak                          # noqa: E402
from fastapi.testclient import TestClient             # noqa: E402
from web3 import Web3                                 # noqa: E402

import api                                            # noqa: E402
import routes.catalog  # noqa: E402
import routes.deps  # noqa: E402
from abi import ientry_point                          # noqa: E402
import bundler                                        # noqa: E402
import smart_wallet_agent                             # noqa: E402
import tools                                          # noqa: E402
import tools.checks  # noqa: E402
import tools.erc8004  # noqa: E402
import tools.positions  # noqa: E402
import tools.sending  # noqa: E402
import tools.transfers  # noqa: E402
import tools.uniswap  # noqa: E402
import tools.wallet_reads  # noqa: E402
from agent_context import AgentContext                # noqa: E402
from constants import (  # noqa: E402
    ETH_SENTINEL, get_always_counted_ticker, get_chain_display_name, get_default_spenders, get_exchange,
    get_native_asset_ticker, get_native_wrapped_ticker, get_router, get_v3_network,
)
from contracts import load_registry, read_spending_config  # noqa: E402
from db import acting_network, get_rpc_url, get_sign_request, get_token_address  # noqa: E402
from langchain_core.tools import ToolException        # noqa: E402
from langchain_erc20 import ERC20_ABI                 # noqa: E402
from langchain_erc8004.abis import REPUTATION_REGISTRY_ABI  # noqa: E402
from langchain_uniswap_v2.abis import factory_abi, pair_abi, router_abi  # noqa: E402
from langchain_uniswap_v3 import v3math               # noqa: E402
from langchain_uniswap_v3.abis import position_manager_abi  # noqa: E402
import quotes                                         # noqa: E402
import tx_movements                                   # noqa: E402
from userop import build_execute_calldata             # noqa: E402
import passkey                                       # noqa: E402
import signing                                       # noqa: E402
from passkey import SoftPasskey                      # noqa: E402
from webauthn.helpers import base64url_to_bytes      # noqa: E402
from web3.exceptions import ContractLogicError        # noqa: E402
from web3.logs import DISCARD                         # noqa: E402

# Where test_self_bundling writes the measured cost of each transfer, for reporting.
COST_REPORT_PATH = os.getenv("E2E_COST_REPORT_PATH", "/tmp/e2e_cost_report.json")
# The fork under test, named as `make setup-test ARGS=...` names it. Requests speak its chain ID,
# which is the live chain's: a fork reports its parent's id.
FORK_CHAIN_IDS = {f"{name}-fork": cid for cid, name in routes.catalog.CHAIN_NAME_BY_ID.items() if cid in routes.catalog.FORKABLE_CHAIN_IDS}
NETWORK = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "sepolia-fork"
if NETWORK not in FORK_CHAIN_IDS:
    raise SystemExit(f"Unknown fork '{NETWORK}'. One of: {sorted(FORK_CHAIN_IDS)}")
CHAIN_ID = FORK_CHAIN_IDS[NETWORK]
# The wrapped native coin: WETH on the Ethereum chains, WBNB on BSC (where "weth" is bridged ETH).
WRAPPED = get_native_wrapped_ticker(CHAIN_ID)
# The same rpcs row the API reads, so the test and the API always talk to the same node: each fork
# runs on its own port (the Makefile's FORK_PORT_*).
RPC = get_rpc_url(NETWORK)
if RPC is None:
    raise SystemExit(f"No rpcs row for '{NETWORK}'. Run: make db")
w3 = Web3(Web3.HTTPProvider(RPC))


def require_local_fork():
    """
    Refuses to run anywhere but a local fork.

    This is a real safety gate, not a formality. A fork and its live chain report the same chain id
    (11155111 for Sepolia), so nothing else in the stack can tell them apart -- if APP_FORK_MODE were
    0, every transaction below would be broadcast to the real network with a real funded key.
    anvil_setBalance exists only on a local node, so a successful call is positive proof of where we are.
    """
    if not w3.is_connected():
        raise SystemExit(f"No node at {RPC}. Run: make {NETWORK}")
    if w3.eth.chain_id != CHAIN_ID:
        raise SystemExit(f"Node reports chain {w3.eth.chain_id}, expected {CHAIN_ID} ({NETWORK}).")
    probe = Account.create().address
    try:
        w3.provider.make_request("anvil_setBalance", [probe, hex(10**18)])
        assert w3.eth.get_balance(probe) == 10**18
    except Exception as e:
        raise SystemExit(
            f"anvil_setBalance failed ({e}). This does not look like a local fork — refusing to "
            f"sign anything. Check APP_FORK_MODE=1 and that `make {NETWORK}` is what is on {RPC}."
        )
    if not routes.catalog.FORK_MODE:
        raise SystemExit("APP_FORK_MODE is not set; the API would target the live chain. Refusing.")
    print(f"  local fork confirmed: {NETWORK}, chain {CHAIN_ID} at block {w3.eth.block_number}")


def make_client() -> TestClient:
    """A client with the agent lifespan stubbed out (the agent is not under test here)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop(_app):
        yield

    api.app.router.lifespan_context = _noop
    api.limiter.enabled = False
    api.limiter.reset()
    return TestClient(api.app)


def new_funded_account(eth: int = 100):
    """
    Creates a fresh EOA and funds it on the fork.

    Fresh rather than a stock anvil account for two reasons: on a fork those well-known addresses
    carry inherited EIP-7702 delegation code, and a new owner keeps deployCount clear of
    every other test's, so nobody's CREATE2 prediction moves under them.
    """
    acct = Account.create()
    w3.provider.make_request("anvil_setBalance", [acct.address, hex(eth * 10**18)])
    return acct


def sign_and_send(acct, tx: dict) -> str:
    """Signs a prepared transaction with `acct` and broadcasts it, returning the hash."""
    payload = {
        "to": Web3.to_checksum_address(tx["to"]),
        "data": tx["data"],
        "value": int(tx.get("value", "0x0"), 16),
        "nonce": int(tx["nonce"], 16),
        "chainId": int(tx["chainId"], 16),
        "gas": int(tx["gas"], 16),
    }
    if "maxFeePerGas" in tx:
        payload["maxFeePerGas"] = int(tx["maxFeePerGas"], 16)
        payload["maxPriorityFeePerGas"] = int(tx["maxPriorityFeePerGas"], 16)
    else:
        payload["gasPrice"] = int(tx["gasPrice"], 16)
    signed = acct.sign_transaction(payload)
    return w3.eth.send_raw_transaction(signed.raw_transaction).hex()


def sign_in_as(c: TestClient, acct) -> dict:
    """Signs in as `acct` through the real SIWE endpoint -- the only way in -- and returns its headers."""
    _, headers, _ = sign_in(c, acct, CHAIN_ID)
    return headers


# The passkey each account in this run approves with, by user id: made through the API as the web
# app makes one (register_passkey), and kept here as a phone keeps it.
PASSKEYS: dict[int, SoftPasskey] = {}


def register_passkey(c: TestClient, headers: dict, name: str = "E2E phone", primary: bool = True) -> SoftPasskey:
    """
    Makes a passkey for the signed-in account the way the web app does: the API's options, a
    passkey's answer to them, and POST /api/passkeys/register, which py_webauthn checks for real.

    @param primary  Whether it is the one the account approves its quotes with (PASSKEYS).
    """
    key = SoftPasskey(rp_id=passkey.rp_id(), origin=passkey.allowed_origins()[0])
    r = c.post("/api/passkeys/register/options", headers=headers)
    assert r.status_code == 200, f"passkey options: {r.status_code} {r.text[:200]}"
    challenge = base64url_to_bytes(r.json()["options"]["challenge"])
    r = c.post("/api/passkeys/register", headers=headers,
               json={"credential": key.registration_response(challenge), "device_name": name})
    assert r.status_code == 200, f"passkey register: {r.status_code} {r.text[:200]}"
    if primary:
        PASSKEYS[c.get("/api/me", headers=headers).json()["user_id"]] = key
    return key


def approve_quote(user_id: int, quote_id: str):
    """
    What a quote's card and the user's phone do before the send: open its sign request, build the
    op (signing.start: fresh fees, a nonce carrying the op's deadline), approve the op's hash with
    the account's passkey, and check the approval as the API does (signing.approve: py_webauthn,
    then the wallet's own checks), which claims the request.

    @return  signing.Approved, for tools.sending.submit_signed.
    @raises ToolException  If there is no such quote waiting, or the approval is refused.
    """
    pending = quotes._pending.get(quote_id)
    offered = signing.open_request(pending, signing.WEB) if pending is not None else None
    if offered is None:
        raise ToolException(f"There is no pending transaction {quote_id} to approve.")
    try:
        started = signing.start(get_sign_request(offered.request["id"]))
        credential = PASSKEYS[user_id].assertion(started.challenge)
        approved = signing.approve(signing.find_by_sign_id(started.sign_id), credential)
    except (signing.Expired, signing.NoPasskey, signing.Rejected, signing.WouldFail) as e:
        raise ToolException(f"{type(e).__name__}: {e}")
    quotes.drop(user_id, quote_id)
    return approved


def send_approved(approved):
    """The send half: on the wire through the bundler's lanes (tools.sending.submit_signed)."""
    request = approved.request
    with acting_network(request["user_id"], NETWORK, CHAIN_ID):
        return tools.sending.submit_signed(approved)


def approve_and_send(user_id: int, quote_id: str) -> str:
    """
    A quote approved with the account's passkey (approve_quote), sent, and waited for.

    @return  What the user is told once it lands: "Sent — <action>. Tx hash: `0x…`", and what is
             left of the limit.
    @raises ToolException  If there is no such quote waiting, or it would fail, or it failed.
    """
    return tools.sending.settle_quoted(send_approved(approve_quote(user_id, quote_id)))


def passkey_op(user_id: int, executions: list[tuple[str, int, bytes]], account) -> tuple:
    """
    One op from the user's wallet, priced, built and approved by the account's passkey, but not
    sent: the bundler's own steps, for a test that drives the bundle itself.

    @return  (the prepared op, the EntryPoint) -- bundler.PreparedUserOp, ready for a lane.
    """
    with acting_network(user_id, NETWORK, CHAIN_ID):
        session_handler, entry_point, calldata = build_execute_calldata(user_id, executions)
        quote = bundler.quote_user_op(user_id, session_handler, entry_point, calldata, account)
        final = bundler.finalize_user_op(user_id, session_handler, entry_point, quote, account, int(time.time()) + 600)
        signature = PASSKEYS[user_id].contract_signature(final.user_op_hash)
        return bundler.check_signed_user_op(user_id, entry_point, quote, final, signature, account), entry_point


def owner_action(c: TestClient, headers: dict, acct, path: str, body: dict) -> dict:
    """prepare -> sign -> broadcast -> confirm, asserting each hop. Returns the confirm payload."""
    r = c.post(path, headers=headers, json={"chain_id": CHAIN_ID, **body})
    assert r.status_code == 200, f"{path} prepare failed: {r.status_code} {r.text[:250]}"
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    r = c.post(
        "/api/wallet/tx/confirm", headers=headers, json={"chain_id": CHAIN_ID, "tx_hash": tx_hash}
    )
    assert r.status_code == 200, f"{path} confirm failed: {r.status_code} {r.text[:250]}"
    return r.json()


def test_deploy_round_trip(c: TestClient, acct, headers: dict) -> str:
    """
    The real onboarding path: make a passkey, prepare the deploy approving it, sign it, confirm it,
    verify it on chain.
    """
    print("\n[1] deploy: passkey -> prepare -> sign -> confirm")
    phone = register_passkey(c, headers)
    body = {
        "chain_id": CHAIN_ID,
        "deployer": acct.address,
        "daily_limit_usd": 50_000,
        # Nothing picked on purpose: the API adds WETH regardless, since it always counts like ETH.
        "watched_tokens": [],
        "prefund_eth": "1",
        "key_id": phone.key_id,
    }

    # A browser wallet may replace the API's gas estimate with a lower limit of its own. The deploy
    # is then mined but runs out of gas, and confirm must say that rather than a bare "reverted".
    # It leaves deployCount alone, so the real deploy below still lands at its prediction.
    r = c.post("/api/deploy", headers=headers, json=body)
    starved = {**r.json()["tx"], "gas": hex(int(r.json()["tx"]["gas"], 16) * 2 // 3)}
    tx_hash = sign_and_send(acct, starved)
    check("a deploy sent with too little gas is mined and fails",
          w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)["status"] == 0)
    r = c.post(
        "/api/deploy/confirm",
        headers=headers,
        json={**{k: body[k] for k in ("chain_id", "deployer")}, "tx_hash": tx_hash,
              "predicted_address": r.json()["predicted_address"]},
    )
    check("confirm reports it as out of gas, with the limit it needed",
          r.status_code == 400 and "ran out of gas" in r.json()["detail"], f"{r.status_code} {r.text[:250]}")

    r = c.post("/api/deploy", headers=headers, json=body)
    check("deploy prepares", r.status_code == 200, f"{r.status_code} {r.text[:250]}")
    if r.status_code != 200:
        raise SystemExit("cannot continue without a wallet")
    prepared = r.json()
    predicted = prepared["predicted_address"]
    check("the deploy approves the passkey it was asked to", prepared["key_id"] == phone.key_id, str(prepared.get("key_id")))
    weth = Web3.to_checksum_address(get_token_address(CHAIN_ID, WRAPPED))
    check("WETH is watched though the request picked nothing", prepared["watched_tokens"] == [weth],
          str(prepared["watched_tokens"]))

    tx_hash = sign_and_send(acct, prepared["tx"])
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    check("the deploy transaction succeeds", receipt["status"] == 1)

    r = c.post(
        "/api/deploy/confirm",
        headers=headers,
        json={
            "chain_id": CHAIN_ID,
            "deployer": acct.address,
            "tx_hash": tx_hash,
            "predicted_address": predicted,
        },
    )
    check("confirm returns 200", r.status_code == 200, f"{r.status_code} {r.text[:250]}")
    confirmed = r.json()
    wallet = confirmed["wallet_address"]

    check("the wallet landed at the predicted address", wallet == predicted, f"{wallet} vs {predicted}")
    check("the passkey is approved ON CHAIN", confirmed["key_id"] == phone.key_id
          and confirmed["session_key_authorized"] is True and confirmed["session_key_expires_at"] > time.time(),
          str(confirmed))
    check("the wallet holds the prefund", w3.eth.get_balance(wallet) == 10**18,
          str(w3.eth.get_balance(wallet)))

    abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    on_chain = w3.eth.contract(address=wallet, abi=abi)
    check("the owner is the signer", on_chain.functions.owner().call() == acct.address)
    key_ids, _ = on_chain.functions.getSessions().call()
    check("getSessions lists exactly that passkey", ["0x" + bytes(k).hex() for k in key_ids] == [phone.key_id],
          str(key_ids))
    check("the seeded key carries a live deadline",
          on_chain.functions.isSessionActive(bytes.fromhex(phone.key_id[2:])).call() is True)
    return wallet


def test_wallet_state_read(c: TestClient, headers: dict, acct, wallet: str):
    """
    GET /api/wallet/{chain_id} -- the read half a dashboard runs on.

    Deliberately asserted against the CHAIN rather than against the values sent to /api/deploy: a
    handler that echoed its own inputs back would pass the second way and be useless. Runs before
    the owner actions, so the numbers here are a freshly deployed wallet's.
    """
    print("\n[2] wallet state reads back from chain")

    r = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers)
    check("state reads", r.status_code == 200, f"{r.status_code} {r.text[:250]}")
    if r.status_code != 200:
        return
    state = r.json()

    abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    on_chain = w3.eth.contract(address=wallet, abi=abi)
    cfg = read_spending_config(on_chain)

    check("the address is this account's wallet", state["address"] == wallet, state["address"])
    check("the owner matches chain", state["owner"] == on_chain.functions.owner().call())
    check("is_owner true for the SIWE-bound deployer", state["is_owner"] is True)
    check("not paused", state["paused"] is False)
    check("the cap matches chain", state["spending"]["daily_limit_usd"] == cfg["dailyLimitUsd"] / routes.deps.USD_DECIMALS,
          f'{state["spending"]["daily_limit_usd"]} vs {cfg["dailyLimitUsd"] / routes.deps.USD_DECIMALS}')
    check("nothing spent yet", state["spending"]["spent_usd"] == 0.0, str(state["spending"]["spent_usd"]))
    check("remaining matches getRemainingBudget",
          state["spending"]["remaining_usd"] == on_chain.functions.getRemainingBudget().call() / routes.deps.USD_DECIMALS)
    check("weth is watched and named, not a bare address",
          [t["ticker"] for t in state["spending"]["watched_tokens"]] == [WRAPPED],
          str(state["spending"]["watched_tokens"]))
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    check("the wallet's one passkey is listed, active, under its device's name",
          [(k["key_id"], k["device_name"], k["active"]) for k in state["session"]["keys"]]
          == [(PASSKEYS[user_id].key_id, "E2E phone", True)] and state["session"]["max_keys"] == 5,
          str(state["session"]))
    check("the gas ceiling is a string, not a float64-mangled int",
          isinstance(state["limits"]["max_op_gas_cost_wei"], str))
    gas_budget = state["limits"]["session_gas"] or {}
    check("the daily fee budget is the wallet's, whole: the assistant has paid no fees yet",
          gas_budget.get("budget_wei") == str(on_chain.functions.sessionGasBudget().call())
          and gas_budget.get("remaining_wei") == gas_budget.get("budget_wei") and gas_budget.get("refills_at") is None,
          str(gas_budget))
    # SwapRouter02 and the position manager on a Uniswap V3 chain, PancakeSwap's router on BSC.
    check("the exchange's contracts were seeded as trusted spenders",
          sorted(state["limits"]["trusted_spenders"]) == sorted(get_default_spenders(CHAIN_ID)),
          f'{state["limits"]["trusted_spenders"]} vs {get_default_spenders(CHAIN_ID)}')
    check("a new wallet holds no Uniswap V3 positions", state["positions"] == [], str(state.get("positions")))

    native = [b for b in state["balances"] if b["native"]][0]
    check("the native balance matches chain",
          native["raw"] == str(w3.eth.get_balance(wallet)), native["raw"])
    check("the dashboard shows only what the new wallet counts: the native token and the wrapped one",
          [b["ticker"] for b in state["balances"]] == [get_native_asset_ticker(CHAIN_ID).lower(), get_always_counted_ticker(CHAIN_ID)],
          str([b["ticker"] for b in state["balances"]]))
    check("no balance errored", not [b for b in state["balances"] if b.get("error")],
          str([b for b in state["balances"] if b.get("error")]))

    # A second account must not read the first's wallet: it has no wallet on this chain, so the
    # lookup is keyed by the CALLER and 404s rather than falling through to somebody else's row.
    other = new_funded_account()
    other_headers = sign_in_as(c, other)
    check("another account gets 404, not someone else's wallet",
          c.get(f"/api/wallet/{CHAIN_ID}", headers=other_headers).status_code == 404)
    check("reading needs a token", c.get(f"/api/wallet/{CHAIN_ID}").status_code == 401)


def test_dashboard_tokens(c: TestClient, acct, headers: dict, wallet: str):
    """
    The dashboard list against the real chain: a listed token added by address shows up, is counted
    by a real owner transaction, can't come off while it counts, and comes off once it stops.
    Leaves USDC as it found it -- uncounted and off the dashboard -- for the tests after it.
    """
    print("\n[2b] dashboard tokens: add a listed token, count it, stop counting, remove it")
    abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    sh = w3.eth.contract(address=wallet, abi=abi)
    usdc = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))

    def shown() -> list[str]:
        balances = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers).json()["balances"]
        return [b["ticker"] for b in balances if not b["native"]]

    check("USDC isn't shown to begin with", "usdc" not in shown(), str(shown()))
    r = c.post("/api/tokens/custom/lookup", headers=headers, json={"chain_id": CHAIN_ID, "address": usdc.lower()})
    check("a listed token's address is accepted, as the listed token",
          r.status_code == 200 and (r.json()["listed"], r.json()["ticker"]) == (True, "usdc"), r.text[:200])
    r = c.post("/api/tokens/custom", headers=headers, json={"chain_id": CHAIN_ID, "address": usdc})
    check("adding it -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    check("it shows on the dashboard", "usdc" in shown(), str(shown()))

    owner_action(c, headers, acct, "/api/wallet/watched-tokens/prepare", {"token": "usdc", "action": "add"})
    check("it counts on chain", sh.functions.isWatched(usdc).call() is True)
    r = c.delete(f"/api/tokens/custom/{CHAIN_ID}/{usdc}", headers=headers)
    check("removing it while it counts -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    check("…and it stays", "usdc" in shown(), str(shown()))

    owner_action(c, headers, acct, "/api/wallet/watched-tokens/prepare", {"token": "usdc", "action": "remove"})
    check("it no longer counts on chain", sh.functions.isWatched(usdc).call() is False)
    check("stopping counting doesn't take it off the dashboard", "usdc" in shown(), str(shown()))
    r = c.delete(f"/api/tokens/custom/{CHAIN_ID}/{usdc}", headers=headers)
    check("now it comes off -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("and no longer shows", "usdc" not in shown(), str(shown()))

    always = get_always_counted_ticker(CHAIN_ID)
    r = c.delete(f"/api/tokens/custom/{CHAIN_ID}/{get_token_address(CHAIN_ID, always)}", headers=headers)
    check(f"{always.upper()} can't come off -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")


def test_owner_actions(c: TestClient, acct, headers: dict, wallet: str):
    """Every owner endpoint, with the on-chain effect asserted after each."""
    print("\n[3] owner actions: each one changes real chain state")
    abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    sh = w3.eth.contract(address=wallet, abi=abi)

    owner_action(c, headers, acct, "/api/wallet/daily-limit/prepare", {"daily_limit_usd": 1234})
    check("daily limit changed on chain",
          read_spending_config(sh)["dailyLimitUsd"] == 1234 * 10**18,
          str(read_spending_config(sh)["dailyLimitUsd"]))

    owner_action(c, headers, acct, "/api/wallet/window-duration/prepare", {"window_secs": 3600})
    check("window duration changed on chain", read_spending_config(sh)["windowDuration"] == 3600)

    owner_action(c, headers, acct, "/api/wallet/max-op-gas-cost/prepare", {"max_cost_eth": "0.25"})
    check("gas ceiling changed on chain",
          sh.functions.maxOpGasCost().call() == w3.to_wei("0.25", "ether"))

    owner_action(c, headers, acct, "/api/wallet/session-gas-budget/prepare", {"budget_eth": "0.3"})
    check("daily fee budget changed on chain",
          sh.functions.sessionGasBudget().call() == w3.to_wei("0.3", "ether"))

    link = Web3.to_checksum_address(get_token_address(CHAIN_ID, "link"))
    owner_action(c, headers, acct, "/api/wallet/watched-tokens/prepare",
                 {"token": "link", "action": "add"})
    check("LINK is now watched", sh.functions.isWatched(link).call() is True)
    owner_action(c, headers, acct, "/api/wallet/watched-tokens/prepare",
                 {"token": "link", "action": "remove"})
    check("LINK is no longer watched", sh.functions.isWatched(link).call() is False)

    spender = Account.create().address
    owner_action(c, headers, acct, "/api/wallet/trusted-spenders/prepare",
                 {"spender": spender, "action": "add"})
    check("the trusted spender was added",
          spender in [Web3.to_checksum_address(a) for a in read_spending_config(sh)["trustedSpenders"]])

    # The passkeys: a second device added, the first renewed, one revoked, then every one, then the
    # phone approved again -- each read back from the wallet, which is the only record of them.
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    phone = PASSKEYS[user_id]
    laptop = register_passkey(c, headers, "E2E laptop", primary=False)

    def session_action(body: dict) -> tuple[dict, dict]:
        r = c.post("/api/wallet/session/prepare", headers=headers, json={"chain_id": CHAIN_ID, **body})
        assert r.status_code == 200, f"session/prepare {body}: {r.status_code} {r.text[:250]}"
        prepared = r.json()
        tx_hash = sign_and_send(acct, prepared["tx"])
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        r = c.post("/api/wallet/session/confirm", headers=headers, json={"chain_id": CHAIN_ID, "tx_hash": tx_hash})
        assert r.status_code == 200, f"session/confirm {body}: {r.status_code} {r.text[:250]}"
        return prepared, r.json()

    def on_wallet() -> dict[str, int]:
        ids, untils = sh.functions.getSessions().call()
        return {"0x" + bytes(k).hex(): int(u) for k, u in zip(ids, untils)}

    def refused(body: dict) -> int:
        return c.post("/api/wallet/session/prepare", headers=headers, json={"chain_id": CHAIN_ID, **body}).status_code

    prepared, confirmed = session_action({"action": "add", "key_id": laptop.key_id, "ttl_secs": 7 * 86_400})
    check("adding a second device names it, as new", prepared["key_id"] == laptop.key_id and prepared["renews"] is False,
          str({k: prepared.get(k) for k in ("key_id", "renews")}))
    check("both devices are approved on chain", set(on_wallet()) == {phone.key_id, laptop.key_id}, str(on_wallet()))
    check("the confirm reads the wallet's own list back, with the devices' names",
          confirmed["status"] == "confirmed"
          and {(k["key_id"], k["device_name"], k["active"]) for k in confirmed["session"]["keys"]}
          == {(phone.key_id, "E2E phone", True), (laptop.key_id, "E2E laptop", True)}, str(confirmed))
    before = on_wallet()[phone.key_id]
    prepared, _ = session_action({"action": "add", "key_id": phone.key_id, "ttl_secs": 60 * 86_400})
    check("adding a device the wallet already approves renews it, in place",
          prepared["renews"] is True and on_wallet()[phone.key_id] > before and len(on_wallet()) == 2, str(on_wallet()))
    prepared, _ = session_action({"action": "remove", "key_id": laptop.key_id})
    check("revoking one device names it, and only it goes",
          prepared["revokes"] == laptop.key_id and set(on_wallet()) == {phone.key_id}, str(on_wallet()))
    check("revoking a device the wallet doesn't approve is refused before anything is signed",
          refused({"action": "remove", "key_id": laptop.key_id}) == 400)
    stranger = SoftPasskey()
    check("approving a passkey that isn't the account's is refused",
          refused({"action": "add", "key_id": stranger.key_id}) == 400)
    check("approving for longer than the wallet allows is refused",
          refused({"action": "add", "key_id": phone.key_id, "ttl_secs": 91 * 86_400}) == 400)
    prepared, _ = session_action({"action": "remove_all"})
    check("revoking every device names each, and none is left on chain",
          prepared["revokes"] == [phone.key_id] and on_wallet() == {}, f"{prepared.get('revokes')} {on_wallet()}")
    check("revoking every device of a wallet with none is refused", refused({"action": "remove_all"}) == 400)

    # A confirm that never arrives -- the owner closed the tab while the approval mined. The next
    # wallet read shows it anyway: it reads the wallet, not anything the app wrote down.
    r = c.post("/api/wallet/session/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "add", "key_id": phone.key_id})
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    session = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers).json()["session"]
    check("an approval nobody confirmed shows on the next wallet read",
          [(k["key_id"], k["active"]) for k in session["keys"]] == [(phone.key_id, True)] and session["max_keys"] == 5,
          str(session))
    r = c.post("/api/wallet/session/confirm", headers=headers, json={"chain_id": CHAIN_ID, "tx_hash": tx_hash})
    check("a confirm arriving after the read still reports it", r.json().get("status") == "confirmed", r.text[:200])
    check("and the phone can approve again", sh.functions.isSessionActive(bytes.fromhex(phone.key_id[2:])).call() is True)

    # Withdraw actually moves value.
    sink = Account.create().address
    before = w3.eth.get_balance(wallet)
    owner_action(c, headers, acct, "/api/wallet/withdraw/prepare",
                 {"token": "eth", "amount": "0.1", "to": sink})
    check("the recipient received the withdrawal", w3.eth.get_balance(sink) == w3.to_wei("0.1", "ether"))
    check("the wallet balance dropped", w3.eth.get_balance(wallet) == before - w3.to_wei("0.1", "ether"))

    # Pause last: it stops validation, so do it after everything else.
    owner_action(c, headers, acct, "/api/wallet/pause/prepare", {})
    check("the wallet is paused on chain", sh.functions.paused().call() is True)
    owner_action(c, headers, acct, "/api/wallet/unpause/prepare", {})
    check("the wallet is unpaused again", sh.functions.paused().call() is False)


def test_simulations_bite(c: TestClient, acct, headers: dict, wallet: str):
    """
    The part no offline test can reach: a prepare must REFUSE what the contract would revert.

    Each of these has to come back 400 from the eth_call, before the user is ever asked to sign.
    """
    print("\n[4] simulations refuse what the chain would revert")

    r = c.post("/api/wallet/withdraw/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "token": "eth", "amount": "9999", "to": acct.address})
    check("withdrawing more than the balance -> 400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")
    check("...and names the contract's own error", "NotEnoughBalance" in r.text, r.text[:200])

    r = c.post("/api/wallet/withdraw/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "token": "eth", "amount": "0.01",
                     "to": "0x0000000000000000000000000000000000000000"})
    check("withdrawing to the zero address -> 400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")

    # dai is in every chain's token table, but the oracle prices it only on some (not on Sepolia).
    r = c.post("/api/wallet/watched-tokens/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "token": "dai", "action": "add"})
    if r.status_code == 400:
        check("watching an unpriced token -> 400", True)
        check("...and names TokenNotPriced", "TokenNotPriced" in r.text, r.text[:200])
    else:
        # DAI is priced on this deployment, so the simulation correctly allows it.
        check("watching a priced token is allowed", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

    r = c.post("/api/wallet/watched-tokens/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "token": WRAPPED, "action": "remove"})
    check("removing WETH -> 400: it always counts", r.status_code == 400 and "always counts" in r.text,
          f"{r.status_code} {r.text[:150]}")

    # Pause twice: the second must be refused by the simulation, not by a failed transaction.
    owner_action(c, headers, acct, "/api/wallet/pause/prepare", {})
    r = c.post("/api/wallet/pause/prepare", headers=headers, json={"chain_id": CHAIN_ID})
    check("pausing an already-paused wallet -> 400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")
    owner_action(c, headers, acct, "/api/wallet/unpause/prepare", {})


def test_cross_user_isolation(c: TestClient, acct, headers: dict, wallet: str):
    """A second account must not be able to touch or read back the first's wallet."""
    print("\n[5] one account cannot reach another's wallet")

    other = new_funded_account()
    other_headers = sign_in_as(c, other)

    # A real transaction against wallet A, reported by account B.
    r = c.post("/api/wallet/pause/prepare", headers=headers, json={"chain_id": CHAIN_ID})
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    r = c.post("/api/wallet/tx/confirm", headers=other_headers,
               json={"chain_id": CHAIN_ID, "tx_hash": tx_hash})
    check("B cannot confirm A's transaction", r.status_code in (400, 404), f"{r.status_code} {r.text[:150]}")

    r = c.post("/api/deploy", headers=other_headers,
               json={"chain_id": CHAIN_ID, "deployer": acct.address, "daily_limit_usd": 100})
    check("B cannot deploy from A's address", r.status_code == 403, f"{r.status_code} {r.text[:150]}")

    # Put A back.
    r = c.post("/api/wallet/unpause/prepare", headers=headers, json={"chain_id": CHAIN_ID})
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)


def test_contacts_are_owner_managed(c: TestClient, headers: dict, acct):
    """
    The contact routes that replaced the save_contact / delete_contact tools. Adding one takes the
    owner wallet's EIP-712 signature (`acct` here).

    The offline suite covers these in depth; what this adds is the real deployed app -- the same
    process that just built and confirmed transactions -- rather than a TestClient with a stubbed
    lifespan. Cheap, since contacts touch no chain: it is the last leg for that reason.
    """
    print("\n[6] contacts are managed by the owner, not the agent")

    # Compared against the account's own starting list rather than assumed empty: on a fresh
    # wallet.db the first sign-in is handed user id 1 -- the harness user APP_USER_ID usually names,
    # whose demo contact deploy_wallet.py has already saved.
    baseline = c.get("/api/contacts", headers=headers).json()["contacts"]
    payee = Account.create().address
    r = add_contact(c, headers, Account.create(), "Sandy", payee)
    check("a signature from any wallet but the owner's adds nothing", r.status_code == 400,
          f"{r.status_code} {r.text[:140]}")
    r = add_contact(c, headers, acct, "Sandy", payee)
    check("the owner's signature adds a contact", r.status_code == 201, f"{r.status_code} {r.text[:140]}")

    listed = c.get("/api/contacts", headers=headers).json()["contacts"]
    check("it is listed back", {"name": "sandy", "address": payee} in listed and len(listed) == len(baseline) + 1,
          str(listed)[:140])

    check("deleting works", c.delete("/api/contacts/sandy", headers=headers).status_code == 200)
    check("the list is back to where it started",
          c.get("/api/contacts", headers=headers).json()["contacts"] == baseline)

    # The other half of the boundary, asserted against the tools the running agent would use.
    import tools
    exported = {t.name for t in tools.get_tools()}
    check("no contact-writing tool is exported to the agent",
          not (exported & {"save_contact", "delete_contact"}), str(sorted(exported)))


def test_allowlist(c: TestClient, acct, headers: dict, wallet: str):
    """
    The contract allowlist end to end: the suggestions read, the list filled and turned on with one
    signature, a listed token and a plain wallet still paid, an unlisted contract refused with the
    reason in words, and the list emptied and turned off again for the steps that follow.

    Quotes only: the quote's simulation runs the wallet's guard exactly as a send would, so nothing
    needs to go out to prove what it lets through.
    """
    print("\n[7b] the contract allowlist: one signature to turn on, refusals the assistant can explain")
    abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    sh = w3.eth.contract(address=wallet, abi=abi)
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    turn = itertools.count(100_000)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    def read() -> dict:
        r = c.get(f"/api/wallet/{CHAIN_ID}/allowlist", headers=headers)
        assert r.status_code == 200, f"allowlist read failed: {r.status_code} {r.text[:250]}"
        return r.json()

    usdc = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
    dai = Web3.to_checksum_address(get_token_address(CHAIN_ID, "dai"))
    payee = next(x["address"] for x in c.get("/api/contacts", headers=headers).json()["contacts"]
                 if x["name"] == "payee")
    # Only the dashboard's tokens are suggested, and test_dashboard_tokens leaves USDC off it, so it
    # goes on here, as an owner who pays in it would put it. Taken off again at the end.
    r = c.post("/api/tokens/custom", headers=headers, json={"chain_id": CHAIN_ID, "address": usdc})
    check("USDC goes on the dashboard", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    body = read()
    suggested = {s["address"]: s["label"] for s in body["suggested"]}
    check("the list starts off and empty", body["enabled"] is False and body["targets"] == [], str(body)[:200])
    check("every contract the assistant trades through is suggested",
          all(spender in suggested for spender in get_default_spenders(CHAIN_ID)), str(suggested))
    if get_exchange(CHAIN_ID) == "uniswap-v3":
        v3 = get_v3_network(CHAIN_ID)
        check("...by name: the Uniswap V3 router and its positions",
              (suggested.get(v3["swap_router"]), suggested.get(v3["position_manager"]))
              == ("Uniswap V3 router", "Uniswap V3 positions"), str(suggested))
        try:
            v2_router = get_router(CHAIN_ID)
        except ValueError:
            v2_router = None
        check("...and not the V2 router the assistant no longer trades through", v2_router not in suggested,
              str(suggested))
    check("USDC, on the dashboard, is suggested by name", suggested.get(usdc) == "USDC", str(suggested))
    check("a listed token that isn't on the dashboard is not", dai not in suggested, str(suggested))
    check("the review registry is suggested", sh.functions.REPUTATION_REGISTRY().call() in suggested)
    check("a contact with a plain wallet is not", payee not in suggested)

    owner_action(c, headers, acct, "/api/wallet/allowlist/prepare", {"action": "enable", "targets": [usdc]})
    check("one signature turned the list on", sh.functions.sessionAllowlistEnabled().call() is True)
    check("...with USDC as its one entry", sh.functions.getAllowedTargets().call() == [usdc])
    body = read()
    check("the read names the entry", body["targets"] == [{"address": usdc, "label": "USDC"}], str(body)[:200])
    check("...and stops suggesting it", usdc not in {s["address"] for s in body["suggested"]})

    quoted = tools.transfers.transfer_erc20.func(next_turn(), token="usdc", recipient="payee", amount=1)
    check("a listed token still quotes", "quote_id" in quoted, str(quoted)[:200])
    tools.sending.cancel_transaction.func(next_turn(), quote_id=quoted["quote_id"])
    quoted = tools.transfers.send_eth.func(next_turn(), recipient="payee", amount_eth=0.001)
    check("a plain wallet needs no entry: sending it the native asset still quotes",
          "quote_id" in quoted, str(quoted)[:200])
    tools.sending.cancel_transaction.func(next_turn(), quote_id=quoted["quote_id"])

    # An unlisted contract. The wallet holds the token, so the balance check passes and the refusal
    # is the guard's own.
    link = Web3.to_checksum_address(get_token_address(CHAIN_ID, "link"))
    deal_erc20(link, wallet, 10 * 10**18)
    try:
        tools.transfers.transfer_erc20.func(next_turn(), token="link", recipient="payee", amount=1)
        check("an unlisted contract is refused", False, "it was quoted")
    except ToolException as e:
        check("an unlisted contract is refused, naming it", link in str(e), str(e)[:300])
        check("...and saying what the owner can do", "Controls -> Advanced" in str(e), str(e)[:300])

    # Emptied, the list stays on and blocks every contract -- it never reopens by itself -- and it
    # can't be turned on again empty.
    owner_action(c, headers, acct, "/api/wallet/allowlist/prepare", {"action": "remove", "targets": [usdc]})
    check("the entry is gone", sh.functions.getAllowedTargets().call() == [])
    check("...and the list is still on: it fails closed", sh.functions.sessionAllowlistEnabled().call() is True)
    r = c.post("/api/wallet/allowlist/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "enable", "targets": []})
    check("turning on an empty list is refused before signing",
          r.status_code == 400 and "EmptyAllowlist" in r.text, f"{r.status_code} {r.text[:160]}")
    owner_action(c, headers, acct, "/api/wallet/allowlist/prepare", {"action": "disable"})
    check("the list is off again", sh.functions.sessionAllowlistEnabled().call() is False)
    r = c.delete(f"/api/tokens/custom/{CHAIN_ID}/{usdc}", headers=headers)
    check("USDC comes off the dashboard again", r.status_code == 200, f"{r.status_code} {r.text[:160]}")


def deal_erc20(token: str, holder: str, amount: int):
    """
    Sets `holder`'s balance of a standard ERC20 on the fork, by finding the token's balances mapping
    slot -- what forge-std's deal() does. Tries each slot, keeps the one balanceOf reflects, and puts
    every other slot back as it was.
    """
    erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
    word = "0x" + amount.to_bytes(32, "big").hex()
    for slot in range(64):
        key = "0x" + keccak(bytes.fromhex(holder[2:].rjust(64, "0")) + slot.to_bytes(32, "big")).hex()
        original = "0x" + bytes(w3.eth.get_storage_at(token, key)).hex()
        w3.provider.make_request("anvil_setStorageAt", [token, key, word])
        if erc20.functions.balanceOf(holder).call() == amount:
            return
        w3.provider.make_request("anvil_setStorageAt", [token, key, original])
    raise AssertionError(f"could not find the balances slot of {token}")


def sent_by_bundlers() -> int:
    """How many transactions the API's bundler keys have sent between them: "nothing was sent" is this unmoved."""
    return sum(w3.eth.get_transaction_count(a.address) for a in bundler.bundler_keys())


def user_op_cost(sh, receipt) -> dict:
    """
    What one session-key transaction really cost, read back from its receipt.

    The wallet pays two things on top of the amount it sends: the network fee -- the EntryPoint's
    actualGasCost, repaid to the bundler out of the wallet's deposit -- and the protocol fee, sent
    to the treasury. The bundler's own outer transaction is shown beside it, to prove it was repaid.
    """
    entry_point = w3.eth.contract(address=sh.functions.ENTRY_POINT().call(), abi=ientry_point)
    op = [e for e in entry_point.events.UserOperationEvent().process_receipt(receipt, errors=DISCARD)][0]["args"]
    fee = sum(e["args"]["fee"] for e in sh.events.ProtocolFeePaid().process_receipt(receipt, errors=DISCARD))
    handle_ops = entry_point.decode_function_input(w3.eth.get_transaction(receipt["transactionHash"])["input"])[1]
    packed = handle_ops["ops"][0]
    limits = int.from_bytes(packed["accountGasLimits"], "big")
    outer_cost = receipt["gasUsed"] * receipt["effectiveGasPrice"]

    def usd(wei: int) -> float:
        return sh.functions.getUsdValue(ETH_SENTINEL, wei).call() / 10**18 if wei else 0.0

    return {
        "tx_hash": "0x" + bytes(receipt["transactionHash"]).hex(),
        "op_gas_used": op["actualGasUsed"],
        "verification_gas_limit": limits >> 128,
        "call_gas_limit": limits & ((1 << 128) - 1),
        "pre_verification_gas": packed["preVerificationGas"],
        "gas_price_gwei": receipt["effectiveGasPrice"] / 1e9,
        "network_fee_wei": op["actualGasCost"],
        "network_fee_usd": usd(op["actualGasCost"]),
        "protocol_fee_wei": fee,
        "protocol_fee_usd": usd(fee),
        "total_fee_usd": usd(op["actualGasCost"] + fee),
        "eth_usd": usd(10**18),
        "bundler_gas_used": receipt["gasUsed"],
        "bundler_paid_wei": outer_cost,
        "bundler_net_wei": op["actualGasCost"] - outer_cost,
    }


def test_self_bundling(c: TestClient, acct, headers: dict, wallet: str):
    """
    The self-bundler end to end: real ERC20 transfers through the agent's own tool, what each one
    cost, and the failure modes a live chain adds.

    The transfers go through tools.transfer_erc20 -- the exact function the agent calls, minus the
    model -- so the path is the production one: langchain-erc20's plan, the signature-free gas
    estimate, the passkey's approval (approve_and_send: the sign flow the card and phone drive),
    handleOps from the API's bundler key. One transfer moves a token the cap does not watch (no
    price read), one a token it does (one), so the cost of the oracle shows up in the numbers.
    """
    print("\n[7] self-bundling: ERC20 transfers, their real cost, and the live-chain failure modes")
    abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    sh = w3.eth.contract(address=wallet, abi=abi)
    user_id = c.get("/api/me", headers=headers).json()["user_id"]

    # A tool runtime for the NEXT conversation turn. Every quote gets its own, as each message does
    # in production (smart_wallet_agent._next_turn_id); here the test plays the part of the user.
    turn = itertools.count(1)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    # Keys of its own for each process, so the API and the Telegram bot never hand out the same nonce.
    api_bundlers = [a.address for a in bundler.bundler_keys()]
    bundler.use_bundler_key(bundler.TELEGRAM_BUNDLER_ENV)
    try:
        telegram_bundlers = [a.address for a in bundler.bundler_keys()]
    finally:
        bundler.use_bundler_key(bundler.API_BUNDLER_ENV)
    check(f"the API and the Telegram bot bundle with keys of their own ({len(api_bundlers)} and "
          f"{len(telegram_bundlers)})", not set(api_bundlers) & set(telegram_bundlers))
    check("the API's keys are the default", [a.address for a in bundler.bundler_keys()] == api_bundlers)
    api_bundler = bundler.resolve_bundler().address
    check("...and the first of them is the deployer", api_bundler == Web3.to_checksum_address(os.environ["SEPOLIA_ACCOUNT"]))
    check("every bundler key is a plain account holding gas on this fork",
          all(w3.eth.get_code(a) == b"" and w3.eth.get_balance(a) > 0 for a in api_bundlers + telegram_bundlers))
    check("a fork never broadcasts privately", bundler._send_w3_for(NETWORK) is None)
    check("live mainnet does", bundler._send_w3_for("mainnet") is not None)
    check("...and so does live BSC", bundler._send_w3_for("bsc") is not None)

    usdc = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
    token = w3.eth.contract(address=usdc, abi=ERC20_ABI)
    unit = 10 ** token.functions.decimals().call()
    deal_erc20(usdc, wallet, 1_000 * unit)
    payee = Account.create().address
    r = add_contact(c, headers, acct, "payee", payee)
    check("the payee is saved as a contact", r.status_code == 201, f"{r.status_code} {r.text[:140]}")

    def quote_transfer(amount: float) -> dict:
        """The quote half: builds and prices the transfer. Sends nothing."""
        return tools.transfers.transfer_erc20.func(
            next_turn(), token="usdc",
            recipient="payee", amount=amount,
        )

    def confirm(quoted: dict):
        """The send half: the user approves the card with their passkey."""
        result = approve_and_send(user_id, quoted["quote_id"])
        return w3.eth.get_transaction_receipt("0x" + result.split("`")[1].removeprefix("0x"))

    def transfer(amount: float) -> tuple[dict, int]:
        before = token.functions.balanceOf(payee).call()
        cost = user_op_cost(sh, confirm(quote_transfer(amount)))
        return cost, token.functions.balanceOf(payee).call() - before

    report = {"network": NETWORK, "native_asset": get_native_asset_ticker(CHAIN_ID), "transfers": []}

    # The first transfer pays one-off costs a repeat does not -- this wallet's first UserOp (its
    # EntryPoint nonce and deposit go from zero) and a payee who never held USDC -- so it is
    # reported on its own, and the watched/unwatched comparison below is made between repeats.
    cost_first, moved = transfer(10)
    check("a first USDC transfer lands", moved == 10 * unit, str(moved))
    report["transfers"].append({"label": "10 USDC, first transfer (new payee, wallet's first op)", **cost_first})

    cost, moved = transfer(10)
    check("an unwatched USDC transfer lands", moved == 10 * unit, str(moved))
    report["transfers"].append({"label": "10 USDC, repeat, USDC not watched (no price read)", **cost})

    owner_action(c, headers, acct, "/api/wallet/watched-tokens/prepare", {"token": "usdc", "action": "add"})
    cost_watched, moved = transfer(10)
    check("a watched USDC transfer lands", moved == 10 * unit, str(moved))
    report["transfers"].append({"label": "10 USDC, repeat, USDC watched (one price read)", **cost_watched})
    check("the price read shows up in the gas", cost_watched["op_gas_used"] > cost["op_gas_used"],
          f'{cost_watched["op_gas_used"]} vs {cost["op_gas_used"]}')

    # The quote/approval split itself. A quote prices a transaction that is still unsigned and
    # unsent; no tool can send it -- only the user's passkey -- it belongs to one user, it works
    # once, and what it quoted has to match what the chain then charges.
    sent_before = sent_by_bundlers()
    held_before = token.functions.balanceOf(payee).call()
    same_turn = next_turn()
    quoted = tools.transfers.transfer_erc20.func(
        same_turn, token="usdc", recipient="payee", amount=7
    )
    check("a quote says plainly that nothing was sent", "NOT SENT" in quoted["status"], quoted["status"])
    check("quoting broadcasts nothing", sent_by_bundlers() == sent_before)
    check("...and moves no tokens", token.functions.balanceOf(payee).call() == held_before)
    check("the quote names the contract the value goes through", quoted["destinations"] == [usdc],
          str(quoted["destinations"]))
    check("the quote names the network it runs on", quoted["network"] == get_chain_display_name(CHAIN_ID),
          str(quoted.get("network")))
    check("the quote describes the transfer itself", "7" in quoted["action"] and payee[2:10].lower()
          in quoted["action"].lower().replace("0x", ""), quoted["action"])
    # The model is shown one fee figure; the breakdown and the ceiling stay with the stored quote.
    [stored] = quotes.raised_in(user_id, CHAIN_ID, same_turn.context.turn_id)
    cost = stored.cost
    check("the quote shows the fees as one USD figure", quoted["fees_usd"] == cost["total_usd"] > 0,
          f'{quoted.get("fees_usd")} vs {cost.get("total_usd")}')
    check("...and none of the breakdown", not {"network_fee_usd", "protocol_fee_usd", "max_total_usd"} & set(quoted),
          str(sorted(quoted)))
    check("the ceiling is at least the estimate", cost["max_total_usd"] >= cost["total_usd"],
          f'{cost["max_total_usd"]} vs {cost["total_usd"]}')
    check("the protocol fee is priced separately", cost["protocol_fee_usd"] > 0, str(cost.get("protocol_fee_usd")))
    # The quote ran the wallet checks itself -- the agent no longer calls preflight_check first.
    check("the quote says what it is worth and what the (watched) USDC counts toward the limit",
          abs(quoted["usd_value"] - 7) < 0.1 and abs(quoted["charged_usd"] - 7) < 0.1,
          f'{quoted.get("usd_value")} / {quoted.get("charged_usd")}')
    check("...but not what is left of the limit, nor the key's time while it has weeks",
          "remaining_usd" not in quoted and "session_expires_in_secs" not in quoted, str(sorted(quoted)))

    try:
        shown = tools.sending.confirm_transaction.func(next_turn(), quote_id=quoted["quote_id"])
    except ToolException as e:
        shown = str(e)
    check("the agent's confirm_transaction only shows the card again", shown == tools.sending.CARD_SHOWN, shown[:160])
    check("...and still nothing was broadcast", sent_by_bundlers() == sent_before)

    signing.open_request(quotes._pending[quoted["quote_id"]], signing.WEB)
    try:
        signing.find_by_quote(user_id + 9_999, CHAIN_ID, quoted["quote_id"])
        check("another user cannot approve someone else's quote", False, "it was found")
    except signing.Expired:
        check("another user cannot approve someone else's quote", True)

    receipt = confirm(quoted)
    actual = user_op_cost(sh, receipt)
    check("confirming sends the quoted transfer, once",
          token.functions.balanceOf(payee).call() - held_before == 7 * unit)
    check("the real cost stayed under the quoted ceiling",
          actual["total_fee_usd"] <= cost["max_total_usd"],
          f'${actual["total_fee_usd"]:.4f} charged vs ${cost["max_total_usd"]:.4f} quoted as the max')
    # Tight on purpose. A quote that is merely an upper bound is not a price: it is the number the
    # user decides on, so it has to track what the chain then charges, not the gas that was merely
    # reserved. 10% leaves room for the base fee moving between the quote and the block.
    check("the quoted cost was within 10% of the real one",
          abs(actual["total_fee_usd"] - quoted["fees_usd"]) <= 0.10 * actual["total_fee_usd"],
          f'quoted ${quoted["fees_usd"]:.4f}, charged ${actual["total_fee_usd"]:.4f}')
    report["quote_vs_actual"] = {
        "quoted_total_usd": quoted["fees_usd"],
        "quoted_max_usd": cost["max_total_usd"],
        "charged_total_usd": actual["total_fee_usd"],
    }

    try:
        approve_and_send(user_id, quoted["quote_id"])
        check("a quote cannot be approved twice", False, "it was sent again")
    except ToolException as e:
        check("a quote cannot be approved twice", "no pending transaction" in str(e), str(e)[:160])

    # Over the spending cap: the quote's own wallet checks refuse it, saying how much is left --
    # before anything is signed, sent or paid for. (The bundler's execution estimate would refuse
    # it too, as BudgetExceeded; the check's reason is the one the user can act on.)
    deal_erc20(usdc, wallet, 5_000 * unit)
    nonce_before = sent_by_bundlers()
    native_before = w3.eth.get_balance(wallet)
    try:
        tools.transfers.transfer_erc20.func(next_turn(), token="usdc",
                                  recipient="payee", amount=2_000)
        check("a transfer over the cap is refused", False, "it was sent")
    except ToolException as e:
        check("a transfer over the cap is refused, saying what is left, before sending",
              "spending limit" in str(e) and "is left" in str(e) and "Nothing was sent" in str(e), str(e)[:200])
    check("...so the bundlers sent nothing", sent_by_bundlers() == nonce_before)
    check("...and the wallet paid no gas", w3.eth.get_balance(wallet) == native_before)

    # A paused wallet: refused with the reason, rather than as a failed simulation.
    owner_action(c, headers, acct, "/api/wallet/pause/prepare", {})
    try:
        tools.transfers.transfer_erc20.func(next_turn(), token="usdc", recipient="payee", amount=1)
        check("a paused wallet's transfer is refused", False, "it was quoted")
    except ToolException as e:
        check("a paused wallet's transfer is refused, saying it is paused",
              "paused" in str(e) and "Nothing was sent" in str(e), str(e)[:200])
    finally:
        owner_action(c, headers, acct, "/api/wallet/unpause/prepare", {})
    check("...and the bundlers sent nothing for it either", sent_by_bundlers() == nonce_before)
    owner_action(c, headers, acct, "/api/wallet/watched-tokens/prepare", {"token": "usdc", "action": "remove"})

    for t in report["transfers"]:
        # The bundler fronts the outer transaction and is repaid by the EntryPoint out of the
        # wallet's prefund. preVerificationGas is what covers the part the EntryPoint cannot
        # measure; if it were short, the bundler would lose a little on every op.
        check(f'the bundler was repaid in full ({t["label"]})', t["bundler_net_wei"] >= 0,
              f'net {t["bundler_net_wei"]} wei')

    # Someone else lands our op first. The signature does not cover the beneficiary, so a rival that
    # saw it can submit it naming itself; ours then reverts on the used nonce. The user's transfer
    # still happened, once -- and the app must say so, not report a failure the user might retry.
    before = token.functions.balanceOf(payee).call()
    amount = 5 * unit
    bundler_account = bundler.resolve_bundler()
    prepared, entry_point = passkey_op(
        user_id, [(usdc, 0, bytes.fromhex(token.encode_abi("transfer", args=[payee, amount])[2:]))], bundler_account
    )
    check("the op's hash, worked out locally, is the EntryPoint's own",
          prepared.user_op_hash == bytes(entry_point.functions.getUserOpHash(prepared.op).call()))
    rival = new_funded_account()
    rival_tx = entry_point.functions.handleOps([prepared.op], rival.address).build_transaction({
        "from": rival.address, "nonce": w3.eth.get_transaction_count(rival.address),
        "gas": prepared.outer_gas, "chainId": CHAIN_ID, **prepared.fees,
    })
    rival_hash = w3.eth.send_raw_transaction(rival.sign_transaction(rival_tx).raw_transaction)
    w3.eth.wait_for_transaction_receipt(rival_hash, timeout=60)
    ours_nonce = w3.eth.get_transaction_count(api_bundler)
    tx_hash, receipt = bundler.broadcast_user_op(user_id, prepared, bundler_account)
    check("our own handleOps went out (and reverted on the used nonce)",
          w3.eth.get_transaction_count(api_bundler) == ours_nonce + 1)
    check("the app reports the rival's transaction instead of a failure",
          bytes(tx_hash) == bytes(rival_hash), f"{bytes(tx_hash).hex()} vs {bytes(rival_hash).hex()}")
    check("the payee was paid once, not twice", token.functions.balanceOf(payee).call() - before == amount)

    # An unfunded bundler refuses up front, rather than failing in the mempool.
    saved = w3.eth.get_balance(api_bundler)
    w3.provider.make_request("anvil_setBalance", [api_bundler, hex(10**6)])
    before = token.functions.balanceOf(payee).call()
    try:
        tools.transfers.transfer_erc20.func(next_turn(), token="usdc",
                                  recipient="payee", amount=1)
        check("an unfunded bundler refuses", False, "it was sent")
    except ToolException as e:
        check("an unfunded bundler refuses, and says so", "top it up" in str(e), str(e)[:200])
    finally:
        w3.provider.make_request("anvil_setBalance", [api_bundler, hex(saved)])
    check("...and nothing moved", token.functions.balanceOf(payee).call() == before)

    with open(COST_REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    for t in report["transfers"]:
        print(f'  COST  {t["label"]}: network ${t["network_fee_usd"]:.4f} + protocol ${t["protocol_fee_usd"]:.4f}'
              f' = ${t["total_fee_usd"]:.4f}  ({t["op_gas_used"]:,} gas at {t["gas_price_gwei"]:.3f} gwei;'
              f' bundler net {t["bundler_net_wei"]:+,} wei)')
    print(f"  cost report written to {COST_REPORT_PATH}")


def deploy_another_wallet(c: TestClient, acct, headers: dict) -> str:
    """
    A second account's wallet, deployed the way test_deploy_round_trip does it, minus the checks:
    with a passkey of its own, made through the API, which the deploy approves.
    """
    key = register_passkey(c, headers)
    body = {"chain_id": CHAIN_ID, "deployer": acct.address, "daily_limit_usd": 50_000,
            "watched_tokens": [], "prefund_eth": "1", "key_id": key.key_id}
    prepared = c.post("/api/deploy", headers=headers, json=body).json()
    tx_hash = sign_and_send(acct, prepared["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    r = c.post("/api/deploy/confirm", headers=headers, json={
        "chain_id": CHAIN_ID, "deployer": acct.address, "tx_hash": tx_hash,
        "predicted_address": prepared["predicted_address"],
    })
    assert r.status_code == 200, f"second wallet: {r.status_code} {r.text[:250]}"
    return r.json()["wallet_address"]


# How many wallets test_bundling puts in one bundle: the run's own wallet plus new ones. Two by
# default, to keep the run short; E2E_BUNDLE_WALLETS=40 fills a whole bundle (bundler.BUNDLE_MAX_OPS).
BUNDLE_WALLETS = int(os.getenv("E2E_BUNDLE_WALLETS", "2"))


def test_bundling(c: TestClient, acct, headers: dict, wallet: str):
    """
    Several accounts' ops in ONE handleOps (bundler.Lane): every one runs, each from its own wallet,
    each found by its own hash in the shared receipt, History reads each from its own logs, and
    the bundler is repaid in full.

    The ops are queued all at once, as ops pile up while the lane is busy, so the lane forms the
    bundle itself -- caps, one op per wallet, the simulation and the fee -- exactly as under load.
    """
    n = BUNDLE_WALLETS
    print(f"\n[7d] bundling: {n} wallets' transfers in one handleOps")
    senders = [(headers, wallet)]
    for _ in range(n - 1):
        other = new_funded_account()
        other_headers = sign_in_as(c, other)
        senders.append((other_headers, deploy_another_wallet(c, other, other_headers)))
    print(f"  INFO  {n - 1} more wallets deployed")

    usdc = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
    token = w3.eth.contract(address=usdc, abi=ERC20_ABI)
    unit = 10 ** token.functions.decimals().call()
    account = bundler.resolve_bundler()
    ops = []  # (user_id, wallet, payee, amount, prepared)
    for i, (account_headers, sender) in enumerate(senders, start=1):
        user_id = c.get("/api/me", headers=account_headers).json()["user_id"]
        deal_erc20(usdc, sender, 100 * unit)
        payee, amount = Account.create().address, i * unit
        prepared, _ = passkey_op(
            user_id, [(usdc, 0, bytes.fromhex(token.encode_abi("transfer", args=[payee, amount])[2:]))], account
        )
        ops.append((user_id, sender, payee, amount, prepared))

    lane = bundler._lane_for(ops[0][0], account)
    cap = bundler.bundle_gas_cap(NETWORK)
    counted = sum(prepared.outer_gas for *_, prepared in ops)
    print(f"  INFO  {n} ops count {counted:,} gas against the bundle cap of {cap:,} on {NETWORK}")
    nonce_before = w3.eth.get_transaction_count(account.address)
    balance_before = w3.eth.get_balance(account.address)
    queued = [bundler._Queued(prepared) for *_, prepared in ops]
    with lane._ready:
        lane._queue.extend(queued)
        lane._ready.notify()
    bundles = [q.on_wire.result(timeout=120) for q in queued]
    groups: dict[int, list] = {}
    for (*_, prepared), bundle in zip(ops, bundles):
        groups.setdefault(id(bundle), []).append(prepared)

    results = [
        bundler.await_user_op(bundler.SubmittedUserOp(prepared, lane.w3, lane.entry_point, lane, bundle))
        for (*_, prepared), bundle in zip(ops, bundles)
    ]
    if n <= bundler.BUNDLE_MAX_OPS and counted <= cap:
        check(f"all {n} ops went out in one transaction", len(groups) == 1, f"{len(groups)} bundles")
        check("...the bundler's only one", w3.eth.get_transaction_count(account.address) == nonce_before + 1)
    else:
        check(f"{n} ops went out in {len(groups)} bundles, each within the op and gas caps",
              all(len(g) == 1 or (len(g) <= bundler.BUNDLE_MAX_OPS and sum(p.outer_gas for p in g) <= cap)
                  for g in groups.values()),
              str([len(g) for g in groups.values()]))
    check(f"all {n} ran, each in its own bundle's transaction",
          all(r[1]["status"] == 1 and r[0] in b.sent.hashes for r, b in zip(results, bundles)),
          str({r[0].hex() for r in results}))
    paid = [token.functions.balanceOf(payee).call() == amount for _, _, payee, amount, _ in ops]
    check(f"every payee got exactly its amount ({sum(paid)}/{n})", all(paid))

    entry_point = w3.eth.contract(address=lane.entry_point.address, abi=ientry_point)
    receipts = list({r[1]["transactionHash"]: r[1] for r in results}.values())
    events = [e for receipt in receipts
              for e in entry_point.events.UserOperationEvent().process_receipt(receipt, errors=DISCARD)]
    check(f"the receipts hold all {n} ops' events, every one successful",
          sorted(e["args"]["sender"] for e in events) == sorted(Web3.to_checksum_address(s) for _, s, *_ in ops)
          and all(e["args"]["success"] for e in events), f"{len(events)} events")

    read_right = 0
    for (_, sender, payee, amount, prepared), (tx_hash, receipt) in zip(ops, results):
        moved = tx_movements.assistant_movements(
            sender, w3.eth.get_transaction(tx_hash), receipt, lambda t: ("USDC", 6) if t == usdc else None,
            native=get_native_asset_ticker(CHAIN_ID), wrapped=None, exchanges=(), user_op_hash=prepared.user_op_hash,
        )
        read_right += [(m.direction, m.amount, m.counterparty) for m in moved] == [("out", amount, payee)]
    check(f"History reads each wallet from its own logs: its one transfer ({read_right}/{n})", read_right == n)

    gas_used = sum(receipt["gasUsed"] for receipt in receipts)
    net = w3.eth.get_balance(account.address) - balance_before
    print(f"  INFO  {len(receipts)} transaction(s), {gas_used:,} gas: {gas_used // n:,} gas an op; "
          f"bundler net {net:+,} wei")
    check("the bundler was repaid in full", net >= 0, f"net {net} wei")


# How many wallets test_bundler_pool sends from at once, spread over the API's bundler keys: nine by
# default (three a key, every kind of transaction at least once); E2E_POOL_WALLETS=120 gives three
# keys forty each. E2E_POOL_OUT names a JSON file to list the wallets in, so the agent can be run
# against the same ones afterwards.
POOL_WALLETS = int(os.getenv("E2E_POOL_WALLETS", "9"))
POOL_OUT = os.getenv("E2E_POOL_OUT")


def _pool_kinds() -> list[tuple[str, object, dict]]:
    """
    What test_bundler_pool's wallets send, wallet by wallet in turn: (label, quoting tool, its
    arguments). Small enough for any fork's prices, a wallet's 1 native coin and its 100 USDC.
    """
    native = get_native_asset_ticker(CHAIN_ID)
    return [
        ("USDC transfer", tools.transfers.transfer_erc20, {"token": "usdc", "recipient": "payee", "amount": 1.5}),
        (f"{native} send", tools.transfers.send_eth, {"recipient": "payee", "amount_eth": 0.001}),
        (f"wrap {native}", tools.transfers.wrap_eth, {"amount_eth": 0.002}),
        (f"swap {native} for USDC", tools.uniswap.swap, {"token_in": "eth", "token_out": "usdc", "amount_in": 0.001}),
        (f"swap USDC for {native}", tools.uniswap.swap, {"token_in": "usdc", "token_out": "eth", "amount_in": 2}),
        ("swap and send", tools.uniswap.swap,
         {"token_in": "eth", "token_out": "usdc", "amount_in": 0.001, "recipient": "payee"}),
        ("ERC-8004 rating", tools.erc8004.post_reputation_feedback, {"score": 87, "tag2": "pool-test"}),
    ]


def test_bundler_pool(c: TestClient):
    """
    The API's pool of bundler keys under load: many wallets' Confirms at once, each a different kind
    of transaction -- transfers, native sends, wraps, swaps both ways, a swap sent on to a contact,
    ERC-8004 ratings -- through the production path: each card approved with its account's passkey
    (approve_quote, as each approval page does), then every one sent at once
    (tools.sending.submit_signed, then settle_quoted). They spread over the keys (bundler.pick_bundler), each key bundles its own share
    (bundler.Lane), every op runs and does what it was quoted to do, History lists each under the
    hash that ran it, and every key is repaid.
    """
    n = POOL_WALLETS
    keys = [a.address for a in bundler.bundler_keys()]
    kinds = _pool_kinds()
    print(f"\n[7e] the bundler pool: {n} wallets' mixed transactions confirmed at once over {len(keys)} keys")

    usdc_address = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
    usdc = w3.eth.contract(address=usdc_address, abi=ERC20_ABI)
    wrapped = w3.eth.contract(address=Web3.to_checksum_address(get_token_address(CHAIN_ID, WRAPPED)), abi=ERC20_ABI)
    unit = 10 ** usdc.functions.decimals().call()
    started = time.monotonic()
    wallets = []  # one dict per wallet: who it is, what it sends, and what that should change
    for i in range(n):
        owner = new_funded_account()
        owner_headers = sign_in_as(c, owner)
        wallet = deploy_another_wallet(c, owner, owner_headers)
        payee = Account.create().address
        r = add_contact(c, owner_headers, owner, "payee", payee)
        assert r.status_code == 201, f"contact: {r.status_code} {r.text[:140]}"
        deal_erc20(usdc_address, wallet, 100 * unit)
        label, tool, args = kinds[i % len(kinds)]
        wallets.append({"user_id": c.get("/api/me", headers=owner_headers).json()["user_id"], "wallet": wallet,
                        "payee": payee, "headers": owner_headers, "kind": label, "tool": tool, "args": args})
    print(f"  INFO  {n} wallets deployed and funded in {time.monotonic() - started:.0f} s")

    sh_abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    first_sh = w3.eth.contract(address=wallets[0]["wallet"], abi=sh_abi)
    reputation = w3.eth.contract(address=first_sh.functions.REPUTATION_REGISTRY().call(), abi=REPUTATION_REGISTRY_ABI)
    agent_id = first_sh.functions.getAgentId().call()

    def effect(item: dict) -> int:
        """The one figure this wallet's transaction should move."""
        kind, wallet, payee = item["kind"], item["wallet"], item["payee"]
        if kind == "USDC transfer" or kind == "swap and send":
            return usdc.functions.balanceOf(payee).call()
        if kind.endswith(" send"):
            return w3.eth.get_balance(payee)
        if kind.startswith("wrap"):
            return wrapped.functions.balanceOf(wallet).call()
        if kind.startswith("swap"):
            return usdc.functions.balanceOf(wallet).call()
        return reputation.functions.getLastIndex(agent_id, wallet).call()

    def expected(item: dict, before: int, after: int) -> bool:
        kind = item["kind"]
        if kind == "USDC transfer":
            return after - before == int(Decimal("1.5") * unit)
        if kind == "swap and send":
            return after > before
        if kind.endswith(" send"):
            return after - before == 10**15
        if kind.startswith("wrap"):
            return after - before == 2 * 10**15
        if kind.startswith("swap USDC"):
            return before - after == 2 * unit
        if kind.startswith("swap"):
            return after > before
        return after == before + 1

    turn = itertools.count(5_000_000)

    def quote(item: dict) -> dict:
        runtime = SimpleNamespace(context=AgentContext(user_id=item["user_id"], turn_id=next(turn)))
        return item["tool"].func(runtime, **item["args"])

    started = time.monotonic()
    # 8 at a time: each quote is dozens of reads, and an Anvil fork fetching cold state from its
    # upstream RPC for 32 at once stopped answering altogether (2026-10-20). The Confirms below
    # still go all at once.
    with ThreadPoolExecutor(max_workers=8) as pool:
        quoted = list(pool.map(quote, wallets))
    print(f"  INFO  {n} quotes in {time.monotonic() - started:.1f} s (8 at a time)")
    check(f"every wallet's transaction is quoted ({n}/{n})", all("quote_id" in q for q in quoted))
    for item, q in zip(wallets, quoted):
        item["quote"], item["before"] = q, effect(item)

    def approve(item: dict):
        try:
            return approve_quote(item["user_id"], item["quote"]["quote_id"])
        except ToolException as e:
            return e

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        approvals = list(pool.map(approve, wallets))
    print(f"  INFO  {n} approvals in {time.monotonic() - started:.1f} s (8 at a time)")
    check(f"every card is approved by its account's passkey ({n}/{n})",
          not [a for a in approvals if isinstance(a, Exception)],
          str([str(a)[:120] for a in approvals if isinstance(a, Exception)][:3]))
    for item, approved in zip(wallets, approvals):
        item["approved"] = approved

    nonces = {k: w3.eth.get_transaction_count(k) for k in keys}
    balances = {k: w3.eth.get_balance(k) for k in keys}
    barrier = threading.Barrier(n)

    def confirm(item: dict):
        """One Confirm tap: on the wire, then landed -- or the error a user would see."""
        barrier.wait(timeout=120)
        t0 = time.monotonic()
        try:
            if isinstance(item["approved"], Exception):
                raise item["approved"]
            sending = send_approved(item["approved"])
            on_wire = time.monotonic() - t0
            return sending, tools.sending.settle_quoted(sending), on_wire, time.monotonic() - t0
        except Exception as e:  # noqa: BLE001 -- counted and shown below
            return None, f"{type(e).__name__}: {e}", None, time.monotonic() - t0

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(confirm, wallets))
    wall = time.monotonic() - started
    failed = [(item["kind"], text[:160]) for item, (sending, text, *_) in zip(wallets, results) if sending is None
              or not text.startswith("Sent")]
    check(f"all {n} Confirms went out and landed", not failed, str(failed[:5]))

    by_key = Counter(sending.submitted.lane.bundler.address for sending, *_ in results if sending)
    check(f"the ops spread over all {len(keys)} keys: {[by_key[k] for k in keys]}",
          set(by_key) == set(keys) and max(by_key.values()) <= 2 * -(-n // len(keys)), str(by_key))

    hashes = {}  # the hash that ran each op, from its reply
    for item, (sending, text, *_) in zip(wallets, results):
        if sending and "`" in text:
            hashes[item["wallet"]] = text.split("`")[1]
    receipts = {h: w3.eth.get_transaction_receipt(h) for h in set(hashes.values())}
    entry_point = w3.eth.contract(address=first_sh.functions.ENTRY_POINT().call(), abi=ientry_point)
    sizes = {h: len(entry_point.events.UserOperationEvent().process_receipt(r, errors=DISCARD)) for h, r in receipts.items()}
    senders = {h: w3.eth.get_transaction(h)["from"] for h in receipts}
    cap = bundler.bundle_gas_cap(NETWORK)
    for k in keys:
        mine = [h for h in receipts if senders[h] == k]
        print(f"  INFO  key {keys.index(k) + 1} ({k[:10]}…): {by_key[k]} ops in {len(mine)} transaction(s) of "
              f"{sorted((sizes[h] for h in mine), reverse=True)} ops, "
              f"nonce +{w3.eth.get_transaction_count(k) - nonces[k]}, "
              f"net {w3.eth.get_balance(k) - balances[k]:+,} wei")
    check("every op ran in a transaction from the key it was given",
          all(senders[hashes[item["wallet"]]] == sending.submitted.lane.bundler.address
              for item, (sending, *_) in zip(wallets, results) if sending and item["wallet"] in hashes))
    check(f"every bundle within the op and gas caps (≤ {bundler.BUNDLE_MAX_OPS} ops, ≤ {cap:,} gas)",
          all(sizes[h] <= bundler.BUNDLE_MAX_OPS and r["gasUsed"] <= cap for h, r in receipts.items()))
    check("each key sent only its bundles", all(
        w3.eth.get_transaction_count(k) - nonces[k] == sum(1 for h in receipts if senders[h] == k) for k in keys))
    check(f"{n} ops went out in {len(receipts)} transactions", sum(sizes.values()) == n,
          f"{sum(sizes.values())} ops in the receipts")

    wrong = []
    for item in wallets:
        after = effect(item)
        if not expected(item, item["before"], after):
            wrong.append((item["kind"], item["before"], after))
    done = Counter(item["kind"] for item in wallets)
    check(f"every transaction did what it was quoted to do ({', '.join(f'{k} ×{v}' for k, v in done.items())})",
          not wrong, str(wrong[:5]))

    listed_right = 0
    for item in wallets:
        rows = c.get(f"/api/transactions?chain_id={CHAIN_ID}&limit=20", headers=item["headers"]).json()["transactions"]
        ours = [t for t in rows if t["source"] == "assistant"]
        listed_right += (len(ours) == 1 and ours[0]["status"] == "confirmed"
                         and ours[0]["tx_hash"] == hashes.get(item["wallet"], "").lower()
                         and ours[0]["action"] == item["quote"]["action"])
    check(f"History lists each wallet's one transaction, confirmed, under the hash that ran it ({listed_right}/{n})",
          listed_right == n)

    net = sum(w3.eth.get_balance(k) - balances[k] for k in keys)
    check("the keys were repaid in full between them", net >= 0, f"net {net} wei")
    on_wire = sorted(r[2] for r in results if r[2] is not None)
    landed = sorted(r[3] for r in results)
    print(f"  INFO  all {n} at once: on the wire in {on_wire[len(on_wire) // 2]:.2f} s median, "
          f"{on_wire[-1]:.2f} s slowest; landed in {landed[len(landed) // 2]:.2f} s median, "
          f"{landed[-1]:.2f} s slowest; {wall:.1f} s for the lot")
    gas = sum(r["gasUsed"] for r in receipts.values())
    print(f"  INFO  {gas:,} gas for {n} ops: {gas // n:,} an op")

    if POOL_OUT:
        with open(POOL_OUT, "w") as f:
            json.dump({"network": NETWORK, "chain_id": CHAIN_ID, "wallets": [
                {"user_id": item["user_id"], "wallet": item["wallet"], "payee": item["payee"], "kind": item["kind"]}
                for item in wallets]}, f, indent=1)
        print(f"  INFO  wallets listed in {POOL_OUT}")


def eoa_call(acct, fn, value: int = 0):
    """Sends a contract call (or deployment) from a plain EOA on the fork and returns its receipt."""
    tx = fn.build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID, "value": value,
    })
    # Headroom over the estimate: the V3 seed mint ran out of gas at exactly the estimate on base-fork.
    tx["gas"] = tx["gas"] * 13 // 10
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction), timeout=60
    )
    assert receipt["status"] == 1, f"EOA call reverted: {receipt['transactionHash'].hex()}"
    return receipt


def spend_metered(sh, receipt) -> list[int]:
    """The netOutflowUsd of every SpendMetered the wallet's hook emitted in `receipt` (none = nothing charged)."""
    module = w3.eth.contract(
        address=sh.functions.SH_MODULE().call(),
        abi=db.get_json("./out/SpendingLimitModule.sol/SpendingLimitModule.json")["abi"],
    )
    return [
        e["args"]["netOutflowUsd"]
        for e in module.events.SpendMetered().process_receipt(receipt, errors=DISCARD)
        if e["args"]["account"] == sh.address
    ]


# The one position-manager function the V3 toolkit leaves out on purpose -- it never creates a
# pool -- and a test seeding a fresh token's pool needs.
_CREATE_POOL_ABI = [{
    "type": "function", "name": "createAndInitializePoolIfNecessary", "stateMutability": "payable",
    "inputs": [{"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
               {"name": "fee", "type": "uint24"}, {"name": "sqrtPriceX96", "type": "uint160"}],
    "outputs": [{"name": "pool", "type": "address"}],
}]
# The fee tier a seeded Uniswap V3 pool gets: 0.3%, whose ticks are 60 apart.
_SEED_FEE, _SEED_TICK_SPACING = 3000, 60


def seed_weth_pool(deployer, token, token_amount: int, eth_amount: int):
    """
    Gives a fresh token one TOKEN/WETH pool on the chain's exchange, seeded by `deployer` with
    `token_amount` of it against `eth_amount` of ETH -- which sets its price.

    On PancakeSwap V2 (BSC) that is the router's addLiquidityETH. On Uniswap V3 the pool is created
    and priced at that ratio, then given a full-range position, so it trades like the V2 pool at any
    price the tests move it to; the ETH goes in as value and the position manager wraps it.
    """
    eoa_call(deployer, token.functions.mint(deployer.address, token_amount))
    deadline = w3.eth.get_block("latest")["timestamp"] + 600
    if get_exchange(CHAIN_ID) != "uniswap-v3":
        router = w3.eth.contract(address=get_router(CHAIN_ID), abi=router_abi)
        eoa_call(deployer, token.functions.approve(router.address, token_amount))
        eoa_call(deployer, router.functions.addLiquidityETH(
            token.address, token_amount, 0, 0, deployer.address, deadline,
        ), value=eth_amount)
        return
    v3 = get_v3_network(CHAIN_ID)
    manager = w3.eth.contract(address=v3["position_manager"], abi=position_manager_abi + _CREATE_POOL_ABI)
    weth = v3["native_wrapped"]
    token0, token1 = sorted((token.address, weth), key=lambda a: int(a, 16))
    amount0, amount1 = (token_amount, eth_amount) if token0 == token.address else (eth_amount, token_amount)
    # sqrtPriceX96 = sqrt(amount1 / amount0) * 2**96, in integers.
    eoa_call(deployer, manager.functions.createAndInitializePoolIfNecessary(
        token0, token1, _SEED_FEE, math.isqrt(amount1 * 2**192 // amount0),
    ))
    eoa_call(deployer, token.functions.approve(manager.address, token_amount))
    tick_lower, tick_upper = v3math.full_range_ticks(_SEED_TICK_SPACING)
    mint = manager.encode_abi("mint", args=[(
        token0, token1, _SEED_FEE, tick_lower, tick_upper, amount0, amount1, 0, 0, deployer.address, deadline,
    )])
    refund = manager.encode_abi("refundETH", args=[])
    eoa_call(deployer, manager.functions.multicall([bytes.fromhex(mint[2:]), bytes.fromhex(refund[2:])]),
             value=eth_amount)


def test_custom_tokens(c: TestClient, acct, headers: dict, wallet: str):
    """
    A token the user adds by address, on a real chain: a brand-new ERC-20 with no price feed and one
    TOKEN/WETH pool on the chain's exchange (seed_weth_pool), the usual shape. Buying it with ETH
    goes straight through that pool; paying in USDC hops through WETH (the toolkit's own routing,
    on Uniswap V3 and PancakeSwap V2 alike).

    Covers the add rules against real contracts, the wallet read, the assistant sending it (not
    charged), buying it with ETH (the FULL amount charged, exactly), the assistant refusing a token
    nobody added and a slippage over 12%, selling it back (not charged, and its unpriced approval
    clears because the router is a trusted spender), the owner withdrawing it by address, and
    removing it from the list.
    """
    print("\n[7b] tokens the user adds: added by address, sent, bought with ETH, sold, withdrawn")
    sh = w3.eth.contract(address=wallet, abi=db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    turn = itertools.count(1_000)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    def confirm(quoted: dict):
        """The user approves the card with their passkey; it is sent and lands."""
        result = approve_and_send(user_id, quoted["quote_id"])
        return w3.eth.get_transaction_receipt("0x" + result.split("`")[1].removeprefix("0x"))

    # A fresh token nobody prices, and a fake one calling itself USDC.
    deployer = new_funded_account()
    mock = db.get_json("./out/ERC20Mock.sol/ERC20Mock.json")
    factory = w3.eth.contract(abi=mock["abi"], bytecode=mock["bytecode"]["object"])
    symbol = f"MF{int(time.time()) % 100_000}"
    token_address = eoa_call(deployer, factory.constructor("Mitfah Test Token", symbol, 18))["contractAddress"]
    fake_usdc = eoa_call(deployer, factory.constructor("USD Coin", "USDC", 6))["contractAddress"]
    token = w3.eth.contract(address=token_address, abi=mock["abi"])
    unit = 10**18
    eoa_call(deployer, token.functions.mint(wallet, 1_000 * unit))

    ticker = symbol.lower()
    usdc = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
    r = c.post("/api/tokens/custom/lookup", headers=headers, json={"chain_id": CHAIN_ID, "address": usdc})
    check("the real USDC comes back as the listed token, not an added one",
          r.status_code == 200 and (r.json()["listed"], r.json()["ticker"]) == (True, "usdc"), f"{r.status_code} {r.text[:160]}")
    for label, address, expected in (
        ("a token copying a listed symbol is refused", fake_usdc, "fake a token"),
        ("an address with no contract asks to check it again", Account.create().address, "Check the token address again"),
        # The exchange's router: a contract, but with no symbol() or decimals().
        ("a contract that isn't a token asks to check it again", get_default_spenders(CHAIN_ID)[0],
         "Check the token address again"),
    ):
        r = c.post("/api/tokens/custom/lookup", headers=headers, json={"chain_id": CHAIN_ID, "address": address})
        check(label, r.status_code == 400 and expected in r.json()["detail"], f"{r.status_code} {r.text[:160]}")

    r = c.post("/api/tokens/custom/lookup", headers=headers, json={"chain_id": CHAIN_ID, "address": token_address.lower()})
    check("lookup reads the new token off the chain", r.status_code == 200 and r.json()["ticker"] == ticker
          and r.json()["balance_raw"] == str(1_000 * unit), f"{r.status_code} {r.text[:200]}")
    check("...showing its symbol and decimals, which make it an ERC-20",
          (r.json().get("symbol"), r.json().get("decimals")) == (symbol, 18), r.text[:200])
    r = c.post("/api/tokens/custom", headers=headers, json={"chain_id": CHAIN_ID, "address": token_address})
    check("the token is added", r.status_code == 201, f"{r.status_code} {r.text[:160]}")

    balances = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers).json()["balances"]
    row = next((b for b in balances if b["address"] == token_address), None)
    check("the wallet read shows it, marked custom, with its balance",
          row is not None and row["custom"] and row["raw"] == str(1_000 * unit) and row["ticker"] == ticker, str(row))
    check("listed tokens are marked not custom", all(b["custom"] is False for b in balances if not b["native"] and b is not row))
    check("the assistant lists it as custom", ticker in tools.wallet_reads.get_supported_tokens.func(next_turn())["custom"])

    try:
        tools.wallet_reads.get_price.func(next_turn(), token=ticker)
        check("get_price refuses it by name", False, "it answered a price")
    except ToolException as e:
        check("get_price refuses it by name", "no price" in str(e), str(e)[:160])
    pre = tools.checks.preflight_check.func(next_turn(), token=ticker, amount=10)
    check("preflight: no USD value, nothing charged, passes",
          pre["usd_value"] is None and pre["charged_usd"] == 0 and pre["within_budget"], str(pre))

    # The assistant sends it to a contact: not charged.
    payee = c.get("/api/contacts", headers=headers).json()["contacts"]
    payee = next(p for p in payee if p["name"] == "payee")["address"]
    quoted = tools.transfers.transfer_erc20.func(next_turn(), token=ticker,
                                       recipient="payee", amount=10)
    check("the transfer quote says it isn't covered", "isn't covered by the spending limit" in quoted.get("details", ""),
          str(quoted.get("details")))
    receipt = confirm(quoted)
    check("the contact received it", token.functions.balanceOf(payee).call() == 10 * unit)
    check("and nothing was charged to the limit", spend_metered(sh, receipt) == [], str(spend_metered(sh, receipt)))

    # One TOKEN/WETH pool on the chain's exchange, seeded from the deployer.
    seed_weth_pool(deployer, token, 1_000_000 * unit, w3.to_wei(1, "ether"))

    weth = Web3.to_checksum_address(get_token_address(CHAIN_ID, WRAPPED))
    quote = tools.uniswap.get_quote_out.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001)
    check("buying it with ETH goes through its WETH pool",
          [Web3.to_checksum_address(a) for a in quote["path"]] == [weth, token_address], str(quote.get("path")))
    quote = tools.uniswap.get_quote_out.func(next_turn(), token_in="usdc", token_out=ticker, amount_in=1)
    check("paying in USDC hops through WETH",
          [Web3.to_checksum_address(a) for a in quote["path"]] == [usdc, weth, token_address], str(quote.get("path")))

    held = token.functions.balanceOf(wallet).call()
    quoted = tools.uniswap.swap.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001, slippage_bps=500)
    check("the buy quote says the full amount paid counts", "full amount" in quoted.get("details", ""),
          str(quoted.get("details")))
    check("...and shows what should come back, the least it will accept, and the tolerance",
          all(part in quoted.get("details", "") for part in ("received: about", "at least", "slippage tolerance 5%")),
          str(quoted.get("details")))
    expected = sh.functions.getUsdValue(ETH_SENTINEL, w3.to_wei("0.001", "ether")).call()
    check("...and counts the full ETH paid toward the limit",
          abs(quoted["charged_usd"] - expected / 10**18) < 1e-9, f'{quoted.get("charged_usd")} vs {expected / 10**18}')
    receipt = confirm(quoted)
    check("the wallet received the token", token.functions.balanceOf(wallet).call() > held)
    charged = spend_metered(sh, receipt)
    check("the limit was charged exactly the ETH paid", charged == [expected], f"{charged} vs {expected}")

    # Only tokens the owner chose, within 12% slippage. Both refusals happen before anything is
    # built or priced, so neither costs the wallet a thing.
    sent_before = sent_by_bundlers()
    try:
        tools.uniswap.swap.func(next_turn(), token_in="eth", token_out=fake_usdc, amount_in=0.001)
        check("a token the user never added is refused by address", False, "it was quoted")
    except ToolException as e:
        check("a token the user never added is refused by address",
              "not a token this wallet knows" in str(e), str(e)[:160])
    quote = tools.uniswap.get_quote_out.func(next_turn(), token_in="eth", token_out=token_address, amount_in=0.001)
    check("the user's own token still works by address",
          [Web3.to_checksum_address(a) for a in quote["path"]] == [weth, token_address], str(quote.get("path")))
    try:
        tools.uniswap.swap.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001, slippage_bps=1201)
        check("a slippage over 12% is refused", False, "it was quoted")
    except ToolException as e:
        check("a slippage over 12% is refused", "outside what this wallet allows" in str(e), str(e)[:160])
    quoted = tools.uniswap.swap.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001, slippage_bps=1200)
    check("exactly 12% is quoted", "NOT SENT" in quoted["status"], str(quoted.get("status")))
    tools.sending.cancel_transaction.func(next_turn(), quote_id=quoted["quote_id"])
    check("...and none of that sent anything", sent_by_bundlers() == sent_before)

    # Selling it back: the unpriced approval clears (the router is a trusted spender), nothing charged.
    native_before = w3.eth.get_balance(wallet)
    quoted = tools.uniswap.swap.func(next_turn(), token_in=ticker, token_out="eth", amount_in=100, slippage_bps=500)
    check("the sell quote says selling costs nothing", "selling it costs nothing" in quoted.get("details", ""),
          str(quoted.get("details")))
    check("...counts nothing toward the limit and states no price for it",
          quoted["charged_usd"] == 0 and quoted["usd_value"] is None, f'{quoted.get("charged_usd")} / {quoted.get("usd_value")}')
    receipt = confirm(quoted)
    check("selling it lands", receipt["status"] == 1)
    check("and charges nothing to the limit", spend_metered(sh, receipt) == [], str(spend_metered(sh, receipt)))
    check("the ETH came back to the wallet (less gas)", w3.eth.get_balance(wallet) > native_before - w3.to_wei("0.01", "ether"))

    # The owner withdraws it by address.
    sink = Account.create().address
    owner_action(c, headers, acct, "/api/wallet/withdraw/prepare", {"token": token_address, "amount": "5", "to": sink})
    check("the owner withdrew it by address", token.functions.balanceOf(sink).call() == 5 * unit)

    r = c.delete(f"/api/tokens/custom/{CHAIN_ID}/{token_address}", headers=headers)
    check("the token is removed from the list", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    balances = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers).json()["balances"]
    check("the wallet read no longer shows it", all(b["address"] != token_address for b in balances))
    try:
        tools.transfers.transfer_erc20.func(next_turn(), token=ticker,
                                  recipient="payee", amount=1)
        check("the assistant no longer recognises it", False, "it quoted a transfer")
    except ToolException as e:
        check("the assistant no longer recognises it", "Add token" in str(e), str(e)[:160])


def test_swaps_and_liquidity(c: TestClient, acct, headers: dict, wallet: str):
    """
    The one swap tool and the chain's liquidity tools on a real chain. Where it trades on Uniswap
    V3 (Ethereum, Sepolia, Arbitrum, Base), _swaps_and_positions: every swap route against the
    chain's own ETH/USDC pools, and a position from its range options to its burn. On BSC,
    _swaps_and_lp_tokens: PancakeSwap V2's swap routes and LP tokens.

    Then, on either, the refusals that need a real balance: sending more of the native asset than
    the wallet holds, and sending nearly all of it, which leaves nothing for the fees.
    """
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    turn = itertools.count(2_000_000)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    def confirm(quoted: dict):
        """The user approves the card with their passkey; it is sent and lands."""
        result = approve_and_send(user_id, quoted["quote_id"])
        return w3.eth.get_transaction_receipt("0x" + result.split("`")[1].removeprefix("0x"))

    if get_exchange(CHAIN_ID) == "uniswap-v3":
        _swaps_and_positions(c, headers, wallet, next_turn, confirm)
    else:
        _swaps_and_lp_tokens(c, acct, headers, wallet, next_turn, confirm)

    # The native asset has to cover what is sent and the fees. Both refusals happen before anything
    # is signed, so neither costs the wallet a thing. The limit is raised for them first: nearly all
    # the ETH is worth more than the $1,234 set in [3], and that refusal would come first.
    sh = w3.eth.contract(address=wallet, abi=db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    limit = read_spending_config(sh)["dailyLimitUsd"] // 10**18
    owner_action(c, headers, acct, "/api/wallet/daily-limit/prepare", {"daily_limit_usd": 100_000})
    sent_before = sent_by_bundlers()
    balance = w3.eth.get_balance(wallet)
    ticker_native = get_native_asset_ticker(CHAIN_ID)
    try:
        tools.transfers.send_eth.func(next_turn(), recipient="payee", amount_eth=balance / 10**18 + 1)
        check("sending more than the wallet holds is refused", False, "it was quoted")
    except ToolException as e:
        check("sending more than the wallet holds is refused, with what it holds",
              f"Not enough {ticker_native}: the wallet holds" in str(e) and "Nothing was sent" in str(e), str(e)[:200])
    fee = load_registry(user_id).functions.getFee().call()
    try:
        tools.transfers.send_eth.func(next_turn(), recipient="payee", amount_eth=(balance - fee // 2) / 10**18)
        check("sending nearly all of it is refused", False, "it was quoted")
    except ToolException as e:
        check("sending nearly all of it is refused: too little left for the fees",
              "leaves too little for the fees" in str(e), str(e)[:200])
    pre = tools.checks.preflight_check.func(next_turn(), token="eth", amount=balance / 10**18 + 1)
    check("preflight says the wallet doesn't hold it", pre["enough_balance"] is False
          and "the wallet holds" in pre.get("balance_short", ""), str(pre)[:200])
    check("...and none of that sent anything", sent_by_bundlers() == sent_before)
    owner_action(c, headers, acct, "/api/wallet/daily-limit/prepare", {"daily_limit_usd": limit})
    check("the limit is back as it was", read_spending_config(sh)["dailyLimitUsd"] == limit * 10**18)


def _swaps_and_lp_tokens(c: TestClient, acct, headers: dict, wallet: str, next_turn, confirm):
    """
    PancakeSwap V2 (BSC): the one swap tool and the two liquidity tools, by every route they can
    take: a fresh token with one TOKEN/WETH pool, as in [7b]. Every swap route is quoted and checked
    to have reached its own router function -- the quote's action is the router call's own
    description -- and the routes never sent before are sent and read back from the chain. Both
    forms of adding and of removing liquidity are sent: with the native asset (the default) and with
    WETH.

    The dashboard follows the pool's LP tokens through it all: they show after the first deposit,
    match the chain after every change, go when the owner withdraws them all, come back with the
    next deposit, and go again when the assistant takes it all out.
    """
    print("\n[7c] one swap tool, one add and one remove: every route, LP tokens on the dashboard, and the balance refusals")
    # A fresh token, the wallet holding 10,000 of it, and one TOKEN/WETH pool: 1,000,000 TOKEN
    # against 1 ETH, so 1 TOKEN is worth 0.000001 ETH.
    deployer = new_funded_account()
    mock = db.get_json("./out/ERC20Mock.sol/ERC20Mock.json")
    factory = w3.eth.contract(abi=mock["abi"], bytecode=mock["bytecode"]["object"])
    symbol = f"MS{int(time.time()) % 100_000}"
    token_address = eoa_call(deployer, factory.constructor("Mitfah Swap Token", symbol, 18))["contractAddress"]
    token = w3.eth.contract(address=token_address, abi=mock["abi"])
    unit = 10**18
    ticker = symbol.lower()
    eoa_call(deployer, token.functions.mint(wallet, 10_000 * unit))
    router = w3.eth.contract(address=get_router(CHAIN_ID), abi=router_abi)
    eoa_call(deployer, token.functions.mint(deployer.address, 1_000_000 * unit))
    eoa_call(deployer, token.functions.approve(router.address, 1_000_000 * unit))
    eoa_call(deployer, router.functions.addLiquidityETH(
        token_address, 1_000_000 * unit, 0, 0, deployer.address,
        w3.eth.get_block("latest")["timestamp"] + 600,
    ), value=w3.to_wei(1, "ether"))
    r = c.post("/api/tokens/custom", headers=headers, json={"chain_id": CHAIN_ID, "address": token_address})
    check("the token is added", r.status_code == 201, f"{r.status_code} {r.text[:160]}")

    weth = w3.eth.contract(address=Web3.to_checksum_address(get_token_address(CHAIN_ID, WRAPPED)), abi=ERC20_ABI)
    weth_before = weth.functions.balanceOf(wallet).call()
    check("wrapping 0.01 ETH lands", confirm(tools.transfers.wrap_eth.func(next_turn(), amount_eth=0.01))["status"] == 1)
    check("...and the wallet holds the WETH", weth.functions.balanceOf(wallet).call() == weth_before + 10**16)

    # Every route, by the router call it reached: exact in says "for at least", exact out "Swap up
    # to ... for exactly", and the native side is "the native asset".
    def route(action: str) -> tuple[str, str, str]:
        exact = "in" if "for at least" in action else "out" if action.startswith("Swap up to") else "?"
        spends = "native" if "of the native asset for" in action else "token"
        gets = "native" if action.endswith("of the native asset") else "token"
        return exact, spends, gets

    routes = (
        ("ETH for the token, exact in", dict(token_in="eth", token_out=ticker, amount_in=0.0001), ("in", "native", "token")),
        ("ETH for the token, exact out", dict(token_in="eth", token_out=ticker, amount_out=100), ("out", "native", "token")),
        ("the token for ETH, exact in", dict(token_in=ticker, token_out="eth", amount_in=100), ("in", "token", "native")),
        ("the token for ETH, exact out", dict(token_in=ticker, token_out="eth", amount_out=0.0001), ("out", "token", "native")),
        ("the token for WETH, exact in", dict(token_in=ticker, token_out=WRAPPED, amount_in=100), ("in", "token", "token")),
        ("WETH for the token, exact out", dict(token_in=WRAPPED, token_out=ticker, amount_out=100), ("out", "token", "token")),
    )
    quoted = {}
    for label, args, expected in routes:
        quoted[label] = tools.uniswap.swap.func(next_turn(), **args)
        check(f"swap, {label}: quoted through its own router function",
              route(quoted[label]["action"]) == expected, quoted[label]["action"])
    check("a quote's action is the swap itself, without the approvals around it",
          quoted["the token for ETH, exact out"]["action"].startswith("Swap up to")
          and "Approve" not in quoted["the token for ETH, exact out"]["action"],
          quoted["the token for ETH, exact out"]["action"])
    check("an exact-out quote says what it should cost and the most it will pay",
          all(part in quoted["ETH for the token, exact out"].get("details", "") for part in ("spent: about", "at most")),
          str(quoted["ETH for the token, exact out"].get("details")))

    # The routes [7b] never sent: each exact-out one, and token for token both ways.
    held = token.functions.balanceOf(wallet).call()
    check("buying exactly 100 of the token with ETH lands", confirm(quoted["ETH for the token, exact out"])["status"] == 1)
    check("...and exactly 100 arrived", token.functions.balanceOf(wallet).call() == held + 100 * unit)
    for label in ("ETH for the token, exact in", "the token for ETH, exact in"):
        tools.sending.cancel_transaction.func(next_turn(), quote_id=quoted[label]["quote_id"])
    # Quoted before the buy above moved the pool, so these are re-quoted at the price it left.
    held = token.functions.balanceOf(wallet).call()
    receipt = confirm(tools.uniswap.swap.func(next_turn(), token_in=ticker, token_out="eth", amount_out=0.0001))
    check("selling the token for exactly 0.0001 ETH lands", receipt["status"] == 1)
    check("...spending some of the token", token.functions.balanceOf(wallet).call() < held)
    held, weth_held = token.functions.balanceOf(wallet).call(), weth.functions.balanceOf(wallet).call()
    check("selling exactly 100 of the token for WETH lands",
          confirm(tools.uniswap.swap.func(next_turn(), token_in=ticker, token_out=WRAPPED, amount_in=100))["status"] == 1)
    check("...100 left and WETH came back", token.functions.balanceOf(wallet).call() == held - 100 * unit
          and weth.functions.balanceOf(wallet).call() > weth_held)
    held, weth_held = token.functions.balanceOf(wallet).call(), weth.functions.balanceOf(wallet).call()
    check("buying exactly 100 of the token with WETH lands",
          confirm(tools.uniswap.swap.func(next_turn(), token_in=WRAPPED, token_out=ticker, amount_out=100))["status"] == 1)
    check("...100 arrived and WETH paid for it", token.functions.balanceOf(wallet).call() == held + 100 * unit
          and weth.functions.balanceOf(wallet).call() < weth_held)
    for label in ("the token for ETH, exact out", "the token for WETH, exact in", "WETH for the token, exact out"):
        tools.sending.cancel_transaction.func(next_turn(), quote_id=quoted[label]["quote_id"])

    # Liquidity: the native asset by default, WETH when named. The dashboard's row for the pool is
    # checked against the chain after each step.
    native = get_native_asset_ticker(CHAIN_ID).lower()
    v2_factory = w3.eth.contract(address=router.functions.factory().call(), abi=factory_abi)
    pool = w3.eth.contract(address=v2_factory.functions.getPair(token_address, weth.address).call(), abi=pair_abi)

    def lp_rows() -> list[dict]:
        r = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers)
        assert r.status_code == 200, f"wallet read failed: {r.status_code} {r.text[:250]}"
        return [b for b in r.json()["balances"] if b.get("lp")]

    def shows_pool(label: str):
        rows = lp_rows()
        balance, supply = pool.functions.balanceOf(wallet).call(), pool.functions.totalSupply().call()
        reserves = dict(zip((pool.functions.token0().call(), pool.functions.token1().call()),
                            pool.functions.getReserves().call()[:2]))
        expected = {
            "ticker": f"{native}/{ticker} lp", "address": pool.address, "raw": str(balance),
            "underlying": [{"ticker": native, "decimals": 18, "raw": str(balance * reserves[weth.address] // supply)},
                           {"ticker": ticker, "decimals": 18, "raw": str(balance * reserves[token_address] // supply)}],
        }
        check(label, len(rows) == 1 and {k: rows[0].get(k) for k in expected} == expected,
              f"{rows} vs {expected}"[:400])

    check("the dashboard shows no LP tokens before the first deposit", lp_rows() == [])
    try:
        tools.uniswap.add_liquidity.func(next_turn(), token_a="eth", amount_a=0.001, token_b=ticker)
        check("adding with the native amount fixed is refused", False, "it was quoted")
    except ToolException as e:
        check("adding with the native amount fixed is refused, asking for the token's amount first",
              f"Name how much {ticker.upper()} to deposit first" in str(e), str(e)[:200])
    quote = tools.uniswap.add_liquidity.func(next_turn(), token_a=ticker, amount_a=1000)
    check("adding liquidity pairs with the native asset by default", quote["action"].endswith("of the native asset"),
          quote["action"])
    check("...counting the ETH deposited, not the token, which has no price",
          quote["charged_usd"] > 0 and quote["usd_value"] is None, f'{quote.get("charged_usd")} / {quote.get("usd_value")}')
    held = token.functions.balanceOf(wallet).call()
    check("...and it lands", confirm(quote)["status"] == 1)
    lp_native = tools.uniswap.get_liquidity_token_balance.func(next_turn(), token_a=ticker)
    check("the wallet holds LP tokens for the pool, and the token went in",
          lp_native > 0 and token.functions.balanceOf(wallet).call() == held - 1000 * unit, str(lp_native))
    shows_pool("the dashboard shows them, native side first, with what they hold in the pool")
    r = c.get(f"/api/wallet/{CHAIN_ID}/allowlist", headers=headers)
    suggested = {s["address"]: s["label"] for s in r.json()["suggested"]} if r.status_code == 200 else {}
    check("...and the contract allowlist suggests the pool, which taking liquidity out calls",
          suggested.get(pool.address) == f"{native.upper()}/{ticker.upper()} pool", f"{r.status_code} {suggested}"[:300])
    quote = tools.uniswap.add_liquidity.func(next_turn(), token_a=ticker, amount_a=1000, token_b=WRAPPED)
    check("naming WETH deposits WETH instead", "native asset" not in quote["action"], quote["action"])
    weth_held = weth.functions.balanceOf(wallet).call()
    check("...and it lands, paying in WETH", confirm(quote)["status"] == 1
          and weth.functions.balanceOf(wallet).call() < weth_held)
    lp = tools.uniswap.get_liquidity_token_balance.func(next_turn(), token_a=ticker, token_b=WRAPPED)
    check("...into the same pool, so the LP tokens add up", lp > lp_native, f"{lp} vs {lp_native}")
    shows_pool("...and the dashboard has one row for the pool, with the new total")

    held, native_held = token.functions.balanceOf(wallet).call(), w3.eth.get_balance(wallet)
    quote = tools.uniswap.remove_liquidity.func(next_turn(), token_a=ticker, lp_amount=round(lp / 4, 6))
    check("removing liquidity returns the native asset by default", "native asset" in quote["action"],
          quote["action"])
    check("...shows what should come back and the least it will accept",
          all(part in quote.get("details", "") for part in ("returned: about", "at least")), str(quote.get("details")))
    check("...and it lands, the token coming back", confirm(quote)["status"] == 1
          and token.functions.balanceOf(wallet).call() > held)
    weth_held = weth.functions.balanceOf(wallet).call()
    quote = tools.uniswap.remove_liquidity.func(next_turn(), token_a=ticker, lp_amount=round(lp / 4, 6), token_b=WRAPPED)
    check("naming WETH takes WETH back instead", "native asset" not in quote["action"], quote["action"])
    check("...and it lands, WETH coming back", confirm(quote)["status"] == 1
          and weth.functions.balanceOf(wallet).call() > weth_held)
    check("half the LP tokens are left", tools.uniswap.get_liquidity_token_balance.func(next_turn(), token_a=ticker) < lp)
    shows_pool("...and the dashboard shows what is left")

    held = pool.functions.balanceOf(wallet).call()
    owner_action(c, headers, acct, "/api/wallet/withdraw/prepare",
                 {"token": pool.address, "amount": str(Decimal(held) / Decimal(10**18)), "to": acct.address})
    check("the owner can withdraw LP tokens like any other token", pool.functions.balanceOf(wallet).call() == 0
          and pool.functions.balanceOf(acct.address).call() >= held)
    check("...and with none left, the pool is gone from the dashboard", lp_rows() == [])
    check("a new deposit lands", confirm(tools.uniswap.add_liquidity.func(next_turn(), token_a=ticker, amount_a=500))["status"] == 1)
    shows_pool("...and brings the pool back to the dashboard")

    # "All of it", as the assistant asks for it: the balance it reads, in whole units. That passes
    # through a float, which the package scales back up to a few wei more than the wallet holds
    # (refused) or a few wei less (accepted, leaving them behind) -- about half the time each. The
    # largest amount it accepts is what lands, and the few wei it leaves must not keep the row.
    everything = tools.uniswap.get_liquidity_token_balance.func(next_turn(), token_a=ticker)
    held = pool.functions.balanceOf(wallet).call()
    while int(Decimal(str(everything)) * 10**18) > held:
        everything = math.nextafter(everything, 0)
    check("the assistant taking it all out lands",
          confirm(tools.uniswap.remove_liquidity.func(next_turn(), token_a=ticker, lp_amount=everything))["status"] == 1)
    check("...leaving at most a few wei", pool.functions.balanceOf(wallet).call() < 1000,
          str(pool.functions.balanceOf(wallet).call()))
    check("...which the dashboard counts as none: the pool is gone", lp_rows() == [])


def _swaps_and_positions(c: TestClient, headers: dict, wallet: str, next_turn, confirm):
    """
    Uniswap V3 (Ethereum, Sepolia, Arbitrum, Base), against the chain's own ETH/USDC pools: the one
    swap tool by each of its six router functions, sent and read back from the chain, then a
    position's whole life -- the range options, a position opened on one of them, listed by the
    assistant and on the dashboard, its fees, half of it taken out, then the rest, which burns it --
    and the History rows that leaves.

    Every USDC amount is set from what 0.001 ETH buys on the pool, never in dollars: a testnet
    pool's price can be anything, and amounts of a milli-ETH or two cost little on any fork.
    """
    print("\n[7c] Uniswap V3: every swap route on ETH/USDC, a position from its range options to its burn, and the "
          "balance refusals")
    sh = w3.eth.contract(address=wallet, abi=db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    manager = w3.eth.contract(address=get_v3_network(CHAIN_ID)["position_manager"], abi=position_manager_abi)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc")), abi=ERC20_ABI)
    weth = w3.eth.contract(address=Web3.to_checksum_address(get_token_address(CHAIN_ID, WRAPPED)), abi=ERC20_ABI)
    usdc_unit = 10 ** usdc.functions.decimals().call()

    def raw(amount: float) -> int:
        """Whole USDC in base units, converted exactly as the toolkit converts them."""
        return int(Decimal(str(amount)) * usdc_unit)

    def held() -> tuple[int, int]:
        return usdc.functions.balanceOf(wallet).call(), weth.functions.balanceOf(wallet).call()

    def state() -> dict:
        r = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers)
        assert r.status_code == 200, f"wallet read failed: {r.status_code} {r.text[:250]}"
        return r.json()

    per_milli_eth = tools.uniswap.get_quote_out.func(
        next_turn(), token_in="eth", token_out="usdc", amount_in=0.001
    )["amount_out"]
    small = round(per_milli_eth / 2, 6)  # about half a milli-ETH's worth
    check("the chain's ETH/USDC pool prices a milli-ETH", small > 0, str(per_milli_eth))
    if small <= 0:
        return
    deal_erc20(usdc.address, wallet, max(1_000 * usdc_unit, raw(round(per_milli_eth * 20, 6))))
    check("wrapping 0.002 ETH lands", confirm(tools.transfers.wrap_eth.func(next_turn(), amount_eth=0.002))["status"] == 1)

    # Every route, by the router function it reached: exact in says "for at least", exact out "Swap
    # up to ... for exactly", and the native side is "the native asset". The description ends by
    # naming the pool or pools it goes through, "(Uniswap V3, 0.05% pool)"; that part is dropped first.
    def route(action: str) -> tuple[str, str, str]:
        action = action.split(" (Uniswap V3")[0]
        exact = "in" if "for at least" in action else "out" if action.startswith("Swap up to") else "?"
        spends = "native" if "of the native asset for" in action else "token"
        gets = "native" if action.endswith("of the native asset") else "token"
        return exact, spends, gets

    def moved_as(delta: int, rule) -> bool:
        """rule: the exact change, or "+" (rose), "-" (fell) or "=" (unchanged)."""
        return delta == rule if isinstance(rule, int) else {"+": delta > 0, "-": delta < 0, "=": delta == 0}[rule]

    # (label, swap arguments, the route it must take, how USDC moves, how WETH moves). Each is quoted
    # and sent in turn, so each is priced where the one before left the pool. The native side never
    # touches the wallet's WETH: the router wraps what comes in and unwraps what goes out.
    routes = (
        ("ETH for USDC, exact in", dict(token_in="eth", token_out="usdc", amount_in=0.001),
         ("in", "native", "token"), "+", "="),
        ("ETH for USDC, exact out", dict(token_in="eth", token_out="usdc", amount_out=small),
         ("out", "native", "token"), raw(small), "="),
        ("USDC for ETH, exact in", dict(token_in="usdc", token_out="eth", amount_in=small),
         ("in", "token", "native"), -raw(small), "="),
        ("USDC for ETH, exact out", dict(token_in="usdc", token_out="eth", amount_out=0.0005),
         ("out", "token", "native"), "-", "="),
        ("USDC for WETH, exact in", dict(token_in="usdc", token_out=WRAPPED, amount_in=small),
         ("in", "token", "token"), -raw(small), "+"),
        ("WETH for USDC, exact out", dict(token_in=WRAPPED, token_out="usdc", amount_out=small),
         ("out", "token", "token"), raw(small), "-"),
    )
    for label, args, expected, usdc_rule, weth_rule in routes:
        quoted = tools.uniswap.swap.func(next_turn(), **args)
        check(f"swap, {label}: quoted through its own router function, on Uniswap V3",
              route(quoted["action"]) == expected and "(Uniswap V3" in quoted["action"], quoted["action"])
        if label == "USDC for ETH, exact out":
            check("a quote's action is the swap itself, without the approvals around it",
                  quoted["action"].startswith("Swap up to") and "Approve" not in quoted["action"], quoted["action"])
        if label == "ETH for USDC, exact out":
            check("an exact-out quote says what it should cost and the most it will pay",
                  all(part in quoted.get("details", "") for part in ("spent: about", "at most")),
                  str(quoted.get("details")))
        usdc_before, weth_before = held()
        check(f"swap, {label}: it lands", confirm(quoted)["status"] == 1)
        usdc_after, weth_after = held()
        check(f"swap, {label}: USDC moved as it should ({usdc_rule}), and WETH ({weth_rule})",
              moved_as(usdc_after - usdc_before, usdc_rule) and moved_as(weth_after - weth_before, weth_rule),
              f"USDC {usdc_after - usdc_before:+}, WETH {weth_after - weth_before:+}")

    # The other exchange's liquidity tools are hidden from the model here; called anyway, they refuse.
    try:
        tools.uniswap.add_liquidity.func(next_turn(), token_a="usdc", amount_a=1)
        check("PancakeSwap V2's add_liquidity is refused on a Uniswap V3 chain", False, "it was quoted")
    except ToolException as e:
        check("PancakeSwap V2's add_liquidity is refused on a Uniswap V3 chain, pointing at the position tools",
              "suggest_price_ranges" in str(e), str(e)[:200])

    # A position, from the range options to its burn.
    listed = tools.positions.get_positions.func(next_turn())
    check("the assistant finds no positions before the first", listed["positions"] == [], str(listed)[:200])
    nfts_before = manager.functions.balanceOf(wallet).call()

    offered = tools.positions.suggest_price_ranges.func(next_turn(), token_a="usdc")
    check("the range options for USDC and the native asset are ETH/USDC's, priced in USDC per ETH",
          offered["pair"] == "ETH/USDC" and offered["current_price"].endswith("USDC per ETH"), str(offered)[:300])
    check("...narrow, medium and wide bands of 5%, 15% and 50% -- not a stablecoin pair -- and the full range",
          [(o["range"], o["width"]) for o in offered["options"]]
          == [("narrow", "±5% around today's price"), ("medium", "±15% around today's price"),
              ("wide", "±50% around today's price"), ("full", "every price")],
          str([(o["range"], o["width"]) for o in offered["options"]]))

    # The wide band: today's price stays inside it whatever the fork does meanwhile, so the deposit
    # takes both tokens, about half and half by value.
    quoted = tools.positions.open_position.func(
        next_turn(), token_a="usdc", amount_a=round(per_milli_eth * 2, 6), range="wide"
    )
    check("opening a position on the wide option quotes a mint, nothing sent",
          quoted["action"].startswith("Open a Uniswap V3 position") and "NOT SENT" in quoted["status"],
          f'{quoted.get("action")} / {quoted.get("status")}')
    check("...on the range that was shown, with today's price inside it",
          "(the wide option)" in quoted.get("details", "") and "inside the range" in quoted.get("details", ""),
          str(quoted.get("details")))
    check("...counting the ETH deposited toward the limit", quoted["charged_usd"] > 0, str(quoted.get("charged_usd")))
    usdc_before, _ = held()
    receipt = confirm(quoted)
    sent = {"open": (receipt, quoted["action"], "out")}
    check("...and it lands", receipt["status"] == 1)
    check("...the USDC going in", held()[0] < usdc_before)
    charged = spend_metered(sh, receipt)
    check("...charged to the limit: the position that comes back has no price", charged and charged[0] > 0, str(charged))
    check("the wallet holds one more position NFT", manager.functions.balanceOf(wallet).call() == nfts_before + 1)

    listed = tools.positions.get_positions.func(next_turn())["positions"]
    check("the assistant lists it: ETH/USDC, in range, with its range in USDC per ETH",
          len(listed) == 1 and listed[0]["pair"] == "ETH/USDC" and listed[0]["in_range"] is True
          and listed[0]["range"].endswith("USDC per ETH"), str(listed)[:400])
    if len(listed) != 1:
        return
    position_id = listed[0]["position_id"]
    check("...under the id of the NFT the wallet now holds",
          manager.functions.tokenOfOwnerByIndex(wallet, nfts_before).call() == position_id
          and manager.functions.ownerOf(position_id).call() == wallet, str(position_id))
    liquidity = manager.functions.positions(position_id).call()[7]
    check("...holding liquidity", liquidity > 0, str(liquidity))

    wallet_state = state()
    rows = [p for p in wallet_state["positions"] if p["id"] == position_id]
    check("the dashboard shows it: ETH/USDC, in range, ETH side first, holding both",
          len(rows) == 1 and rows[0]["pair"] == "eth/usdc" and rows[0]["in_range"] is True
          and rows[0]["full_range"] is False and [a["ticker"] for a in rows[0]["amounts"]] == ["eth", "usdc"]
          and all(int(a["raw"]) > 0 for a in rows[0]["amounts"]), str(rows)[:400])
    check("...as a position, not an LP token", not [b for b in wallet_state["balances"] if b.get("lp")])

    # Fees: a position earns only from swaps that cross it after it opened, and on a fork nothing
    # else trades, so it has usually earned nothing -- and collecting nothing is refused.
    fees = [int(f["raw"]) for f in rows[0]["fees"]] if rows else [0, 0]
    if not any(fees):
        try:
            tools.positions.collect_fees.func(next_turn(), position_id=position_id)
            check("collecting from a position that has earned nothing is refused", False, "it was quoted")
        except ToolException as e:
            check("collecting from a position that has earned nothing is refused",
                  "no fees to collect" in str(e), str(e)[:200])
    else:
        quoted = tools.positions.collect_fees.func(next_turn(), position_id=position_id)
        receipt = confirm(quoted)
        check("collecting the fees it has earned lands", receipt["status"] == 1)
        check("...leaving the position as it was", manager.functions.positions(position_id).call()[7] == liquidity)
        check("...charging nothing to the limit", spend_metered(sh, receipt) == [], str(spend_metered(sh, receipt)))

    usdc_before, weth_before = held()
    quoted = tools.positions.withdraw_from_position.func(next_turn(), position_id=position_id, percent=50)
    check("taking half out quotes it, with its fees, without closing it",
          f"Takes 50% of position #{position_id}" in quoted.get("details", "")
          and "burned" not in quoted.get("details", ""), str(quoted.get("details")))
    receipt = confirm(quoted)
    sent["half"] = (receipt, quoted["action"], "in")
    check("...and it lands", receipt["status"] == 1)
    check("...halving the position's liquidity", manager.functions.positions(position_id).call()[7]
          == liquidity - liquidity // 2, str(manager.functions.positions(position_id).call()[7]))
    usdc_after, weth_after = held()
    check("...the USDC coming back, and the ETH side as ETH rather than WETH",
          usdc_after > usdc_before and weth_after == weth_before, f"{usdc_after - usdc_before} / {weth_after - weth_before}")
    check("...charging nothing to the limit: value only came back", spend_metered(sh, receipt) == [],
          str(spend_metered(sh, receipt)))
    shown = [p for p in state()["positions"] if p["id"] == position_id]
    check("the dashboard still shows it, holding less of each",
          len(shown) == 1 and len(rows) == 1 and all(
              int(now["raw"]) < int(then["raw"]) for now, then in zip(shown[0]["amounts"], rows[0]["amounts"])
          ), str(shown)[:300])

    quoted = tools.positions.withdraw_from_position.func(next_turn(), position_id=position_id, percent=100)
    check("taking the rest out says it closes the position", "its NFT is burned" in quoted.get("details", ""),
          str(quoted.get("details")))
    usdc_before, _ = held()
    receipt = confirm(quoted)
    sent["rest"] = (receipt, quoted["action"], "in")
    check("...and it lands", receipt["status"] == 1)
    try:
        manager.functions.ownerOf(position_id).call()
        burned = False
    except ContractLogicError:
        burned = True
    check("...burning the NFT", burned and manager.functions.balanceOf(wallet).call() == nfts_before)
    check("...the rest of the USDC coming back", held()[0] > usdc_before)
    check("...charging nothing to the limit", spend_metered(sh, receipt) == [], str(spend_metered(sh, receipt)))
    check("the assistant finds no positions again", tools.positions.get_positions.func(next_turn())["positions"] == [])
    check("...nor does the dashboard", state()["positions"] == [])
    try:
        tools.positions.withdraw_from_position.func(next_turn(), position_id=position_id, percent=100)
        check("the burned position is refused", False, "it was quoted")
    except ToolException as e:
        check("the burned position is refused: it is no longer one of the wallet's",
              "this wallet's own positions" in str(e), str(e)[:200])
    for token, ticker in ((usdc, "USDC"), (weth, "WETH")):
        for spender in get_default_spenders(CHAIN_ID):
            check(f"no {ticker} allowance is left on {spender}", token.functions.allowance(wallet, spender).call() == 0)

    # The History tab. Each read of its first page works out what a few transactions moved
    # (tx_history.read_movements), newest first, so these settle within a read or two.
    hashes = {name: Web3.to_hex(receipt["transactionHash"]).lower() for name, (receipt, _, _) in sent.items()}
    for _ in range(5):
        r = c.get(f"/api/transactions?chain_id={CHAIN_ID}&limit=50", headers=headers)
        assert r.status_code == 200, f"history read failed: {r.status_code} {r.text[:250]}"
        by_hash = {t["tx_hash"]: t for t in r.json()["transactions"]}
        if all(by_hash.get(h, {}).get("direction") is not None for h in hashes.values()):
            break
    described = {"open": "the deposit that opened it", "half": "taking half out", "rest": "taking the rest out"}
    for name, (_, action, direction) in sent.items():
        row = by_hash.get(hashes[name])
        check(f"History lists {described[name]} as the assistant's, confirmed, described by its quote",
              row is not None and row["source"] == "assistant" and row["status"] == "confirmed"
              and row["action"] == action, str(row))
        check(f"...as {'outgoing' if direction == 'out' else 'incoming'}",
              row is not None and row["direction"] == direction, str(row and row["direction"]))


def test_price_pause_is_named(c: TestClient, acct, headers: dict, wallet: str):
    """
    An L2 sequencer outage must reach the agent as a NAMED error, not 4 bytes of hex -- and must NOT
    lock the owner out.

    The outage halts every valuation, so a session-key UserOp that moves native value fails, as does
    a price read. The owner's web-app actions are direct calls that never reach the oracle
    (THREAT_MODEL §3.14), so a withdraw must still prepare -- that is the user's way out meanwhile.

    Runs only where the oracle has a sequencer uptime feed (Arbitrum); elsewhere there is nothing to
    pause. The oracle holds the feed's address as an immutable, so the feed is faked IN PLACE: its
    code is swapped for MockV3Aggregator's and the three storage slots latestRoundData reads are
    written directly. Both are put back exactly afterwards -- the original code and the original
    value of every slot written -- so the fork is left as it was found.

    Last in the run on purpose: it swaps a live feed's code, and restores it only at the end.
    """
    print("\n[8] a sequencer outage: named for the agent, no lock-out for the owner")
    sh = w3.eth.contract(address=wallet, abi=db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    registry = w3.eth.contract(address=sh.functions.REGISTRY().call(),
                               abi=db.get_json("./out/SHRegistry.sol/SHRegistry.json")["abi"])
    oracle = w3.eth.contract(address=registry.functions.priceOracle().call(),
                             abi=db.get_json("./out/SHOracle.sol/SHOracle.json")["abi"])
    feed = oracle.functions.SEQUENCER_UPTIME_FEED().call()
    if int(feed, 16) == 0:
        print("  (skipped: this chain's oracle has no sequencer uptime feed)")
        return

    # MockV3Aggregator's latestRoundData returns (latestRound, getAnswer[r], getStartedAt[r], ...):
    # slot 3, and the slot-4 and slot-6 mappings at key r.
    rnd = 1
    latest_round_slot = 3
    answer_slot = int.from_bytes(keccak(rnd.to_bytes(32, "big") + (4).to_bytes(32, "big")), "big")
    started_at_slot = int.from_bytes(keccak(rnd.to_bytes(32, "big") + (6).to_bytes(32, "big")), "big")
    written = (latest_round_slot, answer_slot, started_at_slot)
    saved_code = w3.eth.get_code(feed)
    saved = {slot: w3.eth.get_storage_at(feed, slot) for slot in written}

    def set_slot(slot: int, value: int):
        w3.provider.make_request("anvil_setStorageAt", [feed, hex(slot), "0x" + value.to_bytes(32, "big").hex()])

    def report(answer: int, started_at: int):
        """Makes the feed report `answer` (0 up, 1 down) with the current status begun at `started_at`."""
        set_slot(latest_round_slot, rnd)
        set_slot(answer_slot, answer)
        set_slot(started_at_slot, started_at)

    def agent_read_message() -> str:
        """What the agent would be told if a price read failed now, or "" if it succeeds."""
        try:
            sh.functions.getUsdValue(weth, 10**18).call()
            return ""
        except Exception as e:  # noqa: BLE001 -- web3 raises ContractCustomError here
            return smart_wallet_agent._tool_failure_message(e)

    sink = Account.create().address
    withdraw = {"chain_id": CHAIN_ID, "token": "eth", "amount": "0.01", "to": sink}
    weth = Web3.to_checksum_address(get_token_address(CHAIN_ID, WRAPPED))
    mock_runtime = db.get_json("./out/MockV3Aggregator.sol/MockV3Aggregator.json")["deployedBytecode"]["object"]
    w3.provider.make_request("anvil_setCode", [feed, mock_runtime])
    try:
        now = w3.eth.get_block("latest")["timestamp"]
        report(answer=1, started_at=now)

        r = c.post("/api/wallet/withdraw/prepare", headers=headers, json=withdraw)
        check("the owner can still withdraw during an outage", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

        # The agent, on a read: what the tool-failure middleware turns a raw revert into.
        message = agent_read_message()
        check("the agent is told the error by name on a read", "PriceOracle_SequencerDown" in message, message[:200])

        # The agent, on a transaction: a real passkey op through the self-bundling path. Its inner
        # call fails in the hook's valuation, which the bundler's execution estimate hits first --
        # so the op is refused by name before the passkey is even asked, and nothing is mined or paid.
        user_id = c.get("/api/me", headers=headers).json()["user_id"]
        try:
            passkey_op(user_id, [(sink, 10**15, b"")], bundler.resolve_bundler())
            check("a UserOp fails during an outage", False, "it succeeded")
        except RuntimeError as e:
            check("the agent is told the error by name on a transaction",
                  "PriceOracle_SequencerDown" in str(e), str(e)[:200])
        check("...and nothing moved", w3.eth.get_balance(sink) == 0)

        # Back up, but inside the one-hour grace window.
        report(answer=0, started_at=w3.eth.get_block("latest")["timestamp"] - 60)
        message = agent_read_message()
        check("inside the grace window the agent is told that by name",
              "PriceOracle_SequencerGracePeriod" in message, message[:200])
    finally:
        w3.provider.make_request("anvil_setCode", [feed, "0x" + bytes(saved_code).hex()])
        for slot, value in saved.items():
            w3.provider.make_request("anvil_setStorageAt", [feed, hex(slot), "0x" + bytes(value).hex()])

    check("the real feed is back: prices read again", agent_read_message() == "", agent_read_message()[:200])


def test_transaction_history(c: TestClient, acct, headers: dict, wallet: str):
    """
    The History tab against the real chain. A send by the assistant is listed with the hash its
    reply reported; a deposit sent straight from the owner's wallet, as the Fund drawer sends one,
    is listed once reported; and the deploy and owner actions this run made are all there,
    described from their calldata, with hashes the chain knows and the outcome it recorded.
    """
    print("\n[9] the History tab: this run's transactions, with the hashes the chain knows")
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    turn = itertools.count(1_000_000)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    usdc = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
    deal_erc20(usdc, wallet, 10 * 10 ** w3.eth.contract(address=usdc, abi=ERC20_ABI).functions.decimals().call())
    quoted = tools.transfers.transfer_erc20.func(next_turn(), token="usdc", recipient="payee", amount=1)
    reply = approve_and_send(user_id, quoted["quote_id"])
    sent_hash = reply.split("`")[1]
    check("the reply reports a 0x-prefixed hash", sent_hash.startswith("0x") and len(sent_hash) == 66, sent_hash)
    sent_receipt = w3.eth.get_transaction_receipt(sent_hash)

    deposit = acct.sign_transaction({
        "to": wallet, "value": w3.to_wei("0.05", "ether"), "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID, "gas": 100_000, "gasPrice": w3.eth.gas_price * 2,
    })
    deposit_hash = Web3.to_hex(w3.eth.send_raw_transaction(deposit.raw_transaction))
    w3.eth.wait_for_transaction_receipt(deposit_hash, timeout=60)
    r = c.post("/api/transactions/deposit", headers=headers, json={"chain_id": CHAIN_ID, "tx_hash": deposit_hash})
    check("the deposit is reported", r.status_code == 200, f"{r.status_code} {r.text[:160]}")

    r = c.get(f"/api/transactions?chain_id={CHAIN_ID}&limit=100", headers=headers)
    check("the history reads", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    listed = r.json()["transactions"]
    by_hash = {t["tx_hash"]: t for t in listed}
    actions = [t["action"] for t in listed]

    sent = by_hash.get(sent_hash.lower())
    check("the assistant's send is listed under the hash its reply reported", sent is not None, str(actions[:5]))
    if sent:
        check("...as the assistant's, confirmed, described by its quote",
              sent["source"] == "assistant" and sent["status"] == "confirmed" and sent["action"] == quoted["action"],
              str(sent))
        check("...at the time its block was mined",
              sent["mined_at"] == w3.eth.get_block(sent_receipt["blockNumber"])["timestamp"], str(sent))
    added = by_hash.get(deposit_hash.lower())
    check("the deposit is listed, confirmed, described from the transaction",
          added is not None and added["status"] == "confirmed" and added["action"] == f"Add 0.05 {get_native_asset_ticker(CHAIN_ID).upper()} to the wallet"
          and added["source"] == "owner", str(added))

    for expected in ("Set the spending limit to $1,234", "Set the spending period to 1 hour", "Pause the wallet",
                     "Unpause the wallet", "Revoke “E2E laptop”", "Revoke every device", "Count USDC toward the limit",
                     "Stop counting USDC toward the limit"):
        check(f"the owner action “{expected}” is listed", expected in actions, str(actions))
    check("the withdrawal is listed with its amount",
          any(a.startswith(f"Withdraw 0.1 {get_native_asset_ticker(CHAIN_ID).upper()} to 0x") for a in actions), str(actions))
    deploys = [t for t in listed if t["action"] == "Create your Mitfah smart wallet"]
    check("the deploy is listed as confirmed, and the out-of-gas one as failed",
          sorted(t["status"] for t in deploys) == ["confirmed", "failed"], str(deploys))
    check("nothing is left pending", not [t for t in listed if t["status"] == "pending"],
          str([t for t in listed if t["status"] == "pending"]))

    disagree = []
    for t in listed:
        receipt = w3.eth.get_transaction_receipt(t["tx_hash"])
        if t["source"] == "owner" and (receipt["status"] == 1) != (t["status"] == "confirmed"):
            disagree.append(t)
    check("every hash is a transaction the chain knows, and owner outcomes match its receipts", not disagree,
          str(disagree))
    check("newest first", [t["id"] for t in listed] == sorted((t["id"] for t in listed), reverse=True))


def test_web_confirm_button(c: TestClient, headers: dict, wallet: str):
    """
    The web chat's card against the real chain: a quote a turn offered shows on GET /api/chat/quote,
    and the passkey's approval -- POST /api/sign/start, then /finish -- sends exactly it, with no
    model, under a turn of its own. It answers once the transaction is on the wire; the History row
    it names, followed on GET /api/transactions/{id}, lands as a successful transaction.
    """
    print("\n[10] the web chat's card: offered, approved with a passkey, sent on chain")
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    turn_id = smart_wallet_agent._next_turn_id()
    runtime = SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=turn_id))
    quoted = tools.transfers.transfer_erc20.func(runtime, token="usdc", recipient="payee", amount=1)
    offered = signing.offer(user_id, CHAIN_ID, turn_id, signing.WEB)
    check("the turn's one quote is offered on a card, with a sign request",
          offered is not None and offered.pending.quote_id == quoted["quote_id"] and offered.token is None)

    card = c.get(f"/api/chat/quote?chain_id={CHAIN_ID}", headers=headers).json()["quote"]
    check("the card is the stored quote", card is not None and card["quote_id"] == quoted["quote_id"]
          and card["action"] == quoted["action"], str(card))

    ref = {"chain_id": CHAIN_ID, "quote_id": quoted["quote_id"]}
    r = c.post("/api/sign/start", headers=headers, json=ref)
    started = r.json() if r.status_code == 200 else {}
    check("opening the approval builds the op and names the passkey that may approve it",
          r.status_code == 200 and started["allow_credentials"] == [
              {"type": "public-key", "id": PASSKEYS[user_id].credential_id_b64}], f"{r.status_code} {r.text[:200]}")

    recorded = []
    saved_record = smart_wallet_agent._record_button
    # The agent isn't running here; note what would go into the conversation instead.
    smart_wallet_agent._record_button = lambda *args, **kwargs: recorded.append((args, kwargs))
    try:
        credential = PASSKEYS[user_id].assertion(base64url_to_bytes(started["challenge"]))
        r = c.post("/api/sign/finish", headers=headers, json={"sign_id": started["sign_id"], "credential": credential})
    finally:
        smart_wallet_agent._record_button = saved_record
    outcome = r.json().get("outcome", "") if r.status_code == 200 else ""
    row_id = r.json().get("transaction_id") if r.status_code == 200 else None
    check("the approval sends it, and answers once it's on the wire",
          r.status_code == 200 and r.json()["status"] == "sent" and outcome.startswith(f"Sending — {quoted['action']}")
          and isinstance(row_id, int), f"{r.status_code} {r.text[:200]}")
    row = {}
    deadline = time.time() + 120
    while time.time() < deadline:
        row = c.get(f"/api/transactions/{row_id}", headers=headers).json()
        if row.get("status") != "pending":
            break
        time.sleep(0.5)
    sent_hash = row.get("tx_hash") or ""
    receipt = w3.eth.get_transaction_receipt(sent_hash) if sent_hash else None
    check("...and the History row it names lands, confirmed, as a transaction that succeeded on chain",
          row.get("status") == "confirmed" and receipt is not None and receipt["status"] == 1, str(row))
    check("...recorded as a sent approval",
          len(recorded) == 1 and recorded[0][0][2] == smart_wallet_agent.CONFIRM_BUTTON
          and recorded[0][1].get("sent") is True, str(recorded))
    check("the card is gone, and a second approval sends nothing",
          c.get(f"/api/chat/quote?chain_id={CHAIN_ID}", headers=headers).json()["quote"] is None
          and c.post("/api/sign/finish", headers=headers,
                     json={"sign_id": started["sign_id"], "credential": credential}).status_code == 409
          and c.post("/api/sign/start", headers=headers, json=ref).status_code == 404)

    listed = c.get(f"/api/transactions?chain_id={CHAIN_ID}&limit=100", headers=headers).json()["transactions"]
    row = next((t for t in listed if t["tx_hash"] == sent_hash.lower()), None)
    check("the History tab lists it as the assistant's, confirmed",
          row is not None and row["source"] == "assistant" and row["status"] == "confirmed", str(row))


if __name__ == "__main__":
    print("=== preflight ===")
    require_local_fork()

    client = make_client()
    owner = new_funded_account()
    auth_headers = sign_in_as(client, owner)
    print(f"  test owner: {owner.address}")

    deployed = test_deploy_round_trip(client, owner, auth_headers)
    print(f"  wallet: {deployed}")
    test_wallet_state_read(client, auth_headers, owner, deployed)
    test_dashboard_tokens(client, owner, auth_headers, deployed)
    test_owner_actions(client, owner, auth_headers, deployed)
    test_simulations_bite(client, owner, auth_headers, deployed)
    test_cross_user_isolation(client, owner, auth_headers, deployed)
    test_contacts_are_owner_managed(client, auth_headers, owner)
    test_self_bundling(client, owner, auth_headers, deployed)
    test_bundling(client, owner, auth_headers, deployed)
    test_bundler_pool(client)
    test_allowlist(client, owner, auth_headers, deployed)
    test_custom_tokens(client, owner, auth_headers, deployed)
    test_swaps_and_liquidity(client, owner, auth_headers, deployed)
    test_price_pause_is_named(client, owner, auth_headers, deployed)
    test_transaction_history(client, owner, auth_headers, deployed)
    test_web_confirm_button(client, auth_headers, deployed)

    finish(f"All fork e2e checks passed on {NETWORK}.")
