"""
Checks how AI apps connect to a Mitfah account -- this server's OAuth for MCP, the Allow page's API,
connected apps, /mcp itself -- and signing in to the website with a passkey. Run against a
throwaway database.

The properties under test: an app gets a code only at an address the real app owns, and only the
browser that opened the request can approve it; a token speaks for the account that approved it and
no other, and stops working the moment the user disconnects the app or a copy of it shows up; a
passkey signs in only if a wallet of its account approves it.

Offline: no chain and no RPC. Whether a wallet approves a passkey is stood in for.

Run: make mcp-test   (or: python app/tests/test_mcp.py)
"""
import contextlib
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from checks import check, finish, set_everywhere, sign_in   # first: it puts app/ on sys.path for the imports below

# A scratch database, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"                   # the test client speaks http
os.environ["SIWE_DOMAIN"] = "localhost:3000"
os.environ["WEB_APP_URL"] = "http://localhost:3000"  # the issuer; set before .env can set another

import db                                   # noqa: E402

db.DB_PATH = _tmp_db.name
db.init_db()

from fastapi.testclient import TestClient   # noqa: E402
from webauthn.helpers import base64url_to_bytes  # noqa: E402

import api                                  # noqa: E402
import mcp_auth                             # noqa: E402
import mcp_server                           # noqa: E402
import routes.passkeys                      # noqa: E402
from mcp_clients import CHATGPT, CLAUDE, CLAUDE_CODE, find_app  # noqa: E402
from passkey import SoftPasskey             # noqa: E402

# Passkeys are for `localhost`, asked from the dev server's origin, whatever .env says.
os.environ.pop("WEBAUTHN_RP_ID", None)
os.environ.pop("WEBAUTHN_ORIGINS", None)

ISSUER = "http://localhost:3000"
RESOURCE = ISSUER + "/mcp"
CLAUDE_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_ID = "https://chatgpt.com/oauth/client.json"
CHATGPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"


@asynccontextmanager
async def _mcp_only(_app):
    """The API's lifespan without the agent: only the MCP session manager runs."""
    async with mcp_server.running():
        yield


def make_client() -> TestClient:
    """A test client with MCP running, the agent left out and the rate limits off. Use it in `with`."""
    api.app.router.lifespan_context = _mcp_only
    api.limiter.enabled = False
    return TestClient(api.app)


def authorize(c: TestClient, *, client_id=CLAUDE_ID, redirect_uri=CLAUDE_CB, verifier=None, **extra):
    """Opens /api/oauth/authorize as an app would. Returns (the response, the PKCE verifier)."""
    verifier = verifier or secrets.token_urlsafe(48)
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": mcp_auth.s256(verifier), "code_challenge_method": "S256",
        "state": "st-1", "resource": RESOURCE, "scope": "wallet", **extra,
    }
    params = {k: v for k, v in params.items() if v is not None}
    return c.get("/api/oauth/authorize", params=params, follow_redirects=False), verifier


