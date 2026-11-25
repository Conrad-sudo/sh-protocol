"""
AI apps end to end, against the RUNNING API and a local fork -- Sepolia unless another is named.

An app connects through the real OAuth flow, reads the wallet over /mcp, quotes a token transfer,
and hands the user a link; the user approves it there with their passkey, as Mitfah's page does
(/api/sign/start and /finish with the link's token, no session); the app follows it to confirmed.
Also: a newer quote replaces an older one, cancel_transaction stops a link working, a used link
can't send twice, a personal access token made with the wallet's passkey reads the chain read-only
until revoked, and a trading agent the owner adds trades with its own key and no passkey -- held to
the module's rules and its own limit, its trades made outside Mitfah reported by the keeper's look --
until the owner removes it.

Everything goes to the API over HTTP, as an app and a browser would, so this process never sends
with the API's bundler keys (two processes counting the same keys' nonces collide). What it does
itself: signs the owner's deploy with a fresh EOA, and gives the wallet USDC with anvil cheats.

Requires:
    make sepolia-fork                       (or another fork; started with --hardfork osaka)
    make deploy ARGS=sepolia-fork           (then make db, if the database predates it)
    make api                                (E2E_API_URL, default http://127.0.0.1:8000)

Run: make mcp-e2e [ARGS=arbitrum-fork]
"""
import json
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

from checks import add_contact, check, finish, sign_in   # first: it puts app/ on sys.path

load_dotenv()

import httpx                                          # noqa: E402
from eth_account import Account                       # noqa: E402
from eth_utils import keccak                          # noqa: E402
from langchain_erc20 import ERC20_ABI                 # noqa: E402
from web3 import Web3                                 # noqa: E402
from webauthn.helpers import base64url_to_bytes       # noqa: E402

import mcp_auth                                       # noqa: E402
import passkey                                        # noqa: E402
import routes.catalog                                 # noqa: E402
from constants import get_chain_display_name         # noqa: E402
from db import get_rpc_url, get_token_address         # noqa: E402
from passkey import SoftPasskey                       # noqa: E402

API_URL = os.getenv("E2E_API_URL", "http://127.0.0.1:8000").rstrip("/")
FORK_CHAIN_IDS = {f"{name}-fork": cid for cid, name in routes.catalog.CHAIN_NAME_BY_ID.items()
                  if cid in routes.catalog.FORKABLE_CHAIN_IDS}
NETWORK = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "sepolia-fork"
if NETWORK not in FORK_CHAIN_IDS:
    raise SystemExit(f"Unknown fork '{NETWORK}'. One of: {sorted(FORK_CHAIN_IDS)}")
CHAIN_ID = FORK_CHAIN_IDS[NETWORK]
# How an app names the network: as get_account shows it ("Sepolia"), not by its chain id.
NETWORK_NAME = get_chain_display_name(CHAIN_ID)
RPC = get_rpc_url(NETWORK)
if RPC is None:
    raise SystemExit(f"No rpcs row for '{NETWORK}'. Run: make db")
w3 = Web3(Web3.HTTPProvider(RPC))

# Claude, as the known-app list has it.
CLAUDE_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
PROTOCOL = "2025-06-18"


