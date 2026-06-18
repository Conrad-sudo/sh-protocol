// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AggregatorV3Interface} from "@chainlink/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

/**
 * @title SHOracle
 * @author Conrad Japhet
 * @notice Converts token amounts to USD equivalents using Chainlink price feeds
 * @dev Accounts for depeg scenarios (e.g., USDC at $0.87 during SVB crisis) by querying
 *      real-time prices from Chainlink oracles instead of assuming fixed rates.
 *      All USD calculations use 18 decimals for precision before converting to token decimals.
 *
 *      Supports native ETH (sentinel address(0)) and any registered ERC-20 token.
 *      Token decimals are read on-chain via IERC20Metadata; ETH is hardcoded to 18.
 *      Tokens with no registered feed revert with SHOracle_UnsupportedToken.
 */
contract SHOracle {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts when an unsupported token address is provided
    error SHOracle_UnsupportedToken();

    /// @dev Reverts when a Chainlink price feed has not been updated within its configured heartbeat
    error SHOracle_StalePrice();

    /// @dev Reverts when the tokens and priceFeeds constructor arrays have different lengths
    error SHOracle_ArrayLengthMismatch();

    /*//////////////////////////////////////////////////////////////
                            STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Sentinel value for native ETH (used instead of an actual token address)
    address private constant ETH_TOKEN_ADDRESS = address(0);

    /// @notice Maps each registered token address to its Chainlink USD price feed address
    /// @dev Populated once in the constructor. Unregistered tokens map to address(0).
    mapping(address => address) private sPriceFeed;

    /// @notice Maps each registered price feed address to its expected heartbeat interval in seconds
    /// @dev Chainlink heartbeats vary per feed: volatile assets update hourly, stablecoins every 23–24 hours.
    ///      Using a uniform timeout would either flag stablecoin feeds as stale or mask genuinely stale volatile feeds.
    mapping(address => uint256) private sHeartbeat;

    /// @notice Multiplier to convert Chainlink's 8-decimal prices to 18-decimal precision
    /// @dev Chainlink returns prices with 8 decimals. Multiply by 1e10 to get 18 decimals.
    uint256 private constant ADDITIONAL_FEED_PRECISION = 1e10;

    /*//////////////////////////////////////////////////////////////
                             CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Registers token–feed pairs and initialises the oracle
     * @dev Pairs whose priceFeed is address(0) are silently skipped, allowing callers to
     *      pass the full NetworkConfig arrays even when some feeds are unavailable on the
     *      current network (e.g., Sepolia). Use address(0) as the token address to register
     *      native ETH.
     *
     * @param tokens      Ordered list of token addresses to support. Use address(0) for ETH.
     * @param priceFeeds  Ordered list of Chainlink USD price feed addresses, one per token.
     *                    Pass address(0) for tokens that have no feed on this network.
     * @param heartbeats  Ordered list of heartbeat intervals in seconds, one per feed.
     *                    Matches the Chainlink-published heartbeat for each feed (e.g. 3600 for ETH/USD, 82800 for USDC/USD).
     *                    The value at index i is ignored when priceFeeds[i] is address(0).
     */
    constructor(address[] memory tokens, address[] memory priceFeeds, uint256[] memory heartbeats) {
        if (tokens.length != priceFeeds.length || priceFeeds.length != heartbeats.length) {
            revert SHOracle_ArrayLengthMismatch();
        }
        for (uint256 i = 0; i < tokens.length; i++) {
            if (priceFeeds[i] != address(0)) {
                sPriceFeed[tokens[i]] = priceFeeds[i];
                sHeartbeat[priceFeeds[i]] = heartbeats[i];
            }
        }
    }

    /*//////////////////////////////////////////////////////////////
                        EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Returns the current USD price of a token from its registered Chainlink feed
     * @dev Reverts if the token has no registered feed or the price is stale.
     *
     * @param token     Token address. Use address(0) for native ETH.
     * @return price    The current price with 8 decimals (Chainlink standard).
     * @return decimals The decimal count of the returned price (always 8 for Chainlink USD feeds).
     */
    function getPrice(address token) external view returns (uint256 price, uint8 decimals) {
        address feed = sPriceFeed[token];
        if (feed == address(0)) revert SHOracle_UnsupportedToken();
        price = _stalePriceCheck(feed);
        decimals = AggregatorV3Interface(feed).decimals();
    }

    /**
     * @notice Returns the USD value of a token amount with 18 decimals of precision
     * @dev For ERC-20 tokens, token decimals are read via IERC20Metadata.decimals() so the
     *      oracle works correctly with tokens of any decimal count (e.g., USDC at 6, WBTC at 8).
     *      ETH (address(0)) is hardcoded to 18 decimals since it has no on-chain contract.
     *
     *      Formula: (amount × chainlinkPrice × 1e10) / (10 ** tokenDecimals)
     *      Example — 1000 USDC (6 dec) at $0.99:
     *        chainlinkPrice = 99_000_000  (8 dec)
     *        (1000e6 × 99_000_000 × 1e10) / 1e6 = 990e18  → $990 with 18 decimals
     *
     * @param token  Token address. Use address(0) for native ETH.
     * @param amount Amount of the token in its native base units.
     * @return       USD value with 18 decimals of precision.
     */
    function getUsdValue(address token, uint256 amount) external view returns (uint256) {
        address feed = sPriceFeed[token];
        if (feed == address(0)) revert SHOracle_UnsupportedToken();

        uint256 price = _stalePriceCheck(feed);
        uint8 decimals = token == ETH_TOKEN_ADDRESS ? 18 : IERC20Metadata(token).decimals();

        return (amount * price * ADDITIONAL_FEED_PRECISION) / (10 ** decimals);
    }

    /**
     * @dev Validates Chainlink price feed freshness and returns the current price
     * @param priceFeed Address of the Chainlink price feed to query
     * @return price    The current price with 8 decimals (Chainlink standard)
     *
     * @notice Reverts with SHOracle_StalePrice if the feed has not updated within TIMEOUT.
     *
     * Why this matters: stale price data can lead to incorrect USD conversions. For instance,
     * if ETH crashes from $2500 to $1500 but the feed has not updated in 5 hours, using the
     * stale price would incorrectly value ETH and may allow overspending beyond session limits.
     */
    function _stalePriceCheck(address priceFeed) internal view returns (uint256) {
        (, int256 price,, uint256 updatedAt,) = AggregatorV3Interface(priceFeed).latestRoundData();

        if (block.timestamp - updatedAt > sHeartbeat[priceFeed]) {
            revert SHOracle_StalePrice();
        }

        // forge-lint: disable-next-line(unsafe-typecast)
        return uint256(price);
    }
}
