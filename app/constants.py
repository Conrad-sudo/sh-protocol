



from langchain_erc20.tokens import ZERO_ADDRESS
from langchain_uniswap_v2 import KNOWN_NETWORKS as V2_NETWORKS
from langchain_uniswap_v3.networks import KNOWN_NETWORKS as V3_NETWORKS
from web3 import Web3

CHAIN_ID_ANVIL = 31337
CHAIN_ID_MAINNET = 1
CHAIN_ID_SEPOLIA = 11155111
CHAIN_ID_BSC = 56
CHAIN_ID_CELO = 42220
CHAIN_ID_ARBITRUM = 42161
CHAIN_ID_BASE = 8453
WEI_PER_ETH = 10**18

# How long before a passkey's approval on a wallet runs out the user starts hearing about it: the
# dashboard flags it for renewal, the Telegram bot warns, a quote mentions it. A fixed lead time
# rather than a fraction of the approval's life: what the user needs is time to sign a renewal in the
# web app, and that doesn't scale with how long it was granted for.
SESSION_RENEWAL_WARNING_SECS = 3 * 86_400
# SessionHandler's MAX_SESSION_KEYS and MAX_SESSION_TTL, mirrored for what the dashboard reports
# without reading them: both are compile-time constants of the contract. The session prepare route
# reads MAX_SESSION_TTL off the wallet itself before it checks a grant against it.
MAX_SESSION_KEYS = 5
MAX_SESSION_TTL_SECS = 90 * 86_400
# How much life a passkey's approval on the wallet must have left to count for a transaction: in
# the wallet checks every quote runs, and in which passkeys an approval page may use. The wallet's
# own comparison is exact (valid through the deadline second, matching the EntryPoint), so this
# margin is purely client side: it stops a transaction being quoted and approved only to be refused
# with AA22 while it was in flight.
SESSION_EXPIRY_MARGIN_SECS = 60
# The address(0) sentinel the wallet, SHOracle and both toolkits all use for "native asset".
# Taken from langchain-erc20 so there is exactly one definition in play: it is what the ERC20
# toolkit is configured with as native_sentinel, and a mismatch would silently route a native
# operation down the ERC-20 path.
ETH_SENTINEL = ZERO_ADDRESS

# Which exchange the assistant trades on is not protocol configuration: SHRegistry.router was
# removed because the venue is a wallet-level choice. Every exchange address below is resolved from
# the toolkit packages' own network tables rather than a local copy, so this app and the calldata
# the toolkits build can never disagree about which contract is which. Fork modes inherit the
# forked chain's ID, so a fork trades wherever its chain does.
#
#   - "uniswap-v3" on every chain langchain_uniswap_v3 lists (mainnet, Sepolia, Arbitrum, Base):
#     swaps through SwapRouter02, liquidity as position NFTs through the position manager. Almost
#     all Uniswap liquidity there is on V3; the V2 pools left are thin, and empty on Base and
#     Arbitrum. Being in that table is what moves a chain to V3, so the answer and the addresses
#     can't disagree.
#   - "pancakeswap-v2" on BSC, through PancakeSwap's V2 router (get_router).
#   - None anywhere else (a bare Anvil, Celo): no exchange, and nothing for a wallet to trust.

# The address fields of a langchain_uniswap_v3 network row, checksummed by get_v3_network.
_V3_ADDRESS_KEYS = ("swap_router", "quoter", "position_manager", "native_wrapped")


def get_exchange(chain_id: int) -> str | None:
    """
    The exchange the assistant trades on for chain_id: "uniswap-v3", "pancakeswap-v2", or None where
    it can't trade (see the table above). /api/chains reports it as each chain's `exchange`.
    """
    if chain_id in V3_NETWORKS:
        return "uniswap-v3"
    if chain_id == CHAIN_ID_BSC:
        return "pancakeswap-v2"
    return None


def get_v3_network(chain_id: int) -> dict | None:
    """
    The Uniswap V3 contracts on chain_id: its langchain_uniswap_v3 KNOWN_NETWORKS row, with
    "swap_router" (SwapRouter02), "quoter" (QuoterV2), "position_manager" and "native_wrapped"
    (WETH9) checksummed. None on a chain that doesn't trade on V3.

    A copy, so a caller that changes it can't change it for everyone else.
    """
    network = V3_NETWORKS.get(chain_id)
    if network is None:
        return None
    return {
        key: Web3.to_checksum_address(value) if key in _V3_ADDRESS_KEYS else value
        for key, value in network.items()
    }


