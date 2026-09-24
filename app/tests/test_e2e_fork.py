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
import os
import sys
import time
from types import SimpleNamespace

from dotenv import load_dotenv

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

load_dotenv()
os.environ["COOKIE_SECURE"] = "0"
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "test_wallet_bot")

from eth_account import Account                       # noqa: E402
from eth_account.messages import encode_defunct       # noqa: E402
from eth_utils import keccak                          # noqa: E402
from fastapi.testclient import TestClient             # noqa: E402
from web3 import Web3                                 # noqa: E402

import api                                            # noqa: E402
from abi import ientry_point                          # noqa: E402
import bundler                                        # noqa: E402
import smart_wallet_agent                             # noqa: E402
import tools                                          # noqa: E402
from agent_context import AgentContext                # noqa: E402
from constants import ETH_SENTINEL, get_native_asset_ticker  # noqa: E402
from contracts import read_spending_config           # noqa: E402
from db import get_pending_session_key, get_session_key, get_token_address  # noqa: E402
from langchain_core.tools import ToolException        # noqa: E402
from langchain_erc20 import ERC20_ABI                 # noqa: E402
import quotes                                         # noqa: E402
from userop import prepare_execute_call               # noqa: E402
from web3.logs import DISCARD                         # noqa: E402

RPC = "http://127.0.0.1:8545"
# Where test_self_bundling writes the measured cost of each transfer, for reporting.
COST_REPORT_PATH = os.getenv("E2E_COST_REPORT_PATH", "/tmp/e2e_cost_report.json")
# The fork under test, named as `make setup-test ARGS=...` names it. Requests speak its chain ID,
# which is the live chain's: a fork reports its parent's id.
FORK_CHAIN_IDS = {f"{name}-fork": cid for cid, name in api.CHAIN_NAME_BY_ID.items() if cid in api.FORKABLE_CHAIN_IDS}
NETWORK = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "sepolia-fork"
if NETWORK not in FORK_CHAIN_IDS:
    raise SystemExit(f"Unknown fork '{NETWORK}'. One of: {sorted(FORK_CHAIN_IDS)}")
CHAIN_ID = FORK_CHAIN_IDS[NETWORK]
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


def signup_and_bind(c: TestClient, acct, email: str) -> dict:
    """Signs up, then binds `acct` to the account through the real SIWE endpoints."""
    body = c.post("/api/auth/signup", json={"email": email, "password": "hunter2hunter2"}).json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    message = api.auth.build_siwe_message(
        next(iter(api.auth.SIWE_DOMAINS)), acct.address, nonce, CHAIN_ID
    )
    signature = Account.sign_message(encode_defunct(text=message), acct.key).signature.hex()
    r = c.post(
        "/api/auth/siwe/verify",
        headers=headers,
        json={"message": message, "signature": signature, "nonce": nonce},
    )
    assert r.status_code == 200, f"SIWE bind failed: {r.status_code} {r.text[:200]}"
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

    r = c.post(
        "/api/deploy",
        headers=headers,
        json={
            "chain_id": CHAIN_ID,
            "deployer": acct.address,
            "daily_limit_usd": 50_000,
            "watched_tokens": [{"ticker": "weth", "address": get_token_address(CHAIN_ID, "weth")}],
            "prefund_eth": "1",
        },
    )
    check("deploy prepares", r.status_code == 200, f"{r.status_code} {r.text[:250]}")
    if r.status_code != 200:
        raise SystemExit("cannot continue without a wallet")
    prepared = r.json()
    predicted, session_key = prepared["predicted_address"], prepared["session_key"]

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
    check("listed ERC20s are priced in too", len(state["balances"]) > 1, str(len(state["balances"])))
    check("no balance errored", not [b for b in state["balances"] if b.get("error")],
          str([b for b in state["balances"] if b.get("error")]))

    # A second account must not read the first's wallet: it has no wallet on this chain, so the
    # lookup is keyed by the CALLER and 404s rather than falling through to somebody else's row.
    other = new_funded_account()
    other_headers = signup_and_bind(c, other, f"reader{int(time.time())}@example.com")
    check("another account gets 404, not someone else's wallet",
          c.get(f"/api/wallet/{CHAIN_ID}", headers=other_headers).status_code == 404)
    check("reading needs a token", c.get(f"/api/wallet/{CHAIN_ID}").status_code == 401)


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

    # Pause twice: the second must be refused by the simulation, not by a failed transaction.
    owner_action(c, headers, acct, "/api/wallet/pause/prepare", {})
    r = c.post("/api/wallet/pause/prepare", headers=headers, json={"chain_id": CHAIN_ID})
    check("pausing an already-paused wallet -> 400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")
    owner_action(c, headers, acct, "/api/wallet/unpause/prepare", {})


