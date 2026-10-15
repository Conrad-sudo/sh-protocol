// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SHUniswapV3ForkTestBase} from "./SHUniswapV3ForkTestBase.sol";

/**
 * @title SHSepoliaUniswapV3Test
 * @notice Sepolia-fork run of the shared spending-cap fork suite against the official Sepolia
 *         Uniswap V3 SwapRouter02 and position manager, live Sepolia Chainlink feeds, and the
 *         canonical EntryPoint.
 * @dev Forks Sepolia itself in setUp (foundry.toml `sepolia` → SEPOLIA_RPC_URL). Run with:
 *        make sepolia-uniswap-test
 *        (forge test --match-path test/fork/SHSepoliaUniswapV3Test.t.sol)
 *
 *      Swap pool: WETH/USDC 0.05% (~100 WETH against 4.2M USDC; both ends Chainlink-priced).
 *      NOTE: Sepolia pool ratios diverge wildly from real Chainlink prices (this pool prices WETH
 *      ~19x above the feed). That is fine here: the base suite computes expected metering from
 *      actual balance diffs priced through the oracle, so a "profitable-looking" swap simply meters
 *      as net inflow => spend 0. Amounts stay small relative to pool depth.
 *
 *      The liquidity round-trip mints a V3 position in this same pool, sized at the pool's OWN
 *      price: the USDC side is whatever that price asks for (~19x what the feed would suggest), and
 *      the deposit is metered at the oracle's price for both tokens, as the swaps are.
 */
contract SHSepoliaUniswapV3Test is SHUniswapV3ForkTestBase {
    function setUp() public override {
        vm.createSelectFork(vm.rpcUrl("sepolia"));
        super.setUp();
    }

    function _swapPath() internal view override returns (address[] memory path) {
        path = new address[](2);
        path[0] = config.weth;
        path[1] = config.usdc;
    }

    function _poolFee() internal pure override returns (uint24) {
        return 500; // 0.05%
    }

    function _swapAmount() internal pure override returns (uint256) {
        return 0.05e18; // 0.05 WETH — sized against real Sepolia pool depth
    }

    function _feedsToFreshen() internal view override returns (address[] memory feeds) {
        feeds = new address[](2);
        feeds[0] = config.ethUsdPriceFeed;
        feeds[1] = config.usdcUsdPriceFeed;
    }
}
