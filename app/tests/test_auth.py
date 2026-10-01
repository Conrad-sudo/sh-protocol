"""
End-to-end checks on the API's authentication, run against a throwaway database.

The property under test is the one the whole identity refactor exists to establish: a request acts
on the account its TOKEN names, and on no other. Everything here is offline -- no RPC, no chain, no
Vault -- so it is safe to run anywhere.

Run: make auth-test   (or: python app/tests/test_auth.py)
"""
import os
import tempfile
import time
from urllib.parse import quote

from checks import add_contact, check, finish, sign_in   # first: it puts app/ on sys.path for the imports below

# A scratch database, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"          # the test client speaks http
os.environ["TELEGRAM_BOT_USERNAME"] = "test_wallet_bot"
os.environ["SIWE_DOMAIN"] = "localhost:3000"   # explicit, so a SIWE_DOMAIN in .env cannot move it

import db                                   # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

from fastapi.testclient import TestClient   # noqa: E402
from eth_account import Account             # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402
from web3 import Web3                       # noqa: E402

import api                                  # noqa: E402
import auth                                 # noqa: E402
from constants import get_router            # noqa: E402

# A checksummed address, used by the contacts tests to prove the API normalises what it stores.
ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


# The agent's checkpointer is not needed for auth, and opening it would touch the real DB path;
# the lifespan is stubbed out so TestClient can start the app without it.
api.app.router.lifespan_context = None


