//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {EntryPoint} from "@account-abstraction/contracts/core/EntryPoint.sol";
import {ERC20Mock} from "../src/mocks/ERC20Mock.sol";
import {MockWeth} from "../src/mocks/MockWeth.sol";
import {MockV3Aggregator} from "../src/mocks/MockV3Aggregator.sol";

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
 *      │ Anvil (local)       │ 31337      │ Freshly deployed EntryPoint (cached)         │
 *      └─────────────────────┴────────────┴──────────────────────────────────────────────┘
 *
 *      Local Anvil config is lazily initialised and cached in s_localNetworkConfig
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
     *@param uniswapRouter Address of the Uniswap V2 Router02 contract on the current chain. Set to address(0) on chains where Uniswap is unavailable.
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
     * @param mkr        Maker (MKR) ERC-20 token address. address(0) on Sepolia.
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
     * @param mkrUsdPriceFeed     Chainlink MKR/USD price feed address. address(0) on Sepolia.
     * @param sandUsdPriceFeed    Chainlink SAND/USD price feed address. address(0) on Sepolia.
     * @param sushiUsdPriceFeed   Chainlink SUSHI/USD price feed address. address(0) on Sepolia.
     * @param wtaoUsdPriceFeed    Chainlink TAO/USD price feed address. address(0) on Sepolia.
     * @param uniUsdPriceFeed     Chainlink UNI/USD price feed address. address(0) on Sepolia.
     * @param yfiUsdPriceFeed     Chainlink YFI/USD price feed address. address(0) on Sepolia.
     * @param wavax              Wrapped AVAX (WAVAX) ERC-20 token address. address(0) on Sepolia.
     * @param wavaxUsdPriceFeed  Chainlink AVAX/USD price feed address. address(0) on Sepolia.
     * @param wavaxHeartbeat     Chainlink AVAX/USD feed heartbeat in seconds (mainnet: 86400)
     * @param bat                Basic Attention Token (BAT) ERC-20 token address. address(0) on Sepolia.
     * @param batUsdPriceFeed    Chainlink BAT/USD price feed address. address(0) on Sepolia.
     * @param batHeartbeat       Chainlink BAT/USD feed heartbeat in seconds (mainnet: 86400)
     * @param imx                Immutable X (IMX) ERC-20 token address. address(0) on Sepolia.
     * @param imxUsdPriceFeed    Chainlink IMX/USD price feed address. address(0) on Sepolia.
     * @param imxHeartbeat       Chainlink IMX/USD feed heartbeat in seconds (mainnet: 86400)
     * @param knc                Kyber Network Crystal (KNC) ERC-20 token address. address(0) on Sepolia.
     * @param kncUsdPriceFeed    Chainlink KNC/USD price feed address. address(0) on Sepolia.
     * @param kncHeartbeat       Chainlink KNC/USD feed heartbeat in seconds (mainnet: 86400)
     * @param rdnt               Radiant Capital (RDNT) ERC-20 token address. address(0) on Sepolia.
     * @param rdntUsdPriceFeed   Chainlink RDNT/USD price feed address. address(0) on Sepolia.
     * @param rdntHeartbeat      Chainlink RDNT/USD feed heartbeat in seconds (mainnet: 86400)
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
     * @param mkrHeartbeat        Chainlink MKR/USD feed heartbeat in seconds (mainnet: 3600)
     * @param sandHeartbeat       Chainlink SAND/USD feed heartbeat in seconds (mainnet: 86400)
     * @param sushiHeartbeat      Chainlink SUSHI/USD feed heartbeat in seconds (mainnet: 86400)
     * @param wtaoHeartbeat       Chainlink TAO/USD feed heartbeat in seconds (mainnet: 86400)
     * @param uniHeartbeat        Chainlink UNI/USD feed heartbeat in seconds (mainnet: 3600)
     * @param yfiHeartbeat        Chainlink YFI/USD feed heartbeat in seconds (mainnet: 86400)
     */
    struct NetworkConfig {
        address entryPoint;
        address account;
        address uniswapRouter;
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
        address bnb;
        address wbtc;
        address comp;
        address crv;
        address ens;
        address mkr;
        address sand;
        address sushi;
        address wtao;
        address uni;
        address yfi;
        address wavax;
        address bat;
        address imx;
        address knc;
        address rdnt;
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
        address mkrUsdPriceFeed;
        address sandUsdPriceFeed;
        address sushiUsdPriceFeed;
        address wtaoUsdPriceFeed;
        address uniUsdPriceFeed;
        address yfiUsdPriceFeed;
        address wavaxUsdPriceFeed;
        address batUsdPriceFeed;
        address imxUsdPriceFeed;
        address kncUsdPriceFeed;
        address rdntUsdPriceFeed;
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
        uint256 mkrHeartbeat;
        uint256 sandHeartbeat;
        uint256 sushiHeartbeat;
        uint256 wtaoHeartbeat;
        uint256 uniHeartbeat;
        uint256 yfiHeartbeat;
        uint256 wavaxHeartbeat;
        uint256 batHeartbeat;
        uint256 imxHeartbeat;
        uint256 kncHeartbeat;
        uint256 rdntHeartbeat;
    }

    /*//////////////////////////////////////////////////////////////
                            STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Chain ID for the Ethereum Sepolia testnet
    uint256 public constant ETH_SEPOLIA_CHAIN_ID = 11155111;

    /// @notice Chain ID for the zkSync Sepolia testnet
    uint256 public constant ZKSYNC_SEPOLIA_CHAIN_ID = 300;

    /// @notice Chain ID used by a local Anvil node
    uint256 public constant LOCAL_CHAIN_ID = 31337;

    /// @notice Deployer account used on live networks — must be funded before broadcasting
    address public SEPOLIA_ACCOUNT = vm.envAddress("SEPOLIA_ACCOUNT");

    /// @notice Default pre-funded account on a local Anvil node (account index 0)
    address public constant ANVIL_BURNER_WALLET = 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;

    /// @notice The number of decimals the price from the V3Aggregator will return
    uint8 public constant DECIMALS = 8;

    /// @notice The latest price of ETH in USD from the MockV3Aggregator
    int256 public constant ETH_USD_PRICE = 1000e8;

    /// @notice The latest price of USDC in USD from the MockV3Aggregator
    int256 public constant USDC_USD_PRICE = 0.998e8;

    /// @notice The latest price of DAI in USD from the MockV3Aggregator
    int256 public constant DAI_USD_PRICE = 1.2e8;

    /// @notice Mock price of Aave (AAVE) in USD from the MockV3Aggregator
    int256 public constant AAVE_USD_PRICE = 119e8;

    /// @notice Mock price of Chainlink (LINK) in USD from the MockV3Aggregator — sourced 2026-03-16
    int256 public constant LINK_USD_PRICE = 9.21e8;

    /// @notice Mock price of 1inch Network (1INCH) in USD from the MockV3Aggregator
    int256 public constant ONEINCH_USD_PRICE = 0.1e8;

    /// @notice Mock price of ApeCoin (APE) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant APE_USD_PRICE = 0.1e8;

    /// @notice Mock price of Arbitrum (ARB) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant ARB_USD_PRICE = 0.1e8;

    /// @notice Mock price of BNB in USD — sourced 2026-03-16, 8 decimals
    int256 public constant BNB_USD_PRICE = 674.03e8;

    /// @notice Mock price of Bitcoin (BTC) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant BTC_USD_PRICE = 71498.24e8;

    /// @notice Mock price of Compound (COMP) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant COMP_USD_PRICE = 18.6e8;

    /// @notice Mock price of Curve DAO Token (CRV) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant CRV_USD_PRICE = 0.23e8;

    /// @notice Mock price of Ethereum Name Service (ENS) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant ENS_USD_PRICE = 6.11e8;

    /// @notice Mock price of Maker (MKR) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant MKR_USD_PRICE = 1896.21e8;

    /// @notice Mock price of The Sandbox (SAND) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant SAND_USD_PRICE = 0.08e8;

    /// @notice Mock price of Solana (SOL) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant SOL_USD_PRICE = 88.63e8;

    /// @notice Mock price of SushiSwap (SUSHI) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant SUSHI_USD_PRICE = 0.22e8;

    /// @notice Mock price of Bittensor (TAO) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant TAO_USD_PRICE = 271.61e8;

    /// @notice Mock price of Uniswap (UNI) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant UNI_USD_PRICE = 3.77e8;

    /// @notice Mock price of yearn.finance (YFI) in USD — sourced 2026-03-16, 8 decimals
    int256 public constant YFI_USD_PRICE = 2561.07e8;

    /// @notice Mock price of Wrapped AVAX (WAVAX) in USD — sourced 2026-04-30, 8 decimals
    int256 public constant WAVAX_USD_PRICE = 20e8;

    /// @notice Mock price of Basic Attention Token (BAT) in USD — sourced 2026-04-30, 8 decimals
    int256 public constant BAT_USD_PRICE = 0.17e8;

    /// @notice Mock price of Immutable X (IMX) in USD — sourced 2026-04-30, 8 decimals
    int256 public constant IMX_USD_PRICE = 0.75e8;

    /// @notice Mock price of Kyber Network Crystal (KNC) in USD — sourced 2026-04-30, 8 decimals
    int256 public constant KNC_USD_PRICE = 0.55e8;

    /// @notice Mock price of Radiant Capital (RDNT) in USD — sourced 2026-04-30, 8 decimals
    int256 public constant RDNT_USD_PRICE = 0.04e8;

    /// @notice Mock price of Tether (USDT) in USD from the MockV3Aggregator
    int256 public constant USDT_USD_PRICE = 1e8;

    /// @notice Heartbeat for volatile-asset Chainlink feeds that update every hour
    uint256 public constant HEARTBEAT_1H = 1 hours;

    /// @notice Heartbeat for USDC/USD — Chainlink publishes updates approximately every 23 hours
    uint256 public constant HEARTBEAT_23H = 23 hours;

    /// @notice Heartbeat for low-volatility feeds that publish updates approximately every 24 hours
    uint256 public constant HEARTBEAT_24H = 24 hours;

    /**
     * @notice Canonical ERC-4337 EntryPoint v0.9 address
     * @dev Deployed at the same address on Ethereum mainnet, Sepolia, and most EVM-compatible chains.
     *      Source: https://github.com/eth-infinitism/account-abstraction/releases
     */
    address public constant ENTRYPOINT_V07 = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;

    /**
     * @dev Cached Anvil network config. Populated on first call to getOrCreateAnvilConfig.
     *      Both fields must be non-zero for the cache to be considered valid.
     */
    NetworkConfig private s_localNetworkConfig;

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
     *      Falls back to mainnet config for unrecognised chain IDs rather than reverting,
     *      allowing deployment to any EVM chain that shares the canonical EntryPoint address.
     * @param chainId The EVM chain ID to resolve configuration for
     * @return config NetworkConfig for the specified chain
     */
    function getConfigByChainId(uint256 chainId) internal returns (NetworkConfig memory) {
        if (chainId == ETH_SEPOLIA_CHAIN_ID) {
            return getEthSepoliaConfig();
        } else if (chainId == LOCAL_CHAIN_ID) {
            return getOrCreateAnvilConfig();
        } else {
            return getMainnetConfig();
        }
    }

    /**
     * @notice Returns the Ethereum Sepolia testnet configuration
     * @dev Uses the canonical EntryPoint v0.9 address and the burner wallet as deployer.
     *      Ensure SEPOLIA_ACCOUNT is funded with Sepolia ETH before broadcasting.
     * @return config NetworkConfig for Ethereum Sepolia
     */
    function getEthSepoliaConfig() internal pure returns (NetworkConfig memory) {
        return NetworkConfig({
            entryPoint: ENTRYPOINT_V07,
            account: SEPOLIA_ACCOUNT,
            // Stablecoins
            usdc: 0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238,
            weth: 0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14,
            uniswapRouter: address(0), // No official UniswapV2 deployment on Sepolia
            dai: 0x68194a729C2450ad26072b3D33ADaCbcef39D574,
            usdt: 0x7169D38820dfd117C3FA1f22a697dBA58d90BA06,
            // ERC-20 tokens — address(0) where no official Sepolia deployment exists
            aave: 0x88541670E55cC00bEEFD87eB59EDd1b7C511AC9a, // Aave V3 Sepolia testnet token
            link: 0x779877A7B0D9E8603169DdbD7836e478b4624789, // Chainlink official Sepolia faucet token
            oneinch: address(0), // No official Sepolia deployment
            ape: address(0), // No official Sepolia deployment
            arb: address(0), // No official Sepolia deployment
            bnb: address(0), // No official Sepolia deployment
            wbtc: 0x29f2D40B0605204364af54EC677bD022dA425d03, // Aave V3 Sepolia testnet token
            comp: address(0), // No official Sepolia deployment
            crv: address(0), // No official Sepolia deployment
            ens: address(0), // No official Sepolia deployment
            mkr: address(0), // No official Sepolia deployment
            sand: address(0), // No official Sepolia deployment
            sushi: address(0), // No official Sepolia deployment
            wtao: address(0), // No official Sepolia deployment
            uni: 0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984, // Uniswap official Sepolia deployment
            yfi: address(0), // No official Sepolia deployment
            wavax: address(0), // No official Sepolia deployment
            bat: address(0), // No official Sepolia deployment
            imx: address(0), // No official Sepolia deployment
            knc: address(0), // No official Sepolia deployment
            rdnt: address(0), // No official Sepolia deployment
            // Chainlink price feeds — only ETH, USDC, DAI, LINK, BTC have feeds on Sepolia
            ethUsdPriceFeed: 0x694AA1769357215DE4FAC081bf1f309aDC325306,
            usdcUsdPriceFeed: 0xA2F78ab2355fe2f984D808B5CeE7FD0A93D5270E,
            daiUsdPriceFeed: 0x14866185B1962B63C3Ea9E03Bc1da838bab34C19,
            usdtUsdPriceFeed: address(0), // No USDT/USD feed on Sepolia
            aaveUsdPriceFeed: address(0),
            linkUsdPriceFeed: 0xc59E3633BAAC79493d908e63626716e204A45EdF,
            oneinchUsdPriceFeed: address(0),
            apeUsdPriceFeed: address(0),
            arbUsdPriceFeed: address(0),
            bnbUsdPriceFeed: address(0),
            btcUsdPriceFeed: 0x1b44F3514812d835EB1BDB0acB33d3fA3351Ee43,
            compUsdPriceFeed: address(0),
            crvUsdPriceFeed: address(0),
            ensUsdPriceFeed: address(0),
            mkrUsdPriceFeed: address(0),
            sandUsdPriceFeed: address(0),
            sushiUsdPriceFeed: address(0),
            wtaoUsdPriceFeed: address(0),
            uniUsdPriceFeed: address(0),
            yfiUsdPriceFeed: address(0),
            wavaxUsdPriceFeed: address(0),
            batUsdPriceFeed: address(0),
            imxUsdPriceFeed: address(0),
            kncUsdPriceFeed: address(0),
            rdntUsdPriceFeed: address(0),
            // Heartbeats — use 1 hour for all Sepolia feeds (conservative default for testnet)
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
            mkrHeartbeat: HEARTBEAT_1H,
            sandHeartbeat: HEARTBEAT_1H,
            sushiHeartbeat: HEARTBEAT_1H,
            wtaoHeartbeat: HEARTBEAT_1H,
            uniHeartbeat: HEARTBEAT_1H,
            yfiHeartbeat: HEARTBEAT_1H,
            wavaxHeartbeat: HEARTBEAT_1H,
            batHeartbeat: HEARTBEAT_1H,
            imxHeartbeat: HEARTBEAT_1H,
            kncHeartbeat: HEARTBEAT_1H,
            rdntHeartbeat: HEARTBEAT_1H
        });
    }

    /**
     * @notice Returns the mainnet (and generic EVM chain) configuration
     * @dev Assumes the canonical EntryPoint v0.9 is deployed at ENTRYPOINT_V07.
     *      Used as the fallback for any unrecognised chain ID.
     *      Ensure SEPOLIA_ACCOUNT is funded before broadcasting on any live network.
     * @return config NetworkConfig for Ethereum mainnet and compatible chains
     */
    function getMainnetConfig() internal pure returns (NetworkConfig memory) {
        return NetworkConfig({
            entryPoint: ENTRYPOINT_V07,
            account: ANVIL_BURNER_WALLET,//swap for the deployer account on mainnet and ensure it's funded before broadcasting
            uniswapRouter: 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D,
            // Stablecoins
            usdc: 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
            dai: 0x6B175474E89094C44Da98b954EedeAC495271d0F,
            usdt: 0xdAC17F958D2ee523a2206206994597C13D831ec7,
            // ERC-20 tokens
            weth: 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2,
            aave: 0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9,
            link: 0x514910771AF9Ca656af840dff83E8264EcF986CA,
            oneinch: 0x111111111117dC0aa78b770fA6A738034120C302,
            ape: 0x4d224452801ACEd8B2F0aebE155379bb5D594381,
            arb: 0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1,
            bnb: 0xB8c77482e45F1F44dE1745F52C74426C631bDD52,
            wbtc: 0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599,
            comp: 0xc00e94Cb662C3520282E6f5717214004A7f26888,
            crv: 0xD533a949740bb3306d119CC777fa900bA034cd52,
            ens: 0xC18360217D8F7Ab5e7c516566761Ea12Ce7F9D72,
            mkr: 0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2,
            sand: 0x3845badAde8e6dFF049820680d1F14bD3903a5d0,
            sushi: 0x6B3595068778DD592e39A122f4f5a5cF09C90fE2,
            wtao: 0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44,
            uni: 0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984,
            yfi: 0x0bc529c00C6401aEF6D220BE8C6Ea1667F6Ad93e,
            wavax: 0x85f138bfEE4ef8e540890CFb48F620571d67Eda3,
            bat: 0x0D8775F648430679A709E98d2b0Cb6250d2887EF,
            imx: 0xF57e7e7C23978C3cAEC3C3548E3D615c346e79fF,
            knc: 0xdeFA4e8a7bcBA345F687a2f1456F5Edd9CE97202,
            rdnt: 0x137dDB47Ee24EaA998a535Ab00378d6BFa84F893,
            // Chainlink price feeds
            ethUsdPriceFeed: 0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419,
            usdcUsdPriceFeed: 0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6,
            daiUsdPriceFeed: 0xAed0c38402a5d19df6E4c03F4E2DceD6e29c1ee9,
            usdtUsdPriceFeed: 0x3E7d1eAB13ad0104d2750B8863b489D65364e32D,
            aaveUsdPriceFeed: 0x547a514d5e3769680Ce22B2361c10Ea13619e8a9,
            linkUsdPriceFeed: 0x2c1d072e956AFFC0D435Cb7AC38EF18d24d9127c,
            oneinchUsdPriceFeed: 0xc929ad75B72593967DE83E7F7Cda0493458261D9,
            apeUsdPriceFeed: 0xD10aBbC76679a20055E167BB80A24ac851b37056,
            arbUsdPriceFeed: 0x31697852a68433DbCc2Ff612c516d69E3D9bd08F,
            bnbUsdPriceFeed: 0x14e613AC84a31f709eadbdF89C6CC390fDc9540A,
            btcUsdPriceFeed: 0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c,
            compUsdPriceFeed: 0xdbd020CAeF83eFd542f4De03e3cF0C28A4428bd5,
            crvUsdPriceFeed: 0xCd627aA160A6fA45Eb793D19Ef54f5062F20f33f,
            ensUsdPriceFeed: 0x5C00128d4d1c2F4f652C267d7bcdD7aC99C16E16,
            mkrUsdPriceFeed: 0xec1D1B3b0443256cc3860e24a46F108e699484Aa,
            sandUsdPriceFeed: 0x35E3f7E558C04cE7eEE1629258EcbbA03B36Ec56,
            sushiUsdPriceFeed: 0xCc70F09A6CC17553b2E31954cD36E4A2d89501f7,
            wtaoUsdPriceFeed: 0x1c88503c9A52aE6aaE1f9bb99b3b7e9b8Ab35459,
            uniUsdPriceFeed: 0x553303d460EE0afB37EdFf9bE42922D8FF63220e,
            yfiUsdPriceFeed: 0xA027702dbb89fbd58938e4324ac03B58d812b0E1,
            wavaxUsdPriceFeed: 0xFF3EEb22B5E3dE6e705b44749C2559d704923FD7,
            batUsdPriceFeed: 0x0d16d4528239e9ee52fa531af613AcdB23D88c94,
            imxUsdPriceFeed: 0xBAEbEFc1D023c0feCcc047Bff42E75F15Ff213E6,
            kncUsdPriceFeed: 0xf8fF43E991A81e6eC886a3D281A2C6cC19aE70Fc,
            rdntUsdPriceFeed: 0x393CC05baD439c9B36489384F11487d9C8410471,
            // Heartbeats sourced from Chainlink reference data (feeds-mainnet.json)
            ethHeartbeat: HEARTBEAT_1H,
            usdcHeartbeat: HEARTBEAT_23H,
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
            mkrHeartbeat: HEARTBEAT_1H,
            sandHeartbeat: HEARTBEAT_24H,
            sushiHeartbeat: HEARTBEAT_24H,
            wtaoHeartbeat: HEARTBEAT_24H,
            uniHeartbeat: HEARTBEAT_1H,
            yfiHeartbeat: HEARTBEAT_24H,
            wavaxHeartbeat: HEARTBEAT_24H,
            batHeartbeat: HEARTBEAT_24H,
            imxHeartbeat: HEARTBEAT_24H,
            kncHeartbeat: HEARTBEAT_24H,
            rdntHeartbeat: HEARTBEAT_24H
        });
    }

    /**
     * @notice Returns the local Anvil configuration, deploying a fresh EntryPoint if needed
     * @dev Lazily deploys a new EntryPoint contract on the first call and caches the result
     *      in s_localNetworkConfig. Subsequent calls return the cached config without
     *      redeploying. Cache validity is determined by both fields being non-zero.
     *
     *      The EntryPoint is deployed without vm.startBroadcast since this is an internal
     *      setup step, not a user-facing deployment.
     * @return config NetworkConfig for the local Anvil node with a freshly deployed EntryPoint
     */
    function getOrCreateAnvilConfig() internal returns (NetworkConfig memory) {
        // Return cached config if EntryPoint has already been deployed this session
        if (s_localNetworkConfig.entryPoint != address(0) && s_localNetworkConfig.account != address(0)) {
            return s_localNetworkConfig;
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
            ERC20Mock mkr = new ERC20Mock("Maker", "MKR", 18);
            ERC20Mock sand = new ERC20Mock("The Sandbox", "SAND", 18);
            ERC20Mock sushi = new ERC20Mock("SushiSwap", "SUSHI", 18);
            ERC20Mock wtao = new ERC20Mock("Wrapped TAO", "wTAO", 18);
            ERC20Mock uni = new ERC20Mock("Uniswap", "UNI", 18);
            ERC20Mock yfi = new ERC20Mock("yearn.finance", "YFI", 18);
            ERC20Mock wavax = new ERC20Mock("Wrapped AVAX", "WAVAX", 18);
            ERC20Mock bat = new ERC20Mock("Basic Attention Token", "BAT", 18);
            ERC20Mock imx = new ERC20Mock("Immutable X", "IMX", 18);
            ERC20Mock knc = new ERC20Mock("Kyber Network Crystal", "KNC", 18);
            ERC20Mock rdnt = new ERC20Mock("Radiant Capital", "RDNT", 18);

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
            MockV3Aggregator mkrUsdPriceFeed = new MockV3Aggregator(DECIMALS, MKR_USD_PRICE);
            MockV3Aggregator sandUsdPriceFeed = new MockV3Aggregator(DECIMALS, SAND_USD_PRICE);
            MockV3Aggregator sushiUsdPriceFeed = new MockV3Aggregator(DECIMALS, SUSHI_USD_PRICE);
            MockV3Aggregator wtaoUsdPriceFeed = new MockV3Aggregator(DECIMALS, TAO_USD_PRICE);
            MockV3Aggregator uniUsdPriceFeed = new MockV3Aggregator(DECIMALS, UNI_USD_PRICE);
            MockV3Aggregator yfiUsdPriceFeed = new MockV3Aggregator(DECIMALS, YFI_USD_PRICE);
            MockV3Aggregator wavaxUsdPriceFeed = new MockV3Aggregator(DECIMALS, WAVAX_USD_PRICE);
            MockV3Aggregator batUsdPriceFeed = new MockV3Aggregator(DECIMALS, BAT_USD_PRICE);
            MockV3Aggregator imxUsdPriceFeed = new MockV3Aggregator(DECIMALS, IMX_USD_PRICE);
            MockV3Aggregator kncUsdPriceFeed = new MockV3Aggregator(DECIMALS, KNC_USD_PRICE);
            MockV3Aggregator rdntUsdPriceFeed = new MockV3Aggregator(DECIMALS, RDNT_USD_PRICE);

            vm.stopBroadcast();

            s_localNetworkConfig = NetworkConfig({
                entryPoint: address(entryPoint),
                account: ANVIL_BURNER_WALLET,
                uniswapRouter: address(0), // Uniswap not deployed on Anvil by default
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
                bnb: address(bnb),
                wbtc: address(wbtc),
                comp: address(comp),
                crv: address(crv),
                ens: address(ens),
                mkr: address(mkr),
                sand: address(sand),
                sushi: address(sushi),
                wtao: address(wtao),
                uni: address(uni),
                yfi: address(yfi),
                wavax: address(wavax),
                bat: address(bat),
                imx: address(imx),
                knc: address(knc),
                rdnt: address(rdnt),
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
                mkrUsdPriceFeed: address(mkrUsdPriceFeed),
                sandUsdPriceFeed: address(sandUsdPriceFeed),
                sushiUsdPriceFeed: address(sushiUsdPriceFeed),
                wtaoUsdPriceFeed: address(wtaoUsdPriceFeed),
                uniUsdPriceFeed: address(uniUsdPriceFeed),
                yfiUsdPriceFeed: address(yfiUsdPriceFeed),
                wavaxUsdPriceFeed: address(wavaxUsdPriceFeed),
                batUsdPriceFeed: address(batUsdPriceFeed),
                imxUsdPriceFeed: address(imxUsdPriceFeed),
                kncUsdPriceFeed: address(kncUsdPriceFeed),
                rdntUsdPriceFeed: address(rdntUsdPriceFeed),
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
                mkrHeartbeat: HEARTBEAT_1H,
                sandHeartbeat: HEARTBEAT_1H,
                sushiHeartbeat: HEARTBEAT_1H,
                wtaoHeartbeat: HEARTBEAT_1H,
                uniHeartbeat: HEARTBEAT_1H,
                yfiHeartbeat: HEARTBEAT_1H,
                wavaxHeartbeat: HEARTBEAT_1H,
                batHeartbeat: HEARTBEAT_1H,
                imxHeartbeat: HEARTBEAT_1H,
                kncHeartbeat: HEARTBEAT_1H,
                rdntHeartbeat: HEARTBEAT_1H
            });
            return s_localNetworkConfig;
        }
    }
}
