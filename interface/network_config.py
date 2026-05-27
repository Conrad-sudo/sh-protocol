from web3 import Web3
from db import get_rpc_url, get_chain_id_from_name, get_user_network


def load_network_config(chat_id: int) -> tuple[Web3, int, str]:
    """
    Initializes and returns a Web3 instance connected to the RPC URL for the
    specified chain, along with the chain ID. Both values are looked up from
    the chains and rpcs tables in wallet.db.

    @param chat_id  The Telegram chat ID of the user.
    @return            A tuple of (Web3 instance, chain_id, chain_name).
    """
    chain_name = get_user_network(chat_id)
    rpc_url = get_rpc_url(chain_name)
    chain_id = get_chain_id_from_name(chain_name)

    if rpc_url is None or chain_id is None:
        raise ValueError(f"Chain name '{chain_name}' not found in database")

    return Web3(Web3.HTTPProvider(rpc_url)), chain_id, chain_name


def load_network_config_by_name(chain_name: str) -> tuple[Web3, int]:
    """
    Initializes and returns a Web3 instance connected to the RPC URL for the
    specified chain, along with the chain ID. Both values are looked up from
    the chains and rpcs tables in wallet.db.

    @param chain_name  The name of the blockchain network.
    @return            A tuple of (Web3 instance, chain_id).
    """

    rpc_url = get_rpc_url(chain_name)
    chain_id = get_chain_id_from_name(chain_name)

    if rpc_url is None or chain_id is None:
        raise ValueError(f"Chain name '{chain_name}' not found in database")

    return Web3(Web3.HTTPProvider(rpc_url)), chain_id
