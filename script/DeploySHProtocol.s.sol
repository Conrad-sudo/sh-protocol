//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;
import {Script} from "forge-std/Script.sol";
import {HelperConfig} from "./HelperConfig.s.sol";
import {SHOracle} from "../src/SHOracle.sol";
import {SHTreasury} from "../src/SHTreasury.sol";
import {SHRegistry} from "../src/SHRegistry.sol";
import {SHFactory} from "../src/SHFactory.sol";
import {IIdentityRegistry} from "../src/interfaces/IIdentityRegistry.sol";
import {SpendingLimitModule} from "../src/SpendingLimitModule.sol";
import "./Constants.s.sol";

/**
 * @title DeploySHProtocol
 * @notice Deployment script for the SessionHandler protocol's shared infrastructure
 * @dev Retrieves network configuration from HelperConfig and deploys the SHTreasury, SHOracle,
 *      SHRegistry, SpendingLimitModule, and SHFactory. Individual SessionHandler wallets are
 *      deployed later via SHFactory.deployWallet().
 *
 *      SHTreasury is the protocol's single admin root and is therefore deployed FIRST, so its
 *      address can be passed as the `owner` argument to the oracle, registry, and factory. Every
 *      one of them is born already owned by the treasury — no transferOwnership step, and no
 *      window in which the deployer EOA owns a live contract. The one back-reference the treasury
 *      needs (the registry it administers) is wired in afterwards with setRegistry, which is
 *      write-once.
 *
 *      Ownership graph after this script:
 *        config.account → SHTreasury → { SHRegistry, SHOracle, SHFactory }
 *
 *      Deployment is broadcast as config.account so that account becomes the Ownable owner of
 *      SHTreasury, and thereby the root of everything below it.
 *
 *      Supported networks are determined by HelperConfig:
 *      - Anvil (chainid 31337): deploys a local EntryPoint and uses the default anvil account
 *      - Live networks: uses the canonical ERC-4337 EntryPoint and the configured account
 */
