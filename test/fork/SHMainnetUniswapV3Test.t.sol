// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SHUniswapV3ForkTestBase} from "./SHUniswapV3ForkTestBase.sol";

/**
 * @title SHMainnetUniswapV3Test
 * @notice Mainnet-fork run of the shared spending-cap fork suite against the real Uniswap V3
 *         SwapRouter02 and position manager, real Chainlink feeds, and the canonical EntryPoint.
 * @dev Forks mainnet itself in setUp (foundry.toml `mainnet` → MAINNET_RPC_URL). Run with:
 *        make mainnet-uniswap-test
 *        (forge test --match-path test/fork/SHMainnetUniswapV3Test.t.sol)
 *
 *      Swap pool: WETH/DAI 0.3%, the deepest mainnet WETH/DAI V3 pool (~610 WETH against 1.9M
 *      DAI); both ends priced by the deployed SHOracle. The liquidity round-trip mints a V3
 *      position in this same pool and takes it out again (see SHUniswapV3ForkTestBase). Adding
 *      liquidity trades nothing, so the pool's depth does not limit it; it needs only the pool's
 *      current price, and 1 WETH with the DAI to match.
 */
contract SHMainnetUniswapV3Test is SHUniswapV3ForkTestBase {
    function setUp() public override {
        vm.createSelectFork(vm.rpcUrl("mainnet"));
        super.setUp();
    }

    function _swapPath() internal view override returns (address[] memory path) {
        path = new address[](2);
        path[0] = config.weth;
        path[1] = config.dai;
    }

    function _poolFee() internal pure override returns (uint24) {
        return 3000; // 0.3%
    }

    function _swapAmount() internal pure override returns (uint256) {
        return 1e18; // 1 WETH
    }

    function _feedsToFreshen() internal view override returns (address[] memory feeds) {
        feeds = new address[](2);
        feeds[0] = config.ethUsdPriceFeed;
        feeds[1] = config.daiUsdPriceFeed;
    }
}
