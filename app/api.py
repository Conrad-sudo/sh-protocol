from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import hvac.exceptions
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


import functools
import os
import secrets
import sqlite3
import time
from decimal import Decimal

from eth_utils import to_hex
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted, TransactionNotFound
from web3.logs import DISCARD
from constants import (
    CHAIN_ID_ANVIL,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BASE,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
    ETH_SENTINEL,
    get_always_counted_ticker,
    get_native_asset_ticker,
    get_router,
)
from network_config import load_network_config_by_name
from db import (
    get_json,
    get_factory_address,
    get_rpc_url,
    get_session_key,
    get_supported_tokens_by_chain_id,
    get_token_address,
    acting_network,
    get_custom_tokens,
    save_custom_token,
    delete_custom_token,
    add_dashboard_tokens,
    get_dashboard_tokens,
    remove_dashboard_token,
    get_lp_tokens,
    set_lp_token_pair,
    get_supported_token_by_address,
    resolve_token,
    save_wallet_address,
    save_user_network,
    save_contact,
    get_contact,
    get_all_contacts,
    delete_contact,
    save_session_key,
    get_pending_session_key,
    delete_pending_session_key,
    get_wallet_address,
    get_wallet_chains,
    get_transactions,
    transaction_cursor,
    create_user,
    get_user_by_id,
    get_user_by_owner_addr,
    unlink_telegram,
    save_telegram_link_nonce,
)
from userop import create_pending_session_key, reconcile_session_key
from contracts import invalidate_cache, read_spending_config
from contract_errors import name_revert
from custom_tokens import CustomTokenError, inspect_custom_token
from parallel import read_all, start_read
import auth
from auth import get_current_user
from smart_wallet_agent import (
    ConversationBusy,
    chat,
    clear_history,
    close_checkpointer,
    get_history,
    init_agent,
    open_checkpointer,
)
import tx_history

from langchain_erc20 import ERC20_ABI
from langchain_uniswap_v2.abis import factory_abi, pair_abi, router_abi
from langchain_erc20.amounts import to_base_units

from pydantic import BaseModel, Field


