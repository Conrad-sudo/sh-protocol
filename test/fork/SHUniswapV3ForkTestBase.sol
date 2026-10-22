// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/interfaces/IERC20.sol";
import {Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {Vm} from "forge-std/Vm.sol";
import {IV3SwapRouter} from "@uniswap/swap-router-contracts/contracts/interfaces/IV3SwapRouter.sol";
import {IPeripheryImmutableState} from "@uniswap/v3-periphery/contracts/interfaces/IPeripheryImmutableState.sol";
import {IPeripheryPayments} from "@uniswap/v3-periphery/contracts/interfaces/IPeripheryPayments.sol";
import {IMulticall} from "@uniswap/v3-periphery/contracts/interfaces/IMulticall.sol";
import {INonfungiblePositionManager} from "@uniswap/v3-periphery/contracts/interfaces/INonfungiblePositionManager.sol";
import {IUniswapV3Factory} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Factory.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {SHForkTestBase} from "./SHForkTestBase.sol";
import "../../script/Constants.s.sol";

/**
 * @title SHUniswapV3ForkTestBase
 * @notice The shared fork suite on Uniswap V3: swaps go through SwapRouter02, liquidity is a V3
 *         position (an ERC-721) minted by the NonfungiblePositionManager (the "position manager"),
 *         and the wallet trusts both, as constants.get_default_spenders does. Every test in
 *         SHForkTestBase runs; three things differ from the V2 suite: the swap calldata, the trusted
 *         spenders, and the liquidity round-trip.
 * @dev SwapRouter02 is called through IV3SwapRouter, not v3-periphery's ISwapRouter: its params have
 *      no `deadline`, so the selectors differ (see Constants.s.sol).
 *
 *      Each suite trades ONE pool, WETH/_tokenOut() at _poolFee(), so _swapPath() must start at the
 *      router's WETH9:
 *        - token swap:  exactInputSingle(WETH -> tokenOut)
 *        - native in:   exactInputSingle(WETH9 -> tokenOut) with the ETH forwarded; the router wraps
 *                       it. amountIn equals the value sent, so no ETH is left in the router.
 *        - native out:  multicall[exactInputSingle(tokenOut -> WETH9, paid to the router),
 *                       unwrapWETH9(0, wallet)]. One call, so the WETH never sits in the router
 *                       between the steps, where anyone could sweep it.
 *
 *      The liquidity round-trip opens a position in that same pool and closes it again, every step
 *      a session-key UserOp through the real EntryPoint, like the app sends them:
 *        1. approve both tokens to the position manager, mint, approve both back to 0 (one batch);
 *        2. multicall[decreaseLiquidity(half), collect] (one batch);
 *        3. multicall[decreaseLiquidity(the rest), collect, burn] (one batch).
 *      Step 1 is metered like any deposit: both tokens leave the wallet. Steps 2 and 3 are not: all
 *      of it comes back, and neither needs an approval, because the wallet owns the NFT. The position
 *      manager mints with a plain `_mint`, not `_safeMint`, so SessionHandler (which has no
 *      onERC721Received) can hold a position, and step 1 proves that on every network.
 */
abstract contract SHUniswapV3ForkTestBase is SHForkTestBase {
    /// @dev The chain's NonfungiblePositionManager, read from Constants.s.sol like the router. Set in
    ///      setUp, before the base deploys the wallet, because {_trustedSpenders} names it.
    address positionManager;

    /// @dev How far either side of the current tick a test position reaches, in tick spacings. A few
    ///      hundred keeps the current price well inside the range at every fee tier, so the pool
    ///      takes BOTH tokens, while it would still take a big price move to leave it.
    int256 private constant POSITION_HALF_WIDTH_SPACINGS = 300;

    /// @dev The fields of a position that the round-trip checks, as `positions(tokenId)` reports them.
    struct Position {
        address token0;
        address token1;
        uint24 fee;
        int24 tickLower;
        int24 tickUpper;
        uint128 liquidity;
    }

    /// @dev Fee tier of the WETH/_tokenOut() pool, in hundredths of a basis point (500 = 0.05%).
    function _poolFee() internal view virtual returns (uint24);

    /// @dev Resolves the position manager before the base deploys the wallet. Each chain suite selects
    ///      its fork before it calls this, so block.chainid is already the forked chain's.
    function setUp() public virtual override {
        positionManager = _positionManagerForChain();
        super.setUp();
    }

    function _positionManagerForChain() internal view returns (address) {
        if (block.chainid == MAINNET_CHAIN_ID) return MNT_UNISWAP_V3_POSITION_MANAGER;
        if (block.chainid == SEPOLIA_CHAIN_ID) return SPO_UNISWAP_V3_POSITION_MANAGER;
        if (block.chainid == ARB_CHAIN_ID) return ARB_UNISWAP_V3_POSITION_MANAGER;
        if (block.chainid == BASE_CHAIN_ID) return BASE_UNISWAP_V3_POSITION_MANAGER;
        revert("no position manager constant for this chain");
    }

    /// @dev [SwapRouter02, position manager]: what the app passes deployWallet on these chains. The
    ///      grants exist for tokens the oracle cannot price, and nothing in this suite approves one,
    ///      so test_walletDeploysWithCapConfigOnFork, which checks both entries landed, is the only
    ///      test that depends on them.
    function _trustedSpenders() internal view override returns (address[] memory spenders) {
        spenders = new address[](2);
        spenders[0] = swapRouter;
        spenders[1] = positionManager;
    }

    function _swapRouterForChain() internal view override returns (address) {
        if (block.chainid == MAINNET_CHAIN_ID) return MNT_UNISWAP_V3_SWAP_ROUTER_02;
        if (block.chainid == SEPOLIA_CHAIN_ID) return SPO_UNISWAP_V3_SWAP_ROUTER_02;
        if (block.chainid == ARB_CHAIN_ID) return ARB_UNISWAP_V3_SWAP_ROUTER_02;
        if (block.chainid == BASE_CHAIN_ID) return BASE_UNISWAP_V3_SWAP_ROUTER_02;
        revert("no SwapRouter02 constant for this chain");
    }

    function _swapCalldata(uint256 amountIn) internal view override returns (bytes memory) {
        return _exactInputSingle(address(_tokenIn()), address(_tokenOut()), address(wallet), amountIn);
    }

    function _nativeInSwapCalldata(uint256 value) internal view override returns (bytes memory) {
        return _exactInputSingle(_weth9(), address(_tokenOut()), address(wallet), value);
    }

    function _nativeOutSwapCalldata(address token, uint256 amount) internal view override returns (bytes memory) {
        bytes[] memory calls = new bytes[](2);
        calls[0] = _exactInputSingle(token, _weth9(), swapRouter, amount);
        calls[1] = abi.encodeCall(IPeripheryPayments.unwrapWETH9, (0, address(wallet)));
        return abi.encodeCall(IMulticall.multicall, (calls));
    }

    function _exactInputSingle(address tokenIn, address tokenOut, address recipient, uint256 amountIn)
        private
        view
        returns (bytes memory)
    {
        return abi.encodeCall(
            IV3SwapRouter.exactInputSingle,
            (
                IV3SwapRouter.ExactInputSingleParams({
                    tokenIn: tokenIn,
                    tokenOut: tokenOut,
                    fee: _poolFee(),
                    recipient: recipient,
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            )
        );
    }

    /// @dev The wrapped native token the router wraps into and unwraps from.
    function _weth9() internal view returns (address) {
        return IPeripheryImmutableState(swapRouter).WETH9();
    }

    /*//////////////////////////////////////////////////////////////
               LIQUIDITY ROUND-TRIP (a V3 position, as an NFT)
    //////////////////////////////////////////////////////////////*/

    /// @dev Opens a position in the suite's own pool, takes half out, then the rest and burns it,
    ///      all as the session key. See the contract-level NatSpec for what each step must show.
    function _liquidityRoundTrip() internal override {
        INonfungiblePositionManager npm = INonfungiblePositionManager(positionManager);

        // The constant has to name the real contract, and the same Uniswap deployment the router
        // belongs to. Fail here with a name if it does not, not somewhere inside the mint.
        assertGt(address(npm).code.length, 0, "no position manager at this address on this chain");
        assertEq(npm.factory(), IPeripheryImmutableState(swapRouter).factory(), "position manager: wrong V3 factory");
        assertEq(npm.WETH9(), _weth9(), "position manager: wrong WETH9");

        uint256 tokenId = _openPosition(npm);
        _withdrawHalf(npm, tokenId);
        _withdrawRestAndBurn(npm, tokenId);
    }

    /// @dev Step 1, one batch: [approve tokenIn, approve tokenOut, mint, approve tokenIn 0, approve
    ///      tokenOut 0]. The mint pulls only what the pool's price and the range call for, so at least
    ///      one of the two approvals is bigger than what is used, and the trailing zeros are what
    ///      leave nothing standing. Returns the new position's token id.
    function _openPosition(INonfungiblePositionManager npm) private returns (uint256 tokenId) {
        IERC20 tokenIn = _tokenIn();
        IERC20 tokenOut = _tokenOut();
        INonfungiblePositionManager.MintParams memory params = _mintParams(npm);
        (uint256 amountIn, uint256 amountOut) = params.token0 == address(tokenIn)
            ? (params.amount0Desired, params.amount1Desired)
            : (params.amount1Desired, params.amount0Desired);

        // The position manager pays the WETH side out of its OWN ETH when it holds at least that
        // much, which a testnet's can. Empty it, so the WETH that goes in is the wallet's.
        vm.deal(address(npm), 0);

        Execution[] memory open = new Execution[](5);
        open[0] = Execution(address(tokenIn), 0, abi.encodeCall(IERC20.approve, (address(npm), amountIn)));
        open[1] = Execution(address(tokenOut), 0, abi.encodeCall(IERC20.approve, (address(npm), amountOut)));
        open[2] = Execution(address(npm), 0, abi.encodeCall(INonfungiblePositionManager.mint, (params)));
        open[3] = Execution(address(tokenIn), 0, abi.encodeCall(IERC20.approve, (address(npm), 0)));
        open[4] = Execution(address(tokenOut), 0, abi.encodeCall(IERC20.approve, (address(npm), 0)));

        uint256 nftsBefore = npm.balanceOf(address(wallet));
        uint256 inBefore = tokenIn.balanceOf(address(wallet));
        uint256 outBefore = tokenOut.balanceOf(address(wallet));
        int256 spentBefore = _spentInWindow();

        _runSessionBatch(open);

        // The wallet owns the new position. A fresh NFT is appended to the owner's list, so it sits
        // at the index equal to the balance before. Checked against `positions`, so what the wallet
        // holds is the position that was asked for, not just some NFT.
        assertEq(npm.balanceOf(address(wallet)), nftsBefore + 1, "wallet did not receive a position NFT");
        tokenId = npm.tokenOfOwnerByIndex(address(wallet), nftsBefore);
        assertEq(npm.ownerOf(tokenId), address(wallet), "position NFT is not the wallet's");
        Position memory position = _position(npm, tokenId);
        assertEq(position.token0, params.token0, "position token0");
        assertEq(position.token1, params.token1, "position token1");
        assertEq(position.fee, _poolFee(), "position fee tier");
        assertEq(position.tickLower, params.tickLower, "position lower tick");
        assertEq(position.tickUpper, params.tickUpper, "position upper tick");
        assertGt(position.liquidity, 0, "position has no liquidity");

        // The deposit is metered like any other outflow: both tokens left the wallet, nothing came
        // back, so the window rose by exactly their oracle value. Computed from the real balance
        // diffs, so it holds whatever the pool ratio.
        uint256 usedIn = inBefore - tokenIn.balanceOf(address(wallet));
        uint256 usedOut = outBefore - tokenOut.balanceOf(address(wallet));
        assertGt(usedIn, 0, "no tokenIn went into the position");
        assertGt(usedOut, 0, "no tokenOut went into the position");
        int256 expected = oracle.getPrice(address(tokenIn), usedIn) + oracle.getPrice(address(tokenOut), usedOut);
        assertGt(_spentInWindow(), spentBefore, "deposit was not metered");
        assertEq(_spentInWindow(), spentBefore + expected, "metering != oracle value of what went in");

        // The no-standing-approval rule, held to the letter: neither token has an allowance left.
        assertEq(tokenIn.allowance(address(wallet), address(npm)), 0, "tokenIn approval left standing");
        assertEq(tokenOut.allowance(address(wallet), address(npm)), 0, "tokenOut approval left standing");
    }

    /// @dev Step 2, one batch of one call, the way the app's withdraw_from_position sends it: a
    ///      position-manager multicall of [decreaseLiquidity(half), collect(wallet, max, max)]. No
    ///      approval is involved. Both tokens come back, so the window must not move.
    function _withdrawHalf(INonfungiblePositionManager npm, uint256 tokenId) private {
        uint128 liquidity = _position(npm, tokenId).liquidity;
        uint128 half = liquidity / 2;
        assertGt(half, 0, "position too small to halve");

        bytes[] memory calls = new bytes[](2);
        calls[0] = _decreaseLiquidity(tokenId, half);
        calls[1] = _collectAll(tokenId);
        Execution[] memory withdraw = new Execution[](1);
        withdraw[0] = Execution(address(npm), 0, abi.encodeCall(IMulticall.multicall, (calls)));

        uint256 inBefore = _tokenIn().balanceOf(address(wallet));
        uint256 outBefore = _tokenOut().balanceOf(address(wallet));
        int256 spentBefore = _spentInWindow();

        _runSessionBatch(withdraw);

        _assertCameBackUnmetered(inBefore, outBefore, spentBefore);
        assertEq(_position(npm, tokenId).liquidity, liquidity - half, "wrong liquidity left in the position");
        assertEq(npm.ownerOf(tokenId), address(wallet), "wallet no longer owns the position");
    }

    /// @dev Step 3, the same shape with the rest: [decreaseLiquidity(all that is left), collect,
    ///      burn]. burn only works on an empty position whose tokens are all collected, so a clean
    ///      run also shows that `collect(max, max)` took everything. The NFT is gone afterwards.
    function _withdrawRestAndBurn(INonfungiblePositionManager npm, uint256 tokenId) private {
        uint128 rest = _position(npm, tokenId).liquidity;
        assertGt(rest, 0, "nothing left to withdraw");

        bytes[] memory calls = new bytes[](3);
        calls[0] = _decreaseLiquidity(tokenId, rest);
        calls[1] = _collectAll(tokenId);
        calls[2] = abi.encodeCall(INonfungiblePositionManager.burn, (tokenId));
        Execution[] memory close = new Execution[](1);
        close[0] = Execution(address(npm), 0, abi.encodeCall(IMulticall.multicall, (calls)));

        uint256 nftsBefore = npm.balanceOf(address(wallet));
        uint256 inBefore = _tokenIn().balanceOf(address(wallet));
        uint256 outBefore = _tokenOut().balanceOf(address(wallet));
        int256 spentBefore = _spentInWindow();

        _runSessionBatch(close);

        assertEq(npm.balanceOf(address(wallet)), nftsBefore - 1, "position NFT was not burned");
        try npm.ownerOf(tokenId) returns (address stillOwnedBy) {
            revert(string.concat("burned position still has an owner: ", vm.toString(stillOwnedBy)));
        } catch {}
        _assertCameBackUnmetered(inBefore, outBefore, spentBefore);
    }

    /// @dev What a withdrawal must look like to the cap: both tokens back in the wallet, and the
    ///      window exactly where it was, because an inflow is never metered as spending.
    function _assertCameBackUnmetered(uint256 inBefore, uint256 outBefore, int256 spentBefore) private view {
        assertGt(_tokenIn().balanceOf(address(wallet)), inBefore, "tokenIn did not come back");
        assertGt(_tokenOut().balanceOf(address(wallet)), outBefore, "tokenOut did not come back");
        assertEq(_spentInWindow(), spentBefore, "a withdrawal was metered as spending");
    }

    /// @dev The mint parameters for a position in the suite's own pool, with the wallet funded to
    ///      afford it. The pool comes from the factory (`getPool`), never from a computed address.
    ///      The range is POSITION_HALF_WIDTH_SPACINGS tick spacings either side of the current tick,
    ///      rounded down to the pool's tick grid, which a position's ticks must sit on.
    ///
    ///      Sized by what the suite's usual trade is worth: _swapAmount() of tokenIn (WETH), and, as
    ///      the other side, twice what that is worth in tokenOut at the pool's own price. A range
    ///      centred on the price takes about equal value of each token, so the doubled side always
    ///      has more than the pool asks for, and the mint is bounded by the tokenIn side. That makes
    ///      the deposit worth about twice _swapAmount() of WETH (two ETH at the most, on mainnet),
    ///      far inside the $100k cap. Both minimums are 0, which is fine in a test: nothing can move
    ///      the price between reading it here and the mint.
    function _mintParams(INonfungiblePositionManager npm)
        private
        returns (INonfungiblePositionManager.MintParams memory params)
    {
        IUniswapV3Pool pool = IUniswapV3Pool(
            IUniswapV3Factory(npm.factory()).getPool(address(_tokenIn()), address(_tokenOut()), _poolFee())
        );
        require(address(pool) != address(0), "no V3 pool for the swap pair at this fee tier");

        (uint160 sqrtPriceX96, int24 tick,,,,,) = pool.slot0();
        int256 spacing = pool.tickSpacing();
        int256 centre = _floorToSpacing(tick, spacing);

        params.token0 = pool.token0();
        params.token1 = pool.token1();
        params.fee = _poolFee();
        params.tickLower = SafeCast.toInt24(centre - POSITION_HALF_WIDTH_SPACINGS * spacing);
        params.tickUpper = SafeCast.toInt24(centre + POSITION_HALF_WIDTH_SPACINGS * spacing);
        params.recipient = address(wallet);
        params.deadline = block.timestamp + 1 hours;

        uint256 amountIn = _swapAmount();
        bool inIsToken0 = address(_tokenIn()) == params.token0;
        uint256 amountOut = 2 * _worthAtPoolPrice(sqrtPriceX96, inIsToken0, amountIn);
        (params.amount0Desired, params.amount1Desired) = inIsToken0 ? (amountIn, amountOut) : (amountOut, amountIn);

        // setUp funds tokenIn only (a position takes both sides), so top the wallet up on each.
        _fundWallet(_tokenIn(), amountIn);
        _fundWallet(_tokenOut(), amountOut);
    }

    /// @dev What `amount` of one pool token is worth in the other at the pool's CURRENT price, in raw
    ///      units, so differing decimals sort themselves out. The pool quotes token1 per token0 as
    ///      (sqrtPriceX96 / 2^96)^2; mulDiv keeps full precision where a plain multiply would
    ///      overflow, and the two steps keep each intermediate small.
    /// @param sqrtPriceX96 The pool's current sqrt price, as `slot0` reports it.
    /// @param amountIsToken0 True if `amount` is in the pool's token0, false if in token1.
    /// @param amount The amount to convert, in the units of the token it is in.
    function _worthAtPoolPrice(uint160 sqrtPriceX96, bool amountIsToken0, uint256 amount)
        private
        pure
        returns (uint256)
    {
        uint256 q96 = 1 << 96;
        if (amountIsToken0) return Math.mulDiv(Math.mulDiv(amount, sqrtPriceX96, q96), sqrtPriceX96, q96);
        return Math.mulDiv(Math.mulDiv(amount, q96, sqrtPriceX96), q96, sqrtPriceX96);
    }

    /// @dev Rounds `tick` DOWN to a multiple of `spacing`. Solidity's `/` truncates toward zero, which
    ///      rounds a negative tick UP, so a negative tick that is off the grid takes one more step
    ///      down. Prices of WETH against a stablecoin are negative ticks on Arbitrum and Base.
    function _floorToSpacing(int256 tick, int256 spacing) private pure returns (int256) {
        int256 steps = tick / spacing;
        if (tick < 0 && tick % spacing != 0) steps--;
        return steps * spacing;
    }

    /// @dev Tops the wallet up to AT LEAST `amount` of `token`, leaving a bigger balance alone.
    function _fundWallet(IERC20 token, uint256 amount) private {
        if (token.balanceOf(address(wallet)) < amount) deal(address(token), address(wallet), amount);
    }

    function _position(INonfungiblePositionManager npm, uint256 tokenId) private view returns (Position memory p) {
        (,, p.token0, p.token1, p.fee, p.tickLower, p.tickUpper, p.liquidity,,,,) = npm.positions(tokenId);
    }

    function _decreaseLiquidity(uint256 tokenId, uint128 liquidity) private view returns (bytes memory) {
        return abi.encodeCall(
            INonfungiblePositionManager.decreaseLiquidity,
            (INonfungiblePositionManager.DecreaseLiquidityParams({
                    tokenId: tokenId,
                    liquidity: liquidity,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp + 1 hours
                }))
        );
    }

    /// @dev Collects everything the position is owed (the liquidity just taken out, and any fees) to
    ///      the wallet itself, never to the position manager, which would leave it there.
    function _collectAll(uint256 tokenId) private view returns (bytes memory) {
        return abi.encodeCall(
            INonfungiblePositionManager.collect,
            (INonfungiblePositionManager.CollectParams({
                    tokenId: tokenId,
                    recipient: address(wallet),
                    amount0Max: type(uint128).max,
                    amount1Max: type(uint128).max
                }))
        );
    }

    /*//////////////////////////////////////////////////////////////
                         SESSION-KEY BATCHES
    //////////////////////////////////////////////////////////////*/

    /// @dev Runs `execs` as ONE batch UserOp signed by the session key, through the real EntryPoint
    ///      (the same helpers the swap tests use), and fails the test if the batch did not execute.
    ///      That check is not optional: when an account's execution reverts, handleOps does NOT. The
    ///      EntryPoint records the op as failed, still pays the bundler, and returns normally, so a
    ///      broken batch would otherwise show up only as unchanged state further down, with no reason.
    ///      Here the reason is re-thrown as it came (a custom error or a string), so the test fails
    ///      on what really went wrong.
    function _runSessionBatch(Execution[] memory execs) internal {
        PackedUserOperation memory userOp = _sessionBatchOp(execs);

        vm.recordLogs();
        _handleOps(userOp);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bool reported;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter != config.entryPoint || logs[i].topics.length == 0) continue;
            if (logs[i].topics[0] == IEntryPoint.UserOperationRevertReason.selector) {
                (, bytes memory reason) = abi.decode(logs[i].data, (uint256, bytes));
                assembly ("memory-safe") {
                    revert(add(reason, 0x20), mload(reason))
                }
            }
            if (logs[i].topics[0] == IEntryPoint.UserOperationEvent.selector) {
                (, bool success,,) = abi.decode(logs[i].data, (uint256, bool, uint256, uint256));
                assertTrue(success, "session batch reverted without a reason");
                reported = true;
            }
        }
        assertTrue(reported, "EntryPoint reported no result for the session batch");
    }
}
