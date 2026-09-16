"""
Static reference data seeded into SQLite by `make db` (see db.py).

The data lives in Python rather than JSON so it can carry comments, fail
loudly on a typo'd name, and pull RPC API keys from the environment instead
of source control.

SEEDS at the bottom is the manifest db.py iterates over. To add a dataset:
define the dict here, add a SEEDS entry, and CREATE the table in db.py
init_db().
"""

import os

from dotenv import load_dotenv
from langchain_erc20 import KNOWN_NETWORKS as ERC20_NETWORKS

load_dotenv()


def _wrapped_native(chain_id: int) -> str:
    """The chain's wrapped-native address, from langchain-erc20's verified table.

    Not hardcoded alongside the tokens below: this one address is what toolkits.py passes as
    `native_wrapped_address`, so wrap/unwrap and every *ETH-suffixed router call depend on it
    being exactly right. The package verifies each entry on-chain (bytecode present, symbol()
    matches, decimals() == 18, deposit()/withdraw() in the deployed code) and is the same table
    the toolkits resolve against, so sourcing it here removes the only copy that could drift.

    Every OTHER token below stays local on purpose — the package ships no token registry, since
    a wrong address in one is a silent, unrecoverable loss of funds.
    """
    return ERC20_NETWORKS[chain_id]["native_wrapped"]


# Function-selector tables were removed with the per-target session-key design: the old
# SpendingLimitModule allowlisted a session key to specific (target, selector) pairs, so the DB
# had to know every selector. The current module enforces one global USD spending cap instead —
# no selector allowlisting — so nothing consumes these anymore.


# ── Networks ──────────────────────────────────────────────────────────────────

CHAINS = {
    "anvil": 31337,
    "mainnet": 1,
    "mainnet-fork": 1,
    "goerli": 5,  # network shut down early 2024
    "sepolia": 11155111,
    "sepolia-fork": 11155111,
    "polygon": 137,
    "mumbai": 80001,  # network shut down April 2024
    "optimism": 10,
    "optimism-goerli": 420,  # retired with Goerli
    "arbitrum": 42161,
    "arbitrum-fork": 42161,
    "arbitrum-goerli": 421613,  # retired with Goerli
    "avalanche": 43114,
    "fuji": 43113,
    "bsc": 56,
    "bsc-fork": 56,
    "bsc-testnet": 97,
    "fantom": 250,
    "fantom-testnet": 4002,
    "celo": 42220,
    "celo-fork": 42220,
    "alfajores": 44787,
    "aurora": 1313161554,
    "aurora-testnet": 1313161555,
    "harmony": 1666600000,
    "harmony-testnet": 1666700000,
    "moonbeam": 1284,
    "moonbase": 1287,
    "avalanche-fuji": 43113,  # duplicate of "fuji"
}

# Keyed RPC endpoints belong in .env, not here — this file is committed.
RPCS = {
    "anvil": "http://127.0.0.1:8545",
    "mainnet-fork": "http://127.0.0.1:8545",
    "mainnet": "https://cloudflare-eth.com",
    "goerli": "https://ethereum-goerli-rpc.publicnode.com",
    "sepolia-fork": "http://127.0.0.1:8545",
    "bsc-fork": "http://127.0.0.1:8545",
    "celo-fork": "http://127.0.0.1:8545",
    "sepolia": os.getenv("SEPOLIA_RPC_URL")
    or "https://ethereum-sepolia-rpc.publicnode.com",
    "polygon": "https://polygon-rpc.com",
    "mumbai": "https://rpc-mumbai.maticvigil.com",
    "optimism": "https://mainnet.optimism.io",
    "optimism-goerli": "https://goerli.optimism.io",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "arbitrum-fork": "http://127.0.0.1:8545",  # `make arb-fork`
    "arbitrum-goerli": "https://goerli-rollup.arbitrum.io/rpc",
    "avalanche": "https://api.avax.network/ext/bc/C/rpc",
    "fuji": "https://api.avax-test.network/ext/bc/C/rpc",
    "bsc": "https://bsc-dataseed.binance.org",
    "bsc-testnet": "https://data-seed-prebsc-1-s1.bnbchain.org:8545",
    "fantom": "https://rpc.ftm.tools",
    "fantom-testnet": "https://rpc.testnet.fantom.network",
    "celo": "https://forno.celo.org",
    "alfajores": "https://alfajores-forno.celo-testnet.org",
    "aurora": "https://mainnet.aurora.dev",
    "aurora-testnet": "https://testnet.aurora.dev",
    "harmony": "https://api.harmony.one",
    "harmony-testnet": "https://api.s0.b.hmny.io",
    "moonbeam": "https://rpc.api.moonbeam.network",
    "moonbase": "https://rpc.api.moonbase.moonbeam.network",
    "avalanche-fuji": "https://api.avax-test.network/ext/bc/C/rpc",
}


# ── Tokens (ticker → address) ─────────────────────────────────────────────────

