from web3 import Web3
from web3.providers.rpc import HTTPProvider
from db import get_rpc_url, get_chain_id_from_name, get_user_network


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
