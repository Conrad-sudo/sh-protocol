// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SHForkTestBase} from "./SHForkTestBase.sol";

/**
 * @title SHPancakeswapV2Test
 * @notice BSC-fork run of the shared spending-cap fork suite against the real PancakeSwap V2
 *         router, real Chainlink feeds, and the canonical EntryPoint.
 * @dev Forks BSC itself in setUp (foundry.toml `bschain` → BSC_RPC_URL). Run with:
 *        make pancakeswap-test
 *        (forge test --match-path test/fork/SHPancakeswapV2Test.t.sol)
 *
 *      Swap path: USDT -> WBNB -> LINK (multi-hop through WBNB, matching real BSC liquidity;
 *      only the path ENDS are watched/metered — the module never inspects the route). BSC stays
 *      on V2: its liquidity is PancakeSwap V2, and Uniswap's SwapRouter02 there has no V2 factory.
 *
 *      Everything venue-specific is the base's V2 default: the wallet trusts the PancakeSwap
 *      router, and the liquidity round-trip adds liquidity to a pair, then removes it by approving
 *      the pair's UNPRICED LP token to that router, which the module allows only because
 *      deployWallet seeded the router as a trusted spender.
 */
contract SHPancakeswapV2Test is SHForkTestBase {
    function setUp() public override {
        vm.createSelectFork(vm.rpcUrl("bschain"));
        super.setUp();
    }

    function _swapPath() internal view override returns (address[] memory path) {
        path = new address[](3);
        path[0] = config.usdt;
        path[1] = config.wbnb;
        path[2] = config.link;
    }

    function _swapAmount() internal pure override returns (uint256) {
        return 500e18; // 500 USDT (BSC-USD uses 18 decimals)
    }

    function _feedsToFreshen() internal view override returns (address[] memory feeds) {
        // BNB/USD is included because native value (address(0)) is now metered — the native-in/out
        // swap tests price the wallet's BNB delta through this feed, so it must stay fresh too.
        feeds = new address[](3);
        feeds[0] = config.usdtUsdPriceFeed;
        feeds[1] = config.linkUsdPriceFeed;
        feeds[2] = config.bnbUsdPriceFeed;
    }
}