load_dotenv()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Opens the agent's checkpointer and builds the agent on startup, closing it on shutdown.

    Exactly what telebot.py does in post_init/post_shutdown. Without it `agent` stays None and
    every /api/chat request fails on the first attribute access.
    """
    await open_checkpointer()
    init_agent()
    try:
        yield
    finally:
        await close_checkpointer()


app = FastAPI(lifespan=lifespan)

# Rate limiting, applied to the open sign-in endpoints and to the ones that issue nonces.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(hvac.exceptions.VaultError)
async def vault_unavailable(request: Request, exc: hvac.exceptions.VaultError):
    """
    Names Vault when it refuses a request, instead of a bare 500.

    Every session key is encrypted and decrypted through Vault, so deploys, grants and wallet reads
    all stop when it does. The everyday cause is the dev container restarting: it keeps everything
    in memory, so it comes back without the AppRole and the transit key, and the login in .env is
    refused. A bare 500 reached the web app only as "Something went wrong on our side", with
    nothing pointing at Vault. The exception itself goes to the log, not the client.
    """
    print(f"Vault error on {request.url.path}: {exc!r}")
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "Key storage (Vault) refused the request, so no session key could be made or "
            "read. If the Vault container was restarted, run `make vault`, then restart the API."
        },
    )

# The refresh token travels as a cookie, so the browser must be allowed to send credentials --
# which means the allowed origins have to be listed explicitly (a wildcard is rejected by the
# browser alongside credentials, and would be wrong here anyway).
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Name of the refresh-token cookie, and the path it is scoped to. Scoping it to the auth routes
# means it is not attached to every other API call, so it cannot leak through logs or a mistake in
# an unrelated handler.
#
# /api/auth, not /api/auth/refresh: logout has to RECEIVE the cookie to revoke it. With the
# narrower path the browser never sent it to /api/auth/logout, so signing out only deleted the
# browser's copy and left the token redeemable for its full 30 days.
REFRESH_COOKIE = "refresh_token"
REFRESH_COOKIE_PATH = "/api/auth"
# Where the cookie lived before the fix above. Cleared whenever the cookie is replaced or removed,
# so a browser that still holds one does not carry two.
LEGACY_REFRESH_COOKIE_PATH = "/api/auth/refresh"
# Secure cookies require HTTPS, which local development does not have. Defaults to on.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1").lower() in ("1", "true", "yes")

# Spending-window length used when the client sends none (24h — the cap refills each window).
DEFAULT_WINDOW_SECS = 86_400
# How long a granted session key lasts when the client sends no ttl (30 days). SessionHandler caps
# any grant at its own MAX_SESSION_TTL; this is the shorter figure the app actually asks for, so a
# key nobody renews dies on its own. Renewal is one owner-signed transaction.
DEFAULT_SESSION_TTL_SECS = 30 * 86_400
# How long before expiry the wallet read starts flagging a key as needing renewal, so a UI can
# prompt while there is still time rather than after the assistant has stopped working.
SESSION_RENEWAL_WARNING_SECS = 3 * 86_400
# SpendingLimitModule's cap is an 18-decimal USD value. The API takes whole dollars and scales here,
# so the front end never has to hold a 10**18-sized integer (see _to_json_tx for why that matters).
USD_DECIMALS = 10**18
# How long a confirm (/api/deploy/confirm, the owner confirms, a deposit) waits for the user's
# transaction before answering "still pending". Short, because it holds a worker thread: the front
# end polls the same endpoint again.
CONFIRM_POLL_TIMEOUT_SECS = 20
# How far back /api/deploy/confirm looks for the block that created a wallet when the hash it was
# given never mined because the user's wallet replaced it. Searched by halving, so it costs about 20
# reads; finding nothing only leaves that deploy out of the History tab.
REPLACED_DEPLOY_LOOKBACK_BLOCKS = 1_000_000

# The API speaks chain IDs, because that is what a browser wallet reports (eth_chainId) and what the
# user is actually connected to. Everything downstream of it speaks chain NAMES: save_user_network
# stores one, and load_network_config, contracts.py, tools.py and tx_sender all read it back. This
# map is the only place the two meet.
#
# It cannot be a DB lookup. `chains` holds two rows per forkable network — sepolia AND sepolia-fork
# both claim 11155111, with different RPCs (Alchemy vs 127.0.0.1:8545) — so get_chain_name_from_id
# returns whichever row SQLite reaches first. Which of the pair is meant is a fact about where THIS
# server runs, not something a browser can assert, so it is resolved here and never taken from the
# request.
CHAIN_NAME_BY_ID: dict[int, str] = {
    CHAIN_ID_ANVIL: "anvil",
    CHAIN_ID_MAINNET: "mainnet",
    CHAIN_ID_SEPOLIA: "sepolia",
    CHAIN_ID_BSC: "bsc",
    CHAIN_ID_CELO: "celo",
    CHAIN_ID_ARBITRUM: "arbitrum",
    CHAIN_ID_BASE: "base",
}
# Chains whose live name has a local `-fork` twin. Anvil is absent: it is already local and has no
# live counterpart to fork.
FORKABLE_CHAIN_IDS = {
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BASE,
}
# Set APP_FORK_MODE=1 to point every forkable chain at its local anvil fork instead of the live RPC.
# A deployment-wide switch, read once at import: a process serves forks or it serves live chains,
# and a request must not be able to choose.
FORK_MODE = os.getenv("APP_FORK_MODE", "").lower() in ("1", "true", "yes")


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
    # How long the seeded session key stays valid, in seconds. Optional so the deploy screen need
    # not ask; the contract enforces its own MAX_SESSION_TTL ceiling on top of this.
    session_ttl_secs: int = Field(default=DEFAULT_SESSION_TTL_SECS, gt=0)


class ConfirmRequest(BaseModel):
    """Body of POST /api/deploy/confirm — sent once the user's wallet returns a transaction hash.

    As with DeployRequest, the user id comes from the token rather than the body.
    """

    chain_id: int
    deployer: str
    tx_hash: str
    # The address /api/deploy predicted. Echoed back so the session key minted against it can be
    # re-pointed if this owner's deployCount moved in between (they deployed twice, two tabs open).
    predicted_address: str


def _to_json_tx(tx: dict) -> dict:
    """
    Hex-encodes every integer in an unsigned transaction so a browser can read it back intact.

    JSON numbers arrive in JavaScript as float64, whose integers stop being exact above 2**53. A
    1 ETH msg.value is 10**18 wei and maxFeePerGas * gas is the same order, so returning them as
    JSON numbers silently corrupts them. Hex strings are also what eth_sendTransaction expects, so
    the front end can hand this dict straight to MetaMask.
    """
    return {
        k: to_hex(v) if isinstance(v, (int, bytes, bytearray)) and not isinstance(v, bool) else v
        for k, v in tx.items()
    }


def _router_or_none(chain_id: int) -> str | None:
    """
    The exchange router every wallet on `chain_id` is deployed trusting, checksummed, or None where
    the chain has none (a bare Anvil). RPC-free: it reads constants only.
    """
    try:
        return Web3.to_checksum_address(get_router(chain_id))
    except ValueError:
        return None


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


def _failed_tx_detail(w3: Web3, tx_hash: str, receipt, what: str, hint: str = "") -> str:
    """
    The 400 detail for a mined transaction that failed: "<what> reverted (tx: …)", unless it ran out
    of gas, which says so and gives the numbers.

    Every transaction this API prepares carries its own gas estimate, but the user's wallet has the
    last word on the limit and some wallets replace it. A deploy watching 20 tokens needs ~1.47M gas;
    one went out with a 1,000,000 limit, died partway through seeding the tokens, and reached the
    user as a bare "reverted" that pointed nowhere. Re-estimating the same call against the state it
    ran on tells the two apart: if it needs more gas than it was given, the limit was the problem.

    @param what  How the message names the transaction ("deployWallet", "That transaction").
    @param hint  Appended to the out-of-gas message only.
    """
    reverted = f"{what} reverted (tx: {tx_hash})"
    try:
        tx = w3.eth.get_transaction(tx_hash)
        needed = w3.eth.estimate_gas(
            {"from": tx["from"], "to": tx["to"], "data": tx["input"], "value": tx["value"]},
            block_identifier=receipt["blockNumber"] - 1,
        )
    except Exception:
        # It reverts at any gas limit (a real revert), or the node no longer holds that state.
        return reverted
    if needed <= tx["gas"]:
        return reverted
    return (
        f"{what} ran out of gas (tx: {tx_hash}). Your wallet sent it with a gas limit of "
        f"{tx['gas']:,}, but it needs about {needed:,}. Try again and set the gas limit in your "
        f"wallet to at least that{hint}."
    )


def _tx_seen(w3: Web3, tx_hash: str) -> bool:
    """
    Whether this node knows the transaction at all, mined or still waiting in its pool.

    The confirms answer "pending" until a receipt exists, which cannot tell a slow transaction from
    one that never arrived: a wallet that handed back a hash and then failed to broadcast it, one
    sent through another RPC for the same chain ID, one the wallet replaced (sped up or cancelled),
    or a fork restarted since. Those never mine. The 202 carries this so the page can say so instead
    of waiting out its whole deadline in silence.
    """
    try:
        w3.eth.get_transaction(tx_hash)
    except TransactionNotFound:
        return False
    return True


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


def _network_name(chain_id: int) -> str:
    """
    This server's network name for `chain_id` -- its `-fork` twin in fork mode -- or 400s. RPC-free.

    See CHAIN_NAME_BY_ID for why the name is resolved here and never taken from the request.
    """
    chain_name = CHAIN_NAME_BY_ID.get(chain_id)
    if chain_name is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported chain ID: {chain_id}. Supported: {sorted(CHAIN_NAME_BY_ID)}",
        )
    if FORK_MODE and chain_id in FORKABLE_CHAIN_IDS:
        chain_name = f"{chain_name}-fork"
    return chain_name


def _resolve_chain(chain_id: int) -> tuple[Web3, str]:
    """
    Resolves a requested chain ID to (w3, chain_name), or 400s.

    The name is for internal use only — save_user_network and everything that reads it back. It is
    never echoed to the client, so the front end stays chain_id-only.

    Also asserts the RPC actually IS the requested chain. Without that, a mis-set APP_FORK_MODE or a
    stale rpcs row would silently build the user a transaction for a different network — one that
    could still be signed, and would then either revert or, worse, succeed somewhere unintended.

    @param chain_id  The chain ID the user's wallet reported.
    @return          (Web3 instance, the network name for that chain).
    """
    chain_name = _network_name(chain_id)

    try:
        w3, _ = load_network_config_by_name(chain_name)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    try:
        live_id = w3.eth.chain_id
    except Exception as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Cannot reach the RPC for {chain_name}: {e}"
        )
    if live_id != chain_id:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"The RPC configured for '{chain_name}' reports chain {live_id}, not the requested "
            f"{chain_id}. Check the rpcs table and APP_FORK_MODE.",
        )
    return w3, chain_name


# ── Auth ──────────────────────────────────────────────────────────────────────


class SiweLoginRequest(BaseModel):
    """Body of POST /api/auth/siwe/login."""

    # Capped so an oversized body is refused before the regex and the signature recovery run.
    message: str = Field(max_length=2048)
    signature: str = Field(max_length=200)
    nonce: str = Field(max_length=64)


class ChatRequest(BaseModel):
    """Body of POST /api/chat."""

    chain_id: int
    message: str = Field(min_length=1, max_length=4000)


def _set_refresh_cookie(response: Response, token: str):
    """
    Attaches the refresh token as an httpOnly cookie.

    httpOnly keeps it out of reach of JavaScript, so an XSS bug on the site cannot read the
    long-lived credential -- it would reach at most the in-memory access token, which expires in
    minutes. SameSite=Lax plus the narrow path is the CSRF defence: the cookie is only ever sent
    to the auth routes, and not on cross-site POSTs.

    @param response  The response to attach the cookie to.
    @param token     The refresh token.
    """
    response.delete_cookie(REFRESH_COOKIE, path=LEGACY_REFRESH_COOKIE_PATH)
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=auth.REFRESH_TOKEN_TTL_SECS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def _issue_session(response: Response, user_id: int) -> dict:
    """
    Issues a fresh token pair for a user: access token in the body, refresh token in the cookie.

    @param response  The response to attach the refresh cookie to.
    @param user_id   The account to sign in.
    @return          The JSON body: the access token, its lifetime, and the user id.
    """
    _set_refresh_cookie(response, auth.issue_refresh_token(user_id))
    return {
        "access_token": auth.create_access_token(user_id),
        "token_type": "bearer",
        "expires_in": auth.ACCESS_TOKEN_TTL_SECS,
        "user_id": user_id,
    }


@app.post("/api/auth/refresh")
def refresh(response: Response, refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE)):
    """
    Exchanges the refresh cookie for a new access token, rotating the refresh token.

    @return  A new access token; a new refresh cookie replaces the old one.
    """
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token")
    user_id, new_token = auth.rotate_refresh_token(refresh_token)
    _set_refresh_cookie(response, new_token)
    return {
        "access_token": auth.create_access_token(user_id),
        "token_type": "bearer",
        "expires_in": auth.ACCESS_TOKEN_TTL_SECS,
        "user_id": user_id,
    }


@app.post("/api/auth/logout")
def logout(response: Response, refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE)):
    """
    Signs out by revoking the presented refresh token and clearing the cookie.

    Always answers 200: an unknown or absent token is not an error, and treating it as one would
    reveal which tokens exist.
    """
    if refresh_token:
        auth.revoke_refresh(refresh_token)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    response.delete_cookie(REFRESH_COOKIE, path=LEGACY_REFRESH_COOKIE_PATH)
    return {"status": "signed out"}


@app.get("/api/auth/siwe/nonce")
@limiter.limit("30/minute")
def siwe_nonce(request: Request):
    """
    Issues a nonce for the SIWE message the user is about to sign.

    @return  {"nonce": str} — include it verbatim in the message.
    """
    return {"nonce": auth.issue_siwe_nonce()}


@app.get("/api/auth/siwe/account")
@limiter.limit("15/minute")
def siwe_account(request: Request, address: str = Query(max_length=64)):
    """
    Says whether an address already has an account, so the sign-in page can greet a returning user
    and call the button "Sign up" for a new one. Sign-in itself does not depend on it.

    Open to anyone: it reveals only whether an address has signed in to Mitfah before. Anyone with a
    wallet already shows that on chain through deployWallet, and the rate limit keeps a sweep slow.

    @param address  The connected wallet's address, any case.
    @return         {"registered": bool}.
    """
    try:
        owner = Web3.to_checksum_address(address)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{address}' is not an Ethereum address")
    return {"registered": get_user_by_owner_addr(owner) is not None}


@app.post("/api/auth/siwe/login")
@limiter.limit("10/minute")
def siwe_login(request: Request, req: SiweLoginRequest, response: Response):
    """
    Signs in with a Sign-In With Ethereum message, creating the account on the address's first one.

    The only way in. The account IS the address that signed: the same EOA owns the account's
    wallets on chain (deployWallet sets owner = msg.sender), so whoever can sign for it already
    controls everything the account stands for. verify_siwe makes the signature evidence: this
    site's domain, a fresh single-use nonce, and a signer that is the address the message names.

    Mitfah cannot recover an account whose address is lost. Neither could it before: only that
    address could ever pause, limit or withdraw from the wallet.

    @param req  The signed message, its signature, and the nonce it carries.
    @return     An access token; the refresh token is set as a cookie.
    """
    address = auth.verify_siwe(req.message, req.signature, req.nonce)
    user = get_user_by_owner_addr(address)
    if user is not None:
        return _issue_session(response, user["id"])
    try:
        return _issue_session(response, create_user(owner_addr=address))
    except ValueError:
        # The same address's first sign-in, twice at once: the other request made the account.
        return _issue_session(response, get_user_by_owner_addr(address)["id"])


@app.get("/api/me")
def me(user_id: int = Depends(get_current_user)):
    """
    Returns the signed-in account, without anything secret.

    @return  The account's id, the address it signs in as (which owns its wallets), whether
             Telegram is linked, and its wallets.
    """
    user = get_user_by_id(user_id)
    return {
        "user_id": user["id"],
        "owner_addr": user["owner_addr"],
        "telegram_linked": user["telegram_chat_id"] is not None,
        "wallet_chains": get_wallet_chains(user_id),
    }


# ── Telegram linking ──────────────────────────────────────────────────────────


@app.post("/api/integrations/telegram/link")
def telegram_link(user_id: int = Depends(get_current_user)):
    """
    Mints a single-use deep link that binds the user's Telegram chat to this account.

    The user follows the link, Telegram sends the bot `/start <nonce>`, and the bot binds the chat
    id **from the update it receives**. That indirection is the point: a chat id is self-asserted
    and enumerable, so a form that accepted one would let anyone attach their Telegram to another
    person's wallet and spend against its cap.

    @return  {"url", "nonce", "expires_in"} — send the user to `url`.
    """
    bot = os.getenv("TELEGRAM_BOT_USERNAME")
    if not bot:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "TELEGRAM_BOT_USERNAME is not configured"
        )
    nonce = secrets.token_urlsafe(24)
    save_telegram_link_nonce(nonce, user_id, int(time.time()) + auth.TELEGRAM_NONCE_TTL_SECS)
    return {
        "url": f"https://t.me/{bot}?start={nonce}",
        "nonce": nonce,
        "expires_in": auth.TELEGRAM_NONCE_TTL_SECS,
    }


@app.delete("/api/integrations/telegram/link")
def telegram_unlink(user_id: int = Depends(get_current_user)):
    """Detaches the Telegram chat from the signed-in account."""
    unlink_telegram(user_id)
    return {"status": "unlinked"}


# ── Contacts ──────────────────────────────────────────────────────────────────
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


@app.post("/api/contacts/prepare")
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


@app.post("/api/contacts", status_code=status.HTTP_201_CREATED)
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


@app.get("/api/contacts")
def list_contacts(user_id: int = Depends(get_current_user)):
    """Returns the signed-in account's contacts, sorted by name."""
    return {"contacts": get_all_contacts(user_id)}


