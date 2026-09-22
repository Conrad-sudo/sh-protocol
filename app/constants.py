



from langchain_erc20.tokens import ZERO_ADDRESS
from langchain_uniswap_v2 import KNOWN_NETWORKS
from web3 import Web3

CHAIN_ID_ANVIL = 31337
CHAIN_ID_MAINNET = 1
CHAIN_ID_SEPOLIA = 11155111
CHAIN_ID_BSC = 56
CHAIN_ID_CELO = 42220
CHAIN_ID_ARBITRUM = 42161
WEI_PER_ETH = 10**18
# The address(0) sentinel the wallet, SHOracle and both toolkits all use for "native asset".
# Taken from langchain-erc20 so there is exactly one definition in play: it is what the ERC20
# toolkit is configured with as native_sentinel, and a mismatch would silently route a native
# operation down the ERC-20 path.
ETH_SENTINEL = ZERO_ADDRESS

# The router is no longer protocol configuration: SHRegistry.router was removed because which
# venue a wallet trades on is a wallet-level choice. It is resolved from langchain-uniswap-v2's
# own network table rather than a local copy, so this app and the toolkit can never disagree
# about which contract "the router" is.


def get_router(chain_id: int) -> str:
    """
    Returns the canonical V2-compatible router address for chain_id.

    The one address deploy_wallet.trust_router passes to addTrustedSpender AND toolkits.py binds
    the Uniswap toolkit to. They must match: the module rejects an approval whose spender it does
    not trust, so a mismatch turns every LP-token approval into a revert.

    @param chain_id  The numeric chain ID.
    @return             The router address, checksummed. The package stores some in lowercase
                        (Arbitrum's, for one), which web3 refuses as a contract argument.
    @raises ValueError  If the package knows no V2 deployment for the chain (e.g. a bare Anvil
                        node — fork modes inherit the forked chain's ID and so resolve normally).
    """
    network = KNOWN_NETWORKS.get(chain_id)
    if network is None:
        raise ValueError(f"No Uniswap-V2-compatible router configured for chain_id {chain_id}")
    return Web3.to_checksum_address(network["router"])

# The V2 factory address is not hardcoded per chain: langchain-uniswap-v2 reads router.factory()
# off the router above, so pair lookups can never drift from the router in use.

# The ticker of the chain's actual wrapped-native-asset contract — the one *ETH*-suffixed
# router functions and deposit()/withdraw() calls operate against. WETH on Ethereum/Sepolia/
# Anvil, and on Arbitrum, whose native gas asset is also ETH. WBNB on BSC. On Celo, CELO is
# natively an ERC-20 and Ubeswap has no WETH() function, so "celo" here refers to the CELO
# ERC-20 used in token-to-token swaps (not a wrap/unwrap).
NATIVE_WRAPPED_TICKER = {
    CHAIN_ID_MAINNET: "weth",
    CHAIN_ID_SEPOLIA: "weth",
    CHAIN_ID_ANVIL: "weth",
    CHAIN_ID_BSC: "wbnb",
    CHAIN_ID_CELO: "celo",
    CHAIN_ID_ARBITRUM: "weth",
}


def get_native_wrapped_ticker(chain_id: int) -> str:
    """
    Returns the wrapped-native-asset ticker for chain_id.

    @param chain_id  The numeric chain ID.
    @raises ValueError  If chain_id has no configured native-wrapped ticker.
    """
    ticker = NATIVE_WRAPPED_TICKER.get(chain_id)
    if ticker is None:
        raise ValueError(f"No native-wrapped ticker configured for chain_id {chain_id}")
    return ticker


# Display name of the chain's native gas asset — the thing "eth" as a session/ticker
# argument actually refers to everywhere in this codebase (ETH_SENTINEL, get_eth_balance,
# send_eth, the swap_*_ETH tools, etc). Purely cosmetic: lets user-facing text say "BNB"
# instead of "ETH" when the wallet is deployed on BSC.
NATIVE_ASSET_TICKER = {
    CHAIN_ID_MAINNET: "ETH",
    CHAIN_ID_SEPOLIA: "ETH",
    CHAIN_ID_ANVIL: "ETH",
    CHAIN_ID_BSC: "BNB",
    CHAIN_ID_CELO: "CELO",
    CHAIN_ID_ARBITRUM: "ETH",
}


def get_native_asset_ticker(chain_id: int) -> str:
    """
    Returns the display name of chain_id's native gas asset (e.g. "ETH", "BNB", "CELO").

    @param chain_id  The numeric chain ID.
    @raises ValueError  If chain_id has no configured native-asset ticker.
    """
    ticker = NATIVE_ASSET_TICKER.get(chain_id)
    if ticker is None:
        raise ValueError(f"No native-asset ticker configured for chain_id {chain_id}")
    return ticker

