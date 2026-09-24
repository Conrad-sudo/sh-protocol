// SPDX-License-Identifier: BUSL-1.1
// Copyright (C) 2026 Conrad Japhet
// Use of this software is governed by the Business Source License included in the LICENSE file.
// Change Date: 2029-06-12. Change License: MIT.
pragma solidity ^0.8.24;

import {AggregatorV3Interface} from "./interfaces/AggregatorV3Interface.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

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
 *      Tokens with no registered feed revert with PriceOracle_UnsupportedToken.
 *
 *      Staleness is enforced per feed via a heartbeat set once at construction, not a
 *      caller-supplied age: a Chainlink feed's real staleness ceiling IS its heartbeat (how
 *      often its node network commits to updating it), so a caller-chosen threshold tighter
 *      than that would revert constantly for no reason, and looser is meaningless since the
 *      feed can never be fresher than its own heartbeat regardless. This differs from a pull
 *      oracle like Pyth, where "how recent" is genuinely caller-choosable since anyone can pay
 *      to submit a fresh update at any time.
 *
 *      On an L2 the heartbeat alone is not enough. Chainlink's nodes publish updates THROUGH the
 *      sequencer, so while the sequencer is down every feed keeps serving its last pre-outage
 *      answer — and that answer can still be well inside its heartbeat when the queued
 *      transactions land on recovery. {SEQUENCER_UPTIME_FEED} closes that gap: valuations revert
 *      while the sequencer is down and for {SEQUENCER_GRACE_PERIOD} after it returns, giving the
 *      feeds time to publish post-outage prices. It is address(0) on chains with no sequencer
 *      (Ethereum mainnet, BSC, local Anvil), where the check is skipped outright.
 */