def require_local_fork(c: httpx.Client):
    """
    Refuses to run anywhere but a local fork, with the API up. A fork and its live chain share a
    chain id, so only a cheat RPC proves where we are (anvil_setBalance exists only on a local node).
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
        raise SystemExit(f"anvil_setBalance failed ({e}): not a local fork. Refusing to sign anything.")
    if not routes.catalog.FORK_MODE:
        raise SystemExit("APP_FORK_MODE is not set; the API would target the live chain. Refusing.")
    try:
        c.get("/api/me")
    except httpx.HTTPError as e:
        raise SystemExit(f"No API at {API_URL} ({e}). Run: make api")
    print(f"  local fork confirmed: {NETWORK}, chain {CHAIN_ID} at block {w3.eth.block_number}; API at {API_URL}")


def sign_and_send(acct, tx: dict) -> str:
    """Signs a prepared transaction with `acct` and broadcasts it, returning the hash."""
    payload = {
        "to": Web3.to_checksum_address(tx["to"]), "data": tx["data"], "value": int(tx.get("value", "0x0"), 16),
        "nonce": int(tx["nonce"], 16), "chainId": int(tx["chainId"], 16), "gas": int(tx["gas"], 16),
    }
    if "maxFeePerGas" in tx:
        payload["maxFeePerGas"] = int(tx["maxFeePerGas"], 16)
        payload["maxPriorityFeePerGas"] = int(tx["maxPriorityFeePerGas"], 16)
    else:
        payload["gasPrice"] = int(tx["gasPrice"], 16)
    return w3.eth.send_raw_transaction(acct.sign_transaction(payload).raw_transaction).hex()


def deal_erc20(token: str, holder: str, amount: int):
    """Sets `holder`'s balance of a standard ERC20 on the fork (forge-std's deal(), by slot search)."""
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


def setup_wallet(c: httpx.Client) -> tuple:
    """
    A brand-new account with a wallet on the fork: SIWE sign-in, a soft passkey made through the
    API, the deploy signed by the owner and confirmed. Returns (owner, headers, passkey, wallet).
    """
    print("\n[1] an account with a wallet and a passkey")
    owner = Account.create()
    w3.provider.make_request("anvil_setBalance", [owner.address, hex(100 * 10**18)])
    _, headers, _ = sign_in(c, owner, CHAIN_ID)

    key = SoftPasskey(rp_id=passkey.rp_id(), origin=passkey.allowed_origins()[0])
    r = c.post("/api/passkeys/register/options", headers=headers)
    assert r.status_code == 200, f"passkey options: {r.status_code} {r.text[:200]}"
    challenge = base64url_to_bytes(r.json()["options"]["challenge"])
    r = c.post("/api/passkeys/register", headers=headers,
               json={"credential": key.registration_response(challenge), "device_name": "MCP e2e phone"})
    assert r.status_code == 200, f"passkey register: {r.status_code} {r.text[:200]}"

    body = {"chain_id": CHAIN_ID, "deployer": owner.address, "daily_limit_usd": 50_000, "watched_tokens": [],
            "count_native": True, "prefund_eth": "1", "key_id": key.key_id}
    r = c.post("/api/deploy", headers=headers, json=body)
    if r.status_code != 200:
        raise SystemExit(f"deploy prepare failed: {r.status_code} {r.text[:300]}")
    prepared = r.json()
    tx_hash = sign_and_send(owner, prepared["tx"])
    check("the deploy transaction succeeds", w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)["status"] == 1)
    r = c.post("/api/deploy/confirm", headers=headers, json={
        "chain_id": CHAIN_ID, "deployer": owner.address, "tx_hash": tx_hash,
        "predicted_address": prepared["predicted_address"]})
    check("the deploy is confirmed, the passkey approved on chain",
          r.status_code == 200 and r.json()["session_key_authorized"] is True, f"{r.status_code} {r.text[:250]}")
    if r.status_code != 200:
        raise SystemExit("cannot continue without a wallet")
    return owner, headers, key, r.json()["wallet_address"]


def connect_app(c: httpx.Client, headers: dict) -> str:
    """
    Claude connecting, as it does: /api/oauth/authorize, the user's Allow (from the same browser,
    by its binding cookie), and the code swapped for tokens. Returns the access token.
    """
    print("\n[2] an AI app connects through OAuth")
    verifier = secrets.token_urlsafe(48)
    r = c.get("/api/oauth/authorize", follow_redirects=False, params={
        "response_type": "code", "client_id": CLAUDE_ID, "redirect_uri": CLAUDE_CB,
        "code_challenge": mcp_auth.s256(verifier), "code_challenge_method": "S256", "state": "e2e",
        "resource": mcp_auth.resource(), "scope": mcp_auth.SCOPE})
    check("authorize sends the browser to the Allow page", r.status_code in (302, 303) and "location" in r.headers,
          f"{r.status_code} {r.text[:200]}")
    request_id = parse_qs(urlsplit(r.headers["location"]).query)["request"][0]
    # Sent by hand: the cookie may be Secure, which httpx keeps off a plain-http request.
    binding = {"Cookie": f"oauth_binding={r.cookies['oauth_binding']}"}
    r = c.post(f"/api/oauth/requests/{request_id}/approve", headers={**headers, **binding})
    check("the user allows it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    code = parse_qs(urlsplit(r.json()["redirect_to"]).query)["code"][0]
    r = c.post("/api/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_ID, "redirect_uri": CLAUDE_CB,
        "code_verifier": verifier, "resource": mcp_auth.resource()})
    check("the code is swapped for an access token", r.status_code == 200 and "access_token" in r.json(),
          f"{r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


def mcp(c: httpx.Client, access: str, method: str, params: dict | None = None) -> httpx.Response:
    """One JSON-RPC call to /mcp, as an app makes it."""
    headers = {"Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream"}
    if method != "initialize":
        headers["MCP-Protocol-Version"] = PROTOCOL
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})}
    return c.post("/mcp", headers=headers, json=body)


def call_tool(c: httpx.Client, access: str, name: str, arguments: dict) -> tuple[bool, object]:
    """A tool call over /mcp: (whether it was an error, its JSON result or its text)."""
    r = mcp(c, access, "tools/call", {"name": name, "arguments": arguments})
    result = r.json().get("result", {}) if r.status_code == 200 else {}
    text = (result.get("content") or [{}])[0].get("text", r.text)
    if result.get("isError"):
        return True, text
    try:
        return False, json.loads(text)
    except ValueError:
        return False, text


def token_of(approve_url: str) -> str:
    """The link token at the end of an approve_url (<issuer>/c/<token>)."""
    return urlsplit(approve_url).path.rsplit("/c/", 1)[1]


def test_reads(c: httpx.Client, access: str, wallet: str):
    """The app sees the account and reads the real chain through the wallet's tools."""
    print("\n[3] the app reads the wallet")
    r = mcp(c, access, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                      "clientInfo": {"name": "mitfah-e2e", "version": "1"}})
    check("initialize answers", r.status_code == 200 and "serverInfo" in r.json().get("result", {}),
          f"{r.status_code} {r.text[:200]}")
    tools = [t["name"] for t in mcp(c, access, "tools/list").json()["result"]["tools"]]
    check("27 tools are listed", len(tools) == 27, str(len(tools)))

    error, account = call_tool(c, access, "get_account", {})
    wallets = {w["chain_id"]: w["address"] for w in account.get("wallets", [])} if not error else {}
    check("get_account names the new wallet", wallets.get(CHAIN_ID) == wallet, str(account)[:300])

    error, native = call_tool(c, access, "get_eth_balance", {"network": CHAIN_ID})
    check("the network may be named by its chain id, as a number", not error and "1" in json.dumps(native), str(native)[:200])
    error, native = call_tool(c, access, "get_eth_balance", {"network": NETWORK_NAME})
    check("get_eth_balance reads the prefund from the chain", not error and "1" in json.dumps(native), str(native)[:200])
    error, usdc = call_tool(c, access, "get_erc20_balance", {"network": NETWORK_NAME, "token": "usdc"})
    check("get_erc20_balance reads the USDC", not error and "10" in json.dumps(usdc), str(usdc)[:200])
    error, status = call_tool(c, access, "get_wallet_status", {"network": NETWORK_NAME})
    check("get_wallet_status reads the limit", not error, str(status)[:200])