def get_default_spenders(chain_id: int) -> list[str]:
    """
    The contracts every new wallet on chain_id is deployed trusting (deployWallet's
    trustedSpenders): the exchange contracts the assistant approves tokens to.
    [SwapRouter02, the position manager] on a V3 chain, [the PancakeSwap V2 router] on BSC, and []
    where there is no exchange. Checksummed.

    They have to be trusted because SpendingLimitModule refuses an approval for a token the oracle
    can't price unless its spender is trusted. Without them, a token the user added by address
    couldn't be swapped or deposited, and on BSC a pool's LP token couldn't be approved back to the
    router to take liquidity out. Approvals of priced tokens never needed it.

    The app refuses to remove any of them (routes/owner.py), and /api/chains lists them so the
    Controls page keeps them off the removable list.
    """
    exchange = get_exchange(chain_id)
    if exchange == "uniswap-v3":
        network = get_v3_network(chain_id)
        return [network["swap_router"], network["position_manager"]]
    if exchange == "pancakeswap-v2":
        return [get_router(chain_id)]
    return []


def get_router(chain_id: int) -> str:
    """
    Returns chain_id's V2 router: PancakeSwap's on BSC, and Uniswap Labs' own V2 redeployment on
    the other chains langchain-uniswap-v2 lists.

    On BSC it is the exchange: the V2 toolkit builds calldata for it, and it is the chain's one
    default spender. On the V3 chains nothing new trades through it. It stays for what wallets did
    before the move: the dashboard reads their V2 LP tokens through its factory, History still names
    it and credits the native coin it unwrapped, and wallets deployed before the move trust it.

    @param chain_id  The numeric chain ID.
    @return             The router address, checksummed. The package stores some in lowercase
                        (Arbitrum's, for one), which web3 refuses as a contract argument.
    @raises ValueError  If the package knows no V2 deployment for the chain (e.g. a bare Anvil
                        node — fork modes inherit the forked chain's ID and so resolve normally).
    """
    network = V2_NETWORKS.get(chain_id)
    if network is None:
        raise ValueError(f"No Uniswap-V2-compatible router configured for chain_id {chain_id}")
    return Web3.to_checksum_address(network["router"])

# The V2 factory address is not hardcoded per chain: langchain-uniswap-v2 reads router.factory()
# off the router above, so pair lookups can never drift from the router in use. The V3 factory
# isn't either: the V3 toolkit reads factory() off SwapRouter02, the quoter and the position
# manager, and refuses to start unless all three agree.

# The ticker of the chain's actual wrapped-native-asset contract — the one *ETH*-suffixed
# router functions and deposit()/withdraw() calls operate against. WETH on Ethereum/Sepolia/
# Anvil, and on Arbitrum and Base, whose native gas asset is also ETH. WBNB on BSC. On Celo, CELO is
# natively an ERC-20 and Ubeswap has no WETH() function, so "celo" here refers to the CELO
# ERC-20 used in token-to-token swaps (not a wrap/unwrap).
NATIVE_WRAPPED_TICKER = {
    CHAIN_ID_MAINNET: "weth",
    CHAIN_ID_SEPOLIA: "weth",
    CHAIN_ID_ANVIL: "weth",
    CHAIN_ID_BSC: "wbnb",
    CHAIN_ID_CELO: "celo",
    CHAIN_ID_ARBITRUM: "weth",
    CHAIN_ID_BASE: "weth",
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


def get_always_counted_ticker(chain_id: int) -> str | None:
    """
    The ERC-20 Mitfah always counts toward the spending limit on chain_id: the wrapped native token
    (WETH, WBNB), so it is treated like the native asset it wraps -- which the wallet contract always
    meters. New wallets are always deployed watching it, and the API won't build its removal.

    This is app policy, not a contract rule: the owner could still call removeWatchedToken on the
    wallet directly, and a wallet deployed before this rule may not watch it (the Controls page
    offers to add it).

    None on Celo, where "celo" is the native asset itself exposed as an ERC-20: watching it would
    count every CELO movement twice.
    """
    if chain_id == CHAIN_ID_CELO:
        return None
    return NATIVE_WRAPPED_TICKER.get(chain_id)


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
    CHAIN_ID_BASE: "ETH",
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


# The chain's name as the web app shows it (viem's chain names, web/src/wallet/chains.ts), so a
# quote names the same network the user picked on the page.
CHAIN_DISPLAY_NAME = {
    CHAIN_ID_MAINNET: "Ethereum",
    CHAIN_ID_SEPOLIA: "Sepolia",
    CHAIN_ID_ANVIL: "Anvil",
    CHAIN_ID_BSC: "BNB Smart Chain",
    CHAIN_ID_CELO: "Celo",
    CHAIN_ID_ARBITRUM: "Arbitrum One",
    CHAIN_ID_BASE: "Base",
}


def get_chain_display_name(chain_id: int) -> str:
    """The chain's user-facing name (e.g. "BNB Smart Chain"), or "chain <id>" for an unlisted one."""
    return CHAIN_DISPLAY_NAME.get(chain_id, f"chain {chain_id}")

