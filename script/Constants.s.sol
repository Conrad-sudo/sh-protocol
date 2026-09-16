// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*//////////////////////////////////////////////////////////////
                        CHAIN IDs
//////////////////////////////////////////////////////////////*/
uint256 constant MAINNET_CHAIN_ID = 1;
uint256 constant SEPOLIA_CHAIN_ID = 11155111;
uint256 constant BSC_CHAIN_ID = 56;
uint256 constant ARB_CHAIN_ID=42161;
uint256 constant LOCAL_CHAIN_ID = 31337;

/*//////////////////////////////////////////////////////////////
                        CROSS-CHAIN
//////////////////////////////////////////////////////////////*/

// ─── ERC-4337 ─────────────────────────────────────────────────────────────────
// Canonical EntryPoint v0.7 — deployed at the same address on mainnet, Sepolia, and most EVM chains.
address constant ENTRYPOINT_V07 = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;

// Canonical ERC8004 registries — deployed at the same address on mainnets and testnets.

// ─── Mainnet Agent Registries ─────────────────────────────────────────────────
address constant MNT_REPUTATION_REGISTRY = 0x8004BAa17C55a88189AE136b182e5fdA19dE9b63;
address constant MNT_IDENTITY_REGISTRY = 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432;

// ─── Testnet Agent Registries ─────────────────────────────────────────────────
address constant TESTNET_REPUTATION_REGISTRY = 0x8004B663056A597Dffe9eCcC1965A193B7388713;
address constant TESTNET_IDENTITY_REGISTRY = 0x8004A818BFB912233c491871b3d84c89A494BD9e;

/*//////////////////////////////////////////////////////////////
                        ROUTERS
//////////////////////////////////////////////////////////////*/

// ─── Mainnet Uniswap ──────────────────────────────────────────────────────────────────
address constant MNT_UNISWAP_V2_ROUTER_02 = 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D;
address constant MNT_UNISWAP_V2_FACTORY = 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f;

// ─── Sepolia Uniswap ──────────────────────────────────────────────────────────────────
address constant SPO_UNISWAP_V2_ROUTER_02 = 0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3;
address constant SPO_UNISWAP_V2_FACTORY = 0xF62c03E08ada871A0bEb309762E260a7a6a880E6;

// ─── PancakeSwap ──────────────────────────────────────────────────────────────
address constant PANCAKE_V2_ROUTER_02 = 0x10ED43C718714eb63d5aA57B78B54704E256024E;
address constant PANCAKE_V2_FACTORY = 0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73;

// ─── Arbitrum Uniswap ──────────────────────────────────────────────────────────────────
address constant ARB_UNISWAP_V2_ROUTER=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24;
address constant ARB_UNISWAP_V2_FACTORY=0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9;


// ─── Base Uniswap ──────────────────────────────────────────────────────────────────
address constant BASE_UNISWAP_V2_ROUTER=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24;
address constant BASE_UNISWAP_V2_FACTORY=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6;


/*//////////////////////////////////////////////////////////////
                      ETHEREUM MAINNET
//////////////////////////////////////////////////////////////*/
// ─── Mainnet Tokens ───────────────────────────────────────────────────────────
address constant MNT_USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
address constant MNT_DAI = 0x6B175474E89094C44Da98b954EedeAC495271d0F;
address constant MNT_USDT = 0xdAC17F958D2ee523a2206206994597C13D831ec7;
address constant MNT_WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
address constant MNT_AAVE = 0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9;
address constant MNT_LINK = 0x514910771AF9Ca656af840dff83E8264EcF986CA;
address constant MNT_ONEINCH = 0x111111111117dC0aa78b770fA6A738034120C302;
address constant MNT_APE = 0x4d224452801ACEd8B2F0aebE155379bb5D594381;
address constant MNT_ARB = 0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1;
address constant MNT_BNB = 0xB8c77482e45F1F44dE1745F52C74426C631bDD52;
address constant MNT_WBTC = 0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599;
address constant MNT_COMP = 0xc00e94Cb662C3520282E6f5717214004A7f26888;
address constant MNT_CRV = 0xD533a949740bb3306d119CC777fa900bA034cd52;
address constant MNT_ENS = 0xC18360217D8F7Ab5e7c516566761Ea12Ce7F9D72;
address constant MNT_SAND = 0x3845badAde8e6dFF049820680d1F14bD3903a5d0;
address constant MNT_SUSHI = 0x6B3595068778DD592e39A122f4f5a5cF09C90fE2;
address constant MNT_WTAO = 0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44;
address constant MNT_UNI = 0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984;
address constant MNT_YFI = 0x0bc529c00C6401aEF6D220BE8C6Ea1667F6Ad93e;
address constant MNT_WAVAX = 0x85f138bfEE4ef8e540890CFb48F620571d67Eda3;
address constant MNT_IMX = 0xF57e7e7C23978C3cAEC3C3548E3D615c346e79fF;
address constant MNT_KNC = 0xdeFA4e8a7bcBA345F687a2f1456F5Edd9CE97202;

