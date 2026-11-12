// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {VmSafe} from "forge-std/Vm.sol";
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
import {RecurringPaymentModule} from "../../src/RecurringPaymentModule.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import "../../script/Constants.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {configuredTokens} from "../utils/ConfiguredTokens.sol";
import {PasskeyHelper} from "../utils/PasskeyHelper.sol";

/**
 * @title SHForkTestBase
 * @notice Shared fork-test base for the USD-spending-cap design, parameterized per network by a
 *         swap path and feed list. Each network file (Uniswap V3 on mainnet, Sepolia, Arbitrum and
 *         Base; PancakeSwap V2 on BSC) supplies live token/pool specifics; every test here runs
 *         identically against the real router, real Chainlink feeds, and the real (canonical)
 *         EntryPoint. Three things depend on the venue, and each is a virtual hook with its V2
 *         version here: the swap calldata, the spenders the wallet trusts ({_trustedSpenders}), and
 *         the liquidity round-trip ({_liquidityRoundTrip}). SHUniswapV3ForkTestBase overrides all
 *         three for V3; the BSC suite keeps them as they are.
 * @dev Metering assertions are computed from ACTUAL balance diffs priced through the deployed
 *      oracle, never from hardcoded dollar figures — so they hold regardless of pool ratios
 *      (testnet pools price independently of Chainlink; see project memory). Feeds are re-served
 *      at their real prices with a fresh timestamp (vm.mockCall) to remove staleness flake on
 *      pinned forks without distorting any valuation.
 */
