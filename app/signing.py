"""
Approving a quote with a passkey: the sign request behind every card, from the moment the card is
shown to the moment the approval is checked.

  1. offer       A turn's quote is shown on a card -- in the web chat, or in Telegram -- and gets a
                 sign request: the quote frozen in the database (db.sign_requests). Frozen because
                 the Telegram bot and the API are separate processes: the bot raises the quote, and
                 the approval page posts to the API, which must sign and send what the bot quoted.
                 A Telegram card also gets a link token, stored only as its hash.
  2. start       The approval page opens (POST /api/sign/start): the final UserOperation is built
                 now -- fresh fees, a fresh nonce key carrying the op's deadline
                 (bundler.finalize_user_op) -- and its hash is the WebAuthn challenge. Each start replaces the last build, under a new sign_id.
  3. approve     The passkey's assertion comes back (POST /api/sign/finish): py_webauthn checks it
                 against the build's challenge, this site's rp id and origins, and user
                 verification; it is packed into the signature the wallet reads (passkey.py); and
                 the request is claimed, once. Sending it is tools.sending's (submit_signed).
  4. cancel      Either side can drop a request that is still open.

Nothing here talks HTTP or runs the agent: the routes (routes/sign.py) and the agent
(smart_wallet_agent.confirm_signed) do. What this module guarantees is what the quote store
guaranteed before passkeys (THREAT_MODEL 4.2): only a quote the code wrote can be approved (a sign
request is made from a PendingTransaction, never from anything the model or the browser says),
each is sent at most once (claim_sign_request is one conditional UPDATE), and each expires with
its quote.
"""
import dataclasses
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass

from webauthn import verify_authentication_response
from webauthn.helpers import bytes_to_base64url, parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException

import quotes
from bundler import SIGNATURE_BUDGET, FinalUserOp, UserOpQuote, finalize_user_op, resolve_bundler
from constants import SESSION_EXPIRY_MARGIN_SECS, get_chain_display_name
from contracts import load_entry_point, load_session_handler
from db import (
    acting_network,
    cancel_sign_request,
    claim_sign_request,
    get_open_sign_requests,
    get_passkey_by_credential,
    get_passkeys,
    get_sign_request_by_quote,
    get_sign_request_by_sign_id,
    get_sign_request_by_token,
    open_sign_request,
    save_sign_build,
    set_sign_request_token,
    update_passkey_sign_count,
)
from db.sign_requests import OPEN
from network_config import network_name
from parallel import start_read
from passkey import (
    WEBAUTHN_TIMEOUT_MS,
    PasskeyError,
    allowed_origins,
    contract_signature,
    rp_id,
    verify_like_contract,
)
from userop import OP_DEADLINE_GRACE_SECS, read_sessions

# Where a quote was offered, and so where its outcome goes.
WEB, TELEGRAM = "web", "telegram"

# The answers the spec fixes, word for word.
EXPIRED = "This confirmation has expired. Ask again."
NO_PASSKEY = "No passkey on this device is approved for this wallet."
# A typed "yes" while a card is waiting: answered without the model (see is_yes).
YES_REPLY = "Tap Confirm and approve with your passkey."

# A typed reply that only agrees (spec 6). While a card is waiting, it gets YES_REPLY and the card
# again -- never a model call, which could do nothing more: no tool can send. One or more agreeing
# words in a row count ("Yes, confirm it.", "ok send it please"); any other word makes it not a yes.
_AGREE = r"(?:yes|y|yeah|yep|ok|okay|sure|please|confirm(?: it)?|send(?: it)?|go(?: ahead)?|do it)"
_YES = re.compile(rf"^{_AGREE}(?:[\s,.!]+{_AGREE})*[.!\s]*$", re.IGNORECASE)


class Expired(Exception):
    """The request is unknown, expired, already used, cancelled, or rebuilt since."""


class NoPasskey(Exception):
    """None of the user's passkeys is approved on the wallet the request is for."""


class Rejected(Exception):
    """The passkey's approval failed a check. Its text says which, for the user."""


class WouldFail(Exception):
    """The transaction would fail on chain. Its text names the reason."""


def is_yes(text: str) -> bool:
    """Whether a typed message only agrees ("yes", "ok", "send it", ...), ignoring outer space."""
    return bool(_YES.match(text.strip()))


def token_hash(token: str) -> str:
    """SHA-256 of a Telegram link token: all the database keeps of it."""
    return hashlib.sha256(token.encode()).hexdigest()