// ─── Mainnet Price Feeds ──────────────────────────────────────────────────────
address constant MNT_ETH_USD_PRICE_FEED = 0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419;
address constant MNT_USDC_USD_PRICE_FEED = 0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6;
address constant MNT_DAI_USD_PRICE_FEED = 0xAed0c38402a5d19df6E4c03F4E2DceD6e29c1ee9;
address constant MNT_USDT_USD_PRICE_FEED = 0x3E7d1eAB13ad0104d2750B8863b489D65364e32D;
address constant MNT_AAVE_USD_PRICE_FEED = 0x547a514d5e3769680Ce22B2361c10Ea13619e8a9;
address constant MNT_LINK_USD_PRICE_FEED = 0x2c1d072e956AFFC0D435Cb7AC38EF18d24d9127c;
address constant MNT_ONEINCH_USD_PRICE_FEED = 0xc929ad75B72593967DE83E7F7Cda0493458261D9;
address constant MNT_APE_USD_PRICE_FEED = 0xD10aBbC76679a20055E167BB80A24ac851b37056;
address constant MNT_ARB_USD_PRICE_FEED = 0x31697852a68433DbCc2Ff612c516d69E3D9bd08F;
address constant MNT_BNB_USD_PRICE_FEED = 0x14e613AC84a31f709eadbdF89C6CC390fDc9540A;
address constant MNT_BTC_USD_PRICE_FEED = 0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c;
address constant MNT_COMP_USD_PRICE_FEED = 0xdbd020CAeF83eFd542f4De03e3cF0C28A4428bd5;
address constant MNT_CRV_USD_PRICE_FEED = 0xCd627aA160A6fA45Eb793D19Ef54f5062F20f33f;
address constant MNT_ENS_USD_PRICE_FEED = 0x5C00128d4d1c2F4f652C267d7bcdD7aC99C16E16;
address constant MNT_SAND_USD_PRICE_FEED = 0x35E3f7E558C04cE7eEE1629258EcbbA03B36Ec56;
address constant MNT_SUSHI_USD_PRICE_FEED = 0xCc70F09A6CC17553b2E31954cD36E4A2d89501f7;
address constant MNT_WTAO_USD_PRICE_FEED = 0x1c88503c9A52aE6aaE1f9bb99b3b7e9b8Ab35459;
address constant MNT_UNI_USD_PRICE_FEED = 0x553303d460EE0afB37EdFf9bE42922D8FF63220e;
address constant MNT_YFI_USD_PRICE_FEED = 0xA027702dbb89fbd58938e4324ac03B58d812b0E1;
address constant MNT_WAVAX_USD_PRICE_FEED = 0xFF3EEb22B5E3dE6e705b44749C2559d704923FD7;
address constant MNT_IMX_USD_PRICE_FEED = 0xBAEbEFc1D023c0feCcc047Bff42E75F15Ff213E6;
address constant MNT_KNC_USD_PRICE_FEED = 0xf8fF43E991A81e6eC886a3D281A2C6cC19aE70Fc;

/*//////////////////////////////////////////////////////////////
                      SEPOLIA TESTNET
//////////////////////////////////////////////////////////////*/

// ─── Sepolia Tokens ───────────────────────────────────────────────────────────
address constant SPO_USDC = 0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238;
address constant SPO_WETH = 0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14;
address constant SPO_DAI = 0x68194a729C2450ad26072b3D33ADaCbcef39D574;
address constant SPO_LINK = 0x779877A7B0D9E8603169DdbD7836e478b4624789; // Chainlink faucet token
address constant SPO_WBTC = 0x29f2D40B0605204364af54EC677bD022dA425d03; // Aave V3 testnet token

