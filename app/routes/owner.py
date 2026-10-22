"""
Owner actions: each prepares an unsigned transaction the owner signs in the browser, simulated first,
and confirms it once mined.
"""
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from langchain_erc20 import ERC20_ABI
from langchain_erc20.amounts import to_base_units
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

import tx_history
from auth import get_current_user
from constants import (
    ETH_SENTINEL,
    get_always_counted_ticker,
    get_default_spenders,
    get_native_asset_ticker,
)
from constants import MAX_SESSION_KEYS
from contracts import invalidate_cache, read_spending_config
from db import (
    get_passkey,
    get_token_address,
    resolve_token,
)
from routes.deps import (
    CONFIRM_POLL_TIMEOUT_SECS,
    DEFAULT_SESSION_TTL_SECS,
    USD_DECIMALS,
    _failed_tx_detail,
    _load_wallet_for_chain,
    _require_owner,
    _resolve_chain,
    _revert_reason,
    _to_json_tx,
    _tx_seen,
)
from userop import read_sessions, session_block

router = APIRouter()


#
# All three are `onlyOwner` on SessionHandler, and the owner is the USER's own EOA -- deployWallet
# set `owner = msg.sender`. The backend holds no key that can call them, which is exactly what makes
# this wallet non-custodial, so these endpoints cannot "do" the action. They PREPARE an unsigned
# transaction the user signs in their browser, then confirm the result.
#
# The value the API adds is the simulation. Each of these reverts for reasons a user cannot see from
# the UI -- withdrawing more than the balance, watching a token the oracle cannot price, pausing an
# already-paused wallet -- and an eth_call catches that before they pay for a failing transaction.
#
# These must be sent DIRECTLY to the wallet by the owner, never routed through execute(): the
# module's admin-surface guard rejects execute-routed admin calls, the owner included.
class PauseRequest(BaseModel):
    """Body of the pause/unpause prepare endpoints."""

    chain_id: int


class WithdrawRequest(BaseModel):
    """Body of POST /api/wallet/withdraw/prepare."""

    chain_id: int
    # A listed ticker, a ticker the user added, a token's contract address, or "eth"/the chain's
    # native ticker to withdraw native value.
    token: str
    # Whole units (e.g. "1.5"), scaled to base units server-side. Decimal, not float, so a value
    # like "0.1" converts exactly.
    amount: Decimal = Field(gt=0)
    # Where the funds go. A plain address: the owner is signing this themselves in their own wallet,
    # so unlike the agent's tools there is no injection risk to defend against here.
    to: str


class WatchedTokenRequest(BaseModel):
    """Body of POST /api/wallet/watched-tokens/prepare."""

    chain_id: int
    token: str
    action: str = Field(pattern="^(add|remove)$")


class TxConfirmRequest(BaseModel):
    """Body of POST /api/wallet/tx/confirm."""

    chain_id: int
    tx_hash: str


def _prepare_owner_tx(w3: Web3, owner: str, fn, chain_id: int) -> dict:
    """
    Simulates an owner call, then builds it as an unsigned transaction for the browser to sign.

    The eth_call runs first and is the whole point: it surfaces the contract's own revert reason
    (NotEnoughBalance, TokenNotPriced, EnforcedPause, …) as a 400 the UI can show, instead of letting
    the user discover it by paying for a reverting transaction. build_transaction would estimate gas
    and fail anyway, but with a far less useful message.

    @param w3        A Web3 bound to `chain_id`.
    @param owner     The EOA that will sign, used as `from` for both the call and the transaction.
    @param fn        A bound web3 contract function, ready to call.
    @param chain_id  The chain, stamped into the transaction so it cannot be replayed elsewhere.
    @return          {"tx": <hex-encoded unsigned tx>}.
    @raises HTTPException 400 with the revert reason if the simulation fails.
    """
    try:
        fn.call({"from": owner})
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"This transaction would fail: {_revert_reason(e)}"
        )

    try:
        tx = fn.build_transaction({
            "from": owner,
            # The user's own wallet signs, so unlike the bot's path (where tx_sender hands out
            # nonces under a lock) the real nonce has to be here.
            "nonce": w3.eth.get_transaction_count(owner),
            "chainId": chain_id,
        })
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not build the transaction: {_revert_reason(e)}")
    return {"tx": _to_json_tx(tx)}