@app.delete("/api/contacts/{name:path}")
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


# ── Chat ──────────────────────────────────────────────────────────────────────


@app.post("/api/chat")
def post_chat(req: ChatRequest, user_id: int = Depends(get_current_user)):
    """
    Runs one turn of the wallet agent.

    A plain `def`, not `async def`, on purpose: chat() blocks for 10-60 seconds while the agent
    calls tools and waits on chain confirmations. FastAPI runs a sync handler in its threadpool, so
    one slow turn does not stall the event loop for everybody else. Declared `async def` it would.

    The user id comes from the token and is handed to the agent as runtime context, so nothing the
    message says can change whose wallet is acted on. The chain is the one the page is on, and the
    whole turn acts on it -- not on the user's saved network, which is just the chain they last
    deployed on (see db.acting_network).

    @param req  The chain and the user's message.
    @return     {"reply": str}
    @raises HTTPException 400 for a chain this server does not serve, as chat_history does.
    """
    return {"reply": chat(user_id, req.chain_id, req.message, _network_name(req.chain_id))}


@app.get("/api/chat/history")
def chat_history(
    chain_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    user_id: int = Depends(get_current_user),
):
    """
    Returns the recent conversation for one chain, so the web chat is not blank after a reload.

    Only what was said is returned -- the user's messages and the assistant's text. Tool traffic
    stays inside: those messages carry the session-key ciphertext. See smart_wallet_agent.get_history.
    The thread is shared with Telegram, so messages sent there appear too.

    A plain `def` for the same reason as post_chat: the checkpointer's sync read must not run on the
    event loop.

    @param chain_id  The chain whose conversation to read.
    @param limit     How many of the most recent messages to return (1-200).
    @return          {"chain_id", "messages": [{"role": "user" | "assistant", "text"}, ...]}, oldest first.
    """
    if chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    return {"chain_id": chain_id, "messages": get_history(user_id, chain_id, limit)}


@app.delete("/api/chat/history")
def delete_chat_history(chain_id: int | None = None, user_id: int = Depends(get_current_user)):
    """
    Deletes the conversation on one chain, or on every chain when `chain_id` is left out.

    The conversation is shared with Telegram, so this also clears what the assistant remembers
    there. Quotes raised in it are discarded with it. The transaction history is kept: it records
    what happened on chain, which deleting a chat can't undo.

    A plain `def`: deleting a checkpoint thread is a sync call into the async checkpointer, which
    works only off the event loop.

    @param chain_id  The chain whose conversation to delete; every chain's when omitted.
    @return          {"status": "cleared", "chain_ids": [...]}.
    @raises HTTPException 400 for a chain this server does not serve; 409 while the assistant is
            still answering a message in one of them (nothing is deleted then).
    """
    if chain_id is not None and chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    chain_ids = [chain_id] if chain_id is not None else sorted(CHAIN_NAME_BY_ID)
    try:
        clear_history(user_id, chain_ids)
    except ConversationBusy:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The assistant is still answering a message. Try again once it has replied.",
        )
    return {"status": "cleared", "chain_ids": chain_ids}


# ── Transaction history ───────────────────────────────────────────────────────


class DepositRequest(BaseModel):
    """Body of POST /api/transactions/deposit."""

    chain_id: int
    tx_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


def _web3_or_none(chain_id: int) -> Web3 | None:
    """
    A Web3 for `chain_id` when this server serves it and its RPC answers, else None: for work that
    skips a chain it can't reach rather than failing, like settling the History tab's pending rows.
    """
    if chain_id not in CHAIN_NAME_BY_ID:
        return None
    try:
        return _resolve_chain(chain_id)[0]
    except HTTPException:
        return None


def _transaction_json(row: dict) -> dict:
    """A transactions row as the History tab sees it. The fields that find a lost op stay inside."""
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "source": row["source"],
        "action": row["action"],
        "status": row["status"],
        "tx_hash": row["tx_hash"],
        "created_at": row["created_at"],
        "mined_at": row["mined_at"],
    }


@app.get("/api/transactions")
def list_transactions(
    chain_id: int | None = None,
    before: str | None = Query(default=None, pattern=r"^\d{1,12}-\d{1,12}$"),
    limit: int = Query(default=50, ge=1, le=100),
    user_id: int = Depends(get_current_user),
):
    """
    The History tab: every transaction on this account's wallets, newest first -- what the
    assistant sent, from the web or Telegram, what the user signed in the browser, and what
    happened outside Mitfah.

    Reading the first page also settles transactions still pending, where it can: a send that
    outlived the wait, or an owner transaction whose page was closed before it mined. That reads
    the chain, hence a plain `def`. It also starts a background search of the block explorers for
    activity outside Mitfah (tx_history.start_outside_sync); the page never waits on it.

    @param chain_id  Only this chain's transactions; every chain's when omitted.
    @param before    The `next_before` of the previous page.
    @param limit     How many to return (1-100).
    @return          {"transactions": [{"id", "chain_id", "source", "action", "status", "tx_hash",
                     "created_at", "mined_at"}, ...], "next_before": str | None, "syncing": bool}.
                     Times are Unix seconds; `mined_at` is the block's. `syncing` is true while an
                     explorer search is running, so the tab reads again soon.
    """
    if chain_id is not None and chain_id not in CHAIN_NAME_BY_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported chain ID: {chain_id}")
    syncing = False
    if before is None:
        web3_for = functools.cache(_web3_or_none)
        tx_history.settle_pending(user_id, web3_for)
        chains = [c for c in get_wallet_chains(user_id) if chain_id is None or c == chain_id]
        wallets = {c: get_wallet_address(user_id, c) for c in chains if c in CHAIN_NAME_BY_ID}
        syncing = tx_history.start_outside_sync(user_id, wallets, web3_for)
    cursor = tuple(int(part) for part in before.split("-")) if before is not None else None
    rows = get_transactions(user_id, chain_id, cursor, limit + 1)
    page = rows[:limit]
    return {
        "transactions": [_transaction_json(row) for row in page],
        "next_before": "-".join(map(str, transaction_cursor(page[-1]))) if len(rows) > limit else None,
        "syncing": syncing,
    }


