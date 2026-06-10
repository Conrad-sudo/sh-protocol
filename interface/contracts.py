from web3.contract import Contract
from network_config import load_network_config
from db import (
    get_json,
    get_wallet_address,
    get_token_address,
    get_entry_point_address,
    get_identity_registry_address,
    get_reputation_registry_address,
)
from constants import UNISWAP_V2_ROUTER, UNISWAP_V2_FACTORY, ETH_SENTINEL, CHAIN_ID_ANVIL

_session_handler_cache: dict[int, Contract] = {}
_entry_point_cache: dict[int, Contract] = {}
_erc20_cache: dict[tuple[int, str], Contract] = {}
_router_cache: dict[int, Contract] = {}
_factory_cache: dict[int, Contract] = {}
_pair_cache: dict[str, Contract] = {}
_identity_registry_cache: dict[int, Contract] = {}
_reputation_registry_cache: dict[int, Contract] = {}


def load_session_handler(chat_id: int) -> Contract:
    """
    Loads the SessionHandler contract ABI and binds it to the address stored in the DB
    for the given chat ID.

    @param chat_id  The Telegram chat ID of the user.
    @return         A web3.py Contract instance pointing to the deployed SessionHandler.
    """
    if chat_id not in _session_handler_cache:
        w3, _, _ = load_network_config(chat_id)
        abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
        address = get_wallet_address(chat_id)
        _session_handler_cache[chat_id] = w3.eth.contract(address=address, abi=abi)
    return _session_handler_cache[chat_id]


def load_entry_point(chat_id: int) -> Contract:
    """
    Loads the EntryPoint contract ABI and binds it to the address stored in the DB.

    @return  A web3.py Contract instance pointing to the deployed EntryPoint.
    """
    if chat_id not in _entry_point_cache:
        w3, chain_id, _ = load_network_config(chat_id)
        abi = get_json("./interface/artifacts/EntryPoint.json")["abi"]
        address = get_entry_point_address(chain_id=chain_id)
        _entry_point_cache[chat_id] = w3.eth.contract(address=address, abi=abi)
    return _entry_point_cache[chat_id]


def load_ierc20(chat_id: int, token: str, uniswap_pair=False) -> Contract:
    """
    Loads an IERC20 Contract instance for the given ticker symbol.

    @param token  The token ticker symbol to look up (e.g. "usdc", "dai").
    @return       A web3.py Contract instance for the matching ERC20Mock deployment.
    """
    key = (chat_id, token)
    if key not in _erc20_cache:
        w3, chain_id, _ = load_network_config(chat_id)
        if token == "weth":
            abi = get_json("./interface/artifacts/IWETH.json")["abi"]
        else:
            abi = get_json("./interface/artifacts/IERC20Extended.json")["abi"]
        if uniswap_pair:
            address = token
        else:
            address = get_token_address(chain_id, token)

        _erc20_cache[key] = w3.eth.contract(address=address, abi=abi)
    return _erc20_cache[key]


def invalidate_cache(chat_id: int) -> None:
    """Drop all cached contract instances for chat_id after a redeploy."""
    _session_handler_cache.pop(chat_id, None)
    _entry_point_cache.pop(chat_id, None)
    _router_cache.pop(chat_id, None)
    _factory_cache.pop(chat_id, None)
    for key in [k for k in _erc20_cache if k[0] == chat_id]:
        del _erc20_cache[key]
    for key in [k for k in _pair_cache if k[0] == chat_id]:
        del _pair_cache[key]


def load_calldata(instance: Contract, fn_name: str, args: list) -> bytes:
    """
    ABI-encodes a call to a function and returns the raw calldata bytes.

    @param instance       A bound web3.py  Contract instance.
    @param fn_name         The function name to encode (e.g. "transfer").
    @param args            The positional arguments for the function.
    @return                The ABI-encoded calldata as bytes (without 0x prefix).
    """
    return bytes.fromhex(
        instance.encode_abi(abi_element_identifier=fn_name, args=args)[2:]
    )


