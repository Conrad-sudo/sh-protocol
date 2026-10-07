import os

from dotenv import load_dotenv
from web3 import Web3
from web3.providers.rpc import HTTPProvider
from constants import (
    CHAIN_ID_ANVIL,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BASE,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
)
from db import get_rpc_url, get_chain_id_from_name, get_user_network

load_dotenv()

# The API and the Telegram bots speak chain IDs: a browser wallet reports one (eth_chainId), and
# each bot serves one. Everything downstream of them speaks chain NAMES: save_user_network stores
# one, and load_network_config, contracts.py, tools.py and tx_sender all read it back. This map is
# the only place the two meet.
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
# and a request must not be able to choose. The API and the Telegram bots read the same switch, so
# a bot acts on the same network the web app shows for its chain.
FORK_MODE = os.getenv("APP_FORK_MODE", "").lower() in ("1", "true", "yes")


def network_name(chain_id: int) -> str:
    """
    This server's network name for `chain_id`: its `-fork` twin in fork mode. RPC-free.

    @param chain_id  A chain ID.
    @return          The network name, e.g. "base", or "base-fork" in fork mode.
    @raises ValueError  For a chain this server does not serve.
    """
    chain_name = CHAIN_NAME_BY_ID.get(chain_id)
    if chain_name is None:
        raise ValueError(f"Unsupported chain ID: {chain_id}. Supported: {sorted(CHAIN_NAME_BY_ID)}")
    if FORK_MODE and chain_id in FORKABLE_CHAIN_IDS:
        chain_name = f"{chain_name}-fork"
    return chain_name


class _ChainIdOnceProvider(HTTPProvider):
    """
    An HTTPProvider that asks the node for its chain id once, instead of before every call.

    web3's validation middleware reads `w3.eth.chain_id` before every eth_call and eth_estimateGas
    (twice per call, in practice), so most of the requests the app sent were that same question:
    22 of the 37 in quoting one transfer. On a live RPC each one is a full round trip. The chain id
    behind an RPC URL never changes, so the node's first answer is kept and reused -- its real
    answer, not the one in the database, so the middleware's check still means something.

    Not web3's own request cache: that keys its entries by thread, and LangGraph runs each turn's
    tools on fresh threads, so it would still ask about once per tool call.
    """

    _chain_id_response: dict | None = None

    def make_request(self, method, params):
        if method != "eth_chainId":
            return super().make_request(method, params)
        if self._chain_id_response is None:
            response = super().make_request(method, params)
            if "error" in response or response.get("result") is None:
                return response
            self._chain_id_response = response
        return dict(self._chain_id_response)

# One cached {"instance": Web3, "chain_id": int} per chain_name, so the underlying
# HTTP keep-alive pool is reused instead of a fresh session being built on every
# lookup. Shared by both loaders below (both key by chain_name). web3.py's
# HTTPProvider is safe to share across the bot's worker threads (it sends over a
# thread-safe requests.Session). RPC URLs are static config; restart to pick up a
# changed rpcs table.
_web3_cache: dict[str, dict] = {}


def load_network_config(user_id: int) -> tuple[Web3, int, str]:
    """
    Initializes and returns a Web3 instance connected to the RPC URL for the
    specified chain, along with the chain ID. Both values are looked up from
    the chains and rpcs tables in wallet.db.

    @param user_id  The application user ID.
    @return            A tuple of (Web3 instance, chain_id, chain_name).
    """

    
    chain_name = get_user_network(user_id)
    if chain_name is None:
        raise ValueError(f"No network configured for user {user_id}. Deploy first.")
    
    if chain_name not in _web3_cache:

        rpc_url = get_rpc_url(chain_name)
        chain_id = get_chain_id_from_name(chain_name)

        if rpc_url is None or chain_id is None:
            raise ValueError(f"Chain name '{chain_name}' not found in database")

        instance = Web3(_ChainIdOnceProvider(rpc_url))

        
        _web3_cache[chain_name] = {
            "instance": instance,
            "chain_id": chain_id
        }
    return _web3_cache[chain_name]["instance"], _web3_cache[chain_name]["chain_id"], chain_name

    
    


def load_network_config_by_name(chain_name: str) -> tuple[Web3, int]:
    """
    Initializes and returns a Web3 instance connected to the RPC URL for the
    specified chain, along with the chain ID. Both values are looked up from
    the chains and rpcs tables in wallet.db.

    @param chain_name  The name of the blockchain network.
    @return            A tuple of (Web3 instance, chain_id).
    """

    if chain_name not in _web3_cache:
        rpc_url = get_rpc_url(chain_name)
        chain_id = get_chain_id_from_name(chain_name)

        if rpc_url is None or chain_id is None:
            raise ValueError(f"Chain name '{chain_name}' not found in database")

        _web3_cache[chain_name] = {
            "instance": Web3(_ChainIdOnceProvider(rpc_url)),
            "chain_id": chain_id,
        }
    return _web3_cache[chain_name]["instance"], _web3_cache[chain_name]["chain_id"]