def make_client(rate_limit: bool = False) -> TestClient:
    """
    A test client with the agent lifespan disabled.

    Rate limiting is off by default: these tests sign in many times in a few seconds, which the
    real 10/minute sign-in limit would (correctly) block. test_rate_limit turns it back on to check
    the limit itself still bites.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop(_app):
        yield

    api.app.router.lifespan_context = _noop
    api.limiter.enabled = rate_limit
    api.limiter.reset()
    return TestClient(api.app)


def test_siwe_sign_in_and_refresh():
    print("\n[1] SIWE sign-in -> refresh -> protected endpoint; an address is one account")
    c = make_client()
    acct = Account.create()

    nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    check("the nonce is alphanumeric, as EIP-4361 requires", nonce.isalnum() and len(nonce) >= 8, nonce)
    r = siwe_login(c, acct, auth.build_siwe_message("localhost:3000", acct.address, nonce, 11155111), nonce)
    check("a first sign-in answers 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    body = r.json()
    check("it returns an access token", "access_token" in body)
    check("the refresh token is NOT in the body", "refresh_token" not in body)
    check("the refresh cookie is set", api.REFRESH_COOKIE in r.cookies, str(dict(r.cookies)))

    r = c.get("/api/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    check("the token reaches a protected endpoint", r.status_code == 200, r.text[:120])
    me = r.json()
    check("it resolves to the right account", me["user_id"] == body["user_id"], str(me))
    check("the account is the address that signed", me["owner_addr"] == acct.address, str(me))
    check("nothing about email, passwords or Google is left", not {"email", "has_password", "google_linked"} & set(me),
          str(me))

    again, _, _ = sign_in(c, acct)
    check("signing in again reaches the same account", again["user_id"] == body["user_id"], str(again))
    other, _, _ = sign_in(c)
    check("another address is another account", other["user_id"] != body["user_id"], str(other))

    r = c.post("/api/auth/refresh")
    check("the cookie alone refreshes", r.status_code == 200, r.text[:160])
    check("refresh returns a new access token", "access_token" in r.json())

    for path in ("/api/auth/signup", "/api/auth/login", "/api/auth/google", "/api/auth/google/link",
                 "/api/auth/siwe/verify"):
        code = c.post(path, json={}).status_code
        check(f"{path} is gone", code == 404, str(code))


def test_bad_tokens_rejected():
    print("\n[2] forged, expired and mistyped tokens are refused")
    c = make_client()
    sign_in(c)

    check("no token -> 401", c.get("/api/me").status_code == 401)
    check(
        "garbage token -> 401",
        c.get("/api/me", headers={"Authorization": "Bearer not.a.token"}).status_code == 401,
    )

    # Signed with the wrong key: right shape, wrong signature.
    import jwt
    forged = jwt.encode(
        {"sub": "1", "typ": "access", "exp": int(time.time()) + 600}, "wrong-key", algorithm="HS256"
    )
    check(
        "a token signed with the wrong key -> 401",
        c.get("/api/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401,
    )

    expired = jwt.encode(
        {"sub": "1", "typ": "access", "iat": int(time.time()) - 100, "exp": int(time.time()) - 10},
        auth.JWT_SECRET,
        algorithm="HS256",
    )
    check(
        "an expired token -> 401",
        c.get("/api/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401,
    )

    # A refresh token presented as an access token: caught by the typ claim.
    wrong_typ = jwt.encode(
        {"sub": "1", "typ": "refresh", "exp": int(time.time()) + 600},
        auth.JWT_SECRET,
        algorithm="HS256",
    )
    check(
        "a non-access token -> 401",
        c.get("/api/me", headers={"Authorization": f"Bearer {wrong_typ}"}).status_code == 401,
    )


def test_refresh_reuse_revokes_everything():
    print("\n[3] reusing a rotated refresh token signs every session out")
    c = make_client()
    sign_in(c)
    stolen = c.cookies[api.REFRESH_COOKIE]

    r = c.post("/api/auth/refresh")
    check("the first refresh works", r.status_code == 200, r.text[:120])

    # Replay the pre-rotation token, as a thief holding a copy would.
    c.cookies.clear()
    c.cookies.set(api.REFRESH_COOKIE, stolen, path=api.REFRESH_COOKIE_PATH)
    r = c.post("/api/auth/refresh")
    check("replaying the old token -> 401", r.status_code == 401, str(r.status_code))

    # And the legitimate session is gone too: reuse means the token leaked, so everything dies.
    r2 = c.post("/api/auth/refresh")
    check("every session for that account is revoked", r2.status_code == 401, str(r2.status_code))

    # Signing out must revoke the token on the server, not just delete the browser's copy. The
    # client's cookie jar applies path matching like a browser does, so this also proves the cookie
    # actually reaches /api/auth/logout -- with the old /api/auth/refresh path it never did.
    c = make_client()
    sign_in(c)
    before_logout = c.cookies[api.REFRESH_COOKIE]
    r = c.post("/api/auth/logout")
    check("logout answers 200", r.status_code == 200, str(r.status_code))
    c.cookies.clear()
    c.cookies.set(api.REFRESH_COOKIE, before_logout, path=api.REFRESH_COOKIE_PATH)
    r = c.post("/api/auth/refresh")
    check("a token presented after logout is refused", r.status_code == 401, f"{r.status_code} {r.text[:80]}")


def test_identity_comes_from_token_not_body():
    print("\n[4] a request acts on the account its TOKEN names")
    c = make_client()
    alice, alice_headers, alice_acct = sign_in(c)
    bob, _, _ = sign_in(c)
    check("two distinct accounts", alice["user_id"] != bob["user_id"])

    # Alice's token, Bob's id in the body. The body must be ignored -- and in fact there is no
    # user_id field left to send, so this also proves the schema rejects the old shape.
    r = c.get("/api/me", headers=alice_headers)
    check("Alice's token resolves to Alice", r.json()["user_id"] == alice["user_id"])

    r = c.post(
        "/api/deploy",
        headers=alice_headers,
        json={
            "user_id": bob["user_id"],          # ignored: not a field on DeployRequest
            "chain_id": 31337,
            "deployer": "0x0000000000000000000000000000000000000001",
            "daily_limit_usd": 100,
        },
    )
    # 403: the body cannot name an account, and it cannot name an EOA other than the one the
    # caller signed in as.
    check("deploy refuses a deployer that isn't the signed-in address", r.status_code == 403,
          f"{r.status_code} {r.text[:160]}")
    check("the refusal names the address Alice signs in as", alice_acct.address in r.text, r.text[:160])


def siwe_login(c: TestClient, acct, message: str, nonce: str):
    """Signs `message` with `acct` and posts it to the SIWE sign-in endpoint."""
    signature = Account.sign_message(encode_defunct(text=message), acct.key).signature.hex()
    return c.post("/api/auth/siwe/login", json={"message": message, "signature": signature, "nonce": nonce})


def headers_for_account_without_address() -> dict:
    """
    Auth headers for an account with no wallet address, like the ones migrated from Telegram-only
    days. The web app cannot sign one in any more; a token is minted directly to test the guards.
    """
    return {"Authorization": f"Bearer {auth.create_access_token(db.create_user())}"}


def test_siwe_checks_and_deployer_check():
    print("\n[5] SIWE sign-in refuses every forged or replayed message, and only that address may deploy")
    c = make_client()
    acct = Account.create()
    nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    message = auth.build_siwe_message("localhost:3000", acct.address, nonce, 11155111)

    r = siwe_login(c, acct, message, nonce)
    check("a valid signature signs in", r.status_code == 200, r.text[:160])
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # The nonce is single-use.
    r = siwe_login(c, acct, message, nonce)
    check("replaying the same nonce is refused", r.status_code == 400, str(r.status_code))

    # A signature over text that is not a SIWE message at all.
    fresh = c.get("/api/auth/siwe/nonce").json()["nonce"]
    r = siwe_login(c, acct, f"unrelated message {fresh}", fresh)
    check("a non-SIWE message is refused, even carrying the nonce", r.status_code == 400, str(r.status_code))

    # A well-formed message whose Nonce field is a different, also-issued nonce.
    other_nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    r = siwe_login(c, acct, auth.build_siwe_message("localhost:3000", acct.address, other_nonce, 1), fresh)
    check("a message carrying a different nonce is refused", r.status_code == 400, str(r.status_code))

    # The phishing case the domain check exists for: another site fetched a nonce from us and had the
    # victim sign a message naming ITSELF (so the victim's wallet showed no mismatch warning), and
    # now replays it here to sign in as the victim.
    victim = Account.create()
    nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    phished = auth.build_siwe_message("evil.example", victim.address, nonce, 1)
    r = siwe_login(c, victim, phished, nonce)
    check("a message written for another site is refused", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check("the refusal names the other site", "evil.example" in r.text, r.text[:160])
    check("and no account was made for the victim", db.get_user_by_owner_addr(victim.address) is None)

    # A message that names one address but is signed by another describes someone else.
    nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    r = siwe_login(c, acct, auth.build_siwe_message("localhost:3000", victim.address, nonce, 1), nonce)
    check("a signer different from the named address is refused", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check("and it signed nobody in as the named address", db.get_user_by_owner_addr(victim.address) is None)

    # Expired.
    from datetime import datetime, timedelta, timezone
    nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    stale = auth.build_siwe_message(
        "localhost:3000", victim.address, nonce, 1,
        expiration_time=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    r = siwe_login(c, victim, stale, nonce)
    check("an expired message is refused", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    # The layouts viem's createSiweMessage actually produces must all be accepted: with a statement,
    # with a scheme on the domain, and with a live expiry.
    for label, build in [
        ("a message with a statement",
         lambda a, n: auth.build_siwe_message("localhost:3000", a, n, 1, statement="Sign in to Mitfah.")),
        ("a message with an https:// scheme",
         lambda a, n: auth.build_siwe_message("localhost:3000", a, n, 1).replace(
             "localhost:3000 wants", "https://localhost:3000 wants", 1)),
        ("a message with a future expiry",
         lambda a, n: auth.build_siwe_message(
             "localhost:3000", a, n, 1, expiration_time=datetime.now(timezone.utc) + timedelta(minutes=5))),
    ]:
        signer = Account.create()
        nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
        r = siwe_login(c, signer, build(signer.address, nonce), nonce)
        check(f"{label} is accepted", r.status_code == 200, f"{r.status_code} {r.text[:160]}")

    # An unredeemed nonce does not linger: issuing one clears those past their lifetime.
    db.get_db().execute("INSERT INTO siwe_nonces (nonce, issued_at) VALUES ('abandoned0', ?)",
                        (int(time.time()) - auth.SIWE_NONCE_TTL_SECS - 1,))
    db.get_db().commit()
    c.get("/api/auth/siwe/nonce")
    check("expired nonces are cleared",
          db.get_db().execute("SELECT 1 FROM siwe_nonces WHERE nonce = 'abandoned0'").fetchone() is None)

    # Deploying from an address this account does not sign in as.
    r = c.post(
        "/api/deploy",
        headers=headers,
        json={
            "chain_id": 31337,
            "deployer": "0x000000000000000000000000000000000000dEaD",
            "daily_limit_usd": 100,
        },
    )
    check("deploying from someone else's EOA -> 403", r.status_code == 403, f"{r.status_code} {r.text[:160]}")


def test_telegram_link_nonce():
    print("\n[6] Telegram links are minted server-side and are single-use")
    c = make_client()
    signed_in, headers, _ = sign_in(c)

    r = c.post("/api/integrations/telegram/link", headers=headers)
    check("a link is minted", r.status_code == 200, r.text[:160])
    check("it points at the bot", r.json()["url"].startswith("https://t.me/test_wallet_bot?start="))
    check("linking needs a token", c.post("/api/integrations/telegram/link").status_code == 401)

    nonce = r.json()["nonce"]
    check("the nonce redeems to its account",
          db.consume_telegram_link_nonce(nonce) == signed_in["user_id"])
    check("and only once", db.consume_telegram_link_nonce(nonce) is None)

    # An unknown nonce cannot bind anything.
    check("an unknown nonce redeems to nothing", db.consume_telegram_link_nonce("made-up") is None)


def test_bot_start_explains_a_chat_linked_elsewhere():
    """
    A chat already linked to one account that follows another account's link is told why nothing
    happened; otherwise that account's settings page would wait out the link's expiry in silence.
    """
    print("\n[6b] /start with another account's link says the chat is linked elsewhere")
    import asyncio
    from types import SimpleNamespace

    import telebot

    c = make_client()
    first, _, _ = sign_in(c)
    second, _, _ = sign_in(c)
    chat_id = 424242
    db.link_telegram(first["user_id"], chat_id)

    def start(user):
        link = c.post(
            "/api/integrations/telegram/link",
            headers={"Authorization": f"Bearer {user['access_token']}"},
        ).json()
        replies = []

        async def reply_text(text):
            replies.append(text)

        update = SimpleNamespace(message=SimpleNamespace(chat_id=chat_id, reply_text=reply_text))
        job_queue = SimpleNamespace(get_jobs_by_name=lambda name: [], run_repeating=lambda *a, **k: None)
        context = SimpleNamespace(args=[link["nonce"]], job_queue=job_queue)
        asyncio.run(telebot.start(update, context))
        return link["nonce"], replies

    nonce, replies = start(second)
    check("the chat is told it belongs to another account",
          any("already linked to a different account" in r for r in replies), str(replies))
    check("the chat stays with its account", db.get_user_by_id(second["user_id"])["telegram_chat_id"] is None)
    check("the link is burned", db.consume_telegram_link_nonce(nonce) is None)

    _, replies = start(first)
    check("its own account's link gets no such warning",
          not any("different account" in r for r in replies), str(replies))


def test_owner_actions_are_guarded():
    print("\n[7] owner actions need a token AND an account with an owner address")
    c = make_client()
    # Every account the web app signs in has an address; one from Telegram-only days does not.
    headers = headers_for_account_without_address()

    bodies = {
        "/api/wallet/pause/prepare": {"chain_id": 31337},
        "/api/wallet/unpause/prepare": {"chain_id": 31337},
        "/api/wallet/withdraw/prepare": {"chain_id": 31337, "token": "eth", "amount": "1", "to": "0x000000000000000000000000000000000000dEaD"},
        "/api/wallet/watched-tokens/prepare": {"chain_id": 31337, "token": "usdc", "action": "add"},
        "/api/wallet/daily-limit/prepare": {"chain_id": 31337, "daily_limit_usd": 500},
        "/api/wallet/window-duration/prepare": {"chain_id": 31337, "window_secs": 86400},
        "/api/wallet/session/prepare": {"chain_id": 31337, "action": "remove"},
        "/api/wallet/trusted-spenders/prepare": {"chain_id": 31337, "spender": "0x000000000000000000000000000000000000dEaD", "action": "add"},
        "/api/wallet/max-op-gas-cost/prepare": {"chain_id": 31337, "max_cost_eth": "0.05"},
        "/api/wallet/tx/confirm": {"chain_id": 31337, "tx_hash": "0x" + "11" * 32},
    }

    for path, body in bodies.items():
        check(f"{path} without a token -> 401", c.post(path, json=body).status_code == 401)

    for path, body in bodies.items():
        r = c.post(path, headers=headers, json=body)
        # 403, not 404/503/500: the owner check runs before any chain or RPC work, so an account
        # with no address never reaches the node.
        check(f"{path} without an owner address -> 403", r.status_code == 403, f"{r.status_code} {r.text[:100]}")

    # An unknown action is rejected by the schema, not by a silent fall-through to "remove".
    r = c.post(
        "/api/wallet/watched-tokens/prepare",
        headers=headers,
        json={"chain_id": 31337, "token": "usdc", "action": "destroy"},
    )
    check("an invalid watched-token action -> 422", r.status_code == 422, str(r.status_code))

    # A non-positive withdraw is rejected by the schema too.
    r = c.post(
        "/api/wallet/withdraw/prepare",
        headers=headers,
        json={"chain_id": 31337, "token": "eth", "amount": "0", "to": "0x000000000000000000000000000000000000dEaD"},
    )
    check("a zero-amount withdraw -> 422", r.status_code == 422, str(r.status_code))

    # Bounds that mirror the contract's own reverts, so the UI fails fast instead of at eth_call.
    bad = {
        "a negative daily limit": ("/api/wallet/daily-limit/prepare", {"chain_id": 31337, "daily_limit_usd": -1}),
        "a zero window": ("/api/wallet/window-duration/prepare", {"chain_id": 31337, "window_secs": 0}),
        "a window past the uint48 ceiling": ("/api/wallet/window-duration/prepare", {"chain_id": 31337, "window_secs": 281_474_976_710_656}),
        "a zero gas ceiling": ("/api/wallet/max-op-gas-cost/prepare", {"chain_id": 31337, "max_cost_eth": "0"}),
        "an invalid session action": ("/api/wallet/session/prepare", {"chain_id": 31337, "action": "steal"}),
    }
    for label, (path, body) in bad.items():
        r = c.post(path, headers=headers, json=body)
        check(f"{label} -> 422", r.status_code == 422, f"{r.status_code} {r.text[:90]}")

    # A daily limit of 0 IS valid -- it freezes agent spending without revoking the key. It must
    # reach the owner check (403 here) rather than being rejected by the schema.
    r = c.post("/api/wallet/daily-limit/prepare", headers=headers, json={"chain_id": 31337, "daily_limit_usd": 0})
    check("a zero daily limit is accepted by the schema", r.status_code == 403, str(r.status_code))


def test_router_removal_is_refused():
    print("\n[7b] the exchange router can't be removed through the app")
    c = make_client()
    _, headers, _ = sign_in(c)

    router = get_router(11155111)
    # Lower case on purpose: the comparison must not depend on how the address is written.
    r = c.post(
        "/api/wallet/trusted-spenders/prepare",
        headers=headers,
        json={"chain_id": 11155111, "spender": router.lower(), "action": "remove"},
    )
    check("removing the router -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check("the refusal says why", "remove liquidity" in r.json().get("detail", ""), r.text[:160])


def test_wrapped_native_always_counts():
    print("\n[7c] WETH/WBNB always count: flagged in the token list, never removable through the app")
    db.get_db().execute(
        "INSERT OR REPLACE INTO supported_tokens (chain_id, ticker, address) VALUES (?, ?, ?), (?, ?, ?)",
        (11155111, "weth", "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
         11155111, "usdc", "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"),
    )
    db.get_db().commit()
    c = make_client()
    tokens = {t["ticker"]: t["always_counted"] for t in c.get("/api/tokens?chain_id=11155111").json()["tokens"]}
    check("WETH is flagged as always counted", tokens.get("weth") is True, str(tokens))
    check("other tokens are not", tokens.get("usdc") is False, str(tokens))

    _, headers, _ = sign_in(c)
    # Upper case on purpose: the ticker comparison must not depend on how it is written.
    r = c.post(
        "/api/wallet/watched-tokens/prepare",
        headers=headers,
        json={"chain_id": 11155111, "token": "WETH", "action": "remove"},
    )
    check("removing WETH -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check("the refusal says why", "always counts" in r.json().get("detail", ""), r.text[:160])


def test_wallet_state_read_is_guarded():
    """
    GET /api/wallet/{chain_id} needs a token, and only ever reads the CALLER's wallet.

    The on-chain assertions live in test_e2e_fork; what matters offline is that the route cannot be
    reached anonymously and does not fall through to another account's row. A signed-in account with
    no wallet must get 404 -- never somebody else's.
    """
    print("\n[8] wallet state reads are authenticated and per-account")
    c = make_client()
    _, headers, _ = sign_in(c)

    check("reading needs a token", c.get("/api/wallet/31337").status_code == 401)
    check("a forged token is refused",
          c.get("/api/wallet/31337", headers={"Authorization": "Bearer nope"}).status_code == 401)

    # No wallet on this chain for this account. 404, and notably not a 500 from calling a bare
    # address or a 200 carrying someone else's.
    #
    # This assertion caught a real ordering bug: _resolve_chain ran first, so the answer was a 400
    # about chain configuration and an RPC had been dialled on behalf of a caller with nothing on
    # that chain. The wallet lookup now runs first -- cheap, authoritative, and no RPC.
    r = c.get("/api/wallet/31337", headers=headers)
    check("no wallet on that chain -> 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

    # Same for a chain this deployment does not serve: the caller has no wallet there either, and
    # that is the answer they can act on.
    r = c.get("/api/wallet/999999", headers=headers)
    check("an unknown chain -> 404 with no RPC attempted", r.status_code == 404,
          f"{r.status_code} {r.text[:120]}")


def test_contacts_are_web_only_and_per_account():
    """
    Adding a payee takes the owner wallet's signature, and reaches only your own list.

    The agent has no save_contact tool (guarded in test_identity.py); this is the other half --
    the endpoint that replaced it must require a signed-in account AND the owner's EIP-712
    signature over the exact contact, and must not let one account read, write or delete another's
    contacts. Contacts are the allowlist of destinations for the wallet's funds, so a write here
    without the owner's say-so would be a way to add a payee to somebody's wallet.
    """
    print("\n[9] contacts need the owner's signature and stay within one account")
    c = make_client()
    _, a_headers, alice = sign_in(c)
    _, b_headers, bob = sign_in(c)
    contact = {"name": "mallory", "address": ADDR}

    # Unauthenticated, on every verb.
    check("preparing needs a token", c.post("/api/contacts/prepare", json=contact).status_code == 401)
    check("adding needs a token",
          c.post("/api/contacts", json={**contact, "nonce": "abc", "signature": "0x00"}).status_code == 401)
    check("listing needs a token", c.get("/api/contacts").status_code == 401)
    check("deleting needs a token", c.delete("/api/contacts/mallory").status_code == 401)

    # What the wallet is asked to sign: the contact exactly as it will be stored.
    r = c.post("/api/contacts/prepare", headers=a_headers, json={"name": " Sandy ", "address": ADDR.lower()})
    check("preparing answers the typed data", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    typed = r.json()
    check("it is an AddContact for Mitfah",
          typed["primaryType"] == "AddContact" and typed["domain"] == {"name": "Mitfah", "version": "1"}, str(typed))
    check("it carries the name and address as they will be stored",
          typed["message"]["name"] == "sandy" and typed["message"]["address"] == ADDR, str(typed["message"]))
    check("preparing saves nothing", c.get("/api/contacts", headers=a_headers).json()["contacts"] == [])

    # A session alone is not enough: no signature, or anybody's but the owner's, saves nothing.
    check("a save without a signature -> 422",
          c.post("/api/contacts", headers=a_headers, json=typed["message"]).status_code == 422)
    signed_by_bob = Account.sign_typed_data(bob.key, typed["domain"], typed["types"], typed["message"])
    r = c.post("/api/contacts", headers=a_headers, json={**typed["message"], "signature": signed_by_bob.signature.hex()})
    check("a signature from another wallet -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check("the refusal names the owner", alice.address in r.text, r.text[:160])
    check("and nothing was saved", c.get("/api/contacts", headers=a_headers).json()["contacts"] == [])

    # The owner's signature over one contact cannot save another, or be used twice.
    r = c.post("/api/contacts/prepare", headers=a_headers, json={"name": "sandy", "address": ADDR})
    typed = r.json()
    signature = Account.sign_typed_data(alice.key, typed["domain"], typed["types"], typed["message"]).signature.hex()
    other = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    r = c.post("/api/contacts", headers=a_headers, json={**typed["message"], "address": other, "signature": signature})
    check("a signature for one address can't save another", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = c.post("/api/contacts", headers=a_headers, json={**typed["message"], "signature": signature})
    check("its nonce was burned by that attempt", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    # The happy path, and the checksumming.
    r = add_contact(c, a_headers, alice, "Sandy", ADDR.lower())
    check("the owner's signature saves the contact", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
    check("the name is stored lowercase", r.json()["name"] == "sandy", r.text[:80])
    check("the address is checksummed", r.json()["address"] == ADDR, r.text[:80])

    r = c.get("/api/contacts", headers=a_headers)
    check("it comes back in the list", r.json()["contacts"] == [{"name": "sandy", "address": ADDR}], r.text[:120])

    # Cross-account isolation, which is the property that matters most here.
    check("another account does not see it", c.get("/api/contacts", headers=b_headers).json()["contacts"] == [])
    check("another account cannot delete it",
          c.delete("/api/contacts/sandy", headers=b_headers).status_code == 404)
    check("and it survived that attempt",
          c.get("/api/contacts", headers=a_headers).json()["contacts"][0]["name"] == "sandy")
    r = c.post("/api/contacts/prepare", headers=a_headers, json={"name": "eve", "address": other})
    typed = r.json()
    signature = Account.sign_typed_data(alice.key, typed["domain"], typed["types"], typed["message"]).signature.hex()
    r = c.post("/api/contacts", headers=b_headers, json={**typed["message"], "signature": signature})
    check("another account can't spend Alice's approval", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check("so Bob's list is still empty", c.get("/api/contacts", headers=b_headers).json()["contacts"] == [])

    # Bad input is refused before the wallet is asked to sign anything.
    for label, body in [
        ("a non-address", {"name": "x", "address": "not-an-address"}),
        ("a truncated address", {"name": "x", "address": ADDR[:-2]}),
        ("a blank name", {"name": "   ", "address": ADDR}),
        ("an empty name", {"name": "", "address": ADDR}),
        ("a name with a slash", {"name": "a/b", "address": ADDR}),
        ("an over-long name", {"name": "z" * 65, "address": ADDR}),
        # A browser can't send DELETE /api/contacts/.. as written, so these could never be removed.
        ("a '.' name", {"name": ".", "address": ADDR}),
        ("a '..' name", {"name": " .. ", "address": ADDR}),
    ]:
        r = c.post("/api/contacts/prepare", headers=a_headers, json=body)
        check(f"{label} is rejected", r.status_code == 422, f"{r.status_code} {r.text[:90]}")

    # "me" already means the wallet itself in _resolve_contact, so a contact under that name
    # would be listed and permanently unpayable.
    r = c.post("/api/contacts/prepare", headers=a_headers, json={"name": "Me", "address": ADDR})
    check("'me' is reserved", r.status_code == 422, f"{r.status_code} {r.text[:90]}")

    # Re-saving a name updates it rather than duplicating -- with a signature of its own.
    add_contact(c, a_headers, alice, "sandy", other)
    contacts = c.get("/api/contacts", headers=a_headers).json()["contacts"]
    check("re-saving updates in place", contacts == [{"name": "sandy", "address": other}], str(contacts))

    # The web app deletes by the name run through encodeURIComponent, so a name with spaces and
    # URL characters must survive that round trip.
    odd = "o'neil & co? #1"
    add_contact(c, a_headers, alice, odd, ADDR)
    r = c.delete("/api/contacts/" + quote(odd, safe="-_.!~*'()"), headers=a_headers)
    check("a name with spaces, ?, & and # deletes by its encoded form",
          r.status_code == 200 and r.json()["name"] == odd, f"{r.status_code} {r.text[:90]}")

    # Deleting only ever shrinks the allowlist, so it takes no signature.
    check("deleting works", c.delete("/api/contacts/SANDY", headers=a_headers).status_code == 200)
    check("the list is empty again", c.get("/api/contacts", headers=a_headers).json()["contacts"] == [])
    check("deleting an unknown contact is a 404",
          c.delete("/api/contacts/sandy", headers=a_headers).status_code == 404)

    # An account with no owner address could never sign, so it is refused before a nonce is issued.
    r = c.post("/api/contacts/prepare", headers=headers_for_account_without_address(), json=contact)
    check("an account with no owner address can't prepare one -> 403", r.status_code == 403, f"{r.status_code}")


def test_chat_history_shows_only_the_conversation():
    """
    GET /api/chat/history returns what was SAID, never the tool traffic around it.

    Tool messages and tool-call arguments carry the session-key ciphertext, so the stub thread below
    plants a marker in every place a ciphertext can sit and asserts it never reaches the response.
    The real agent is not needed (or wanted: it would call Anthropic) -- only its checkpointed state.
    """
    print("\n[10] chat history returns what was said, never tool traffic")
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    import smart_wallet_agent

    secret = "CIPHERTEXT-must-never-leave-0xdeadbeef"
    thread = [
        HumanMessage(content="send 5 usdc to sandy"),
        # A tool-only turn: no text, arguments carrying the secret.
        AIMessage(content="", tool_calls=[{"name": "get_session_keys", "args": {"x": secret}, "id": "t1"}]),
        ToolMessage(content=secret, tool_call_id="t1"),
        # The announcement-before-a-transaction turn: text AND a tool_use block, as Anthropic returns it.
        AIMessage(
            content=[
                {"type": "text", "text": "Sending transaction, hold tight."},
                {"type": "tool_use", "id": "t2", "name": "transfer_erc20", "input": {"ciphertext": secret}},
            ],
            tool_calls=[{"name": "transfer_erc20", "args": {"ciphertext": secret}, "id": "t2"}],
        ),
        ToolMessage(content=f"ok {secret}", tool_call_id="t2"),
        AIMessage(content="Done: sent 5 USDC to sandy."),
    ]
    threads_read = []

    class StubAgent:
        def get_state(self, config):
            threads_read.append(config["configurable"]["thread_id"])
            return SimpleNamespace(values={"messages": thread})

    original = smart_wallet_agent.agent
    smart_wallet_agent.agent = StubAgent()
    try:
        c = make_client()
        body, headers, _ = sign_in(c)
        url = "/api/chat/history?chain_id=11155111"

        check("history needs a token", c.get(url).status_code == 401)

        r = c.get(url, headers=headers)
        check("history is readable when signed in", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        got = r.json()["messages"]
        check("only the conversation comes back, oldest first", got == [
            {"role": "user", "text": "send 5 usdc to sandy"},
            {"role": "assistant", "text": "Sending transaction, hold tight."},
            {"role": "assistant", "text": "Done: sent 5 USDC to sandy."},
        ], str(got))
        check("the ciphertext never appears", secret not in r.text)
        check("the thread read is the caller's own",
              threads_read[-1] == f"{body['user_id']}:11155111", str(threads_read))

        r = c.get(url + "&limit=1", headers=headers)
        check("limit keeps the most recent", r.json()["messages"] == [
            {"role": "assistant", "text": "Done: sent 5 USDC to sandy."}
        ], r.text[:160])
        check("a limit over 200 -> 422", c.get(url + "&limit=201", headers=headers).status_code == 422)
        check("an unsupported chain -> 400",
              c.get("/api/chat/history?chain_id=999999", headers=headers).status_code == 400)
    finally:
        smart_wallet_agent.agent = original


def test_chat_turn_returns_text_and_hides_failures():
    """
    POST /api/chat answers the reply as plain text, refuses a chain the server does not serve, and
    never repeats an exception to the user: its text can carry an RPC URL with an API key in it.
    """
    print("\n[10b] a chat turn returns text, and a failed turn hides its error")
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    import smart_wallet_agent

    secret = "https://rpc.example/v2/API-KEY-must-never-leave"
    turns = []

    class StubAgent:
        fail = False

        def get_state(self, config):
            # An empty conversation: nothing for chat() to start afresh.
            return SimpleNamespace(values={"messages": []})

        def invoke(self, state, config, context):
            turns.append((state["messages"][0].content, config["configurable"]["thread_id"], context.user_id))
            if self.fail:
                raise RuntimeError(f"HTTPError for url: {secret}")
            # Anthropic content blocks, not a string.
            return {"messages": [AIMessage(content=[
                {"type": "text", "text": "You have "},
                {"type": "text", "text": "1.5 ETH."},
            ])]}

    stub = StubAgent()
    original = smart_wallet_agent.agent
    smart_wallet_agent.agent = stub
    try:
        c = make_client()
        body, headers, _ = sign_in(c)
        ask = {"chain_id": 11155111, "message": "what's my balance?"}

        check("chat needs a token", c.post("/api/chat", json=ask).status_code == 401)
        r = c.post("/api/chat", json=ask, headers=headers)
        check("a reply in content blocks comes back as one string",
              r.status_code == 200 and r.json() == {"reply": "You have 1.5 ETH."}, f"{r.status_code} {r.text[:160]}")
        check("the turn ran on the caller's own thread, message untouched",
              turns[-1] == ("what's my balance?", f"{body['user_id']}:11155111", body["user_id"]), str(turns))

        runs = len(turns)
        r = c.post("/api/chat", json={**ask, "chain_id": 999999}, headers=headers)
        check("an unsupported chain -> 400, and the agent never runs",
              r.status_code == 400 and len(turns) == runs, f"{r.status_code} {len(turns)}")
        check("an empty message -> 422",
              c.post("/api/chat", json={**ask, "message": ""}, headers=headers).status_code == 422)

        stub.fail = True
        r = c.post("/api/chat", json=ask, headers=headers)
        check("a failed turn still answers with an apology", r.status_code == 200 and "Sorry" in r.json()["reply"], r.text[:160])
        check("the apology does not leak the exception", "API-KEY" not in r.text and "HTTPError" not in r.text, r.text[:160])
    finally:
        smart_wallet_agent.agent = original


def test_chat_acts_on_the_pages_network():
    """
    A chat turn acts on the network the page is on, not the user's saved one -- which is only the
    chain they last deployed on. Asked "how much BNB do I have?" on the BNB Smart Chain page, the
    agent used to read the Arbitrum wallet.

    Driven through a real LangGraph agent with a scripted model, because the thing to prove is that
    the TOOLS see the turn's network: LangGraph runs a message's tool calls in worker threads.
    """
    print("\n[10d] a chat turn acts on the page's network, not the saved one")
    from langchain.agents import create_agent
    from langchain.tools import ToolRuntime, tool
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    import smart_wallet_agent
    from agent_context import AgentContext

    seen = []

    @tool
    def which_network(runtime: ToolRuntime[AgentContext]) -> str:
        """Reports the network this tool acts on."""
        seen.append(db.get_user_network(runtime.context.user_id))
        return seen[-1]

    class ScriptedModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    # Two calls in one message, so LangGraph runs them in parallel threads.
    calls = [{"name": "which_network", "args": {}, "id": f"call-{i}", "type": "tool_call"} for i in (1, 2)]
    model = ScriptedModel(messages=iter([AIMessage(content="", tool_calls=calls), AIMessage(content="done")]))

    original = smart_wallet_agent.agent
    smart_wallet_agent.agent = create_agent(
        model, tools=[which_network], context_schema=AgentContext, checkpointer=InMemorySaver()
    )
    try:
        c = make_client()
        body, headers, _ = sign_in(c)
        db.save_user_network(body["user_id"], "arbitrum-fork")   # the chain they last deployed on

        r = c.post("/api/chat", json={"chain_id": 56, "message": "how much BNB do I have?"}, headers=headers)
        page = api._network_name(56)
        check("the turn ran", r.status_code == 200 and r.json() == {"reply": "done"}, f"{r.status_code} {r.text[:160]}")
        check("every tool call acted on the page's network, not the saved one", seen == [page, page], str(seen))
        check("the saved network is left alone", db.get_user_network(body["user_id"]) == "arbitrum-fork",
              str(db.get_user_network(body["user_id"])))

        try:
            smart_wallet_agent.chat(body["user_id"], 56, "hi", "arbitrum-fork")
            refused = False
        except ValueError:
            refused = True
        check("a network that is not the turn's chain is refused, not answered", refused)
    finally:
        smart_wallet_agent.agent = original


def test_vault_failure_is_named():
    """
    A Vault error answers 503 naming Vault, not a bare 500 — which the web app can only show as
    "Something went wrong on our side". The usual cause is the dev container restarting and losing
    its AppRole, so the login in .env is refused.
    """
    print("\n[10c] a Vault failure is named, not a bare 500")
    import hvac.exceptions

    @api.app.get("/__test/vault-down")
    def _vault_down():
        raise hvac.exceptions.Forbidden("permission denied, on post http://127.0.0.1:8200/v1/auth/approle/login")

    r = make_client().get("/__test/vault-down")
    check("a Vault error -> 503", r.status_code == 503, f"{r.status_code} {r.text[:160]}")
    detail = r.json().get("detail", "")
    check("the detail names Vault and the fix", "Vault" in detail and "make vault" in detail, r.text[:200])
    check("the exception itself stays in the log", "8200" not in detail, r.text[:200])


def test_chains_lists_only_deployed_served_chains():
    print("\n[11] /api/chains lists chains that are served AND deployed")
    conn = db.get_db()
    conn.execute("DELETE FROM factory")
    conn.executemany(
        "INSERT INTO factory (chain_id, address) VALUES (?, ?)",
        [(11155111, ADDR), (56, ADDR), (999999, ADDR)],   # 999999: deployed, but not served
    )
    fork_rpcs = {11155111: "http://127.0.0.1:8545", 56: "http://127.0.0.1:8546"}
    conn.executemany(
        "INSERT OR REPLACE INTO rpcs (name, rpc_url) VALUES (?, ?)",
        [("sepolia-fork", fork_rpcs[11155111]), ("bsc-fork", fork_rpcs[56])],
    )
    conn.commit()

    c = make_client()
    r = c.get("/api/chains")
    check("the chain list is public", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    chains = r.json()["chains"]
    ids = [x["chain_id"] for x in chains]
    check("served chains with a factory are listed, in order", ids == [56, 11155111], str(ids))
    check("a factory on a chain this server does not serve is left out", 999999 not in ids)
    check("a served chain with no factory is left out", 1 not in ids)

    by_id = {x["chain_id"]: x for x in chains}
    check("each carries its name and native ticker",
          by_id[11155111]["name"] == "sepolia" and by_id[11155111]["native_ticker"] == "ETH"
          and by_id[56]["native_ticker"] == "BNB", str(chains))
    check("the fork flag follows APP_FORK_MODE", by_id[11155111]["fork"] == api.FORK_MODE, str(chains))
    check("a fork carries its own local node, a live chain none",
          {cid: by_id[cid]["rpc_url"] for cid in fork_rpcs}
          == (fork_rpcs if api.FORK_MODE else dict.fromkeys(fork_rpcs)), str(chains))
    check("each carries the router its wallets trust",
          by_id[11155111]["router"] == Web3.to_checksum_address(get_router(11155111)), str(chains))


def test_rate_limit():
    print("\n[13] the open sign-in endpoints are rate limited")
    c = make_client(rate_limit=True)
    bogus = {"message": "not a SIWE message", "signature": "0x00", "nonce": "abcdefgh"}
    codes = [c.post("/api/auth/siwe/login", json=bogus).status_code for _ in range(14)]
    check("repeated sign-in attempts eventually get 429", 429 in codes, f"codes: {codes}")
    codes = [c.get("/api/auth/siwe/nonce").status_code for _ in range(34)]
    check("so does asking for nonces without end", 429 in codes, f"codes: {codes}")
    api.limiter.enabled = False


if __name__ == "__main__":
    try:
        test_siwe_sign_in_and_refresh()
        test_bad_tokens_rejected()
        test_refresh_reuse_revokes_everything()
        test_identity_comes_from_token_not_body()
        test_siwe_checks_and_deployer_check()
        test_telegram_link_nonce()
        test_bot_start_explains_a_chat_linked_elsewhere()
        test_owner_actions_are_guarded()
        test_router_removal_is_refused()
        test_wrapped_native_always_counts()
        test_wallet_state_read_is_guarded()
        test_contacts_are_web_only_and_per_account()
        test_chat_history_shows_only_the_conversation()
        test_chat_turn_returns_text_and_hides_failures()
        test_chat_acts_on_the_pages_network()
        test_vault_failure_is_named()
        test_chains_lists_only_deployed_served_chains()
        test_rate_limit()
    finally:
        os.unlink(_tmp_db.name)

    finish("All auth checks passed.")