@app.post("/api/transactions/deposit")
def confirm_deposit(req: DepositRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for a deposit made from the Fund drawer, and lists it in the History tab.

    The browser sends a deposit itself -- anyone may fund a wallet, so it never goes through the
    owner confirms -- and the drawer asks here until it has mined, through this node: the one the
    dashboard reads the balance from. Only a transaction actually sent to this user's wallet on the
    chain counts. A deposit is listed from the first answer that finds it on the network, so closing
    the drawer before it mines loses nothing; the History tab settles it later.

    Answers 202 while the transaction is still pending, with `seen` as in /api/wallet/tx/confirm;
    the front end polls until it gets a 200.

    @return  {"status": "confirmed", "tx_hash"} once mined.
    @raises HTTPException 400 if it was not sent to the wallet, or failed.
    """
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    try:
        tx = w3.eth.get_transaction(req.tx_hash)
    except TransactionNotFound:
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": False}
    if tx["to"] is None or Web3.to_checksum_address(tx["to"]) != wallet.address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transaction {req.tx_hash} was not sent to your wallet on chain {req.chain_id}.",
        )
    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash)
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash, "seen": True}

    tx_history.record_owner_tx(w3, user_id, req.chain_id, wallet, req.tx_hash, receipt)
    if receipt["status"] != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, _failed_tx_detail(w3, req.tx_hash, receipt, "The transfer")
        )
    return {"status": "confirmed", "tx_hash": req.tx_hash}


def _require_own_deployer(user_id: int, deployer: str):
    """
    Refuses a deploy unless the deploying EOA is the one this account signs in as (via SIWE).

    Without this the deployer is just an address in the request body, and two things go wrong.
    A caller can name somebody else's EOA, which mints a session key for a wallet they will never
    own; and, given another user's deploy transaction, they can call /api/deploy/confirm for it and
    register that wallet against their own account. The stolen row cannot SPEND -- the key seeded
    into the real deploy is the owner's, so the attacker's key is never authorized -- but their
    agent would happily read the victim's balances through it.

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


@app.get("/api/chains")
def list_chains():
    """
    Lists the chains a user can deploy a wallet on with this server.

    A chain qualifies when this deployment serves it (CHAIN_NAME_BY_ID) AND the protocol is deployed
    there (a `factory` row). The front end builds its network picker from this rather than from a
    list of its own that would drift from the server's.

    Public and RPC-free, like /api/tokens: it reads local tables and says nothing that is not
    already public on chain, apart from a fork's local node address.

    @return  {"chains": [{"chain_id", "name", "native_ticker", "fork", "rpc_url", "router"}, ...]},
             by chain ID.
             `fork` is true when this server points that chain at a local fork (APP_FORK_MODE).
             `rpc_url` is that fork's local node, for the user to set in their browser wallet — each
             fork runs on its own port. None on a live chain: its RPC may carry an API key.
             `router` is the exchange router every wallet on that chain is deployed trusting (see
             /api/deploy), or None where there is none; the Controls page keeps it off the
             removable list.
    """
    chains = []
    for chain_id, name in sorted(CHAIN_NAME_BY_ID.items()):
        try:
            get_factory_address(chain_id)
        except ValueError:
            continue
        try:
            native_ticker = get_native_asset_ticker(chain_id)
        except ValueError:
            native_ticker = None
        fork = FORK_MODE and chain_id in FORKABLE_CHAIN_IDS
        chains.append({
            "chain_id": chain_id,
            "name": name,
            "native_ticker": native_ticker,
            "fork": fork,
            "rpc_url": get_rpc_url(f"{name}-fork") if fork else None,
            "router": _router_or_none(chain_id),
        })
    return {"chains": chains}


@app.get("/api/tokens")
def list_tokens(chain_id: int):
    """
    Lists the tokens a wallet on `chain_id` may meter, for the deploy screen's picker.

    The user's selection comes back as DeployRequest.watched_tokens. Every ticker here is from the
    same table the deploy endpoint validates against, so anything pickable is deployable.

    Deliberately does NOT go through _resolve_chain: listing tokens needs no RPC, and a fork shares
    its parent's token table, so the fork/live distinction cannot change the answer.

    `always_counted` marks the wrapped native token (WETH, WBNB): every new wallet counts it, like
    the native asset it wraps, so the picker shows it ticked and locked (see /api/deploy).

    @return  {"chain_id", "tokens": [{"ticker", "address", "always_counted"}, ...]}.
    """
    try:
        tokens = get_supported_tokens_by_chain_id(chain_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    always = get_always_counted_ticker(chain_id)
    return {"chain_id": chain_id, "tokens": [{**t, "always_counted": t["ticker"] == always} for t in tokens]}


# ── Custom tokens ─────────────────────────────────────────────────────────────
#
# Tokens a user adds by contract address, MetaMask-style. They are a list Mitfah keeps, not a
# setting on the wallet: the wallet can already hold and move any ERC-20, and one without a price
# feed sits outside the spending cap whether it is listed here or not. So adding or removing one
# needs no signature and changes nothing on chain -- it decides what the dashboard shows and which
# names the assistant resolves. Every check (a real ERC-20, a symbol safe to show the assistant, no
# copy of a listed ticker) is in custom_tokens.py and runs against the chain, never the request.
#
# A token Mitfah lists can be added the same way: it goes on the dashboard (dashboard_tokens), and
# the web app offers to count it -- a separate owner transaction (watched-tokens/prepare). Either
# kind can be removed, except a token the wallet still counts: the dashboard must show everything
# the limit covers, so that one has to stop counting first.


class CustomTokenRequest(BaseModel):
    """Body of POST /api/tokens/custom/lookup and POST /api/tokens/custom."""

    chain_id: int
    address: str = Field(max_length=64)


def _inspect_for_user(req: CustomTokenRequest, user_id: int) -> dict:
    """
    Runs the add-a-token checks for this user's wallet on `req.chain_id`, as 400s the UI can show.

    The checks read the token through contracts.load_ierc20, which finds the chain through the
    user's network -- so the request's chain is made the acting network for the duration, exactly
    as a chat turn does (db.acting_network). The saved network, which the Telegram bot follows, is
    left alone.
    """
    w3, chain_name = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    try:
        with acting_network(user_id, chain_name, req.chain_id):
            return inspect_custom_token(user_id, req.chain_id, req.address, wallet.address)
    except CustomTokenError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@app.post("/api/tokens/custom/lookup")
@limiter.limit("30/minute")
def lookup_custom_token(request: Request, req: CustomTokenRequest, user_id: int = Depends(get_current_user)):
    """
    Reads a token off the chain so the user can see what they are about to add. Saves nothing.

    @return  {"chain_id", "address", "ticker", "symbol", "name", "decimals", "balance_raw"}: the
             symbol and decimals are what make it an ERC-20 -- an address that can't answer both is
             refused with a message asking the user to check it again.
    @raises HTTPException 400 with the reason the token can't be added; 404 with no wallet here.
    """
    return {"chain_id": req.chain_id, **_inspect_for_user(req, user_id)}


@app.post("/api/tokens/custom", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
def add_custom_token(request: Request, req: CustomTokenRequest, user_id: int = Depends(get_current_user)):
    """
    Adds a token to the signed-in account's list for `chain_id`, after checking it again on chain.
    A listed token goes on the dashboard; counting it is the owner's separate transaction.

    The lookup's answer is not trusted: the checks run again here, so a request that skipped the
    preview (or raced another tab) gets the same answer.

    @return  The saved token: {"chain_id", "address", "ticker", "symbol", "name", "decimals",
             "balance_raw", "listed"}.
    @raises HTTPException 400 if the token can't be added, 409 if another request just added it.
    """
    token = _inspect_for_user(req, user_id)
    if token["listed"]:
        add_dashboard_tokens(user_id, req.chain_id, [token["ticker"]])
        return {"chain_id": req.chain_id, **token}
    try:
        save_custom_token(
            user_id, req.chain_id, token["address"], token["ticker"], token["name"], token["decimals"]
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "That token was just added. Reload to see it.")
    # No cache to drop, here or in the bot's process: the tools resolve names from the table on
    # every call (db.resolve_token), and nothing caches by ticker.
    return {"chain_id": req.chain_id, **token}


def _remove_listed_token(user_id: int, chain_id: int, token: dict):
    """Takes a listed token off the dashboard, refusing while the wallet still counts it."""
    name = token["ticker"].upper()
    # Both refusals that need no chain come first, so they cost no RPC round trip.
    if token["ticker"] == get_always_counted_ticker(chain_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{name} always counts toward your limit, like {get_native_asset_ticker(chain_id)}, "
            "so it stays on your dashboard.",
        )
    if not any(t["ticker"] == token["ticker"] for t in get_dashboard_tokens(user_id, chain_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} is not on your dashboard")
    try:
        get_wallet_address(user_id, chain_id)
        has_wallet = True
    except ValueError:
        has_wallet = False  # nothing can count it: nothing to check
    if has_wallet:
        w3, _ = _resolve_chain(chain_id)
        watched = read_spending_config(_load_wallet_for_chain(w3, user_id, chain_id))["watchedTokens"]
        if any(Web3.to_checksum_address(a) == token["address"] for a in watched):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{name} counts toward your limit. Stop counting it first, then remove it.",
            )
    remove_dashboard_token(user_id, chain_id, token["ticker"])


@app.delete("/api/tokens/custom/{chain_id}/{address}")
def remove_custom_token(chain_id: int, address: str, user_id: int = Depends(get_current_user)):
    """
    Removes a token from the signed-in account's list. The tokens themselves stay in the wallet --
    the owner can still withdraw them, and adding the token again brings them back into view.

    A listed token comes off the dashboard, but not while the wallet counts it toward the limit
    (read from the chain, not trusted to the page), and never the wrapped native token, which
    always counts. An added-by-address token has no price, so it never counts.

    @raises HTTPException 400 for the wrapped native token, 404 if the token is not on this
            account's list for `chain_id`, 409 while a listed token still counts.
    """
    try:
        address = Web3.to_checksum_address(address)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{address}' is not on your token list")
    listed = get_supported_token_by_address(chain_id, address)
    if listed is not None:
        _remove_listed_token(user_id, chain_id, listed)
        return {"status": "deleted", "chain_id": chain_id, "address": address}
    if not delete_custom_token(user_id, chain_id, address):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{address} is not on your token list")
    return {"status": "deleted", "chain_id": chain_id, "address": address}


@app.post("/api/deploy")
def deploy_wallet(req: DeployRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the UNSIGNED SHFactory.deployWallet transaction for the user to sign in their own wallet.

    Nothing is broadcast here and no key of the app's signs anything — that is what keeps the wallet
    non-custodial. The response carries the transaction, the address it will produce and the session
    key seeded into it. The front end passes the transaction to MetaMask and posts the resulting hash
    to /api/deploy/confirm, which is where anything durable is written.

    The session key HAS to be minted before signing: its Vault entry is keyed by wallet address and
    deployWallet authorizes it inside initialize(), so it must exist while the calldata is built.
    CREATE2 is what makes that possible — predictWalletAddress answers for this owner's next deploy.

    @param req  Deploy parameters collected by the front end.
    @return     {"chain_id", "predicted_address", "session_key", "watched_tokens", "tx"}.
    """
    # Authorize BEFORE resolving the chain: _resolve_chain dials an RPC, and a caller who may not
    # deploy from this address should not be able to make the server do work or learn which chains
    # it is configured for.
    try:
        deployer = Web3.to_checksum_address(req.deployer)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.deployer}")

    _require_own_deployer(user_id, deployer)

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
    # Minted into pending: the user has not signed the deploy yet, and may never. Nothing is
    # authorized on chain until /api/deploy/confirm sees the wallet, which is what promotes it.
    session_key, _ = create_pending_session_key(user_id, chain_id, predicted)
    session_key_valid_until = int(time.time()) + req.session_ttl_secs

    # The router is a wallet-level choice, not protocol config. Sourced from constants.get_router so
    # the router the wallet trusts is the one toolkits.py builds calldata for. Bare Anvil has none.
    try:
        trusted_spenders = [w3.to_checksum_address(get_router(chain_id))]
    except ValueError:
        print(f"No router configured for chain {chain_id}; deploying with no trusted spenders")
        trusted_spenders = []

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
            session_key,
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
            status.HTTP_400_BAD_REQUEST, f"Could not build deployWallet transaction: {e}"
        )

    return {
        "chain_id": chain_id,
        "predicted_address": predicted,
        "session_key": session_key,
        "watched_tokens": watched_tokens,
        "tx": _to_json_tx(tx),
    }


@app.post("/api/deploy/confirm")
def confirm_deploy(req: ConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Records the wallet once the user's signed deployWallet transaction has mined.

    This is the half that writes: the wallet address, the user's network, and — if the address moved
    — the session key's target. Splitting it from /api/deploy is what stops an abandoned signature
    prompt from leaving a row for a wallet that does not exist, and what lets the app recover the
    address from the receipt rather than trusting the prediction.

    Answers 202 while the transaction is still pending, with `seen` as in /api/wallet/tx/confirm;
    the front end polls until it gets a 200.

    A hash the node has never seen still finishes the deploy when the wallet is already at the
    predicted address. The user's wallet replaced the transaction (sped it up, say) and it mined
    under a hash nobody told the app. Answered "not seen", the page would offer to start over, and a
    second deploy makes a second wallet.

    @param req  The chain, the user's EOA, the transaction hash and the predicted address.
    @return     {"status", "chain_id", "wallet_address", "session_key", "session_key_authorized"}.
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
        # this deploy, mined under another hash. Only theirs: the owner check, and the session key
        # that only /api/deploy for this account mints, keep anyone else's wallet out.
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

    @param predicted       The address /api/deploy predicted, which the pending session key is under.
    @param wallet_address  Where the wallet actually is.
    @return                The confirm's 200 body.
    """
    # The prediction can go stale: deployCount is per-owner, so a second deploy by this same user
    # between /api/deploy and their signature moves the address. The key seeded into initialize() is
    # still the one minted against `predicted`, so MOVE the row rather than minting a new key — a
    # fresh key would not be authorized on chain and every tool would fail to sign.
    row = get_pending_session_key(user_id, chain_id, predicted)
    if row is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No session key held for predicted address {predicted}; call /api/deploy first.",
        )
    session_key, ciphertext = row
    if wallet_address != predicted:
        print(f"Predicted {predicted} but the deploy landed at {wallet_address}; re-pointing session key.")

    # Verify on chain that the key we hold can actually sign for this wallet, rather than assuming
    # initialize() seeded what was passed. isSessionActive covers both halves: the wallet's one key
    # IS this one, and its deadline has not already gone by.
    handler_abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    wallet_contract = w3.eth.contract(address=wallet_address, abi=handler_abi)
    authorized = wallet_contract.functions.isSessionActive(session_key).call()

    # Promote out of pending under the address the wallet ACTUALLY got, which is the prediction
    # except when another deploy by this same user landed in between.
    save_session_key(user_id, chain_id, wallet_address, session_key, ciphertext)
    delete_pending_session_key(user_id, chain_id, predicted)

    # Saved even when the key is NOT authorized: the wallet exists and holds the prefund, so losing
    # the reference is worse than recording one the bot cannot sign for. The owner can still drive it
    # from their own EOA, and deploy_wallet.add_default_session() re-grants a key.
    save_user_network(user_id, chain_name)
    save_wallet_address(user_id, chain_id, wallet_address)
    invalidate_cache(user_id)

    if not authorized:
        print(
            f"WARNING: {session_key} is NOT authorized on {wallet_address} — "
            "run deploy_wallet.add_default_session to re-grant it."
        )

    return {
        "status": "deployed",
        "chain_id": chain_id,
        "wallet_address": wallet_address,
        "session_key": session_key,
        "session_key_authorized": authorized,
        "session_key_expires_at": wallet_contract.functions.currentSessionValidUntil().call(),
    }







# ── Owner actions (pause / withdraw / watched tokens) ─────────────────────────
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


def _require_owner(user_id: int) -> str:
    """
    Returns the account's bound owner EOA, or 403s.

    Every owner action is signed by this address, so without a binding there is nothing to build a
    transaction for.

    @param user_id  The authenticated account.
    @return         The checksummed owner address.
    @raises HTTPException 403 for an account with no address: one migrated from Telegram-only
            days, which never signed in on the web.
    """
    owner_addr = get_user_by_id(user_id)["owner_addr"]
    if not owner_addr:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account has no wallet address. Sign in on the web app with the wallet that owns it.",
        )
    return owner_addr


def _load_wallet_for_chain(w3: Web3, user_id: int, chain_id: int):
    """
    Binds this user's SessionHandler on `chain_id`.

    Deliberately not contracts.load_session_handler, which resolves the chain through the user's
    SAVED network: the chain being acted on here is the one the browser wallet is connected to, and
    the two can differ. Resolving it internally would build a transaction against the wrong wallet.

    Checks for code at the address for the same reason _load_factory_for_chain does -- a restarted
    anvil leaves a stale row, and calling a bare address raises an opaque web3 error.

    @param w3        A Web3 bound to `chain_id`.
    @param user_id   The authenticated account.
    @param chain_id  The chain the wallet lives on.
    @return          A web3.py Contract for the wallet.
    @raises HTTPException 404 if this user has no wallet on this chain, 503 if the address is empty.
    """
    try:
        address = get_wallet_address(user_id, chain_id)
    except ValueError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"You have no wallet on chain {chain_id}."
        )
    if w3.eth.get_code(address) in (b"", HexBytes("0x")):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"No code at {address} on chain {chain_id}. The wallet record is stale — if this is a "
            f"local node that restarted, redeploy.",
        )
    abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    return w3.eth.contract(address=address, abi=abi)


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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not build the transaction: {e}")
    return {"tx": _to_json_tx(tx)}


