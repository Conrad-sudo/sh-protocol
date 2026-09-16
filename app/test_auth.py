"""
End-to-end checks on the API's authentication, run against a throwaway database.

The property under test is the one the whole identity refactor exists to establish: a request acts
on the account its TOKEN names, and on no other. Everything here is offline -- no RPC, no chain, no
Vault -- so it is safe to run anywhere.

Run: make auth-test   (or: python app/test_auth.py)
"""
import os
import sys
import tempfile
import time

# A scratch database, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"          # the test client speaks http
os.environ["TELEGRAM_BOT_USERNAME"] = "test_wallet_bot"

import db                                   # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

from fastapi.testclient import TestClient   # noqa: E402
from eth_account import Account             # noqa: E402

import api                                  # noqa: E402
import auth                                 # noqa: E402

failures: list[str] = []

# A checksummed address, used by the contacts tests to prove the API normalises what it stores.
ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")
        failures.append(label)


# The agent's checkpointer is not needed for auth, and opening it would touch the real DB path;
# the lifespan is stubbed out so TestClient can start the app without it.
api.app.router.lifespan_context = None


def make_client(rate_limit: bool = False) -> TestClient:
    """
    A test client with the agent lifespan disabled.

    Rate limiting is off by default: these tests create many accounts in a few seconds, which the
    real 5/minute signup limit would (correctly) block. test_rate_limit turns it back on to check
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


def test_signup_login_refresh():
    print("\n[1] signup -> login -> refresh -> protected endpoint")
    c = make_client()

    r = c.post("/api/auth/signup", json={"email": "a@example.com", "password": "hunter2hunter2"})
    check("signup returns 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
    body = r.json()
    check("signup returns an access token", "access_token" in body)
    check("the refresh token is NOT in the body", "refresh_token" not in body)
    check("the refresh cookie is set", api.REFRESH_COOKIE in r.cookies, str(dict(r.cookies)))
    user_id = body["user_id"]

    r = c.get("/api/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    check("the token reaches a protected endpoint", r.status_code == 200, r.text[:120])
    check("it resolves to the right account", r.json()["user_id"] == user_id)

    r = c.post("/api/auth/signup", json={"email": "a@example.com", "password": "hunter2hunter2"})
    check("a duplicate email is refused", r.status_code == 409, str(r.status_code))

    r = c.post("/api/auth/login", json={"email": "a@example.com", "password": "wrongwrongwrong"})
    check("a wrong password is refused", r.status_code == 401, str(r.status_code))

    r = c.post("/api/auth/login", json={"email": "nobody@example.com", "password": "hunter2hunter2"})
    check("an unknown account gives the SAME 401", r.status_code == 401, str(r.status_code))

    r = c.post("/api/auth/login", json={"email": "a@example.com", "password": "hunter2hunter2"})
    check("a correct password signs in", r.status_code == 200, r.text[:120])

    r = c.post("/api/auth/refresh")
    check("the cookie alone refreshes", r.status_code == 200, r.text[:160])
    check("refresh returns a new access token", "access_token" in r.json())


def test_bad_tokens_rejected():
    print("\n[2] forged, expired and mistyped tokens are refused")
    c = make_client()
    c.post("/api/auth/signup", json={"email": "b@example.com", "password": "hunter2hunter2"})

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
    r = c.post("/api/auth/signup", json={"email": "c@example.com", "password": "hunter2hunter2"})
    stolen = r.cookies[api.REFRESH_COOKIE]

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


def test_identity_comes_from_token_not_body():
    print("\n[4] a request acts on the account its TOKEN names")
    c = make_client()
    alice = c.post("/api/auth/signup", json={"email": "alice@example.com", "password": "hunter2hunter2"}).json()
    bob = c.post("/api/auth/signup", json={"email": "bob@example.com", "password": "hunter2hunter2"}).json()
    check("two distinct accounts", alice["user_id"] != bob["user_id"])

    # Alice's token, Bob's id in the body. The body must be ignored -- and in fact there is no
    # user_id field left to send, so this also proves the schema rejects the old shape.
    r = c.get("/api/me", headers={"Authorization": f"Bearer {alice['access_token']}"})
    check("Alice's token resolves to Alice", r.json()["user_id"] == alice["user_id"])

    r = c.post(
        "/api/deploy",
        headers={"Authorization": f"Bearer {alice['access_token']}"},
        json={
            "user_id": bob["user_id"],          # ignored: not a field on DeployRequest
            "chain_id": 31337,
            "deployer": "0x0000000000000000000000000000000000000001",
            "daily_limit_usd": 100,
        },
    )
    # 403 because Alice has bound no address yet -- which is the point: the body cannot name an
    # account, and it cannot name an EOA the caller has not proved they hold.
    check("deploy refuses an unbound deployer", r.status_code == 403, f"{r.status_code} {r.text[:160]}")
    check("the refusal names the SIWE step", "siwe" in r.text.lower(), r.text[:160])


def test_siwe_binding_and_deployer_check():
    print("\n[5] SIWE binds an address, and only that address may deploy")
    c = make_client()
    signed_in = c.post("/api/auth/signup", json={"email": "d@example.com", "password": "hunter2hunter2"}).json()
    headers = {"Authorization": f"Bearer {signed_in['access_token']}"}

    acct = Account.create()
    nonce = c.get("/api/auth/siwe/nonce").json()["nonce"]
    message = f"session-key-infra wants you to sign in with your Ethereum account.\nNonce: {nonce}"
    signature = Account.sign_message(
        __import__("eth_account").messages.encode_defunct(text=message), acct.key
    ).signature.hex()

    r = c.post(
        "/api/auth/siwe/verify",
        headers=headers,
        json={"message": message, "signature": signature, "nonce": nonce},
    )
    check("a valid signature binds the address", r.status_code == 200, r.text[:160])
    check("the recovered address is right", r.json()["owner_addr"] == acct.address, r.text[:160])

    # The nonce is single-use.
    r = c.post(
        "/api/auth/siwe/verify",
        headers=headers,
        json={"message": message, "signature": signature, "nonce": nonce},
    )
    check("replaying the same nonce is refused", r.status_code == 400, str(r.status_code))

    # A signature over a message that does not carry the issued nonce.
    fresh = c.get("/api/auth/siwe/nonce").json()["nonce"]
    other = Account.sign_message(
        __import__("eth_account").messages.encode_defunct(text="unrelated message"), acct.key
    ).signature.hex()
    r = c.post(
        "/api/auth/siwe/verify",
        headers=headers,
        json={"message": "unrelated message", "signature": other, "nonce": fresh},
    )
    check("a message not carrying the nonce is refused", r.status_code == 400, str(r.status_code))

    # Deploying from an address this account did NOT prove it holds.
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
    signed_in = c.post("/api/auth/signup", json={"email": "e@example.com", "password": "hunter2hunter2"}).json()
    headers = {"Authorization": f"Bearer {signed_in['access_token']}"}

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


def test_owner_actions_are_guarded():
    print("\n[7] owner actions need a token AND a proved owner address")
    c = make_client()
    signed_in = c.post("/api/auth/signup", json={"email": "f@example.com", "password": "hunter2hunter2"}).json()
    headers = {"Authorization": f"Bearer {signed_in['access_token']}"}

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
        # that has not proved an address never reaches the node.
        check(f"{path} without a bound address -> 403", r.status_code == 403, f"{r.status_code} {r.text[:100]}")

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


def test_wallet_state_read_is_guarded():
    """
    GET /api/wallet/{chain_id} needs a token, and only ever reads the CALLER's wallet.

    The on-chain assertions live in test_e2e_fork; what matters offline is that the route cannot be
    reached anonymously and does not fall through to another account's row. A signed-in account with
    no wallet must get 404 -- never somebody else's.
    """
    print("\n[8] wallet state reads are authenticated and per-account")
    c = make_client()
    signed_in = c.post("/api/auth/signup", json={"email": "w@example.com", "password": "hunter2hunter2"}).json()
    headers = {"Authorization": f"Bearer {signed_in['access_token']}"}

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
    Adding a payee takes the web credential, and reaches only your own list.

    The agent has no save_contact tool (guarded in test_identity.py); this is the other half --
    the endpoint that replaced it must actually require a signed-in account, and must not let one
    account read, write or delete another's contacts. Contacts are the allowlist of destinations
    for the wallet's funds, so a cross-account write here would be a way to add a payee to
    somebody else's wallet.
    """
    print("\n[9] contacts need a web session and stay within one account")
    c = make_client()
    alice = c.post("/api/auth/signup", json={"email": "g@example.com", "password": "hunter2hunter2"}).json()
    bob = c.post("/api/auth/signup", json={"email": "h@example.com", "password": "hunter2hunter2"}).json()
    a_headers = {"Authorization": f"Bearer {alice['access_token']}"}
    b_headers = {"Authorization": f"Bearer {bob['access_token']}"}

    # Unauthenticated, on every verb.
    check("adding needs a token",
          c.post("/api/contacts", json={"name": "mallory", "address": ADDR}).status_code == 401)
    check("listing needs a token", c.get("/api/contacts").status_code == 401)
    check("deleting needs a token", c.delete("/api/contacts/mallory").status_code == 401)

    # The happy path, and the checksumming.
    r = c.post("/api/contacts", headers=a_headers, json={"name": "Sandy", "address": ADDR.lower()})
    check("a signed-in user can add a contact", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
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

    # Bad input is refused before it can be stored and read back as a destination later.
    for label, body in [
        ("a non-address", {"name": "x", "address": "not-an-address"}),
        ("a truncated address", {"name": "x", "address": ADDR[:-2]}),
        ("a blank name", {"name": "   ", "address": ADDR}),
        ("an empty name", {"name": "", "address": ADDR}),
        ("a name with a slash", {"name": "a/b", "address": ADDR}),
        ("an over-long name", {"name": "z" * 65, "address": ADDR}),
    ]:
        r = c.post("/api/contacts", headers=a_headers, json=body)
        check(f"{label} is rejected", r.status_code == 422, f"{r.status_code} {r.text[:90]}")

    # "me" already means the wallet itself in _resolve_contact, so a contact under that name
    # would be listed and permanently unpayable.
    r = c.post("/api/contacts", headers=a_headers, json={"name": "Me", "address": ADDR})
    check("'me' is reserved", r.status_code == 422, f"{r.status_code} {r.text[:90]}")

    # Re-saving a name updates it rather than duplicating.
    other = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    c.post("/api/contacts", headers=a_headers, json={"name": "sandy", "address": other})
    contacts = c.get("/api/contacts", headers=a_headers).json()["contacts"]
    check("re-saving updates in place", contacts == [{"name": "sandy", "address": other}], str(contacts))

    check("deleting works", c.delete("/api/contacts/SANDY", headers=a_headers).status_code == 200)
    check("the list is empty again", c.get("/api/contacts", headers=a_headers).json()["contacts"] == [])
    check("deleting an unknown contact is a 404",
          c.delete("/api/contacts/sandy", headers=a_headers).status_code == 404)


def test_rate_limit():
    print("\n[10] credential endpoints are rate limited")
    c = make_client(rate_limit=True)
    codes = [
        c.post(
            "/api/auth/login", json={"email": f"rl{i}@example.com", "password": "hunter2hunter2"}
        ).status_code
        for i in range(14)
    ]
    check("repeated login attempts eventually get 429", 429 in codes, f"codes: {codes}")
    api.limiter.enabled = False


if __name__ == "__main__":
    try:
        test_signup_login_refresh()
        test_bad_tokens_rejected()
        test_refresh_reuse_revokes_everything()
        test_identity_comes_from_token_not_body()
        test_siwe_binding_and_deployer_check()
        test_telegram_link_nonce()
        test_owner_actions_are_guarded()
        test_wallet_state_read_is_guarded()
        test_contacts_are_web_only_and_per_account()
        test_rate_limit()
    finally:
        os.unlink(_tmp_db.name)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print("All auth checks passed.")