def quote(c: httpx.Client, access: str, amount: float) -> dict:
    error, quoted = call_tool(c, access, "transfer_erc20",
                              {"network": NETWORK_NAME, "token": "usdc", "recipient": "payee", "amount": amount})
    if error or not isinstance(quoted, dict):
        raise AssertionError(f"transfer_erc20 refused: {quoted}")
    return quoted


def status_of(c: httpx.Client, access: str, quote_id: str) -> dict:
    error, status = call_tool(c, access, "get_transaction_status", {"quote_id": quote_id})
    return {"error": status} if error else status


def test_replace_and_cancel(c: httpx.Client, access: str):
    """A newer quote cancels the older one; cancel_transaction kills a link; neither link can start."""
    print("\n[4] a newer quote replaces the older; cancel_transaction stops a link")
    first = quote(c, access, 1)
    check("a quote comes back waiting, with a link to Mitfah's page",
          first["status"] == "waiting_for_approval" and first["approve_url"].startswith(mcp_auth.issuer() + "/c/"),
          str(first)[:300])
    check("...and get_transaction_status agrees",
          status_of(c, access, first["quote_id"]).get("status") == "waiting_for_approval")

    second = quote(c, access, 2)
    check("a second quote on the network cancels the first",
          status_of(c, access, first["quote_id"]).get("status") == "cancelled"
          and status_of(c, access, second["quote_id"]).get("status") == "waiting_for_approval")
    r = c.post("/api/sign/start", json={"token": token_of(first["approve_url"])})
    check("...whose link no longer opens", r.status_code == 404, f"{r.status_code} {r.text[:200]}")

    error, cancelled = call_tool(c, access, "cancel_transaction", {"quote_id": second["quote_id"]})
    check("cancel_transaction cancels the second", not error and cancelled.get("status") == "cancelled", str(cancelled))
    r = c.post("/api/sign/start", json={"token": token_of(second["approve_url"])})
    check("...whose link no longer opens either", r.status_code == 404, f"{r.status_code} {r.text[:200]}")