def _revert_reason(e: Exception) -> str:
    """
    Turns a web3 revert into something worth showing a user.

    The whole value of simulating before signing is the message, so a bare `0x4c0a7758` is a failed
    simulation as far as the UI is concerned. Any 4-byte selector in the exception text is swapped
    for its error signature; anything unrecognised falls through as the original first line, which
    is still better than nothing.
    """
    message = str(e).split("\n")[0].strip() or e.__class__.__name__
    return name_revert(message) or message


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


def _ticker_map(chain_id: int) -> dict[str, str]:
    """
    Lowercased token address -> ticker, for `chain_id`.

    Chain-explicit on purpose. tools._addresses_to_tickers does the same job but resolves the chain
    from the user's SAVED network, which is not necessarily the chain being read here -- the same
    reason _load_wallet_for_chain exists.
    """
    try:
        return {t["address"].lower(): t["ticker"] for t in get_supported_tokens_by_chain_id(chain_id)}
    except ValueError:
        return {}


def _token_balances(w3: Web3, user_id: int, chain_id: int, account: str) -> list[dict]:
    """
    Balances for `account` of the native token and the tokens on the user's dashboard: the listed
    ones in dashboard_tokens, then the ones added by address, then the LP tokens of the pools the
    assistant deposited into (_lp_balances).

    Each token is fetched independently and a failure is reported per-token rather than raised: one
    token with no code (a stale row, a chain that moved a deployment) must not blank out the whole
    dashboard. Raw amounts are strings -- a uint256 of wei does not survive JSON's float64.

    Two eth_calls per listed token (decimals + balanceOf). Tokens added by address cost one: their
    decimals were read and stored when they were added. `custom` marks those -- they have no price
    and never count toward the cap. `always_counted` marks the wrapped native token (WETH, WBNB),
    which stays on the dashboard.
    """
    native_raw = w3.eth.get_balance(account)
    balances: list[dict] = [
        {
            "ticker": get_native_asset_ticker(chain_id).lower(),
            "address": None,
            "native": True,
            "decimals": 18,
            "raw": str(native_raw),
            "amount": float(Decimal(native_raw) / Decimal(10**18)),
        }
    ]
    always = get_always_counted_ticker(chain_id)
    for token in get_dashboard_tokens(user_id, chain_id):
        entry = {
            "ticker": token["ticker"],
            "address": token["address"],
            "native": False,
            "custom": False,
            "always_counted": token["ticker"] == always,
        }
        try:
            erc20 = w3.eth.contract(address=entry["address"], abi=ERC20_ABI)
            decimals = erc20.functions.decimals().call()
            raw = erc20.functions.balanceOf(account).call()
            entry |= {
                "decimals": decimals,
                "raw": str(raw),
                "amount": float(Decimal(raw) / Decimal(10**decimals)),
            }
        except Exception as e:  # noqa: BLE001 -- reported per token, never fatal
            entry |= {"decimals": None, "raw": None, "amount": None, "error": _revert_reason(e)}
        balances.append(entry)
    for token in get_custom_tokens(user_id, chain_id):
        entry = {
            "ticker": token["ticker"],
            "address": token["address"],
            "native": False,
            "custom": True,
            "always_counted": False,
            "name": token["name"],
        }
        try:
            raw = w3.eth.contract(address=token["address"], abi=ERC20_ABI).functions.balanceOf(account).call()
            entry |= {
                "decimals": token["decimals"],
                "raw": str(raw),
                "amount": float(Decimal(raw) / Decimal(10 ** token["decimals"])),
            }
        except Exception as e:  # noqa: BLE001 -- reported per token, never fatal
            entry |= {"decimals": None, "raw": None, "amount": None, "error": _revert_reason(e)}
        balances.append(entry)
    return balances + _lp_balances(w3, user_id, chain_id, account)


