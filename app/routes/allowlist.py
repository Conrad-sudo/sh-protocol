"""
The wallet's contract allowlist: the owner's changes to it, and what is on it.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

from auth import get_current_user
from constants import (
    CHAIN_ID_BSC,
    get_always_counted_ticker,
    get_default_spenders,
    get_exchange,
    get_native_asset_ticker,
    get_router,
    get_v3_network,
)
from db import (
    get_all_contacts,
    get_custom_tokens,
    get_dashboard_tokens,
    get_supported_tokens_by_chain_id,
    get_wallet_address,
)
from parallel import read_all
from routes.deps import (
    _load_wallet_for_chain,
    _require_owner,
    _resolve_chain,
)
from routes.owner import _prepare_owner_tx
from routes.wallet import _known_pools

router = APIRouter()


#
# The wallet can confine its session key to a list of contracts (THREAT_MODEL §3.13), off by
# default. While it is on, a call to an address with no code -- a contact's plain wallet -- still
# goes through, and a call to any contract not on the list is refused. The list lives on chain
# only: nothing here writes wallet.db, so /api/wallet/tx/confirm confirms these like any other
# owner change.
class AllowlistRequest(BaseModel):
    """Body of POST /api/wallet/allowlist/prepare."""

    chain_id: int
    # enable:  list `targets` and turn the list on, in ONE transaction. `targets` may be empty when
    #          the list already has entries.
    # add:     list `targets`.
    # remove:  unlist the one address in `targets` -- the contract removes one at a time.
    # disable: turn the list off. Its entries stay, for turning it back on later.
    action: str = Field(pattern="^(enable|add|remove|disable)$")
    # Bounded so one transaction stays a sensible size: each new entry is two storage writes.
    targets: list[str] = Field(default_factory=list, max_length=64)


def _allowlist_targets(raw: list[str]) -> list[str]:
    """
    Checksums `raw` and drops repeats, keeping the order. Refuses anything that isn't an address,
    and the zero address, before any RPC: the contract would refuse either later, less helpfully.
    """
    targets: list[str] = []
    for value in raw:
        try:
            target = Web3.to_checksum_address(value)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {value}")
        if int(target, 16) == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The zero address can't be listed.")
        if target not in targets:
            targets.append(target)
    return targets


@router.post("/api/wallet/allowlist/prepare")
def prepare_allowlist(req: AllowlistRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned transaction for one change to the wallet's contract allowlist:
    `enableAllowList(targets)`, `addAllowedTargets(targets)`, `removeAllowedTarget(target)` or
    `toggleAllowList(false)`.

    Turning the list on and removing an entry TIGHTEN what the assistant can reach; adding an entry
    while the list is on, and turning it off, loosen it -- treat those as such in the UI. The
    contract refuses to turn on an empty list, which the simulation reports before anything is signed.

    @return  {"tx", "targets"}: the addresses as the transaction names them, checksummed.
    """
    owner = _require_owner(user_id)
    targets = _allowlist_targets(req.targets)
    # Before _resolve_chain, like the address checks: a request that can only be refused should not
    # cost an RPC round trip.
    if req.action == "add" and not targets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name at least one address to add.")
    if req.action == "remove" and len(targets) != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Remove one address at a time.")

    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    if req.action in ("enable", "add") and wallet.address in targets:
        # Harmless on chain -- the guard blocks the wallet's own address whatever the list says --
        # but the list would then name a contract the assistant can never actually call.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That's this wallet's own address. The assistant can never call it, so it can't be listed.",
        )
    if req.action == "enable":
        fn = wallet.functions.enableAllowList(targets)
    elif req.action == "add":
        fn = wallet.functions.addAllowedTargets(targets)
    elif req.action == "remove":
        fn = wallet.functions.removeAllowedTarget(targets[0])
    else:
        fn = wallet.functions.toggleAllowList(False)
    return {**_prepare_owner_tx(w3, owner, fn, req.chain_id), "targets": targets}


def _exchange_labels(chain_id: int) -> dict[str, str]:
    """
    The exchange contracts on `chain_id` Mitfah can name, by checksummed address: Uniswap V3's
    router and position manager where the chain trades on V3, and the V2 router wherever there is
    one -- BSC's exchange, and on a V3 chain the router wallets used before the move. RPC-free.
    """
    labels = {}
    v3 = get_v3_network(chain_id)
    if v3 is not None:
        labels[v3["swap_router"]] = "Uniswap V3 router"
        labels[v3["position_manager"]] = "Uniswap V3 positions"
    try:
        labels[get_router(chain_id)] = "PancakeSwap V2 router" if chain_id == CHAIN_ID_BSC else "Uniswap V2 router"
    except ValueError:
        pass  # no V2 deployment here (a bare Anvil)
    return labels