def freeze(pending: quotes.PendingTransaction) -> str:
    """
    A quote as its sign request keeps it: everything the approval page shows and the send needs --
    the action, the executions, the cost, a deposit's pool, its direction, the slippage and limit
    figures its card shows, and the priced op (bundler.UserOpQuote) whose calldata and gas limits
    the approved op carries.
    """
    return json.dumps({
        "action": pending.action,
        "calls": pending.calls,
        "cost": pending.cost,
        "lp_pool": pending.lp_pool,
        "direction": pending.direction,
        "slippage_bps": pending.slippage_bps,
        "charged_usd": pending.charged_usd,
        "quote": dataclasses.asdict(pending.quote),
    })


def thaw(request: dict) -> dict:
    """A sign request's frozen quote, with its UserOpQuote rebuilt."""
    frozen = json.loads(request["quote"])
    frozen["quote"] = UserOpQuote(**frozen["quote"])
    return frozen


def is_live(request: dict | None) -> bool:
    """Whether a request can still be approved: open, and not past its quote's expiry."""
    return request is not None and request["status"] == OPEN and request["expires_at"] >= int(time.time())


# ── Offering ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Offer:
    """A quote on a card, waiting for a passkey's approval."""

    pending: quotes.PendingTransaction
    request: dict        # its sign request (db.sign_requests row)
    token: str | None    # a Telegram card's link token, new with this offer; None on the web
    # The sign requests of the user's older cards on the chain that this one voided (offer), so a
    # Telegram caller can take their buttons off.
    superseded: tuple[dict, ...] = ()


def open_request(pending: quotes.PendingTransaction, origin: str, telegram_chat_id: int | None = None) -> Offer | None:
    """
    The sign request for a quote being put on a card: made the first time, the same one after. A
    Telegram card gets a fresh link token every time it is shown, so an older card's link dies.

    @return  The offer, or None if the quote's request is no longer open (dealt with elsewhere).
    """
    request = open_sign_request(
        pending.quote_id, pending.user_id, pending.chain_id, origin, freeze(pending), pending.expires_at,
        telegram_chat_id=telegram_chat_id,
    )
    if not is_live(request):
        return None
    token = None
    if request["origin"] == TELEGRAM:
        token = secrets.token_urlsafe(24)
        if not set_sign_request_token(request["id"], token_hash(token)):
            return None
    return Offer(pending, request, token)


def offer(user_id: int, chain_id: int, turn_id: int | None, origin: str,
          telegram_chat_id: int | None = None) -> Offer | None:
    """
    The card a turn ends with: the quote it offers (quotes.offer), with its sign request.

    One card at a time per chain: every other quote the user has waiting there is voided, its sign
    request cancelled, so its link and its card can no longer approve it. Otherwise an older quote
    comes back on the card once this one is dealt with. 2026-10-22: a request asked again was quoted
    afresh without confirm_transaction (so quotes.repeats never voided the first quote); once the
    new card was sent, the web chat showed the first one again, and approving it would have paid
    twice.

    @return  None when the turn offers no quote.
    """
    pending = quotes.offer(user_id, chain_id, turn_id)
    offered = open_request(pending, origin, telegram_chat_id) if pending is not None else None
    if offered is None:
        return None
    quotes.drop_others(user_id, chain_id, keep=pending.quote_id)
    superseded = tuple(
        r for r in get_open_sign_requests(user_id, chain_id)
        if r["quote_id"] != pending.quote_id and cancel_sign_request(r["id"])
    )
    return dataclasses.replace(offered, superseded=superseded)


def show_again(request: dict) -> str | None:
    """
    A fresh link token for an open Telegram request whose card is being shown again (a typed
    "yes"): the old link dies with it. None if the request is no longer open.
    """
    token = secrets.token_urlsafe(24)
    return token if set_sign_request_token(request["id"], token_hash(token)) else None


def waiting(user_id: int, chain_id: int, origin: str | None = None) -> dict | None:
    """The user's newest request on `chain_id` still waiting for an approval, or None."""
    open_requests = get_open_sign_requests(user_id, chain_id, origin)
    return open_requests[0] if open_requests else None


# ── Finding a request ─────────────────────────────────────────────────────────


def find_by_token(token: str) -> dict:
    """
    The open request a Telegram link's token names.

    @raises Expired  If the token names none, or one that can no longer be approved.
    """
    request = get_sign_request_by_token(token_hash(token))
    if not is_live(request):
        raise Expired()
    return request


def find_by_quote(user_id: int, chain_id: int, quote_id: str) -> dict:
    """
    The open request of one of the user's quotes, as the web chat's card names it.

    @raises Expired  If there is none, or it can no longer be approved.
    """
    request = get_sign_request_by_quote(user_id, chain_id, quote_id)
    if not is_live(request):
        raise Expired()
    return request


