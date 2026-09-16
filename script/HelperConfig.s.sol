//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {EntryPoint} from "@account-abstraction/contracts/core/EntryPoint.sol";
import {ERC20Mock} from "../src/mocks/ERC20Mock.sol";
import {MockWeth} from "../src/mocks/MockWeth.sol";
import {MockV3Aggregator} from "../src/mocks/MockV3Aggregator.sol";
import {MockIdentityRegistry} from "../src/mocks/MockIdentityRegistry.sol";
import {MockReputationRegistry} from "../src/mocks/MockReputationRegistry.sol";
// forge-lint: disable-next-line(unaliased-plain-import)
import "./Constants.s.sol";

/**
 * @title HelperConfig
 * @author Conrad Japhet
 * @notice Configuration helper that resolves chain-specific deployment parameters
 *         for the SessionHandler ERC-4337 smart account system
 * @dev Abstracts away network differences so deployment and test scripts can remain
 *      chain-agnostic. Resolves the correct EntryPoint address and deployer account
 *      for the current chain at runtime.
 *
 *      Supported networks:
 *      ┌─────────────────────┬────────────┬──────────────────────────────────────────────┐
 *      │ Network             │ Chain ID   │ EntryPoint                                   │
 *      ├─────────────────────┼────────────┼──────────────────────────────────────────────┤
 *      │ Ethereum Sepolia    │ 11155111   │ ENTRYPOINT_V07 (canonical)                   │
 *      │ zkSync Sepolia      │ 300        │ address(0) — native AA, no EntryPoint needed │
 *      │ Mainnet + others    │ any        │ ENTRYPOINT_V07 (canonical)                   │
 *      │ BSC                 │ 56         │ ENTRYPOINT_V07 (canonical)                   │
 *      │ Arbitrum One        │ 42161      │ ENTRYPOINT_V07 (canonical)                   │
 *      │ Anvil (local)       │ 31337      │ Freshly deployed EntryPoint (cached)         │
 *      └─────────────────────┴────────────┴──────────────────────────────────────────────┘
 *
 *      Local Anvil config is lazily initialised and cached in sLocalNetworkConfig
 *      to avoid redeploying EntryPoint on repeated calls within the same session.
 */