abstract contract SHForkTestBase is Test {
    using PasskeyHelper for PasskeyHelper.Passkey;

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
    /// @dev The router the swap tests trade through: Uniswap V3's SwapRouter02, or PancakeSwap's V2
    ///      router on BSC. See {_swapRouterForChain}.
    address swapRouter;
    /// @dev The chain's V2 router. On BSC it is the swap router, the liquidity router and the wallet's
    ///      trusted spender. The V3 suites resolve it too (setUp is shared) but trade, add liquidity
    ///      and trust elsewhere, so nothing there touches it.
    IUniswapV2Router01 v2Router;

    int256 constant DAILY_LIMIT = 100_000e18; // $100k window cap: roomy enough for real-pool swaps
    uint256 constant WINDOW = 1 days;

    address owner;
    /// @dev The wallet's session key: a soft passkey, seeded through deployWallet in setUp. The fork
    ///      suites sign with it the way the app's users do, through the REAL EntryPoint.
    PasskeyHelper.Passkey sessionKey;
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

    /// @dev This chain's listed tokens that really have increaseApproval. None by default.
    function _tokensWithIncreaseApproval() internal view virtual returns (address[] memory tokens) {}

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
        sessionKey = PasskeyHelper.create("sessionKey");

        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        feeRegistry = treasury.REGISTRY();
        module = SpendingLimitModule(feeRegistry.spendingLimitModule());
        owner = config.account;
        v2Router = IUniswapV2Router01(_v2RouterForChain());
        swapRouter = _swapRouterForChain();

        address[] memory watched = new address[](2);
        watched[0] = address(_tokenIn());
        watched[1] = address(_tokenOut());

        // Deploy exactly as app/deploy_wallet.py does in production: the session key and the chain's
        // REAL default spenders ({_trustedSpenders}) are seeded through deployWallet, not granted by
        // follow-up owner calls.
        //
        // This is deliberately the one suite that covers the seeded path against live routers. The
        // unit tests only ever seed a makeAddr() spender against an ERC20Mock, and if setUp here
        // re-granted the spenders with addTrustedSpender afterwards (as it used to), a regression in
        // deploy-time seeding would be masked on every fork — the second grant would quietly repair
        // it. On BSC removeLiquidity's unpriced LP-token approval would pass either way; on the V3
        // networks nothing else in the suite depends on the grants at all (every approval there is
        // for a priced token), so test_walletDeploysWithCapConfigOnFork is what proves they landed.
        //
        // The spenders are still not protocol configuration: they are passed in by the caller,
        // exactly as constants.get_default_spenders(chain_id) is passed in by the app.
        address[] memory trustedSpenders = _trustedSpenders();

        vm.prank(owner);
        wallet = SessionHandler(
            payable(factory.deployWallet(
                    DAILY_LIMIT, WINDOW, watched, true, sessionKey.qx, sessionKey.qy, _sessionDeadline(), trustedSpenders
                ))
        );

        sendPackedUserOp = new SendPackedUserOp();

        vm.deal(address(wallet), 10 ether);
        deal(address(_tokenIn()), address(wallet), _swapAmount() * 20);

        _freshenFeeds();
    }

    /// @dev The canonical V2 router for the forked chain, read straight from Constants.s.sol rather
    ///      than from protocol config. Which venue a wallet trades on is a wallet-level choice now,
    ///      so the test suite picks it the same way an owner would.
    function _v2RouterForChain() internal view returns (address) {
        if (block.chainid == MAINNET_CHAIN_ID) return MNT_UNISWAP_V2_ROUTER_02;
        if (block.chainid == SEPOLIA_CHAIN_ID) return SPO_UNISWAP_V2_ROUTER_02;
        if (block.chainid == BSC_CHAIN_ID) return PANCAKE_V2_ROUTER_02;
        if (block.chainid == ARB_CHAIN_ID) return ARB_UNISWAP_V2_ROUTER;
        if (block.chainid == BASE_CHAIN_ID) return BASE_UNISWAP_V2_ROUTER;
        revert("no router constant for this chain");
    }

    /*//////////////////////////////////////////////////////////////
          VENUE HOOKS (V2 here; V3 suites override them)
    //////////////////////////////////////////////////////////////*/

    /// @dev The spenders deployWallet trusts, in the same shape the app seeds
    ///      constants.get_default_spenders(chain_id): the V2 router here (PancakeSwap's, on BSC);
    ///      [SwapRouter02, position manager] on the V3 networks. setUp calls it after v2Router and
    ///      swapRouter are set, so an override may read either, and
    ///      test_walletDeploysWithCapConfigOnFork asserts that every entry ended up trusted.
    function _trustedSpenders() internal view virtual returns (address[] memory spenders) {
        spenders = new address[](1);
        spenders[0] = address(v2Router);
    }

    /// @dev The router the swap tests trade through. Set after v2Router in setUp.
    function _swapRouterForChain() internal view virtual returns (address) {
        return address(v2Router);
    }

    /// @dev Sells exactly `amountIn` of _tokenIn() along _swapPath(), output to the wallet, no minimum.
    function _swapCalldata(uint256 amountIn) internal view virtual returns (bytes memory) {
        return abi.encodeCall(
            IUniswapV2Router01.swapExactTokensForTokens,
            (amountIn, 0, _swapPath(), address(wallet), block.timestamp + 1 hours)
        );
    }

    /// @dev Sells exactly the forwarded native value for _tokenOut(), output to the wallet.
    function _nativeInSwapCalldata(uint256 /* value */ ) internal view virtual returns (bytes memory) {
        address[] memory path = new address[](2);
        path[0] = v2Router.WETH(); // wrapped native (WETH/WBNB); the router wraps the forwarded value
        path[1] = address(_tokenOut()); // watched -> received amount is metered as a token inflow
        return abi.encodeCall(
            IUniswapV2Router01.swapExactETHForTokens, (0, path, address(wallet), block.timestamp + 1 hours)
        );
    }

    /// @dev Sells exactly `amount` of `token` for native value, paid to the wallet.
    function _nativeOutSwapCalldata(address token, uint256 amount) internal view virtual returns (bytes memory) {
        address[] memory path = new address[](2);
        path[0] = token;
        path[1] = v2Router.WETH();
        return abi.encodeCall(
            IUniswapV2Router01.swapExactTokensForETH, (amount, 0, path, address(wallet), block.timestamp + 1 hours)
        );
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

    /// @dev The nonce key every passkey op here uses. Its op deadline is later than any key's, so the
    ///      key's own deadline is the window; PasskeyValidationTest covers the op deadline itself.
    function _passkeyNonceKey() internal pure returns (uint192) {
        return PasskeyHelper.nonceKey(PasskeyHelper.LATEST_OP_DEADLINE, 0);
    }

    /// @dev A single-call UserOp from the wallet, approved by the session passkey.
    function _sessionOp(address dest, uint256 value, bytes memory data)
        internal
        view
        returns (PackedUserOperation memory userOp)
    {
        bytes32 hash;
        (userOp, hash) =
            sendPackedUserOp.generateUnsignedUserOp(address(wallet), config, dest, value, data, _passkeyNonceKey());
        userOp.signature = sessionKey.sign(hash);
    }

    /// @dev A batch UserOp from the wallet, approved by the session passkey.
    function _sessionBatchOp(Execution[] memory execs) internal view returns (PackedUserOperation memory userOp) {
        bytes32 hash;
        (userOp, hash) =
            sendPackedUserOp.generateUnsignedBatchUserOp(address(wallet), config, execs, _passkeyNonceKey());
        userOp.signature = sessionKey.sign(hash);
    }

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
        execs[0] = Execution(address(_tokenIn()), 0, abi.encodeCall(IERC20.approve, (swapRouter, approveAmount)));
        execs[1] = Execution(swapRouter, 0, _swapCalldata(amountIn));
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
        // deployWallet seeds every spender in _trustedSpenders() (the V2 router on BSC; SwapRouter02
        // and the position manager on the V3 networks), so unpriced-token approvals to any of them
        // are permitted. Each one is checked on its own, so a grant that was dropped cannot hide
        // behind the others that were not.
        address[] memory spenders = _trustedSpenders();
        assertGt(spenders.length, 0, "no trusted spenders to check");
        for (uint256 i = 0; i < spenders.length; i++) {
            assertTrue(
                wallet.isTrustedSpender(spenders[i]), string.concat("spender not trusted: ", vm.toString(spenders[i]))
            );
        }
    }

    /// @dev SpendingLimitModule._isPlainSend reads only the sent token on the strength of this: a
    ///      priced token's `transfer` runs no outside code, so it can move nothing but itself. A new
    ///      token failing here must not stay priced without changing that function. Both ends have
    ///      code because a token that treats contracts differently would only show it then.
    function test_everyConfiguredToken_transferCallsNothingOutside() public {
        address[22] memory configured = configuredTokens(config);
        address sender = makeAddr("contractSender");
        address recipient = makeAddr("contractRecipient");
        vm.etch(sender, hex"00");
        vm.etch(recipient, hex"00");
        for (uint256 i = 0; i < configured.length; i++) {
            address token = configured[i];
            if (token == address(0)) continue;
            uint256 oneToken = 10 ** IERC20Metadata(token).decimals();
            deal(token, sender, oneToken);

            vm.startStateDiffRecording();
            vm.prank(sender);
            // Low-level: some of these (mainnet USDT, BNB) return nothing from transfer.
            (bool ok,) = token.call(abi.encodeCall(IERC20.transfer, (recipient, oneToken)));
            VmSafe.AccountAccess[] memory accesses = vm.stopAndReturnStateDiff();

            assertTrue(ok, vm.toString(token));
            for (uint256 j = 0; j < accesses.length; j++) {
                assertTrue(accesses[j].account != recipient, string.concat("recipient touched by ", vm.toString(token)));
                // A proxy may read its beacon and delegatecall its implementation; those run as the token.
                bool changesState = accesses[j].kind == VmSafe.AccountAccessKind.Call
                    || accesses[j].kind == VmSafe.AccountAccessKind.CallCode
                    || accesses[j].kind == VmSafe.AccountAccessKind.Create;
                assertFalse(
                    changesState && accesses[j].account != token,
                    string.concat("outside call from ", vm.toString(token))
                );
            }
        }
    }

    /// @dev Every token this chain's config names gets a live price. The unit suite proves each one
    ///      reaches the oracle, but only against mocks; this is where a feed Chainlink has shut down
    ///      shows up (a proxy whose aggregator is address(0) reverts on every read). A token with a
    ///      dead feed must be zeroed in HelperConfig, feed and token together, and left out of the
    ///      app's token list, as BSC's YFI was: registered, it deploys fine and then reverts every
    ///      execution that has to value it.
    ///      Unfreshened feeds are read as they are: at the latest block each is inside its heartbeat.
    function test_everyConfiguredTokenPricesOnFork() public view {
        address[22] memory configured = configuredTokens(config);
        for (uint256 i = 0; i < configured.length; i++) {
            address token = configured[i];
            if (token == address(0)) continue;
            uint256 oneToken = 10 ** IERC20Metadata(token).decimals();
            try oracle.getPrice(token, oneToken) returns (int256 usd) {
                assertGt(usd, 0, vm.toString(token));
            } catch {
                revert(string.concat("no live price for configured token ", vm.toString(token)));
            }
        }
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

    /// @notice A recurring payment of a real token: the keeper's run() pays the payee, the hook charges
    ///         the cap at the oracle's price, and the treasury gets the protocol fee plus the keeper
    ///         fee. A second run in the same period pays nothing.
    function test_recurringPayment_paysARealTokenThroughTheHook() public {
        RecurringPaymentModule recurring = RecurringPaymentModule(feeRegistry.recurringPaymentModule());
        assertTrue(wallet.isModuleInstalled(2, address(recurring), ""), "wallet deployed without the executor");
        uint256 amount = _swapAmount();
        int256 expectedUsd = oracle.getPrice(address(_tokenIn()), amount);

        vm.prank(owner);
        uint256 id = recurring.createSchedule(address(wallet), address(_tokenIn()), sink, amount, 30 days, 0, 0);

        uint256 sinkBefore = _tokenIn().balanceOf(sink);
        uint256 treasuryBefore = address(treasury).balance;
        vm.prank(makeAddr("keeper"));
        recurring.run(address(wallet), id);

        assertEq(_tokenIn().balanceOf(sink), sinkBefore + amount, "payee not paid");
        assertEq(_spentInWindow(), expectedUsd, "not metered at the oracle price");
        assertEq(
            address(treasury).balance,
            treasuryBefore + feeRegistry.getFee() + feeRegistry.keeperFee(),
            "treasury did not get both fees"
        );

        vm.expectRevert(
            abi.encodeWithSelector(RecurringPaymentModule.RecurringPayment_NotDue.selector, block.timestamp + 30 days)
        );
        recurring.run(address(wallet), id);
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

        PackedUserOperation memory userOp = _sessionBatchOp(_swapBatch(amountIn, amountIn));
        _handleOps(userOp);

        uint256 received = _tokenOut().balanceOf(address(wallet)) - outBefore;
        assertEq(_tokenIn().balanceOf(address(wallet)), inBefore - amountIn, "swap input not spent");
        assertGt(received, 0, "swap produced no output");
        assertEq(_tokenIn().allowance(address(wallet), swapRouter), 0, "router approval left standing");

        int256 netUsd = oracle.getPrice(address(_tokenIn()), amountIn) - oracle.getPrice(address(_tokenOut()), received);
        int256 expected = netUsd > 0 ? netUsd : int256(0);
        assertEq(_spentInWindow(), expected, "metering != oracle-priced net portfolio change");
    }

    /// @notice The headline case for native metering: a session key spends NATIVE value via
    ///         an exact-input native swap (ETH/BNB -> watched token) through the real EntryPoint. The
    ///         metered spend must equal the oracle-priced net (native out - token in), floored at
    ///         zero. Because this runs through the EntryPoint, gas is really charged — yet exact-
    ///         input means the native outflow is EXACTLY the forwarded value, so an exact match
    ///         proves both that native is metered AND that gas is excluded (the ERC-4337 prefund is
    ///         taken before preCheck and the refund settles to the EntryPoint deposit after
    ///         postCheck, so neither ever lands in the metered native delta).
    function test_nativeInSwap_metersNetAndExcludesGas() public {
        uint256 ethIn = _nativeSwapValue();
        uint256 outBefore = _tokenOut().balanceOf(address(wallet));

        PackedUserOperation memory userOp = _sessionOp(
            swapRouter,
            ethIn, // forwarded as msg.value -> spent from the wallet's native balance
            _nativeInSwapCalldata(ethIn)
        );
        _handleOps(userOp);

        uint256 received = _tokenOut().balanceOf(address(wallet)) - outBefore;
        assertGt(received, 0, "native swap produced no output");

        int256 netUsd = oracle.getPrice(address(0), ethIn) - oracle.getPrice(address(_tokenOut()), received);
        int256 expected = netUsd > 0 ? netUsd : int256(0);
        assertEq(_spentInWindow(), expected, "native swap: metering != net, or gas leaked into it");
    }

    /// @notice The native-INCREASE branch: selling a watched token for native value
    ///         (an exact-input swap paid out in native) makes the wallet's balance rise, and that inflow must be
    ///         credited against the token outflow. Driven owner-side (not via the EntryPoint) so the
    ///         native balance delta is exactly the ETH received — no gas prefund to confound it —
    ///         letting the metered spend be checked to the wei against the oracle-priced net,
    ///         computed from real diffs so it holds even on skewed testnet pools.
    function test_nativeOutSwap_ethInflowOffsetsOutflow() public {
        IERC20 tokenSold = _tokenOut(); // watched; sold for native
        uint256 amtSold = 10 ** IERC20Metadata(address(tokenSold)).decimals(); // 1 whole token: tiny, in-budget
        deal(address(tokenSold), address(wallet), amtSold * 1000);

        uint256 ethBefore = address(wallet).balance;
        uint256 soldBalBefore = tokenSold.balanceOf(address(wallet));

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(tokenSold), 0, abi.encodeCall(IERC20.approve, (swapRouter, amtSold)));
        execs[1] = Execution(swapRouter, 0, _nativeOutSwapCalldata(address(tokenSold), amtSold));
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        uint256 ethReceived = address(wallet).balance - ethBefore; // owner-direct: no gas -> exact
        uint256 sold = soldBalBefore - tokenSold.balanceOf(address(wallet));
        assertGt(ethReceived, 0, "no native received");

        int256 netUsd = oracle.getPrice(address(tokenSold), sold) - oracle.getPrice(address(0), ethReceived);
        int256 expected = netUsd > 0 ? netUsd : int256(0);
        assertEq(_spentInWindow(), expected, "native inflow not credited against token outflow");
    }

    /// @notice The same sale on a wallet with native metering OFF is charged the FULL value sold: the
    ///         native that comes back offsets nothing. This is what stops a watched token being turned
    ///         into native at no charge and the native then sent out unmetered.
    function test_nativeUnmetered_sellingForNativeChargesTheFullAmount() public {
        vm.prank(owner);
        wallet.setNativeMetering(false, address(0));

        IERC20 tokenSold = _tokenOut(); // watched; sold for native
        uint256 amtSold = 10 ** IERC20Metadata(address(tokenSold)).decimals();
        deal(address(tokenSold), address(wallet), amtSold * 1000);

        uint256 ethBefore = address(wallet).balance;
        uint256 soldBalBefore = tokenSold.balanceOf(address(wallet));

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(tokenSold), 0, abi.encodeCall(IERC20.approve, (swapRouter, amtSold)));
        execs[1] = Execution(swapRouter, 0, _nativeOutSwapCalldata(address(tokenSold), amtSold));
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        uint256 sold = soldBalBefore - tokenSold.balanceOf(address(wallet));
        assertGt(address(wallet).balance, ethBefore, "no native received");
        assertEq(_spentInWindow(), oracle.getPrice(address(tokenSold), sold), "a native inflow earned credit");
    }

    /// @notice With native metering OFF, native spent by a session key is not charged: neither a
    ///         native-in swap on the real router through the real EntryPoint, nor a plain send. The
    ///         watched token bought is an inflow, which never adds to the tally.
    function test_nativeUnmetered_nativeSpendIsNotCharged() public {
        vm.prank(owner);
        wallet.setNativeMetering(false, address(0));

        uint256 ethIn = _nativeSwapValue();
        uint256 outBefore = _tokenOut().balanceOf(address(wallet));
        PackedUserOperation memory userOp = _sessionOp(swapRouter, ethIn, _nativeInSwapCalldata(ethIn));
        _handleOps(userOp);

        assertGt(_tokenOut().balanceOf(address(wallet)) - outBefore, 0, "native swap produced no output");
        assertEq(_spentInWindow(), 0, "native was charged while unmetered");

        vm.prank(owner);
        wallet.execute(bytes32(0), abi.encodePacked(sink, uint256(1 ether), bytes("")));
        assertEq(sink.balance, 1 ether);
        assertEq(_spentInWindow(), 0);
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT);
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
                swapRouter,
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
                address(_tokenIn()), uint256(0), abi.encodeCall(IERC20.approve, (swapRouter, type(uint256).max))
            )
        );
    }

    /// @notice No function on any token this chain lists may leave a spender able to pull the wallet's
    ///         tokens once the transaction is over: such a pull happens outside any wallet execution,
    ///         so the hook never sees it and the cap never counts it.
    /// @dev The list is every allowance-granting function found on the listed tokens by reading their
    ///      bytecode, proxies followed (2026-11-05). Signatures are spelled out rather than taken from
    ///      IERC20Extended, so a typo there fails here. Tokens without a function just revert, which
    ///      passes. Every standing allowance is collected before failing, so one run names them all.
    ///      A token added to the oracle needs its functions checked the same way (add-network skill).
    function test_everyConfiguredToken_noCallLeavesAStandingAllowance() public {
        address spender = makeAddr("standingSpender");
        uint256 amount = 1e18;
        // Sandbox's call-forwarding functions require the data's first argument to be the caller, and
        // the data to be at least two words long.
        bytes memory forwarded = abi.encodeWithSelector(bytes4(0xdeadbeef), address(wallet), uint256(0));

        bytes[] memory grants = new bytes[](7);
        grants[0] = abi.encodeWithSignature("approve(address,uint256)", spender, amount);
        grants[1] = abi.encodeWithSignature("increaseAllowance(address,uint256)", spender, amount);
        grants[2] = abi.encodeWithSignature("increaseApproval(address,uint256)", spender, amount);
        grants[3] = abi.encodeWithSignature("approveFor(address,address,uint256)", address(wallet), spender, amount);
        grants[4] =
            abi.encodeWithSignature("addAllowanceIfNeeded(address,address,uint256)", address(wallet), spender, amount);
        grants[5] = abi.encodeWithSignature("approveAndCall(address,uint256,bytes)", spender, amount, forwarded);
        grants[6] = abi.encodeWithSignature("paidCall(address,uint256,bytes)", spender, amount, forwarded);

        string memory standing;
        address[22] memory configured = configuredTokens(config);
        for (uint256 i = 0; i < configured.length; i++) {
            address token = configured[i];
            if (token == address(0)) continue;
            for (uint256 j = 0; j < grants.length; j++) {
                // Each call starts clean, or one standing allowance would blame every call after it.
                uint256 clean = vm.snapshotState();
                vm.prank(owner);
                try wallet.execute(bytes32(0), abi.encodePacked(token, uint256(0), grants[j])) {} catch {}
                if (IERC20(token).allowance(address(wallet), spender) != 0) {
                    standing = string.concat(standing, " ", IERC20Metadata(token).symbol(), ":", vm.toString(j));
                }
                vm.revertToState(clean);
            }
        }
        assertEq(bytes(standing).length, 0, string.concat("token:grant index left standing:", standing));
    }

    /// @notice The tokens that really have increaseApproval hit the standing-approval rule, exact residual
    ///         and all, so the test above can't pass just because a token lacks the function.
    function test_increaseApproval_onRealTokens_revertsAsStanding() public {
        address spender = makeAddr("standingSpender");
        address[] memory tokens = _tokensWithIncreaseApproval();
        for (uint256 i = 0; i < tokens.length; i++) {
            vm.prank(owner);
            vm.expectRevert(
                abi.encodeWithSelector(
                    SpendingLimitModule.SpendingLimitModule_StandingApprovalNotAllowed.selector,
                    tokens[i],
                    spender,
                    uint256(1e18)
                )
            );
            wallet.execute(
                bytes32(0),
                abi.encodePacked(
                    tokens[i],
                    uint256(0),
                    abi.encodeWithSignature("increaseApproval(address,uint256)", spender, uint256(1e18))
                )
            );
        }
    }

    /// @notice The same refusal for a session key on the real EntryPoint. The op lands and its inner
    ///         call fails, so the allowance is what shows whether it worked.
    function test_sessionOp_increaseApprovalLeavesNoAllowance() public {
        address spender = makeAddr("standingSpender");
        _handleOps(
            _sessionOp(config.link, 0, abi.encodeWithSignature("increaseApproval(address,uint256)", spender, 1e18))
        );
        assertEq(IERC20(config.link).allowance(address(wallet), spender), 0, "allowance left standing");
    }

    /*//////////////////////////////////////////////////////////////
                  TOKEN CAPS, ON REAL TOKENS AND POOLS
    //////////////////////////////////////////////////////////////*/

    /// @dev Moves `token` from the USD limit to a cap in its own units, as an owner would.
    function _capInItsOwnUnits(address token, uint256 limit) internal {
        vm.startPrank(owner);
        wallet.removeWatchedToken(token);
        wallet.setTokenCap(token, limit);
        vm.stopPrank();
    }

    function _capRemaining(address token) internal view returns (uint256 remaining) {
        (,, remaining) = wallet.getTokenCap(token);
    }

    /// @notice Buying a token capped in its own units on the real pool: the full amount paid counts
    ///         against the USD limit, since the token coming in isn't valued, and its own cap is untouched.
    function test_tokenCap_buyingACappedToken_chargesTheUsdLimitInFull() public {
        address capped = address(_tokenOut());
        _capInItsOwnUnits(capped, 1);
        uint256 amountIn = _swapAmount();
        uint256 balanceBefore = IERC20(capped).balanceOf(address(wallet));

        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(_swapBatch(amountIn, amountIn)));

        assertGt(IERC20(capped).balanceOf(address(wallet)), balanceBefore, "the swap bought nothing");
        assertEq(_spentInWindow(), oracle.getPrice(address(_tokenIn()), amountIn), "not charged in full");
        assertEq(_capRemaining(capped), 1, "buying used the token's own cap");
    }

    /// @notice Selling a capped token on the real pool charges its cap by the amount sold and leaves the
    ///         USD limit alone. Past the cap, the whole trade reverts.
    function test_tokenCap_sellingACappedToken_chargesItsOwnCapOnly() public {
        address capped = address(_tokenOut());
        uint256 oneToken = 10 ** IERC20Metadata(capped).decimals();
        _capInItsOwnUnits(capped, oneToken * 3 / 2);
        deal(capped, address(wallet), oneToken * 10);
        uint256 nativeBefore = address(wallet).balance;

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(capped, 0, abi.encodeCall(IERC20.approve, (swapRouter, oneToken)));
        execs[1] = Execution(swapRouter, 0, _nativeOutSwapCalldata(capped, oneToken));
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        assertGt(address(wallet).balance, nativeBefore, "the swap paid no native");
        assertEq(_capRemaining(capped), oneToken / 2);
        assertEq(_spentInWindow(), 0, "selling charged the USD limit");

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_TokenCapExceeded.selector,
                capped,
                oneToken * 2,
                oneToken * 3 / 2
            )
        );
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));
    }

    /// @notice A session key on the real EntryPoint is held to the cap on a real token, and a fresh window
    ///         gives the cap back. LINK stands in for any token with a feed the owner chose to cap this way.
    function test_tokenCap_sessionKeyIsHeldToItOnRealLink() public {
        address link = config.link;
        uint256 limit = 100e18;
        _capInItsOwnUnits(link, limit);
        deal(link, address(wallet), 1000e18);

        _handleOps(_sessionOp(link, 0, abi.encodeCall(IERC20.transfer, (sink, limit + 1))));
        assertEq(IERC20(link).balanceOf(address(wallet)), 1000e18, "went past the cap");

        _handleOps(_sessionOp(link, 0, abi.encodeCall(IERC20.transfer, (sink, limit))));
        assertEq(IERC20(link).balanceOf(sink), limit, "couldn't spend up to the cap");
        assertEq(_capRemaining(link), 0);
        assertEq(_spentInWindow(), 0, "a capped token charged the USD limit");

        vm.warp(block.timestamp + WINDOW);
        assertEq(_capRemaining(link), limit, "a fresh window didn't give the cap back");
    }

    /// @notice LINK has transferAndCall and more besides. Capped in its own units, only transfer and the
    ///         known grants may be aimed at it, whatever the real token offers.
    function test_tokenCap_refusesOtherCallsOnRealLink() public {
        address link = config.link;
        _capInItsOwnUnits(link, 100e18);
        bytes memory call = abi.encodeWithSignature("transferAndCall(address,uint256,bytes)", sink, 1e18, bytes(""));

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_CallNotAllowedOnCappedToken.selector, link, bytes4(call)
            )
        );
        wallet.execute(bytes32(0), abi.encodePacked(link, uint256(0), call));
    }

    /*//////////////////////////////////////////////////////////////
                     GUARD + AUTH ON THE REAL ENTRYPOINT
    //////////////////////////////////////////////////////////////*/

    /// @notice A session-key UserOp aimed at the module's config reverts at execution on the real
    ///         EntryPoint; the cap survives untouched.
    function test_sessionOp_cannotTouchAdminSurface() public {
        PackedUserOperation memory userOp = _sessionOp(
            address(module), 0, abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(1_000_000_000e18)))
        );
        _handleOps(userOp);

        assertEq(wallet.getConfig().dailyLimitUsd, DAILY_LIMIT, "session key changed the cap on fork");
    }

    /*//////////////////////////////////////////////////////////////
           LIQUIDITY ROUND-TRIP (the body depends on the venue)
    //////////////////////////////////////////////////////////////*/

    /// @notice Adds liquidity on this network's real DEX and takes all of it out again, with the
    ///         spending cap watching. The test is the same on every network and its body is not:
    ///         {_liquidityRoundTrip} is the V2 version here (the BSC suite runs it) and a V3
    ///         position in SHUniswapV3ForkTestBase.
    function test_liquidityRoundTrip() public {
        _liquidityRoundTrip();
    }

    /// @dev The V2 round-trip, proving the trusted-spender exemption on a real DEX: add liquidity
    ///      (priced-token approvals), then REMOVE it — which requires approving the pool's UNPRICED
    ///      LP token to the router. That approval is legal only because deployWallet seeded the router
    ///      as a trusted spender (see setUp); the no-standing-approval rule still forces it to zero
    ///      within the same transaction. Driven owner-side so an illegal LP approval surfaces as a
    ///      direct revert (SpendingLimitModule_TokenNotPriced) rather than being absorbed by the
    ///      EntryPoint. The V3 networks override this: a V3 position is an NFT the wallet owns, so
    ///      taking liquidity out needs no approval at all.
    function _liquidityRoundTrip() internal virtual {
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
        add[0] = Execution(address(tokenIn), 0, abi.encodeCall(IERC20.approve, (address(v2Router), amtIn)));
        add[1] = Execution(address(tokenOut), 0, abi.encodeCall(IERC20.approve, (address(v2Router), tokenOutBudget)));
        add[2] = Execution(
            address(v2Router),
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
        add[3] = Execution(address(tokenIn), 0, abi.encodeCall(IERC20.approve, (address(v2Router), 0)));
        add[4] = Execution(address(tokenOut), 0, abi.encodeCall(IERC20.approve, (address(v2Router), 0)));
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(add));

        address pair = IUniswapV2Factory(v2Router.factory()).getPair(address(tokenIn), address(tokenOut));
        uint256 lpBal = IERC20(pair).balanceOf(address(wallet));
        assertGt(lpBal, 0, "no LP tokens minted");

        uint256 inBefore = tokenIn.balanceOf(address(wallet));
        uint256 outBefore = tokenOut.balanceOf(address(wallet));

        // 2. removeLiquidity batch: approve the UNPRICED LP token to the router (allowed only because
        //    the router is trusted), then burn it. removeLiquidity pulls exactly lpBal -> residual 0.
        Execution[] memory rem = new Execution[](2);
        rem[0] = Execution(pair, 0, abi.encodeCall(IERC20.approve, (address(v2Router), lpBal)));
        rem[1] = Execution(
            address(v2Router),
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
        assertEq(IERC20(pair).allowance(address(wallet), address(v2Router)), 0, "LP approval left standing");
    }

    /// @notice A revoked session key fails validation (AA24) on the real EntryPoint.
    function test_revokedSessionKey_failsValidation() public {
        vm.prank(owner);
        wallet.removeSession(sessionKey.keyId());

        PackedUserOperation memory userOp = _sessionOp(address(_tokenIn()), 0, abi.encodeCall(IERC20.transfer, (sink, 1)));

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }
}