def _resolve_withdraw_token(w3: Web3, user_id: int, chain_id: int, token: str) -> tuple[str, int]:
    """
    Resolves a withdraw token -- a listed ticker, one the user added, or a contract address -- to
    (token address, decimals).

    Native value is `address(0)` with 18 decimals — the same sentinel SessionHandler.withdraw,
    SHOracle and the agent's tools all use for the chain's gas asset.

    Any address is accepted, not only listed or added ones: withdraw is the owner's escape hatch and
    the contract takes any token, so a token the owner never added must still be recoverable. The
    simulation in _prepare_owner_tx catches an address that isn't a token.

    @return  (address, decimals).
    @raises HTTPException 400 if the ticker is unknown or the address doesn't answer decimals().
    """
    # "eth" is this codebase's generic label for the native gas asset on every chain (see
    # ETH_SENTINEL, get_eth_balance, send_eth), so it is accepted everywhere; the chain's real
    # ticker is accepted too, for a UI that shows "BNB" or "CELO".
    if token.lower() in ("eth", get_native_asset_ticker(chain_id).lower()):
        return ETH_SENTINEL, 18
    try:
        address = w3.to_checksum_address(resolve_token(user_id, chain_id, token))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    try:
        decimals = w3.eth.contract(address=address, abi=ERC20_ABI).functions.decimals().call()
    except Exception:  # noqa: BLE001 -- no code, or not an ERC-20
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{address} doesn't answer like an ERC-20 token.")
    return address, decimals