def load_iuniswap_router(chat_id: int) -> Contract:
    """
    Loads the Uniswap V2 Router interface ABI and binds it to the known mainnet address.

    @return  A web3.py Contract instance for the Uniswap V2 Router.
    """
    if chat_id not in _router_cache:
        w3, _, _ = load_network_config(chat_id)
        abi = get_json("./interface/artifacts/IUniswapV2Router02.json")["abi"]
        address = UNISWAP_V2_ROUTER
        _router_cache[chat_id] = w3.eth.contract(address=address, abi=abi)
    return _router_cache[chat_id]


def load_iuniswap_factory(chat_id: int) -> Contract:
    """
    Loads the Uniswap V2 Factory interface ABI and binds it to the known mainnet address.

    @return  A web3.py Contract instance for the Uniswap V2 Factory.
    """
    if chat_id not in _factory_cache:
        w3, _, _ = load_network_config(chat_id)
        abi = get_json("./interface/artifacts/IUniswapV2Factory.json")["abi"]
        address = UNISWAP_V2_FACTORY
        _factory_cache[chat_id] = w3.eth.contract(address=address, abi=abi)
    return _factory_cache[chat_id]


def load_identity_registry(chat_id: int) -> Contract:
    """Loads the ERC-8004 Identity Registry bound to the correct address for this chain.
    Anvil uses the locally compiled ABI; Sepolia/Mainnet use the canonical artifact."""
    if chat_id not in _identity_registry_cache:
        w3, chain_id, _ = load_network_config(chat_id)
        if chain_id == CHAIN_ID_ANVIL:
            abi = get_json("./out/AgentIdentityRegistry.sol/AgentIdentityRegistry.json")["abi"]
        else:
            abi = get_json("./interface/artifacts/IdentityRegistry.json")
        address = get_identity_registry_address(chain_id)
        _identity_registry_cache[chat_id] = w3.eth.contract(address=address, abi=abi)
    return _identity_registry_cache[chat_id]


def load_reputation_registry(chat_id: int) -> Contract:
    """Loads the ERC-8004 Reputation Registry bound to the correct address for this chain.
    Anvil uses the locally compiled ABI; Sepolia/Mainnet use the canonical artifact."""
    if chat_id not in _reputation_registry_cache:
        w3, chain_id, _ = load_network_config(chat_id)
        if chain_id == CHAIN_ID_ANVIL:
            abi = get_json("./out/ReputationRegistry.sol/ReputationRegistry.json")["abi"]
        else:
            abi = get_json("./interface/artifacts/ReputationRegistry.json")
        address = get_reputation_registry_address(chain_id)
        _reputation_registry_cache[chat_id] = w3.eth.contract(address=address, abi=abi)
    return _reputation_registry_cache[chat_id]


def load_iuniswap_pair(chat_id: int, token_a: str, token_b: str) -> Contract:
    """
    Loads the Uniswap V2 Pair interface ABI and binds it to the address of the pair for the given tokens.

    @param token_a   The ticker symbol of the first token (e.g. "usdc").
    @param token_b   The ticker symbol of the second token (e.g. "dai").
    @return          A web3.py Contract instance for the Uniswap V2 Pair of the two tokens.
    """

    key = (chat_id, token_a, token_b)
    if key not in _pair_cache:
        w3, chain_id, _ = load_network_config(chat_id)
        factory = load_iuniswap_factory(chat_id)
        address = factory.functions.getPair(
            get_token_address(chain_id, token_a), get_token_address(chain_id, token_b)
        ).call()
        if address == ETH_SENTINEL:
            raise ValueError(
                f"No Uniswap V2 pool exists for {token_a.upper()}/{token_b.upper()}."
            )
        abi = get_json("./interface/artifacts/IUniswapV2Pair.json")["abi"]
        _pair_cache[key] = w3.eth.contract(address=address, abi=abi)
    return _pair_cache[key]
