from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


import os
import secrets
import sys
import time
from decimal import Decimal

from eth_utils import keccak, to_hex
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from web3.logs import DISCARD
from constants import (
    CHAIN_ID_ANVIL,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
    ETH_SENTINEL,
    get_native_asset_ticker,
    get_native_wrapped_ticker,
    get_router,
)
from network_config import load_network_config_by_name, load_network_config
from db import (
    get_json,
    get_factory_address,
    get_session_key,
    get_supported_tokens_by_chain_id,
    get_token_address,
    save_wallet_address,
    save_user_network,
    save_contact,
    get_contact,
    get_all_contacts,
    delete_contact,
    save_session_key,
    get_wallet_address,
    get_wallet_chains,
    create_user,
    get_user_by_id,
    get_user_by_email,
    get_user_by_google_sub,
    get_user_by_owner_addr,
    link_google,
    link_owner_addr,
    link_telegram,
    unlink_telegram,
    save_telegram_link_nonce,
    set_password_hash,
    revoke_all_refresh_tokens,
)
from userop import get_or_create_session_key
from tx_sender import send_and_confirm
from contracts import (
    invalidate_cache,
    load_factory,
    load_session_handler,
    load_ierc20,
)
import auth
from auth import get_current_user
from smart_wallet_agent import (
    chat,
    close_checkpointer,
    get_history,
    init_agent,
    open_checkpointer,
)

from langchain_erc20 import ERC20_ABI
from langchain_erc20.amounts import to_base_units

from pydantic import BaseModel, EmailStr, Field


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

# Rate limiting, applied to the endpoints that guess-able credentials would be thrown at.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
# SpendingLimitModule's cap is an 18-decimal USD value. The API takes whole dollars and scales here,
# so the front end never has to hold a 10**18-sized integer (see _to_json_tx for why that matters).
USD_DECIMALS = 10**18
# How long /api/deploy/confirm waits for the user's transaction before answering "still pending".
# Short, because it holds a worker thread: the front end polls the same endpoint again.
CONFIRM_POLL_TIMEOUT_SECS = 20

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
}
# Chains whose live name has a local `-fork` twin. Anvil is absent: it is already local and has no
# live counterpart to fork.
FORKABLE_CHAIN_IDS = {
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_ARBITRUM,
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
    chain_name = CHAIN_NAME_BY_ID.get(chain_id)
    if chain_name is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported chain ID: {chain_id}. Supported: {sorted(CHAIN_NAME_BY_ID)}",
        )
    if FORK_MODE and chain_id in FORKABLE_CHAIN_IDS:
        chain_name = f"{chain_name}-fork"

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


class SignupRequest(BaseModel):
    """Body of POST /api/auth/signup."""

    email: EmailStr
    # 8 is a floor, not a policy: length is what matters for Argon2, and composition rules mostly
    # push users toward predictable substitutions. The cap stops a megabyte of input reaching the
    # hasher, which is CPU-bound by design and would otherwise be a cheap way to burn the server.
    password: str = Field(min_length=8, max_length=1024)


class LoginRequest(BaseModel):
    """Body of POST /api/auth/login."""

    email: EmailStr
    password: str = Field(max_length=1024)


class GoogleRequest(BaseModel):
    """Body of POST /api/auth/google — the ID token from Google Identity Services."""

    id_token: str


class SiweVerifyRequest(BaseModel):
    """Body of POST /api/auth/siwe/verify."""

    message: str
    signature: str
    nonce: str


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


