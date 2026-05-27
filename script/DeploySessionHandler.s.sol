//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;
import {Script, console} from "forge-std/Script.sol";
import {SessionHandler} from "../src/SessionHandler.sol";
import {HelperConfig} from "./HelperConfig.s.sol";
import {PriceOracle} from "../src/PriceOracle.sol";
import {ERC20Mock} from "../src/mocks/ERC20Mock.sol";
import {MockWeth} from "../src/mocks/MockWeth.sol";

/**
 * @title DeploySessionHandler
 * @notice Deployment script for the SessionHandler ERC-4337 smart account
 * @dev Retrieves network configuration from HelperConfig and deploys SessionHandler
 *      with the appropriate EntryPoint address for the current chain.
 *
 *      Deployment is broadcast as config.account so that account becomes the
 *      Ownable owner of the deployed SessionHandler. This ensures the known
 *      deployer address has owner privileges for session key management.
 *
 *      Supported networks are determined by HelperConfig:
 *      - Anvil (chainid 31337): deploys a local EntryPoint and uses the default anvil account
 *      - Live networks: uses the canonical ERC-4337 EntryPoint and the configured account
 */
contract DeploySessionHandler is Script {
    /**
     * @notice Deploys a new SessionHandler instance and returns it alongside the network config
     * @dev Fetches the EntryPoint address and deployer account from HelperConfig, then
     *      broadcasts the SessionHandler deployment as config.account so that address
     *      becomes the contract owner.
     *
     *      Deployment steps:
     *      1. Instantiate HelperConfig to resolve chain-specific configuration
     *      2. Broadcast as config.account to set it as the Ownable owner
     *      3. Deploy SessionHandler with the resolved EntryPoint address
     *
     * @return sessionHandler The newly deployed SessionHandler instance
     * @return config The resolved NetworkConfig containing the entryPoint address and deployer account
     */
    function run() external returns (SessionHandler, HelperConfig.NetworkConfig memory, PriceOracle) {
        HelperConfig helperConfig = new HelperConfig();
        HelperConfig.NetworkConfig memory config = helperConfig.getConfig();

        // Broadcast as config.account so it becomes the Ownable owner of SessionHandler
        vm.startBroadcast(config.account);

        // Build parallel token/feed arrays for PriceOracle.
        // address(0) registers native ETH. Pairs with a zero feed are skipped inside the constructor,
        // so it is safe to pass address(0) feed entries for tokens unavailable on the current network.
        address[] memory tokens = new address[](21);
        address[] memory priceFeeds = new address[](21);
        uint256[] memory heartbeats = new uint256[](21);
        tokens[0] = address(0);
        priceFeeds[0] = config.ethUsdPriceFeed;
        heartbeats[0] = config.ethHeartbeat;
        tokens[1] = config.usdc;
        priceFeeds[1] = config.usdcUsdPriceFeed;
        heartbeats[1] = config.usdcHeartbeat;
        tokens[2] = config.dai;
        priceFeeds[2] = config.daiUsdPriceFeed;
        heartbeats[2] = config.daiHeartbeat;
        tokens[3] = config.aave;
        priceFeeds[3] = config.aaveUsdPriceFeed;
        heartbeats[3] = config.aaveHeartbeat;
        tokens[4] = config.link;
        priceFeeds[4] = config.linkUsdPriceFeed;
        heartbeats[4] = config.linkHeartbeat;
        tokens[5] = config.oneinch;
        priceFeeds[5] = config.oneinchUsdPriceFeed;
        heartbeats[5] = config.oneinchHeartbeat;
        tokens[6] = config.ape;
        priceFeeds[6] = config.apeUsdPriceFeed;
        heartbeats[6] = config.apeHeartbeat;
        tokens[7] = config.arb;
        priceFeeds[7] = config.arbUsdPriceFeed;
        heartbeats[7] = config.arbHeartbeat;
        tokens[8] = config.bnb;
        priceFeeds[8] = config.bnbUsdPriceFeed;
        heartbeats[8] = config.bnbHeartbeat;
        tokens[9] = config.wbtc;
        priceFeeds[9] = config.btcUsdPriceFeed;
        heartbeats[9] = config.btcHeartbeat;
        tokens[10] = config.comp;
        priceFeeds[10] = config.compUsdPriceFeed;
        heartbeats[10] = config.compHeartbeat;
        tokens[11] = config.crv;
        priceFeeds[11] = config.crvUsdPriceFeed;
        heartbeats[11] = config.crvHeartbeat;
        tokens[12] = config.ens;
        priceFeeds[12] = config.ensUsdPriceFeed;
        heartbeats[12] = config.ensHeartbeat;
        tokens[13] = config.mkr;
        priceFeeds[13] = config.mkrUsdPriceFeed;
        heartbeats[13] = config.mkrHeartbeat;
        tokens[14] = config.sand;
        priceFeeds[14] = config.sandUsdPriceFeed;
        heartbeats[14] = config.sandHeartbeat;
        tokens[15] = config.sushi;
        priceFeeds[15] = config.sushiUsdPriceFeed;
        heartbeats[15] = config.sushiHeartbeat;
        tokens[16] = config.wtao;
        priceFeeds[16] = config.wtaoUsdPriceFeed;
        heartbeats[16] = config.wtaoHeartbeat;
        tokens[17] = config.uni;
        priceFeeds[17] = config.uniUsdPriceFeed;
        heartbeats[17] = config.uniHeartbeat;
        tokens[18] = config.yfi;
        priceFeeds[18] = config.yfiUsdPriceFeed;
        heartbeats[18] = config.yfiHeartbeat;
        tokens[19] = config.weth;
        priceFeeds[19] = config.ethUsdPriceFeed;
        heartbeats[19] = config.ethHeartbeat;
        tokens[20] = config.usdt;
        priceFeeds[20] = config.usdtUsdPriceFeed;
        heartbeats[20] = config.usdtHeartbeat;

        PriceOracle oracle = new PriceOracle(tokens, priceFeeds, heartbeats);
        SessionHandler sessionHandler = new SessionHandler(config.entryPoint, address(oracle), config.uniswapRouter);

        if (block.chainid == 31337) {
            MockWeth(payable(config.weth)).mint(address(sessionHandler), 1000e18);
            ERC20Mock(config.usdc).mint(address(sessionHandler), 10000e6);
            ERC20Mock(config.usdt).mint(address(sessionHandler), 20000e18);
            ERC20Mock(config.dai).mint(address(sessionHandler), 10000e18);
            ERC20Mock(config.aave).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.link).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.oneinch).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.ape).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.arb).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.bnb).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.wbtc).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.comp).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.crv).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.ens).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.mkr).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.sand).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.sushi).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.wtao).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.uni).mint(address(sessionHandler), 2000e18);
            ERC20Mock(config.yfi).mint(address(sessionHandler), 2000e18);
            //(bool success,) = payable(address(sessionHandler)).call{value: 100 ether}("");
            //(success);
            vm.deal(address(sessionHandler), 10 ether);
        }
        vm.stopBroadcast();

        return (sessionHandler, config, oracle);
    }
}
