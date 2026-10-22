"""
Deploying a wallet: the unsigned deployWallet transaction the user signs, and its confirmation.
"""
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from hexbytes import HexBytes
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from web3.logs import DISCARD

import tx_history
from auth import get_current_user
from constants import (
    MAX_SESSION_TTL_SECS,
    get_always_counted_ticker,
    get_default_spenders,
)
from contracts import invalidate_cache
from db import (
    get_factory_address,
    get_json,
    get_passkey,
    get_token_address,
    get_wallet_address,
    get_wallet_chains,
    save_user_network,
    save_wallet_address,
)
from routes.deps import (
    CONFIRM_POLL_TIMEOUT_SECS,
    DEFAULT_SESSION_TTL_SECS,
    USD_DECIMALS,
    _failed_tx_detail,
    _require_owner,
    _resolve_chain,
    _revert_reason,
    _to_json_tx,
    _tx_seen,
)
from userop import read_sessions

router = APIRouter()


# Spending-window length used when the client sends none (24h — the cap refills each window).
DEFAULT_WINDOW_SECS = 86_400


# How far back /api/deploy/confirm looks for the block that created a wallet when the hash it was
# given never mined because the user's wallet replaced it. Searched by halving, so it costs about 20
# reads; finding nothing only leaves that deploy out of the History tab.
REPLACED_DEPLOY_LOOKBACK_BLOCKS = 1_000_000


class WatchedToken(BaseModel):
    """One row of GET /api/tokens, echoed back in the deploy body as the user's selection."""

    ticker: str
    address: str


class DeployRequest(BaseModel):
    """Body of POST /api/deploy — everything the front end collects on the deploy screen.

    Note what is NOT here: the user id. It comes from the caller's access token via
    get_current_user, never from the body — a body field would let any signed-in user deploy a
    wallet against somebody else's account.
    """

    # As reported by the user's wallet (eth_chainId). Resolved to a network name server-side by
    # _resolve_chain — see CHAIN_NAME_BY_ID for why the name is not accepted from the request.
    chain_id: int
    # The user's own EOA. They are msg.sender for deployWallet, so they own the wallet and its
    # CREATE2 salt comes from THEIR deployCount — nothing another user does moves the prediction.
    deployer: str
    # Whole US dollars, e.g. 50000. Scaled to 18 decimals server-side.
    daily_limit_usd: int = Field(gt=0)
    window_secs: int = Field(default=DEFAULT_WINDOW_SECS, gt=0)
    # The user's picks from GET /api/tokens. The ticker is what the server trusts — the address is
    # re-resolved from the same table the list came from, so a wallet can only be seeded with tokens
    # this deployment actually prices (deployWallet reverts TokenNotPriced otherwise, on the user's gas).
    watched_tokens: list[WatchedToken] = Field(default_factory=list)
    # ETH sent as deployWallet's msg.value so the new wallet can pay its own ERC-4337 prefund.
    # Decimal, not float: "0.15" has to convert to wei exactly.
    prefund_eth: Decimal = Field(default=Decimal("0"), ge=0)
    # The passkey made in onboarding, by its key id: the wallet approves it from the start, so the
    # assistant's first quote can be confirmed with no second transaction. Must be one of the
    # account's own passkeys (db.passkeys), which is where its (qx, qy) come from. None deploys an
    # owner-only wallet: no device can confirm anything until the owner approves one.
    key_id: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    # How long that approval lasts, in seconds. Optional so the deploy screen need not ask; checked
    # against MAX_SESSION_TTL here, which the contract enforces too.
    session_ttl_secs: int = Field(default=DEFAULT_SESSION_TTL_SECS, gt=0)


class ConfirmRequest(BaseModel):
    """Body of POST /api/deploy/confirm — sent once the user's wallet returns a transaction hash.

    As with DeployRequest, the user id comes from the token rather than the body.
    """

    chain_id: int
    deployer: str
    tx_hash: str
    # The address /api/deploy predicted. Echoed back so a deploy the user's wallet replaced (sped it
    # up, say) can still be found there, mined under a hash nobody told the app.
    predicted_address: str


