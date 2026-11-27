"""
The contact list: the only destinations the assistant may pay, written only by the owner.
"""
import logging
import time

from ens.exceptions import InvalidName
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from web3.logs import DISCARD

import auth
import ens_names
from auth import get_current_user
from db import (
    add_transaction,
    delete_contact,
    get_json,
    get_all_contacts,
    get_contact,
    save_contact,
)
from redact import redact
from routes.deps import (
    CONFIRM_POLL_TIMEOUT_SECS,
    _failed_tx_detail,
    _load_wallet_for_chain,
    _require_owner,
    _resolve_chain,
    _tx_seen,
    limiter,
)
from routes.owner import TxConfirmRequest, _prepare_owner_tx

# The wallet's spending-limit module, where an owner voids their contact passes.
LIMIT_MODULE_ABI_PATH = "./out/SpendingLimitModule.sol/SpendingLimitModule.json"

router = APIRouter()
logger = logging.getLogger(__name__)


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


class SignedContactRequest(BaseModel):
    """
    Body of POST /api/contacts: the message of POST /api/contacts/prepare's typed data, as signed, and
    the owner's signature over it.
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[^/\\\x00-\x1f]+$")
    payee: str
    issuedAt: int = Field(ge=0, lt=2**64)
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


@router.get("/api/contacts/resolve")
@limiter.limit("30/minute")
def resolve_contact_name(
    request: Request,
    name: str = Query(min_length=1, max_length=255),
    user_id: int = Depends(get_current_user),
):
    """
    The address an ENS name (sam.eth) points to now, so a contact can be added by its name.

    A lookup only: it saves nothing and issues no nonce. The address then goes through the usual
    prepare -> sign -> save, so the owner signs the address itself and the contact keeps it even if
    the name is later pointed elsewhere (see ens_names).

    @param name  The ENS name as typed.
    @return      {"name": the name as ENS normalises it, "address": the checksummed address}
    """
    try:
        normalized = ens_names.normalize(name)
    except InvalidName:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{name}' isn't a valid ENS name")
    try:
        address = ens_names.resolve(normalized)
    except Exception as e:
        # The exception can name the RPC's URL, which carries the provider's key: only the log gets
        # it, masked.
        logger.warning("Cannot resolve the ENS name %s: %s", normalized, redact(e))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Couldn't look up {normalized} right now"
        )
    if address is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{normalized} doesn't point to an address")
    return {"name": normalized, "address": address}


@router.post("/api/contacts/prepare")
@limiter.limit("30/minute")
def prepare_contact(request: Request, req: ContactRequest, user_id: int = Depends(get_current_user)):
    """
    Checks a contact and returns the EIP-712 typed data the owner wallet signs to save it.

    Checked first, so a name or address that would be refused fails before the wallet is asked to
    sign anything. The typed data carries the name and address exactly as they will be stored, dated
    now: the wallet accepts the signature on every chain, until the owner voids their passes there.

    @return  {"domain", "types", "primaryType", "message"}, for viem's signTypedData as it is.
    """
    # An account with no owner could never produce the signature: refuse before asking for one.
    _require_owner(user_id)
    name, address = _normalise_contact(req)
    return auth.contact_typed_data(name, address, int(time.time()))


@router.post("/api/contacts", status_code=status.HTTP_201_CREATED)
def create_contact(req: SignedContactRequest, user_id: int = Depends(get_current_user)):
    """
    Saves or updates a contact for the signed-in account, given the owner's signature over it.
    See the note above on why this is here, and why it takes a signature and not just a session.

    The signature is kept: Mitfah attaches it to every op that pays this contact, and the wallet pays
    no one without one (PayeeGuard). Saving an existing name again (to re-sign it, or to give it a new
    address) replaces the old signature.

    @param req  The typed data's message as signed (name, payee, issuedAt), and the owner wallet's
                EIP-712 signature over it.
    @return     The stored contact, with the address in checksummed form.
    """
    owner = _require_owner(user_id)
    name, address = _normalise_contact(ContactRequest(name=req.name, address=req.payee))
    signature = auth.verify_contact_signature(owner, name, address, req.issuedAt, req.signature)
    save_contact(user_id, name, address, req.issuedAt, signature)
    return {"name": name, "address": address, "signed": True, "issued_at": req.issuedAt}


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


# ── Cancelling old contact signatures ─────────────────────────────────────────
#
# A signature can't be taken back, and deleting a contact here only stops Mitfah from attaching it: a
# copy of it (in a backup of wallet.db, say, or a hacked server) still pays that address. So the owner
# can void, on one network's wallet, every pass issued until now (SpendingLimitModule.voidContactPasses),
# then sign again the contacts to keep -- free, and good on every network. The void is one transaction
# from the owner's own wallet, per network.


class VoidContactsRequest(BaseModel):
    """Body of POST /api/contacts/void/prepare."""

    chain_id: int


def _limit_module(w3: Web3, wallet):
    """The wallet's SpendingLimitModule, or a 400 for a wallet whose module has no contact passes."""
    module = w3.eth.contract(
        address=wallet.functions.SH_MODULE().call(), abi=get_json(LIMIT_MODULE_ABI_PATH)["abi"]
    )
    try:
        module.functions.contactsVoidedAt(wallet.address).call()
    except Exception:  # noqa: BLE001 -- a module from before contact passes has no such getter
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This wallet doesn't check contacts on chain yet.")
    return module