def find_by_sign_id(sign_id: str) -> dict:
    """
    The open request whose latest build is `sign_id`. An older build's id names nothing.

    @raises Expired  If there is none, or it can no longer be approved.
    """
    request = get_sign_request_by_sign_id(sign_id)
    if not is_live(request):
        raise Expired()
    return request


# ── Starting: the op the passkey will sign ───────────────────────────────────


@dataclass(frozen=True)
class Started:
    """What the approval page needs to ask the passkey: the challenge, and which keys may answer."""

    sign_id: str
    challenge: bytes
    allow_credentials: list[str]   # base64url credential ids
    expires_at: int
    summary: dict


def _op_json(final: FinalUserOp) -> str:
    """A built op as the request keeps it: every field of the PackedUserOperation, bytes as hex."""
    sender, nonce, init_code, call_data, gas_limits, pvg, gas_fees, paymaster, _ = final.op
    return json.dumps({
        "sender": sender, "nonce": nonce, "init_code": init_code.hex(), "call_data": call_data.hex(),
        "account_gas_limits": gas_limits.hex(), "pre_verification_gas": pvg, "gas_fees": gas_fees.hex(),
        "paymaster_and_data": paymaster.hex(), "max_fee": final.max_fee, "tip": final.tip,
        "user_op_hash": final.user_op_hash.hex(),
    })


def built_op(request: dict) -> FinalUserOp:
    """The op a request's latest start built, as finalize_user_op returned it."""
    op = json.loads(request["op"])
    return FinalUserOp(
        op=(
            op["sender"], op["nonce"], bytes.fromhex(op["init_code"]), bytes.fromhex(op["call_data"]),
            bytes.fromhex(op["account_gas_limits"]), op["pre_verification_gas"], bytes.fromhex(op["gas_fees"]),
            bytes.fromhex(op["paymaster_and_data"]), b"",
        ),
        user_op_hash=bytes.fromhex(op["user_op_hash"]),
        max_fee=op["max_fee"],
        tip=op["tip"],
    )


def summary(request: dict, frozen: dict) -> dict:
    """
    What the approval page shows above its button: written from the frozen quote, never by the
    model. `cost` carries the web chat card's own fields (QuoteOffer), so one view can show both.
    """
    chain_id = request["chain_id"]
    return {
        "action": frozen["action"],
        "network": get_chain_display_name(chain_id),
        "chain_id": chain_id,
        "cost": {
            "quote_id": request["quote_id"],
            "chain_id": chain_id,
            "action": frozen["action"],
            "fees": quotes.fee_text(frozen["cost"]),
            "expires_at": request["expires_at"],
        },
    }


def start(request: dict) -> Started:
    """
    Builds the op an open request's passkey will sign, replacing any earlier build.

    The op is built NOW rather than at offer time: its fees must be current when it is signed, and
    a card may sit for minutes. Its nonce key is fresh (userop.passkey_nonce), so two approvals in
    flight -- two cards, or two devices -- never queue behind or clash with each other, and it
    carries the op's own deadline: the request's expiry plus OP_DEADLINE_GRACE_SECS for the send.
    An approval that is never sent dies then, not when the passkey's approval on the wallet does.

    The passkeys that may answer are the user's own (db.passkeys) that the wallet approves and that
    have at least SESSION_EXPIRY_MARGIN_SECS left, read from the wallet itself (getSessions).

    @raises Expired     If the request can no longer be approved, or its chain isn't served now.
    @raises NoPasskey   If none of the user's passkeys is approved on the wallet.
    @raises WouldFail   If the op can't be priced: the base fee is past what the wallet will pay.
    """
    user_id, chain_id = request["user_id"], request["chain_id"]
    try:
        network = network_name(chain_id)
    except ValueError as exc:
        raise Expired() from exc
    frozen = thaw(request)
    with acting_network(user_id, network, chain_id):
        wallet = load_session_handler(user_id)
        entry_point, bundler = load_entry_point(user_id), resolve_bundler()
        sessions = start_read(lambda: read_sessions(wallet))
        try:
            final = finalize_user_op(
                user_id, wallet, entry_point, frozen["quote"], bundler,
                request["expires_at"] + OP_DEADLINE_GRACE_SECS,
            )
        except RuntimeError as exc:
            raise WouldFail(str(exc)) from exc
        now = int(time.time())
        live = {s.key_id for s in sessions.result() if s.active(now, SESSION_EXPIRY_MARGIN_SECS)}
    allowed = [row["credential_id"] for row in get_passkeys(user_id) if row["key_id"] in live]
    if not allowed:
        raise NoPasskey()
    sign_id = secrets.token_urlsafe(16)
    if not save_sign_build(request["id"], sign_id, _op_json(final), "0x" + final.user_op_hash.hex()):
        raise Expired()
    return Started(sign_id, final.user_op_hash, allowed, request["expires_at"], summary(request, frozen))