def _load_factory_for_chain(w3: Web3, chain_id: int):
    """
    Binds SHFactory on `chain_id` WITHOUT touching the user's saved network.

    contracts.load_factory resolves its chain through user_network, so using it here would mean
    calling save_user_network() before the user has signed anything — repointing their bot session
    at a chain they may end up with no wallet on if they close the tab. The network is saved in
    /api/deploy/confirm instead, once the wallet actually exists.

    Checks the address actually holds code. The `factory` table can outlive the chain it describes —
    a restarted anvil is the everyday case — and calling a bare address raises BadFunctionCallOutput
    ("is contract deployed correctly and chain synced?") from deep inside web3, which reaches the
    client as a 500 with a stack trace and no hint that the fix is to redeploy.
    """
    abi = get_json("./out/SHFactory.sol/SHFactory.json")["abi"]
    address = get_factory_address(chain_id)
    if w3.eth.get_code(address) in (b"", HexBytes("0x")):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"No SHFactory code at {address} on chain {chain_id}. The protocol is not deployed on "
            f"this chain, or the `factory` table is stale — redeploy and re-run `make db`.",
        )
    return w3.eth.contract(address=address, abi=abi)


def _is_wallet_of(w3: Web3, address: str, owner: str) -> bool:
    """Whether `address` holds a SessionHandler that `owner` owns."""
    if w3.eth.get_code(address) in (b"", HexBytes("0x")):
        return False
    abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    try:
        return w3.eth.contract(address=address, abi=abi).functions.owner().call() == owner
    except Exception:  # noqa: BLE001 -- code that is no wallet of ours
        return False


def _find_deploy_tx(w3: Web3, factory, wallet_address: str):
    """
    The receipt of the transaction that created `wallet_address`, or None. Best effort.

    Providers cap how many blocks one log search may cover (Alchemy's free tier: 10), so rather than
    search a range for the WalletDeployed log, this halves the last REPLACED_DEPLOY_LOOKBACK_BLOCKS
    down to the block the wallet's code first appears in, then reads that one block's logs. Reading
    old state needs an archive node; without one this finds nothing.
    """
    def has_code(block: int) -> bool:
        return w3.eth.get_code(wallet_address, block_identifier=block) not in (b"", HexBytes("0x"))

    try:
        latest = w3.eth.block_number
        before, created = max(0, latest - REPLACED_DEPLOY_LOOKBACK_BLOCKS), latest
        if has_code(before) or not has_code(created):
            return None
        while created - before > 1:
            middle = (before + created) // 2
            if has_code(middle):
                created = middle
            else:
                before = middle
        logs = factory.events.WalletDeployed().get_logs(
            argument_filters={"walletAddress": wallet_address}, from_block=created, to_block=created
        )
        return w3.eth.get_transaction_receipt(logs[0]["transactionHash"]) if logs else None
    except Exception:  # noqa: BLE001 -- the History tab goes without the row
        return None


def _require_own_deployer(user_id: int, deployer: str):
    """
    Refuses a deploy unless the deploying EOA is the one this account signs in as (via SIWE).

    Without this the deployer is just an address in the request body, and two things go wrong.
    A caller can name somebody else's EOA and have this server build transactions for a wallet they
    will never own; and, given another user's deploy transaction, they can call /api/deploy/confirm
    for it and register that wallet against their own account. The stolen row could not SPEND --
    every transaction needs a passkey the real owner approved -- but their agent would happily read
    the victim's balances through it.

    The UI never sends anything else: it deploys from the wallet the user signed in with.

    @param user_id   The authenticated account.
    @param deployer  The checksummed EOA the request wants to deploy from.
    @raises HTTPException 403 if the account has no address, or a different one.
    """
    owner_addr = _require_owner(user_id)
    if owner_addr != deployer:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"You're signed in as {owner_addr}, not {deployer}. Switch to that account in your "
            "wallet.",
        )