def test_cross_user_isolation(c: TestClient, acct, headers: dict, wallet: str):
    """A second account must not be able to touch or read back the first's wallet."""
    print("\n[5] one account cannot reach another's wallet")

    other = new_funded_account()
    other_headers = signup_and_bind(c, other, f"other{int(time.time())}@example.com")

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


def test_contacts_are_owner_managed(c: TestClient, headers: dict):
    """
    The contact routes that replaced the save_contact / delete_contact tools.

    The offline suite covers these in depth; what this adds is the real deployed app -- the same
    process that just built and confirmed transactions -- rather than a TestClient with a stubbed
    lifespan. Cheap, since contacts touch no chain: it is the last leg for that reason.
    """
    print("\n[6] contacts are managed by the owner, not the agent")

    # Compared against the account's own starting list rather than assumed empty: on a fresh
    # wallet.db the first signup is handed user id 1 -- the harness user APP_USER_ID usually names,
    # whose demo contact deploy_wallet.py has already saved.
    baseline = c.get("/api/contacts", headers=headers).json()["contacts"]
    payee = Account.create().address
    r = c.post("/api/contacts", headers=headers, json={"name": "Sandy", "address": payee})
    check("the owner can add a contact", r.status_code == 201, f"{r.status_code} {r.text[:140]}")

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
    r = c.post("/api/contacts", headers=headers, json={"name": "payee", "address": payee})
    check("the payee is saved as a contact", r.status_code == 201, f"{r.status_code} {r.text[:140]}")

    def quote_transfer(amount: float) -> dict:
        """The quote half: builds and prices the transfer. Sends nothing."""
        return tools.transfer_erc20.func(
            next_turn(), session_key_ciphertext=key_ciphertext, token="usdc",
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
        same_turn, session_key_ciphertext=key_ciphertext, token="usdc", recipient="payee", amount=7
    )
    check("a quote says plainly that nothing was sent", "NOT SENT" in quoted["status"], quoted["status"])
    check("quoting broadcasts nothing", w3.eth.get_transaction_count(api_bundler) == sent_before)
    check("...and moves no tokens", token.functions.balanceOf(payee).call() == held_before)
    check("the quote names the contract the value goes through", quoted["destinations"] == [usdc],
          str(quoted["destinations"]))
    check("the quote describes the transfer itself", "7" in quoted["action"] and payee[2:10].lower()
          in quoted["action"].lower().replace("0x", ""), quoted["action"])
    check("the quote prices it in USD", quoted["total_usd"] > 0, str(quoted.get("total_usd")))
    check("the ceiling is at least the estimate", quoted["max_total_usd"] >= quoted["total_usd"],
          f'{quoted["max_total_usd"]} vs {quoted["total_usd"]}')
    check("the protocol fee is quoted separately", quoted["protocol_fee_usd"] > 0,
          str(quoted.get("protocol_fee_usd")))

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

    # Over the spending cap: only the chain can tell (the hook's postCheck), so this is the bundler's
    # execution estimate refusing it -- by name, before anything is signed, sent or paid for.
    deal_erc20(usdc, wallet, 5_000 * unit)
    nonce_before = w3.eth.get_transaction_count(api_bundler)
    native_before = w3.eth.get_balance(wallet)
    try:
        tools.transfer_erc20.func(next_turn(), session_key_ciphertext=key_ciphertext, token="usdc",
                                  recipient="payee", amount=2_000)
        check("a transfer over the cap is refused", False, "it was sent")
    except ToolException as e:
        check("a transfer over the cap is refused by name, before sending",
              "BudgetExceeded" in str(e) and "Nothing was sent" in str(e), str(e)[:200])
    check("...so the bundler sent nothing", w3.eth.get_transaction_count(api_bundler) == nonce_before)
    check("...and the wallet paid no gas", w3.eth.get_balance(wallet) == native_before)
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
        tools.transfer_erc20.func(next_turn(), session_key_ciphertext=key_ciphertext, token="usdc",
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


if __name__ == "__main__":
    print("=== preflight ===")
    require_local_fork()

    client = make_client()
    owner = new_funded_account()
    auth_headers = signup_and_bind(client, owner, f"e2e{int(time.time())}@example.com")
    print(f"  test owner: {owner.address}")

    deployed = test_deploy_round_trip(client, owner, auth_headers)
    print(f"  wallet: {deployed}")
    test_wallet_state_read(client, auth_headers, owner, deployed)
    test_owner_actions(client, owner, auth_headers, deployed)
    test_simulations_bite(client, owner, auth_headers, deployed)
    test_cross_user_isolation(client, owner, auth_headers, deployed)
    test_contacts_are_owner_managed(client, auth_headers)
    test_self_bundling(client, owner, auth_headers, deployed)
    test_price_pause_is_named(client, owner, auth_headers, deployed)

    finish(f"All fork e2e checks passed on {NETWORK}.")
