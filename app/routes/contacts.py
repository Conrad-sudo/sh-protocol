"""
The contact list: the only destinations the assistant may pay, written only by the owner.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from web3 import Web3

import auth
from auth import get_current_user
from db import (
    delete_contact,
    get_all_contacts,
    get_contact,
    save_contact,
)
from routes.deps import _require_owner, limiter

router = APIRouter()


#
# Writing the contact list is an OWNER action, and lives here rather than in the agent's toolset
# for one reason: the contact list is the allowlist of destinations for the wallet's funds.
# tools._resolve_contact accepts a saved name and refuses a raw address, so every value-moving
# tool -- send_eth, transfer_erc20, transferFrom_erc20, every swap with a recipient -- can only
# pay somebody the list already names. A `save_contact` tool would have handed whoever holds the
# chat surface the ability to name themselves, and the cap would then be the ONLY thing between
# them and the balance.
#
# The case that motivates it is an unlocked stolen phone: the thief inherits the Telegram session
# and can talk to the agent, but adding a payee takes the owner wallet they do not have. Since
# 2026-10-01 not even a web session is enough: every new or changed contact carries the owner's
# EIP-712 signature over its exact name and address (auth.contact_typed_data), so a session token
# lifted by an XSS bug, or a browser left signed in, adds no payee either.
# The residual is stated plainly: they can still move up to the remaining cap to contacts the
# owner already saved, which is worth little to a thief. Revoking the session key
# (POST /api/wallet/session/prepare) is the response to a lost device.
#
# DELETE lives here too, and not because deleting could steal anything -- it only ever shrinks the
# allowlist. It is here so the rule stays a single sentence: the agent READS the contact list and
# never writes it. "May write, but only destructively" is the kind of distinction that gets
# re-derived wrong later. Reads stay on the agent; they create no new destination.
class ContactRequest(BaseModel):
    """Body of POST /api/contacts/prepare."""

    # Names index a lowercase column and are what the user types at the agent, so they are kept
    # short and free of the characters that would make them awkward to name back ("/" would also
    # collide with the delete route's path parameter).
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^/\\\x00-\x1f]+$")
    address: str


class SignedContactRequest(ContactRequest):
    """Body of POST /api/contacts: the contact, and the owner's signature over its typed data."""

    nonce: str = Field(max_length=64)
    signature: str = Field(max_length=200)


def _normalise_contact(req: ContactRequest) -> tuple[str, str]:
    """
    The contact's name and address as they are signed and stored, or a 422 saying what is wrong.

    The address is checksummed rather than trusted as typed: it is stored once and then read back
    as a transaction destination for as long as the contact exists, so a typo caught here is a
    transaction that never gets built, while one stored raw surfaces much later as an opaque
    failure deep in the calldata builder -- or, if it happens to be valid, as funds sent somewhere
    real.
    """
    name = req.name.strip().lower()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name cannot be blank")
    # "me" is reserved: tools._resolve_contact short-circuits it to the wallet's own address
    # before it ever reaches the contact table, so a contact saved under that name would be
    # silently unreachable -- the user would see it listed and never be able to pay it.
    if name == "me":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "'me' is reserved — it always refers to your own wallet",
        )
    # A browser rewrites /api/contacts/. and /api/contacts/.. (even spelled %2E) before sending
    # the request, so a contact under either name could never be deleted from the web app.
    if name in (".", ".."):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a name can't be just dots")
    try:
        address = Web3.to_checksum_address(req.address)
    except (ValueError, TypeError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{req.address}' is not an Ethereum address"
        )
    return name, address


@router.post("/api/contacts/prepare")
@limiter.limit("30/minute")
def prepare_contact(request: Request, req: ContactRequest, user_id: int = Depends(get_current_user)):
    """
    Checks a contact and returns the EIP-712 typed data the owner wallet signs to save it.

    Checked first, so a name or address that would be refused fails before the wallet is asked to
    sign anything. The typed data carries the name and address exactly as they will be stored, and a
    nonce issued for this account and this contact alone.

    @return  {"domain", "types", "primaryType", "message"}, for viem's signTypedData as it is.
    """
    # An account with no owner could never produce the signature: refuse before issuing a nonce.
    _require_owner(user_id)
    name, address = _normalise_contact(req)
    return auth.contact_typed_data(name, address, auth.issue_contact_nonce(user_id, name, address))


@router.post("/api/contacts", status_code=status.HTTP_201_CREATED)
def create_contact(req: SignedContactRequest, user_id: int = Depends(get_current_user)):
    """
    Saves or updates a contact for the signed-in account, given the owner's signature over it.
    See the note above on why this is here, and why it takes a signature and not just a session.

    @param req  The contact, the nonce from POST /api/contacts/prepare, and the owner wallet's
                EIP-712 signature over that typed data.
    @return     The stored contact, with the address in checksummed form.
    """
    owner = _require_owner(user_id)
    name, address = _normalise_contact(req)
    auth.verify_contact_signature(user_id, owner, name, address, req.nonce, req.signature)
    save_contact(user_id, name, address)
    return {"name": name, "address": address}


@router.get("/api/contacts")
def list_contacts(user_id: int = Depends(get_current_user)):
    """Returns the signed-in account's contacts, sorted by name."""
    return {"contacts": get_all_contacts(user_id)}


@router.delete("/api/contacts/{name:path}")
def remove_contact(name: str, user_id: int = Depends(get_current_user)):
    """
    Deletes one of the signed-in account's contacts.

    `{name:path}` rather than a plain path parameter so that a name stored before the write route
    existed -- which validated nothing -- is still deletable whatever characters it contains.

    @param name  The contact name. Case-insensitive.
    """
    name = name.strip().lower()
    if not name or get_contact(user_id, name) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{name}' is not a saved contact")
    delete_contact(user_id, name)
    return {"status": "deleted", "name": name}