def _listed_targets(wallet) -> list[str] | None:
    """
    The wallet's allowlist entries, or None for a wallet created before the list could be read
    back: its implementation has no getAllowedTargets, so the call reverts in the account's fallback.
    A failure to reach the node is not that, and is raised.
    """
    try:
        return [Web3.to_checksum_address(a) for a in wallet.functions.getAllowedTargets().call()]
    except ContractLogicError:
        return None


@router.get("/api/wallet/{chain_id}/allowlist")
def get_allowlist(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The wallet's contract allowlist, each entry named where Mitfah knows it, and the contracts worth
    listing that aren't yet. Read only when Controls → Advanced is opened, so the dashboard's wallet
    read doesn't pay for it.

    `suggested` is what the assistant calls for this account, in this order: the exchange's
    contracts (Uniswap V3's router and position manager on a V3 chain, PancakeSwap's V2 router on
    BSC), the tokens on the dashboard (the wrapped native one included, then the ones added by
    address), on BSC the pools it deposited into (taking liquidity out calls the pool's own LP
    token), the ERC-8004 review registry, and any contact whose address holds code. A contact with a
    plain wallet needs no entry -- while the list is on, an address with no code is never blocked.

    On a V3 chain the V2 router and the V2 pools a wallet used before the move are still named, for
    a list that holds them, but not suggested: the assistant no longer trades through them there.

    Not gated on _require_owner, like the wallet read: reading costs nothing.

    @return  {"enabled", "targets": [{"address", "label"}] | null, "suggested": [{"address", "label"}]}.
             `label` is null for an address Mitfah has no name for. `targets` is null for a wallet
             created before its list could be read back: its on/off state still is.
    @raises HTTPException 404 if this account has no wallet on `chain_id`.
    """
    # The wallet-row lookup first, as in get_wallet_state: no wallet here costs no RPC.
    try:
        get_wallet_address(user_id, chain_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"You have no wallet on chain {chain_id}.")

    # The database next, on this thread (see parallel.py): every name, and the candidates in the
    # order they are suggested.
    named: dict[str, str] = {}
    candidates: list[str] = []

    def name(address: str, label: str, suggest: bool):
        address = Web3.to_checksum_address(address)
        named.setdefault(address, label)
        if suggest and address not in candidates:
            candidates.append(address)

    # The exchange's contracts first. Only the chain's default spenders -- what the assistant trades
    # through -- are suggested.
    trades_through = get_default_spenders(chain_id)
    for address, label in _exchange_labels(chain_id).items():
        name(address, label, address in trades_through)
    try:
        listed_tokens = get_supported_tokens_by_chain_id(chain_id)
    except ValueError:
        listed_tokens = []
    always = get_always_counted_ticker(chain_id)
    on_dashboard = {t["ticker"] for t in get_dashboard_tokens(user_id, chain_id)} | {always}
    for token in listed_tokens:
        name(token["address"], token["ticker"].upper(), token["ticker"] in on_dashboard)
    for token in get_custom_tokens(user_id, chain_id):
        name(token["address"], token["ticker"].upper(), True)
    contacts = get_all_contacts(user_id)

    w3, _ = _resolve_chain(chain_id)
    # Named like the pool's dashboard row, the native asset's side first: "ETH/USDC pool". Suggested
    # only where the assistant still takes V2 liquidity out; a V3 chain's liquidity is positions,
    # which only the position manager touches.
    v2_liquidity = get_exchange(chain_id) != "uniswap-v3"
    native = get_native_asset_ticker(chain_id).lower()
    for pool in _known_pools(w3, user_id, chain_id):
        sides = (pool["ticker0"], pool["ticker1"])
        if pool["ticker1"] == native:
            sides = sides[::-1]
        name(pool["pair"], "/".join(side.upper() for side in sides) + " pool", v2_liquidity)
    wallet = _load_wallet_for_chain(w3, user_id, chain_id)
    reads = read_all({
        "enabled": wallet.functions.sessionAllowlistEnabled().call,
        "targets": lambda: _listed_targets(wallet),
        "registry": wallet.functions.REPUTATION_REGISTRY().call,
        **{
            f"code:{contact['address']}": (lambda address=contact["address"]: w3.eth.get_code(address))
            for contact in contacts
        },
    })

    # Every network's config names one; the guard keeps a zero address -- which can't be listed --
    # out of the suggestions should one ever not.
    if int(reads["registry"], 16):
        name(reads["registry"], "ERC-8004 review registry", True)
    for contact in contacts:
        # Only a contract needs an entry; a plain wallet is never blocked.
        name(contact["address"], f"{contact['name']} (contact)", len(reads[f"code:{contact['address']}"]) > 0)

    targets = reads["targets"]
    listed = set(targets or [])
    return {
        "enabled": reads["enabled"],
        "targets": None if targets is None else [{"address": a, "label": named.get(a)} for a in targets],
        "suggested": [{"address": a, "label": named[a]} for a in candidates if a not in listed],
    }