// ─── Sepolia Price Feeds ──────────────────────────────────────────────────────
address constant SPO_ETH_USD_PRICE_FEED = 0x694AA1769357215DE4FAC081bf1f309aDC325306;
address constant SPO_USDC_USD_PRICE_FEED = 0xA2F78ab2355fe2f984D808B5CeE7FD0A93D5270E;
address constant SPO_DAI_USD_PRICE_FEED = 0x14866185B1962B63C3Ea9E03Bc1da838bab34C19;
address constant SPO_LINK_USD_PRICE_FEED = 0xc59E3633BAAC79493d908e63626716e204A45EdF;
address constant SPO_BTC_USD_PRICE_FEED = 0x1b44F3514812d835EB1BDB0acB33d3fA3351Ee43;

/*//////////////////////////////////////////////////////////////
                  BSC (BINANCE SMART CHAIN)
//////////////////////////////////////////////////////////////*/

// ─── BSC Tokens ───────────────────────────────────────────────────────────────
address constant BSC_WETH = 0x2170Ed0880ac9A755fd29B2688956BD959F933F8;
address constant BSC_WBNB = 0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c;
address constant BSC_USDT = 0x55d398326f99059fF775485246999027B3197955;
address constant BSC_USDC = 0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d;
address constant BSC_DAI = 0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3;
address constant BSC_AAVE = 0xfb6115445Bff7b52FeB98650C87f44907E58f802;
address constant BSC_LINK = 0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD;
address constant BSC_ONEINCH = 0x111111111117dC0aa78b770fA6A738034120C302;
address constant BSC_WBTC = 0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c;
address constant BSC_COMP = 0x52CE071Bd9b1C4B00A0b92D298c512478CaD67e8;
address constant BSC_CRV = 0x9996D0276612d23b35f90C51EE935520B3d7355B;
address constant BSC_SUSHI = 0x947950BcC74888a40Ffa2593C5798F11Fc9124C4;
address constant BSC_UNI = 0xBf5140A22578168FD562DCcF235E5D43A02ce9B1;
address constant BSC_YFI = 0x88f1A5ae2A3BF98AEAF342D26B30a79438c9142e;
address constant BSC_WAVAX = 0x1CE0c2827e2eF14D5C4f29a091d735A204794041;
address constant BSC_KNC = 0xfe56d5892BDffC7BF58f2E84BE1b2C32D21C308b;
address constant BSC_CAKE = 0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82;

// ─── BSC Price Feeds (Chainlink) ──────────────────────────────────────────────
address constant BSC_ETH_USD_PRICE_FEED = 0x9ef1B8c0E4F7dc8bF5719Ea496883DC6401d5b2e;
address constant BSC_BNB_USD_PRICE_FEED = 0x0567F2323251f0Aab15c8dFb1967E4e8A7D42aeE;
address constant BSC_USDT_USD_PRICE_FEED = 0xB97Ad0E74fa7d920791E90258A6E2085088b4320;
address constant BSC_USDC_USD_PRICE_FEED = 0x51597f405303C4377E36123cBc172b13269EA163;
address constant BSC_DAI_USD_PRICE_FEED = 0x132d3C0B1D2cEa0BC552588063bdBb210FDeecfA;
address constant BSC_AAVE_USD_PRICE_FEED = 0xA8357BF572460fC40f4B0aCacbB2a6A61c89f475;
address constant BSC_LINK_USD_PRICE_FEED = 0xca236E327F629f9Fc2c30A4E95775EbF0B89fac8;
address constant BSC_ONEINCH_USD_PRICE_FEED = 0x9a177Bb9f5b6083E962f9e62bD21d4b5660Aeb03;
address constant BSC_BTC_USD_PRICE_FEED = 0x264990fbd0A4796A3E3d8E37C4d5F87a3aCa5Ebf;
address constant BSC_COMP_USD_PRICE_FEED = 0x0Db8945f9aEf5651fa5bd52314C5aAe78DfDe540;
address constant BSC_CRV_USD_PRICE_FEED = 0x2e1C3b6Fcae47b20Dd343D9354F7B1140a1E6B27;
address constant BSC_SUSHI_USD_PRICE_FEED = 0xa679C72a97B654CFfF58aB704de3BA15Cde89B07;
address constant BSC_UNI_USD_PRICE_FEED = 0xb57f259E7C24e56a1dA00F66b55A5640d9f9E7e4;
address constant BSC_YFI_USD_PRICE_FEED = 0xD7eAa5Bf3013A96e3d515c055Dbd98DbdC8c620D;
address constant BSC_AVAX_USD_PRICE_FEED = 0x5974855ce31EE8E1fff2e76591CbF83D7110F151;
address constant BSC_KNC_USD_PRICE_FEED = 0xF2f8273F6b9Fc22C90891DC802cAf60eeF805cDF;
address constant BSC_CAKE_USD_PRICE_FEED = 0xB6064eD41d4f67e353768aA239cA86f4F73665a1; // heartbeat: 1 min