@router.post("/api/wallet/pause/prepare")
def prepare_pause(req: PauseRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `pause()` transaction.

    Pausing stops validation, so a paused wallet rejects UserOps before they reach the EntryPoint and
    pays no gas for the refusal. It is the kill switch when something looks wrong with a device.

    @return  {"tx": …} for the browser to sign, then POST to /api/wallet/tx/confirm.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    return _prepare_owner_tx(w3, owner, wallet.functions.pause(), req.chain_id)


@router.post("/api/wallet/unpause/prepare")
def prepare_unpause(req: PauseRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `unpause()` transaction.

    @return  {"tx": …} for the browser to sign, then POST to /api/wallet/tx/confirm.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    return _prepare_owner_tx(w3, owner, wallet.functions.unpause(), req.chain_id)


@router.post("/api/wallet/withdraw/prepare")
def prepare_withdraw(req: WithdrawRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `withdraw(token, amount, to)` transaction.

    The owner's escape hatch: it moves value straight out of the wallet without touching the session
    key or the spending cap, which is what makes the cap a bound on the AGENT rather than on the
    user. Simulation catches the two ways it reverts — a zero recipient and an amount above the
    wallet's balance — before the user pays for either.

    @return  {"tx", "token_address", "amount_base_units"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)

    try:
        to = w3.to_checksum_address(req.to)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.to}")

    token_address, decimals = _resolve_withdraw_token(w3, user_id, req.chain_id, req.token)
    amount, _truncated = to_base_units(str(req.amount), decimals)
    if amount == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{req.amount} {req.token} rounds to zero at {decimals} decimals.",
        )

    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    prepared = _prepare_owner_tx(
        w3, owner, wallet.functions.withdraw(token_address, amount, to), req.chain_id
    )
    return {**prepared, "token_address": token_address, "amount_base_units": str(amount)}


@router.post("/api/wallet/watched-tokens/prepare")
def prepare_watched_token(req: WatchedTokenRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `addWatchedToken(token)` or `removeWatchedToken(token)` transaction.

    A watched token has its value movements metered against the USD cap; an unwatched ERC20 moves
    freely. So adding one TIGHTENS the agent's leash and removing one loosens it — removal is the
    direction to think twice about.

    Adding reverts if the oracle cannot price the token (no silent exemptions) or once the list holds
    32, both caught by the simulation. Adding a token that is already watched is a no-op on chain,
    not an error.

    Removing the wrapped native token (WETH, WBNB) is refused: Mitfah always counts it, like the
    native asset it wraps. As with the exchange router, the owner can still call removeWatchedToken
    on the wallet directly -- this stops an accidental removal from the app, not a deliberate one.

    @return  {"tx", "token_address"}.
    """
    owner = _require_owner(user_id)
    always = get_always_counted_ticker(req.chain_id)
    # Before _resolve_chain, like the router check: a request that can only be refused should not
    # cost an RPC round trip.
    if req.action == "remove" and always is not None and req.token.lower() == always:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{always.upper()} always counts toward your limit, like {get_native_asset_ticker(req.chain_id)}, "
            "so it can't be removed.",
        )
    w3, _ = _resolve_chain(req.chain_id)

    try:
        token_address = w3.to_checksum_address(get_token_address(req.chain_id, req.token))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    fn = (
        wallet.functions.addWatchedToken(token_address)
        if req.action == "add"
        else wallet.functions.removeWatchedToken(token_address)
    )
    return {**_prepare_owner_tx(w3, owner, fn, req.chain_id), "token_address": token_address}


@router.post("/api/wallet/tx/confirm")
def confirm_owner_tx(req: TxConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for an owner transaction to mine and reports how it went.

    One endpoint for all three actions because none of them writes to wallet.db — pause state,
    balances and the watched list all live on chain and are read back from there. Contrast
    /api/deploy/confirm, which exists precisely because it has a row to write.

    Answers 202 while the transaction is still pending; the front end polls until it gets a 200.
    The 202's `seen` is false while this node has never seen the hash (see _tx_seen).

    @return  {"status", "tx_hash", "wallet_state"} once mined.
    """
    _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    # Answered at once, without the receipt wait: a hash this node has never seen may never arrive.
    if not _tx_seen(w3, req.tx_hash):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": False}
    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        # In the History tab from the first poll, so closing the page before it mines loses nothing.
        tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash)
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": True}

    # Only accept a transaction that was actually sent TO this user's wallet, so one user cannot
    # report another's transaction hash and read state back through this endpoint.
    if receipt["to"] is None or w3.to_checksum_address(receipt["to"]) != wallet.address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transaction {req.tx_hash} was not sent to your wallet on chain {req.chain_id}.",
        )

    tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash, receipt)
    if receipt["status"] != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            _failed_tx_detail(w3, req.tx_hash, receipt, "That transaction"),
        )

    config = read_spending_config(wallet)
    return {
        "status": "confirmed",
        "tx_hash": req.tx_hash,
        "wallet_state": {
            "paused": wallet.functions.paused().call(),
            "watched_tokens": config["watchedTokens"],
            "daily_limit_usd": config["dailyLimitUsd"] / USD_DECIMALS,
            "remaining_usd": wallet.functions.getRemainingBudget().call() / USD_DECIMALS,
        },
    }


class DailyLimitRequest(BaseModel):
    """Body of POST /api/wallet/daily-limit/prepare."""

    chain_id: int
    # Whole US dollars, scaled to 18 decimals server-side. ge=0 because the contract allows 0, and
    # 0 is meaningful: it freezes agent spending without revoking the key or pausing the wallet.
    daily_limit_usd: int = Field(ge=0)


class WindowDurationRequest(BaseModel):
    """Body of POST /api/wallet/window-duration/prepare."""

    chain_id: int
    # Seconds. The contract rejects 0 and anything past the uint48 storage ceiling.
    window_secs: int = Field(gt=0, le=281_474_976_710_655)


