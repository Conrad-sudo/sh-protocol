# Uniswap Labs' own V3 deployments, for the chains this repo trades V3 on. Every address is the
# same as script/Constants.s.sol's *_UNISWAP_V3_SWAP_ROUTER_02 / _QUOTER_V2 / _POSITION_MANAGER,
# which stays the source of truth: app/tests/test_uniswap_v3.py fails if the two ever differ.
#
# Verified on-chain (2026-10-12 for the router and quoter, 2026-10-14 for the position manager):
# all three report the same factory() -- the chain's V3 factory -- and the same WETH9(), which is
# the chain's own wrapped native token. The toolkit re-checks both at construction, so a stale or
# mistyped entry fails loudly instead of building calldata for the wrong contract.
#
# Three things that look like mistakes and are not:
#   - Mainnet and Arbitrum share every address: Uniswap deployed V3 to both from the same
#     deployer at the same nonces.
#   - "swap_router" is SwapRouter02 (IV3SwapRouter, no deadline in its structs), not the original
#     SwapRouter at 0xE592...1564, which only exists on mainnet and Arbitrum.
#   - Base's WETH, 0x4200...0006, is the OP Stack predeploy, not a placeholder.
#
# rpc_url is a free public endpoint -- fine for prototyping, rate-limited; pass your own via
# for_chain(..., rpc_url=...). Chains with no V3 deployment here are omitted, not guessed; use the
# main constructor with explicit addresses for any other chain.
KNOWN_NETWORKS: dict[int, dict] = {
    1: {  # Ethereum Mainnet
        "name": "mainnet",
        "rpc_url": "https://ethereum.publicnode.com",
        "swap_router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "position_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "native_wrapped": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
        "native_token": "ETH",
    },
    11155111: {  # Sepolia
        "name": "sepolia",
        "rpc_url": "https://ethereum-sepolia-rpc.publicnode.com",
        "swap_router": "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E",
        "quoter": "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3",
        "position_manager": "0x1238536071E1c677A632429e3655c799b22cDA52",
        "native_wrapped": "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",  # WETH
        "native_token": "ETH",
    },
    42161: {  # Arbitrum One
        "name": "arbitrum",
        "rpc_url": "https://arb1.arbitrum.io/rpc",
        "swap_router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "position_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "native_wrapped": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
        "native_token": "ETH",
    },
    8453: {  # Base
        "name": "base",
        "rpc_url": "https://base.publicnode.com",
        "swap_router": "0x2626664c2603336E57B271c5C0b26F421741e481",
        "quoter": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        "position_manager": "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
        "native_wrapped": "0x4200000000000000000000000000000000000006",  # WETH
        "native_token": "ETH",
    },
}
