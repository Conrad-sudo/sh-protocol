"""
The account's passkeys: making one on a device (WebAuthn registration), naming, listing and
forgetting them.

A passkey made here is not yet trusted by any wallet. Approving it on a wallet -- each chain's
separately -- is an owner transaction (POST /api/wallet/session/prepare), and only then can it
confirm the assistant's transactions there. So registering checks only what WebAuthn can: that the
browser made a new ES256 credential for this site, verified the user (Face ID, a fingerprint or a
PIN), and answered the challenge this server issued to this account.
"""
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Response, status
from hexbytes import HexBytes
from pydantic import BaseModel, Field
from webauthn import generate_registration_options, verify_registration_response
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.options_to_json_dict import options_to_json_dict
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

import passkey
from auth import get_current_user
from db import (
    consume_passkey_challenge,
    delete_passkey,
    get_json,
    get_or_create_webauthn_handle,
    get_passkey,
    get_passkeys,
    get_user_by_id,
    get_wallet_address,
    get_wallet_chains,
    rename_passkey,
    save_passkey,
    save_passkey_challenge,
)
from network_config import load_network_config_by_name, network_name
from parallel import read_all
from redact import redact
from routes.deps import logger
from userop import read_sessions

router = APIRouter()

# How long a registration challenge stays good: the browser's own timeout, plus room for a slow
# user to find their device.
REGISTRATION_CHALLENGE_TTL_SECS = 5 * 60

# The relying party's name, as the device shows it when asking to make or use a passkey.
RP_NAME = "Mitfah"

class RegisterRequest(BaseModel):
    """Body of POST /api/passkeys/register."""

    # The browser's RegistrationResponseJSON, as navigator.credentials.create() returned it.
    credential: dict
    # What the user calls the device, shown beside the key on the dashboard ("iPhone").
    device_name: str = Field(min_length=1, max_length=40)


class RenameRequest(BaseModel):
    """Body of PATCH /api/passkeys/{key_id}."""

    device_name: str = Field(min_length=1, max_length=40)


def _shown(row: dict) -> dict:
    """A passkey as the list and the rename show it: never its public key or counter."""
    return {
        "key_id": row["key_id"],
        "credential_id": row["credential_id"],
        "device_name": row["device_name"],
        "created_at": row["created_at"],
    }


def _device_name(name: str) -> str:
    """A device name as stored: its outer spaces dropped, refused when nothing is left."""
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give the device a name.")
    return name


@router.post("/api/passkeys/register/options")
def register_options(user_id: int = Depends(get_current_user)):
    """
    The options for navigator.credentials.create(): a passkey for this site, for this account.

    ES256 only (the one curve the wallet can verify on every chain), user verification required (the
    wallet requires it on every approval, so a key that can't do it would be useless), attestation
    "none" (nothing here depends on which make of authenticator it is), and the account's existing
    passkeys excluded, so one device doesn't make a second. The challenge is single use and lives
    in the database, so a restart between options and response loses nothing.

    @return  {"options": PublicKeyCredentialCreationOptionsJSON}.
    """
    user = get_user_by_id(user_id)
    owner = user["owner_addr"] or f"account-{user_id}"
    challenge = secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=passkey.rp_id(),
        rp_name=RP_NAME,
        user_id=get_or_create_webauthn_handle(user_id),
        user_name=owner,
        user_display_name=f"Mitfah {owner[:6]}…{owner[-4:]}" if owner.startswith("0x") else f"Mitfah {owner}",
        challenge=challenge,
        timeout=passkey.WEBAUTHN_TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
            # Not sent: residentKey says it, and WebAuthn wants requireResidentKey only for "required".
            require_resident_key=None,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id"])) for row in get_passkeys(user_id)
        ],
        supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256],
    )
    save_passkey_challenge(user_id, bytes_to_base64url(challenge), int(time.time()) + REGISTRATION_CHALLENGE_TTL_SECS)
    return {"options": options_to_json_dict(options)}