# Every Uniswap V2 pair's LP token has 18 decimals, fixed in the pair contract -- PancakeSwap's too.
_LP_DECIMALS = 18
# A wallet holding less than 1/_LP_DUST of a pool holds none, as far as the dashboard goes. "Remove
# all" passes the amount as a float, so it can leave a few wei behind -- at most ~2e-16 of what was
# held, so always far below this. No real position is this small: in a $1B pool it is $0.001.
_LP_DUST = 10**12
# Each chain's V2 factory, read off its router once: a router's factory never changes.
_v2_factories: dict[int, str] = {}


def _v2_factory(w3: Web3, chain_id: int):
    """
    The V2 factory behind the router the wallets on `chain_id` trust -- found the way the assistant's
    toolkit finds it (router.factory(), see toolkits.get_uniswap_tools), so both see the same pools.
    """
    if chain_id not in _v2_factories:
        router = w3.eth.contract(address=get_router(chain_id), abi=router_abi)
        _v2_factories[chain_id] = router.functions.factory().call()
    return w3.eth.contract(address=_v2_factories[chain_id], abi=factory_abi)


def _read_new_pools(w3: Web3, user_id: int, chain_id: int, pools: list[dict]):
    """
    Finds each pool's address and its two tokens' decimals, all at once, and saves them, so the
    dashboard reads them only the first time it shows a pool. A pool with no address yet (the
    deposit that would create it hasn't landed) is left for a later read.

    @param pools  Rows of db.get_lp_tokens with no `pair` yet. Each is updated in place.
    """
    factory = _v2_factory(w3, chain_id)
    reads = {}
    for i, pool in enumerate(pools):
        reads[i, "pair"] = factory.functions.getPair(pool["token0"], pool["token1"]).call
        for side in ("0", "1"):
            token = w3.eth.contract(address=pool["token" + side], abi=ERC20_ABI)
            reads[i, "decimals" + side] = token.functions.decimals().call
    found = read_all(reads)
    for i, pool in enumerate(pools):
        if int(found[i, "pair"], 16) == 0:
            continue
        pool |= {"pair": found[i, "pair"], "decimals0": found[i, "decimals0"], "decimals1": found[i, "decimals1"]}
        set_lp_token_pair(
            user_id, chain_id, pool["token0"], pool["token1"], pool["pair"], pool["decimals0"], pool["decimals1"]
        )


def _known_pools(w3: Web3, user_id: int, chain_id: int) -> list[dict]:
    """
    The pools the assistant deposited into (db.lp_tokens) whose address is known, reading it off the
    chain the first time (_read_new_pools). One that can't be read, or doesn't exist yet, is left
    out and tried again on the next read.
    """
    pools = get_lp_tokens(user_id, chain_id)
    new = [pool for pool in pools if pool["pair"] is None]
    if new:
        try:
            _read_new_pools(w3, user_id, chain_id, new)
        except Exception as e:  # noqa: BLE001 -- tried again on the next read
            print(f"Could not read the new pools for user {user_id} on chain {chain_id}: {e!r}")
    return [pool for pool in pools if pool["pair"] is not None]


