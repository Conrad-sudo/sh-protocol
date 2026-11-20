"""
AI apps end to end, against the RUNNING API and a local fork -- Sepolia unless another is named.

An app connects through the real OAuth flow, reads the wallet over /mcp, quotes a token transfer,
and hands the user a link; the user approves it there with their passkey, as Mitfah's page does
(/api/sign/start and /finish with the link's token, no session); the app follows it to confirmed.
Also: a newer quote replaces an older one, cancel_transaction stops a link working, and a used link
can't send twice.

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

    finish(f"All AI-app fork checks passed on {NETWORK}.")