def start_options(started: Started) -> dict:
    """POST /api/sign/start's answer (spec 5), from a start."""
    return {
        "sign_id": started.sign_id,
        "challenge": bytes_to_base64url(started.challenge),
        "rp_id": rp_id(),
        "allow_credentials": [{"type": "public-key", "id": credential} for credential in started.allow_credentials],
        "user_verification": "required",
        "timeout_ms": WEBAUTHN_TIMEOUT_MS,
        "expires_at": started.expires_at,
        "summary": started.summary,
    }


# ── Approving ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Approved:
    """A request whose approval checked out, claimed for sending: everything the send needs."""

    request: dict
    frozen: dict            # thaw(request)
    final: FinalUserOp      # the op the passkey signed
    signature: bytes        # its signature, packed as the wallet reads it
    key_id: str             # the passkey that signed


def approve(request: dict, credential: dict) -> Approved:
    """
    Checks a passkey's approval of a request's latest build, packs it, and claims the request.

    The assertion is checked by py_webauthn before anything else, against what THIS server built
    and expects: the challenge it stored, this site's rp id and origins, a passkey registered to
    the request's own user, its counter, and user verification -- a tap without Face ID or a PIN
    is refused, as the wallet refuses it too. Only then is the request claimed, so a failed check
    leaves it open for another try, and two approvals racing claim it once (claim_sign_request).

    The approval is packed for the wallet (passkey.contract_signature) and checked the way the
    contract will check it (passkey.verify_like_contract) -- a cheap guard that names a packing
    fault as one instead of as an AA24 later. An approval longer than the op's preVerificationGas
    was sized for (bundler.SIGNATURE_BUDGET) is refused: sending it short would have the bundler pay
    the difference, which a client could make as large as it liked.

    @param request     The request, found by its sign_id (find_by_sign_id).
    @param credential  The browser's AuthenticationResponseJSON.
    @raises Rejected   If the approval fails a check. The request stays open.
    @raises Expired    If the request was built again, used or cancelled meanwhile.
    """
    if not request.get("op") or not request.get("challenge") or not request.get("sign_id"):
        raise Expired()
    user_id = request["user_id"]
    challenge = bytes.fromhex(request["challenge"][2:])
    try:
        parsed = parse_authentication_credential_json(credential)
    except (WebAuthnException, ValueError, TypeError, KeyError) as exc:
        raise Rejected("The passkey's answer couldn't be read.") from exc
    passkey_row = get_passkey_by_credential(user_id, bytes_to_base64url(parsed.raw_id))
    if passkey_row is None:
        raise Rejected("That passkey isn't one this account registered.")
    try:
        verified = verify_authentication_response(
            credential=parsed,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=allowed_origins(),
            credential_public_key=passkey_row["public_key"],
            credential_current_sign_count=passkey_row["sign_count"],
            require_user_verification=True,
        )
    except WebAuthnException as exc:
        raise Rejected(f"The passkey check failed: {exc}") from exc
    update_passkey_sign_count(passkey_row["key_id"], verified.new_sign_count)

    response = parsed.response
    try:
        signature = contract_signature(
            bytes.fromhex(passkey_row["qx"][2:]),
            bytes.fromhex(passkey_row["qy"][2:]),
            challenge,
            bytes(response.authenticator_data),
            bytes(response.client_data_json),
            bytes(response.signature),
        )
    except PasskeyError as exc:
        raise Rejected(str(exc)) from exc
    if len(signature) > SIGNATURE_BUDGET:
        raise Rejected("That approval is larger than the wallet accepts. Try again from Mitfah's own page.")
    if not verify_like_contract(challenge, signature):
        raise Rejected("The wallet couldn't check that approval. Try again.")

    if not claim_sign_request(request["id"], request["sign_id"]):
        raise Expired()
    return Approved(request, thaw(request), built_op(request), signature, passkey_row["key_id"])


# ── Cancelling ───────────────────────────────────────────────────────────────


def cancel(request: dict) -> bool:
    """Cancels an open request, and drops its quote if this process holds it. True if it was open."""
    if not cancel_sign_request(request["id"]):
        return False
    quotes.drop(request["user_id"], request["quote_id"])
    return True