@app.post("/api/auth/signup", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def signup(request: Request, req: SignupRequest, response: Response):
    """
    Creates an account from an email and password and signs it in.

    @param req  The email and password.
    @return     An access token; the refresh token is set as a cookie.
    """
    email=req.email.lower()
    if get_user_by_email(email):
        # Deliberately explicit. Signup is where an enumeration-proof answer costs the most (the
        # user needs to know to log in instead), and login/reset already reveal nothing.
        #raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists.")
        raise HTTPException(status.HTTP_409_CONFLICT,"An account with that email already exists")
    user_id = create_user(email=email, password_hash=auth.hash_password(req.password))
    return _issue_session(response, user_id)


@app.post("/api/auth/login")
@limiter.limit("10/minute")
def login(request: Request, req: LoginRequest, response: Response):
    """
    Signs in with an email and password.

    The same 401 answers a wrong password and an unknown account, so the endpoint cannot be used
    to discover which emails have accounts. The password is verified even when no account was
    found, so the response time does not give the same thing away.

    @param req  The email and password.
    @return     An access token; the refresh token is set as a cookie.
    """
    user = get_user_by_email(req.email.lower())
    stored_hash = user["password_hash"] if user else None
    if not auth.verify_password(stored_hash, req.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    # Upgrade the stored hash if the cost parameters have moved on since it was written.
    if auth.needs_rehash(stored_hash):
        set_password_hash(user["id"], auth.hash_password(req.password))

    return _issue_session(response, user["id"])


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


@app.post("/api/auth/google")
@limiter.limit("10/minute")
def google_sign_in(request: Request, req: GoogleRequest, response: Response):
    """
    Signs in with a Google ID token, creating the account on first use.

    Accounts are keyed on Google's `sub`, never on the email: an email can move between Google
    accounts, and matching on it would let whoever holds the address today take over the account.

    **A Google sign-in never merges into an existing password account of the same email.** Doing so
    silently would mean anyone who can get Google to assert an address could take over the password
    account behind it. Linking is possible, but only from the other direction: sign in with the
    password first, then attach Google from settings.

    @param req  The ID token from Google Identity Services.
    @return     An access token; the refresh token is set as a cookie.
    """
    google_sub, email = auth.verify_google_id_token(req.id_token)

    user = get_user_by_google_sub(google_sub)
    if user:
        return _issue_session(response, user["id"])

    if email and get_user_by_email(email):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with that email already exists. Sign in with your password, then link "
            "Google from your account settings.",
        )

    return _issue_session(response, create_user(email=email, google_sub=google_sub))


@app.post("/api/auth/google/link")
def link_google_account(req: GoogleRequest, user_id: int = Depends(get_current_user)):
    """
    Attaches a Google account to the signed-in account.

    The safe direction of the linking rule above: the caller has already proved they hold this
    account, so binding Google to it grants nothing they did not already have.
    """
    google_sub, _ = auth.verify_google_id_token(req.id_token)
    if get_user_by_google_sub(google_sub):
        raise HTTPException(status.HTTP_409_CONFLICT, "That Google account is already linked.")
    try:
        link_google(user_id, google_sub)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return {"status": "linked"}


@app.get("/api/auth/siwe/nonce")
def siwe_nonce():
    """
    Issues a nonce for the SIWE message the user is about to sign.

    @return  {"nonce": str} — include it verbatim in the message.
    """
    return {"nonce": auth.issue_siwe_nonce()}


@app.post("/api/auth/siwe/verify")
def siwe_verify(req: SiweVerifyRequest, user_id: int = Depends(get_current_user)):
    """
    Binds the EOA that signed a SIWE message to the signed-in account.

    This address is the one that will OWN the SessionHandler on chain: deployWallet sets
    owner = msg.sender, and only transferOwnership signed by that address can ever change it. The
    app has no authority over it, so if the user loses the key, account recovery cannot restore
    control of the wallet. Say so in the UI at bind time.

    @param req  The signed message, its signature, and the nonce it carries.
    @return     {"owner_addr": str} — the bound address.
    """
    address = auth.verify_siwe(req.message, req.signature, req.nonce)

    existing = get_user_by_owner_addr(address)
    if existing and existing["id"] != user_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That address is already linked to another account."
        )
    try:
        link_owner_addr(user_id, address)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return {"owner_addr": address}