MAINNET_TOKENS = {
    "weth": _wrapped_native(1),
    "usdt": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "dai": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "aave": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
    "link": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "oneinch": "0x111111111117dC0aa78b770fA6A738034120C302",
    "ape": "0x4d224452801ACEd8B2F0aebE155379bb5D594381",
    "arb": "0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1",
    "bnb": "0xB8c77482e45F1F44dE1745F52C74426C631bDD52",
    "wbtc": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "comp": "0xc00e94Cb662C3520282E6f5717214004A7f26888",
    "crv": "0xD533a949740bb3306d119CC777fa900bA034cd52",
    "ens": "0xC18360217D8F7Ab5e7c516566761Ea12Ce7F9D72",
    "sand": "0x3845badAde8e6dFF049820680d1F14bD3903a5d0",
    "sushi": "0x6B3595068778DD592e39A122f4f5a5cF09C90fE2",
    "wtao": "0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44",
    "uni": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "yfi": "0x0bc529c00C6401aEF6D220BE8C6Ea1667F6Ad93e",
    "imx": "0xf57e7e7c23978c3caec3c3548e3d615c346e79ff",
    "knc": "0xdefa4e8a7bcba345f687a2f1456f5edd9ce97202",
    "rpl": "0xd33526068d116ce69f19a9ee46f0bd304f21a51f",
    "sky": "0x56072C95FAA701256059aa122697B133aDEd9279",
    "snx": "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f",
    "stg": "0xaf5191b0de278c7286d6c7cc6ab6bb8a73ba2cd6",
    "sxt": "0xE6Bfd33F52d82Ccb5b37E16D3dD81f9FFDAbB195",
    "trump": "0x576e2bed8f7b46d34016198911cdf9886f78bea7",
    "zec": "0x4a64515e5e1d1073e83f30cb97bed20400b66e10",
}

SEPOLIA_TOKENS = {
    "weth": _wrapped_native(11155111),
    "link": "0x779877A7B0D9E8603169DdbD7836e478b4624789",
    # USDC/DAI have live Sepolia Chainlink feeds and are the default watched tokens for Sepolia
    # deploys (deploy_wallet.DEFAULT_WATCHED_TICKERS) — addresses match Constants.s.sol SPO_USDC/SPO_DAI.
    "usdc": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
    "dai": "0x68194a729C2450ad26072b3D33ADaCbcef39D574",
}

BSC_TOKENS = {
    "weth": "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
    "wbnb": _wrapped_native(56),
    "usdt": "0x55d398326f99059fF775485246999027B3197955",
    "usdc": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    "dai": "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",
    "aave": "0xfb6115445Bff7b52FeB98650C87f44907E58f802",
    "link": "0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD",
    "oneinch": "0x111111111117dC0aa78b770fA6A738034120C302",
    "wbtc": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",
    "comp": "0x52CE071Bd9b1C4B00A0b92D298c512478CaD67e8",
    "crv": "0x9996D0276612d23b35f90C51EE935520B3d7355B",
    "sushi": "0x947950BcC74888a40Ffa2593C5798F11Fc9124C4",
    "uni": "0xBf5140A22578168FD562DCcF235E5D43A02ce9B1",
    "yfi": "0x88f1A5ae2A3BF98AEAF342D26B30a79438c9142e",
    "wavax": "0x1CE0c2827e2eF14D5C4f29a091d735A204794041",
    "knc": "0xfe56d5892BDffC7BF58f2E84BE1b2C32D21C308b",
    "cake": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
}

# Every address below is the one in script/Constants.s.sol's ARB_* block, so the app and the
# on-chain oracle config cannot disagree about what "usdc on Arbitrum" means. Each was re-verified
# against Arbitrum One: symbol()/decimals() answered as expected and each is EIP-55 checksummed.
#
# The set is exactly the Arbitrum tokens SHOracle prices (see HelperConfig.getArbConfig) — an
# unpriced token is unusable to the spending-limit hook, so listing one here would only offer the
# user a watched-token choice that makes deployWallet revert with TokenNotPriced.
ARBITRUM_TOKENS = {
    "weth": _wrapped_native(42161),
    "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # native Circle USDC, not USDC.e
    "dai": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    "usdt": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # answers symbol() "USD₮0" since Tether's rebrand
    "aave": "0xba5DdD1f9d7F570dc94a51479a000E3BCE967196",
    "link": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
    "oneinch": "0x6314C31A7a1652cE482cffe247E9CB7c3f4BB9aF",
    "ape": "0x7f9FBf9bDd3F4105C478b996B648FE6e828a1e98",  # ApeCoin's own Arbitrum deployment
    "arb": "0x912CE59144191C1204E64559FE8253a0e49E6548",
    "wbtc": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
    "comp": "0x354A6dA3fcde098F8389cad84b0182725c6C91dE",
    "crv": "0x11cDb42B0EB46D95f990BeDD4695A6e3fA034978",
    "sushi": "0xd4d42F0b6DEF4CE0383636770eF773390d85c61A",
    "uni": "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0",
    "yfi": "0x82e3A8F066a6989666b031d916c43672085b1582",
    "cake": "0x1b896893dfc86bb67Cf57767298b9073D2c1bA2c",  # PancakeSwap's own Arbitrum deployment
}

CELO_TOKENS = {
    "celo": "0x471EcE3750Da237f93B8E339c536989b8978a438",
    "usdc": "0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
    "usdt": "0x617f3112bf5397D0467D315cC709EF968D9ba546",
    "weth": "0x66803FB87aBd4aaC3cbB3fAd7C3aa01f6F3FB207",
    "wbtc": "0xBAAB46E28388d2779e6E31Fd00cF0e5Ad95E327B",
    "cusd": "0x765DE816845861e75A25fCA122bb6898B8B1282a",
}


# ── Seed manifest ─────────────────────────────────────────────────────────────
# (table, key column, value column, data, checksum addresses before insert)
#for table, key_col, value_col, data, checksum in SEEDS:
SEEDS = [
    ("chains", "name", "chain_id", CHAINS, False),
    ("rpcs", "name", "rpc_url", RPCS, False),
    ("mainnet_tokens", "ticker", "address", MAINNET_TOKENS, True),
    ("sepolia_tokens", "ticker", "address", SEPOLIA_TOKENS, True),
    ("bsc_tokens", "ticker", "address", BSC_TOKENS, True),
    ("celo_tokens", "ticker", "address", CELO_TOKENS, True),
    ("arbitrum_tokens", "ticker", "address", ARBITRUM_TOKENS, True),
]