def _lp_balances(w3: Web3, user_id: int, chain_id: int, account: str) -> list[dict]:
    """
    The wallet's LP tokens: one row for each pool the assistant deposited into (db.lp_tokens) that
    the wallet still holds some of. A pool it has taken everything out of isn't shown; the next
    deposit brings it back.

    `underlying` is what the LP tokens hold right now: the wallet's share of each of the pool's
    tokens (balance x reserve / totalSupply), which is about what taking it all out would return.
    A few wei left behind by a "remove all" count as nothing (_LP_DUST).
    Rows are named like "eth/usdc lp", the native asset's side first, and `underlying` follows the
    same order.

    Three reads per pool, all at once, plus a round the first time a pool is shown (_read_new_pools).
    A pool that can't be read is reported, like any other balance, never raised. An LP token has no
    price and the assistant can't send one (tools._token_address), so it is neither `custom` nor
    counted; `lp` marks it.
    """
    pools = _known_pools(w3, user_id, chain_id)
    reads = {}
    for pool in pools:
        pair = w3.eth.contract(address=pool["pair"], abi=pair_abi)
        reads[pool["pair"]] = [
            start_read(read)
            for read in (pair.functions.balanceOf(account).call, pair.functions.totalSupply().call,
                         pair.functions.getReserves().call)
        ]

    native = get_native_asset_ticker(chain_id).lower()
    rows = []
    for pool in pools:
        sides = [(pool["ticker0"], pool["decimals0"]), (pool["ticker1"], pool["decimals1"])]
        order = (1, 0) if pool["ticker1"] == native else (0, 1)
        entry = {
            "ticker": "/".join(sides[i][0] for i in order) + " lp",
            "address": pool["pair"],
            "native": False,
            "custom": False,
            "always_counted": False,
            "lp": True,
        }
        try:
            balance, supply, (reserve0, reserve1, _) = (read.result() for read in reads[pool["pair"]])
        except Exception as e:  # noqa: BLE001 -- reported per pool, never fatal
            rows.append(entry | {"decimals": None, "raw": None, "amount": None, "underlying": None,
                                 "error": _revert_reason(e)})
            continue
        if balance == 0 or balance * _LP_DUST < supply:
            continue
        held = (balance * reserve0 // supply, balance * reserve1 // supply)
        rows.append(entry | {
            "decimals": _LP_DECIMALS,
            "raw": str(balance),
            "amount": float(Decimal(balance) / Decimal(10**_LP_DECIMALS)),
            "underlying": [
                {"ticker": sides[i][0], "decimals": sides[i][1], "raw": str(held[i])} for i in order
            ],
        })
    return rows


def _show_counted_tokens(user_id: int, chain_id: int, watched: list[str]):
    """
    Puts every token the limit covers on the dashboard. Run on every wallet read, so it covers the
    deploy's picks, tokens counted later in Controls (or on chain directly), and wallets made before
    the dashboard list existed. The wrapped native token goes on too: it always counts, even on an
    older wallet that doesn't count it yet. A watched address Mitfah doesn't list is skipped -- it
    has no ticker to show (Controls shows it by address).

    @param watched  The wallet's watchedTokens, as read from the chain.
    """
    tickers = _ticker_map(chain_id)
    always = get_always_counted_ticker(chain_id)
    add_dashboard_tokens(
        user_id,
        chain_id,
        [tickers[a.lower()] for a in watched if a.lower() in tickers]
        + ([always] if always in tickers.values() else []),
    )


@app.get("/api/wallet/{chain_id}")
def get_wallet_state(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The read half of the wallet API: everything a dashboard needs, in one call.

    Every other /api/wallet route is a WRITE (prepare -> sign -> confirm), so until this existed a
    front end could change the wallet but not display it -- the only way to read cap, spend or
    paused state was to ask the agent in /api/chat and parse prose.

    Authenticated but NOT gated on `_require_owner`. That check exists so an owner ACTION has an
    address to build a transaction for; reading costs nothing and the wallet row is already keyed by
    the caller's own user_id, so demanding an owner address first would only lock out an account
    from Telegram-only days, whose wallet was created outside the browser flow. `is_owner` reports
    whether the address the account signs in as owns the wallet, so a UI can decide whether to offer
    the owner controls.

    **Reading the session block.** The wallet authorizes ONE session key at a time, held in
    `currentSession`, so `session.wallet_key` is the whole truth about what the wallet trusts -- no
    enumeration problem and no caveat. `session.key` is the key THIS APP holds; `is_app_key` says
    whether they are the same, which is what decides if the assistant can act. `active` additionally
    accounts for expiry, and `needs_renewal` turns true while the key still works, so a UI can prompt
    the owner before the assistant stops rather than after.

    @param chain_id  The chain to read. Must be one this deployment serves.
    @return          Wallet address, owner, paused state, the spending cap and window, session-key
                     status, the loosening knobs, and the balances of the tokens on the dashboard.
    @raises HTTPException 404 if this account has no wallet on `chain_id`.
    """
    # The wallet-row lookup comes FIRST, before _resolve_chain dials anything. Two reasons, the
    # same ones that put the authorization check ahead of the chain resolution in the deploy
    # handlers: a caller with no wallet on this chain should cost no RPC, and the honest answer for
    # them is 404 rather than a 400 about chain configuration they cannot act on. Pure SQLite, so
    # the repeated lookup inside _load_wallet_for_chain below is free.
    try:
        get_wallet_address(user_id, chain_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"You have no wallet on chain {chain_id}.")

    w3, chain_name = _resolve_chain(chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, chain_id)

    # By field name, never position: see contracts.read_spending_config.
    cfg = read_spending_config(wallet)
    remaining = wallet.functions.getRemainingBudget().call()
    tickers = _ticker_map(chain_id)
    _show_counted_tokens(user_id, chain_id, cfg["watchedTokens"])

    # The wallet authorizes ONE key, so it can be read outright -- the old mapping getter could only
    # answer "is THIS key allowed", which is why this endpoint used to have to caveat its own answer.
    wallet_key = wallet.functions.currentSession().call()
    wallet_key = wallet_key if int(wallet_key, 16) else None
    session_expires_at = wallet.functions.currentSessionValidUntil().call()
    # Reconciled BEFORE reporting: a grant or revocation whose confirm never arrived (the owner
    # closed the tab while it mined) is picked up here, the next time anyone looks at the wallet.
    before = get_session_key(user_id, chain_id, wallet.address)
    app_key_row = reconcile_session_key(user_id, chain_id, wallet.address, wallet_key)
    if app_key_row != before:
        invalidate_cache(user_id)
    app_key = w3.to_checksum_address(app_key_row[0]) if app_key_row else None

    on_chain_owner = wallet.functions.owner().call()
    bound_owner = get_user_by_id(user_id)["owner_addr"]

    return {
        "chain_id": chain_id,
        "chain_name": chain_name,
        "address": wallet.address,
        "owner": on_chain_owner,
        # False means the UI should not offer the owner controls: this account has not proved it
        # holds the key those transactions must be signed with.
        "is_owner": bool(bound_owner) and bound_owner == on_chain_owner,
        "paused": wallet.functions.paused().call(),
        "spending": {
            "hook_installed": cfg["installed"],
            "daily_limit_usd": cfg["dailyLimitUsd"] / USD_DECIMALS,
            "spent_usd": cfg["spentInWindow"] / USD_DECIMALS,
            "remaining_usd": remaining / USD_DECIMALS,
            "window_hours": cfg["windowDuration"] / 3600,
            "window_start": cfg["windowStart"],
            # Watched ERC20s are what the cap meters. The native asset is ALWAYS metered and is
            # deliberately absent here -- see SpendingLimitModule. Unknown addresses fall back to
            # the raw address rather than being dropped.
            "watched_tokens": [
                {"ticker": tickers.get(a.lower()), "address": w3.to_checksum_address(a)} for a in cfg["watchedTokens"]
            ],
        },
        # `wallet_key` is whatever key the WALLET authorizes, read straight off currentSession --
        # the wallet holds one at a time, so this is the whole truth, not a guess. `key` is the one
        # this app holds; when they differ the assistant cannot sign and `is_app_key` says so.
        "session": {
            "key": app_key,
            "wallet_key": wallet_key,
            "is_app_key": bool(app_key) and wallet_key == app_key,
            "active": wallet.functions.isSessionActive(app_key).call() if app_key else False,
            "expires_at": session_expires_at or None,
            "expires_in_secs": max(session_expires_at - int(time.time()), 0) if session_expires_at else 0,
            # True once it is worth prompting the owner to renew, while the key still works.
            "needs_renewal": bool(session_expires_at)
            and session_expires_at - int(time.time()) < SESSION_RENEWAL_WARNING_SECS,
        },
        # The loosening knobs, grouped so a UI can present them as such.
        "limits": {
            "max_op_gas_cost_wei": str(wallet.functions.maxOpGasCost().call()),
            "allowlist_enabled": wallet.functions.sessionAllowlistEnabled().call(),
            "trusted_spenders": [w3.to_checksum_address(s) for s in cfg["trustedSpenders"]],
        },
        "balances": _token_balances(w3, user_id, chain_id, wallet.address),
    }


@app.post("/api/wallet/pause/prepare")
def prepare_pause(req: PauseRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `pause()` transaction.

    Pausing stops validation, so a paused wallet rejects UserOps before they reach the EntryPoint and
    pays no gas for the refusal. It is the kill switch for a suspected session-key compromise.

    @return  {"tx": …} for the browser to sign, then POST to /api/wallet/tx/confirm.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    return _prepare_owner_tx(w3, owner, wallet.functions.pause(), req.chain_id)


@app.post("/api/wallet/unpause/prepare")
def prepare_unpause(req: PauseRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `unpause()` transaction.

    @return  {"tx": …} for the browser to sign, then POST to /api/wallet/tx/confirm.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    return _prepare_owner_tx(w3, owner, wallet.functions.unpause(), req.chain_id)


@app.post("/api/wallet/withdraw/prepare")
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


@app.post("/api/wallet/watched-tokens/prepare")
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


@app.post("/api/wallet/tx/confirm")
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
    action: str = Field(pattern="^(add|remove)$")
    # How long the new key should last, in seconds. Only read for `add`. The contract enforces its
    # own MAX_SESSION_TTL ceiling on top of this, so an over-long value reverts rather than silently
    # granting forever.
    ttl_secs: int = Field(default=DEFAULT_SESSION_TTL_SECS, gt=0)
    # NOTE: there is deliberately no `session_key` field. The owner cannot nominate an address:
    # every grant mints a fresh key this app holds, so the wallet and the app can never disagree
    # about who signs. Nominating an outside address only ever cost the user their assistant --
    # the owner can already sign UserOps and call execute() directly, so it bought them nothing.


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


@app.post("/api/wallet/daily-limit/prepare")
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


@app.post("/api/wallet/window-duration/prepare")
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


@app.post("/api/wallet/session/prepare")
def prepare_session_key(req: SessionKeyRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `addSession(key, validUntil)` or `removeSession()` transaction.

    `remove` is the precise kill switch: it cuts off the agent while leaving the owner's own access
    untouched, unlike `pause`, which stops validation for everything including the owner's UserOps.
    Reach for this first if a session key looks compromised.

    `add` mints a BRAND-NEW key and grants it. Never the key already held: a key is revoked exactly
    when it might be compromised, so handing the same address back would undo the revocation. The
    new key is kept in `pending_session_keys` and is NOT used for anything until the transaction
    mines and /api/wallet/session/confirm promotes it — so the assistant keeps working on the old
    key right up to the moment the wallet switches, and a prepared-but-never-signed grant changes
    nothing. The wallet authorizes one key at a time, so the grant also evicts whatever it held.

    What a session key IS, unchanged by any of this: a bare signer that can drive any execute() call,
    bounded by the USD cap, the admin guard, the per-op gas ceiling — and now its own deadline.

    @return  {"tx", "session_key", "valid_until", "replaces"} for `add`;
             {"tx", "revokes"} for `remove`.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    current = wallet.functions.currentSession().call()
    current = current if int(current, 16) else None

    if req.action == "remove":
        if current is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Your wallet on chain {req.chain_id} has no session key to revoke.",
            )
        prepared = _prepare_owner_tx(w3, owner, wallet.functions.removeSession(), req.chain_id)
        return {**prepared, "revokes": current}

    max_ttl = wallet.functions.MAX_SESSION_TTL().call()
    if req.ttl_secs > max_ttl:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A session key may last at most {max_ttl} seconds ({max_ttl // 86_400} days); "
            f"{req.ttl_secs} was requested.",
        )

    session_key, _ = create_pending_session_key(user_id, req.chain_id, wallet.address)
    valid_until = int(time.time()) + req.ttl_secs

    prepared = _prepare_owner_tx(
        w3, owner, wallet.functions.addSession(session_key, valid_until), req.chain_id
    )
    return {
        **prepared,
        "session_key": session_key,
        "valid_until": valid_until,
        # What signing this will revoke. Null on a first grant; otherwise the UI should say so,
        # because one key at a time means granting is also revoking.
        "replaces": current,
    }


@app.post("/api/wallet/session/confirm")
def confirm_session_key(req: TxConfirmRequest, response: Response, user_id: int = Depends(get_current_user)):
    """
    Waits for a session grant or revocation to mine, then makes wallet.db match the chain.

    Separate from /api/wallet/tx/confirm — which exists for the actions that write NOTHING locally
    — because this one has rows to move, and moving them before the transaction mines is exactly
    how the app and the wallet drift apart.

    The chain is the source of truth here, not the request: this reads `currentSession` back and
    reconciles against it, so a client cannot talk the app into filing a key the wallet never
    authorized.

      - matches the pending key -> promote it; the assistant now signs with it
      - zero                    -> the revocation landed; forget the key entirely, ciphertext and all
      - anything else           -> leave the rows alone and report the drift

    Answers 202 while the transaction is still pending, with `seen` as in /api/wallet/tx/confirm;
    the front end polls until it gets a 200.

    @return  {"status", "tx_hash", "session"} once mined.
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
    # one user cannot report another's hash and drive state through this endpoint.
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

    on_chain = wallet.functions.currentSession().call()
    on_chain = on_chain if int(on_chain, 16) else None
    # Shared with the wallet read, which may have got here first: the outcome is therefore read off
    # the state left behind, not off which branch ran. A revocation drops the ciphertext as well as
    # the reference, so a key revoked because it leaked can never be granted again.
    app_key_row = reconcile_session_key(user_id, req.chain_id, wallet.address, on_chain)
    app_key = w3.to_checksum_address(app_key_row[0]) if app_key_row else None
    if on_chain is None:
        outcome = "revoked"
    elif app_key == on_chain:
        outcome = "granted"
    else:
        # The wallet authorizes a key this app did not mint -- an owner acting outside the app, or a
        # grant confirmed against the wrong transaction. Nothing was touched; say so.
        outcome = "unrecognized_key"

    invalidate_cache(user_id)
    expires_at = wallet.functions.currentSessionValidUntil().call()

    return {
        "status": outcome,
        "tx_hash": req.tx_hash,
        "session": {
            "key": app_key,
            "wallet_key": on_chain,
            "is_app_key": bool(app_key) and app_key == on_chain,
            "expires_at": expires_at or None,
        },
    }


@app.post("/api/wallet/trusted-spenders/prepare")
def prepare_trusted_spender(req: TrustedSpenderRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `addTrustedSpender(spender)` or `removeTrustedSpender(spender)` transaction.

    A trusted spender may be granted an approval for a token the oracle cannot price. That exemption
    exists so routers work with unpriced tokens; it is also a hole in the metering, so the list is
    capped at 16 and the deploy seeds only this deployment's router.

    Adding one is a genuine loosening of the spending controls — treat it as such in the UI.

    Removing the chain's exchange router is refused: the agent's liquidity removal approves the
    router for an LP token the oracle cannot price, which only a trusted spender may receive, so
    without it every remove_liquidity reverts. The owner can still call removeTrustedSpender on the
    wallet directly — this stops an accidental removal from the app, not a deliberate one.

    @return  {"tx", "spender"}.
    """
    owner = _require_owner(user_id)

    try:
        spender = Web3.to_checksum_address(req.spender)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.spender}")

    # Before _resolve_chain, like the owner check: a request that can only be refused should not
    # cost an RPC round trip.
    if req.action == "remove" and spender == _router_or_none(req.chain_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Your exchange's router can't be removed: the assistant needs it to remove liquidity.",
        )

    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    fn = (
        wallet.functions.addTrustedSpender(spender)
        if req.action == "add"
        else wallet.functions.removeTrustedSpender(spender)
    )
    return {**_prepare_owner_tx(w3, owner, fn, req.chain_id), "spender": spender}


@app.post("/api/wallet/max-op-gas-cost/prepare")
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


# ── Contract allowlist ────────────────────────────────────────────────────────
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


@app.post("/api/wallet/allowlist/prepare")
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


@app.get("/api/wallet/{chain_id}/allowlist")
def get_allowlist(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The wallet's contract allowlist, each entry named where Mitfah knows it, and the contracts worth
    listing that aren't yet. Read only when Controls → Advanced is opened, so the dashboard's wallet
    read doesn't pay for it.

    `suggested` is what the assistant calls for this account, in this order: the exchange router,
    the tokens on the dashboard (the wrapped native one included, then the ones added by address),
    the pools it deposited into (taking liquidity out calls the pool's own LP token), the ERC-8004
    review registry, and any contact whose address holds code. A contact with a plain wallet needs
    no entry -- while the list is on, an address with no code is never blocked.

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

    router = _router_or_none(chain_id)
    if router:
        name(router, "PancakeSwap V2 router" if chain_id == CHAIN_ID_BSC else "Uniswap V2 router", True)
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
    # Named like the pool's dashboard row, the native asset's side first: "ETH/USDC pool".
    native = get_native_asset_ticker(chain_id).lower()
    for pool in _known_pools(w3, user_id, chain_id):
        sides = (pool["ticker0"], pool["ticker1"])
        if pool["ticker1"] == native:
            sides = sides[::-1]
        name(pool["pair"], "/".join(side.upper() for side in sides) + " pool", True)
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