@app.get("/api/me")
def me(user_id: int = Depends(get_current_user)):
    """
    Returns the signed-in account, without anything secret.

    @return  The account's id, email, bound EOA, which sign-in methods it has, whether Telegram is
             linked, and its wallets.
    """
    user = get_user_by_id(user_id)
    return {
        "user_id": user["id"],
        "email": user["email"],
        "owner_addr": user["owner_addr"],
        # A Google-created account has no password; the settings page says so rather than implying
        # an email login that would always fail.
        "has_password": user["password_hash"] is not None,
        "google_linked": user["google_sub"] is not None,
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
# and can talk to the agent, but adding a payee now takes the web credential they do not have.
# The residual is stated plainly: they can still move up to the remaining cap to contacts the
# owner already saved, which is worth little to a thief. Revoking the session key
# (POST /api/wallet/session/prepare) is the response to a lost device.
#
# DELETE lives here too, and not because deleting could steal anything -- it only ever shrinks the
# allowlist. It is here so the rule stays a single sentence: the agent READS the contact list and
# never writes it. "May write, but only destructively" is the kind of distinction that gets
# re-derived wrong later. Reads stay on the agent; they create no new destination.


class ContactRequest(BaseModel):
    """Body of POST /api/contacts."""

    # Names index a lowercase column and are what the user types at the agent, so they are kept
    # short and free of the characters that would make them awkward to name back ("/" would also
    # collide with the delete route's path parameter).
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^/\\\x00-\x1f]+$")
    address: str


@app.post("/api/contacts", status_code=status.HTTP_201_CREATED)
def create_contact(req: ContactRequest, user_id: int = Depends(get_current_user)):
    """
    Saves or updates a contact for the signed-in account. See the note above on why this is here.

    The address is checksummed before storage rather than trusted as typed: it is stored once and
    then read back as a transaction destination for as long as the contact exists, so a typo
    caught here is a transaction that never gets built, while one stored raw surfaces much later
    as an opaque failure deep in the calldata builder -- or, if it happens to be valid, as funds
    sent somewhere real.

    @param req  The contact name and its Ethereum address.
    @return     The stored contact, with the address in checksummed form.
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
    try:
        address = Web3.to_checksum_address(req.address)
    except (ValueError, TypeError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{req.address}' is not an Ethereum address"
        )

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
    message says can change whose wallet is acted on.

    @param req  The chain and the user's message.
    @return     {"reply": str}
    """
    return {"reply": chat(user_id, req.chain_id, req.message)}


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


def _require_own_deployer(user_id: int, deployer: str):
    """
    Refuses a deploy unless the deploying EOA is the one this account proved it holds via SIWE.

    Without this the deployer is just an address in the request body, and two things go wrong.
    A caller can name somebody else's EOA, which mints a session key for a wallet they will never
    own; and, given another user's deploy transaction, they can call /api/deploy/confirm for it and
    register that wallet against their own account. The stolen row cannot SPEND -- the key seeded
    into the real deploy is the owner's, so the attacker's key is never authorized -- but their
    agent would happily read the victim's balances through it.

    Requiring the SIWE binding first also matches the order the UI wants anyway: connect wallet,
    prove it, then deploy.

    @param user_id   The authenticated account.
    @param deployer  The checksummed EOA the request wants to deploy from.
    @raises HTTPException 403 if no address is bound, or a different one is.
    """
    owner_addr = get_user_by_id(user_id)["owner_addr"]
    if not owner_addr:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Link your wallet address first: GET /api/auth/siwe/nonce, sign the message, then "
            "POST /api/auth/siwe/verify.",
        )
    if owner_addr != deployer:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"This account is linked to {owner_addr}, not {deployer}. Switch accounts in your "
            "wallet, or link the new address first.",
        )


