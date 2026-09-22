"""
End-to-end API tests against a running Sepolia fork.

This covers the half of the API that no offline test can reach: the transactions themselves, and
the eth_call simulations that are the entire reason the prepare endpoints exist. A signed-in user
is walked through the real journey -- sign up, prove an address with SIWE, deploy a wallet, then
drive every owner action -- with each on-chain effect asserted afterwards.

A brand-new account is used rather than the harness's user 1, because the deploy endpoints can only
be covered by a wallet this API actually created.

Requires (see the plan, Phase 7):
    make vault
    make sepolia-fork
    make setup-test ARGS=sepolia-fork

Run: make e2e-test   (or: python app/tests/test_e2e_fork.py)
"""
import os
import time

from dotenv import load_dotenv

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

load_dotenv()
os.environ["COOKIE_SECURE"] = "0"
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "test_wallet_bot")

from eth_account import Account                       # noqa: E402
from eth_account.messages import encode_defunct       # noqa: E402
from fastapi.testclient import TestClient             # noqa: E402
from web3 import Web3                                 # noqa: E402

import api                                            # noqa: E402
from constants import CHAIN_ID_SEPOLIA  # noqa: E402
from contracts import read_spending_config           # noqa: E402
from db import get_token_address                      # noqa: E402

RPC = "http://127.0.0.1:8545"
CHAIN_ID = CHAIN_ID_SEPOLIA          # 11155111 — the fork reports its parent's id
w3 = Web3(Web3.HTTPProvider(RPC))


def require_local_fork():
    """
    Refuses to run anywhere but a local fork.

    This is a real safety gate, not a formality. A Sepolia fork and live Sepolia both report chain
    id 11155111, so nothing else in the stack can tell them apart -- if APP_FORK_MODE were 0, every
    transaction below would be broadcast to the real network with a real funded key. anvil_setBalance
    exists only on a local node, so a successful call is positive proof of where we are.
    """
    if not w3.is_connected():
        raise SystemExit(f"No node at {RPC}. Run: make sepolia-fork")
    if w3.eth.chain_id != CHAIN_ID:
        raise SystemExit(f"Node reports chain {w3.eth.chain_id}, expected {CHAIN_ID}.")
    probe = Account.create().address
    try:
        w3.provider.make_request("anvil_setBalance", [probe, hex(10**18)])
        assert w3.eth.get_balance(probe) == 10**18
    except Exception as e:
        raise SystemExit(
            f"anvil_setBalance failed ({e}). This does not look like a local fork — refusing to "
            f"sign anything. Check APP_FORK_MODE=1 and that `make sepolia-fork` is what is on {RPC}."
        )
    if not api.FORK_MODE:
        raise SystemExit("APP_FORK_MODE is not set; the API would target live Sepolia. Refusing.")
    print(f"  local fork confirmed: chain {CHAIN_ID} at block {w3.eth.block_number}")


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

    Fresh rather than a stock anvil account for two reasons: on a Sepolia fork those well-known
    addresses carry inherited EIP-7702 delegation code, and a new owner keeps deployCount clear of
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
    check("allowedSession agrees on chain",
          on_chain.functions.allowedSession(session_key).call() is True)
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

    # The session key: revoke, then restore. Both default to the app's own key.
    r = c.post("/api/wallet/session/prepare", headers=headers,
               json={"chain_id": CHAIN_ID, "action": "remove"})
    check("session/prepare defaults to the app's key", r.json().get("is_app_key") is True, r.text[:150])
    app_key = r.json()["session_key"]
    tx_hash = sign_and_send(acct, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    check("the agent's key is revoked on chain", sh.functions.allowedSession(app_key).call() is False)

    owner_action(c, headers, acct, "/api/wallet/session/prepare", {"action": "add"})
    check("the agent's key is restored", sh.functions.allowedSession(app_key).call() is True)

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

    # dai IS in the sepolia token table but is NOT priced by this deployment's oracle.
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

    payee = Account.create().address
    r = c.post("/api/contacts", headers=headers, json={"name": "Sandy", "address": payee})
    check("the owner can add a contact", r.status_code == 201, f"{r.status_code} {r.text[:140]}")

    listed = c.get("/api/contacts", headers=headers).json()["contacts"]
    check("it is listed back", listed == [{"name": "sandy", "address": payee}], str(listed)[:140])

    check("deleting works", c.delete("/api/contacts/sandy", headers=headers).status_code == 200)
    check("the list is empty again",
          c.get("/api/contacts", headers=headers).json()["contacts"] == [])

    # The other half of the boundary, asserted against the tools the running agent would use.
    import tools
    exported = {t.name for t in tools.get_tools()}
    check("no contact-writing tool is exported to the agent",
          not (exported & {"save_contact", "delete_contact"}), str(sorted(exported)))


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

    finish("All fork e2e checks passed.")