contract DeploySHProtocol is Script {
    uint256 public constant INITIAL_PROTOCOL_FEE = 0.02e18;
    string public constant AGENT_URI = "ipfs://QmZyYpLh7qjH1n9Zt2Xqj8Vh5v6s9z5X7w8y9z0a1b2c3/metadata.json";

    /**
     * @notice Deploys the SHOracle, SHTreasury, and SHFactory and returns them alongside the network config
     * @dev Fetches the EntryPoint address and deployer account from HelperConfig, then
     *      broadcasts all deployments as config.account so that address becomes the owner.
     *
     *      Deployment steps:
     *      1. Instantiate HelperConfig to resolve chain-specific configuration
     *      2. Broadcast as config.account to set it as the Ownable owner of the treasury
     *      3. Deploy SHTreasury first, then SHOracle and SHRegistry owned by it, wire the registry
     *         back into the treasury, then the SpendingLimitModule and SHFactory
     *
     * @return factory  The newly deployed SHFactory used to deploy individual SessionHandler wallets
     * @return treasury The newly deployed SHTreasury that owns the registry, oracle, and factory
     * @return config   The resolved NetworkConfig containing the entryPoint address and deployer account
     * @return oracle   The newly deployed SHOracle
     */
    function run()
        external
        returns (SHFactory factory, SHTreasury treasury, HelperConfig.NetworkConfig memory config, SHOracle oracle)
    {
        HelperConfig helperConfig = new HelperConfig();
        config = helperConfig.getConfig();

        // Build parallel token/feed arrays for SHOracle.
        // address(0) registers native ETH. Pairs with a zero feed are skipped inside the constructor,
        // so it is safe to pass address(0) feed entries for tokens unavailable on the current network.
        address[] memory tokens = new address[](20);
        address[] memory priceFeeds = new address[](20);
        uint256[] memory heartbeats = new uint256[](20);
        tokens[0] = address(0);

        if (block.chainid == BSC_CHAIN_ID) {
            priceFeeds[0] = config.bnbUsdPriceFeed;
            heartbeats[0] = config.bnbHeartbeat;
        } else if (
            block.chainid == MAINNET_CHAIN_ID || block.chainid == SEPOLIA_CHAIN_ID || block.chainid == LOCAL_CHAIN_ID
                || block.chainid == ARB_CHAIN_ID // Arbitrum's native gas token is ETH
        ) {
            priceFeeds[0] = config.ethUsdPriceFeed;
            heartbeats[0] = config.ethHeartbeat;
        }

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
        tokens[8] = config.wbnb;
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
        tokens[13] = config.sand;
        priceFeeds[13] = config.sandUsdPriceFeed;
        heartbeats[13] = config.sandHeartbeat;
        tokens[14] = config.sushi;
        priceFeeds[14] = config.sushiUsdPriceFeed;
        heartbeats[14] = config.sushiHeartbeat;
        tokens[15] = config.wtao;
        priceFeeds[15] = config.wtaoUsdPriceFeed;
        heartbeats[15] = config.wtaoHeartbeat;
        tokens[16] = config.uni;
        priceFeeds[16] = config.uniUsdPriceFeed;
        heartbeats[16] = config.uniHeartbeat;
        tokens[17] = config.yfi;
        priceFeeds[17] = config.yfiUsdPriceFeed;
        heartbeats[17] = config.yfiHeartbeat;
        tokens[18] = config.weth;
        priceFeeds[18] = config.ethUsdPriceFeed;
        heartbeats[18] = config.ethHeartbeat;
        tokens[19] = config.usdt;
        priceFeeds[19] = config.usdtUsdPriceFeed;
        heartbeats[19] = config.usdtHeartbeat;

        vm.deal(config.account, 100 ether); // Fund the deployer account with 10 ETH for deployment costs

        // Broadcast as config.account so it becomes the Ownable owner of SHTreasury — and, through
        // it, the root of the registry, oracle, and factory deployed below.
        vm.startBroadcast(config.account);

        // 1. The admin root, deployed first so its address can own everything that follows.
        treasury = new SHTreasury();

        // 2. Price oracle, born owned by the treasury (feed admin runs through its passthroughs).
        oracle = new SHOracle(address(treasury), config.sequencerUptimeFeed, tokens, priceFeeds, heartbeats);

        // 3. Register the protocol's agent, whose id the registry records.
        uint256 agentId = IIdentityRegistry(config.identityRegistry).register(AGENT_URI);

        // 4. Config registry, owned by the treasury and paying fees to it. It carries every address
        //    a deployed wallet needs, so the factory below stores none of them itself.
        SHRegistry registry = new SHRegistry(
            address(treasury),
            INITIAL_PROTOCOL_FEE,
            address(treasury),
            address(oracle),
            config.reputationRegistry,
            config.identityRegistry,
            config.entryPoint,
            agentId
        );

        // 5. Wire the registry back into the treasury. Write-once, so this fixes the pairing.
        treasury.setRegistry(address(registry));

        // 6. The ERC-7579 spending-limit hook. It must come AFTER the registry: its constructor reads
        //    priceOracle() off the registry to fail loudly on a mis-wired deployment. That is exactly
        //    why the module is not a registry constructor argument — the two would be mutually
        //    undeployable. It is registered in the step below instead.
        SpendingLimitModule module = new SpendingLimitModule(address(registry));
        treasury.setSpendingLimitModule(address(module));

        // 7. Factory, owned by the treasury. It reads EntryPoint, both ERC-8004 registries, and the
        //    module from the registry at deploy-wallet time, so it takes only owner + registry.
        factory = new SHFactory(address(treasury), address(registry));

        // 8. Record the factory on the registry for off-chain discoverability.
        treasury.setFactory(address(factory));

        vm.stopBroadcast();
    }
}