@app.get("/api/chains")
def list_chains():
    """
    Lists the chains a user can deploy a wallet on with this server.

    A chain qualifies when this deployment serves it (CHAIN_NAME_BY_ID) AND the protocol is deployed
    there (a `factory` row). The front end builds its network picker from this rather than from a
    list of its own that would drift from the server's.

    Public and RPC-free, like /api/tokens: it reads two local tables and says nothing that is not
    already public on chain.

    @return  {"chains": [{"chain_id", "name", "native_ticker", "fork"}, ...]}, by chain ID.
             `fork` is true when this server points that chain at a local fork (APP_FORK_MODE).
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
        chains.append({
            "chain_id": chain_id,
            "name": name,
            "native_ticker": native_ticker,
            "fork": FORK_MODE and chain_id in FORKABLE_CHAIN_IDS,
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
    """
    try:
        return {"chain_id": chain_id, "tokens": get_supported_tokens_by_chain_id(chain_id)}
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


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
    session_key, _ = get_or_create_session_key(user_id, chain_id, predicted)

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

    Answers 202 while the transaction is still pending; the front end polls until it gets a 200.

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

    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash}

    if receipt["status"] != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"deployWallet reverted (tx: {req.tx_hash})")

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

    # The prediction can go stale: deployCount is per-owner, so a second deploy by this same user
    # between /api/deploy and their signature moves the address. The key seeded into initialize() is
    # still the one minted against `predicted`, so MOVE the row rather than minting a new key — a
    # fresh key would not be authorized on chain and every tool would fail to sign.
    row = get_session_key(user_id, chain_id, predicted)
    if row is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No session key held for predicted address {predicted}; call /api/deploy first.",
        )
    session_key, ciphertext = row
    if wallet_address != predicted:
        print(f"Predicted {predicted} but the deploy landed at {wallet_address}; re-pointing session key.")
        save_session_key(user_id, chain_id, wallet_address, session_key, ciphertext)

    # Verify on chain that the key we hold can actually sign for this wallet, rather than assuming
    # initialize() seeded what was passed.
    handler_abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    authorized = w3.eth.contract(address=wallet_address, abi=handler_abi).functions.allowedSession(
        session_key
    ).call()

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
    # Ticker, or "eth"/the chain's native ticker to withdraw native value.
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
    @raises HTTPException 403 if the account has not completed the SIWE binding.
    """
    owner_addr = get_user_by_id(user_id)["owner_addr"]
    if not owner_addr:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Link your wallet address first: GET /api/auth/siwe/nonce, sign the message, then "
            "POST /api/auth/siwe/verify.",
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


def _error_selectors() -> dict[str, str]:
    """
    Builds a 4-byte-selector -> error-signature map from the wallet and module ABIs.

    Solidity custom errors reach web3 as a bare selector like `0x4c0a7758`, which is useless in a
    UI. The ABIs carry every error's name and argument types, and the selector is just
    keccak(signature)[:4], so the mapping can be rebuilt locally with no chain access.

    Both ABIs are needed: a reverting owner call may fail in SessionHandler itself
    (SessionHandler_NotEnoughBalance) or inside the module it forwards to
    (SpendingLimitModule_TokenNotPriced).
    """
    selectors: dict[str, str] = {}
    for path in (
        "./out/SessionHandler.sol/SessionHandler.json",
        "./out/SpendingLimitModule.sol/SpendingLimitModule.json",
    ):
        try:
            abi = get_json(path)["abi"]
        except (FileNotFoundError, KeyError):
            continue  # not built — fall back to the raw selector rather than failing the request
        for entry in abi:
            if entry.get("type") != "error":
                continue
            signature = f"{entry['name']}({','.join(i['type'] for i in entry['inputs'])})"
            selectors["0x" + keccak(text=signature)[:4].hex()] = signature
    return selectors


# Built once at import: the ABIs do not change while the process runs.
ERROR_SELECTORS = _error_selectors()


def _revert_reason(e: Exception) -> str:
    """
    Turns a web3 revert into something worth showing a user.

    The whole value of simulating before signing is the message, so a bare `0x4c0a7758` is a failed
    simulation as far as the UI is concerned. Any 4-byte selector in the exception text is swapped
    for its error signature; anything unrecognised falls through as the original first line, which
    is still better than nothing.
    """
    message = str(e).split("\n")[0].strip() or e.__class__.__name__
    for selector, signature in ERROR_SELECTORS.items():
        if selector in message:
            return signature
    return message


def _resolve_withdraw_token(w3: Web3, chain_id: int, token: str) -> tuple[str, int]:
    """
    Resolves a withdraw ticker to (token address, decimals).

    Native value is `address(0)` with 18 decimals — the same sentinel SessionHandler.withdraw,
    SHOracle and the agent's tools all use for the chain's gas asset.

    @return  (address, decimals).
    @raises HTTPException 400 if the ticker is not listed on this chain.
    """
    # "eth" is this codebase's generic label for the native gas asset on every chain (see
    # ETH_SENTINEL, get_eth_balance, send_eth), so it is accepted everywhere; the chain's real
    # ticker is accepted too, for a UI that shows "BNB" or "CELO".
    if token.lower() in ("eth", get_native_asset_ticker(chain_id).lower()):
        return ETH_SENTINEL, 18
    try:
        address = w3.to_checksum_address(get_token_address(chain_id, token))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    decimals = w3.eth.contract(address=address, abi=ERC20_ABI).functions.decimals().call()
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


def _token_balances(w3: Web3, chain_id: int, account: str) -> list[dict]:
    """
    Native + listed-ERC20 balances for `account`.

    Each token is fetched independently and a failure is reported per-token rather than raised: one
    token with no code (a stale row, a chain that moved a deployment) must not blank out the whole
    dashboard. Raw amounts are strings -- a uint256 of wei does not survive JSON's float64.

    Two eth_calls per token (decimals + balanceOf), which is fine for the handful of tickers seeded
    per chain; if that list ever grows to hundreds, batch it or cache decimals.
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
    for address, ticker in _ticker_map(chain_id).items():
        entry = {"ticker": ticker, "address": w3.to_checksum_address(address), "native": False}
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
    return balances


