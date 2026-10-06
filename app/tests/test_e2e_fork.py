"""
End-to-end API tests against a running local fork -- Sepolia unless another is named.

This covers the half of the API that no offline test can reach: the transactions themselves, and
the eth_call simulations that are the entire reason the prepare endpoints exist. A signed-in user
is walked through the real journey -- sign up, prove an address with SIWE, deploy a wallet, then
drive every owner action -- with each on-chain effect asserted afterwards. The agent's side is
covered too: session-key ERC20 transfers through the self-bundler, with what each one really cost
written to COST_REPORT_PATH.

A brand-new account is used rather than the harness's user 1, because the deploy endpoints can only
be covered by a wallet this API actually created.

Requires (see the plan, Phase 7):
    make vault
    make sepolia-fork                    (or make arbitrum-fork, ...)
    make setup-test ARGS=sepolia-fork    (the same fork name)

Run: make e2e-test [ARGS=arbitrum-fork]   (or: python app/tests/test_e2e_fork.py [arbitrum-fork])
"""
import itertools
import json
import math
import os
import sys
import time
from decimal import Decimal
from types import SimpleNamespace

from dotenv import load_dotenv

from checks import add_contact, check, finish, sign_in   # first: it puts app/ on sys.path for the imports below

load_dotenv()
os.environ["COOKIE_SECURE"] = "0"
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "test_wallet_bot")

from eth_account import Account                       # noqa: E402
from eth_utils import keccak                          # noqa: E402
from fastapi.testclient import TestClient             # noqa: E402
from web3 import Web3                                 # noqa: E402

import api                                            # noqa: E402
from abi import ientry_point                          # noqa: E402
import bundler                                        # noqa: E402
import smart_wallet_agent                             # noqa: E402
import tools                                          # noqa: E402
from agent_context import AgentContext                # noqa: E402
from constants import (  # noqa: E402
    ETH_SENTINEL, get_always_counted_ticker, get_chain_display_name, get_native_asset_ticker, get_router,
)
from contracts import load_registry, read_spending_config  # noqa: E402
from db import get_pending_session_key, get_rpc_url, get_session_key, get_token_address  # noqa: E402
from langchain_core.tools import ToolException        # noqa: E402
from langchain_erc20 import ERC20_ABI                 # noqa: E402
from langchain_uniswap_v2.abis import factory_abi, pair_abi, router_abi  # noqa: E402
import quotes                                         # noqa: E402
from userop import prepare_execute_call               # noqa: E402
from web3.logs import DISCARD                         # noqa: E402

# Where test_self_bundling writes the measured cost of each transfer, for reporting.
COST_REPORT_PATH = os.getenv("E2E_COST_REPORT_PATH", "/tmp/e2e_cost_report.json")
# The fork under test, named as `make setup-test ARGS=...` names it. Requests speak its chain ID,
# which is the live chain's: a fork reports its parent's id.
FORK_CHAIN_IDS = {f"{name}-fork": cid for cid, name in api.CHAIN_NAME_BY_ID.items() if cid in api.FORKABLE_CHAIN_IDS}
NETWORK = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "sepolia-fork"
if NETWORK not in FORK_CHAIN_IDS:
    raise SystemExit(f"Unknown fork '{NETWORK}'. One of: {sorted(FORK_CHAIN_IDS)}")
