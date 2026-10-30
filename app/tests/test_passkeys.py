"""
Passkeys, offline: how an approval is packed for the wallet (checked against the known-answer
vector SessionHandler's own Foundry test accepts), the op deadline in a passkey op's nonce, making
and managing passkeys through the API, and the Telegram card's link -- the approval page's start,
finish and cancel by the link's token alone, and the outcome posted back to the chat.

No chain, no Telegram and no phone: a soft passkey (passkey.SoftPasskey) answers WebAuthn as a
phone would, py_webauthn checks it for real, and the wallet reads, the send and the Bot API are
stand-ins. Runs against a throwaway database.

Run: make passkeys-test   (or: python app/tests/test_passkeys.py)
"""
import os
import re
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from checks import check, finish, sign_in   # first: it puts app/ on sys.path for the imports below

# A scratch database, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"
os.environ["SIWE_DOMAIN"] = "localhost:3000"
# The site every passkey here is made for, set so a .env cannot move it.
os.environ["WEBAUTHN_RP_ID"] = "localhost"
os.environ["WEBAUTHN_ORIGINS"] = "http://localhost:3000"

import db                                   # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

import cbor2                                # noqa: E402
from eth_abi import decode as abi_decode    # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url  # noqa: E402

import api                                  # noqa: E402
import passkey                              # noqa: E402
import quotes                               # noqa: E402
import routes.passkeys                      # noqa: E402
import signing                              # noqa: E402
import smart_wallet_agent                   # noqa: E402
import telegram_notify                      # noqa: E402
import userop                               # noqa: E402
from bundler import FinalUserOp, UserOpQuote  # noqa: E402
from passkey import P256_N, SoftPasskey     # noqa: E402

CHAIN = 11155111
NETWORK = "sepolia-fork"
WALLET = "0x00000000000000000000000000000000000A11CE"
ORIGIN = "http://localhost:3000"
PRICED = UserOpQuote(
    calldata="0x1234", call_gas=100_000, call_gas_used=80_000, verification_gas=200_000,
    verification_gas_used=150_000, pre_verification_gas=60_000, max_fee=2 * 10**9, tip=10**9,
    base_fee=10**9, outer_gas=500_000, simulated_gas=None, chain_name=NETWORK,
)

# The vector PasskeyValidationTest.test_knownAnswer_* accepts on chain: made outside Foundry (a
# Chrome-shaped clientDataJSON, flags 0x1d, counter 7, s folded low). The same bytes must pass here.
KAT_SOURCE = (Path(__file__).resolve().parents[2] / "test/unit/PasskeyValidationTest.t.sol").read_text()


def _kat(name: str) -> bytes:
    """One of the vector's constants, as the Solidity test declares it."""
    found = re.search(name + r'\s*=\s*(?:hex")?(?:0x)?([0-9a-fA-F]+)', KAT_SOURCE)
    assert found, f"{name} not in PasskeyValidationTest.t.sol"
    return bytes.fromhex(found[1])


KAT_QX, KAT_QY, KAT_CHALLENGE, KAT_BLOB = (_kat(n) for n in ("KAT_QX", "KAT_QY", "KAT_CHALLENGE", "KAT_BLOB"))


def make_client() -> TestClient:
    """A test client with the agent lifespan disabled and the rate limits off."""

    @asynccontextmanager
    async def _noop(_app):
        yield

    api.app.router.lifespan_context = _noop
    api.limiter.enabled = False
    return TestClient(api.app)


# ── Packing an approval for the wallet ────────────────────────────────────────