contract SHOracle is Ownable {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts when an unsupported token address is provided
    error PriceOracle_UnsupportedToken();

    /// @dev Reverts when a Chainlink price feed has not been updated within its configured heartbeat
    error PriceOracle_StalePrice();

    /// @dev Reverts when a Chainlink feed reports a non-positive price (0 or negative). This signals a
    ///      feed malfunction, not a real quote, and must be rejected before the cast to uint256.
    error PriceOracle_InvalidPrice();

    /// @dev Reverts when the tokens and priceFeeds constructor arrays have different lengths
    error PriceOracle_ArrayLengthMismatch();

    /// @dev Reverts when address(0) is supplied as a feed address to {setFeed}. Registering a zero
    ///      feed would leave the token reading as unpriced while looking registered; use
    ///      {removeFeed} to deregister instead.
    error PriceOracle_InvalidFeed();

    /// @dev Reverts when a heartbeat is 0 (every feed would read as stale) or exceeds the uint48
    ///      storage ceiling, where the cast would silently truncate into a much tighter ceiling.
    error PriceOracle_InvalidHeartbeat();

    /// @dev Reverts on any attempt to deregister the native (address(0)) feed. SpendingLimitModule
    ///      prices the account's native balance delta on EVERY metered transaction, so removing this
    ///      feed would revert every native-moving execution on every wallet installed against this
    ///      oracle. Repoint it with {setFeed} instead, which overwrites in place.
    error PriceOracle_CannotRemoveNativeFeed();

    /// @dev Reverts when this chain's L2 sequencer uptime feed reports the sequencer as DOWN, or
    ///      reports a round that was never initialised (startedAt == 0) and so carries no status.
    ///      Every price feed on the chain is published through the sequencer, so none of them can
    ///      be trusted while it is offline — however recent their `updatedAt` still looks.
    error PriceOracle_SequencerDown();

    /// @dev Reverts while the sequencer has been back up for {SEQUENCER_GRACE_PERIOD} or less.
    ///      The feeds are reachable again at that point but may not have published since the
    ///      outage, so their answers can still predate it while passing the heartbeat check.
    error PriceOracle_SequencerGracePeriod();

    /// @dev Reverts at construction when the supplied sequencer uptime feed answers with anything
    ///      other than the 0/1 status flag one reports — the signature of a wrong-network address
    ///      or of a PRICE feed passed in by mistake, either of which would otherwise brick every
    ///      valuation the moment the oracle went live.
    error PriceOracle_InvalidSequencerFeed();

    /*//////////////////////////////////////////////////////////////
                            STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Sentinel value for native ETH (used instead of an actual token address)
    address private constant ETH_TOKEN_ADDRESS = address(0);

    /// @notice This chain's Chainlink L2 Sequencer Uptime Feed, or address(0) on a chain that has
    ///         no sequencer (Ethereum mainnet, BSC, local Anvil), which skips the check entirely.
    /// @dev Immutable rather than an owner setter, for three reasons. It is a per-chain constant,
    ///      not something that legitimately changes. It sits on the hot path of every metered
    ///      transaction, and an immutable is read from bytecode instead of costing an SLOAD. And a
    ///      setter would be one more immediate-effect admin lever over cap integrity, which
    ///      THREAT_MODEL §3.8 already names as the protocol's top residual risk. If Chainlink ever
    ///      retires this aggregator, the fix is the route that already exists for a bad oracle:
    ///      {SHRegistry-proposePriceOracle} a replacement, seeded via {SHTreasury-setFeed} while it
    ///      sits in the timelock.
    address public immutable SEQUENCER_UPTIME_FEED;

    /// @notice How long the sequencer must have been continuously up before prices are trusted
    ///         again, matching Chainlink's reference implementation for L2 feed consumers.
    /// @dev Sized to outlast the shortest feed heartbeats on the supported L2s (Arbitrum's
    ///      ETH/USD, BTC/USD and LINK/USD all publish every 1755s), so by the time this window
    ///      closes the feeds a valuation depends on have had the chance to publish post-outage.
    uint256 public constant SEQUENCER_GRACE_PERIOD = 1 hours;

    /// @notice The two answers an L2 Sequencer Uptime Feed reports: 0 while the sequencer is up,
    ///         1 while it is down. A status flag, not a price — anything else is not such a feed.
    int256 private constant SEQUENCER_UP = 0;
    int256 private constant SEQUENCER_DOWN = 1;

    /// @notice Everything getPrice needs for one token, packed into a single 32-byte storage slot.
    /// @dev address(20) + uint8(1) + uint48(6) = 27 bytes, so a getPrice reads ONE slot instead of
    ///      two separate mappings, and `decimals` is cached here at construction so getPrice no
    ///      longer makes an external IERC20Metadata.decimals() call into an arbitrary token on
    ///      every valuation. A uint48 heartbeat holds ~8.9 million years of seconds — Chainlink
    ///      heartbeats are hours-to-days, so there is no practical ceiling being given up.
    struct Feed {
        address feed; // Chainlink USD price feed for this token; address(0) means "not registered"
        uint8 decimals; // the token's own ERC-20 decimals (18 for native ETH), cached at construction
        uint48 heartbeat; // this feed's staleness ceiling in seconds (its Chainlink-published heartbeat)
    }

    /// @notice Maps each registered token address to its packed Feed record.
    /// @dev Populated once in the constructor. Unregistered tokens map to a zeroed Feed (feed == address(0)).
    ///      Chainlink heartbeats vary per feed: volatile assets update hourly, stablecoins every 23–24 hours.
    ///      Using a uniform timeout would either flag stablecoin feeds as stale or mask genuinely stale volatile feeds.
    mapping(address => Feed) private sFeeds;

    /// @notice Multiplier to convert Chainlink's 8-decimal prices to 18-decimal precision
    /// @dev Chainlink returns prices with 8 decimals. Multiply by 1e10 to get 18 decimals.
    int256 private constant ADDITIONAL_FEED_PRECISION = 1e10;

    /*//////////////////////////////////////////////////////////////
                                  EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when a feed is registered or repointed by the owner. One event covers both:
    ///         {setFeed} overwrites in place, so there is no separate "added" vs "updated" case.
    /// @param token     The token the feed prices (address(0) for native).
    /// @param feed      The Chainlink aggregator now registered for it.
    /// @param heartbeat That feed's staleness ceiling in seconds.
    event FeedSet(address indexed token, address indexed feed, uint256 heartbeat);

    /// @notice Emitted when a feed is deregistered by the owner.
    /// @param token The token that is no longer priced.
    event FeedRemoved(address indexed token);

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
     * @param sequencerUptimeFeed This chain's Chainlink L2 Sequencer Uptime Feed. Pass address(0)
     *                    on a chain with no sequencer, which disables the check. Any non-zero value
     *                    is probed once here so a wrong address fails at deploy rather than
     *                    bricking every valuation later.
     * @param tokens      Ordered list of token addresses to support. Use address(0) for ETH.
     * @param priceFeeds  Ordered list of Chainlink USD price feed addresses, one per token.
     *                    Pass address(0) for tokens that have no feed on this network.
     * @param heartbeats  Ordered list of heartbeat intervals in seconds, one per feed.
     *                    Matches the Chainlink-published heartbeat for each feed (e.g. 3600 for ETH/USD, 82800 for USDC/USD).
     *                    The value at index i is ignored when priceFeeds[i] is address(0).
     */
    constructor(
        address owner,
        address sequencerUptimeFeed,
        address[] memory tokens,
        address[] memory priceFeeds,
        uint256[] memory heartbeats
    ) Ownable(owner) {
        if (tokens.length != priceFeeds.length || priceFeeds.length != heartbeats.length) {
            revert PriceOracle_ArrayLengthMismatch();
        }
        SEQUENCER_UPTIME_FEED = sequencerUptimeFeed;
        // A mistyped or wrong-network uptime feed would otherwise pass construction and then revert
        // every single valuation, protocol-wide, with no way to correct it short of the two-day
        // oracle swap. One probe here turns that into a failed deploy: an address with no code (or
        // the wrong ABI) fails the decode, and a PRICE feed passed in by mistake fails the 0/1
        // status check. The status itself is deliberately NOT asserted — a deploy that happens to
        // land during an outage, or inside the grace window, must still succeed.
        if (sequencerUptimeFeed != address(0)) {
            (, int256 status,,,) = AggregatorV3Interface(sequencerUptimeFeed).latestRoundData();
            if (status != SEQUENCER_UP && status != SEQUENCER_DOWN) revert PriceOracle_InvalidSequencerFeed();
        }
        for (uint256 i = 0; i < tokens.length; i++) {
            if (priceFeeds[i] != address(0)) {
                _setFeed(tokens[i], priceFeeds[i], heartbeats[i]);
            }
        }
    }

    /*//////////////////////////////////////////////////////////////
                        EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /// @dev True if `token` has a registered Chainlink price feed (i.e. is safe to price via getPrice).
    function isPriced(address token) external view returns (bool) {
        return sFeeds[token].feed != address(0);
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
    function getPrice(address token, uint256 amount) public view returns (int256) {
        Feed memory f = sFeeds[token]; // single SLOAD: feed + decimals + heartbeat all in one slot
        if (f.feed == address(0)) revert PriceOracle_UnsupportedToken();

        int256 price = _stalePriceCheck(f.feed, f.heartbeat);

        // amount is a token balance/allowance that cannot approach 2**255, so the cast never truncates.
        // forge-lint: disable-next-line(unsafe-typecast)
        return (int256(amount) * price * ADDITIONAL_FEED_PRECISION) / int256(10 ** f.decimals);
    }

    /**
     * @dev Validates Chainlink price feed freshness and returns the current price
     * @param priceFeed Address of the Chainlink price feed to query
     * @param heartbeat This feed's staleness ceiling in seconds (passed in from the packed Feed
     *                  record so this function makes no extra storage read of its own)
     * @return price    The current price with 8 decimals (Chainlink standard)
     *
     * @notice Reverts with PriceOracle_StalePrice if the feed has not updated within its heartbeat.
     *
     * Why this matters: stale price data can lead to incorrect USD conversions. For instance,
     * if ETH crashes from $2500 to $1500 but the feed has not updated in 5 hours, using the
     * stale price would incorrectly value ETH and may allow overspending beyond session limits.
     */
    function _stalePriceCheck(address priceFeed, uint256 heartbeat) internal view returns (int256) {
        // Before trusting any feed's timestamp, confirm the chain that publishes it was actually
        // live. On an L2 an outage freezes every feed without making any of them look stale.
        _requireSequencerUp();

        (, int256 price,, uint256 updatedAt,) = AggregatorV3Interface(priceFeed).latestRoundData();

        if (block.timestamp - updatedAt > heartbeat) {
            revert PriceOracle_StalePrice();
        }
        // A non-positive price is a feed malfunction, not a real quote; reject it before the cast
        // below would turn a negative value into an enormous uint that wildly mis-prices the call.
        if (price <= 0) revert PriceOracle_InvalidPrice();

        // forge-lint: disable-next-line(unsafe-typecast)
        return price;
    }

    /**
     * @dev Refuses to price anything while this chain's sequencer is down, or has only just come
     *      back. No-ops on a chain with no uptime feed configured.
     *
     *      The hole this closes: Chainlink's nodes submit price updates as L2 transactions, so a
     *      sequencer outage freezes every feed at its last pre-outage answer. That answer's
     *      `updatedAt` keeps looking recent — a feed with a 24h heartbeat sails through
     *      {_stalePriceCheck} on a two-hour-old price — so the heartbeat check alone would happily
     *      value a transaction at a price the market has since left behind. Transactions queue
     *      during an outage and land the moment it clears, which is exactly when that matters.
     *
     *      Both branches fail CLOSED, consistent with the rest of the oracle: a valuation that
     *      cannot be trusted blocks the transaction rather than being guessed at. During the window
     *      that halts every session-key execution and any owner execution that moves native or a
     *      watched token; owner executions touching only unwatched tokens, and
     *      {SessionHandler-pause}, stay available.
     */
    function _requireSequencerUp() internal view {
        address uptimeFeed = SEQUENCER_UPTIME_FEED;
        if (uptimeFeed == address(0)) return; // chain has no sequencer — nothing to gate on

        (, int256 answer, uint256 startedAt,,) = AggregatorV3Interface(uptimeFeed).latestRoundData();

        // A zero startedAt is an uninitialised round, which reports no status at all — treated as
        // down, since "we cannot tell" must not read as "up".
        if (answer != SEQUENCER_UP || startedAt == 0) revert PriceOracle_SequencerDown();

        // startedAt stamps when the CURRENT status began, so on an up round this subtraction is
        // exactly how long the sequencer has been back.
        if (block.timestamp - startedAt <= SEQUENCER_GRACE_PERIOD) revert PriceOracle_SequencerGracePeriod();
    }

    /*//////////////////////////////////////////////////////////////
                            FEED ADMIN (owner-only)
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Registers a price feed for `token`, or repoints an existing one in place.
     * @dev Overwriting is deliberate and is the supported way to correct a wrong heartbeat or
     *      migrate to a replacement aggregator without deregistering the token first — a
     *      remove-then-add would leave every wallet unable to price that token in between.
     *      Feeds registered here take effect on the next valuation, protocol-wide, with no delay;
     *      the timelock protecting wallets from a bad oracle lives on
     *      {SHRegistry-proposePriceOracle}, which governs WHICH oracle they read, not its contents.
     *      The owner is therefore trusted for cap integrity either way (THREAT_MODEL §3.8).
     * @dev The token's decimals are read on-chain rather than supplied by the caller: a wrong
     *      decimals value would silently mis-price every valuation of that token by a power of ten,
     *      with nothing on-chain to catch it.
     * @param token     Token to price. Use address(0) for native.
     * @param priceFeed Chainlink USD aggregator for it. Must not be address(0).
     * @param heartbeat That feed's Chainlink-published heartbeat, in seconds. Must be > 0.
     */
    function setFeed(address token, address priceFeed, uint256 heartbeat) external onlyOwner {
        _setFeed(token, priceFeed, heartbeat);
    }

    /**
     * @notice Deregisters `token`, after which {isPriced} reports false and {getPrice} reverts.
     * @dev Refuses to remove the native (address(0)) feed — see {PriceOracle_CannotRemoveNativeFeed}.
     *      Removing a token that accounts currently watch does NOT unwatch it: their next metered
     *      transaction that moves it will revert in the hook's postCheck. Have accounts drop it from
     *      their watched list first.
     * @param token The token to stop pricing. Must not be address(0).
     */
    function removeFeed(address token) external onlyOwner {
        if (token == ETH_TOKEN_ADDRESS) revert PriceOracle_CannotRemoveNativeFeed();
        delete sFeeds[token];
        emit FeedRemoved(token);
    }

    /// @dev Shared write path for the constructor and {setFeed}, so both validate identically and
    ///      both cache the token's own decimals rather than trusting a supplied value.
    function _setFeed(address token, address priceFeed, uint256 heartbeat) internal {
        if (priceFeed == address(0)) revert PriceOracle_InvalidFeed();
        // A zero heartbeat reads as permanently stale; anything past uint48 would truncate on the
        // cast below into a far tighter ceiling than intended. Real feeds are hours-to-days.
        if (heartbeat == 0 || heartbeat > type(uint48).max) revert PriceOracle_InvalidHeartbeat();
        // Native ETH has no contract to query; every other token exposes its own decimals.
        uint8 tokenDecimals = token == ETH_TOKEN_ADDRESS ? 18 : IERC20Metadata(token).decimals();
        sFeeds[token] = Feed({feed: priceFeed, decimals: tokenDecimals, heartbeat: uint48(heartbeat)});
        emit FeedSet(token, priceFeed, heartbeat);
    }
}