@router.post("/api/passkeys/register")
def register(req: RegisterRequest, user_id: int = Depends(get_current_user)):
    """
    Files the passkey a browser just made, once py_webauthn has verified it.

    The challenge it answers must be one this server issued to THIS account and that is unused and
    unexpired; it is used up here whatever happens next. The credential must be ES256 with user
    verification, made for this site's rp id from one of its origins. The public key's (x, y) is
    what the wallet will verify approvals against, and key_id -- keccak256(abi.encode(qx, qy)) --
    is the wallet's name for it.

    @return  {"key_id", "credential_id", "qx", "qy", "device_name", "created_at"}.
    @raises HTTPException 400 if the response doesn't verify, answers no challenge of this account's,
            or the key isn't ES256; 409 if the credential (or its key) is already registered.
    """
    device_name = _device_name(req.device_name)
    try:
        credential = parse_registration_credential_json(req.credential)
        challenge = parse_client_data_json(credential.response.client_data_json).challenge
    except (WebAuthnException, ValueError, TypeError, KeyError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The browser's answer couldn't be read.")
    if not consume_passkey_challenge(user_id, bytes_to_base64url(challenge)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This passkey request has expired. Try again.")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=passkey.rp_id(),
            expected_origin=passkey.allowed_origins(),
            require_user_verification=True,
            supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256],
        )
    except WebAuthnException as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"That passkey couldn't be verified: {e}")
    try:
        qx, qy = passkey.es256_public_key(verified.credential_public_key)
    except passkey.PasskeyError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    try:
        row = save_passkey(
            user_id,
            passkey.key_id(qx, qy),
            bytes_to_base64url(verified.credential_id),
            verified.credential_public_key,
            "0x" + qx.hex(),
            "0x" + qy.hex(),
            device_name,
            verified.sign_count,
        )
    except ValueError:
        raise HTTPException(status.HTTP_409_CONFLICT, "That passkey is already registered.")
    return {**_shown(row), "qx": row["qx"], "qy": row["qy"]}


@router.get("/api/passkeys")
def list_passkeys(user_id: int = Depends(get_current_user)):
    """
    The account's passkeys, oldest first. Which wallet each is approved on comes from the wallet
    read (GET /api/wallet/{chain_id}, session.keys), off the chain.

    @return  {"passkeys": [{"key_id", "credential_id", "device_name", "created_at"}]}.
    """
    return {"passkeys": [_shown(row) for row in get_passkeys(user_id)]}


@router.patch("/api/passkeys/{key_id}")
def rename(key_id: str, req: RenameRequest, user_id: int = Depends(get_current_user)):
    """
    Renames one of the account's passkeys.

    @return  The passkey: {"key_id", "credential_id", "device_name", "created_at"}.
    @raises HTTPException 404 if the account has no such passkey.
    """
    row = rename_passkey(user_id, key_id, _device_name(req.device_name))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You have no such passkey.")
    return _shown(row)


def _live_on(user_id: int, key_id: str) -> list[int]:
    """
    The chains where one of the account's wallets approves `key_id` right now, read from each
    wallet (getSessions), all at once. A wallet row with no code behind it -- a fork restarted
    since -- approves nothing.

    @raises Exception  A chain that can't be read: the caller can't say the key is safe to forget.
    """
    abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]

    def approves(chain_id: int):
        def read() -> bool:
            w3, _ = load_network_config_by_name(network_name(chain_id))
            address = get_wallet_address(user_id, chain_id)
            if w3.eth.get_code(address) in (b"", HexBytes("0x")):
                return False
            now = int(time.time())
            sessions = read_sessions(w3.eth.contract(address=address, abi=abi))
            return any(s.key_id == key_id and s.active(now) for s in sessions)
        return read

    found = read_all({chain_id: approves(chain_id) for chain_id in get_wallet_chains(user_id)})
    return [chain_id for chain_id, live in found.items() if live]


@router.delete("/api/passkeys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove(key_id: str, user_id: int = Depends(get_current_user)):
    """
    Forgets one of the account's passkeys -- but not while any of the account's wallets still
    approves it: forgetting it here would leave a key the wallet trusts that the dashboard can no
    longer name, and whose approvals this server could no longer check. Revoke it on chain first
    (POST /api/wallet/session/prepare, action "remove").

    @raises HTTPException 404 if the account has no such passkey; 409 while it is live on a wallet;
            503 if a wallet couldn't be read, so it can't be told.
    """
    key_id = key_id.lower()
    if get_passkey(user_id, key_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You have no such passkey.")
    try:
        live = _live_on(user_id, key_id)
    except Exception as e:  # noqa: BLE001 -- an RPC or a wallet that can't be read
        logger.warning("Could not check where passkey %s is approved: %s", key_id, redact(e))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Couldn't check your wallets just now, so the passkey was kept. Try again in a minute.",
        )
    if live:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This passkey is still approved on your wallet. Remove it from the wallet first (Controls → Devices).",
        )
    delete_passkey(user_id, key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