def test_packing():
    print("\n[1] an approval is packed the way SessionHandler reads it")
    check("the known-answer vector the contract accepts is accepted here too",
          passkey.verify_like_contract(KAT_CHALLENGE, KAT_QX + KAT_QY + KAT_BLOB))
    check("...for its own challenge only",
          not passkey.verify_like_contract(bytes(31) + b"\x01", KAT_QX + KAT_QY + KAT_BLOB))

    phone = SoftPasskey()
    challenge = secrets.token_bytes(32)
    signature = phone.contract_signature(challenge)
    r, s, challenge_index, type_index, auth_data, client_data = abi_decode(
        ["uint256", "uint256", "uint256", "uint256", "bytes", "bytes"], signature[64:]
    )
    check("it starts with the public key: qx, then qy", signature[:64] == phone.qx + phone.qy)
    check("then six values in a row, not a struct with an offset word in front",
          len(signature) == passkey.packed_length(len(auth_data), len(client_data)) and len(signature) >= 256)
    check("challengeIndex points at the challenge in clientDataJSON",
          client_data[challenge_index:].startswith(b'"challenge":"' + bytes_to_base64url(challenge).encode() + b'"'))
    check("typeIndex points at its type", client_data[type_index:].startswith(b'"type":"webauthn.get"'))
    check("s is the low one, as OpenZeppelin requires", s <= P256_N // 2)
    check("the flags say present AND verified", auth_data[32] & 0x05 == 0x05)
    check("it passes the contract's own checks", passkey.verify_like_contract(challenge, signature))
    tampered = bytearray(signature)
    tampered[-40] ^= 1
    check("one changed byte of clientDataJSON fails them", not passkey.verify_like_contract(challenge, bytes(tampered)))
    check("so does another challenge", not passkey.verify_like_contract(secrets.token_bytes(32), signature))
    unverified = phone.assertion(challenge, user_verified=False)["response"]
    without_uv = passkey.contract_signature(
        phone.qx, phone.qy, challenge, base64url_to_bytes(unverified["authenticatorData"]),
        base64url_to_bytes(unverified["clientDataJSON"]), base64url_to_bytes(unverified["signature"]),
    )
    check("an approval without Face ID or a PIN fails them", not passkey.verify_like_contract(challenge, without_uv))
    check("the key id is the wallet's sessionKeyId: keccak256(abi.encode(qx, qy))",
          phone.key_id == "0x" + passkey.keccak(phone.qx + phone.qy).hex())


def test_op_deadline_nonce():
    print("\n[2] a passkey op's nonce carries its own deadline, then a random lane, sequence 0")
    deadline = int(time.time()) + 600
    nonce = userop.passkey_nonce(deadline)
    check("the wallet reads the deadline back as uint48(nonce >> 208)", (nonce >> 208) & (2**48 - 1) == deadline)
    check("its sequence is 0", nonce & (2**64 - 1) == 0)
    check("it fits a uint256", nonce < 2**256)
    lanes = {(userop.passkey_nonce(deadline) >> 64) & (2**144 - 1) for _ in range(50)}
    check("every op takes a lane of its own", len(lanes) == 50)
    for bad in (0, 2**48, -1):
        try:
            userop.passkey_nonce(bad)
            refused = False
        except ValueError:
            refused = True
        check(f"a deadline of {bad} is refused here, before the wallet would", refused)
    check("the grace covers every fee bump of a send, and a minute more",
          userop.OP_DEADLINE_GRACE_SECS >= (userop.MAX_FEE_BUMPS + 1) * userop.ATTEMPT_TIMEOUT_SECS + 60)


# ── Making and managing passkeys ──────────────────────────────────────────────


def _options(c, headers) -> dict:
    r = c.post("/api/passkeys/register/options", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["options"]


def _register(c, headers, phone: SoftPasskey, name: str = "Test phone", **kwargs):
    challenge = base64url_to_bytes(_options(c, headers)["challenge"])
    return c.post("/api/passkeys/register", headers=headers,
                  json={"credential": phone.registration_response(challenge, **kwargs), "device_name": name})


def test_registration():
    print("\n[3] making a passkey: ES256 only, verified, for this site, one per device")
    c = make_client()
    _, headers, _ = sign_in(c)
    _, other_headers, _ = sign_in(c)

    options = _options(c, headers)
    check("for this site, by its name", options["rp"] == {"id": "localhost", "name": "Mitfah"}, str(options["rp"]))
    check("ES256 only", options["pubKeyCredParams"] == [{"type": "public-key", "alg": -7}], str(options["pubKeyCredParams"]))
    check("user verification required, a discoverable passkey preferred, no attestation",
          options["authenticatorSelection"]["userVerification"] == "required"
          and options["authenticatorSelection"]["residentKey"] == "preferred" and options["attestation"] == "none",
          str(options["authenticatorSelection"]))
    check("the account's handle is random and stays the same",
          options["user"]["id"] == _options(c, headers)["user"]["id"] != _options(c, other_headers)["user"]["id"]
          and len(base64url_to_bytes(options["user"]["id"])) == 16)
    check("nothing to exclude yet", options.get("excludeCredentials", []) == [])

    phone = SoftPasskey(rp_id="localhost", origin=ORIGIN)
    challenge = base64url_to_bytes(_options(c, headers)["challenge"])
    response = phone.registration_response(challenge)
    r = c.post("/api/passkeys/register", headers=headers, json={"credential": response, "device_name": "  Kani's iPhone "})
    body = r.json() if r.status_code == 200 else {}
    check("a good one is filed, under the wallet's name for it",
          r.status_code == 200 and body["key_id"] == phone.key_id and body["credential_id"] == phone.credential_id_b64
          and body["qx"] == "0x" + phone.qx.hex() and body["qy"] == "0x" + phone.qy.hex()
          and body["device_name"] == "Kani's iPhone", f"{r.status_code} {r.text[:200]}")
    r = c.post("/api/passkeys/register", headers=headers, json={"credential": response, "device_name": "again"})
    check("its challenge is used up: the same answer again -> 400", r.status_code == 400, f"{r.status_code} {r.text[:160]}")
    check("the next options exclude it, so the device doesn't make a second",
          [d["id"] for d in _options(c, headers)["excludeCredentials"]] == [phone.credential_id_b64])
    check("the same passkey again, with a fresh challenge -> 409", _register(c, headers, phone).status_code == 409)

    stolen = base64url_to_bytes(_options(c, headers)["challenge"])
    laptop = SoftPasskey(rp_id="localhost", origin=ORIGIN)
    r = c.post("/api/passkeys/register", headers=other_headers,
               json={"credential": laptop.registration_response(stolen), "device_name": "Laptop"})
    check("another account's challenge -> 400", r.status_code == 400, f"{r.status_code} {r.text[:160]}")
    check("without Face ID or a PIN -> 400", _register(c, headers, laptop, user_verified=False).status_code == 400)
    check("made on another site -> 400",
          _register(c, headers, laptop, origin="https://evil.example").status_code == 400)
    rsa = cbor2.dumps({1: 3, 3: -257, -1: b"\x01" * 256, -2: b"\x01\x00\x01"})
    check("a key the wallet can't check (RS256) -> 400", _register(c, headers, laptop, cose_key=rsa).status_code == 400)
    for name, why in (("", "no name"), ("   ", "only spaces"), ("x" * 41, "a name too long")):
        check(f"{why} -> 422", _register(c, headers, laptop, name=name).status_code == 422)
    check("a login-less request -> 401", c.post("/api/passkeys/register/options").status_code == 401)

    r = _register(c, headers, laptop, name="Laptop")
    check("a second device is fine", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    listed = c.get("/api/passkeys", headers=headers).json()["passkeys"]
    check("listed oldest first, without public keys or counters",
          [(p["key_id"], p["device_name"]) for p in listed] == [(phone.key_id, "Kani's iPhone"), (laptop.key_id, "Laptop")]
          and all(set(p) == {"key_id", "credential_id", "device_name", "created_at"} for p in listed), str(listed))
    check("only to their own account", c.get("/api/passkeys", headers=other_headers).json()["passkeys"] == [])


def test_managing():
    print("\n[4] renaming and forgetting a passkey: never one a wallet still approves")
    c = make_client()
    _, headers, _ = sign_in(c)
    _, other_headers, _ = sign_in(c)
    phone = SoftPasskey(rp_id="localhost", origin=ORIGIN)
    assert _register(c, headers, phone).status_code == 200

    r = c.patch(f"/api/passkeys/{phone.key_id}", headers=headers, json={"device_name": "Work phone"})
    check("renamed", r.status_code == 200 and r.json()["device_name"] == "Work phone", f"{r.status_code} {r.text[:160]}")
    check("not by another account",
          c.patch(f"/api/passkeys/{phone.key_id}", headers=other_headers, json={"device_name": "mine"}).status_code == 404)

    saved = routes.passkeys._live_on
    try:
        routes.passkeys._live_on = lambda _user, _key: [CHAIN]
        r = c.delete(f"/api/passkeys/{phone.key_id}", headers=headers)
        check("approved on a wallet: kept, 409, saying where to remove it",
              r.status_code == 409 and "Controls → Devices" in r.json()["detail"], f"{r.status_code} {r.text[:160]}")

        def unreadable(_user, _key):
            raise ConnectionError("https://rpc.example/v2/SECRET down")

        routes.passkeys._live_on = unreadable
        r = c.delete(f"/api/passkeys/{phone.key_id}", headers=headers)
        check("a wallet that can't be read: kept, 503, the RPC's address not shown",
              r.status_code == 503 and "SECRET" not in r.text, f"{r.status_code} {r.text[:160]}")
        routes.passkeys._live_on = lambda _user, _key: []
        check("another account can't forget it", c.delete(f"/api/passkeys/{phone.key_id}", headers=other_headers).status_code == 404)
        check("approved nowhere: forgotten", c.delete(f"/api/passkeys/{phone.key_id}", headers=headers).status_code == 204
              and c.get("/api/passkeys", headers=headers).json()["passkeys"] == [])
        check("once", c.delete(f"/api/passkeys/{phone.key_id}", headers=headers).status_code == 404)
    finally:
        routes.passkeys._live_on = saved


# ── A Telegram card's link ────────────────────────────────────────────────────


def test_telegram_link():
    print("\n[5] a Telegram card's link: approved by its token alone, the outcome posted back to the chat")
    c = make_client()
    body, headers, _ = sign_in(c)
    user = body["user_id"]
    _, other_headers, _ = sign_in(c)
    phone = SoftPasskey(rp_id="localhost", origin=ORIGIN)
    assert _register(c, headers, phone).status_code == 200

    posted, sends = [], []
    saved = {
        "notify": telegram_notify._post,
        "send": (smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted),
        "build": {name: getattr(signing, name) for name in
                  ("load_session_handler", "load_entry_point", "resolve_bundler", "finalize_user_op")},
        "agent": smart_wallet_agent.agent,
    }
    # The conversation the approval is recorded in: not under test here (test_history covers it).
    smart_wallet_agent.agent = SimpleNamespace(update_state=lambda *_args, **_kwargs: None)

    def fake_finalize(user_id, wallet, entry_point, quote, bundler, op_deadline):
        op = (WALLET, userop.passkey_nonce(op_deadline), b"", bytes.fromhex(quote.calldata[2:]), b"\x00" * 32,
              60_000, b"\x00" * 32, b"", b"")
        return FinalUserOp(op, secrets.token_bytes(32), 2 * 10**9, 10**9)

    def fake_submit(approved):
        sends.append((approved.request["user_id"], approved.frozen["action"], approved.key_id))
        return SimpleNamespace(user_id=user, chain_id=CHAIN, action=approved.frozen["action"], record=None,
                               text=f"Sending — {approved.frozen['action']}. Waiting for the network to confirm it.")

    def wait_for_posts(n: int) -> list:
        deadline = time.time() + 5
        while len(posted) < n and time.time() < deadline:
            time.sleep(0.02)
        return posted

    def card(action: str, message_id: int):
        pending = quotes.put(user, CHAIN, 1, action, [], PRICED, {"total_usd": 0.3})
        offered = signing.open_request(pending, signing.TELEGRAM, telegram_chat_id=4242)
        db.set_sign_request_message(offered.request["id"], 4242, message_id)
        return offered

    telegram_notify._post = lambda method, payload: posted.append((method, payload)) or True
    smart_wallet_agent.submit_signed = fake_submit
    smart_wallet_agent.settle_quoted = lambda sending: f"Sent — {sending.action}. Tx hash: `0x{'ab' * 32}`"
    signing.load_session_handler = lambda _user: SimpleNamespace(
        address=WALLET, functions=SimpleNamespace(
            getSessions=lambda: SimpleNamespace(call=lambda: ([bytes.fromhex(phone.key_id[2:])], [2**40]))))
    signing.load_entry_point = lambda _user: None
    signing.resolve_bundler = lambda: "bundler"
    signing.finalize_user_op = fake_finalize
    try:
        offered = card("Transfer 5 USDC to sam", 99)
        stored = db.get_sign_request(offered.request["id"])
        check("a Telegram card gets a link token, kept only as its hash",
              offered.token and stored["token_hash"] == signing.token_hash(offered.token)
              and offered.token not in str(stored))

        r = c.post("/api/sign/start", json={"token": offered.token})
        started = r.json() if r.status_code == 200 else {}
        check("the link's page starts with the token alone, no login",
              r.status_code == 200 and started["summary"]["action"] == "Transfer 5 USDC to sam"
              and started["allow_credentials"] == [{"type": "public-key", "id": phone.credential_id_b64}],
              f"{r.status_code} {r.text[:200]}")
        check("a token that names nothing -> 404, saying it expired",
              c.post("/api/sign/start", json={"token": "x" * 32}).json().get("detail") == signing.EXPIRED)
        check("neither a token nor a login -> 401",
              c.post("/api/sign/start", json={"chain_id": CHAIN, "quote_id": offered.request["quote_id"]}).status_code == 401)

        credential = phone.assertion(base64url_to_bytes(started["challenge"]))
        r = c.post("/api/sign/finish", json={"sign_id": started["sign_id"], "credential": credential})
        check("a finish with neither the token nor a login -> 401", r.status_code == 401)
        other = card("Another card", 98)
        r = c.post("/api/sign/finish", json={"sign_id": started["sign_id"], "credential": credential, "token": other.token})
        check("one with another card's token -> 409", r.status_code == 409, f"{r.status_code} {r.text[:160]}")
        r = c.post("/api/sign/finish", json={"sign_id": started["sign_id"], "credential": credential}, headers=other_headers)
        check("one with another account's login -> 409", r.status_code == 409, f"{r.status_code} {r.text[:160]}")
        check("none of them sent anything", sends == [], str(sends))

        r = c.post("/api/sign/finish", json={"sign_id": started["sign_id"], "credential": credential, "token": offered.token})
        check("the card's own token and the passkey's approval send it",
              r.status_code == 200 and r.json()["status"] == "sent"
              and sends == [(user, "Transfer 5 USDC to sam", phone.key_id)], f"{r.status_code} {r.text[:200]} {sends}")
        notes = wait_for_posts(3)
        check("the chat hears it: the card's buttons off, then the outcome, then how it landed",
              [m for m, _ in notes[:3]] == ["editMessageReplyMarkup", "sendMessage", "sendMessage"]
              and notes[0][1] == {"chat_id": 4242, "message_id": 99}
              and "Sending — Transfer 5 USDC to sam." in notes[1][1]["text"]
              and "Sent — Transfer 5 USDC to sam." in notes[2][1]["text"]
              and notes[1][1]["text"].startswith("<b>"), str(notes))
        check("the link is dead now", c.post("/api/sign/start", json={"token": offered.token}).status_code == 404)

        posted.clear()
        r = c.post("/api/sign/cancel", json={"token": other.token})
        check("the page's Cancel drops the card", r.status_code == 200
              and r.json() == {"outcome": smart_wallet_agent.CANCELLED_REPLY}, f"{r.status_code} {r.text[:160]}")
        notes = wait_for_posts(2)
        check("and the chat hears that too, the card's buttons off",
              [m for m, _ in notes[:2]] == ["editMessageReplyMarkup", "sendMessage"]
              and notes[0][1]["message_id"] == 98 and "Cancelled" in notes[1][1]["text"], str(notes))
        check("its link is dead", c.post("/api/sign/start", json={"token": other.token}).status_code == 404)

        third = card("Shown again", 97)
        fresh = signing.show_again(third.request)
        check("a card shown again gets a new token, and the old link dies",
              c.post("/api/sign/start", json={"token": third.token}).status_code == 404
              and c.post("/api/sign/start", json={"token": fresh}).status_code == 200)
        signing.cancel(db.get_sign_request(third.request["id"]))
    finally:
        telegram_notify._post = saved["notify"]
        smart_wallet_agent.agent = saved["agent"]
        smart_wallet_agent.submit_signed, smart_wallet_agent.settle_quoted = saved["send"]
        for name, value in saved["build"].items():
            setattr(signing, name, value)
        quotes.drop_all(user, CHAIN)


def test_new_card_voids_older():
    print("\n[6] a new card voids the older quotes on its chain: the old card can't come back and pay twice")
    c = make_client()
    body, _, _ = sign_in(c)
    user = body["user_id"]
    other_chain = 8453
    try:
        first = quotes.put(user, CHAIN, 1, "Send 1 ETH to sam", [], PRICED, {"total_usd": 0.3},
                           request="Send 1 ETH to sam.")
        old = signing.offer(user, CHAIN, 1, signing.TELEGRAM, telegram_chat_id=4343)
        elsewhere = quotes.put(user, other_chain, 1, "Send 1 ETH to sam", [], PRICED, {"total_usd": 0.3})
        signing.offer(user, other_chain, 1, signing.WEB)
        unoffered = quotes.put(user, CHAIN, 2, "Wrap 1 ETH", [], PRICED, {"total_usd": 0.3})
        quotes.put(user, CHAIN, 2, "Wrap 2 ETH", [], PRICED, {"total_usd": 0.3})
        check("a turn that raises two quotes offers neither, and voids nothing",
              signing.offer(user, CHAIN, 2, signing.WEB) is None
              and signing.is_live(db.get_sign_request(old.request["id"])))

        # The same request again, quoted afresh without confirm_transaction (2026-10-22).
        second = quotes.put(user, CHAIN, 3, "Send 1 ETH to sam", [], PRICED, {"total_usd": 0.3},
                            request="Send 1 ETH to sam.")
        new = signing.offer(user, CHAIN, 3, signing.WEB)
        check("the new card is offered", new is not None and new.pending.quote_id == second.quote_id)
        check("the older card's sign request is cancelled, and handed back for its buttons",
              [r["quote_id"] for r in new.superseded] == [first.quote_id]
              and db.get_sign_request(old.request["id"])["status"] == "cancelled", str(new.superseded))
        check("its link is dead", c.post("/api/sign/start", json={"token": old.token}).status_code == 404)
        check("the older quotes, offered or not, are gone from the store",
              not any(quotes.raised_in(user, CHAIN, t) for t in (1, 2)))
        try:
            quotes.get(user, CHAIN, unoffered.quote_id)
            gone = False
        except quotes.QuoteError:
            gone = True
        check("so confirm_transaction can't show one again either", gone)
        check("the card waiting is the new one", quotes.offered(user, CHAIN).quote_id == second.quote_id)
        check("another chain's card is left alone",
              quotes.offered(user, other_chain).quote_id == elsewhere.quote_id
              and signing.is_live(signing.find_by_quote(user, other_chain, elsewhere.quote_id)))

        # Once the new card is dealt with, nothing comes back.
        signing.cancel(new.request)
        check("once it is dealt with, no older card comes back", quotes.offered(user, CHAIN) is None)
    finally:
        quotes.drop_all(user, CHAIN)
        quotes.drop_all(user, other_chain)


def test_is_yes():
    print("\n[7] a typed reply that only agrees is a yes, however many agreeing words it has")
    for typed in ("yes", "Yes.", "  ok!  ", "send it", "Yes, confirm it.", "yes please", "OK, go ahead!", "sure, do it"):
        check(f"{typed!r} is a yes", signing.is_yes(typed))
    for typed in ("no", "yes but 2 ETH", "send it to bob", "confirm the swap?", "don't send it", ""):
        check(f"{typed!r} is not", not signing.is_yes(typed))


if __name__ == "__main__":
    try:
        test_packing()
        test_op_deadline_nonce()
        test_registration()
        test_managing()
        test_telegram_link()
        test_new_card_voids_older()
        test_is_yes()
    finally:
        os.unlink(_tmp_db.name)

    finish("All passkey checks passed.")
