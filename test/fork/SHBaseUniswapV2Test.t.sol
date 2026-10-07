// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/interfaces/IERC20.sol";
import {SHForkTestBase} from "./SHForkTestBase.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {AggregatorV3Interface} from "../../src/interfaces/AggregatorV3Interface.sol";
import "../../script/Constants.s.sol";

/**
 * @title SHBaseUniswapV2Test
 * @notice Base-fork run of the shared spending-cap fork suite against the real Uniswap V2 router,
 *         real Chainlink feeds, the real L2 Sequencer Uptime Feed, and the canonical EntryPoint.
 * @dev BASE FORK REQUIRED. Run with:
 *        make base-uniswap-test
 *        (forge test --match-path test/fork/SHBaseUniswapV2Test.t.sol --fork-url $BASE_RPC_URL)
 *
 *      Swap path: WETH -> USDC, the deepest Uniswap V2 pair on Base — around 250 WETH against 650k
 *      native USDC, priced within ~0.3% of Chainlink. Most other V2 pairs on Base hold dust (LINK,
 *      AAVE, WBTC), as most volume sits on V3 and Aerodrome. _swapAmount is 0.1 WETH, well under 1%
 *      of pool depth, so the swap tests are not measuring slippage. Both path ends are priced by the
 *      deployed oracle.
 *
 *      Base is an L2 like Arbitrum, so SHOracle's sequencer gate is live here too: the two tests
 *      below fire it against Base's real uptime feed, and everything inherited from the base proves
 *      it stays invisible in normal operation, since every valuation consults that feed.
 */
contract SHBaseUniswapV2Test is SHForkTestBase {
    function _swapPath() internal view override returns (address[] memory path) {
        path = new address[](2);
        path[0] = config.weth;
        path[1] = config.usdc;
    }

    function _swapAmount() internal pure override returns (uint256) {
        return 0.1e18; // 0.1 WETH — <1% of the V2 pool, so slippage stays negligible
    }

    function _feedsToFreshen() internal view override returns (address[] memory feeds) {
        // ETH/USD covers both the WETH leg and native metering (Base's gas token is ETH), so the
        // native-in/out swap tests price the wallet's ETH delta through this same feed.
        feeds = new address[](2);
        feeds[0] = config.ethUsdPriceFeed;
        feeds[1] = config.usdcUsdPriceFeed;
    }

    /*//////////////////////////////////////////////////////////////
                    L2 SEQUENCER GATE (real feed)
    //////////////////////////////////////////////////////////////*/

    /// @notice The deployed oracle is wired to Base's real uptime feed, that feed reads UP, and it
    ///         has been up for longer than the grace period — so pricing works normally. Without
    ///         this, a silently-zero `sequencerUptimeFeed` would let the whole suite pass while the
    ///         gate was never engaged at all.
    function test_oracleIsGatedByTheRealSequencerFeed() public view {
        assertEq(oracle.SEQUENCER_UPTIME_FEED(), BASE_SEQUENCER_UPTIME_FEED, "oracle is not sequencer-gated");

        (, int256 answer, uint256 startedAt,,) = AggregatorV3Interface(BASE_SEQUENCER_UPTIME_FEED).latestRoundData();
        assertEq(answer, int256(0), "fork block caught the sequencer down");
        assertGt(block.timestamp - startedAt, oracle.SEQUENCER_GRACE_PERIOD(), "fork block is inside the grace window");

        assertGt(oracle.getPrice(address(0), 1 ether), 0, "gate is blocking a healthy chain");
    }

    /// @notice The gate firing, end to end on the real deployment: with the real uptime feed made
    ///         to report DOWN, a watched-token transfer is refused, because postCheck cannot price
    ///         the balance change. Driven owner-side so the revert surfaces directly instead of
    ///         being absorbed by the EntryPoint — and note it blocks the OWNER too, which is the
    ///         blast radius recorded in THREAT_MODEL §3.14.
    function test_sequencerDown_haltsMeteredExecutionOnFork() public {
        uint256 amount = _swapAmount();
        uint256 balanceBefore = _tokenIn().balanceOf(address(wallet));

        _mockSequencer(1, block.timestamp - 1 days); // 1 = down

        vm.prank(owner);
        vm.expectRevert(SHOracle.PriceOracle_SequencerDown.selector);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(_tokenIn()), uint256(0), abi.encodeCall(IERC20.transfer, (sink, amount)))
        );

        assertEq(_tokenIn().balanceOf(address(wallet)), balanceBefore, "revert was not atomic");
    }

    /// @notice Recovery is not instant: with the sequencer back up but only just, the grace period
    ///         still blocks metering, and clears once it elapses.
    function test_sequencerJustRecovered_clearsAfterGraceOnFork() public {
        uint256 amount = _swapAmount();
        uint256 grace = oracle.SEQUENCER_GRACE_PERIOD();
        _mockSequencer(0, block.timestamp); // up, as of right now

        vm.prank(owner);
        vm.expectRevert(SHOracle.PriceOracle_SequencerGracePeriod.selector);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(_tokenIn()), uint256(0), abi.encodeCall(IERC20.transfer, (sink, amount)))
        );

        // As in the Arbitrum suite, the recovery instant is re-derived AFTER the warp rather than
        // carried across it in a local: solc may re-read block.timestamp past a vm.warp.
        vm.warp(block.timestamp + grace + 1);
        _freshenFeeds(); // the warp would otherwise leave the price feeds stale for unrelated reasons
        _mockSequencer(0, block.timestamp - grace - 1);

        vm.prank(owner);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(_tokenIn()), uint256(0), abi.encodeCall(IERC20.transfer, (sink, amount)))
        );

        assertEq(_tokenIn().balanceOf(sink), amount, "transfer still blocked after the grace period");
    }

    /// @dev Re-serves the real uptime feed with a chosen status. `startedAt` is what SHOracle reads
    ///      to decide how long the current status has held; the other fields are inert to it.
    function _mockSequencer(int256 answer, uint256 startedAt) internal {
        vm.mockCall(
            BASE_SEQUENCER_UPTIME_FEED,
            abi.encodeWithSelector(AggregatorV3Interface.latestRoundData.selector),
            abi.encode(uint80(1), answer, startedAt, startedAt, uint80(1))
        );
    }
}