def test_approve_through_link(c: httpx.Client, access: str, headers: dict, key: SoftPasskey, wallet: str, payee: str):
    """The quote approved on the link's page with the passkey, sent, and followed to confirmed."""
    print("\n[5] the user approves through the link; the app follows it to confirmed")
    usdc = w3.eth.contract(address=Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc")), abi=ERC20_ABI)
    one = 10 ** usdc.functions.decimals().call()
    before = usdc.functions.balanceOf(payee).call()
    history_before = len(c.get(f"/api/chat/history?chain_id={CHAIN_ID}", headers=headers).json().get("messages", []))

    quoted = quote(c, access, 1)
    print(f"  quote: {quoted['action']} on {quoted['network']}; fees {quoted['fees']}; "
          f"expires in {quoted['expires_in_minutes']} min")
    token = token_of(quoted["approve_url"])

    # The page has no session: it names the request by the link's token alone.
    r = c.post("/api/sign/start", json={"token": token})
    started = r.json() if r.status_code == 200 else {}
    check("the link's page opens the approval, as an app's",
          r.status_code == 200 and started["summary"].get("origin") == "mcp"
          and started["summary"]["action"] == quoted["action"], f"{r.status_code} {r.text[:300]}")
    check("...naming the passkey that may approve it",
          [a["id"] for a in started.get("allow_credentials", [])] == [key.credential_id_b64])

    credential = key.assertion(base64url_to_bytes(started["challenge"]))
    r = c.post("/api/sign/finish", json={"sign_id": started["sign_id"], "credential": credential, "token": token})
    check("the passkey's approval sends it", r.status_code == 200 and r.json()["status"] == "sent",
          f"{r.status_code} {r.text[:300]}")

    status, deadline = {}, time.time() + 120
    while time.time() < deadline:
        status = status_of(c, access, quoted["quote_id"])
        if status.get("status") not in ("sending", "sent", "waiting_for_approval"):
            break
        time.sleep(1)
    print(f"  status: {status}")
    receipt = w3.eth.get_transaction_receipt(status["tx_hash"]) if status.get("tx_hash") else None
    check("get_transaction_status follows it to confirmed, with its hash",
          status.get("status") == "confirmed" and receipt is not None and receipt["status"] == 1, str(status))
    check("the payee got exactly 1 USDC", usdc.functions.balanceOf(payee).call() - before == one)

    again = c.post("/api/sign/finish", json={"sign_id": started["sign_id"], "credential": credential, "token": token})
    check("a second approval sends nothing", again.status_code == 409, f"{again.status_code} {again.text[:200]}")
    check("...and the link no longer opens", c.post("/api/sign/start", json={"token": token}).status_code == 404)

    history_after = len(c.get(f"/api/chat/history?chain_id={CHAIN_ID}", headers=headers).json().get("messages", []))
    check("the web chat is left alone", history_after == history_before, f"{history_before} -> {history_after}")
    listed = c.get(f"/api/transactions?chain_id={CHAIN_ID}&limit=50", headers=headers).json()["transactions"]
    row = next((t for t in listed if t["tx_hash"] == status["tx_hash"].lower()), None)
    check("the History tab lists it, confirmed, as an AI app's",
          row is not None and row["status"] == "confirmed" and row["source"] == "app", str(row))
    error, again = call_tool(c, access, "get_transaction_status", {"quote_id": quoted["quote_id"]})
    check("the confirmed status carries no leftover 'Sending' text", "outcome" not in again, str(again))


def test_personal_token(c: httpx.Client, headers: dict, key: SoftPasskey, wallet: str):
    """
    A personal access token, made with the passkey the wallet approves on chain (the server reads the
    wallet to check it), reads the real chain read-only, is refused a quote, and stops on Revoke.
    """
    print("\n[6] a personal access token for the user's own script")
    r = c.post("/api/tokens/options", headers=headers)
    check("the token form gets a passkey prompt", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    credential = key.assertion(base64url_to_bytes(r.json()["challenge"]))
    r = c.post("/api/tokens", headers=headers,
               json={"name": "e2e script", "days": 7, "access": "read", "credential": credential})
    check("the passkey the wallet approves on chain makes a token", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    token = r.json()["token"]
    ok = mcp(c, token, "tools/list")
    names = {t["name"] for t in ok.json()["result"]["tools"]} if ok.status_code == 200 else set()
    check("it lists the reads but no quotes", "get_eth_balance" in names and "transfer_erc20" not in names, str(sorted(names)))
    error, result = call_tool(c, token, "get_eth_balance", {"network": NETWORK_NAME})
    on_chain = w3.eth.get_balance(Web3.to_checksum_address(wallet)) / 10**18
    check("it reads the wallet's balance off the chain", not error and abs(float(result["balance"]) - on_chain) < 1e-6,
          str(result))
    error, text = call_tool(c, token, "transfer_erc20", {"network": NETWORK_NAME, "token": "usdc", "recipient": "payee", "amount": 1})
    check("a quote is refused: it's read-only", error and "read-only" in str(text), str(text))
    row = next(x for x in c.get("/api/connections", headers=headers).json()["connections"] if x["kind"] == "token")
    check("Controls lists it", row["app"]["name"] == "e2e script" and row["access"] == "read", str(row))
    check("revoked", c.delete(f"/api/connections/{row['id']}", headers=headers).status_code == 204)
    check("it stops working at once", mcp(c, token, "tools/list").status_code == 401)


def owner_action(c: httpx.Client, headers: dict, owner, path: str, body: dict, confirm: str, **confirm_body):
    """An owner action as the web app runs it: prepared, signed and sent by the owner, then confirmed."""
    r = c.post(path, headers=headers, json={"chain_id": CHAIN_ID, **body})
    if r.status_code != 200:
        raise AssertionError(f"{path}: {r.status_code} {r.text[:300]}")
    tx_hash = sign_and_send(owner, r.json()["tx"])
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    for _ in range(60):
        r = c.post(confirm, headers=headers, json={"chain_id": CHAIN_ID, "tx_hash": tx_hash, **confirm_body})
        if r.status_code != 202:
            break
        time.sleep(1)
    return r


def follow(c: httpx.Client, access: str, quote_id: str) -> dict:
    """get_transaction_status until it settles (or two minutes pass)."""
    status, deadline = {}, time.time() + 120
    while time.time() < deadline:
        status = status_of(c, access, quote_id)
        if status.get("status") not in ("sending", "sent", "waiting_for_approval", "waiting_for_signature"):
            break
        time.sleep(1)
    return status


def test_trading_agent(c: httpx.Client, headers: dict, owner, key: SoftPasskey, wallet: str, payee: str):
    """
    The user's own trading agent: added by the owner, trading through its own token with its own key, held
    to the module's rules and its own limit, every trade made outside Mitfah reported by the keeper's look,
    and stopped by the owner's removal.
    """
    print("\n[7] a trading agent: its own key, its own limit, no passkey")
    import agent_ops
    import agents
    import signing
    from abi import ientry_point
    from db import get_json, get_sign_request_by_quote

    usdc = w3.eth.contract(address=Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc")), abi=ERC20_ABI)
    one = 10 ** usdc.functions.decimals().call()
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    agent = Account.create()

    def wallet_abi_contract():
        return w3.eth.contract(address=Web3.to_checksum_address(wallet),
                               abi=get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])

    r = c.post("/api/tokens/options", headers=headers)
    credential = key.assertion(base64url_to_bytes(r.json()["challenge"]))
    r = c.post("/api/tokens", headers=headers,
               json={"name": "e2e agent", "days": 7, "access": "prepare", "credential": credential})
    check("the agent gets a token that can read and prepare", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    token = r.json()["token"]
    names = {t["name"] for t in mcp(c, token, "tools/list").json()["result"]["tools"]}
    check("...which lists the agent tools", {"get_agent_setup", "prepare_agent_trade", "send_agent_trade"} <= names,
          str(sorted(names)))

    r = owner_action(c, headers, owner, "/api/wallet/agents/prepare",
                     {"action": "add", "agent": agent.address, "limit_usd": "5", "days": 7},
                     "/api/wallet/agents/confirm", name="e2e bot")
    listed = r.json().get("agents", {}).get("agents", []) if r.status_code == 200 else []
    check("the owner adds the agent: $5 a period, 7 days, named in Mitfah",
          r.status_code == 200 and [(a["address"], a["name"], a["limit_usd"], a["active"]) for a in listed]
          == [(agent.address, "e2e bot", 5.0, True)], f"{r.status_code} {r.text[:300]}")
    error, setup = call_tool(c, token, "get_agent_setup", {"network": NETWORK_NAME})
    check("get_agent_setup shows it, and that the limit counts the native asset",
          not error and setup["counts_native"] is True and [a["name"] for a in setup["agents"]] == ["e2e bot"], str(setup))

    transfer = lambda amount: [{"to": usdc.address, "data": usdc.encode_abi("transfer", args=[payee, amount])}]  # noqa: E731
    error, text = call_tool(c, token, "prepare_agent_trade",
                            {"network": NETWORK_NAME, "agent": agent.address, "calls": transfer(one)})
    check("moving a token the limit doesn't count is refused before anything is signed",
          error and "doesn't count it" in str(text), str(text))
    r = owner_action(c, headers, owner, "/api/wallet/watched-tokens/prepare", {"token": "usdc", "action": "add"},
                     "/api/wallet/tx/confirm")
    check("the owner has the limit count USDC", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    def prepared(calls=None, quote_id=None) -> dict:
        arguments = {"network": NETWORK_NAME, "agent": agent.address}
        arguments |= {"calls": calls} if calls is not None else {"quote_id": quote_id}
        error, result = call_tool(c, token, "prepare_agent_trade", arguments)
        if error:
            raise AssertionError(f"prepare_agent_trade refused: {result}")
        return result

    trade = prepared(transfer(one))
    check("a trade is prepared, waiting for the agent's signature, with its typed data",
          trade["status"] == "waiting_for_signature" and trade["typed_data"]["primaryType"] == "AgentOperation"
          and trade["agent_limit_left"] == "$5.00", str({k: v for k, v in trade.items() if k != "typed_data"}))
    from userop import _v07_user_op_hash
    shown = trade["user_op"]
    as_op = (shown["sender"], int(shown["nonce"], 16), b"", bytes.fromhex(shown["callData"][2:]),
             bytes.fromhex(shown["accountGasLimits"][2:]), int(shown["preVerificationGas"], 16),
             bytes.fromhex(shown["gasFees"][2:]), b"", b"")
    recomputed = "0x" + _v07_user_op_hash(as_op, shown["entryPoint"], shown["chainId"]).hex()
    execute_call = wallet_abi_contract().encode_abi("execute", args=[b"\x00" * 32, bytes.fromhex(
        usdc.address[2:] + "00" * 32 + transfer(one)[0]["data"][2:])])
    check("the agent can check what it signs: the op's own hash, and calls it can decode",
          recomputed == trade["typed_data"]["message"]["userOpHash"]
          and shown["callData"] == "0x8dd7712f" + execute_call[2:], f"{recomputed} {shown['callData'][:80]}")
    stranger = Account.create().sign_typed_data(full_message=trade["typed_data"]).signature.hex()
    error, text = call_tool(c, token, "send_agent_trade", {"quote_id": trade["quote_id"], "signature": stranger})
    check("someone else's signature is refused", error and "not by the agent" in str(text), str(text))
    signature = agent.sign_typed_data(full_message=trade["typed_data"]).signature.hex()
    before = usdc.functions.balanceOf(payee).call()
    error, sent = call_tool(c, token, "send_agent_trade", {"quote_id": trade["quote_id"], "signature": signature})
    check("the agent's own signature sends it, no passkey", not error and sent["status"] == "sending", str(sent))
    status = follow(c, token, trade["quote_id"])
    print(f"  status: {status}")
    check("get_transaction_status follows it to confirmed", status.get("status") == "confirmed" and status.get("tx_hash"),
          str(status))
    check("the payee got exactly 1 USDC", usdc.functions.balanceOf(payee).call() - before == one)
    module = agents.module_contract(w3, setup["signing"]["domain"]["verifyingContract"])
    state = agents.read_agent(module, Web3.to_checksum_address(wallet), agent.address)
    print(f"  agent spent ${state.spent / 1e18:.4f} of ${state.limit / 1e18:.2f}")
    check("it counted toward the agent's own limit (the dollar and Mitfah's fee)", 1e18 <= state.spent < 2e18, str(state))
    error, text = call_tool(c, token, "send_agent_trade", {"quote_id": trade["quote_id"], "signature": signature})
    check("a trade is sent once", error and "already sent" in str(text), str(text))
    rows = c.get(f"/api/transactions?chain_id={CHAIN_ID}&limit=50", headers=headers).json()["transactions"]
    row = next((t for t in rows if t["tx_hash"] == status.get("tx_hash", "").lower()), None)
    check("the History tab lists it as the agent's, by name",
          row is not None and row["source"] == "agent" and row.get("agent") == {"address": agent.address, "name": "e2e bot"},
          str(row))

    error, text = call_tool(c, token, "prepare_agent_trade",
                            {"network": NETWORK_NAME, "agent": agent.address, "calls": transfer(5 * one)})
    check("a trade past the agent's own limit is refused before anything is signed",
          error and "past its own limit" in str(text), str(text))

    error, quoted = call_tool(c, token, "transfer_erc20",
                              {"network": NETWORK_NAME, "token": "usdc", "recipient": "payee", "amount": 1})
    check("the agent's token can quote, as an app's can", not error and "approve_url" in quoted, str(quoted))
    link = token_of(quoted["approve_url"])
    taken = prepared(quote_id=quoted["quote_id"])
    check("a quote runs as the agent's trade", taken["action"] == quoted["action"], str(taken.get("action")))
    check("...and its approve link stops working", c.post("/api/sign/start", json={"token": link}).status_code == 404)
    signature = agent.sign_typed_data(full_message=taken["typed_data"]).signature.hex()
    error, sent = call_tool(c, token, "send_agent_trade", {"quote_id": taken["quote_id"], "signature": signature})
    status = follow(c, token, taken["quote_id"])
    check("...and is sent with the agent's signature", not error and status.get("status") == "confirmed", str(status))

    # Outside Mitfah: the agent signs a trade and sends it to the EntryPoint itself, from an account of its own.
    outside = prepared(transfer(one // 2))
    request = get_sign_request_by_quote(user_id, CHAIN_ID, outside["quote_id"])
    frozen, final = signing.thaw(request), signing.built_op(request)
    ecdsa = bytes.fromhex(agent.sign_typed_data(full_message=outside["typed_data"]).signature.hex().removeprefix("0x"))
    op = final.op[:-1] + (agent_ops.pack_signature(agent.address, frozen["epoch"], frozen["deadline"], ecdsa),)
    wallet_contract = w3.eth.contract(address=Web3.to_checksum_address(wallet),
                                      abi=get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"])
    entry_point = w3.eth.contract(address=wallet_contract.functions.ENTRY_POINT().call(), abi=ientry_point)
    relayer = Account.create()
    w3.provider.make_request("anvil_setBalance", [relayer.address, hex(10**18)])
    tx = entry_point.functions.handleOps([op], relayer.address).build_transaction({
        "from": relayer.address, "nonce": 0, "gas": 2_000_000, "chainId": CHAIN_ID,
        "maxFeePerGas": final.max_fee * 2, "maxPriorityFeePerGas": final.tip})
    landed = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(relayer.sign_transaction(tx).raw_transaction), timeout=120)
    check("the agent's key can trade without Mitfah, too", landed["status"] == 1)
    told: list = []
    agents.check_agents(w3, CHAIN_ID, lambda user, chain, text: told.append((user, text)))
    ours = [text for user, text in told if user == user_id]
    print(f"  keeper: {ours}")
    check("the keeper's next look tells the owner it spent outside Mitfah, once",
          len(ours) == 1 and "outside Mitfah" in ours[0] and "“e2e bot”" in ours[0], str(told))
    told.clear()
    agents.check_agents(w3, CHAIN_ID, lambda user, chain, text: told.append((user, text)))
    check("...and the look after says nothing", not [t for u, t in told if u == user_id], str(told))

    r = owner_action(c, headers, owner, "/api/wallet/agents/prepare", {"action": "remove", "agent": agent.address},
                     "/api/wallet/agents/confirm")
    check("the owner removes the agent", r.status_code == 200 and r.json()["agents"]["agents"] == [], f"{r.status_code} {r.text[:200]}")
    error, text = call_tool(c, token, "prepare_agent_trade",
                            {"network": NETWORK_NAME, "agent": agent.address, "calls": transfer(one)})
    check("...and it can trade no more", error and "not one of this wallet's agents" in str(text), str(text))


if __name__ == "__main__":
    print("=== preflight ===")
    with httpx.Client(base_url=API_URL, timeout=180) as client:
        require_local_fork(client)
        owner, auth_headers, phone, deployed = setup_wallet(client)
        print(f"  owner {owner.address}, wallet {deployed}")
        usdc_address = Web3.to_checksum_address(get_token_address(CHAIN_ID, "usdc"))
        decimals = w3.eth.contract(address=usdc_address, abi=ERC20_ABI).functions.decimals().call()
        deal_erc20(usdc_address, deployed, 10 * 10**decimals)
        payee_address = Account.create().address
        r = add_contact(client, auth_headers, owner, "payee", payee_address)
        check("the owner saves the payee as a contact", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")

        access_token = connect_app(client, auth_headers)
        test_reads(client, access_token, deployed)
        test_replace_and_cancel(client, access_token)
        test_approve_through_link(client, access_token, auth_headers, phone, deployed, payee_address)
        test_personal_token(client, auth_headers, phone, deployed)
        test_trading_agent(client, auth_headers, owner, phone, deployed, payee_address)

    finish(f"All AI-app fork checks passed on {NETWORK}.")