@app.get("/api/wallet/{chain_id}")
def get_wallet_state(chain_id: int, user_id: int = Depends(get_current_user)):
    """
    The read half of the wallet API: everything a dashboard needs, in one call.

    Every other /api/wallet route is a WRITE (prepare -> sign -> confirm), so until this existed a
    front end could change the wallet but not display it -- the only way to read cap, spend or
    paused state was to ask the agent in /api/chat and parse prose.

    Authenticated but NOT gated on `_require_owner`. That check exists so an owner ACTION has an
    address to build a transaction for; reading costs nothing and the wallet row is already keyed by
    the caller's own user_id, so demanding a SIWE binding first would only lock out an account whose
    wallet was created outside the browser flow. `is_owner` reports the binding instead, so a UI can
    decide whether to offer the owner controls.

    **What `session.active` does not tell you.** `SessionHandler.allowedSession` is a mapping getter
    with no enumeration, so there is no way to ask the wallet which keys it authorizes -- only whether
    a given one is authorized. This reports on the key THIS APP holds. If the owner authorized a key
    of their own directly, `session.key` is null and `session.active` is false while the wallet does
    in fact have a live session key. A UI should read this as "the assistant can/cannot act", not as
    "the wallet has no active session".

    @param chain_id  The chain to read. Must be one this deployment serves.
    @return          Wallet address, owner, paused state, the spending cap and window, session-key
                     status, the loosening knobs, and native + listed-ERC20 balances.
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

    # (installed, windowStart, windowDuration, dailyLimitUsd, spentInWindow, watchedTokens,
    #  trustedSpenders) -- mirrors tools._get_all_sessions, which reads the same tuple.
    cfg = wallet.functions.getConfig().call()
    remaining = wallet.functions.getRemainingBudget().call()
    tickers = _ticker_map(chain_id)

    app_key_row = get_session_key(user_id, chain_id, wallet.address)
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
            "hook_installed": cfg[0],
            "daily_limit_usd": cfg[3] / USD_DECIMALS,
            "spent_usd": cfg[4] / USD_DECIMALS,
            "remaining_usd": remaining / USD_DECIMALS,
            "window_hours": cfg[2] / 3600,
            "window_start": cfg[1],
            # Watched ERC20s are what the cap meters. The native asset is ALWAYS metered and is
            # deliberately absent here -- see SpendingLimitModule. Unknown addresses fall back to
            # the raw address rather than being dropped.
            "watched_tokens": [
                {"ticker": tickers.get(a.lower()), "address": w3.to_checksum_address(a)} for a in cfg[5]
            ],
        },
        # `key` null means this app holds no session key for the wallet, so the assistant cannot
        # sign for it. See the docstring for what `active` does NOT cover.
        "session": {
            "key": app_key,
            "active": wallet.functions.allowedSession(app_key).call() if app_key else False,
        },
        # The loosening knobs, grouped so a UI can present them as such.
        "limits": {
            "max_op_gas_cost_wei": str(wallet.functions.maxOpGasCost().call()),
            "allowlist_enabled": wallet.functions.sessionAllowlistEnabled().call(),
            "trusted_spenders": [w3.to_checksum_address(s) for s in cfg[6]],
        },
        "balances": _token_balances(w3, chain_id, wallet.address),
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

    token_address, decimals = _resolve_withdraw_token(w3, req.chain_id, req.token)
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

    @return  {"tx", "token_address"}.
    """
    owner = _require_owner(user_id)
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

    @return  {"status", "tx_hash", "wallet_state"} once mined.
    """
    _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    try:
        receipt = w3.eth.wait_for_transaction_receipt(req.tx_hash, timeout=CONFIRM_POLL_TIMEOUT_SECS)
    except (TimeExhausted, TransactionNotFound):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "pending", "tx_hash": req.tx_hash}

    # Only accept a transaction that was actually sent TO this user's wallet, so one user cannot
    # report another's transaction hash and read state back through this endpoint.
    if receipt["to"] is None or w3.to_checksum_address(receipt["to"]) != wallet.address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Transaction {req.tx_hash} was not sent to your wallet on chain {req.chain_id}.",
        )

    if receipt["status"] != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"That transaction reverted (tx: {req.tx_hash})"
        )

    config = wallet.functions.getConfig().call()
    return {
        "status": "confirmed",
        "tx_hash": req.tx_hash,
        "wallet_state": {
            "paused": wallet.functions.paused().call(),
            "watched_tokens": config[5],
            "daily_limit_usd": config[3] / USD_DECIMALS,
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
    # Optional. Left out, it defaults to the key this app holds for the wallet on this chain, which
    # is the case that matters: revoking the assistant, then granting it back.
    session_key: str | None = None


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
    Builds the unsigned `addSession(key)` or `removeSession(key)` transaction.

    `remove` is the precise kill switch: it cuts off the agent while leaving the owner's own access
    untouched, unlike `pause`, which stops validation for everything including the owner's UserOps.
    Reach for this first if a session key looks compromised.

    `add` re-authorizes. Note what a session key IS: a bare signer that can drive any execute() call,
    bounded only by the USD cap and the admin guard — not scoped to particular targets or selectors,
    and with no expiry. So granting one to an address this app does not hold is meaningful and
    irreversible-ish; the response flags that case rather than silently allowing it to look normal.

    Defaults to the key this app holds for the wallet on this chain, which covers revoke-then-restore.

    @return  {"tx", "session_key", "is_app_key"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)

    app_key_row = get_session_key(user_id, req.chain_id, wallet.address)
    app_key = w3.to_checksum_address(app_key_row[0]) if app_key_row else None

    if req.session_key:
        try:
            session_key = w3.to_checksum_address(req.session_key)
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.session_key}"
            )
    elif app_key:
        session_key = app_key
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This app holds no session key for your wallet on chain {req.chain_id}. Pass "
            f"session_key explicitly if you mean to authorize a key you manage yourself.",
        )

    fn = (
        wallet.functions.addSession(session_key)
        if req.action == "add"
        else wallet.functions.removeSession(session_key)
    )
    prepared = _prepare_owner_tx(w3, owner, fn, req.chain_id)
    return {
        **prepared,
        "session_key": session_key,
        # False means the assistant cannot sign with this key -- it has no ciphertext for it. Worth
        # surfacing in the UI, since an `add` of a foreign key looks identical to a normal one.
        "is_app_key": session_key == app_key,
    }


@app.post("/api/wallet/trusted-spenders/prepare")
def prepare_trusted_spender(req: TrustedSpenderRequest, user_id: int = Depends(get_current_user)):
    """
    Builds the unsigned `addTrustedSpender(spender)` or `removeTrustedSpender(spender)` transaction.

    A trusted spender may be granted an approval for a token the oracle cannot price. That exemption
    exists so routers work with unpriced tokens; it is also a hole in the metering, so the list is
    capped at 16 and the deploy seeds only this deployment's router.

    Adding one is a genuine loosening of the spending controls — treat it as such in the UI.

    @return  {"tx", "spender"}.
    """
    owner = _require_owner(user_id)
    w3, _ = _resolve_chain(req.chain_id)

    try:
        spender = w3.to_checksum_address(req.spender)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a valid address: {req.spender}")

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