contract HelperConfig is Script {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts when getConfigByChainId is called with an unrecognised chain ID
    error HelperConfig__InvalidChainId();

    /*//////////////////////////////////////////////////////////////
                                 TYPES
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Encapsulates the chain-specific addresses required for deployment
     * @param entryPoint Address of the ERC-4337 EntryPoint contract on the current chain.
     *                   Set to address(0) on zkSync which uses native account abstraction.
     * @param account    Deployer/owner address used when broadcasting transactions.
     *                   Becomes the Ownable owner of the deployed SessionHandler.
     * @param usdc       Circle USD (USDC) ERC-20 token address
     * @param dai        DAI Stablecoin ERC-20 token address
     * @param usdt       Tether (USDT) ERC-20 token address
     * @param aave       Aave (AAVE) ERC-20 token address
     * @param link       Chainlink (LINK) ERC-20 token address
     * @param oneinch    1inch Network (1INCH) ERC-20 token address. address(0) on Sepolia.
     * @param ape        ApeCoin (APE) ERC-20 token address. address(0) on Sepolia.
     * @param arb        Arbitrum (ARB) ERC-20 token address. address(0) on Sepolia.
     * @param bnb        BNB ERC-20 token address. address(0) on Sepolia.
     * @param wbtc       Wrapped Bitcoin (WBTC) ERC-20 token address
     * @param comp       Compound (COMP) ERC-20 token address. address(0) on Sepolia.
     * @param crv        Curve DAO Token (CRV) ERC-20 token address. address(0) on Sepolia.
     * @param ens        Ethereum Name Service (ENS) ERC-20 token address. address(0) on Sepolia.
     * @param wfil       Wrapped Filecoin (WFIL) ERC-20 token address. address(0) on Sepolia.
     * @param sand       The Sandbox (SAND) ERC-20 token address. address(0) on Sepolia.
     * @param sushi      SushiSwap (SUSHI) ERC-20 token address. address(0) on Sepolia.
     * @param wtao       Wrapped Bittensor (wTAO) ERC-20 token address. address(0) on Sepolia.
     * @param uni        Uniswap (UNI) ERC-20 token address
     * @param yfi        yearn.finance (YFI) ERC-20 token address. address(0) on Sepolia.
     * @param ethUsdPriceFeed     Chainlink ETH/USD price feed address
     * @param usdcUsdPriceFeed    Chainlink USDC/USD price feed address
     * @param daiUsdPriceFeed     Chainlink DAI/USD price feed address
     * @param usdtUsdPriceFeed    Chainlink USDT/USD price feed address. address(0) on Sepolia.
     * @param aaveUsdPriceFeed    Chainlink AAVE/USD price feed address. address(0) on Sepolia.
     * @param linkUsdPriceFeed    Chainlink LINK/USD price feed address. address(0) on Sepolia.
     * @param oneinchUsdPriceFeed Chainlink 1INCH/USD price feed address. address(0) on Sepolia.
     * @param apeUsdPriceFeed     Chainlink APE/USD price feed address. address(0) on Sepolia.
     * @param arbUsdPriceFeed     Chainlink ARB/USD price feed address. address(0) on Sepolia.
     * @param bnbUsdPriceFeed     Chainlink BNB/USD price feed address. address(0) on Sepolia.
     * @param btcUsdPriceFeed     Chainlink BTC/USD price feed address (used for WBTC). address(0) on Sepolia.
     * @param compUsdPriceFeed    Chainlink COMP/USD price feed address. address(0) on Sepolia.
     * @param crvUsdPriceFeed     Chainlink CRV/USD price feed address. address(0) on Sepolia.
     * @param ensUsdPriceFeed     Chainlink ENS/USD price feed address. address(0) on Sepolia.
     * @param wfilUsdPriceFeed    address(0) — no Chainlink FIL/USD feed exists on Ethereum mainnet or Sepolia.
     * @param sandUsdPriceFeed    Chainlink SAND/USD price feed address. address(0) on Sepolia.
     * @param sushiUsdPriceFeed   Chainlink SUSHI/USD price feed address. address(0) on Sepolia.
     * @param wtaoUsdPriceFeed    Chainlink TAO/USD price feed address. address(0) on Sepolia.
     * @param uniUsdPriceFeed     Chainlink UNI/USD price feed address. address(0) on Sepolia.
     * @param yfiUsdPriceFeed     Chainlink YFI/USD price feed address. address(0) on Sepolia.
     * @param wavax              Wrapped AVAX (WAVAX) ERC-20 token address. address(0) on Sepolia.
     * @param wavaxUsdPriceFeed  Chainlink AVAX/USD price feed address. address(0) on Sepolia.
     * @param wavaxHeartbeat     Chainlink AVAX/USD feed heartbeat in seconds (mainnet: 86400)
     * @param sequencerUptimeFeed Chainlink L2 Sequencer Uptime Feed for this chain, consumed by
     *                   {SHOracle}. address(0) on every chain that has no sequencer (Ethereum
     *                   mainnet, Sepolia, BSC, Anvil), which disables the check there.
     * @param imx                Immutable X (IMX) ERC-20 token address. address(0) on Sepolia.
     * @param imxUsdPriceFeed    Chainlink IMX/USD price feed address. address(0) on Sepolia.
     * @param imxHeartbeat       Chainlink IMX/USD feed heartbeat in seconds (mainnet: 86400)
     * @param knc                Kyber Network Crystal (KNC) ERC-20 token address. address(0) on Sepolia.
     * @param kncUsdPriceFeed    Chainlink KNC/USD price feed address. address(0) on Sepolia.
     * @param kncHeartbeat       Chainlink KNC/USD feed heartbeat in seconds (mainnet: 86400)
     * @param cake               PancakeSwap Token (CAKE) ERC-20 address. address(0) on Sepolia.
     * @param cakeUsdPriceFeed   Chainlink CAKE/USD price feed. address(0) on mainnet (feed deprecated Nov 2022) and Sepolia.
     * @param cakeHeartbeat      Chainlink CAKE/USD feed heartbeat in seconds (BSC: 60)
     * @param ethHeartbeat        Chainlink ETH/USD feed heartbeat in seconds (mainnet: 3600)
     * @param usdcHeartbeat       Chainlink USDC/USD feed heartbeat in seconds (mainnet: 82800)
     * @param daiHeartbeat        Chainlink DAI/USD feed heartbeat in seconds (mainnet: 3600)
     * @param usdtHeartbeat       Chainlink USDT/USD feed heartbeat in seconds (mainnet: 86400)
     * @param aaveHeartbeat       Chainlink AAVE/USD feed heartbeat in seconds (mainnet: 3600)
     * @param linkHeartbeat       Chainlink LINK/USD feed heartbeat in seconds (mainnet: 3600)
     * @param oneinchHeartbeat    Chainlink 1INCH/USD feed heartbeat in seconds (mainnet: 86400)
     * @param apeHeartbeat        Chainlink APE/USD feed heartbeat in seconds (mainnet: 86400)
     * @param arbHeartbeat        Chainlink ARB/USD feed heartbeat in seconds (mainnet: 86400)
     * @param bnbHeartbeat        Chainlink BNB/USD feed heartbeat in seconds (mainnet: 86400)
     * @param btcHeartbeat        Chainlink BTC/USD feed heartbeat in seconds (mainnet: 3600)
     * @param compHeartbeat       Chainlink COMP/USD feed heartbeat in seconds (mainnet: 3600)
     * @param crvHeartbeat        Chainlink CRV/USD feed heartbeat in seconds (mainnet: 86400)
     * @param ensHeartbeat        Chainlink ENS/USD feed heartbeat in seconds (mainnet: 86400)
     * @param sandHeartbeat       Chainlink SAND/USD feed heartbeat in seconds (mainnet: 86400)
     * @param sushiHeartbeat      Chainlink SUSHI/USD feed heartbeat in seconds (mainnet: 86400)
     * @param wtaoHeartbeat       Chainlink TAO/USD feed heartbeat in seconds (mainnet: 86400)
     * @param uniHeartbeat        Chainlink UNI/USD feed heartbeat in seconds (mainnet: 3600)
     * @param yfiHeartbeat        Chainlink YFI/USD feed heartbeat in seconds (mainnet: 86400)
     */
    struct NetworkConfig {
        address entryPoint;
        address account;
        address identityRegistry;
        address reputationRegistry;
        // Chainlink L2 Sequencer Uptime Feed — address(0) on chains with no sequencer
        address sequencerUptimeFeed;
        // Stablecoins
        address usdc;
        address dai;
        address usdt;
        // ERC-20 tokens (address(0) where no official deployment exists on the network)
        address weth;
        address aave;
        address link;
        address oneinch;
        address ape;
        address arb;
        address wbnb;
        address wbtc;
        address comp;
        address crv;
        address ens;
        address sand;
        address sushi;
        address wtao;
        address uni;
        address yfi;
        address wavax;
        address imx;
        address knc;
        address cake;
        // Chainlink price feeds
        address ethUsdPriceFeed;
        address usdcUsdPriceFeed;
        address daiUsdPriceFeed;
        address usdtUsdPriceFeed;
        address aaveUsdPriceFeed;
        address linkUsdPriceFeed;
        address oneinchUsdPriceFeed;
        address apeUsdPriceFeed;
        address arbUsdPriceFeed;
        address bnbUsdPriceFeed;
        address btcUsdPriceFeed; // BTC/USD — used for WBTC pricing
        address compUsdPriceFeed;
        address crvUsdPriceFeed;
        address ensUsdPriceFeed;
        address sandUsdPriceFeed;
        address sushiUsdPriceFeed;
        address wtaoUsdPriceFeed;
        address uniUsdPriceFeed;
        address yfiUsdPriceFeed;
        address wavaxUsdPriceFeed;
        address imxUsdPriceFeed;
        address kncUsdPriceFeed;
        address cakeUsdPriceFeed;
        // Chainlink price feed heartbeats (maximum seconds between updates)
        uint256 ethHeartbeat;
        uint256 usdcHeartbeat;
        uint256 daiHeartbeat;
        uint256 usdtHeartbeat;
        uint256 aaveHeartbeat;
        uint256 linkHeartbeat;
        uint256 oneinchHeartbeat;
        uint256 apeHeartbeat;
        uint256 arbHeartbeat;
        uint256 bnbHeartbeat;
        uint256 btcHeartbeat;
        uint256 compHeartbeat;
        uint256 crvHeartbeat;
        uint256 ensHeartbeat;
        uint256 sandHeartbeat;
        uint256 sushiHeartbeat;
        uint256 wtaoHeartbeat;
        uint256 uniHeartbeat;
        uint256 yfiHeartbeat;
        uint256 wavaxHeartbeat;
        uint256 imxHeartbeat;
        uint256 kncHeartbeat;
        uint256 cakeHeartbeat;
    }

    /*//////////////////////////////////////////////////////////////
                            STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Deployer/owner account for every network other than local Anvil — Sepolia, mainnet,
    ///         BSC and Arbitrum alike, matching the key the Makefile passes as --private-key. It
    ///         becomes the Ownable owner of SHTreasury, so it must be funded before broadcasting.
    /// @dev One address across all of them on purpose: the app self-bundles with this same key (see
    ///      deploy_wallet.LIVE_PRIVATE_KEY_ENV and anvil.py), so deployer, protocol owner and
    ///      bundler stay in sync. Acceptable on testnets and forks; a real mainnet deployment must
    ///      split the bundler off, since it is an always-online hot key while this one is the admin
    ///      root. Falls back to address(0) when SEPOLIA_ACCOUNT is unset, which fails loudly at
    ///      broadcast rather than silently deploying to a burned owner.
    address public sepoliaAccount = vm.envOr("SEPOLIA_ACCOUNT", address(0));

    /// @notice Default pre-funded account on a local Anvil node (account index 0)
    /// @dev Usable only on a bare Anvil node, never on a fork of a real chain. On real
    ///      mainnet/Sepolia/BSC this address carries an EIP-7702 delegation that sweeps any ERC-721
    ///      it receives to an attacker — a fork inherits that code, which breaks fork tests that
    ///      register an NFT to config.account, and makes it unusable as a handleOps beneficiary
    ///      (the EntryPoint's plain ETH send reverts AA91 against an address with code).
    address public constant ANVIL_BURNER_WALLET = 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;

    /**
     * @dev Cached Anvil network config. Populated on first call to getOrCreateAnvilConfig.
     *      Both fields must be non-zero for the cache to be considered valid.
     */
    NetworkConfig private sLocalNetworkConfig;

    /*//////////////////////////////////////////////////////////////
                           EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Returns the network configuration for the currently executing chain
     * @dev Delegates to getConfigByChainId using the EVM's block.chainid.
     *      Safe to call from both scripts and tests.
     * @return config NetworkConfig containing the resolved entryPoint and account addresses
     */
    function getConfig() external returns (NetworkConfig memory) {
        return getConfigByChainId(block.chainid);
    }

    /*//////////////////////////////////////////////////////////////
                           INTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Resolves and returns the NetworkConfig for a given chain ID
     * @dev Dispatches to the appropriate chain-specific config getter.
     *      Reverts with HelperConfig__InvalidChainId for unrecognised chain IDs rather than silently
     *      returning mainnet's token/feed/registry addresses, which would be wrong for that chain.
     * @param chainId The EVM chain ID to resolve configuration for
     * @return config NetworkConfig for the specified chain
     */
    function getConfigByChainId(uint256 chainId) internal returns (NetworkConfig memory) {
        if (chainId == SEPOLIA_CHAIN_ID) {
            return getEthSepoliaConfig();
        } else if (chainId == LOCAL_CHAIN_ID) {
            return getOrCreateAnvilConfig();
        } else if (chainId == MAINNET_CHAIN_ID) {
            return getMainnetConfig();
        } else if (chainId == BSC_CHAIN_ID) {
            return getBscConfig();
        } else if (chainId == ARB_CHAIN_ID) {
            return getArbConfig();
        } else {
            revert HelperConfig__InvalidChainId();
        }
    }

    /**
     * @notice Returns the Ethereum Sepolia testnet configuration
     * @dev Uses the canonical EntryPoint v0.9 address and sepoliaAccount as deployer.
     *      Ensure sepoliaAccount is funded with Sepolia ETH before broadcasting.
     * @return config NetworkConfig for Ethereum Sepolia
     */
    function getEthSepoliaConfig() internal view returns (NetworkConfig memory) {
        return NetworkConfig({
            entryPoint: ENTRYPOINT_V07,
            account: sepoliaAccount,
            identityRegistry: TESTNET_IDENTITY_REGISTRY,
            reputationRegistry: TESTNET_REPUTATION_REGISTRY,
            sequencerUptimeFeed: address(0), // L1 testnet — no sequencer
            // Stablecoins
            usdc: SPO_USDC,
            weth: SPO_WETH,
            dai: SPO_DAI,
            usdt: address(0),
            // ERC-20 tokens — address(0) where no official Sepolia deployment exists
            aave: address(0),
            link: SPO_LINK,
            oneinch: address(0), // No official Sepolia deployment
            ape: address(0), // No official Sepolia deployment
            arb: address(0), // No official Sepolia deployment
            wbnb: address(0), // No official Sepolia deployment
            wbtc: SPO_WBTC,
            comp: address(0), // No official Sepolia deployment
            crv: address(0), // No official Sepolia deployment
            ens: address(0), // No official Sepolia deployment
            sand: address(0), // No official Sepolia deployment
            sushi: address(0), // No official Sepolia deployment
            wtao: address(0), // No official Sepolia deployment
            uni: address(0),
            yfi: address(0), // No official Sepolia deployment
            wavax: address(0), // No official Sepolia deployment
            imx: address(0), // No official Sepolia deployment
            knc: address(0), // No official Sepolia deployment
            cake: address(0), // No official Sepolia deployment
            // Chainlink price feeds — only ETH, USDC, DAI, LINK, BTC have feeds on Sepolia
            ethUsdPriceFeed: SPO_ETH_USD_PRICE_FEED,
            usdcUsdPriceFeed: SPO_USDC_USD_PRICE_FEED,
            daiUsdPriceFeed: SPO_DAI_USD_PRICE_FEED,
            usdtUsdPriceFeed: address(0), // No USDT/USD feed on Sepolia
            aaveUsdPriceFeed: address(0),
            linkUsdPriceFeed: SPO_LINK_USD_PRICE_FEED,
            oneinchUsdPriceFeed: address(0),
            apeUsdPriceFeed: address(0),
            arbUsdPriceFeed: address(0),
            bnbUsdPriceFeed: address(0),
            btcUsdPriceFeed: SPO_BTC_USD_PRICE_FEED,
            compUsdPriceFeed: address(0),
            crvUsdPriceFeed: address(0),
            ensUsdPriceFeed: address(0),
            sandUsdPriceFeed: address(0),
            sushiUsdPriceFeed: address(0),
            wtaoUsdPriceFeed: address(0),
            uniUsdPriceFeed: address(0),
            yfiUsdPriceFeed: address(0),
            wavaxUsdPriceFeed: address(0),
            imxUsdPriceFeed: address(0),
            kncUsdPriceFeed: address(0),
            cakeUsdPriceFeed: address(0),
            // Heartbeats — Sepolia's Chainlink nodes update noticeably less often than mainnet's
            // (observed gaps of ~16-17h on USDC/DAI in practice, vs ETH/LINK/BTC which typically
            // stay under 1h) — see HEARTBEAT_72H's doc comment in Constants.s.sol. Using one
            // wide window uniformly across all Sepolia feeds rather than tuning per-feed, since
            // this is an accepted testnet-oracle characteristic, not a real staleness risk.
            ethHeartbeat: HEARTBEAT_72H,
            usdcHeartbeat: HEARTBEAT_72H,
            daiHeartbeat: HEARTBEAT_72H,
            usdtHeartbeat: HEARTBEAT_72H,
            aaveHeartbeat: HEARTBEAT_72H,
            linkHeartbeat: HEARTBEAT_72H,
            oneinchHeartbeat: HEARTBEAT_72H,
            apeHeartbeat: HEARTBEAT_72H,
            arbHeartbeat: HEARTBEAT_72H,
            bnbHeartbeat: HEARTBEAT_72H,
            btcHeartbeat: HEARTBEAT_72H,
            compHeartbeat: HEARTBEAT_72H,
            crvHeartbeat: HEARTBEAT_72H,
            ensHeartbeat: HEARTBEAT_72H,
            sandHeartbeat: HEARTBEAT_72H,
            sushiHeartbeat: HEARTBEAT_72H,
            wtaoHeartbeat: HEARTBEAT_72H,
            uniHeartbeat: HEARTBEAT_72H,
            yfiHeartbeat: HEARTBEAT_72H,
            wavaxHeartbeat: HEARTBEAT_72H,
            imxHeartbeat: HEARTBEAT_72H,
            kncHeartbeat: HEARTBEAT_72H,
            cakeHeartbeat: HEARTBEAT_72H
        });
    }

    /**
     * @notice Returns the mainnet (and generic EVM chain) configuration
     * @dev Assumes the canonical EntryPoint v0.9 is deployed at ENTRYPOINT_V07.
     *      Used as the fallback for any unrecognised chain ID.
     *      Ensure sepoliaAccount is funded before broadcasting on any live network.
     * @return config NetworkConfig for Ethereum mainnet and compatible chains
     */
    function getMainnetConfig() internal view returns (NetworkConfig memory) {
        return NetworkConfig({
            entryPoint: ENTRYPOINT_V07,
            account: sepoliaAccount,
            identityRegistry: MNT_IDENTITY_REGISTRY,
            reputationRegistry: MNT_REPUTATION_REGISTRY,
            sequencerUptimeFeed: address(0), // L1 — no sequencer
            // Stablecoins
            usdc: MNT_USDC,
            dai: MNT_DAI,
            usdt: MNT_USDT,
            // ERC-20 tokens
            weth: MNT_WETH,
            aave: MNT_AAVE,
            link: MNT_LINK,
            oneinch: MNT_ONEINCH,
            ape: MNT_APE,
            arb: MNT_ARB,
            wbnb: MNT_BNB,
            wbtc: MNT_WBTC,
            comp: MNT_COMP,
            crv: MNT_CRV,
            ens: MNT_ENS,
            sand: MNT_SAND,
            sushi: MNT_SUSHI,
            wtao: MNT_WTAO,
            uni: MNT_UNI,
            yfi: MNT_YFI,
            wavax: MNT_WAVAX,
            imx: MNT_IMX,
            knc: MNT_KNC,
            cake: address(0),
            // Chainlink price feeds
            ethUsdPriceFeed: MNT_ETH_USD_PRICE_FEED,
            usdcUsdPriceFeed: MNT_USDC_USD_PRICE_FEED,
            daiUsdPriceFeed: MNT_DAI_USD_PRICE_FEED,
            usdtUsdPriceFeed: MNT_USDT_USD_PRICE_FEED,
            aaveUsdPriceFeed: MNT_AAVE_USD_PRICE_FEED,
            linkUsdPriceFeed: MNT_LINK_USD_PRICE_FEED,
            oneinchUsdPriceFeed: MNT_ONEINCH_USD_PRICE_FEED,
            apeUsdPriceFeed: MNT_APE_USD_PRICE_FEED,
            arbUsdPriceFeed: MNT_ARB_USD_PRICE_FEED,
            bnbUsdPriceFeed: MNT_BNB_USD_PRICE_FEED,
            btcUsdPriceFeed: MNT_BTC_USD_PRICE_FEED,
            compUsdPriceFeed: MNT_COMP_USD_PRICE_FEED,
            crvUsdPriceFeed: MNT_CRV_USD_PRICE_FEED,
            ensUsdPriceFeed: MNT_ENS_USD_PRICE_FEED,
            sandUsdPriceFeed: MNT_SAND_USD_PRICE_FEED,
            sushiUsdPriceFeed: MNT_SUSHI_USD_PRICE_FEED,
            wtaoUsdPriceFeed: MNT_WTAO_USD_PRICE_FEED,
            uniUsdPriceFeed: MNT_UNI_USD_PRICE_FEED,
            yfiUsdPriceFeed: MNT_YFI_USD_PRICE_FEED,
            wavaxUsdPriceFeed: MNT_WAVAX_USD_PRICE_FEED,
            imxUsdPriceFeed: MNT_IMX_USD_PRICE_FEED,
            kncUsdPriceFeed: MNT_KNC_USD_PRICE_FEED,
            cakeUsdPriceFeed: address(0), // Chainlink CAKE/USD feed deprecated Nov 2022
            // Heartbeats sourced from Chainlink reference data (feeds-mainnet.json)
            ethHeartbeat: HEARTBEAT_1H,
            usdcHeartbeat: HEARTBEAT_24H,
            daiHeartbeat: HEARTBEAT_1H,
            usdtHeartbeat: HEARTBEAT_24H,
            aaveHeartbeat: HEARTBEAT_1H,
            linkHeartbeat: HEARTBEAT_1H,
            oneinchHeartbeat: HEARTBEAT_24H,
            apeHeartbeat: HEARTBEAT_24H,
            arbHeartbeat: HEARTBEAT_24H,
            bnbHeartbeat: HEARTBEAT_24H,
            btcHeartbeat: HEARTBEAT_1H,
            compHeartbeat: HEARTBEAT_1H,
            crvHeartbeat: HEARTBEAT_24H,
            ensHeartbeat: HEARTBEAT_24H,
            sandHeartbeat: HEARTBEAT_24H,
            sushiHeartbeat: HEARTBEAT_24H,
            wtaoHeartbeat: HEARTBEAT_24H,
            uniHeartbeat: HEARTBEAT_1H,
            yfiHeartbeat: HEARTBEAT_24H,
            wavaxHeartbeat: HEARTBEAT_24H,
            imxHeartbeat: HEARTBEAT_24H,
            kncHeartbeat: HEARTBEAT_24H,
            cakeHeartbeat: HEARTBEAT_24H
        });
    }

    /**
     * @notice Returns the BSC (Binance Smart Chain) mainnet configuration
     * @dev Tokens without a BSC deployment
     *      (APE, ARB, ENS, SAND, wTAO, IMX) are set to address(0).
     * @return config NetworkConfig for BSC mainnet
     */
    function getBscConfig() internal view returns (NetworkConfig memory) {
        return NetworkConfig({
            entryPoint: ENTRYPOINT_V07,
            account: sepoliaAccount,
            identityRegistry: MNT_IDENTITY_REGISTRY,
            reputationRegistry: MNT_REPUTATION_REGISTRY,
            sequencerUptimeFeed: address(0), // BSC is its own L1 — no sequencer
            // Stablecoins
            usdc: BSC_USDC,
            dai: BSC_DAI,
            usdt: BSC_USDT,
            // ERC-20 tokens
            weth: BSC_WETH,
            aave: BSC_AAVE,
            link: BSC_LINK,
            oneinch: BSC_ONEINCH,
            ape: address(0), // No BSC deployment
            arb: address(0), // No BSC deployment
            wbnb: BSC_WBNB, // Wrapped BNB — the IWETH-compatible ERC-20 form of BSC's native gas token
            wbtc: BSC_WBTC,
            comp: BSC_COMP,
            crv: BSC_CRV,
            ens: address(0), // No BSC deployment
            sand: address(0), // No BSC deployment
            sushi: BSC_SUSHI,
            wtao: address(0), // No BSC deployment
            uni: BSC_UNI,
            yfi: BSC_YFI,
            wavax: BSC_WAVAX,
            imx: address(0), // No BSC deployment
            knc: BSC_KNC,
            cake: BSC_CAKE,
            // Chainlink price feeds
            ethUsdPriceFeed: BSC_ETH_USD_PRICE_FEED,
            usdcUsdPriceFeed: BSC_USDC_USD_PRICE_FEED,
            daiUsdPriceFeed: BSC_DAI_USD_PRICE_FEED,
            usdtUsdPriceFeed: BSC_USDT_USD_PRICE_FEED,
            aaveUsdPriceFeed: BSC_AAVE_USD_PRICE_FEED,
            linkUsdPriceFeed: BSC_LINK_USD_PRICE_FEED,
            oneinchUsdPriceFeed: BSC_ONEINCH_USD_PRICE_FEED,
            apeUsdPriceFeed: address(0),
            arbUsdPriceFeed: address(0),
            bnbUsdPriceFeed: BSC_BNB_USD_PRICE_FEED,
            btcUsdPriceFeed: BSC_BTC_USD_PRICE_FEED,
            compUsdPriceFeed: BSC_COMP_USD_PRICE_FEED,
            crvUsdPriceFeed: BSC_CRV_USD_PRICE_FEED,
            ensUsdPriceFeed: address(0),
            sandUsdPriceFeed: address(0),
            sushiUsdPriceFeed: BSC_SUSHI_USD_PRICE_FEED,
            wtaoUsdPriceFeed: address(0),
            uniUsdPriceFeed: BSC_UNI_USD_PRICE_FEED,
            yfiUsdPriceFeed: BSC_YFI_USD_PRICE_FEED,
            wavaxUsdPriceFeed: BSC_AVAX_USD_PRICE_FEED,
            imxUsdPriceFeed: address(0),
            kncUsdPriceFeed: BSC_KNC_USD_PRICE_FEED,
            cakeUsdPriceFeed: BSC_CAKE_USD_PRICE_FEED,
            // Heartbeats — sourced from Chainlink BSC feed data
            ethHeartbeat: HEARTBEAT_1H,
            usdcHeartbeat: HEARTBEAT_24H,
            daiHeartbeat: HEARTBEAT_24H,
            usdtHeartbeat: HEARTBEAT_24H,
            aaveHeartbeat: HEARTBEAT_1H,
            linkHeartbeat: HEARTBEAT_1H,
            oneinchHeartbeat: HEARTBEAT_24H,
            apeHeartbeat: HEARTBEAT_24H,
            arbHeartbeat: HEARTBEAT_24H,
            bnbHeartbeat: HEARTBEAT_24H,
            btcHeartbeat: HEARTBEAT_1H,
            compHeartbeat: HEARTBEAT_24H,
            crvHeartbeat: HEARTBEAT_24H,
            ensHeartbeat: HEARTBEAT_24H,
            sandHeartbeat: HEARTBEAT_24H,
            sushiHeartbeat: HEARTBEAT_24H,
            wtaoHeartbeat: HEARTBEAT_24H,
            uniHeartbeat: HEARTBEAT_24H,
            yfiHeartbeat: HEARTBEAT_24H,
            wavaxHeartbeat: HEARTBEAT_24H,
            imxHeartbeat: HEARTBEAT_24H,
            kncHeartbeat: HEARTBEAT_24H,
            cakeHeartbeat: HEARTBEAT_1H // BSC CAKE/USD feed heartbeat is 1 min; 1h gives a safety buffer
        });
    }

    /**
     * @notice Returns the Arbitrum One mainnet configuration
     * @dev Arbitrum's native gas token is ETH, so `ethUsdPriceFeed` prices the native asset here
     *      exactly as it does on Ethereum mainnet — there is no separate native feed to resolve.
     *
     *      Two independent reasons put a zero in this config, and both zero the token AND its feed
     *      together. DeploySHProtocol pairs each token slot with its feed slot positionally, and
     *      SHOracle treats a zero token as the native-ETH sentinel — so a zero token left next to a
     *      live feed would silently repoint native ETH at that feed. Keeping the pairs symmetric is
     *      what makes the omissions safe:
     *        - No Chainlink USD feed on Arbitrum: ENS, SAND, IMX, KNC (KNC and IMX are deployed on
     *          Arbitrum, but an unpriced token is unusable to the spending-limit hook).
     *        - No credible token deployment on Arbitrum: BNB, AVAX, wTAO. Feeds for all three exist,
     *          but no deployment appears on Arbitrum's canonical bridge list or CoinGecko's Arbitrum
     *          list, and the closest on-chain matches carry dust supply (~3.8 BNB), so there is
     *          nothing safe to point the feed at.
     * @return config NetworkConfig for Arbitrum One
     */
    function getArbConfig() internal view returns (NetworkConfig memory) {
        return NetworkConfig({
            entryPoint: ENTRYPOINT_V07,
            account: sepoliaAccount,
            identityRegistry: MNT_IDENTITY_REGISTRY,
            reputationRegistry: MNT_REPUTATION_REGISTRY,
            sequencerUptimeFeed: ARB_SEQUENCER_UPTIME_FEED,
            // Stablecoins
            usdc: ARB_USDC,
            dai: ARB_DAI,
            usdt: ARB_USDT,
            // ERC-20 tokens
            weth: ARB_WETH,
            aave: ARB_AAVE,
            link: ARB_LINK,
            oneinch: ARB_ONEINCH,
            ape: ARB_APE,
            arb: ARB_ARB,
            wbnb: address(0), // No credible BNB deployment on Arbitrum
            wbtc: ARB_WBTC,
            comp: ARB_COMP,
            crv: ARB_CRV,
            ens: address(0), // No ENS/USD feed on Arbitrum
            sand: address(0), // No SAND/USD feed on Arbitrum
            sushi: ARB_SUSHI,
            wtao: address(0), // No credible wTAO deployment on Arbitrum
            uni: ARB_UNI,
            yfi: ARB_YFI,
            wavax: address(0), // No credible WAVAX deployment on Arbitrum
            imx: address(0), // Deployed on Arbitrum, but no IMX/USD feed
            knc: address(0), // Deployed on Arbitrum, but no KNC/USD feed
            cake: ARB_CAKE,
            // Chainlink price feeds
            ethUsdPriceFeed: ARB_ETH_USD_PRICE_FEED,
            usdcUsdPriceFeed: ARB_USDC_USD_PRICE_FEED,
            daiUsdPriceFeed: ARB_DAI_USD_PRICE_FEED,
            usdtUsdPriceFeed: ARB_USDT_USD_PRICE_FEED,
            aaveUsdPriceFeed: ARB_AAVE_USD_PRICE_FEED,
            linkUsdPriceFeed: ARB_LINK_USD_PRICE_FEED,
            oneinchUsdPriceFeed: ARB_ONEINCH_USD_PRICE_FEED,
            apeUsdPriceFeed: ARB_APE_USD_PRICE_FEED,
            arbUsdPriceFeed: ARB_ARB_USD_PRICE_FEED,
            bnbUsdPriceFeed: address(0), // Feed exists, but zeroed to match the absent wbnb token
            btcUsdPriceFeed: ARB_BTC_USD_PRICE_FEED,
            compUsdPriceFeed: ARB_COMP_USD_PRICE_FEED,
            crvUsdPriceFeed: ARB_CRV_USD_PRICE_FEED,
            ensUsdPriceFeed: address(0),
            sandUsdPriceFeed: address(0),
            sushiUsdPriceFeed: ARB_SUSHI_USD_PRICE_FEED,
            wtaoUsdPriceFeed: address(0), // Feed exists, but zeroed to match the absent wtao token
            uniUsdPriceFeed: ARB_UNI_USD_PRICE_FEED,
            yfiUsdPriceFeed: ARB_YFI_USD_PRICE_FEED,
            wavaxUsdPriceFeed: address(0), // Feed exists, but zeroed to match the absent wavax token
            imxUsdPriceFeed: address(0),
            kncUsdPriceFeed: address(0),
            cakeUsdPriceFeed: ARB_CAKE_USD_PRICE_FEED,
            // Heartbeats — each rounded UP to the nearest bucket from the feed's published heartbeat
            // in Chainlink's Arbitrum reference data. ETH/BTC/LINK publish 1755s, USDC/USDT/CAKE
            // publish 255s, and COMP/CRV publish 3600s, so all seven sit inside HEARTBEAT_1H.
            ethHeartbeat: HEARTBEAT_1H,
            usdcHeartbeat: HEARTBEAT_1H,
            daiHeartbeat: HEARTBEAT_24H,
            usdtHeartbeat: HEARTBEAT_1H,
            aaveHeartbeat: HEARTBEAT_24H,
            linkHeartbeat: HEARTBEAT_1H,
            oneinchHeartbeat: HEARTBEAT_24H,
            apeHeartbeat: HEARTBEAT_24H,
            arbHeartbeat: HEARTBEAT_24H,
            bnbHeartbeat: HEARTBEAT_24H,
            btcHeartbeat: HEARTBEAT_1H,
            compHeartbeat: HEARTBEAT_1H,
            crvHeartbeat: HEARTBEAT_1H,
            ensHeartbeat: HEARTBEAT_24H,
            sandHeartbeat: HEARTBEAT_24H,
            sushiHeartbeat: HEARTBEAT_24H,
            wtaoHeartbeat: HEARTBEAT_24H,
            uniHeartbeat: HEARTBEAT_24H,
            yfiHeartbeat: HEARTBEAT_24H,
            wavaxHeartbeat: HEARTBEAT_24H,
            imxHeartbeat: HEARTBEAT_24H,
            kncHeartbeat: HEARTBEAT_24H,
            cakeHeartbeat: HEARTBEAT_1H
        });
    }

    /**
     * @notice Returns the local Anvil configuration, deploying a fresh EntryPoint if needed
     * @dev Lazily deploys a new EntryPoint contract on the first call and caches the result
     *      in sLocalNetworkConfig. Subsequent calls return the cached config without
     *      redeploying. Cache validity is determined by both fields being non-zero.
     *
     *      The EntryPoint is deployed without vm.startBroadcast since this is an internal
     *      setup step, not a user-facing deployment.
     * @return config NetworkConfig for the local Anvil node with a freshly deployed EntryPoint
     */
    function getOrCreateAnvilConfig() internal returns (NetworkConfig memory) {
        // Return cached config if EntryPoint has already been deployed this session
        if (sLocalNetworkConfig.entryPoint != address(0) && sLocalNetworkConfig.account != address(0)) {
            return sLocalNetworkConfig;
        } else {
            vm.startBroadcast();

            EntryPoint entryPoint = new EntryPoint();

            // Stablecoin mocks
            ERC20Mock usdc = new ERC20Mock("Circle USD", "USDC", 6);
            ERC20Mock dai = new ERC20Mock("DAI Stablecoin", "DAI", 18);
            ERC20Mock usdt = new ERC20Mock("Tether USD", "USDT", 6);

            // ERC-20 token mocks
            MockWeth weth = new MockWeth("Wrapped Ether", "WETH", 18);
            ERC20Mock aave = new ERC20Mock("Aave Token", "AAVE", 18);
            ERC20Mock link = new ERC20Mock("Chainlink Token", "LINK", 18);
            ERC20Mock oneinch = new ERC20Mock("1inch Token", "1INCH", 18);
            ERC20Mock ape = new ERC20Mock("ApeCoin", "APE", 18);
            ERC20Mock arb = new ERC20Mock("Arbitrum", "ARB", 18);
            ERC20Mock bnb = new ERC20Mock("BNB", "BNB", 18);
            ERC20Mock wbtc = new ERC20Mock("Wrapped Bitcoin", "WBTC", 18);
            ERC20Mock comp = new ERC20Mock("Compound", "COMP", 18);
            ERC20Mock crv = new ERC20Mock("Curve DAO Token", "CRV", 18);
            ERC20Mock ens = new ERC20Mock("Ethereum Name Service", "ENS", 18);
            ERC20Mock sand = new ERC20Mock("The Sandbox", "SAND", 18);
            ERC20Mock sushi = new ERC20Mock("SushiSwap", "SUSHI", 18);
            ERC20Mock wtao = new ERC20Mock("Wrapped TAO", "wTAO", 18);
            ERC20Mock uni = new ERC20Mock("Uniswap", "UNI", 18);
            ERC20Mock yfi = new ERC20Mock("yearn.finance", "YFI", 18);
            ERC20Mock wavax = new ERC20Mock("Wrapped AVAX", "WAVAX", 18);
            ERC20Mock imx = new ERC20Mock("Immutable X", "IMX", 18);
            ERC20Mock knc = new ERC20Mock("Kyber Network Crystal", "KNC", 18);
            ERC20Mock cake = new ERC20Mock("PancakeSwap Token", "Cake", 18);

            // Stablecoin price feed mocks
            MockV3Aggregator ethUsdPriceFeed = new MockV3Aggregator(DECIMALS, ETH_USD_PRICE);
            MockV3Aggregator usdcUsdPriceFeed = new MockV3Aggregator(DECIMALS, USDC_USD_PRICE);
            MockV3Aggregator daiUsdPriceFeed = new MockV3Aggregator(DECIMALS, DAI_USD_PRICE);
            MockV3Aggregator usdtUsdPriceFeed = new MockV3Aggregator(DECIMALS, USDT_USD_PRICE);

            // ERC-20 token price feed mocks
            MockV3Aggregator aaveUsdPriceFeed = new MockV3Aggregator(DECIMALS, AAVE_USD_PRICE);
            MockV3Aggregator linkUsdPriceFeed = new MockV3Aggregator(DECIMALS, LINK_USD_PRICE);
            MockV3Aggregator oneinchUsdPriceFeed = new MockV3Aggregator(DECIMALS, ONEINCH_USD_PRICE);
            MockV3Aggregator apeUsdPriceFeed = new MockV3Aggregator(DECIMALS, APE_USD_PRICE);
            MockV3Aggregator arbUsdPriceFeed = new MockV3Aggregator(DECIMALS, ARB_USD_PRICE);
            MockV3Aggregator bnbUsdPriceFeed = new MockV3Aggregator(DECIMALS, BNB_USD_PRICE);
            MockV3Aggregator btcUsdPriceFeed = new MockV3Aggregator(DECIMALS, BTC_USD_PRICE);
            MockV3Aggregator compUsdPriceFeed = new MockV3Aggregator(DECIMALS, COMP_USD_PRICE);
            MockV3Aggregator crvUsdPriceFeed = new MockV3Aggregator(DECIMALS, CRV_USD_PRICE);
            MockV3Aggregator ensUsdPriceFeed = new MockV3Aggregator(DECIMALS, ENS_USD_PRICE);
            MockV3Aggregator sandUsdPriceFeed = new MockV3Aggregator(DECIMALS, SAND_USD_PRICE);
            MockV3Aggregator sushiUsdPriceFeed = new MockV3Aggregator(DECIMALS, SUSHI_USD_PRICE);
            MockV3Aggregator wtaoUsdPriceFeed = new MockV3Aggregator(DECIMALS, TAO_USD_PRICE);
            MockV3Aggregator uniUsdPriceFeed = new MockV3Aggregator(DECIMALS, UNI_USD_PRICE);
            MockV3Aggregator yfiUsdPriceFeed = new MockV3Aggregator(DECIMALS, YFI_USD_PRICE);
            MockV3Aggregator wavaxUsdPriceFeed = new MockV3Aggregator(DECIMALS, WAVAX_USD_PRICE);
            MockV3Aggregator imxUsdPriceFeed = new MockV3Aggregator(DECIMALS, IMX_USD_PRICE);
            MockV3Aggregator kncUsdPriceFeed = new MockV3Aggregator(DECIMALS, KNC_USD_PRICE);
            MockV3Aggregator cakeUsdPriceFeed = new MockV3Aggregator(DECIMALS, CAKE_USD_PRICE);

            MockIdentityRegistry identityRegistry = new MockIdentityRegistry();
            MockReputationRegistry reputationRegistry = new MockReputationRegistry(address(identityRegistry));

            vm.stopBroadcast();

            sLocalNetworkConfig = NetworkConfig({
                entryPoint: address(entryPoint),
                account: ANVIL_BURNER_WALLET,
                identityRegistry: address(identityRegistry),
                reputationRegistry: address(reputationRegistry),
                // Left unset so the ~140 local unit tests keep exercising the no-sequencer path.
                // The sequencer branches are covered by tests that build their own oracle against
                // a MockV3Aggregator uptime feed, and end-to-end by the Arbitrum fork suite.
                sequencerUptimeFeed: address(0),
                // Stablecoins
                usdc: address(usdc),
                dai: address(dai),
                usdt: address(usdt),
                // ERC-20 tokens
                weth: address(weth),
                aave: address(aave),
                link: address(link),
                oneinch: address(oneinch),
                ape: address(ape),
                arb: address(arb),
                wbnb: address(bnb),
                wbtc: address(wbtc),
                comp: address(comp),
                crv: address(crv),
                ens: address(ens),
                sand: address(sand),
                sushi: address(sushi),
                wtao: address(wtao),
                uni: address(uni),
                yfi: address(yfi),
                wavax: address(wavax),
                imx: address(imx),
                knc: address(knc),
                cake: address(cake),
                // Price feeds
                ethUsdPriceFeed: address(ethUsdPriceFeed),
                usdcUsdPriceFeed: address(usdcUsdPriceFeed),
                daiUsdPriceFeed: address(daiUsdPriceFeed),
                usdtUsdPriceFeed: address(usdtUsdPriceFeed),
                aaveUsdPriceFeed: address(aaveUsdPriceFeed),
                linkUsdPriceFeed: address(linkUsdPriceFeed),
                oneinchUsdPriceFeed: address(oneinchUsdPriceFeed),
                apeUsdPriceFeed: address(apeUsdPriceFeed),
                arbUsdPriceFeed: address(arbUsdPriceFeed),
                bnbUsdPriceFeed: address(bnbUsdPriceFeed),
                btcUsdPriceFeed: address(btcUsdPriceFeed),
                compUsdPriceFeed: address(compUsdPriceFeed),
                crvUsdPriceFeed: address(crvUsdPriceFeed),
                ensUsdPriceFeed: address(ensUsdPriceFeed),
                sandUsdPriceFeed: address(sandUsdPriceFeed),
                sushiUsdPriceFeed: address(sushiUsdPriceFeed),
                wtaoUsdPriceFeed: address(wtaoUsdPriceFeed),
                uniUsdPriceFeed: address(uniUsdPriceFeed),
                yfiUsdPriceFeed: address(yfiUsdPriceFeed),
                wavaxUsdPriceFeed: address(wavaxUsdPriceFeed),
                imxUsdPriceFeed: address(imxUsdPriceFeed),
                kncUsdPriceFeed: address(kncUsdPriceFeed),
                cakeUsdPriceFeed: address(cakeUsdPriceFeed),
                // Heartbeats — use 1 hour for all Anvil mock feeds
                ethHeartbeat: HEARTBEAT_1H,
                usdcHeartbeat: HEARTBEAT_1H,
                daiHeartbeat: HEARTBEAT_1H,
                usdtHeartbeat: HEARTBEAT_1H,
                aaveHeartbeat: HEARTBEAT_1H,
                linkHeartbeat: HEARTBEAT_1H,
                oneinchHeartbeat: HEARTBEAT_1H,
                apeHeartbeat: HEARTBEAT_1H,
                arbHeartbeat: HEARTBEAT_1H,
                bnbHeartbeat: HEARTBEAT_1H,
                btcHeartbeat: HEARTBEAT_1H,
                compHeartbeat: HEARTBEAT_1H,
                crvHeartbeat: HEARTBEAT_1H,
                ensHeartbeat: HEARTBEAT_1H,
                sandHeartbeat: HEARTBEAT_1H,
                sushiHeartbeat: HEARTBEAT_1H,
                wtaoHeartbeat: HEARTBEAT_1H,
                uniHeartbeat: HEARTBEAT_1H,
                yfiHeartbeat: HEARTBEAT_1H,
                wavaxHeartbeat: HEARTBEAT_1H,
                imxHeartbeat: HEARTBEAT_1H,
                kncHeartbeat: HEARTBEAT_1H,
                cakeHeartbeat: HEARTBEAT_1H
            });
            return sLocalNetworkConfig;
        }
    }
}
