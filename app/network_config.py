from web3 import Web3
from db import get_rpc_url, get_chain_id_from_name, get_user_network

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

        instance=Web3(Web3.HTTPProvider(rpc_url))

        
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
            "instance": Web3(Web3.HTTPProvider(rpc_url)),
            "chain_id": chain_id,
        }
    return _web3_cache[chain_name]["instance"], _web3_cache[chain_name]["chain_id"]
