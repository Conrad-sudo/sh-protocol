// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {IERC20} from "@openzeppelin/contracts/interfaces/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/interfaces/IERC20Metadata.sol";
import {Execution, MODULE_TYPE_HOOK} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {IUniswapV2Router01} from "lib/v2-periphery/contracts/interfaces/IUniswapV2Router01.sol";
import {IUniswapV2Factory} from "lib/v2-core/contracts/interfaces/IUniswapV2Factory.sol";
import {AggregatorV3Interface} from "../../src/interfaces/AggregatorV3Interface.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import "../../script/Constants.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";

/**
 * @title SHForkTestBase
 * @notice Shared fork-test base for the USD-spending-cap design, parameterized per network by a
 *         swap path and feed list. Each network file (mainnet Uniswap, BSC Pancake, Sepolia
 *         Uniswap) supplies live token/pool specifics; every test here runs identically against
 *         the real router, real Chainlink feeds, and the real (canonical) EntryPoint.
 * @dev Metering assertions are computed from ACTUAL balance diffs priced through the deployed
 *      oracle, never from hardcoded dollar figures — so they hold regardless of pool ratios
 *      (testnet pools price independently of Chainlink; see project memory). Feeds are re-served
 *      at their real prices with a fresh timestamp (vm.mockCall) to remove staleness flake on
 *      pinned forks without distorting any valuation.
 */