@router.post("/api/contacts/void/prepare")
def prepare_void_contacts(req: VoidContactsRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned voidContactPasses transaction for the owner's wallet on `chain_id`: from when it
    mines, that wallet pays no contact whose signature is older. Re-sign the contacts to keep after.

    @return  {"tx", "action": "void_contacts", "voided_at"}: `voided_at` is the wallet's current void
             time (0 if never), so the page can show what changes.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    module = _limit_module(w3, wallet)
    fn = module.functions.voidContactPasses(wallet.address)
    voided_at = module.functions.contactsVoidedAt(wallet.address).call()
    return {**_prepare_owner_tx(w3, owner, fn, req.chain_id), "action": "void_contacts", "voided_at": voided_at}


@router.post("/api/contacts/void/confirm")
def confirm_void_contacts(req: TxConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for a void to mine and records it in the History tab. Only a transaction to this wallet's
    module that voided THIS wallet's passes is accepted. Answers 202 while pending, like
    /api/wallet/tx/confirm.

    @return  {"status": "confirmed", "tx_hash", "voided_at"}: contacts signed at or before `voided_at`
             need signing again before this wallet pays them.
    """
    _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    module = _limit_module(w3, wallet)

    if not _tx_seen(w3, req.tx_hash):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": False}
    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": True}

    if receipt["to"] is None or w3.to_checksum_address(receipt["to"]) != module.address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transaction {req.tx_hash} was not sent to your wallet's spending-limit module on chain {req.chain_id}.",
        )
    if receipt["status"] != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _failed_tx_detail(w3, req.tx_hash, receipt, "That transaction"))
    events = module.events.ContactPassesVoided().process_receipt(receipt, errors=DISCARD)
    ours = [e["args"] for e in events if e["args"]["account"] == wallet.address]
    if not ours:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Transaction {req.tx_hash} didn't void this wallet's contacts.")

    mined_at = w3.eth.get_block(receipt["blockNumber"])["timestamp"]
    add_transaction(
        user_id, req.chain_id, wallet.address, "owner", "Cancel every contact signature made before now",
        "confirmed", tx_hash=req.tx_hash if req.tx_hash.startswith("0x") else f"0x{req.tx_hash}",
        mined_at=mined_at, direction="none",
    )
    return {"status": "confirmed", "tx_hash": req.tx_hash, "voided_at": ours[0]["voidedAt"]}