CHAIN_ID = FORK_CHAIN_IDS[NETWORK]
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
    if not api.FORK_MODE:
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
    """The real onboarding path: prepare the deploy, sign it, confirm it, verify it on chain."""
    print("\n[1] deploy: prepare -> sign -> confirm")
    body = {
        "chain_id": CHAIN_ID,
        "deployer": acct.address,
        "daily_limit_usd": 50_000,
        # Nothing picked on purpose: the API adds WETH regardless, since it always counts like ETH.
        "watched_tokens": [],
        "prefund_eth": "1",
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
    predicted, session_key = prepared["predicted_address"], prepared["session_key"]
    weth = Web3.to_checksum_address(get_token_address(CHAIN_ID, "weth"))
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
    check("the session key is authorized ON CHAIN", confirmed["session_key_authorized"] is True)
    check("the wallet holds the prefund", w3.eth.get_balance(wallet) == 10**18,
          str(w3.eth.get_balance(wallet)))

    abi = api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    on_chain = w3.eth.contract(address=wallet, abi=abi)
    check("the owner is the signer", on_chain.functions.owner().call() == acct.address)
    check("currentSession agrees on chain",
          on_chain.functions.currentSession().call() == session_key)
    check("the seeded key carries a live deadline",
          on_chain.functions.isSessionActive(session_key).call() is True)
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

    abi = api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    on_chain = w3.eth.contract(address=wallet, abi=abi)
    cfg = read_spending_config(on_chain)

    check("the address is this account's wallet", state["address"] == wallet, state["address"])
    check("the owner matches chain", state["owner"] == on_chain.functions.owner().call())
    check("is_owner true for the SIWE-bound deployer", state["is_owner"] is True)
    check("not paused", state["paused"] is False)
    check("the cap matches chain", state["spending"]["daily_limit_usd"] == cfg["dailyLimitUsd"] / api.USD_DECIMALS,
          f'{state["spending"]["daily_limit_usd"]} vs {cfg["dailyLimitUsd"] / api.USD_DECIMALS}')
    check("nothing spent yet", state["spending"]["spent_usd"] == 0.0, str(state["spending"]["spent_usd"]))
    check("remaining matches getRemainingBudget",
          state["spending"]["remaining_usd"] == on_chain.functions.getRemainingBudget().call() / api.USD_DECIMALS)
    check("weth is watched and named, not a bare address",
          [t["ticker"] for t in state["spending"]["watched_tokens"]] == ["weth"],
          str(state["spending"]["watched_tokens"]))
    check("the app holds the session key and it is active on chain",
          state["session"]["key"] is not None and state["session"]["active"] is True,
          str(state["session"]))
    check("the gas ceiling is a string, not a float64-mangled int",
          isinstance(state["limits"]["max_op_gas_cost_wei"], str))
    check("the router was seeded as a trusted spender", len(state["limits"]["trusted_spenders"]) == 1,
          str(state["limits"]["trusted_spenders"]))

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
    abi = api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
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
    abi = api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
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

    # The session key: revoke, then grant again. A grant mints a FRESH key, so the address after
    # the round trip must differ from the one before it -- that is the revocation being real.
    old_key = sh.functions.currentSession().call()
    r = c.post("/api/wallet/session/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "remove"})
    check("session/prepare names the key it will revoke", r.json().get("revokes") == old_key, r.text[:150])
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    r = c.post("/api/wallet/session/confirm", headers=headers,
               json={"chain_id": CHAIN_ID, "tx_hash": tx_hash})
    check("the revocation is confirmed", r.json().get("status") == "revoked", r.text[:200])
    check("the agent's key is revoked on chain", int(sh.functions.currentSession().call(), 16) == 0)
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    check("the app forgot the revoked key", get_session_key(user_id, CHAIN_ID, wallet) is None)

    r = c.post("/api/wallet/session/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "add"})
    new_key = r.json()["session_key"]
    check("a grant mints a fresh key", new_key != old_key, f"{new_key} == {old_key}")
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    r = c.post("/api/wallet/session/confirm", headers=headers,
               json={"chain_id": CHAIN_ID, "tx_hash": tx_hash})
    check("the grant is confirmed", r.json().get("status") == "granted", r.text[:200])
    check("the agent can sign again", sh.functions.isSessionActive(new_key).call() is True)

    # A confirm that never arrives -- the owner closed the tab while the grant mined. The next
    # wallet read has to pick the new key up, or the assistant keeps signing with the evicted one.
    r = c.post("/api/wallet/session/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "add"})
    unconfirmed_key = r.json()["session_key"]
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    session = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers).json()["session"]
    check("a grant nobody confirmed is picked up by the next wallet read",
          session["key"] == unconfirmed_key and session["is_app_key"] and session["active"], str(session))
    check("nothing is left pending once it is picked up",
          get_pending_session_key(user_id, CHAIN_ID, wallet) is None)
    r = c.post("/api/wallet/session/confirm", headers=headers,
               json={"chain_id": CHAIN_ID, "tx_hash": tx_hash})
    check("a confirm arriving after the read still reports the grant",
          r.json().get("status") == "granted", r.text[:200])

    # The same for a revocation.
    r = c.post("/api/wallet/session/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "remove"})
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    session = c.get(f"/api/wallet/{CHAIN_ID}", headers=headers).json()["session"]
    check("a revocation nobody confirmed is picked up by the next wallet read",
          session["key"] is None and session["wallet_key"] is None, str(session))
    check("the app forgot that revoked key too", get_session_key(user_id, CHAIN_ID, wallet) is None)

    # A read landing between "Turn on" and the owner's signature sees a wallet with no key. It must
    # not throw away the key the owner is about to authorize.
    r = c.post("/api/wallet/session/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "add"})
    new_key = r.json()["session_key"]
    c.get(f"/api/wallet/{CHAIN_ID}", headers=headers)
    check("a wallet read keeps the key of a grant still waiting for its signature",
          get_pending_session_key(user_id, CHAIN_ID, wallet) is not None)
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    r = c.post("/api/wallet/session/confirm", headers=headers,
               json={"chain_id": CHAIN_ID, "tx_hash": tx_hash})
    check("so that grant still lands", r.json().get("status") == "granted", r.text[:200])
    check("and the agent can sign with it", sh.functions.isSessionActive(new_key).call() is True)

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
               json={"chain_id": CHAIN_ID, "token": "weth", "action": "remove"})
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
    abi = api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
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
    payee = next(x["address"] for x in c.get("/api/contacts", headers=headers).json()["contacts"]
                 if x["name"] == "payee")
    body = read()
    suggested = {s["address"]: s["label"] for s in body["suggested"]}
    check("the list starts off and empty", body["enabled"] is False and body["targets"] == [], str(body)[:200])
    check("the exchange router is suggested", api._router_or_none(CHAIN_ID) in suggested, str(suggested))
    check("USDC, on the dashboard, is suggested by name", suggested.get(usdc) == "USDC", str(suggested))
    check("the review registry is suggested", sh.functions.REPUTATION_REGISTRY().call() in suggested)
    check("a contact with a plain wallet is not", payee not in suggested)

    owner_action(c, headers, acct, "/api/wallet/allowlist/prepare", {"action": "enable", "targets": [usdc]})
    check("one signature turned the list on", sh.functions.sessionAllowlistEnabled().call() is True)
    check("...with USDC as its one entry", sh.functions.getAllowedTargets().call() == [usdc])
    body = read()
    check("the read names the entry", body["targets"] == [{"address": usdc, "label": "USDC"}], str(body)[:200])
    check("...and stops suggesting it", usdc not in {s["address"] for s in body["suggested"]})

    quoted = tools.transfer_erc20.func(next_turn(), token="usdc", recipient="payee", amount=1)
    check("a listed token still quotes", "quote_id" in quoted, str(quoted)[:200])
    tools.cancel_transaction.func(next_turn(), quote_id=quoted["quote_id"])
    quoted = tools.send_eth.func(next_turn(), recipient="payee", amount_eth=0.001)
    check("a plain wallet needs no entry: sending it the native asset still quotes",
          "quote_id" in quoted, str(quoted)[:200])
    tools.cancel_transaction.func(next_turn(), quote_id=quoted["quote_id"])

    # An unlisted contract. The wallet holds the token, so the balance check passes and the refusal
    # is the guard's own.
    link = Web3.to_checksum_address(get_token_address(CHAIN_ID, "link"))
    deal_erc20(link, wallet, 10 * 10**18)
    try:
        tools.transfer_erc20.func(next_turn(), token="link", recipient="payee", amount=1)
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
    estimate, the session-key signature, handleOps from the API's bundler key. One transfer moves a
    token the cap does not watch (no price read), one a token it does (one), so the cost of the
    oracle shows up in the numbers.
    """
    print("\n[7] self-bundling: ERC20 transfers, their real cost, and the live-chain failure modes")
    abi = api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    sh = w3.eth.contract(address=wallet, abi=abi)
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    _, key_ciphertext = get_session_key(user_id, CHAIN_ID, wallet)

    # A tool runtime for the NEXT conversation turn. Every quote and every confirm gets its own,
    # because confirm_transaction refuses a quote raised in the turn that is confirming it -- the
    # guarantee that the user actually saw the cost and replied. The agent supplies these ids in
    # production (smart_wallet_agent._next_turn_id); here the test plays the part of the user.
    turn = itertools.count(1)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    # One key per process, so the API and the Telegram bot never hand out the same nonce.
    api_bundler = bundler.resolve_bundler(w3).address
    bundler.use_bundler_key(bundler.TELEGRAM_BUNDLER_ENV)
    try:
        telegram_bundler = bundler.resolve_bundler(w3).address
    finally:
        bundler.use_bundler_key(bundler.API_BUNDLER_ENV)
    check("the API and the Telegram bot bundle with different keys", api_bundler != telegram_bundler)
    check("the API's key is the default", bundler.resolve_bundler(w3).address == api_bundler)
    check("a fork never broadcasts privately", bundler._send_w3_for(NETWORK) is None)
    check("live mainnet does", bundler._send_w3_for("mainnet") is not None)

    usdc = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
    token = w3.eth.contract(address=usdc, abi=ERC20_ABI)
    unit = 10 ** token.functions.decimals().call()
    deal_erc20(usdc, wallet, 1_000 * unit)
    payee = Account.create().address
    r = add_contact(c, headers, acct, "payee", payee)
    check("the payee is saved as a contact", r.status_code == 201, f"{r.status_code} {r.text[:140]}")

    def quote_transfer(amount: float) -> dict:
        """The quote half: builds and prices the transfer. Sends nothing."""
        return tools.transfer_erc20.func(
            next_turn(), token="usdc",
            recipient="payee", amount=amount,
        )

    def confirm(quoted: dict):
        """The send half, a turn later -- as it would be after the user replied."""
        result = tools.confirm_transaction.func(next_turn(), quote_id=quoted["quote_id"])
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

    # The quote/confirm split itself. A quote prices a transaction that is still unsigned and
    # unsent; it cannot be confirmed in the turn that raised it, it belongs to one user, it works
    # once, and what it quoted has to match what the chain then charges.
    sent_before = w3.eth.get_transaction_count(api_bundler)
    held_before = token.functions.balanceOf(payee).call()
    same_turn = next_turn()
    quoted = tools.transfer_erc20.func(
        same_turn, token="usdc", recipient="payee", amount=7
    )
    check("a quote says plainly that nothing was sent", "NOT SENT" in quoted["status"], quoted["status"])
    check("quoting broadcasts nothing", w3.eth.get_transaction_count(api_bundler) == sent_before)
    check("...and moves no tokens", token.functions.balanceOf(payee).call() == held_before)
    check("the quote names the contract the value goes through", quoted["destinations"] == [usdc],
          str(quoted["destinations"]))
    check("the quote names the network it runs on", quoted["network"] == get_chain_display_name(CHAIN_ID),
          str(quoted.get("network")))
    check("the quote describes the transfer itself", "7" in quoted["action"] and payee[2:10].lower()
          in quoted["action"].lower().replace("0x", ""), quoted["action"])
    check("the quote prices it in USD", quoted["total_usd"] > 0, str(quoted.get("total_usd")))
    check("the ceiling is at least the estimate", quoted["max_total_usd"] >= quoted["total_usd"],
          f'{quoted["max_total_usd"]} vs {quoted["total_usd"]}')
    check("the protocol fee is quoted separately", quoted["protocol_fee_usd"] > 0,
          str(quoted.get("protocol_fee_usd")))
    # The quote ran the wallet checks itself -- the agent no longer calls preflight_check first.
    check("the quote says what it is worth and what the (watched) USDC counts toward the limit",
          abs(quoted["usd_value"] - 7) < 0.1 and abs(quoted["charged_usd"] - 7) < 0.1,
          f'{quoted.get("usd_value")} / {quoted.get("charged_usd")}')
    check("...and what is left of the limit, and of the session key",
          quoted["remaining_usd"] > 0 and quoted["session_expires_in_secs"] > 0,
          f'{quoted.get("remaining_usd")} / {quoted.get("session_expires_in_secs")}')

    try:
        tools.confirm_transaction.func(same_turn, quote_id=quoted["quote_id"])
        check("confirming in the quoting turn is refused", False, "it was sent")
    except ToolException as e:
        check("confirming in the quoting turn is refused", "has not seen the cost" in str(e), str(e)[:160])
    check("...and still nothing was broadcast", w3.eth.get_transaction_count(api_bundler) == sent_before)

    try:
        quotes.take(user_id + 9_999, CHAIN_ID, quoted["quote_id"], next(turn))
        check("another user cannot claim someone else's quote", False, "it was handed over")
    except quotes.QuoteError:
        check("another user cannot claim someone else's quote", True)

    receipt = confirm(quoted)
    actual = user_op_cost(sh, receipt)
    check("confirming sends the quoted transfer, once",
          token.functions.balanceOf(payee).call() - held_before == 7 * unit)
    check("the real cost stayed under the quoted ceiling",
          actual["total_fee_usd"] <= quoted["max_total_usd"], 
          f'${actual["total_fee_usd"]:.4f} charged vs ${quoted["max_total_usd"]:.4f} quoted as the max')
    # Tight on purpose. A quote that is merely an upper bound is not a price: it is the number the
    # user decides on, so it has to track what the chain then charges, not the gas that was merely
    # reserved. 10% leaves room for the base fee moving between the quote and the block.
    check("the quoted cost was within 10% of the real one",
          abs(actual["total_fee_usd"] - quoted["total_usd"]) <= 0.10 * actual["total_fee_usd"],
          f'quoted ${quoted["total_usd"]:.4f}, charged ${actual["total_fee_usd"]:.4f}')
    report["quote_vs_actual"] = {
        "quoted_total_usd": quoted["total_usd"],
        "quoted_max_usd": quoted["max_total_usd"],
        "charged_total_usd": actual["total_fee_usd"],
    }

    try:
        tools.confirm_transaction.func(next_turn(), quote_id=quoted["quote_id"])
        check("a quote cannot be confirmed twice", False, "it was sent again")
    except ToolException as e:
        check("a quote cannot be confirmed twice", "no pending transaction" in str(e), str(e)[:160])

    # Over the spending cap: the quote's own wallet checks refuse it, saying how much is left --
    # before anything is signed, sent or paid for. (The bundler's execution estimate would refuse
    # it too, as BudgetExceeded; the check's reason is the one the user can act on.)
    deal_erc20(usdc, wallet, 5_000 * unit)
    nonce_before = w3.eth.get_transaction_count(api_bundler)
    native_before = w3.eth.get_balance(wallet)
    try:
        tools.transfer_erc20.func(next_turn(), token="usdc",
                                  recipient="payee", amount=2_000)
        check("a transfer over the cap is refused", False, "it was sent")
    except ToolException as e:
        check("a transfer over the cap is refused, saying what is left, before sending",
              "spending limit" in str(e) and "is left" in str(e) and "Nothing was sent" in str(e), str(e)[:200])
    check("...so the bundler sent nothing", w3.eth.get_transaction_count(api_bundler) == nonce_before)
    check("...and the wallet paid no gas", w3.eth.get_balance(wallet) == native_before)

    # A paused wallet: refused with the reason, rather than as a failed simulation.
    owner_action(c, headers, acct, "/api/wallet/pause/prepare", {})
    try:
        tools.transfer_erc20.func(next_turn(), token="usdc", recipient="payee", amount=1)
        check("a paused wallet's transfer is refused", False, "it was quoted")
    except ToolException as e:
        check("a paused wallet's transfer is refused, saying it is paused",
              "paused" in str(e) and "Nothing was sent" in str(e), str(e)[:200])
    finally:
        owner_action(c, headers, acct, "/api/wallet/unpause/prepare", {})
    check("...and the bundler sent nothing for it either", w3.eth.get_transaction_count(api_bundler) == nonce_before)
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
    session_handler, entry_point, calldata, nonce = prepare_execute_call(
        user_id, usdc, 0, bytes.fromhex(token.encode_abi("transfer", args=[payee, amount])[2:])
    )
    bundler_account = bundler.resolve_bundler(w3)
    op_quote = bundler.quote_user_op(
        user_id, session_handler, entry_point, calldata, nonce, bundler_account
    )
    prepared = bundler.prepare_user_op(
        user_id, key_ciphertext, session_handler, entry_point, op_quote, bundler_account
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
        tools.transfer_erc20.func(next_turn(), token="usdc",
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


def eoa_call(acct, fn, value: int = 0):
    """Sends a contract call (or deployment) from a plain EOA on the fork and returns its receipt."""
    tx = fn.build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID, "value": value,
    })
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction), timeout=60
    )
    assert receipt["status"] == 1, f"EOA call reverted: {receipt['transactionHash'].hex()}"
    return receipt


def spend_metered(sh, receipt) -> list[int]:
    """The netOutflowUsd of every SpendMetered the wallet's hook emitted in `receipt` (none = nothing charged)."""
    module = w3.eth.contract(
        address=sh.functions.SH_MODULE().call(),
        abi=api.get_json("./out/SpendingLimitModule.sol/SpendingLimitModule.json")["abi"],
    )
    return [
        e["args"]["netOutflowUsd"]
        for e in module.events.SpendMetered().process_receipt(receipt, errors=DISCARD)
        if e["args"]["account"] == sh.address
    ]


def test_custom_tokens(c: TestClient, acct, headers: dict, wallet: str):
    """
    A token the user adds by address, on a real chain: a brand-new ERC-20 with no price feed and one
    TOKEN/WETH pool, the usual shape. Buying it with ETH goes straight through that pool; paying in
    USDC hops through WETH (the Uniswap package's own routing).

    Covers the add rules against real contracts, the wallet read, the assistant sending it (not
    charged), buying it with ETH (the FULL amount charged, exactly), the assistant refusing a token
    nobody added and a slippage over 12%, selling it back (not charged, and its unpriced approval
    clears because the router is a trusted spender), the owner withdrawing it by address, and
    removing it from the list.
    """
    print("\n[7b] tokens the user adds: added by address, sent, bought with ETH, sold, withdrawn")
    sh = w3.eth.contract(address=wallet, abi=api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    turn = itertools.count(1_000)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    def confirm(quoted: dict):
        result = tools.confirm_transaction.func(next_turn(), quote_id=quoted["quote_id"])
        return w3.eth.get_transaction_receipt("0x" + result.split("`")[1].removeprefix("0x"))

    # A fresh token nobody prices, and a fake one calling itself USDC.
    deployer = new_funded_account()
    mock = api.get_json("./out/ERC20Mock.sol/ERC20Mock.json")
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
        # The exchange router: a contract, but with no symbol() or decimals().
        ("a contract that isn't a token asks to check it again", get_router(CHAIN_ID), "Check the token address again"),
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
    check("the assistant lists it as custom", ticker in tools.get_supported_tokens.func(next_turn())["custom"])

    try:
        tools.get_price.func(next_turn(), token=ticker)
        check("get_price refuses it by name", False, "it answered a price")
    except ToolException as e:
        check("get_price refuses it by name", "no price" in str(e), str(e)[:160])
    pre = tools.preflight_check.func(next_turn(), token=ticker, amount=10)
    check("preflight: no USD value, nothing charged, passes",
          pre["usd_value"] is None and pre["charged_usd"] == 0 and pre["within_budget"], str(pre))

    # The assistant sends it to a contact: not charged.
    payee = c.get("/api/contacts", headers=headers).json()["contacts"]
    payee = next(p for p in payee if p["name"] == "payee")["address"]
    quoted = tools.transfer_erc20.func(next_turn(), token=ticker,
                                       recipient="payee", amount=10)
    check("the transfer quote says it isn't covered", "isn't covered by the spending limit" in quoted.get("details", ""),
          str(quoted.get("details")))
    receipt = confirm(quoted)
    check("the contact received it", token.functions.balanceOf(payee).call() == 10 * unit)
    check("and nothing was charged to the limit", spend_metered(sh, receipt) == [], str(spend_metered(sh, receipt)))

    # One TOKEN/WETH pool, seeded from the deployer.
    router = w3.eth.contract(address=get_router(CHAIN_ID), abi=router_abi)
    eoa_call(deployer, token.functions.mint(deployer.address, 1_000_000 * unit))
    eoa_call(deployer, token.functions.approve(router.address, 1_000_000 * unit))
    eoa_call(deployer, router.functions.addLiquidityETH(
        token_address, 1_000_000 * unit, 0, 0, deployer.address,
        w3.eth.get_block("latest")["timestamp"] + 600,
    ), value=w3.to_wei(1, "ether"))

    weth = Web3.to_checksum_address(get_token_address(CHAIN_ID, "weth"))
    quote = tools.get_quote_out.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001)
    check("buying it with ETH goes through its WETH pool",
          [Web3.to_checksum_address(a) for a in quote["path"]] == [weth, token_address], str(quote.get("path")))
    quote = tools.get_quote_out.func(next_turn(), token_in="usdc", token_out=ticker, amount_in=1)
    check("paying in USDC hops through WETH",
          [Web3.to_checksum_address(a) for a in quote["path"]] == [usdc, weth, token_address], str(quote.get("path")))

    held = token.functions.balanceOf(wallet).call()
    quoted = tools.swap.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001, slippage_bps=500)
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
    api_bundler = bundler.resolve_bundler(w3).address
    sent_before = w3.eth.get_transaction_count(api_bundler)
    try:
        tools.swap.func(next_turn(), token_in="eth", token_out=fake_usdc, amount_in=0.001)
        check("a token the user never added is refused by address", False, "it was quoted")
    except ToolException as e:
        check("a token the user never added is refused by address",
              "not a token this wallet knows" in str(e), str(e)[:160])
    quote = tools.get_quote_out.func(next_turn(), token_in="eth", token_out=token_address, amount_in=0.001)
    check("the user's own token still works by address",
          [Web3.to_checksum_address(a) for a in quote["path"]] == [weth, token_address], str(quote.get("path")))
    try:
        tools.swap.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001, slippage_bps=1201)
        check("a slippage over 12% is refused", False, "it was quoted")
    except ToolException as e:
        check("a slippage over 12% is refused", "outside what this wallet allows" in str(e), str(e)[:160])
    quoted = tools.swap.func(next_turn(), token_in="eth", token_out=ticker, amount_in=0.001, slippage_bps=1200)
    check("exactly 12% is quoted", "NOT SENT" in quoted["status"], str(quoted.get("status")))
    tools.cancel_transaction.func(next_turn(), quote_id=quoted["quote_id"])
    check("...and none of that sent anything", w3.eth.get_transaction_count(api_bundler) == sent_before)

    # Selling it back: the unpriced approval clears (the router is a trusted spender), nothing charged.
    native_before = w3.eth.get_balance(wallet)
    quoted = tools.swap.func(next_turn(), token_in=ticker, token_out="eth", amount_in=100, slippage_bps=500)
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
        tools.transfer_erc20.func(next_turn(), token=ticker,
                                  recipient="payee", amount=1)
        check("the assistant no longer recognises it", False, "it quoted a transfer")
    except ToolException as e:
        check("the assistant no longer recognises it", "Add token" in str(e), str(e)[:160])


def test_swaps_and_liquidity(c: TestClient, acct, headers: dict, wallet: str):
    """
    The one swap tool and the two liquidity tools on a real chain, by every route they can take: a
    fresh token with one TOKEN/WETH pool, as in [7b]. Every swap route is quoted and checked to have
    reached its own router function -- the quote's action is the router call's own description --
    and the routes never sent before are sent and read back from the chain. Both forms of adding
    and of removing liquidity are sent: with the native asset (the default) and with WETH.

    The dashboard follows the pool's LP tokens through it all: they show after the first deposit,
    match the chain after every change, go when the owner withdraws them all, come back with the
    next deposit, and go again when the assistant takes it all out.

    Also the refusals that need a real balance: sending more of the native asset than the wallet
    holds, and sending nearly all of it, which leaves nothing for the fees.
    """
    print("\n[7c] one swap tool, one add and one remove: every route, LP tokens on the dashboard, and the balance refusals")
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    turn = itertools.count(2_000_000)

    def next_turn() -> SimpleNamespace:
        return SimpleNamespace(context=AgentContext(user_id=user_id, turn_id=next(turn)))

    def confirm(quoted: dict):
        result = tools.confirm_transaction.func(next_turn(), quote_id=quoted["quote_id"])
        return w3.eth.get_transaction_receipt("0x" + result.split("`")[1].removeprefix("0x"))

    # A fresh token, the wallet holding 10,000 of it, and one TOKEN/WETH pool: 1,000,000 TOKEN
    # against 1 ETH, so 1 TOKEN is worth 0.000001 ETH.
    deployer = new_funded_account()
    mock = api.get_json("./out/ERC20Mock.sol/ERC20Mock.json")
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

    weth = w3.eth.contract(address=Web3.to_checksum_address(get_token_address(CHAIN_ID, "weth")), abi=ERC20_ABI)
    weth_before = weth.functions.balanceOf(wallet).call()
    check("wrapping 0.01 ETH lands", confirm(tools.wrap_eth.func(next_turn(), amount_eth=0.01))["status"] == 1)
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
        ("the token for WETH, exact in", dict(token_in=ticker, token_out="weth", amount_in=100), ("in", "token", "token")),
        ("WETH for the token, exact out", dict(token_in="weth", token_out=ticker, amount_out=100), ("out", "token", "token")),
    )
    quoted = {}
    for label, args, expected in routes:
        quoted[label] = tools.swap.func(next_turn(), **args)
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
        tools.cancel_transaction.func(next_turn(), quote_id=quoted[label]["quote_id"])
    # Quoted before the buy above moved the pool, so these are re-quoted at the price it left.
    held = token.functions.balanceOf(wallet).call()
    receipt = confirm(tools.swap.func(next_turn(), token_in=ticker, token_out="eth", amount_out=0.0001))
    check("selling the token for exactly 0.0001 ETH lands", receipt["status"] == 1)
    check("...spending some of the token", token.functions.balanceOf(wallet).call() < held)
    held, weth_held = token.functions.balanceOf(wallet).call(), weth.functions.balanceOf(wallet).call()
    check("selling exactly 100 of the token for WETH lands",
          confirm(tools.swap.func(next_turn(), token_in=ticker, token_out="weth", amount_in=100))["status"] == 1)
    check("...100 left and WETH came back", token.functions.balanceOf(wallet).call() == held - 100 * unit
          and weth.functions.balanceOf(wallet).call() > weth_held)
    held, weth_held = token.functions.balanceOf(wallet).call(), weth.functions.balanceOf(wallet).call()
    check("buying exactly 100 of the token with WETH lands",
          confirm(tools.swap.func(next_turn(), token_in="weth", token_out=ticker, amount_out=100))["status"] == 1)
    check("...100 arrived and WETH paid for it", token.functions.balanceOf(wallet).call() == held + 100 * unit
          and weth.functions.balanceOf(wallet).call() < weth_held)
    for label in ("the token for ETH, exact out", "the token for WETH, exact in", "WETH for the token, exact out"):
        tools.cancel_transaction.func(next_turn(), quote_id=quoted[label]["quote_id"])

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
        tools.add_liquidity.func(next_turn(), token_a="eth", amount_a=0.001, token_b=ticker)
        check("adding with the native amount fixed is refused", False, "it was quoted")
    except ToolException as e:
        check("adding with the native amount fixed is refused, asking for the token's amount first",
              f"Name how much {ticker.upper()} to deposit first" in str(e), str(e)[:200])
    quote = tools.add_liquidity.func(next_turn(), token_a=ticker, amount_a=1000)
    check("adding liquidity pairs with the native asset by default", quote["action"].endswith("of the native asset"),
          quote["action"])
    check("...counting the ETH deposited, not the token, which has no price",
          quote["charged_usd"] > 0 and quote["usd_value"] is None, f'{quote.get("charged_usd")} / {quote.get("usd_value")}')
    held = token.functions.balanceOf(wallet).call()
    check("...and it lands", confirm(quote)["status"] == 1)
    lp_native = tools.get_liquidity_token_balance.func(next_turn(), token_a=ticker)
    check("the wallet holds LP tokens for the pool, and the token went in",
          lp_native > 0 and token.functions.balanceOf(wallet).call() == held - 1000 * unit, str(lp_native))
    shows_pool("the dashboard shows them, native side first, with what they hold in the pool")
    r = c.get(f"/api/wallet/{CHAIN_ID}/allowlist", headers=headers)
    suggested = {s["address"]: s["label"] for s in r.json()["suggested"]} if r.status_code == 200 else {}
    check("...and the contract allowlist suggests the pool, which taking liquidity out calls",
          suggested.get(pool.address) == f"{native.upper()}/{ticker.upper()} pool", f"{r.status_code} {suggested}"[:300])
    quote = tools.add_liquidity.func(next_turn(), token_a=ticker, amount_a=1000, token_b="weth")
    check("naming WETH deposits WETH instead", "native asset" not in quote["action"], quote["action"])
    weth_held = weth.functions.balanceOf(wallet).call()
    check("...and it lands, paying in WETH", confirm(quote)["status"] == 1
          and weth.functions.balanceOf(wallet).call() < weth_held)
    lp = tools.get_liquidity_token_balance.func(next_turn(), token_a=ticker, token_b="weth")
    check("...into the same pool, so the LP tokens add up", lp > lp_native, f"{lp} vs {lp_native}")
    shows_pool("...and the dashboard has one row for the pool, with the new total")

    held, native_held = token.functions.balanceOf(wallet).call(), w3.eth.get_balance(wallet)
    quote = tools.remove_liquidity.func(next_turn(), token_a=ticker, lp_amount=round(lp / 4, 6))
    check("removing liquidity returns the native asset by default", "native asset" in quote["action"],
          quote["action"])
    check("...shows what should come back and the least it will accept",
          all(part in quote.get("details", "") for part in ("returned: about", "at least")), str(quote.get("details")))
    check("...and it lands, the token coming back", confirm(quote)["status"] == 1
          and token.functions.balanceOf(wallet).call() > held)
    weth_held = weth.functions.balanceOf(wallet).call()
    quote = tools.remove_liquidity.func(next_turn(), token_a=ticker, lp_amount=round(lp / 4, 6), token_b="weth")
    check("naming WETH takes WETH back instead", "native asset" not in quote["action"], quote["action"])
    check("...and it lands, WETH coming back", confirm(quote)["status"] == 1
          and weth.functions.balanceOf(wallet).call() > weth_held)
    check("half the LP tokens are left", tools.get_liquidity_token_balance.func(next_turn(), token_a=ticker) < lp)
    shows_pool("...and the dashboard shows what is left")

    held = pool.functions.balanceOf(wallet).call()
    owner_action(c, headers, acct, "/api/wallet/withdraw/prepare",
                 {"token": pool.address, "amount": str(Decimal(held) / Decimal(10**18)), "to": acct.address})
    check("the owner can withdraw LP tokens like any other token", pool.functions.balanceOf(wallet).call() == 0
          and pool.functions.balanceOf(acct.address).call() >= held)
    check("...and with none left, the pool is gone from the dashboard", lp_rows() == [])
    check("a new deposit lands", confirm(tools.add_liquidity.func(next_turn(), token_a=ticker, amount_a=500))["status"] == 1)
    shows_pool("...and brings the pool back to the dashboard")

    # "All of it", as the assistant asks for it: the balance it reads, in whole units. That passes
    # through a float, which the package scales back up to a few wei more than the wallet holds
    # (refused) or a few wei less (accepted, leaving them behind) -- about half the time each. The
    # largest amount it accepts is what lands, and the few wei it leaves must not keep the row.
    everything = tools.get_liquidity_token_balance.func(next_turn(), token_a=ticker)
    held = pool.functions.balanceOf(wallet).call()
    while int(Decimal(str(everything)) * 10**18) > held:
        everything = math.nextafter(everything, 0)
    check("the assistant taking it all out lands",
          confirm(tools.remove_liquidity.func(next_turn(), token_a=ticker, lp_amount=everything))["status"] == 1)
    check("...leaving at most a few wei", pool.functions.balanceOf(wallet).call() < 1000,
          str(pool.functions.balanceOf(wallet).call()))
    check("...which the dashboard counts as none: the pool is gone", lp_rows() == [])

    # The native asset has to cover what is sent and the fees. Both refusals happen before anything
    # is signed, so neither costs the wallet a thing. The limit is raised for them first: nearly all
    # the ETH is worth more than the $1,234 set in [3], and that refusal would come first.
    sh = w3.eth.contract(address=wallet, abi=api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    limit = read_spending_config(sh)["dailyLimitUsd"] // 10**18
    owner_action(c, headers, acct, "/api/wallet/daily-limit/prepare", {"daily_limit_usd": 100_000})
    api_bundler = bundler.resolve_bundler(w3).address
    sent_before = w3.eth.get_transaction_count(api_bundler)
    balance = w3.eth.get_balance(wallet)
    ticker_native = get_native_asset_ticker(CHAIN_ID)
    try:
        tools.send_eth.func(next_turn(), recipient="payee", amount_eth=balance / 10**18 + 1)
        check("sending more than the wallet holds is refused", False, "it was quoted")
    except ToolException as e:
        check("sending more than the wallet holds is refused, with what it holds",
              f"Not enough {ticker_native}: the wallet holds" in str(e) and "Nothing was sent" in str(e), str(e)[:200])
    fee = load_registry(user_id).functions.getFee().call()
    try:
        tools.send_eth.func(next_turn(), recipient="payee", amount_eth=(balance - fee // 2) / 10**18)
        check("sending nearly all of it is refused", False, "it was quoted")
    except ToolException as e:
        check("sending nearly all of it is refused: too little left for the fees",
              "leaves too little for the fees" in str(e), str(e)[:200])
    pre = tools.preflight_check.func(next_turn(), token="eth", amount=balance / 10**18 + 1)
    check("preflight says the wallet doesn't hold it", pre["enough_balance"] is False
          and "the wallet holds" in pre.get("balance_short", ""), str(pre)[:200])
    check("...and none of that sent anything", w3.eth.get_transaction_count(api_bundler) == sent_before)
    owner_action(c, headers, acct, "/api/wallet/daily-limit/prepare", {"daily_limit_usd": limit})
    check("the limit is back as it was", read_spending_config(sh)["dailyLimitUsd"] == limit * 10**18)


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
    sh = w3.eth.contract(address=wallet, abi=api.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    registry = w3.eth.contract(address=sh.functions.REGISTRY().call(),
                               abi=api.get_json("./out/SHRegistry.sol/SHRegistry.json")["abi"])
    oracle = w3.eth.contract(address=registry.functions.priceOracle().call(),
                             abi=api.get_json("./out/SHOracle.sol/SHOracle.json")["abi"])
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
    weth = Web3.to_checksum_address(get_token_address(CHAIN_ID, "weth"))
    mock_runtime = api.get_json("./out/MockV3Aggregator.sol/MockV3Aggregator.json")["deployedBytecode"]["object"]
    w3.provider.make_request("anvil_setCode", [feed, mock_runtime])
    try:
        now = w3.eth.get_block("latest")["timestamp"]
        report(answer=1, started_at=now)

        r = c.post("/api/wallet/withdraw/prepare", headers=headers, json=withdraw)
        check("the owner can still withdraw during an outage", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

        # The agent, on a read: what the tool-failure middleware turns a raw revert into.
        message = agent_read_message()
        check("the agent is told the error by name on a read", "PriceOracle_SequencerDown" in message, message[:200])

        # The agent, on a transaction: a real session-key UserOp through the self-bundling path.
        # Its inner call fails in the hook's valuation, which the bundler's execution estimate hits
        # first -- so the op is refused by name before it is signed, and nothing is mined or paid.
        user_id = c.get("/api/me", headers=headers).json()["user_id"]
        _, key_ciphertext = get_session_key(user_id, CHAIN_ID, wallet)
        try:
            bundler.send_user_op_as_session(user_id, key_ciphertext, sink, 10**15, b"")
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
    quoted = tools.transfer_erc20.func(next_turn(), token="usdc", recipient="payee", amount=1)
    reply = tools.confirm_transaction.func(next_turn(), quote_id=quoted["quote_id"])
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
          added is not None and added["status"] == "confirmed" and added["action"] == "Add 0.05 ETH to the wallet"
          and added["source"] == "owner", str(added))

    for expected in ("Set the spending limit to $1,234", "Set the spending period to 1 hour", "Pause the wallet",
                     "Unpause the wallet", "Turn the assistant off", "Count USDC toward the limit",
                     "Stop counting USDC toward the limit"):
        check(f"the owner action “{expected}” is listed", expected in actions, str(actions))
    check("the withdrawal is listed with its amount",
          any(a.startswith("Withdraw 0.1 ETH to 0x") for a in actions), str(actions))
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
    test_allowlist(client, owner, auth_headers, deployed)
    test_custom_tokens(client, owner, auth_headers, deployed)
    test_swaps_and_liquidity(client, owner, auth_headers, deployed)
    test_price_pause_is_named(client, owner, auth_headers, deployed)
    test_transaction_history(client, owner, auth_headers, deployed)

    finish(f"All fork e2e checks passed on {NETWORK}.")