abstract contract SHForkTestBase is Test {
    /// @dev A deadline every {SessionHandler-addSession} call in this file can use: comfortably in
    ///      the future, comfortably inside MAX_SESSION_TTL. Recomputed per call so a test that warps
    ///      time still grants a live key.
    function _sessionDeadline() internal view returns (uint48) {
        return uint48(block.timestamp + 30 days);
    }

    SHOracle oracle;
    SHRegistry feeRegistry;
    SHTreasury treasury;
    SHFactory factory;
    HelperConfig.NetworkConfig config;
    SessionHandler wallet;
    SpendingLimitModule module;
    SendPackedUserOp sendPackedUserOp;
    IUniswapV2Router01 router;

    int256 constant DAILY_LIMIT = 100_000e18; // $100k window cap: roomy enough for real-pool swaps
    uint256 constant WINDOW = 1 days;

    address owner;
    address sessionKey;
    uint256 sessionKeyPk;
    address sink = makeAddr("sink");
    address bundler = makeAddr("bundler");

    /*//////////////////////////////////////////////////////////////
                     NETWORK-SPECIFIC PARAMETERS
    //////////////////////////////////////////////////////////////*/

    /// @dev The router path for the swap tests; path[0] is spent, path[end] is received. Both ends
    ///      must be priced by the deployed oracle (they become the wallet's watched tokens).
    function _swapPath() internal view virtual returns (address[] memory);

    /// @dev Amount of path[0] to swap; size against real pool depth, not dollar intuition.
    function _swapAmount() internal view virtual returns (uint256);

    /// @dev Chainlink feeds the tests touch; each is re-served with a fresh timestamp in setUp.
    ///      Must include the native (address(0)) feed — ETH/USD or BNB/USD — since native value is
    ///      metered and the native-swap tests price the wallet's balance delta through it.
    function _feedsToFreshen() internal view virtual returns (address[] memory);

    /// @dev Native value forwarded by the native-in swap test (swapExactETHForTokens). Small enough
    ///      to be in-budget and shallow-pool-safe on every network; override if a pool needs less.
    function _nativeSwapValue() internal view virtual returns (uint256) {
        return 0.01 ether;
    }

    function _tokenIn() internal view returns (IERC20) {
        return IERC20(_swapPath()[0]);
    }

    function _tokenOut() internal view returns (IERC20) {
        address[] memory path = _swapPath();
        return IERC20(path[path.length - 1]);
    }

    /*//////////////////////////////////////////////////////////////
                                 SETUP
    //////////////////////////////////////////////////////////////*/

    function setUp() public virtual {
        (sessionKey, sessionKeyPk) = makeAddrAndKey("sessionKey");

        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        feeRegistry = treasury.REGISTRY();
        module = SpendingLimitModule(feeRegistry.spendingLimitModule());
        owner = config.account;
        router = IUniswapV2Router01(_routerForChain());

        address[] memory watched = new address[](2);
        watched[0] = address(_tokenIn());
        watched[1] = address(_tokenOut());

        // Deploy exactly as app/deploy_wallet.py does in production: the session key and the chain's
        // REAL V2 router are seeded through deployWallet, not granted by follow-up owner calls.
        //
        // This is deliberately the one suite that covers the seeded path against a live router. The
        // unit tests only ever seed a makeAddr() spender against an ERC20Mock, and if setUp here
        // re-granted the router with addTrustedSpender afterwards (as it used to), a regression in
        // deploy-time seeding would be masked on every fork — the second grant would quietly repair
        // it and removeLiquidity's unpriced LP-token approval would pass either way.
        //
        // The router is still not protocol configuration: it is passed in by the caller, exactly as
        // constants.get_router(chain_id) is passed in by the app.
        address[] memory trustedSpenders = new address[](1);
        trustedSpenders[0] = address(router);

        vm.prank(owner);
        wallet =
            SessionHandler(payable(factory.deployWallet(DAILY_LIMIT, WINDOW, watched, sessionKey, _sessionDeadline(), trustedSpenders)));

        sendPackedUserOp = new SendPackedUserOp();

        vm.deal(address(wallet), 10 ether);
        deal(address(_tokenIn()), address(wallet), _swapAmount() * 20);

        _freshenFeeds();
    }

    /// @dev The canonical V2 router for the forked chain, read straight from Constants.s.sol rather
    ///      than from protocol config. Which venue a wallet trades on is a wallet-level choice now,
    ///      so the test suite picks it the same way an owner would.
    function _routerForChain() internal view returns (address) {
        if (block.chainid == MAINNET_CHAIN_ID) return MNT_UNISWAP_V2_ROUTER_02;
        if (block.chainid == SEPOLIA_CHAIN_ID) return SPO_UNISWAP_V2_ROUTER_02;
        if (block.chainid == BSC_CHAIN_ID) return PANCAKE_V2_ROUTER_02;
        if (block.chainid == ARB_CHAIN_ID) return ARB_UNISWAP_V2_ROUTER;
        revert("no router constant for this chain");
    }

    /// @dev Re-serves each feed's REAL current price with updatedAt = now, so a pinned fork block
    ///      can never trip the per-feed heartbeat check while keeping valuations honest.
    function _freshenFeeds() internal {
        address[] memory feeds = _feedsToFreshen();
        for (uint256 i = 0; i < feeds.length; i++) {
            if (feeds[i] == address(0)) continue;
            (uint80 roundId, int256 answer, uint256 startedAt,, uint80 answeredInRound) =
                AggregatorV3Interface(feeds[i]).latestRoundData();
            vm.mockCall(
                feeds[i],
                abi.encodeWithSelector(AggregatorV3Interface.latestRoundData.selector),
                abi.encode(roundId, answer, startedAt, block.timestamp, answeredInRound)
            );
        }
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    function _handleOps(PackedUserOperation memory userOp) internal {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        // Two-arg prank: newer EntryPoints require tx.origin == msg.sender (EOA bundler).
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    function _spentInWindow() internal view returns (int256) {
        return wallet.getConfig().spentInWindow;
    }

    /// @dev [approve router for `approveAmount`, swap `amountIn` along _swapPath()] as one batch.
    function _swapBatch(uint256 approveAmount, uint256 amountIn) internal view returns (Execution[] memory execs) {
        execs = new Execution[](2);
        execs[0] = Execution(address(_tokenIn()), 0, abi.encodeCall(IERC20.approve, (address(router), approveAmount)));
        execs[1] = Execution(
            address(router),
            0,
            abi.encodeCall(
                IUniswapV2Router01.swapExactTokensForTokens,
                (amountIn, 0, _swapPath(), address(wallet), block.timestamp + 1 hours)
            )
        );
    }

    /*//////////////////////////////////////////////////////////////
                             DEPLOYMENT
    //////////////////////////////////////////////////////////////*/

    function test_walletDeploysWithCapConfigOnFork() public view {
        assertTrue(wallet.isModuleInstalled(MODULE_TYPE_HOOK, address(module), ""));
        SpendingLimitModule.Config memory cfg = wallet.getConfig();
        assertTrue(cfg.installed);
        assertEq(cfg.dailyLimitUsd, DAILY_LIMIT);
        assertTrue(wallet.isWatched(address(_tokenIn())));
        assertTrue(wallet.isWatched(address(_tokenOut())));
        // Nothing is trusted at deploy; setUp grants the router explicitly as the owner would, so
        // unpriced-token approvals to it (LP tokens in removeLiquidity) are permitted.
        assertTrue(wallet.isTrustedSpender(address(router)), "router not trusted");
    }

    function test_agentIdentityViews_doNotRevert() public view {
        wallet.getAgentIdentity();
        wallet.getAgentId();
    }

    /*//////////////////////////////////////////////////////////////
                        NET-VALUE METERING (live oracle)
    //////////////////////////////////////////////////////////////*/

    function test_watchedTransfer_meteredAtLiveOraclePrice() public {
        uint256 amount = _swapAmount();
        int256 expectedUsd = oracle.getPrice(address(_tokenIn()), amount);
        assertGt(expectedUsd, 0, "live price should be positive");

        vm.prank(owner);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(_tokenIn()), uint256(0), abi.encodeCall(IERC20.transfer, (sink, amount)))
        );

        assertEq(_tokenIn().balanceOf(sink), amount);
        assertEq(_spentInWindow(), expectedUsd);
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT - expectedUsd);
    }

    function test_budgetExceeded_revertsOnFork() public {
        vm.prank(owner);
        wallet.setDailyLimit(1e18); // $1 cap

        uint256 amount = _swapAmount();
        int256 attempted = oracle.getPrice(address(_tokenIn()), amount);
        uint256 balanceBefore = _tokenIn().balanceOf(address(wallet));

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, attempted, int256(1e18)
            )
        );
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(_tokenIn()), uint256(0), abi.encodeCall(IERC20.transfer, (sink, amount)))
        );

        assertEq(_tokenIn().balanceOf(address(wallet)), balanceBefore, "revert was not atomic");
    }

    /// @notice A plain native-value send IS metered now: the wallet's balance drops by the sent
    ///         amount and postCheck prices that native outflow through the address(0) feed. This is
    ///         the isolated native-decrease branch (no offsetting token inflow).
    function test_nativeEthTransfer_isMetered() public {
        int256 expectedUsd = oracle.getPrice(address(0), 1 ether);
        assertGt(expectedUsd, 0, "native price should be positive");

        vm.prank(owner);
        wallet.execute(bytes32(0), abi.encodePacked(sink, uint256(1 ether), bytes("")));

        assertEq(sink.balance, 1 ether);
        assertEq(_spentInWindow(), expectedUsd, "native send not metered");
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT - expectedUsd);
    }

    /*//////////////////////////////////////////////////////////////
                  REAL-DEX SWAPS (session key, end-to-end)
    //////////////////////////////////////////////////////////////*/

    /// @notice A session-key UserOp swaps on the real router: [approve, swap] in one batch so the
    ///         approval is consumed to zero, and the metered spend must equal the oracle-priced
    ///         NET portfolio change (floored at zero) — computed from actual balance diffs.
    function test_sessionSwap_endToEnd_meteredByNetValue() public {
        uint256 amountIn = _swapAmount();
        uint256 inBefore = _tokenIn().balanceOf(address(wallet));
        uint256 outBefore = _tokenOut().balanceOf(address(wallet));

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedBatchUserOp(
            address(wallet), config, _swapBatch(amountIn, amountIn), sessionKey, sessionKeyPk
        );
        _handleOps(userOp);

        uint256 received = _tokenOut().balanceOf(address(wallet)) - outBefore;
        assertEq(_tokenIn().balanceOf(address(wallet)), inBefore - amountIn, "swap input not spent");
        assertGt(received, 0, "swap produced no output");
        assertEq(_tokenIn().allowance(address(wallet), address(router)), 0, "router approval left standing");

        int256 netUsd = oracle.getPrice(address(_tokenIn()), amountIn) - oracle.getPrice(address(_tokenOut()), received);
        int256 expected = netUsd > 0 ? netUsd : int256(0);
        assertEq(_spentInWindow(), expected, "metering != oracle-priced net portfolio change");
    }

    /// @notice The headline case for native metering: a session key spends NATIVE value via
    ///         swapExactETHForTokens (ETH/BNB -> watched token) through the real EntryPoint. The
    ///         metered spend must equal the oracle-priced net (native out - token in), floored at
    ///         zero. Because this runs through the EntryPoint, gas is really charged — yet exact-
    ///         input means the native outflow is EXACTLY the forwarded value, so an exact match
    ///         proves both that native is metered AND that gas is excluded (the ERC-4337 prefund is
    ///         taken before preCheck and the refund settles to the EntryPoint deposit after
    ///         postCheck, so neither ever lands in the metered native delta).
    function test_nativeInSwap_metersNetAndExcludesGas() public {
        uint256 ethIn = _nativeSwapValue();
        uint256 outBefore = _tokenOut().balanceOf(address(wallet));

        address[] memory path = new address[](2);
        path[0] = router.WETH(); // wrapped native (WETH/WBNB); the router wraps the forwarded value
        path[1] = address(_tokenOut()); // watched -> received amount is metered as a token inflow

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(router),
            ethIn, // forwarded as msg.value -> spent from the wallet's native balance
            abi.encodeCall(
                IUniswapV2Router01.swapExactETHForTokens, (0, path, address(wallet), block.timestamp + 1 hours)
            ),
            sessionKey,
            sessionKeyPk
        );
        _handleOps(userOp);

        uint256 received = _tokenOut().balanceOf(address(wallet)) - outBefore;
        assertGt(received, 0, "native swap produced no output");

        int256 netUsd = oracle.getPrice(address(0), ethIn) - oracle.getPrice(address(_tokenOut()), received);
        int256 expected = netUsd > 0 ? netUsd : int256(0);
        assertEq(_spentInWindow(), expected, "native swap: metering != net, or gas leaked into it");
    }

    /// @notice The native-INCREASE branch: selling a watched token for native value
    ///         (swapExactTokensForETH) makes the wallet's balance rise, and that inflow must be
    ///         credited against the token outflow. Driven owner-side (not via the EntryPoint) so the
    ///         native balance delta is exactly the ETH received — no gas prefund to confound it —
    ///         letting the metered spend be checked to the wei against the oracle-priced net,
    ///         computed from real diffs so it holds even on skewed testnet pools.
    function test_nativeOutSwap_ethInflowOffsetsOutflow() public {
        IERC20 tokenSold = _tokenOut(); // watched; sold for native
        uint256 amtSold = 10 ** IERC20Metadata(address(tokenSold)).decimals(); // 1 whole token: tiny, in-budget
        deal(address(tokenSold), address(wallet), amtSold * 1000);

        address[] memory path = new address[](2);
        path[0] = address(tokenSold);
        path[1] = router.WETH();

        uint256 ethBefore = address(wallet).balance;
        uint256 soldBalBefore = tokenSold.balanceOf(address(wallet));

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(tokenSold), 0, abi.encodeCall(IERC20.approve, (address(router), amtSold)));
        execs[1] = Execution(
            address(router),
            0,
            abi.encodeCall(
                IUniswapV2Router01.swapExactTokensForETH, (amtSold, 0, path, address(wallet), block.timestamp + 1 hours)
            )
        );
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        uint256 ethReceived = address(wallet).balance - ethBefore; // owner-direct: no gas -> exact
        uint256 sold = soldBalBefore - tokenSold.balanceOf(address(wallet));
        assertGt(ethReceived, 0, "no native received");

        int256 netUsd = oracle.getPrice(address(tokenSold), sold) - oracle.getPrice(address(0), ethReceived);
        int256 expected = netUsd > 0 ? netUsd : int256(0);
        assertEq(_spentInWindow(), expected, "native inflow not credited against token outflow");
    }

    /// @notice An approval sized above what the swap consumes leaves a residual, and the whole
    ///         transaction must revert with exactly that residual reported.
    function test_standingApprovalAfterSwap_reverts() public {
        uint256 amountIn = _swapAmount();

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_StandingApprovalNotAllowed.selector,
                address(_tokenIn()),
                address(router),
                uint256(1)
            )
        );
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(_swapBatch(amountIn + 1, amountIn)));
    }

    function test_unlimitedApprove_rejectedOnFork() public {
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_UnlimitedApprovalRejected.selector);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(
                address(_tokenIn()), uint256(0), abi.encodeCall(IERC20.approve, (address(router), type(uint256).max))
            )
        );
    }

    /*//////////////////////////////////////////////////////////////
                     GUARD + AUTH ON THE REAL ENTRYPOINT
    //////////////////////////////////////////////////////////////*/

    /// @notice A session-key UserOp aimed at the module's config reverts at execution on the real
    ///         EntryPoint; the cap survives untouched.
    function test_sessionOp_cannotTouchAdminSurface() public {
        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(module),
            0,
            abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(1_000_000_000e18))),
            sessionKey,
            sessionKeyPk
        );
        _handleOps(userOp);

        assertEq(wallet.getConfig().dailyLimitUsd, DAILY_LIMIT, "session key changed the cap on fork");
    }

    /*//////////////////////////////////////////////////////////////
        OPTION C: removeLiquidity via the trusted router (LP approval)
    //////////////////////////////////////////////////////////////*/

    /// @notice Full round-trip on a real DEX proving the trusted-spender exemption: add liquidity
    ///         (priced-token approvals), then REMOVE it — which requires approving the pool's
    ///         UNPRICED LP token to the router. That approval is legal only because initialize()
    ///         trusts the router (granted by the owner in setUp); the no-standing-approval rule still forces it to zero within
    ///         the same transaction. Driven owner-side so an illegal LP approval surfaces as a direct
    ///         revert (SpendingLimitModule_TokenNotPriced) rather than being absorbed by the EntryPoint.
    function test_removeLiquidity_roundTrip_viaTrustedRouter() public {
        IERC20 tokenIn = _tokenIn();
        IERC20 tokenOut = _tokenOut();
        uint256 amtIn = _swapAmount();

        // Fund both sides so the pool ratio, not our balance, bounds addLiquidity. The V2 router
        // creates the pair if this network has no direct tokenIn/tokenOut pool.
        deal(address(tokenIn), address(wallet), amtIn * 4);
        deal(address(tokenOut), address(wallet), amtIn * 1_000_000);
        uint256 tokenOutBudget = tokenOut.balanceOf(address(wallet)) / 2;

        // 1. addLiquidity batch: approve both priced tokens, add, then zero any residual allowance.
        Execution[] memory add = new Execution[](5);
        add[0] = Execution(address(tokenIn), 0, abi.encodeCall(IERC20.approve, (address(router), amtIn)));
        add[1] = Execution(address(tokenOut), 0, abi.encodeCall(IERC20.approve, (address(router), tokenOutBudget)));
        add[2] = Execution(
            address(router),
            0,
            abi.encodeCall(
                IUniswapV2Router01.addLiquidity,
                (
                    address(tokenIn),
                    address(tokenOut),
                    amtIn,
                    tokenOutBudget,
                    0,
                    0,
                    address(wallet),
                    block.timestamp + 1 hours
                )
            )
        );
        add[3] = Execution(address(tokenIn), 0, abi.encodeCall(IERC20.approve, (address(router), 0)));
        add[4] = Execution(address(tokenOut), 0, abi.encodeCall(IERC20.approve, (address(router), 0)));
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(add));

        address pair = IUniswapV2Factory(router.factory()).getPair(address(tokenIn), address(tokenOut));
        uint256 lpBal = IERC20(pair).balanceOf(address(wallet));
        assertGt(lpBal, 0, "no LP tokens minted");

        uint256 inBefore = tokenIn.balanceOf(address(wallet));
        uint256 outBefore = tokenOut.balanceOf(address(wallet));

        // 2. removeLiquidity batch: approve the UNPRICED LP token to the router (allowed only because
        //    the router is trusted), then burn it. removeLiquidity pulls exactly lpBal -> residual 0.
        Execution[] memory rem = new Execution[](2);
        rem[0] = Execution(pair, 0, abi.encodeCall(IERC20.approve, (address(router), lpBal)));
        rem[1] = Execution(
            address(router),
            0,
            abi.encodeCall(
                IUniswapV2Router01.removeLiquidity,
                (address(tokenIn), address(tokenOut), lpBal, 0, 0, address(wallet), block.timestamp + 1 hours)
            )
        );
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(rem));

        assertEq(IERC20(pair).balanceOf(address(wallet)), 0, "LP tokens not burned");
        assertGt(tokenIn.balanceOf(address(wallet)), inBefore, "tokenIn not returned");
        assertGt(tokenOut.balanceOf(address(wallet)), outBefore, "tokenOut not returned");
        assertEq(IERC20(pair).allowance(address(wallet), address(router)), 0, "LP approval left standing");
    }

    /// @notice A revoked session key fails validation (AA24) on the real EntryPoint.
    function test_revokedSessionKey_failsValidation() public {
        vm.prank(owner);
        wallet.removeSession();

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(_tokenIn()),
            0,
            abi.encodeCall(IERC20.transfer, (sink, 1)),
            sessionKey,
            sessionKeyPk
        );

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }
}