def query_of(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def request_id(response) -> str:
    return query_of(response.headers["location"])["request"]


def approved_code(c: TestClient, headers: dict, **kwargs) -> tuple[str, str]:
    """Authorizes and approves as the signed-in user. Returns (the code, the verifier)."""
    r, verifier = authorize(c, **kwargs)
    approved = c.post(f"/api/oauth/requests/{request_id(r)}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return query_of(approved.json()["redirect_to"])["code"], verifier


def exchange(c: TestClient, code: str, verifier: str, *, client_id=CLAUDE_ID, redirect_uri=CLAUDE_CB, **extra):
    form = {"grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": redirect_uri, "code_verifier": verifier, "resource": RESOURCE, **extra}
    return c.post("/api/oauth/token", data={k: v for k, v in form.items() if v is not None})


def connect(c: TestClient, headers: dict, **kwargs) -> dict:
    """The whole flow, as an app and its user go through it. Returns the token response."""
    code, verifier = approved_code(c, headers, **kwargs)
    client = {k: kwargs[k] for k in ("client_id", "redirect_uri") if k in kwargs}
    r = exchange(c, code, verifier, **client)
    assert r.status_code == 200, r.text
    return r.json()


def refresh(c: TestClient, refresh_token: str, **extra):
    return c.post("/api/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                             "client_id": CLAUDE_ID, **extra})


def mcp(c: TestClient, access: str, method: str, params: dict | None = None, *, version: str = "2025-06-18"):
    """One JSON-RPC call to /mcp with `access` as the bearer token."""
    headers = {"Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream"}
    if method != "initialize":
        headers["MCP-Protocol-Version"] = version
    if version >= "2026-07-28":
        # The per-request protocol repeats the method (and a tool call's name) in headers, for proxies.
        headers["Mcp-Method"] = method
        if method == "tools/call":
            headers["Mcp-Name"] = params["name"]
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", headers=headers, json=body)


def account_of(c: TestClient, access: str) -> dict:
    """get_account over MCP, decoded; {} when the call didn't go through."""
    import json

    r = mcp(c, access, "tools/call", {"name": "get_account", "arguments": {}})
    if r.status_code != 200 or "result" not in r.json():
        return {}
    return json.loads(r.json()["result"]["content"][0]["text"])


def works(c: TestClient, access: str) -> bool:
    return mcp(c, access, "tools/list").status_code == 200


# ── 1. Discovery ──────────────────────────────────────────────────────────────


def test_discovery(c: TestClient):
    print("\n[1] Discovery: what an app reads before it signs in")
    m = c.get("/.well-known/oauth-authorization-server").json()
    check("the issuer is the web app's address", m["issuer"] == ISSUER, m["issuer"])
    check("the endpoints are on it",
          (m["authorization_endpoint"], m["token_endpoint"], m["revocation_endpoint"])
          == (f"{ISSUER}/api/oauth/authorize", f"{ISSUER}/api/oauth/token", f"{ISSUER}/api/oauth/revoke"))
    check("CIMD client ids are declared welcome (Claude needs it to use one)", m["client_id_metadata_document_supported"] is True)
    check("no client secret: token auth 'none' only", m["token_endpoint_auth_methods_supported"] == ["none"])
    check("PKCE S256 only", m["code_challenge_methods_supported"] == ["S256"])
    check("iss comes back with every answer (RFC 9207)", m["authorization_response_iss_parameter_supported"] is True)
    check("no registration endpoint: known apps only", "registration_endpoint" not in m)
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        prm = c.get(path).json()
        check(f"{path} names /mcp and this server",
              prm["resource"] == RESOURCE and prm["authorization_servers"] == [ISSUER], str(prm))


# ── 2. The known list ─────────────────────────────────────────────────────────


def test_known_list():
    print("\n[2] The known list: client id AND redirect must belong to one app")
    cases = [
        ("Claude, at Claude's callback", CLAUDE_ID, CLAUDE_CB, CLAUDE),
        ("Claude, at claude.com's callback", CLAUDE_ID, "https://claude.com/api/mcp/auth_callback", CLAUDE),
        ("Claude Code, any loopback port", "https://claude.ai/oauth/claude-code-client-metadata",
         "http://127.0.0.1:53682/callback", CLAUDE_CODE),
        ("Claude Code, localhost without a port", "https://claude.ai/oauth/claude-code-client-metadata",
         "http://localhost/callback", CLAUDE_CODE),
        ("ChatGPT, at its redirect", CHATGPT_ID, CHATGPT_CB, CHATGPT),
        ("ChatGPT, at a connector callback", CHATGPT_ID, "https://chatgpt.com/connector/oauth/abc_123-X", CHATGPT),
        ("Claude's client id with ChatGPT's redirect", CLAUDE_ID, CHATGPT_CB, None),
        ("ChatGPT's client id with Claude's callback", CHATGPT_ID, CLAUDE_CB, None),
        ("Claude Code over https loopback", "https://claude.ai/oauth/claude-code-client-metadata",
         "https://127.0.0.1:5000/callback", None),
        ("Claude Code at another path", "https://claude.ai/oauth/claude-code-client-metadata",
         "http://127.0.0.1:5000/callback/x", None),
        ("Claude Code with a query", "https://claude.ai/oauth/claude-code-client-metadata",
         "http://127.0.0.1:5000/callback?next=https://evil.example", None),
        ("Claude Code at a host that only starts with localhost", "https://claude.ai/oauth/claude-code-client-metadata",
         "http://localhost.evil.example/callback", None),
        ("Claude Code with Claude's callback", "https://claude.ai/oauth/claude-code-client-metadata", CLAUDE_CB, None),
        ("a connector callback with a path in its id", CHATGPT_ID, "https://chatgpt.com/connector/oauth/../x", None),
        ("someone else's client", "https://evil.example/client.json", CLAUDE_CB, None),
        ("a client id that only looks like claude.ai", "https://claude.ai@evil.example/x", CLAUDE_CB, None),
        ("a client id over http", "http://claude.ai/oauth/x", CLAUDE_CB, None),
        ("a client id with no path", "https://claude.ai/", CLAUDE_CB, None),
        ("a client id on another port", "https://claude.ai:8443/x", CLAUDE_CB, None),
        ("another client id on claude.ai", "https://claude.ai/oauth/other-client", CLAUDE_CB, None),
        ("Claude's client id path on claude.com", "https://claude.com/oauth/mcp-oauth-client-metadata", CLAUDE_CB, None),
        ("Claude's callback with a different case", CLAUDE_ID, "https://claude.ai/api/mcp/Auth_Callback", None),
    ]
    for label, client_id, redirect_uri, expected in cases:
        check(label, find_app(client_id, redirect_uri) == expected, str(find_app(client_id, redirect_uri)))


# ── 3. /mcp needs a token ─────────────────────────────────────────────────────


def test_mcp_needs_a_token(c: TestClient):
    print("\n[3] /mcp without a good token")
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    challenge = r.headers.get("www-authenticate", "")
    check("no token: 401", r.status_code == 401, str(r.status_code))
    check("which names the protected-resource metadata (how apps start signing in)",
          f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"' in challenge, challenge)
    check("without calling a missing token invalid", "invalid_token" not in challenge, challenge)
    r = mcp(c, "mfa_" + secrets.token_urlsafe(32), "tools/list")
    check("a made-up token: 401 invalid_token",
          r.status_code == 401 and 'error="invalid_token"' in r.headers.get("www-authenticate", ""), r.text)
    _, headers, _ = sign_in(c)
    r = mcp(c, headers["Authorization"].split()[1], "tools/list")
    check("the website's own access token isn't an MCP token", r.status_code == 401, str(r.status_code))


# ── 4. /api/oauth/authorize ───────────────────────────────────────────────────


def test_authorize(c: TestClient):
    print("\n[4] Authorize: what reaches the Allow page, and what goes back to the app")
    r, _ = authorize(c, client_id="https://evil.example/client.json", redirect_uri="https://evil.example/cb")
    location = r.headers.get("location", "")
    check("an unknown app is shown an error on OUR page", r.status_code == 302 and location == "/connect?error=unknown_app", location)
    r, _ = authorize(c, redirect_uri="https://evil.example/cb")
    check("Claude's client id with another redirect: the same, never sent to that redirect",
          r.headers.get("location") == "/connect?error=unknown_app", r.headers.get("location", ""))
    r = c.get(f"/api/oauth/authorize?client_id={CLAUDE_ID}&client_id=https://evil.example/c.json"
              f"&redirect_uri={CLAUDE_CB}", follow_redirects=False)
    check("a parameter sent twice: refused on our page", r.headers.get("location") == "/connect?error=invalid_request",
          r.headers.get("location", ""))

    def app_error(response) -> dict:
        location = response.headers.get("location", "")
        return query_of(location) if location.startswith(CLAUDE_CB + "?") else {}

    e = app_error(authorize(c, response_type="token")[0])
    check("response_type=token goes back to the app as unsupported_response_type, with state and iss",
          (e.get("error"), e.get("state"), e.get("iss")) == ("unsupported_response_type", "st-1", ISSUER), str(e))
    e = app_error(authorize(c, code_challenge_method=None)[0])
    check("no PKCE method: invalid_request", e.get("error") == "invalid_request", str(e))
    e = app_error(authorize(c, code_challenge_method="plain")[0])
    check("PKCE 'plain': invalid_request", e.get("error") == "invalid_request", str(e))
    e = app_error(authorize(c, resource="https://evil.example/mcp")[0])
    check("tokens for another server: invalid_target", e.get("error") == "invalid_target", str(e))
    e = app_error(authorize(c, state="x" * 2000)[0])
    check("an oversized state is refused, and not echoed back", e.get("error") == "invalid_request" and "state" not in e, str(e))

    r, _ = authorize(c)
    cookie = r.headers.get("set-cookie", "")
    check("a good request goes to the Allow page", r.headers.get("location", "").startswith("/connect?request="),
          r.headers.get("location", ""))
    check("with an httpOnly binding cookie scoped to /api/oauth",
          "oauth_binding=" in cookie and "httponly" in cookie.lower() and "path=/api/oauth" in cookie.lower(), cookie)
    binding = c.cookies.get("oauth_binding")
    authorize(c)
    check("one binding per browser: a second request keeps the same cookie", c.cookies.get("oauth_binding") == binding)


# ── 5. The Allow page ─────────────────────────────────────────────────────────


def test_allow_page(c: TestClient):
    print("\n[5] The Allow page: who may answer, and what the answers say")
    _, headers, _ = sign_in(c)
    r, _ = authorize(c)
    rid = request_id(r)
    check("signed out: 401", c.get(f"/api/oauth/requests/{rid}").status_code == 401)
    other_browser = TestClient(api.app)   # its own cookie jar; no lifespan of its own
    check("another browser, signed in as the same user: 403 (no binding cookie)",
          other_browser.get(f"/api/oauth/requests/{rid}", headers=headers).status_code == 403)
    check("and it can't approve either",
          other_browser.post(f"/api/oauth/requests/{rid}/approve", headers=headers).status_code == 403)
    check("an unknown request: 404", c.get("/api/oauth/requests/nope", headers=headers).status_code == 404)
    shown = c.get(f"/api/oauth/requests/{rid}", headers=headers).json()
    check("the app is named from the known list", shown["app"] == {"key": "claude", "name": "Claude", "site": "claude.ai"},
          str(shown))
    r = c.post(f"/api/oauth/requests/{rid}/approve", headers=headers)
    q = query_of(r.json()["redirect_to"])
    check("Allow sends the browser to the app's callback with a code, the state and iss",
          r.json()["redirect_to"].startswith(CLAUDE_CB + "?") and q["code"].startswith("mfc_")
          and q["state"] == "st-1" and q["iss"] == ISSUER, r.text)
    check("a request is answered once: 410", c.post(f"/api/oauth/requests/{rid}/approve", headers=headers).status_code == 410)

    r, _ = authorize(c, client_id=CHATGPT_ID, redirect_uri=CHATGPT_CB, state="st-2")
    denied = c.post(f"/api/oauth/requests/{request_id(r)}/deny", headers=headers).json()["redirect_to"]
    q = query_of(denied)
    check("Deny tells the app access_denied, with its state and iss",
          denied.startswith(CHATGPT_CB + "?") and (q["error"], q["state"], q["iss"]) == ("access_denied", "st-2", ISSUER), denied)

    r, _ = authorize(c)
    rid = request_id(r)
    db.get_db().execute("UPDATE oauth_requests SET expires_at = ? WHERE id = ?", (int(time.time()) - 1, rid))
    db.get_db().commit()
    check("a request past its 10 minutes: 410", c.post(f"/api/oauth/requests/{rid}/approve", headers=headers).status_code == 410)


# ── 6. The token endpoint ─────────────────────────────────────────────────────


def test_token_endpoint(c: TestClient):
    print("\n[6] Swapping a code for tokens")
    _, headers, _ = sign_in(c)
    code, verifier = approved_code(c, headers)
    for label, kwargs, error in [
        ("a wrong PKCE verifier", {"verifier": secrets.token_urlsafe(48)}, "invalid_grant"),
        ("another redirect_uri", {"redirect_uri": "https://claude.com/api/mcp/auth_callback"}, "invalid_grant"),
        ("another client id", {"client_id": CHATGPT_ID}, "invalid_grant"),
        ("no verifier", {"verifier": None}, "invalid_request"),
        ("tokens for another server", {"resource": "https://evil.example/mcp"}, "invalid_target"),
    ]:
        args = {"verifier": verifier, **kwargs}
        r = exchange(c, code, args.pop("verifier"), **args)
        check(f"{label}: {error}", r.status_code == 400 and r.json().get("error") == error, r.text)
    r = exchange(c, code, verifier)
    body = r.json()
    check("the right verifier: tokens", r.status_code == 200 and body["access_token"].startswith("mfa_")
          and body["refresh_token"].startswith("mfr_"), r.text)
    check("Bearer, an hour, the one permission",
          (body["token_type"], body["expires_in"], body["scope"]) == ("Bearer", 3600, "wallet"), str(body))
    check("never cached", r.headers.get("cache-control") == "no-store", r.headers.get("cache-control", ""))
    check("the token works on /mcp", works(c, body["access_token"]))
    r = exchange(c, code, verifier)
    check("the same code again: invalid_grant", r.status_code == 400 and r.json()["error"] == "invalid_grant", r.text)
    check("and the connection the first exchange made is ended", not works(c, body["access_token"]))

    code, verifier = approved_code(c, headers)
    db.get_db().execute("UPDATE oauth_requests SET code_expires_at = ? WHERE code_hash = ?",
                        (int(time.time()) - 1, mcp_auth.hash_token(code)))
    db.get_db().commit()
    r = exchange(c, code, verifier)
    check("a code past its 60 seconds: invalid_grant", r.status_code == 400 and r.json()["error"] == "invalid_grant", r.text)

    r = c.post("/api/oauth/token", json={"grant_type": "authorization_code", "code": "x"})
    check("JSON instead of a form: invalid_request", r.status_code == 400 and r.json()["error"] == "invalid_request", r.text)
    r = c.post("/api/oauth/token", content="grant_type=authorization_code&code=a&code=b",
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("a parameter sent twice: invalid_request", r.json().get("error") == "invalid_request", r.text)
    r = c.post("/api/oauth/token", data={"grant_type": "client_credentials"})
    check("another grant type: unsupported_grant_type", r.json().get("error") == "unsupported_grant_type", r.text)


# ── 7. MCP calls ──────────────────────────────────────────────────────────────


def test_mcp_calls(c: TestClient):
    print("\n[7] /mcp: the protocol, and each token's own account")
    _, headers_a, account_a = sign_in(c)
    _, headers_b, account_b = sign_in(c)
    user_a = c.get("/api/me", headers=headers_a).json()["user_id"]
    db.save_wallet_address(user_a, 11155111, "0x00000000000000000000000000000000000000Aa")
    access_a = connect(c, headers_a)["access_token"]
    access_b = connect(c, headers_b, client_id=CHATGPT_ID, redirect_uri=CHATGPT_CB)["access_token"]

    r = mcp(c, access_a, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "1"}})
    result = r.json().get("result", {})
    check("initialize answers with the server's name and instructions",
          result.get("serverInfo", {}).get("name") == "mitfah" and "passkey" in result.get("instructions", ""), r.text[:300])
    tools = {t["name"]: t for t in mcp(c, access_a, "tools/list").json()["result"]["tools"]}
    check("get_account is listed, marked read-only",
          tools.get("get_account", {}).get("annotations", {}).get("readOnlyHint") is True, str(list(tools)))
    check("its description has no stray indentation", not tools["get_account"]["description"].startswith((" ", "\n")),
          repr(tools["get_account"]["description"][:40]))
    a = account_of(c, access_a)
    check("A's token reads A's account", a.get("owner_address") == account_a.address and a.get("connected_app") == "Claude", str(a))
    check("with A's wallet", a.get("wallets") == [{"network": "Sepolia", "chain_id": 11155111,
                                                    "address": "0x00000000000000000000000000000000000000Aa"}], str(a))
    b = account_of(c, access_b)
    check("B's token reads B's account, and no wallet", b.get("owner_address") == account_b.address
          and b.get("wallets") == [] and b.get("connected_app") == "ChatGPT", str(b))

    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    r = mcp(c, access_a, "tools/call", {"name": "get_account", "arguments": {}, "_meta": meta}, version="2026-07-28")
    text = r.json().get("result", {}).get("content", [{}])[0].get("text", "") if r.status_code == 200 else ""
    check("the 2026-07-28 per-request protocol works too, as A", account_a.address in text, r.text[:300])


# ── 7b. The wallet's read tools ───────────────────────────────────────────────

READS = {
    "get_account", "get_wallet_status", "get_supported_tokens", "get_all_contacts", "get_contact",
    "get_eth_balance", "get_erc20_balance", "get_price", "get_quote_in", "get_quote_out", "get_positions",
    "get_pool_quote", "get_lp_amounts", "get_liquidity_token_balance",
}
NOT_PER_NETWORK = {"get_account", "get_all_contacts", "get_contact"}


def call_tool(c: TestClient, access: str, name: str, arguments: dict) -> tuple[bool, object]:
    """A tool call over MCP: (whether it was an error, its JSON result or its text)."""
    import json

    r = mcp(c, access, "tools/call", {"name": name, "arguments": arguments})
    result = r.json().get("result", {}) if r.status_code == 200 else {}
    text = (result.get("content") or [{}])[0].get("text", r.text)
    if result.get("isError"):
        return True, text
    try:
        return False, json.loads(text)
    except ValueError:
        return False, text


@contextlib.contextmanager
def stand_in(tool, fake):
    """Runs `fake` in place of a wallet tool's own function (the chain is out of reach here)."""
    real = tool.func
    tool.func = fake
    try:
        yield
    finally:
        tool.func = real


def test_read_tools(c: TestClient):
    print("\n[7b] The wallet's read tools over /mcp")
    import threading

    import mcp_tools
    from langchain_core.tools import ToolException
    from limits import parse
    from network_config import network_name
    from tools.wallet_reads import get_eth_balance, get_price

    _, headers_a, _ = sign_in(c)
    _, headers_b, _ = sign_in(c)
    _, headers_n, _ = sign_in(c)
    user_a = c.get("/api/me", headers=headers_a).json()["user_id"]
    user_b = c.get("/api/me", headers=headers_b).json()["user_id"]
    db.save_wallet_address(user_a, 1, "0x00000000000000000000000000000000000000A1")
    db.save_wallet_address(user_a, 42161, "0x00000000000000000000000000000000000000A2")
    db.save_wallet_address(user_b, 11155111, "0x00000000000000000000000000000000000000B1")
    db.save_contact(user_a, "Sandy", "0x0000000000000000000000000000000000005a01")
    db.save_contact(user_b, "Bob", "0x00000000000000000000000000000000000000b0")
    access_a = connect(c, headers_a)["access_token"]
    access_b = connect(c, headers_b)["access_token"]
    access_n = connect(c, headers_n)["access_token"]

    tools = {t["name"]: t for t in mcp(c, access_a, "tools/list").json()["result"]["tools"]}
    check("tools/list has every read", READS <= set(tools), str(sorted(READS - set(tools))))
    check("every read is marked read-only", all(tools[n].get("annotations", {}).get("readOnlyHint") is True for n in READS))
    schemas = str([t["inputSchema"] for t in tools.values()])
    check("no schema says 'or null'", "anyOf" not in schemas and "'null'" not in schemas, schemas[:200])
    check("every per-network tool takes an optional `network`",
          all("network" in t["inputSchema"].get("properties", {}) and "network" not in t["inputSchema"].get("required", [])
              for n, t in tools.items() if n in READS and n not in NOT_PER_NETWORK))
    check("contacts and the account take none",
          all("network" not in tools[n]["inputSchema"].get("properties", {}) for n in NOT_PER_NETWORK))
    check("descriptions are short (under 700 characters each)", all(len(t["description"]) < 700 for t in tools.values()),
          str({n: len(t["description"]) for n, t in tools.items() if len(t["description"]) >= 700}))

    # Contacts: read from the database, the token's own account only.
    err, contacts = call_tool(c, access_a, "get_all_contacts", {})
    check("A's contacts are A's", not err and [x["name"] for x in contacts] == ["sandy"], str(contacts))
    err, address = call_tool(c, access_a, "get_contact", {"name": "sandy"})
    check("a contact by name, any case", not err and str(address).lower().endswith("5a01"), str(address))
    err, address = call_tool(c, access_b, "get_contact", {"name": "Sandy"})
    check("B can't see A's contact", "5a01" not in str(address).lower(), str(address))

    # The network: named, checked against the account's wallets, and set inside the worker thread.
    seen = {}

    def fake_balance(runtime):
        seen["user"] = runtime.context.user_id
        seen["network"] = db.get_user_network(runtime.context.user_id)
        seen["thread"] = threading.current_thread() is threading.main_thread()
        return {"balance": 1.5, "asset": "ETH"}

    with stand_in(get_eth_balance, fake_balance):
        err, result = call_tool(c, access_a, "get_eth_balance", {"network": "Arbitrum One"})
        check("a read answers on the named network", not err and result == {"balance": 1.5, "asset": "ETH"}
              and seen.get("network") == network_name(42161), f"{result} {seen}")
        check("as the token's account, off the event loop's thread", seen.get("user") == user_a and seen.get("thread") is False, str(seen))
        for name in ("42161", "arbitrum", "ARBITRUM ONE", network_name(42161)):
            seen.clear()
            call_tool(c, access_a, "get_eth_balance", {"network": name})
            check(f"network {name!r} is Arbitrum", seen.get("network") == network_name(42161), str(seen))
        seen.clear()
        call_tool(c, access_a, "get_eth_balance", {"network": "ethereum"})
        check("'ethereum' is mainnet", seen.get("network") == network_name(1), str(seen))
        seen.clear()
        err, text = call_tool(c, access_a, "get_eth_balance", {"network": "Base"})
        check("a network without a wallet is refused, naming the wallets'", err and "no wallet on Base" in text
              and "Ethereum" in text and "Arbitrum One" in text and not seen, text)
        err, text = call_tool(c, access_a, "get_eth_balance", {})
        check("left out with two wallets: asked which", err and "Say which network" in text and not seen, text)
        call_tool(c, access_b, "get_eth_balance", {})
        check("left out with one wallet: that one", seen.get("user") == user_b and seen.get("network") == network_name(11155111), str(seen))
        seen.clear()
        call_tool(c, access_a, "get_eth_balance", {"network": "arbitrum", "user_id": user_b})
        check("a user_id argument is ignored: the token decides", seen.get("user") == user_a, str(seen))
        err, text = call_tool(c, access_n, "get_eth_balance", {"network": "ethereum"})
        check("an account with no wallet is told to create one", err and "no smart wallet yet" in text, text)

    # Refusals reach the model in the tool's words, with keys masked; crashes don't reach it at all.
    key = "k3yk3yk3yk3yk3yk3yk3y"

    def refuses(runtime, token, amount=None):
        raise ToolException(f"Can't price {token}: https://eth-mainnet.g.alchemy.com/v2/{key} timed out")

    def crashes(runtime, token, amount=None):
        raise RuntimeError(f"stack trace with {key}")

    with stand_in(get_price, refuses):
        err, text = call_tool(c, access_a, "get_price", {"network": "ethereum", "token": "usdc"})
        check("a refusal is a tool error in the tool's words", err and "Can't price usdc" in text, text)
        check("with the key masked", key not in text, text)
    with stand_in(get_price, crashes):
        err, text = call_tool(c, access_a, "get_price", {"network": "ethereum", "token": "usdc"})
        check("a crash is a plain sentence, nothing of the exception", err and mcp_tools.UNEXPECTED in text
              and key not in text and "RuntimeError" not in text, text)
    got = {}

    def prices(runtime, token, amount=None):
        got.update(token=token, amount=amount)
        return 2500.0

    with stand_in(get_price, prices):
        err, result = call_tool(c, access_a, "get_price", {"network": "ethereum", "token": "eth"})
        check("an optional argument left out arrives as None", not err and result == 2500.0 and got == {"token": "eth", "amount": None}, f"{result} {got}")
        err, text = call_tool(c, access_a, "get_price", {"network": "ethereum", "token": "eth", "amount": "lots"})
        check("a wrong type is refused before the tool runs", err and got.get("amount") is None, str(text)[:200])

    # The rate limit, per connected app.
    saved = mcp_tools._rate
    mcp_tools._rate = parse("3/minute")
    try:
        access_x = connect(c, headers_a)["access_token"]
        access_y = connect(c, headers_a, client_id=CHATGPT_ID, redirect_uri=CHATGPT_CB)["access_token"]
        results = [call_tool(c, access_x, "get_all_contacts", {})[0] for _ in range(4)]
        check("an app's calls past its limit are refused", results == [False, False, False, True], str(results))
        err, text = call_tool(c, access_x, "get_account", {})
        check("get_account counts too", err and "too many times" in text, str(text)[:200])
        check("another app of the same account is not held back", not call_tool(c, access_y, "get_all_contacts", {})[0])
    finally:
        mcp_tools._rate = saved


# ── 7c. Quotes over MCP ───────────────────────────────────────────────────────

QUOTES = {
    "send_eth", "transfer_erc20", "wrap_eth", "swap", "open_position", "add_to_position",
    "withdraw_from_position", "collect_fees", "add_liquidity", "remove_liquidity",
}
FOLLOW_UPS = {"get_transaction_status", "cancel_transaction"}


def test_quotes(c: TestClient):
    print("\n[7c] Quotes over /mcp: a link to approve with the passkey, and what became of it")
    import mcp_tools
    import quotes
    import routes.chat
    import routes.history
    import signing
    import smart_wallet_agent
    from bundler import UserOpQuote
    from db.chains import CHAINS
    from db.sign_requests import SENT
    from db.transactions import add_transaction
    from langchain_core.tools import ToolException
    from limits import parse_many
    from tools.transfers import transfer_erc20, wrap_eth

    priced = UserOpQuote(
        calldata="0x1234", call_gas=100_000, call_gas_used=80_000, verification_gas=200_000,
        verification_gas_used=150_000, pre_verification_gas=60_000, max_fee=2 * 10**9, tip=10**9,
        base_fee=10**9, outer_gas=500_000, simulated_gas=None, chain_name="sepolia",
    )
    turns = []
    # The fake cards' turn: no real turn has it, so none of them is mistaken for a turn's own quote.
    elsewhere = -1

    def quotes_a_transfer(runtime, token, recipient, amount):
        """Stands in for transfer_erc20: raises a quote on the acting network, as the real one does."""
        user = runtime.context.user_id
        turns.append(runtime.context.turn_id)
        chain_id = CHAINS[db.get_user_network(user)]
        quotes.put(user, chain_id, runtime.context.turn_id, f"Transfer {amount:g} {token.upper()} to {recipient}",
                   [], priced, {"total_usd": 0.35}, charged_usd=5.0)
        return "QUOTE for the chat: show its next_step"

    _, headers, _ = sign_in(c)
    _, headers_b, _ = sign_in(c)
    user = c.get("/api/me", headers=headers).json()["user_id"]
    db.save_wallet_address(user, 11155111, "0x00000000000000000000000000000000000000C1")
    db.save_wallet_address(user, 1, "0x00000000000000000000000000000000000000C2")
    access = connect(c, headers)["access_token"]
    access_b = connect(c, headers_b)["access_token"]

    tools = {t["name"]: t for t in mcp(c, access, "tools/list").json()["result"]["tools"]}
    expected = READS | {"suggest_price_ranges"} | QUOTES | FOLLOW_UPS
    check("tools/list shows the reads, the quotes and their two follow-ups", set(tools) == expected,
          str(sorted(set(tools) ^ expected)))
    check("every quote tool is marked as not read-only, so apps ask first",
          all(tools[n]["annotations"]["readOnlyHint"] is False for n in QUOTES))
    check("each says nothing is sent", all("Nothing is sent" in tools[n]["description"] for n in QUOTES))
    check("get_transaction_status is read-only", tools["get_transaction_status"]["annotations"]["readOnlyHint"] is True)
    schemas = str([t["inputSchema"] for t in tools.values()])
    check("still no schema says 'or null'", "anyOf" not in schemas and "'null'" not in schemas)

    # A web chat card and a Telegram card wait on Sepolia, and a web card on Ethereum.
    web_card = signing.open_request(quotes.put(user, 11155111, elsewhere, "Wrap 1 ETH", [], priced, {"total_usd": 0.3}), signing.WEB)
    tg_card = signing.open_request(quotes.put(user, 11155111, elsewhere, "Wrap 2 ETH", [], priced, {"total_usd": 0.3}),
                                   signing.TELEGRAM, telegram_chat_id=4242)
    other_net = signing.open_request(quotes.put(user, 1, elsewhere, "Wrap 3 ETH", [], priced, {"total_usd": 0.3}), signing.WEB)
    told = []
    real_tell = mcp_tools.telegram_notify.tell
    mcp_tools.telegram_notify.tell = lambda request, text, landed=None: told.append((request["id"], text))
    try:
        with stand_in(transfer_erc20, quotes_a_transfer):
            err, q = call_tool(c, access, "transfer_erc20",
                               {"network": "sepolia", "token": "usdc", "recipient": "sandy", "amount": 5})
    finally:
        mcp_tools.telegram_notify.tell = real_tell
    check("a quote comes back waiting for approval", not err and q.get("status") == "waiting_for_approval", str(q))
    check("written from the quote: action, network, fees, limit, expiry",
          q.get("action") == "Transfer 5 USDC to sandy" and q.get("network") == "Sepolia" and q.get("fees") == "$0.35"
          and q.get("counts_toward_limit") == "$5.00" and q.get("expires_in_minutes") == 5, str(q))
    check("the tool's chat text doesn't reach the app", "QUOTE for the chat" not in str(q))
    check("it ran as a turn of its own", len(turns) == 1 and turns[0] > 0, str(turns))
    url = q.get("approve_url", "")
    check("with a link to the approval page", url.startswith(ISSUER + "/c/"), url)
    check("and a next step that says only the passkey sends it",
          "passkey" in q.get("next_step", "") and "can't send" in q.get("next_step", ""))
    token = url.rsplit("/", 1)[-1]
    request = signing.find_by_token(token)
    check("the link names the quote's sign request, made through an AI app",
          request["quote_id"] == q["quote_id"] and request["origin"] == signing.MCP and request["user_id"] == user)
    check("which the approval page is told came from an AI app",
          signing.summary(request, signing.thaw(request))["origin"] == "mcp")
    check("the web chat's card on Sepolia is cancelled (one card per network)",
          db.get_sign_request(web_card.request["id"])["status"] == "cancelled")
    check("so is the Telegram card, and its chat is told",
          db.get_sign_request(tg_card.request["id"])["status"] == "cancelled"
          # (telegram_notify.tell itself passes over the web card's request: it acts on Telegram's only)
          and (tg_card.request["id"], "Cancelled: a newer request from Claude replaced it. Nothing was sent.") in told, str(told))
    check("a card on another network is left alone", db.get_sign_request(other_net.request["id"])["status"] == "open")

    err, s = call_tool(c, access, "get_transaction_status", {"quote_id": q["quote_id"]})
    check("its status: waiting, and for how long", not err and s.get("status") == "waiting_for_approval"
          and 0 < s.get("expires_in_seconds", 0) <= 300, str(s))
    err, text = call_tool(c, access_b, "get_transaction_status", {"quote_id": q["quote_id"]})
    check("another account can't see it", err and mcp_tools.NO_QUOTE in text, str(text))
    err, text = call_tool(c, access, "get_transaction_status", {"quote_id": web_card.pending.quote_id})
    check("nor can an app see the web chat's quotes", err and mcp_tools.NO_QUOTE in text, str(text))

    # A second quote on the network replaces the first.
    with stand_in(transfer_erc20, quotes_a_transfer):
        _, q2 = call_tool(c, access, "transfer_erc20", {"network": "sepolia", "token": "dai", "recipient": "sandy", "amount": 7})
    _, s = call_tool(c, access, "get_transaction_status", {"quote_id": q["quote_id"]})
    check("a newer quote on the network cancels the older one", s.get("status") == "cancelled", str(s))
    check("whose link dies", _expired(signing, token))

    # Cancelling.
    err, s = call_tool(c, access, "cancel_transaction", {"quote_id": q2["quote_id"]})
    check("cancel_transaction cancels a waiting quote", not err and s.get("status") == "cancelled", str(s))
    check("its link dies", _expired(signing, q2["approve_url"].rsplit("/", 1)[-1]))
    err, s = call_tool(c, access, "cancel_transaction", {"quote_id": q2["quote_id"]})
    check("cancelling it again says there was nothing to cancel", not err and "nothing to cancel" in s.get("note", ""), str(s))

    # What became of it: expired, failed, sent, confirmed, failed on chain.
    def fresh_quote() -> tuple[dict, dict]:
        with stand_in(transfer_erc20, quotes_a_transfer):
            _, made = call_tool(c, access, "transfer_erc20", {"network": "sepolia", "token": "usdc", "recipient": "sandy", "amount": 1})
        return made, signing.find_by_token(made["approve_url"].rsplit("/", 1)[-1])

    made, req = fresh_quote()
    db.get_db().execute("UPDATE sign_requests SET expires_at = ? WHERE id = ?", (int(time.time()) - 1, req["id"]))
    db.get_db().commit()
    check("never approved in time: expired", call_tool(c, access, "get_transaction_status", {"quote_id": made["quote_id"]})[1].get("status") == "expired")

    made, req = fresh_quote()
    db.end_sign_request(req["id"], "failed", "It would fail on the network: not enough USDC.")
    _, s = call_tool(c, access, "get_transaction_status", {"quote_id": made["quote_id"]})
    check("refused when approved: failed, with the reason", s.get("status") == "failed" and "not enough USDC" in s.get("reason", ""), str(s))

    for row_status, shown in (("confirmed", "confirmed"), ("failed", "failed"), ("pending", "sent")):
        made, req = fresh_quote()
        tx_id = add_transaction(user, 11155111, "0x00000000000000000000000000000000000000C1", "assistant",
                                made["action"], row_status, tx_hash="0x" + "ab" * 32)
        db.end_sign_request(req["id"], SENT, "Sending — " + made["action"], tx_id)
        real_watched = routes.history.landing.is_watched
        routes.history.landing.is_watched = lambda _tx: True   # a pending row is being waited on: no chain read
        try:
            _, s = call_tool(c, access, "get_transaction_status", {"quote_id": made["quote_id"]})
        finally:
            routes.history.landing.is_watched = real_watched
        check(f"sent, and {row_status} in History: {shown}", s.get("status") == shown and s.get("tx_hash") == "0x" + "ab" * 32, str(s))

    # A tool that raised no quote, or refused.
    def answers_only(runtime, amount_eth):
        return "The wallet holds no ETH to wrap."

    def refuses(runtime, token, recipient, amount):
        raise ToolException(f"{recipient} isn't a saved contact. Add them on Mitfah's website first.")

    before = len(db.get_open_sign_requests(user, 11155111))
    with stand_in(wrap_eth, answers_only):
        err, text = call_tool(c, access, "wrap_eth", {"network": "sepolia", "amount_eth": 1})
    check("a write tool that raises no quote answers in its own words", not err and text == "The wallet holds no ETH to wrap.", str(text))
    with stand_in(transfer_erc20, refuses):
        err, text = call_tool(c, access, "transfer_erc20", {"network": "sepolia", "token": "usdc", "recipient": "eve", "amount": 1})
    check("a refusal is a tool error in the tool's words", err and "eve isn't a saved contact" in text, str(text))
    check("neither leaves a card", len(db.get_open_sign_requests(user, 11155111)) == before)

    # Busy: the account's one turn is the web chat's right now.
    with routes.chat._chatting_lock:
        routes.chat._chatting.add(user)
    try:
        with stand_in(transfer_erc20, quotes_a_transfer):
            err, text = call_tool(c, access, "transfer_erc20", {"network": "sepolia", "token": "usdc", "recipient": "sandy", "amount": 1})
        check("a quote while the web chat answers the account is refused, not queued", err and mcp_tools.BUSY in text, str(text))
        _, s = call_tool(c, access, "get_all_contacts", {})
        check("reads go on meanwhile", s == [], str(s))
    finally:
        with routes.chat._chatting_lock:
            routes.chat._chatting.discard(user)

    # The account's quote limits.
    saved = mcp_tools.QUOTE_LIMITS
    mcp_tools.QUOTE_LIMITS = parse_many("2/minute")
    try:
        _, headers_l, _ = sign_in(c)
        user_l = c.get("/api/me", headers=headers_l).json()["user_id"]
        db.save_wallet_address(user_l, 11155111, "0x00000000000000000000000000000000000000D1")
        access_l = connect(c, headers_l)["access_token"]
        with stand_in(transfer_erc20, quotes_a_transfer):
            errs = [call_tool(c, access_l, "transfer_erc20", {"token": "usdc", "recipient": "sandy", "amount": 1})[0] for _ in range(3)]
        check("quotes past the account's limit are refused", errs == [False, False, True], str(errs))
    finally:
        mcp_tools.QUOTE_LIMITS = saved

    # Approving or cancelling an app's quote never writes into the web chat's conversation.
    recorded = []
    real_record = smart_wallet_agent._record_button
    smart_wallet_agent._record_button = lambda *args, **kwargs: recorded.append(args)
    try:
        made, req = fresh_quote()
        r = c.post("/api/sign/cancel", json={"token": made["approve_url"].rsplit("/", 1)[-1]})
        check("the approval page's Don't send it cancels an app's quote", r.status_code == 200
              and db.get_sign_request(req["id"])["status"] == "cancelled", r.text)
        check("and writes nothing into the web chat", recorded == [], str(recorded))
        card = signing.open_request(quotes.put(user, 11155111, elsewhere, "Wrap 1 ETH", [], priced, {"total_usd": 0.3}), signing.WEB)
        smart_wallet_agent.cancel_signed(card.request)
        check("a web chat card's cancel still does", len(recorded) == 1, str(recorded))
    finally:
        smart_wallet_agent._record_button = real_record
    check("confirm_signed skips the conversation for an app's quote only",
          not smart_wallet_agent._in_conversation({"origin": "mcp"})
          and smart_wallet_agent._in_conversation({"origin": "web"}) and smart_wallet_agent._in_conversation({"origin": "telegram"}))


def _expired(signing, token: str) -> bool:
    """Whether a link's token no longer opens anything."""
    try:
        signing.find_by_token(token)
    except signing.Expired:
        return True
    return False


# ── 8. Refresh tokens ─────────────────────────────────────────────────────────


def test_refresh(c: TestClient):
    print("\n[8] Refresh: rotation, a lost reply retried, and a stolen copy")
    _, headers, _ = sign_in(c)

    first = connect(c, headers)
    second = refresh(c, first["refresh_token"]).json()
    check("a refresh token buys a new pair", "access_token" in second and works(c, second["access_token"]), str(second))
    retried = refresh(c, first["refresh_token"]).json()
    check("the same refresh token again within a minute, its successor unused: a retry, new pair",
          "access_token" in retried and works(c, retried["access_token"]), str(retried))
    check("the pair the lost reply carried is retired", not works(c, second["access_token"]))
    check("the retry's pair rotates on as usual", "access_token" in refresh(c, retried["refresh_token"]).json())

    first = connect(c, headers)
    second = refresh(c, first["refresh_token"]).json()
    third = refresh(c, second["refresh_token"]).json()
    r = refresh(c, first["refresh_token"])
    check("an old refresh token whose successor was already used: refused", r.json().get("error") == "invalid_grant", r.text)
    check("and the connection is ended", not works(c, third["access_token"]))

    first = connect(c, headers)
    second = refresh(c, first["refresh_token"]).json()
    db.get_db().execute("UPDATE mcp_tokens SET used_at = ? WHERE token_hash = ?",
                        (int(time.time()) - 120, mcp_auth.hash_token(first["refresh_token"])))
    db.get_db().commit()
    r = refresh(c, first["refresh_token"])
    check("a used refresh token two minutes later: refused, connection ended",
          r.json().get("error") == "invalid_grant" and not works(c, second["access_token"]), r.text)

    first = connect(c, headers)
    lost = refresh(c, first["refresh_token"]).json()
    retried = refresh(c, first["refresh_token"]).json()
    r = refresh(c, lost["refresh_token"])
    check("the retired refresh token from the lost reply shows up: refused, connection ended",
          r.json().get("error") == "invalid_grant" and not works(c, retried["access_token"]), r.text)

    first = connect(c, headers)
    db.get_db().execute("UPDATE mcp_tokens SET expires_at = ? WHERE token_hash = ?",
                        (int(time.time()) - 1, mcp_auth.hash_token(first["refresh_token"])))
    db.get_db().commit()
    r = refresh(c, first["refresh_token"])
    check("an expired refresh token: invalid_grant, the connection left alone",
          r.json().get("error") == "invalid_grant" and works(c, first["access_token"]), r.text)
    first = connect(c, headers)
    r = refresh(c, first["refresh_token"], client_id=CHATGPT_ID)
    check("another client's refresh token: invalid_grant", r.json().get("error") == "invalid_grant", r.text)
    r = c.post("/api/oauth/token", data={"grant_type": "refresh_token"})
    check("no refresh token: invalid_request", r.json().get("error") == "invalid_request", r.text)


# ── 9. Revocation ─────────────────────────────────────────────────────────────


def test_revoke(c: TestClient):
    print("\n[9] Revocation (RFC 7009)")
    _, headers, _ = sign_in(c)
    tokens = connect(c, headers)
    r = c.post("/api/oauth/revoke", data={"token": tokens["access_token"]})
    check("revoking an access token: 200, and it stops working", r.status_code == 200 and not works(c, tokens["access_token"]))
    renewed = refresh(c, tokens["refresh_token"]).json()
    check("but the connection lives on", works(c, renewed["access_token"]))
    c.post("/api/oauth/revoke", data={"token": renewed["refresh_token"]})
    check("revoking a refresh token ends the connection", not works(c, renewed["access_token"]))
    check("an unknown token: still 200", c.post("/api/oauth/revoke", data={"token": "mfr_nope"}).status_code == 200)


# ── 10. Connected apps ────────────────────────────────────────────────────────


def test_connections(c: TestClient):
    print("\n[10] Connected apps in Controls")
    _, headers_a, _ = sign_in(c)
    _, headers_b, _ = sign_in(c)
    claude = connect(c, headers_a)
    connect(c, headers_a, client_id=CHATGPT_ID, redirect_uri=CHATGPT_CB)
    works(c, claude["access_token"])
    listed = c.get("/api/connections", headers=headers_a).json()["connections"]
    check("both apps are listed, newest first", [x["app"]["name"] for x in listed] == ["ChatGPT", "Claude"], str(listed))
    check("Claude shows when it was last used", listed[1]["last_used_at"] is not None, str(listed[1]))
    check("another account sees none of them", c.get("/api/connections", headers=headers_b).json()["connections"] == [])
    claude_id = listed[1]["id"]
    check("another account can't disconnect them: 404",
          c.delete(f"/api/connections/{claude_id}", headers=headers_b).status_code == 404)
    check("and trying doesn't touch their tokens", works(c, claude["access_token"]))
    check("the owner disconnects: 204", c.delete(f"/api/connections/{claude_id}", headers=headers_a).status_code == 204)
    check("its token stops working at once", not works(c, claude["access_token"]))
    check("and it leaves the list", [x["app"]["name"] for x in c.get("/api/connections", headers=headers_a).json()["connections"]]
          == ["ChatGPT"])
    check("disconnecting it again: 404", c.delete(f"/api/connections/{claude_id}", headers=headers_a).status_code == 404)
    check("signed out: 401", c.get("/api/connections").status_code == 401)


# ── 11. Signing in with a passkey ─────────────────────────────────────────────


def _register(c: TestClient, headers: dict, phone: SoftPasskey):
    options = c.post("/api/passkeys/register/options", headers=headers).json()["options"]
    r = c.post("/api/passkeys/register", headers=headers,
               json={"credential": phone.registration_response(base64url_to_bytes(options["challenge"])),
                     "device_name": "Test phone"})
    assert r.status_code == 200, r.text


def _challenge(c: TestClient) -> bytes:
    return base64url_to_bytes(c.post("/api/auth/passkey/options").json()["options"]["challenge"])


def test_passkey_signin(c: TestClient):
    print("\n[11] Signing in with a passkey")
    _, headers, account = sign_in(c)
    user_id = c.get("/api/me", headers=headers).json()["user_id"]
    phone = SoftPasskey()
    _register(c, headers, phone)
    handle = db.get_webauthn_handle(user_id)
    approved = {"answer": True}
    saved = set_everywhere(api, "approved_on_a_wallet", lambda _user, _key: approved["answer"])

    def login(assertion):
        # A browser with no session (and no lifespan of its own).
        return TestClient(api.app).post("/api/auth/passkey/login", json={"credential": assertion})

    try:
        options = c.post("/api/auth/passkey/options").json()["options"]
        check("the options name no account: the device offers its Mitfah passkeys",
              not options.get("allowCredentials") and options["userVerification"] == "required"
              and options["rpId"] == "localhost", str(options))
        challenge = base64url_to_bytes(options["challenge"])
        r = login(phone.assertion(challenge, user_handle=handle))
        check("an approved passkey signs in", r.status_code == 200 and "access_token" in r.json(), r.text)
        check("with the same refresh cookie as a wallet sign-in", "refresh_token=" in r.headers.get("set-cookie", ""))
        me = c.get("/api/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
        check("as the passkey's own account", me.get("owner_addr") == account.address, str(me))
        r = login(phone.assertion(challenge, user_handle=handle))
        check("a challenge is used once", r.status_code == 400, r.text)
        r = login(phone.assertion(_challenge(c)))
        check("a passkey that sends no user handle still signs in", r.status_code == 200, r.text)
        r = login(phone.assertion(_challenge(c), user_handle=secrets.token_bytes(16)))
        check("another account's user handle: 401", r.status_code == 401, r.text)
        r = login(phone.assertion(_challenge(c), user_handle=handle, user_verified=False))
        check("no Face ID / PIN (user not verified): 401", r.status_code == 401, r.text)
        r = login(phone.assertion(_challenge(c), user_handle=handle, origin="https://evil.example"))
        check("asked from another site: 401", r.status_code == 401, r.text)
        r = login(SoftPasskey().assertion(_challenge(c)))
        check("a passkey Mitfah never registered: 401", r.status_code == 401, r.text)
        approved["answer"] = False
        r = login(phone.assertion(_challenge(c), user_handle=handle))
        check("registered but approved on no wallet: 403", r.status_code == 403, r.text)
        approved["answer"] = None
        r = login(phone.assertion(_challenge(c), user_handle=handle))
        check("the wallets couldn't be read: 503", r.status_code == 503, r.text)
        r = c.post("/api/auth/passkey/login", json={"credential": {"id": "x"}})
        check("an answer that can't be read: 400", r.status_code == 400, r.text)
    finally:
        set_everywhere(api, "approved_on_a_wallet", saved)


def test_approved_on_a_wallet():
    print("\n[12] Approved on a wallet: one unreadable chain doesn't decide")
    saved = routes.passkeys._approves, routes.passkeys.get_wallet_chains
    answers: dict = {}

    def fake(_user, _key, chain_id):
        if isinstance(answers[chain_id], Exception):
            raise answers[chain_id]
        return answers[chain_id]

    try:
        routes.passkeys._approves = fake
        routes.passkeys.get_wallet_chains = lambda _user: sorted(answers)
        for label, given, expected in [
            ("one chain approves while another can't be read: yes", {1: RuntimeError("rpc down"), 56: True}, True),
            ("every chain read, none approves: no", {1: False, 56: False}, False),
            ("none approves and one can't be read: can't tell", {1: RuntimeError("rpc down"), 56: False}, None),
            ("no wallets at all: no", {}, False),
        ]:
            answers.clear()
            answers.update(given)
            got = routes.passkeys.approved_on_a_wallet(1, "0xkey")
            check(label, got is expected, str(got))
    finally:
        routes.passkeys._approves, routes.passkeys.get_wallet_chains = saved


if __name__ == "__main__":
    test_known_list()
    test_approved_on_a_wallet()
    with make_client() as client:
        test_discovery(client)
        test_mcp_needs_a_token(client)
        test_authorize(client)
        test_allow_page(client)
        test_token_endpoint(client)
        test_mcp_calls(client)
        test_read_tools(client)
        test_quotes(client)
        test_refresh(client)
        test_revoke(client)
        test_connections(client)
        test_passkey_signin(client)
    finish("All MCP sign-in, /mcp and passkey sign-in checks passed.")