@router.post("/api/deploy")
def deploy_wallet(req: DeployRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the UNSIGNED SHFactory.deployWallet transaction for the user to sign in their own wallet.

    Nothing is broadcast here and no key of the app's signs anything — that is what keeps the wallet
    non-custodial. The response carries the transaction, the address it will produce and the passkey
    it approves. The front end passes the transaction to MetaMask and posts the resulting hash to
    /api/deploy/confirm, which is where anything durable is written.

    The passkey the user made in onboarding (`key_id`) goes into the deploy itself: deployWallet
    approves its (qx, qy) inside initialize(), until now + session_ttl_secs, so the wallet can take
    a passkey-confirmed transaction from its first block. Without one the wallet is owner-only.

    @param req  Deploy parameters collected by the front end.
    @return     {"chain_id", "predicted_address", "key_id", "watched_tokens", "tx"}; key_id is null
                for an owner-only wallet.
    @raises HTTPException 400 for a key_id that isn't one of the account's passkeys, or an approval
            longer than MAX_SESSION_TTL.
    """
    # Authorize BEFORE resolving the chain: _resolve_chain dials an RPC, and a caller who may not
    # deploy from this address should not be able to make the server do work or learn which chains
    # it is configured for.
    try:
        deployer = Web3.to_checksum_address(req.deployer)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.deployer}")

    _require_own_deployer(user_id, deployer)

    # The account's own passkey, before any RPC: a request naming another's costs no work.
    key_id = req.key_id.lower() if req.key_id else None
    passkey_row = get_passkey(user_id, key_id) if key_id else None
    if key_id and passkey_row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That isn't one of your passkeys.")
    if key_id and req.session_ttl_secs > MAX_SESSION_TTL_SECS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A passkey may be approved for at most {MAX_SESSION_TTL_SECS // 86_400} days.",
        )

    # chain_name is only needed to reach the RPC; nothing in this endpoint writes it, so it is
    # dropped here and re-resolved in confirm, where it is actually persisted.
    w3, _ = _resolve_chain(req.chain_id)
    chain_id = req.chain_id

    # Tickers -> addresses against this chain's token table. The client's address is checked against
    # the table rather than used: a stale picker (or a tampered body) must not seed a wallet with a
    # token this deployment cannot price. An unknown ticker is the user's error to see now.
    watched_tokens = []
    for token in req.watched_tokens:
        try:
            address = w3.to_checksum_address(get_token_address(chain_id, token.ticker))
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        if token.address and w3.to_checksum_address(token.address) != address:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{token.ticker} is {address} on chain {chain_id}, not {token.address} — "
                "reload the token list.",
            )
        watched_tokens.append(address)
    # The wrapped native token is always counted, like the native asset it wraps -- whatever the
    # picker sent. Added here rather than trusted to the client, so no wallet this API deploys can
    # leave WETH/WBNB outside the limit (the owner could still remove it on chain themselves).
    always = get_always_counted_ticker(chain_id)
    if always is not None:
        try:
            always_address = w3.to_checksum_address(get_token_address(chain_id, always))
        except ValueError:
            always_address = None   # not in this chain's table (an unseeded anvil): nothing to add
        if always_address is not None and always_address not in watched_tokens:
            watched_tokens.insert(0, always_address)

    try:
        factory = _load_factory_for_chain(w3, chain_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    # A user is EXPECTED to hold one wallet per chain, so a wallet elsewhere says nothing. Only one
    # on THIS chain matters: session_handlers is keyed (user_id, chain_id), so confirming a second
    # deploy replaces that row and the old wallet keeps its prefund with nothing pointing at it.
    try:
        existing = get_wallet_address(user_id, chain_id)
        print(
            f"WARNING: user {user_id} already has wallet {existing} on chain {chain_id}. "
            f"Deploying a second one on this chain — the old wallet's funds stay there and become "
            f"unreachable from the app. Withdraw first if that is not what you want. (Wallets on "
            f"other chains are unaffected.)"
        )
    except ValueError:
        others = [c for c in get_wallet_chains(user_id) if c != chain_id]
        if others:
            print(f"user {user_id} has wallets on chains {others}; adding {chain_id}.")

    predicted = factory.functions.predictWalletAddress(deployer).call()
    # Both zero for an owner-only wallet, whose deadline initialize() then ignores.
    if passkey_row is not None:
        session_key_x = bytes.fromhex(passkey_row["qx"][2:])
        session_key_y = bytes.fromhex(passkey_row["qy"][2:])
        session_key_valid_until = int(time.time()) + req.session_ttl_secs
    else:
        session_key_x = session_key_y = b"\x00" * 32
        session_key_valid_until = 0

    # The exchange contracts the assistant approves tokens to: a wallet-level choice, not protocol
    # config. Sourced from constants.get_default_spenders, which reads the same network tables the
    # assistant's toolkits are bound to, so the contracts the wallet trusts are the ones it trades
    # through. A chain with no exchange (a bare Anvil) has none.
    trusted_spenders = get_default_spenders(chain_id)
    if not trusted_spenders:
        print(f"No exchange on chain {chain_id}; deploying with no trusted spenders")

    value = w3.to_wei(req.prefund_eth, "ether")

    # build_transaction runs eth_estimateGas from `deployer` with this value attached, so an
    # underfunded EOA fails there as an opaque node error. Check first and say what is short.
    balance = w3.eth.get_balance(deployer)
    if balance < value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{deployer} holds {w3.from_wei(balance, 'ether')} ETH but the prefund alone needs "
            f"{req.prefund_eth} ETH, before gas.",
        )

    try:
        tx = factory.functions.deployWallet(
            req.daily_limit_usd * USD_DECIMALS,
            req.window_secs,
            watched_tokens,
            session_key_x,
            session_key_y,
            session_key_valid_until,
            trusted_spenders,
        ).build_transaction(
            {
                "value": value,
                "from": deployer,
                # The user's own wallet signs this, so unlike the bot's deploy path — where
                # tx_sender.send_and_confirm hands out nonces under a lock and overwrites whatever
                # is here — it has to carry the real one.
                "nonce": w3.eth.get_transaction_count(deployer),
                "chainId": chain_id,
            }
        )
    except Exception as e:
        # Almost always a reverting estimateGas: the factory's module unset, a watched token the
        # oracle cannot price, or a paused factory.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Could not build deployWallet transaction: {_revert_reason(e)}"
        )

    return {
        "chain_id": chain_id,
        "predicted_address": predicted,
        "key_id": key_id,
        "watched_tokens": watched_tokens,
        "tx": _to_json_tx(tx),
    }


@router.post("/api/deploy/confirm")
def confirm_deploy(req: ConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Records the wallet once the user's signed deployWallet transaction has mined.

    This is the half that writes: the wallet address and the user's network. Splitting it from
    /api/deploy is what stops an abandoned signature prompt from leaving a row for a wallet that does
    not exist, and what lets the app recover the address from the receipt rather than trusting the
    prediction. The passkey the deploy approved is read back off the wallet, not taken on trust.

    Answers 202 while the transaction is still pending, with `seen` as in /api/wallet/tx/confirm;
    the front end polls until it gets a 200.

    A hash the node has never seen still finishes the deploy when the wallet is already at the
    predicted address. The user's wallet replaced the transaction (sped it up, say) and it mined
    under a hash nobody told the app. Answered "not seen", the page would offer to start over, and a
    second deploy makes a second wallet.

    @param req  The chain, the user's EOA, the transaction hash and the predicted address.
    @return     {"status", "chain_id", "wallet_address", "key_id", "session_key_authorized",
                "session_key_expires_at"}.
    """
    # Authorized first, for the same reason as /api/deploy: no RPC work for a caller who may not
    # act on this address. Checked here as well as there because the two calls are independent
    # requests — without it, someone could skip straight to confirm with another user's transaction
    # hash and register that wallet against their own account.
    try:
        deployer = Web3.to_checksum_address(req.deployer)
        predicted = Web3.to_checksum_address(req.predicted_address)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a valid address")

    _require_own_deployer(user_id, deployer)

    # chain_name IS needed here: save_user_network stores it, and load_network_config, contracts.py
    # and tools.py all read it back to find the user's chain.
    w3, chain_name = _resolve_chain(req.chain_id)
    chain_id = req.chain_id

    # Answered at once, without the receipt wait: a hash this node has never seen may never arrive.
    if not _tx_seen(w3, req.tx_hash):
        # The predicted address is where this owner's deploy lands, so a wallet of theirs there is
        # this deploy, mined under another hash. Only theirs: the owner check keeps anyone else's
        # wallet out, and the deployer is this account's own SIWE-proved address.
        if not _is_wallet_of(w3, predicted, deployer):
            response.status_code = status.HTTP_202_ACCEPTED
            return {"status": "pending", "tx_hash": req.tx_hash, "seen": False}
        replacement = _find_deploy_tx(w3, _load_factory_for_chain(w3, chain_id), predicted)
        if replacement is not None:
            tx_history.record_deploy(
                w3, user_id, chain_id, predicted, replacement["transactionHash"], replacement
            )
        return _finish_deploy(w3, user_id, chain_id, chain_name, predicted, predicted)
    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": True}

    if receipt["status"] != 1:
        # Listed as failed (it cost gas) -- but only when it is this user's own transaction.
        if Web3.to_checksum_address(receipt["from"]) == deployer:
            tx_history.record_deploy(w3, user_id, chain_id, predicted, req.tx_hash, receipt)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            _failed_tx_detail(w3, req.tx_hash, receipt, "deployWallet", ", or watch fewer tokens"),
        )

    factory = _load_factory_for_chain(w3, chain_id)
    # process_receipt already drops logs from other contracts; filtering on owner too means a hash
    # belonging to somebody else's deploy cannot register their wallet against this user_id.
    logs = [
        log
        for log in factory.events.WalletDeployed().process_receipt(receipt, errors=DISCARD)
        if log["args"]["owner"] == deployer
    ]
    if not logs:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No WalletDeployed event for {deployer} in tx {req.tx_hash} — wrong transaction?",
        )
    wallet_address = logs[0]["args"]["walletAddress"]
    # Recorded as soon as the deploy is proved, before anything below can refuse: it is on chain.
    tx_history.record_deploy(w3, user_id, chain_id, wallet_address, req.tx_hash, receipt)
    return _finish_deploy(w3, user_id, chain_id, chain_name, predicted, wallet_address)