class SessionKeyRequest(BaseModel):
    """Body of POST /api/wallet/session/prepare."""

    chain_id: int
    action: str = Field(pattern="^(add|remove|remove_all)$")
    # The passkey to approve, renew or remove: its key id, 0x and 32 bytes of hex. For `add` it must
    # be one of the account's own passkeys (db.passkeys), which is where (qx, qy) come from -- never
    # from the request. Not read for `remove_all`.
    key_id: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    # How long the approval should last, in seconds. Only read for `add`. Checked against the
    # wallet's own MAX_SESSION_TTL here, which the contract enforces too.
    ttl_secs: int = Field(default=DEFAULT_SESSION_TTL_SECS, gt=0)


class TrustedSpenderRequest(BaseModel):
    """Body of POST /api/wallet/trusted-spenders/prepare."""

    chain_id: int
    spender: str
    action: str = Field(pattern="^(add|remove)$")


class MaxOpGasCostRequest(BaseModel):
    """Body of POST /api/wallet/max-op-gas-cost/prepare."""

    chain_id: int
    # In ETH, converted to wei server-side. Decimal so "0.05" converts exactly.
    max_cost_eth: Decimal = Field(gt=0)


class SessionGasBudgetRequest(BaseModel):
    """Body of POST /api/wallet/session-gas-budget/prepare."""

    chain_id: int
    # In the chain's native coin, converted to wei server-side. Decimal so "0.02" converts exactly.
    budget_eth: Decimal = Field(gt=0)