/*//////////////////////////////////////////////////////////////
                            ARBITRUM
//////////////////////////////////////////////////////////////*/

// Arbitrum One (chain id 42161). Every address below was read back on-chain: tokens answered
// symbol()/name()/decimals() as expected and each feed answered description() with the pair named
// in its constant. Tokens with no Chainlink USD feed on Arbitrum (ENS, SAND, IMX, KNC) and feeds
// with no credible token deployment (BNB, AVAX, TAO) are deliberately absent — see getArbConfig.

// ─── Arbitrum Tokens ──────────────────────────────────────────────────────────
address constant ARB_USDC = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831; // native Circle USDC, not USDC.e
address constant ARB_DAI = 0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1;
address constant ARB_USDT = 0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9; // Tether's Arbitrum USDT, rebranded USD₮0
address constant ARB_WETH = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;
address constant ARB_AAVE = 0xba5DdD1f9d7F570dc94a51479a000E3BCE967196;
address constant ARB_LINK = 0xf97f4df75117a78c1A5a0DBb814Af92458539FB4;
address constant ARB_ONEINCH = 0x6314C31A7a1652cE482cffe247E9CB7c3f4BB9aF;
address constant ARB_APE = 0x7f9FBf9bDd3F4105C478b996B648FE6e828a1e98; // ApeCoin's own Arbitrum deployment
address constant ARB_ARB = 0x912CE59144191C1204E64559FE8253a0e49E6548;
address constant ARB_WBTC = 0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f;
address constant ARB_COMP = 0x354A6dA3fcde098F8389cad84b0182725c6C91dE;
address constant ARB_CRV = 0x11cDb42B0EB46D95f990BeDD4695A6e3fA034978;
address constant ARB_SUSHI = 0xd4d42F0b6DEF4CE0383636770eF773390d85c61A;
address constant ARB_UNI = 0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0;
address constant ARB_YFI = 0x82e3A8F066a6989666b031d916c43672085b1582;
address constant ARB_CAKE = 0x1b896893dfc86bb67Cf57767298b9073D2c1bA2c; // PancakeSwap's own Arbitrum deployment