def _finish_deploy(
    w3: Web3, user_id: int, chain_id: int, chain_name: str, predicted: str, wallet_address: str
) -> dict:
    """
    Files a wallet that is on chain as the user's wallet on `chain_id`: the second half of
    /api/deploy/confirm.

    Nothing about the passkey is filed: the server holds no session key, and the wallet itself is
    the record of which passkeys it approves. Its one approval from the deploy is read back off the
    chain and reported.

    @param predicted       The address /api/deploy predicted.
    @param wallet_address  Where the wallet actually is: the prediction, unless another deploy by
                           this same owner landed in between (their deployCount moved).
    @return                The confirm's 200 body.
    """
    if wallet_address != predicted:
        print(f"Predicted {predicted} but the deploy landed at {wallet_address}.")

    handler_abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    wallet_contract = w3.eth.contract(address=wallet_address, abi=handler_abi)
    sessions = read_sessions(wallet_contract)
    # A deploy approves at most one passkey (InitConfig carries one), so the wallet holds that one
    # or none. Active is the wallet's own test: approved, and its deadline not yet passed.
    approved = sessions[0] if sessions else None
    authorized = approved is not None and approved.active(int(time.time()))

    # Saved even when no passkey is approved: the wallet exists and holds the prefund, so losing
    # the reference is worse than recording an owner-only wallet. The owner approves a device in
    # Controls → Devices.
    save_user_network(user_id, chain_name)
    save_wallet_address(user_id, chain_id, wallet_address)
    invalidate_cache(user_id)

    return {
        "status": "deployed",
        "chain_id": chain_id,
        "wallet_address": wallet_address,
        "key_id": approved.key_id if approved else None,
        "session_key_authorized": authorized,
        "session_key_expires_at": approved.valid_until if approved else 0,
    }