@router.post("/api/wallet/daily-limit/prepare")
def prepare_daily_limit(req: DailyLimitRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `setDailyLimit(dailyLimitUsd)` transaction.

    The headline control: the USD the agent may spend per window, across every token and venue. Set
    at deploy and otherwise unchangeable until now.

    Lowering it below what is already spent in the current window does not claw anything back — it
    just means nothing more goes out until the window rolls over. Setting 0 freezes agent spending
    while leaving the key authorized and the wallet unpaused, which is the gentlest of the three
    brakes (the others being removeSession and pause).

    @return  {"tx", "daily_limit_usd_scaled"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    scaled = req.daily_limit_usd * USD_DECIMALS
    prepared = _prepare_owner_tx(w3, owner, wallet.functions.setDailyLimit(scaled), req.chain_id)
    return {**prepared, "daily_limit_usd_scaled": str(scaled)}


@router.post("/api/wallet/window-duration/prepare")
def prepare_window_duration(req: WindowDurationRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `setWindowDuration(windowDuration)` transaction.

    How long the spending window lasts before the cap refills. Changing it affects only when the
    window NEXT rolls over — it does not reset the current one, so shortening the window does not
    hand the agent a fresh allowance immediately.

    @return  {"tx"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    return _prepare_owner_tx(
        w3, owner, wallet.functions.setWindowDuration(req.window_secs), req.chain_id
    )


@router.post("/api/wallet/session/prepare")
def prepare_session_key(req: SessionKeyRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned owner transaction that approves one of the account's passkeys on this
    chain's wallet, renews it, or revokes passkeys: `addSession(qx, qy, validUntil)`,
    `removeSession(keyId)` or `removeAllSessions()`.

    A passkey is a device's: up to MAX_SESSION_KEYS of them can confirm the assistant's
    transactions on one wallet, each until its own deadline, and each chain's wallet approves them
    separately. Approving one the wallet already holds is a renewal -- the same key, a new deadline
    -- so `renews` tells the UI which it will be. The wallet sweeps keys that have run out to make
    room for a new one, so it is full only while MAX_SESSION_KEYS live, unexpired keys are held.

    `remove` is the precise kill switch for one device: a lost phone stops confirming anything the
    moment it mines, while the other devices -- and the owner's own access -- carry on. It takes any
    key the wallet holds, named or not, so the owner can drop one granted outside this app.
    `remove_all` revokes every one; `pause` stops even the owner's UserOps.

    Nothing about the passkey's private half ever reaches this server: the request names the key by
    its id, and (qx, qy) come from the account's own registration (db.passkeys).

    @return  add: {"tx", "key_id", "valid_until", "renews"}; remove: {"tx", "revokes": key_id};
             remove_all: {"tx", "revokes": [key_id, ...]}.
    @raises HTTPException 400 for a key that isn't one of this account's passkeys (add), a ttl past
            MAX_SESSION_TTL, a full wallet, a key the wallet doesn't hold (remove), a wallet with
            no keys (remove_all), or a transaction that would fail.
    """
    owner = _require_owner(user_id)
    key_id = req.key_id.lower() if req.key_id else None
    # The account's own passkey first: a request naming anybody else's key costs no RPC.
    if req.action == "add":
        if key_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name the passkey to approve (key_id).")
        passkey_row = get_passkey(user_id, key_id)
        if passkey_row is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That isn't one of your passkeys.")
    elif req.action == "remove" and key_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name the passkey to remove (key_id).")

    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    sessions = read_sessions(wallet)
    held = [s.key_id for s in sessions]

    if req.action == "remove":
        if key_id not in held:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Your wallet on chain {req.chain_id} doesn't approve that passkey."
            )
        prepared = _prepare_owner_tx(w3, owner, wallet.functions.removeSession(bytes.fromhex(key_id[2:])), req.chain_id)
        return {**prepared, "revokes": key_id}

    if req.action == "remove_all":
        if not held:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Your wallet on chain {req.chain_id} has no passkeys to remove."
            )
        prepared = _prepare_owner_tx(w3, owner, wallet.functions.removeAllSessions(), req.chain_id)
        return {**prepared, "revokes": held}

    max_ttl = wallet.functions.MAX_SESSION_TTL().call()
    if req.ttl_secs > max_ttl:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A passkey may be approved for at most {max_ttl} seconds ({max_ttl // 86_400} days); "
            f"{req.ttl_secs} was requested.",
        )
    now = int(time.time())
    renews = key_id in held
    live_others = [s for s in sessions if s.key_id != key_id and s.active(now)]
    if not renews and len(live_others) >= MAX_SESSION_KEYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Your wallet on chain {req.chain_id} already approves {MAX_SESSION_KEYS} passkeys. Remove one "
            "first (Controls → Devices).",
        )
    valid_until = now + req.ttl_secs
    fn = wallet.functions.addSession(
        bytes.fromhex(passkey_row["qx"][2:]), bytes.fromhex(passkey_row["qy"][2:]), valid_until
    )
    prepared = _prepare_owner_tx(w3, owner, fn, req.chain_id)
    return {**prepared, "key_id": key_id, "valid_until": valid_until, "renews": renews}


@router.post("/api/wallet/session/confirm")
def confirm_session_key(req: TxConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for a passkey approval, renewal or revocation to mine, then reports the wallet's keys.

    Separate from /api/wallet/tx/confirm because it answers with the session block the dashboard
    shows (GET /api/wallet/{chain_id}'s `session`), read back off the chain once it has mined: the
    chain is the record of which passkeys a wallet approves. Nothing is written locally -- there is
    no key to file, since the server holds none.

    Answers 202 while the transaction is still pending, with `seen` as in /api/wallet/tx/confirm;
    the front end polls until it gets a 200.

    @return  {"status": "confirmed", "session": {"max_keys", "max_ttl_secs",
             "renewal_warning_secs", "keys": [...]}} once mined.
    """
    _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    # As in /api/wallet/tx/confirm: an unseen hash is answered at once and says so.
    if not _tx_seen(w3, req.tx_hash):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": False}
    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash)
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": True}

    # Same guard as /api/wallet/tx/confirm: only a transaction sent TO this user's wallet counts, so
    # one user cannot report another's hash and read state back through this endpoint.
    if receipt["to"] is None or w3.to_checksum_address(receipt["to"]) != wallet.address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transaction {req.tx_hash} was not sent to your wallet on chain {req.chain_id}.",
        )
    tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash, receipt)
    if receipt["status"] != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            _failed_tx_detail(w3, req.tx_hash, receipt, "That transaction"),
        )

    invalidate_cache(user_id)
    return {"status": "confirmed", "session": session_block(user_id, read_sessions(wallet))}


@router.post("/api/wallet/trusted-spenders/prepare")
def prepare_trusted_spender(req: TrustedSpenderRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `addTrustedSpender(spender)` or `removeTrustedSpender(spender)` transaction.

    A trusted spender may be granted an approval for a token the oracle cannot price. That exemption
    exists so exchanges work with unpriced tokens; it is also a hole in the metering, so the list is
    capped at 16 and the deploy seeds only the chain's default spenders (the exchange contracts the
    assistant trades through, constants.get_default_spenders).

    Adding one is a genuine loosening of the spending controls — treat it as such in the UI.

    Removing any default spender is refused. The assistant approves them for tokens the oracle
    can't price -- a token the user added by address that it swaps or deposits, a V2 pool's LP token
    it takes out on BSC -- which only a trusted spender may receive, so without them those trades
    revert. The owner can still call removeTrustedSpender on the wallet directly — this stops an
    accidental removal from the app, not a deliberate one. A contract that is no longer a default
    (the V2 router, on a chain that moved to Uniswap V3) can be removed like any other.

    @return  {"tx", "spender"}.
    """
    owner = _require_owner(user_id)

    try:
        spender = Web3.to_checksum_address(req.spender)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.spender}")

    # Before _resolve_chain, like the owner check: a request that can only be refused should not
    # cost an RPC round trip.
    if req.action == "remove" and spender in get_default_spenders(req.chain_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That contract can't be removed: the assistant trades through it.",
        )

    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    fn = (
        wallet.functions.addTrustedSpender(spender)
        if req.action == "add"
        else wallet.functions.removeTrustedSpender(spender)
    )
    return {**_prepare_owner_tx(w3, owner, fn, req.chain_id), "spender": spender}


@router.post("/api/wallet/max-op-gas-cost/prepare")
def prepare_max_op_gas_cost(req: MaxOpGasCostRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `setMaxOpGasCost(newMax)` transaction.

    The ceiling on what a single UserOp may cost this account in gas. It bounds a distinct leak the
    USD spending cap cannot see: gas is paid to the EntryPoint, not spent on a metered token, so a
    compromised key could otherwise burn the wallet's balance on expensive operations without ever
    touching the cap (THREAT_MODEL 3.12). Raise it on an expensive chain, lower it to tighten the
    bound on a key you do not fully trust. The contract rejects 0, which would reject every UserOp.

    @return  {"tx", "max_cost_wei"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    max_wei = w3.to_wei(req.max_cost_eth, "ether")
    prepared = _prepare_owner_tx(w3, owner, wallet.functions.setMaxOpGasCost(max_wei), req.chain_id)
    return {**prepared, "max_cost_wei": str(max_wei)}


@router.post("/api/wallet/session-gas-budget/prepare")
def prepare_session_gas_budget(req: SessionGasBudgetRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `setSessionGasBudget(newBudget)` transaction.

    The most the assistant's operations may cost this account in gas per day, all together.
    max-op-gas-cost bounds one operation; this bounds them all, which a stolen key running its own
    bundler would otherwise repeat until the balance is gone (THREAT_MODEL 3.12). It takes effect at
    once and does not reset what today has used. The contract rejects 0: removing the key or
    pausing is how to stop the assistant.

    @return  {"tx", "budget_wei"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    budget_wei = w3.to_wei(req.budget_eth, "ether")
    prepared = _prepare_owner_tx(w3, owner, wallet.functions.setSessionGasBudget(budget_wei), req.chain_id)
    return {**prepared, "budget_wei": str(budget_wei)}