// ─── Arbitrum Price Feeds (Chainlink) ─────────────────────────────────────────
// Canonical Data Feed proxies only — the -svr (Smart Value Recapture) and -cre-backup variants
// Chainlink also publishes on Arbitrum are intentionally not used here.
address constant ARB_ETH_USD_PRICE_FEED = 0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612;
address constant ARB_USDC_USD_PRICE_FEED = 0x50834F3163758fcC1Df9973b6e91f0F0F0434aD3;
address constant ARB_DAI_USD_PRICE_FEED = 0xc5C8E77B397E531B8EC06BFb0048328B30E9eCfB;
address constant ARB_USDT_USD_PRICE_FEED = 0x3f3f5dF88dC9F13eac63DF89EC16ef6e7E25DdE7;
address constant ARB_AAVE_USD_PRICE_FEED = 0xaD1d5344AaDE45F43E596773Bcc4c423EAbdD034;
address constant ARB_LINK_USD_PRICE_FEED = 0x86E53CF1B870786351Da77A57575e79CB55812CB;
address constant ARB_ONEINCH_USD_PRICE_FEED = 0x4bC735Ef24bf286983024CAd5D03f0738865Aaef;
address constant ARB_APE_USD_PRICE_FEED = 0x221912ce795669f628c51c69b7d0873eDA9C03bB;
address constant ARB_ARB_USD_PRICE_FEED = 0xb2A824043730FE05F3DA2efaFa1CBbe83fa548D6;
address constant ARB_BTC_USD_PRICE_FEED = 0x6ce185860a4963106506C203335A2910413708e9; // used for WBTC
address constant ARB_COMP_USD_PRICE_FEED = 0xe7C53FFd03Eb6ceF7d208bC4C13446c76d1E5884;
address constant ARB_CRV_USD_PRICE_FEED = 0xaebDA2c976cfd1eE1977Eac079B4382acb849325;
address constant ARB_SUSHI_USD_PRICE_FEED = 0xb2A8BA74cbca38508BA1632761b56C897060147C;
address constant ARB_UNI_USD_PRICE_FEED = 0x9C917083fDb403ab5ADbEC26Ee294f6EcAda2720;
address constant ARB_YFI_USD_PRICE_FEED = 0x745Ab5b69E01E2BE1104Ca84937Bb71f96f5fB21;
address constant ARB_CAKE_USD_PRICE_FEED = 0x256654437f1ADA8057684b18d742eFD14034C400;

// ─── Arbitrum L2 Sequencer Uptime Feed ────────────────────────────────────────
// Reports 0 while the sequencer is up and 1 while it is down, with `startedAt` marking when that
// status began. SHOracle refuses to price anything while it reads down, or within
// SHOracle.SEQUENCER_GRACE_PERIOD of it coming back — price feeds are published through the
// sequencer, so an outage freezes them all without making any of them look stale.
address constant ARB_SEQUENCER_UPTIME_FEED = 0xFdB631F5EE196F0ed6FAa767959853A9F217697D;



/*//////////////////////////////////////////////////////////////
                  ANVIL FIXED PRICES
//////////////////////////////////////////////////////////////*/

uint8 constant DECIMALS = 8;

int256 constant ETH_USD_PRICE = 1000e8;

int256 constant USDC_USD_PRICE = 0.998e8;

int256 constant DAI_USD_PRICE = 1.2e8;

int256 constant AAVE_USD_PRICE = 119e8;

int256 constant LINK_USD_PRICE = 9.21e8;

int256 constant ONEINCH_USD_PRICE = 0.1e8;

int256 constant APE_USD_PRICE = 0.1e8;

int256 constant ARB_USD_PRICE = 0.1e8;

int256 constant BNB_USD_PRICE = 674.03e8;

int256 constant BTC_USD_PRICE = 71498.24e8;

int256 constant COMP_USD_PRICE = 18.6e8;

int256 constant CRV_USD_PRICE = 0.23e8;

int256 constant ENS_USD_PRICE = 6.11e8;

int256 constant SAND_USD_PRICE = 0.08e8;

int256 constant SOL_USD_PRICE = 88.63e8;

int256 constant SUSHI_USD_PRICE = 0.22e8;

int256 constant TAO_USD_PRICE = 271.61e8;

int256 constant UNI_USD_PRICE = 3.77e8;

int256 constant YFI_USD_PRICE = 2561.07e8;

int256 constant WAVAX_USD_PRICE = 20e8;

int256 constant IMX_USD_PRICE = 0.75e8;

int256 constant KNC_USD_PRICE = 0.55e8;

int256 constant CAKE_USD_PRICE = 2.5e8;

int256 constant USDT_USD_PRICE = 1e8;

uint256 constant HEARTBEAT_1H = 1 hours;

uint256 constant HEARTBEAT_24H = 24 hours;

/// @dev Sepolia's Chainlink nodes update noticeably less often than mainnet's — observed gaps
///      of ~16-17h on the USDC/DAI feeds in practice (ETH/LINK/BTC typically stay under 1h).
///      This is an accepted characteristic of testnet oracles, not a real staleness risk (no
///      real economic value at stake), so Sepolia heartbeats use this wider window instead of
///      HEARTBEAT_24H, which cuts it too close to the observed worst case.
uint256 constant HEARTBEAT_72H = 72 hours;
