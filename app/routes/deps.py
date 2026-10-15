"""
What every router shares: the rate limiter, resolving the chain a request names, loading the
caller's wallet, and turning a failed transaction or a revert into a message a user can act on.
"""
import logging

from dotenv import load_dotenv
from eth_utils import to_hex
from fastapi import HTTPException, status
from hexbytes import HexBytes
from slowapi import Limiter
from slowapi.util import get_remote_address
from web3 import Web3
from web3.exceptions import TransactionNotFound

from contract_errors import name_revert
from db import (
    get_json,
    get_user_by_id,
    get_wallet_address,
)
from network_config import (
    load_network_config_by_name,
    network_name,
)
from redact import redact

# Routers read settings from the environment when they are imported, and every one imports this
# module, so .env is loaded here rather than left to whichever entry point imported them.
load_dotenv()

logger = logging.getLogger(__name__)


# Rate limiting, applied to the open sign-in endpoints, the ones that issue nonces, and the chat
# (per account rather than per address there: see CHAT_RATE_LIMIT).
limiter = Limiter(key_func=get_remote_address)


# How long a granted session key lasts when the client sends no ttl (30 days). SessionHandler caps
# any grant at its own MAX_SESSION_TTL; this is the shorter figure the app actually asks for, so a
# key nobody renews dies on its own. Renewal is one owner-signed transaction.
DEFAULT_SESSION_TTL_SECS = 30 * 86_400


# SpendingLimitModule's cap is an 18-decimal USD value. The API takes whole dollars and scales here,
# so the front end never has to hold a 10**18-sized integer (see _to_json_tx for why that matters).
USD_DECIMALS = 10**18


# How long a confirm (/api/deploy/confirm, the owner confirms, a deposit) waits for the user's
# transaction before answering "still pending". Short, because it holds a worker thread: the front
# end polls the same endpoint again.
CONFIRM_POLL_TIMEOUT_SECS = 20


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


def _network_name(chain_id: int) -> str:
    """
    This server's network name for `chain_id` -- its `-fork` twin in fork mode -- or 400s. RPC-free.

    See network_config.CHAIN_NAME_BY_ID for why the name is resolved here and never taken from the
    request.
    """
    try:
        return network_name(chain_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


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
        # The exception names the RPC's URL, which carries the provider's key, so only the log
        # gets it, masked; the client is told which network is down and nothing more.
        logger.warning("Cannot reach the RPC for %s: %s", chain_name, redact(e))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Cannot reach the RPC for {chain_name}.")
    if live_id != chain_id:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"The RPC configured for '{chain_name}' reports chain {live_id}, not the requested "
            f"{chain_id}. Check the rpcs table and APP_FORK_MODE.",
        )
    return w3, chain_name


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


def _revert_reason(e: Exception) -> str:
    """
    Turns a web3 revert into something worth showing a user.

    The whole value of simulating before signing is the message, so a bare `0x4c0a7758` is a failed
    simulation as far as the UI is concerned. Any 4-byte selector in the exception text is swapped
    for its error signature; anything unrecognised falls through as the original first line, which
    is still better than nothing. That line is masked first: when the node itself failed rather than
    the contract, it is a network error naming the RPC's URL, key and all.
    """
    message = redact(str(e).split("\n")[0].strip()) or e.__class__.__name__
    return name_revert(message) or message
