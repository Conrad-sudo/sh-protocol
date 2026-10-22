//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
//Openzeppelin Imports
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {
    IERC7579Execution,
    Execution,
    MODULE_TYPE_VALIDATOR,
    MODULE_TYPE_EXECUTOR,
    MODULE_TYPE_FALLBACK,
    MODULE_TYPE_HOOK
} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {P256} from "@openzeppelin/contracts/utils/cryptography/P256.sol";
//Local imports
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {configuredTokens} from "../utils/ConfiguredTokens.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockWeth} from "../../src/mocks/MockWeth.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SpendingLimitModuleHarness} from "./SpendingLimitModuleHarness.sol";
import {PasskeyHelper} from "../utils/PasskeyHelper.sol";

/**
 * @title SHProtocolTest
 * @author Conrad Japhet
 * @notice Unit suite for the SessionHandler protocol under the USD-spending-cap design:
 *         SessionHandler (ERC-7579 account, validates its own UserOps, session-key allowlist,
 *         admin-surface guard) + SpendingLimitModule (hook type 4 ONLY, net-value metering +
 *         no-standing-approvals) + SHOracle (Chainlink pricing with per-feed heartbeats).
 * @dev Shape notes worth knowing up front:
 *      - execute() takes (bytes32 mode, bytes executionCalldata), the standard ERC-7579 shape;
 *        single-call payloads are abi.encodePacked(target, value, data), batches are
 *        ERC7579Utils.encodeBatch(Execution[]).
 *      - The hook wraps _execute for EVERY caller, so owner-direct execute() calls are metered
 *        by the spending cap exactly like session-key UserOps -- most metering tests below drive
 *        execute() as the owner for brevity. (The EntryPoint flow is exercised end-to-end in
 *        SessionGuardTest.t.sol.)
 *      - onlyEntryPointOrSelfOrOwner reverts with OZAccount.AccountUnauthorized;
 *        onlyOwner passthroughs revert with Ownable.OwnableUnauthorizedAccount -- two different
 *        errors depending on which function you're testing.
 *      - Module errors carry the SpendingLimitModule_ prefix; oracle errors the PriceOracle_ prefix.
 */
contract SHProtocolTest is Test {
    /// @dev A deadline every {SessionHandler-addSession} call in this file can use: comfortably in
    ///      the future, comfortably inside MAX_SESSION_TTL. Recomputed per call so a test that warps
    ///      time still grants a live key.
    function _sessionDeadline() internal view returns (uint48) {
        return uint48(block.timestamp + 30 days);
    }

    using PasskeyHelper for PasskeyHelper.Passkey;

    /// @dev The owner's n-th device. Named, so the same keys come back on every run.
    function _device(uint256 n) internal pure returns (PasskeyHelper.Passkey memory) {
        return PasskeyHelper.create(string.concat("device ", vm.toString(n)));
    }

    /// @dev "No passkey": what deployWallet reads as an owner-only wallet.
    function _noKey() internal pure returns (PasskeyHelper.Passkey memory) {
        return PasskeyHelper.Passkey({privateKey: 0, qx: bytes32(0), qy: bytes32(0)});
    }

    ERC20Mock usdc;
    ERC20Mock dai;
    MockWeth weth;
    SHOracle oracle;
    SHRegistry feeRegistry;
    SHTreasury treasury;
    SHFactory factory;
    HelperConfig.NetworkConfig config;
    SessionHandler wallet;
    SpendingLimitModule module;
    SpendingLimitModuleHarness harness;
    MockSpender spender;

    /// @dev Per-window USD cap (18 decimals) configured at wallet deployment.
    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;

    address owner;
    address kani = makeAddr("kani");
    address rando = makeAddr("rando");

    function setUp() public {
        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        feeRegistry = treasury.REGISTRY();
        module = SpendingLimitModule(feeRegistry.spendingLimitModule());
        owner = config.account;

        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);
        weth = MockWeth(payable(config.weth));

        address[] memory watched = new address[](2);
        watched[0] = address(usdc);
        watched[1] = address(dai);
        vm.prank(owner);
        wallet =
            SessionHandler(payable(factory.deployWallet(DAILY_LIMIT, WINDOW, watched, bytes32(0), bytes32(0), 0, new address[](0))));

        harness = new SpendingLimitModuleHarness(address(feeRegistry));
        spender = new MockSpender();

        vm.deal(address(wallet), 10 ether);
        usdc.mint(address(wallet), 10_000e6);
        dai.mint(address(wallet), 10_000e18);
        weth.mint(address(wallet), 1_000e18);
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    /// @dev Owner-direct single-call execute (hook applies; guard skipped since msg.sender == owner).
    function _ownerExecute(address dest, uint256 value, bytes memory data) internal {
        vm.prank(owner);
        wallet.execute(bytes32(0), abi.encodePacked(dest, value, data));
    }

    /// @dev Owner-direct batch execute.
    function _ownerExecuteBatch(Execution[] memory execs) internal {
        vm.prank(owner);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));
    }

    function _spentInWindow() internal view returns (int256) {
        return wallet.getConfig().spentInWindow;
    }

    /*//////////////////////////////////////////////////////////////
                        DEPLOYMENT / FACTORY
    //////////////////////////////////////////////////////////////*/

    function test_deployWallet_initializesAccountAndCapConfig() public view {
        assertEq(wallet.owner(), owner);
        assertEq(address(wallet.SH_MODULE()), address(module));
        assertEq(wallet.ENTRY_POINT(), config.entryPoint);
        assertTrue(wallet.isModuleInstalled(MODULE_TYPE_HOOK, address(module), ""));

        SpendingLimitModule.Config memory cfg = wallet.getConfig();
        assertTrue(cfg.installed);
        assertEq(cfg.dailyLimitUsd, DAILY_LIMIT);
        assertEq(uint256(cfg.windowDuration), WINDOW);
        assertEq(cfg.watchedTokens.length, 2);
        assertTrue(wallet.isWatched(address(usdc)));
        assertTrue(wallet.isWatched(address(dai)));
        assertFalse(wallet.isWatched(address(weth)));
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT);
    }

    function test_deployWallet_revertsWhenModuleUnset() public {
        // The module lives on the registry now, and the registry refuses address(0) — so the only
        // way to observe the factory's guard is the genuine pre-set state: a registry whose module
        // has never been set (the window between deploying the registry and the module).
        SHRegistry bareRegistry = new SHRegistry(
            address(this),
            feeRegistry.MIN_PROTOCOL_FEE(),
            feeRegistry.MIN_PROTOCOL_FEE(),
            feeRegistry.MAX_PROTOCOL_FEE(),
            address(treasury),
            address(oracle),
            config.reputationRegistry,
            config.identityRegistry,
            config.entryPoint,
            1
        );
        SHFactory bareFactory = new SHFactory(
            address(this),
            address(bareRegistry),
            SessionHandler.GasCeilings({maxOpGasCost: config.maxOpGasCost, sessionGasBudget: config.sessionGasBudget})
        );

        address[] memory watched = new address[](0);
        vm.expectRevert(SHFactory.SHFactory_SpendingLimitModuleNotSet.selector);
        bareFactory.deployWallet(DAILY_LIMIT, WINDOW, watched, bytes32(0), bytes32(0), 0, new address[](0));
    }

    function test_deployWallet_revertsOnUnpricedWatchedToken() public {
        ERC20Mock unpriced = new ERC20Mock("Unpriced", "UNP", 18);
        address[] memory watched = new address[](1);
        watched[0] = address(unpriced);

        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(unpriced))
        );
        factory.deployWallet(DAILY_LIMIT, WINDOW, watched, bytes32(0), bytes32(0), 0, new address[](0));
    }

    /// @dev Every token NetworkConfig names must reach the oracle. DeploySHProtocol lists them by
    ///      hand, and one it forgets is unpriced: the app still offers it, and a wallet picking it
    ///      fails to deploy with TokenNotPriced (CAKE, AVAX and IMX were missed once). The
    ///      local config sets every token, so the script is checked against the full list here.
    ///      Whether each real chain's feeds answer is test_everyConfiguredTokenPricesOnFork's job.
    function test_everyConfiguredTokenIsPriced() public {
        address[22] memory configured = configuredTokens(config);
        address[] memory watched = new address[](configured.length);
        for (uint256 i = 0; i < configured.length; i++) {
            assertTrue(configured[i] != address(0), "the local config sets every token");
            assertTrue(oracle.isPriced(configured[i]), vm.toString(configured[i]));
            watched[i] = configured[i];
        }

        // The failure users actually saw: a wallet watching all of them deploys.
        vm.prank(owner);
        factory.deployWallet(DAILY_LIMIT, WINDOW, watched, bytes32(0), bytes32(0), 0, new address[](0));
    }

    function test_deployWallet_revertsOnZeroWindow() public {
        address[] memory watched = new address[](0);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidWindowDuration.selector);
        factory.deployWallet(DAILY_LIMIT, 0, watched, bytes32(0), bytes32(0), 0, new address[](0));
    }

    function test_initialize_cannotRerun() public {
        address[] memory watched = new address[](0);
        vm.expectRevert(Initializable.InvalidInitialization.selector);
        wallet.initialize(
            SessionHandler.InitConfig({
                owner: rando,
                walletId: 9,
                spendingLimitModule: address(module),
                dailyLimitUsd: 1,
                windowDuration: 1,
                watchedTokens: watched,
                sessionKeyX: bytes32(0),
                sessionKeyY: bytes32(0),
                sessionKeyValidUntil: 0,
                trustedSpenders: new address[](0)
            })
        );
    }

    function test_deployWallet_forwardsFunding() public {
        address[] memory watched = new address[](0);
        vm.deal(kani, 1 ether);
        vm.prank(kani);
        address funded =
            factory.deployWallet{value: 1 ether}(DAILY_LIMIT, WINDOW, watched, bytes32(0), bytes32(0), 0, new address[](0));
        assertEq(funded.balance, 1 ether);
    }

    /*//////////////////////////////////////////////////////////////
        DEPLOY-TIME SEEDING (session key + trusted spenders, CREATE2)
    //////////////////////////////////////////////////////////////*/

    /// @dev Deploys a wallet as `as_`, seeding whatever the test needs. Empty watched list keeps
    ///      these focused on the seeding, not on metering. The CREATE2 salt comes from the factory's
    ///      per-owner deployCount, so repeated calls with the same `as_` yield distinct wallets.
    function _deploySeeded(address as_, PasskeyHelper.Passkey memory key, address[] memory spenders)
        internal
        returns (SessionHandler)
    {
        vm.prank(as_);
        return SessionHandler(
            payable(
                factory.deployWallet(
                    DAILY_LIMIT, WINDOW, new address[](0), key.qx, key.qy, _sessionDeadline(), spenders
                )
            )
        );
    }

    function test_deployWallet_seedsPasskey() public {
        PasskeyHelper.Passkey memory seeded = _device(0);
        // The address is known before the deploy, so the event can be expected against it.
        address predicted = factory.predictWalletAddress(kani);

        uint48 deadline = _sessionDeadline();

        vm.expectEmit(true, false, false, true, predicted);
        emit SessionHandler.SessionAdded(seeded.keyId(), seeded.qx, seeded.qy, deadline);
        SessionHandler w = _deploySeeded(kani, seeded, new address[](0));

        assertEq(address(w), predicted, "deployed at a different address than predicted");
        assertEq(w.sessionValidUntil(seeded.keyId()), deadline, "seeded key got the wrong deadline");
        assertTrue(w.isSessionActive(seeded.keyId()), "seeded key should be live");
        (bytes32[] memory ids, uint48[] memory untils) = w.getSessions();
        assertEq(ids.length, 1, "exactly the seeded key is listed");
        assertEq(ids[0], seeded.keyId());
        assertEq(untils[0], deadline);
        assertEq(w.owner(), kani, "deployer should own the wallet");
    }

    /// @dev (0, 0) means "owner-only wallet", NOT an error (it would fail the curve check in
    ///      addSession). The deadline is ignored, so the wallet must come up with no key at all.
    function test_deployWallet_zeroKey_authorizesNone() public {
        SessionHandler w = _deploySeeded(kani, _noKey(), new address[](0));
        (bytes32[] memory ids,) = w.getSessions();
        assertEq(ids.length, 0, "an owner-only wallet lists no key");
        assertFalse(w.isSessionActive(w.sessionKeyId(bytes32(0), bytes32(0))));
        assertEq(w.sessionValidUntil(w.sessionKeyId(bytes32(0), bytes32(0))), 0);
    }

    /// @dev The seed goes through the same grant as addSession, so a deploy cannot start a wallet on
    ///      terms the owner could not set afterwards: the whole deploy reverts.
    function test_deployWallet_seedFollowsTheAddSessionRules() public {
        PasskeyHelper.Passkey memory key = _device(0);

        // Not a point on the curve.
        vm.prank(kani);
        vm.expectRevert(SessionHandler.SessionHandler_InvalidSessionKey.selector);
        factory.deployWallet(DAILY_LIMIT, WINDOW, new address[](0), key.qx, bytes32(uint256(key.qy) ^ 1), _sessionDeadline(), new address[](0));

        // One coordinate zero is NOT the owner-only sentinel: it is judged by the curve and refused.
        vm.prank(kani);
        vm.expectRevert(SessionHandler.SessionHandler_InvalidSessionKey.selector);
        factory.deployWallet(DAILY_LIMIT, WINDOW, new address[](0), bytes32(0), bytes32(uint256(1)), _sessionDeadline(), new address[](0));

        // A deadline in the past.
        uint48 past = uint48(block.timestamp);
        vm.prank(kani);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_SessionExpiryInPast.selector, past));
        factory.deployWallet(DAILY_LIMIT, WINDOW, new address[](0), key.qx, key.qy, past, new address[](0));

        // A deadline beyond the longest life a key may have. (The read is hoisted above the prank: a
        // call inside expectRevert's arguments would use the prank up.)
        uint48 maxTtl = wallet.MAX_SESSION_TTL();
        uint48 tooFar = uint48(block.timestamp) + maxTtl + 1;
        vm.prank(kani);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_SessionTtlTooLong.selector, tooFar, maxTtl));
        factory.deployWallet(DAILY_LIMIT, WINDOW, new address[](0), key.qx, key.qy, tooFar, new address[](0));

        assertEq(factory.deployCount(kani), 0, "a refused deploy consumed no wallet");
    }

    function test_deployWallet_seedsTrustedSpenders() public {
        address[] memory spenders = new address[](2);
        spenders[0] = makeAddr("routerA");
        spenders[1] = makeAddr("routerB");

        SessionHandler w = _deploySeeded(kani, _noKey(), spenders);

        assertTrue(w.isTrustedSpender(spenders[0]));
        assertTrue(w.isTrustedSpender(spenders[1]));
        assertEq(w.getConfig().trustedSpenders.length, 2);
    }

    function test_deployWallet_seedsTrustedSpender_zeroReverts() public {
        address[] memory spenders = new address[](1);
        spenders[0] = address(0);
        // The module's own validation propagates out through initialize().
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidTrustedSpender.selector);
        _deploySeeded(kani, _noKey(), spenders);
    }

    /// @dev The point of seeding a spender: an unpriced token (e.g. an LP token) may be approved to
    ///      it. Without the grant the same batch reverts TokenNotPriced -- asserted both ways here.
    function test_deployWallet_seededTrustedSpenderAllowsUnpricedApproval() public {
        ERC20Mock unpriced = new ERC20Mock("Unpriced", "UNP", 18);
        address venue = makeAddr("venue");

        address[] memory spenders = new address[](1);
        spenders[0] = venue;
        SessionHandler trusted = _deploySeeded(kani, _noKey(), spenders);
        SessionHandler untrusted = _deploySeeded(kani, _noKey(), new address[](0));

        // approve then zero in one batch, so the no-standing-approval rule is satisfied either way.
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(unpriced), 0, abi.encodeCall(ERC20Mock.approve, (venue, 1e18)));
        execs[1] = Execution(address(unpriced), 0, abi.encodeCall(ERC20Mock.approve, (venue, 0)));
        bytes32 batchMode = bytes32(uint256(0x01) << 248);

        vm.prank(kani);
        trusted.execute(batchMode, ERC7579Utils.encodeBatch(execs));

        vm.prank(kani);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(unpriced))
        );
        untrusted.execute(batchMode, ERC7579Utils.encodeBatch(execs));
    }

    /*//////////////////////////////////////////////////////////////
                 CREATE2 ADDRESSING / PER-OWNER deployCount
    //////////////////////////////////////////////////////////////*/

    function test_predictWalletAddress_matchesDeployed() public {
        address predicted = factory.predictWalletAddress(kani);
        assertEq(predicted.code.length, 0, "nothing should exist at the predicted address yet");

        SessionHandler w = _deploySeeded(kani, _noKey(), new address[](0));
        assertEq(address(w), predicted);
    }

    /// @dev The prediction advances as the owner's own deployCount is consumed.
    function test_predictWalletAddress_advancesAfterDeploy() public {
        assertEq(factory.deployCount(kani), 0);
        address first = factory.predictWalletAddress(kani);

        SessionHandler a = _deploySeeded(kani, _noKey(), new address[](0));
        assertEq(address(a), first);
        assertEq(factory.deployCount(kani), 1);

        address second = factory.predictWalletAddress(kani);
        assertTrue(second != first, "prediction must advance once the counter is consumed");
        SessionHandler b = _deploySeeded(kani, _noKey(), new address[](0));
        assertEq(address(b), second);
        assertEq(factory.deployCount(kani), 2);
    }

    /// @dev No salt to collide: a repeat deploy consumes the next nonce and yields a new wallet. One
    ///      owner may hold several; refusing a second is the CALLER's policy, enforced by reading
    ///      deployCount before asking for a signature, not the factory's.
    function test_deployWallet_secondDeploySameOwner_getsFreshWallet() public {
        SessionHandler a = _deploySeeded(kani, _noKey(), new address[](0));
        SessionHandler b = _deploySeeded(kani, _noKey(), new address[](0));
        assertTrue(address(a) != address(b));
        assertEq(a.owner(), kani);
        assertEq(b.owner(), kani);
    }

    /// @dev The owner is in the salt and the counter is per-owner, so no other caller can squat a
    ///      predicted address OR move it by deploying first.
    function test_predictWalletAddress_unaffectedByOtherOwners() public {
        address predictedKani = factory.predictWalletAddress(kani);
        address predictedRando = factory.predictWalletAddress(rando);
        assertTrue(predictedKani != predictedRando, "salt must bind the owner");

        // rando deploys first — kani's prediction must not move.
        SessionHandler b = _deploySeeded(rando, _noKey(), new address[](0));
        assertEq(address(b), predictedRando);
        assertEq(factory.predictWalletAddress(kani), predictedKani, "another owner's deploy moved kani's address");
        assertEq(factory.deployCount(kani), 0, "another owner's deploy advanced kani's counter");

        SessionHandler a = _deploySeeded(kani, _noKey(), new address[](0));
        assertEq(address(a), predictedKani);
    }

    /*//////////////////////////////////////////////////////////////
                          MODULE LIFECYCLE
    //////////////////////////////////////////////////////////////*/

    function test_onInstall_revertsIfAlreadyInstalled() public {
        vm.prank(address(wallet));
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_AlreadyInstalled.selector);
        module.onInstall(abi.encode(DAILY_LIMIT, WINDOW, new address[](0)));
    }

    function test_uninstall_wipesStateAndReinstallStartsFresh() public {
        vm.prank(owner);
        wallet.uninstallModule(MODULE_TYPE_HOOK, address(module), "");

        assertFalse(module.getConfig(address(wallet)).installed);
        assertFalse(module.isWatched(address(wallet), address(usdc)));
        assertEq(module.getConfig(address(wallet)).watchedTokens.length, 0);

        // Re-install with a different config: starts from the fresh state, not leftovers.
        address[] memory watched = new address[](1);
        watched[0] = address(dai);
        vm.prank(owner);
        wallet.installModule(MODULE_TYPE_HOOK, address(module), abi.encode(int256(1e18), uint256(2 days), watched));

        SpendingLimitModule.Config memory cfg = module.getConfig(address(wallet));
        assertTrue(cfg.installed);
        assertEq(cfg.dailyLimitUsd, int256(1e18));
        assertEq(uint256(cfg.windowDuration), 2 days);
        assertEq(cfg.watchedTokens.length, 1);
        assertFalse(module.isWatched(address(wallet), address(usdc)));
    }

    function test_isModuleType_hookOnly() public view {
        assertTrue(module.isModuleType(MODULE_TYPE_HOOK));
        assertFalse(module.isModuleType(MODULE_TYPE_VALIDATOR));
        assertFalse(module.isModuleType(MODULE_TYPE_EXECUTOR));
        assertFalse(module.isModuleType(MODULE_TYPE_FALLBACK));
    }

    function test_preCheck_revertsWhenNotInstalled() public {
        vm.prank(rando);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_NotInstalled.selector);
        module.preCheck(rando, 0, "");
    }

    function test_postCheck_silentWhenNotInstalled() public {
        bytes memory hookData = abi.encode(new address[](0), new uint256[](0), new address[](0), new address[](0));
        vm.prank(rando);
        module.postCheck(hookData); // must not revert
    }

    /*//////////////////////////////////////////////////////////////
                           CONFIG SETTERS
    //////////////////////////////////////////////////////////////*/

    function test_setDailyLimit_updatesAndZeroAllowed() public {
        vm.prank(owner);
        wallet.setDailyLimit(0);
        assertEq(wallet.getConfig().dailyLimitUsd, 0);
        assertEq(wallet.getRemainingBudget(), 0);
    }

    function test_setDailyLimit_negativeReverts() public {
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidDailyLimit.selector);
        wallet.setDailyLimit(-1);
    }

    /// @notice The limit is stored as an int128, so anything larger must be refused rather than cut
    ///         off; the largest value that fits is still accepted.
    function test_setDailyLimit_aboveInt128Reverts() public {
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidDailyLimit.selector);
        wallet.setDailyLimit(int256(type(int128).max) + 1);

        vm.prank(owner);
        wallet.setDailyLimit(type(int128).max);
        assertEq(wallet.getConfig().dailyLimitUsd, type(int128).max);
    }

    /// @notice postCheck checks the new tally against the limit BEFORE storing it (that ordering is
    ///         what keeps the int128 store safe). A spend landing exactly ON the limit must still be
    ///         stored in full, and the next unit of spend must then be refused.
    function test_spendExactlyToTheLimit_isStoredThenNextSpendReverts() public {
        uint256 amount = 1000e6;
        int256 exact = oracle.getPrice(address(usdc), amount);
        vm.prank(owner);
        wallet.setDailyLimit(exact);

        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, amount)));
        assertEq(_spentInWindow(), exact, "a spend exactly at the limit must be stored in full");
        assertEq(wallet.getRemainingBudget(), 0);

        int256 oneMore = oracle.getPrice(address(usdc), 1);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, exact + oneMore, exact
            )
        );
        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1)));
    }

    function test_setDailyLimit_nonOwnerReverts() public {
        vm.prank(rando);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, rando));
        wallet.setDailyLimit(1e18);
    }

    function test_setWindowDuration_boundsEnforced() public {
        vm.startPrank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidWindowDuration.selector);
        wallet.setWindowDuration(0);

        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidWindowDuration.selector);
        wallet.setWindowDuration(uint256(type(uint48).max) + 1);

        wallet.setWindowDuration(2 days);
        vm.stopPrank();
        assertEq(uint256(wallet.getConfig().windowDuration), 2 days);
    }

    function test_addWatchedToken_unpricedReverts() public {
        ERC20Mock unpriced = new ERC20Mock("Unpriced", "UNP", 18);
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(unpriced))
        );
        wallet.addWatchedToken(address(unpriced));
    }

    function test_addWatchedToken_duplicateIsNoOp() public {
        uint256 lengthBefore = wallet.getConfig().watchedTokens.length;
        vm.prank(owner);
        wallet.addWatchedToken(address(usdc));
        assertEq(wallet.getConfig().watchedTokens.length, lengthBefore);
    }

    function test_removeWatchedToken_removesAndAbsentIsNoOp() public {
        vm.prank(owner);
        wallet.removeWatchedToken(address(usdc));
        assertFalse(wallet.isWatched(address(usdc)));
        assertEq(wallet.getConfig().watchedTokens.length, 1);

        // absent token: no-op, no revert
        vm.prank(owner);
        wallet.removeWatchedToken(address(usdc));
        assertEq(wallet.getConfig().watchedTokens.length, 1);
    }

    function test_moduleSetters_revertForNonInstalledCaller() public {
        vm.startPrank(rando);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_NotInstalled.selector);
        module.setDailyLimit(1e18);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_NotInstalled.selector);
        module.addWatchedToken(address(usdc));
        vm.stopPrank();
    }

    function test_watchedTokenCap_enforcedAt32() public {
        // Self-contained fixture: 33 priced tokens against a dedicated oracle, with a plain EOA
        // acting as the installing account (the module only cares about msg.sender). The oracle
        // carries one extra entry for native, because SHRegistry's constructor refuses an oracle
        // that cannot price address(0) — the fee path and the module both depend on that feed.
        uint256 n = 33;
        address[] memory tokens = new address[](n + 1);
        address[] memory feeds = new address[](n + 1);
        uint256[] memory beats = new uint256[](n + 1);
        MockV3Aggregator sharedFeed = new MockV3Aggregator(8, 1e8);
        for (uint256 i = 0; i < n; i++) {
            tokens[i] = address(new ERC20Mock("T", "T", 18));
            feeds[i] = address(sharedFeed);
            beats[i] = 1 days;
        }
        tokens[n] = address(0);
        feeds[n] = address(sharedFeed);
        beats[n] = 1 days;
        SHOracle bigOracle = new SHOracle(address(this), address(0), tokens, feeds, beats);
        // The module reads its oracle from a registry, so the fixture needs its own registry
        // pointing at bigOracle (owner/fee/agentId are irrelevant to the watched-list cap).
        SHRegistry bigRegistry = new SHRegistry(
            address(this),
            feeRegistry.MIN_PROTOCOL_FEE(),
            feeRegistry.MIN_PROTOCOL_FEE(),
            feeRegistry.MAX_PROTOCOL_FEE(),
            address(this),
            address(bigOracle),
            config.reputationRegistry,
            config.identityRegistry,
            config.entryPoint,
            1
        );
        SpendingLimitModule freshModule = new SpendingLimitModule(address(bigRegistry));

        address account = makeAddr("eoaAccount");
        address[] memory first32 = new address[](32);
        for (uint256 i = 0; i < 32; i++) {
            first32[i] = tokens[i];
        }
        vm.prank(account);
        freshModule.onInstall(abi.encode(int256(1e18), uint256(1 days), first32));

        vm.prank(account);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_TooManyWatchedTokens.selector);
        freshModule.addWatchedToken(tokens[32]);
    }

    /*//////////////////////////////////////////////////////////////
               SESSION KEYS: THE PASSKEY LIST (account-level)
    //////////////////////////////////////////////////////////////*/

    /// @dev Authorizes devices 0..n-1 as the owner, each until the default deadline.
    function _addDevices(uint256 n) internal {
        for (uint256 i = 0; i < n; i++) {
            PasskeyHelper.Passkey memory key = _device(i);
            vm.prank(owner);
            wallet.addSession(key.qx, key.qy, _sessionDeadline());
        }
    }

    function _listedIds() internal view returns (bytes32[] memory ids) {
        (ids,) = wallet.getSessions();
    }

    function _contains(bytes32[] memory ids, bytes32 id) internal pure returns (bool) {
        for (uint256 i = 0; i < ids.length; i++) {
            if (ids[i] == id) return true;
        }
        return false;
    }

    /// @dev The key ids named by the recorded logs of one of the wallet's events (topic 1).
    function _idsLogged(Vm.Log[] memory logs, bytes32 eventSelector) internal view returns (bytes32[] memory out) {
        uint256 n;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(wallet) && logs[i].topics[0] == eventSelector) n++;
        }
        out = new bytes32[](n);
        n = 0;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(wallet) && logs[i].topics[0] == eventSelector) out[n++] = logs[i].topics[1];
        }
    }

    // ---- what addSession refuses ----------------------------------------------------------------

    /// @dev Not a point on the P-256 curve means no private key exists for it: the grant would
    ///      authorize nobody and use up a slot. (0, 0) is one such pair; it is only deployWallet's way
    ///      of saying "no key".
    function test_addSession_offCurveKeyReverts() public {
        PasskeyHelper.Passkey memory real = _device(0);
        bytes32[4] memory xs = [bytes32(0), bytes32(uint256(1)), real.qx, bytes32(P256.P)];
        bytes32[4] memory ys = [bytes32(0), bytes32(uint256(1)), bytes32(uint256(real.qy) ^ 1), real.qy];

        for (uint256 i = 0; i < xs.length; i++) {
            uint48 deadline = _sessionDeadline();
            vm.prank(owner);
            vm.expectRevert(SessionHandler.SessionHandler_InvalidSessionKey.selector);
            wallet.addSession(xs[i], ys[i], deadline);
        }
        assertEq(_listedIds().length, 0, "a refused key was listed");
    }

    function test_addSession_pastDeadlineReverts() public {
        PasskeyHelper.Passkey memory key = _device(0);
        uint48 past = uint48(block.timestamp);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_SessionExpiryInPast.selector, past));
        wallet.addSession(key.qx, key.qy, past);
    }

    /// @dev Zero is the dangerous one: the EntryPoint reads a zero validUntil as "valid forever".
    function test_addSession_zeroDeadlineReverts() public {
        PasskeyHelper.Passkey memory key = _device(0);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_SessionExpiryInPast.selector, uint48(0)));
        wallet.addSession(key.qx, key.qy, 0);
    }

    function test_addSession_beyondMaxTtlReverts() public {
        PasskeyHelper.Passkey memory key = _device(0);
        // Both reads happen BEFORE the prank: vm.prank applies to the next call only, and a view
        // call in the expectRevert argument would eat it.
        uint48 maxTtl = wallet.MAX_SESSION_TTL();
        uint48 tooFar = uint48(block.timestamp) + maxTtl + 1;
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionTtlTooLong.selector, tooFar, maxTtl)
        );
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, tooFar);
    }

    function test_addSession_exactlyMaxTtlAllowed() public {
        PasskeyHelper.Passkey memory key = _device(0);
        uint48 edge = uint48(block.timestamp) + wallet.MAX_SESSION_TTL();
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, edge);
        assertEq(wallet.sessionValidUntil(key.keyId()), edge);
    }

    function test_maxSessionKeys_isFive() public view {
        assertEq(wallet.MAX_SESSION_KEYS(), 5);
    }

    // ---- add, list, remove ----------------------------------------------------------------------

    function test_addRemoveSession_lifecycleAndEvents() public {
        PasskeyHelper.Passkey memory key = _device(0);
        uint48 deadline = _sessionDeadline();

        vm.expectEmit(true, false, false, true, address(wallet));
        emit SessionHandler.SessionAdded(key.keyId(), key.qx, key.qy, deadline);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, deadline);
        assertEq(wallet.sessionValidUntil(key.keyId()), deadline);
        assertTrue(wallet.isSessionActive(key.keyId()));
        (bytes32[] memory ids, uint48[] memory untils) = wallet.getSessions();
        assertEq(ids.length, 1);
        assertEq(ids[0], key.keyId());
        assertEq(untils[0], deadline);

        vm.expectEmit(true, false, false, false, address(wallet));
        emit SessionHandler.SessionRemoved(key.keyId());
        vm.prank(owner);
        wallet.removeSession(key.keyId());
        assertEq(wallet.sessionValidUntil(key.keyId()), 0, "the deadline must go with the key");
        assertFalse(wallet.isSessionActive(key.keyId()));
        assertEq(_listedIds().length, 0);
    }

    /// @dev The user chose up to five keys, one per device. All five are listed, each with its own deadline.
    function test_sessionKeys_fiveAtOnceEachWithItsOwnDeadline() public {
        for (uint256 i = 0; i < 5; i++) {
            PasskeyHelper.Passkey memory key = _device(i);
            vm.prank(owner);
            wallet.addSession(key.qx, key.qy, uint48(block.timestamp + (i + 1) * 1 days));
        }

        (bytes32[] memory ids, uint48[] memory untils) = wallet.getSessions();
        assertEq(ids.length, 5);
        for (uint256 i = 0; i < 5; i++) {
            PasskeyHelper.Passkey memory key = _device(i);
            assertTrue(_contains(ids, key.keyId()), "a granted key is not listed");
            assertEq(wallet.sessionValidUntil(key.keyId()), uint48(block.timestamp + (i + 1) * 1 days));
            assertTrue(wallet.isSessionActive(key.keyId()));
        }
        assertEq(untils.length, 5);
    }

    /// @dev THE CAP. A sixth key while five are live is refused, with the cap in the error, and
    ///      nothing changes.
    function test_sixthKey_isRefusedWhileFiveAreLive() public {
        _addDevices(5);
        bytes32[] memory before_ = _listedIds();

        PasskeyHelper.Passkey memory sixth = _device(5);
        uint48 deadline = _sessionDeadline();
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_TooManySessionKeys.selector, uint256(5)));
        wallet.addSession(sixth.qx, sixth.qy, deadline);

        bytes32[] memory after_ = _listedIds();
        assertEq(after_.length, 5);
        for (uint256 i = 0; i < 5; i++) {
            assertEq(after_[i], before_[i], "the refused add changed the list");
        }
        assertEq(wallet.sessionValidUntil(sixth.keyId()), 0);
    }

    /// @dev Revoking one device frees its slot for the next one.
    function test_removingAKey_freesItsSlot() public {
        _addDevices(5);
        vm.prank(owner);
        wallet.removeSession(_device(2).keyId());

        PasskeyHelper.Passkey memory sixth = _device(5);
        vm.prank(owner);
        wallet.addSession(sixth.qx, sixth.qy, _sessionDeadline());

        bytes32[] memory ids = _listedIds();
        assertEq(ids.length, 5);
        assertTrue(_contains(ids, sixth.keyId()));
        assertFalse(_contains(ids, _device(2).keyId()));
    }

    /// @dev Renewing a key the wallet already holds uses NO slot: at the cap, any of the five can
    ///      still be extended (or shortened), and only {SessionAdded} is emitted.
    function test_renewal_usesNoSlotAndEmitsOnlyAdded() public {
        _addDevices(5);
        PasskeyHelper.Passkey memory key = _device(3);
        uint48 later = uint48(block.timestamp + 80 days);

        vm.recordLogs();
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, later);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        assertEq(_idsLogged(logs, SessionHandler.SessionRemoved.selector).length, 0, "renewal emitted SessionRemoved");
        bytes32[] memory added = _idsLogged(logs, SessionHandler.SessionAdded.selector);
        assertEq(added.length, 1);
        assertEq(added[0], key.keyId());
        assertEq(wallet.sessionValidUntil(key.keyId()), later);
        assertEq(_listedIds().length, 5, "renewal changed the number of keys");

        // Shortening is a renewal too.
        uint48 sooner = uint48(block.timestamp + 1 days);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, sooner);
        assertEq(wallet.sessionValidUntil(key.keyId()), sooner);
        assertEq(_listedIds().length, 5);
    }

    /// @dev A key that ran out but is still listed comes straight back when renewed.
    function test_renewal_revivesAnExpiredKey() public {
        PasskeyHelper.Passkey memory key = _device(0);
        uint48 short_ = uint48(block.timestamp + 1 days);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, short_);

        vm.warp(uint256(short_) + 1);
        assertFalse(wallet.isSessionActive(key.keyId()));
        assertEq(_listedIds().length, 1, "an expired key stays listed until its slot is needed");

        uint48 renewed = uint48(block.timestamp + 30 days);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, renewed);
        assertTrue(wallet.isSessionActive(key.keyId()));
        assertEq(_listedIds().length, 1);
    }

    /// @dev THE SWEEP. A sixth key when five are held first removes every EXPIRED key, emitting
    ///      {SessionRemoved} for each, then takes a freed slot.
    function test_sixthKey_sweepsExpiredKeys_emittingRemovedForEach() public {
        uint48 long_ = _sessionDeadline();
        uint48 shortA = uint48(block.timestamp + 1 days); // device 1
        uint48 shortB = uint48(block.timestamp + 2 days); // device 3
        uint48[5] memory deadlines = [long_, shortA, long_, shortB, long_];
        for (uint256 i = 0; i < 5; i++) {
            PasskeyHelper.Passkey memory key = _device(i);
            vm.prank(owner);
            wallet.addSession(key.qx, key.qy, deadlines[i]);
        }
        vm.warp(uint256(shortB) + 1); // devices 1 and 3 are over; 0, 2, 4 are not

        PasskeyHelper.Passkey memory sixth = _device(5);
        uint48 sixthUntil = uint48(block.timestamp + 10 days);
        vm.recordLogs();
        vm.prank(owner);
        wallet.addSession(sixth.qx, sixth.qy, sixthUntil);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bytes32[] memory removed = _idsLogged(logs, SessionHandler.SessionRemoved.selector);
        assertEq(removed.length, 2, "one SessionRemoved per expired key");
        assertTrue(_contains(removed, _device(1).keyId()) && _contains(removed, _device(3).keyId()));
        bytes32[] memory added = _idsLogged(logs, SessionHandler.SessionAdded.selector);
        assertEq(added.length, 1);
        assertEq(added[0], sixth.keyId());

        bytes32[] memory ids = _listedIds();
        assertEq(ids.length, 4, "three live keys plus the new one");
        assertTrue(_contains(ids, _device(0).keyId()) && _contains(ids, _device(2).keyId()));
        assertTrue(_contains(ids, _device(4).keyId()) && _contains(ids, sixth.keyId()));
        assertEq(wallet.sessionValidUntil(_device(1).keyId()), 0, "a swept key keeps no deadline");
        assertEq(wallet.sessionValidUntil(_device(3).keyId()), 0);
        assertEq(wallet.sessionValidUntil(sixth.keyId()), sixthUntil);
    }

    /// @dev Expired means `block.timestamp > validUntil`, the EntryPoint's own rule: a key in its
    ///      deadline SECOND still works, so it is not swept and the sixth key is still refused. One
    ///      second later it is swept.
    function test_sweep_leavesAKeyInItsDeadlineSecond() public {
        PasskeyHelper.Passkey memory first = _device(0);
        uint48 edge = uint48(block.timestamp + 1 days);
        vm.prank(owner);
        wallet.addSession(first.qx, first.qy, edge);
        for (uint256 i = 1; i < 5; i++) {
            PasskeyHelper.Passkey memory key = _device(i);
            vm.prank(owner);
            wallet.addSession(key.qx, key.qy, _sessionDeadline());
        }
        PasskeyHelper.Passkey memory sixth = _device(5);

        vm.warp(edge);
        assertTrue(wallet.isSessionActive(first.keyId()));
        uint48 deadline = _sessionDeadline();
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_TooManySessionKeys.selector, uint256(5)));
        wallet.addSession(sixth.qx, sixth.qy, deadline);

        vm.warp(uint256(edge) + 1);
        vm.recordLogs();
        vm.prank(owner);
        wallet.addSession(sixth.qx, sixth.qy, uint48(block.timestamp + 5 days));
        bytes32[] memory removed = _idsLogged(vm.getRecordedLogs(), SessionHandler.SessionRemoved.selector);
        assertEq(removed.length, 1);
        assertEq(removed[0], first.keyId());
    }

    /// @dev With room to spare nothing is swept: an expired key stays until its slot is wanted, and
    ///      is reported with its past deadline so a UI can show "expired".
    function test_expiredKey_staysListedWhileThereIsRoom() public {
        PasskeyHelper.Passkey memory key = _device(0);
        uint48 short_ = uint48(block.timestamp + 1 days);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, short_);
        PasskeyHelper.Passkey memory other = _device(1);
        vm.warp(uint256(short_) + 5);

        vm.recordLogs();
        vm.prank(owner);
        wallet.addSession(other.qx, other.qy, uint48(block.timestamp + 5 days));
        assertEq(_idsLogged(vm.getRecordedLogs(), SessionHandler.SessionRemoved.selector).length, 0);

        (bytes32[] memory ids, uint48[] memory untils) = wallet.getSessions();
        assertEq(ids.length, 2);
        for (uint256 i = 0; i < 2; i++) {
            if (ids[i] == key.keyId()) assertEq(untils[i], short_, "reported with its past deadline");
        }
        assertFalse(wallet.isSessionActive(key.keyId()));
    }

    // ---- removing -------------------------------------------------------------------------------

    /// @dev A typo or a stale id must not read as a successful revoke.
    function test_removeSession_unknownKeyReverts() public {
        _addDevices(2);
        bytes32 stranger = _device(7).keyId();
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_UnknownSessionKey.selector, stranger));
        wallet.removeSession(stranger);

        // Already removed is unknown too.
        bytes32 gone = _device(0).keyId();
        vm.prank(owner);
        wallet.removeSession(gone);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_UnknownSessionKey.selector, gone));
        wallet.removeSession(gone);

        // And so is a wallet that never held any key.
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_UnknownSessionKey.selector, bytes32(0)));
        wallet.removeSession(bytes32(0));
    }

    /// @dev Removing from the MIDDLE of the list moves the last entry into the gap; the others, the
    ///      last one included, are untouched.
    function test_removeSession_fromTheMiddleKeepsTheOthers() public {
        _addDevices(5);
        vm.prank(owner);
        wallet.removeSession(_device(1).keyId());

        (bytes32[] memory ids, uint48[] memory untils) = wallet.getSessions();
        assertEq(ids.length, 4);
        assertFalse(_contains(ids, _device(1).keyId()));
        for (uint256 i = 0; i < ids.length; i++) {
            assertEq(untils[i], _sessionDeadline(), "a survivor lost or changed its deadline");
            assertEq(untils[i], wallet.sessionValidUntil(ids[i]));
        }
        for (uint256 n = 0; n < 5; n++) {
            if (n == 1) continue;
            assertTrue(_contains(ids, _device(n).keyId()), "a survivor went missing");
        }
    }

    /// @dev An expired key is removable like any other; it is the owner's way to tidy the list.
    function test_removeSession_worksOnAnExpiredKey() public {
        PasskeyHelper.Passkey memory key = _device(0);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, uint48(block.timestamp + 1 days));
        vm.warp(block.timestamp + 2 days);

        vm.prank(owner);
        wallet.removeSession(key.keyId());
        assertEq(_listedIds().length, 0);
    }

    function test_removeAllSessions_clearsEveryKeyAndEmitsForEach() public {
        _addDevices(5);

        vm.recordLogs();
        vm.prank(owner);
        wallet.removeAllSessions();
        bytes32[] memory removed = _idsLogged(vm.getRecordedLogs(), SessionHandler.SessionRemoved.selector);

        assertEq(removed.length, 5);
        assertEq(_listedIds().length, 0);
        for (uint256 i = 0; i < 5; i++) {
            assertTrue(_contains(removed, _device(i).keyId()));
            assertEq(wallet.sessionValidUntil(_device(i).keyId()), 0);
            assertFalse(wallet.isSessionActive(_device(i).keyId()));
        }

        // The wallet is empty again: five new keys fit.
        _addDevices(5);
        assertEq(_listedIds().length, 5);
    }

    function test_removeAllSessions_onAnEmptyListIsANoOp() public {
        vm.recordLogs();
        vm.prank(owner);
        wallet.removeAllSessions();
        assertEq(vm.getRecordedLogs().length, 0, "no-op revoke emitted an event");
        assertEq(_listedIds().length, 0);
    }

    // ---- who may change the list ----------------------------------------------------------------

    /// @dev Only the owner, by a direct call: not a stranger, not the EntryPoint, not the wallet itself.
    function test_sessionManagement_nonOwnerReverts() public {
        _addDevices(1);
        PasskeyHelper.Passkey memory key = _device(0);
        PasskeyHelper.Passkey memory other = _device(1);
        bytes32 keyId = key.keyId();
        address[3] memory callers = [rando, config.entryPoint, address(wallet)];

        for (uint256 i = 0; i < callers.length; i++) {
            vm.startPrank(callers[i]);
            vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, callers[i]));
            wallet.addSession(other.qx, other.qy, _sessionDeadline());
            vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, callers[i]));
            wallet.removeSession(keyId);
            vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, callers[i]));
            wallet.removeAllSessions();
            vm.stopPrank();
        }
        assertEq(_listedIds().length, 1, "a non-owner changed the list");
        assertTrue(wallet.isSessionActive(keyId));
    }

    // ---- reads ----------------------------------------------------------------------------------

    /// @dev isSessionActive mirrors the EntryPoint's comparison: still valid in the second that
    ///      equals the deadline, dead in the next one.
    function test_isSessionActive_boundaryMatchesEntryPoint() public {
        PasskeyHelper.Passkey memory key = _device(0);
        uint48 deadline = uint48(block.timestamp + 1 days);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, deadline);

        vm.warp(deadline);
        assertTrue(wallet.isSessionActive(key.keyId()), "should still be live at the deadline second");
        vm.warp(uint256(deadline) + 1);
        assertFalse(wallet.isSessionActive(key.keyId()), "should be dead one second later");
    }

    function test_isSessionActive_falseForAStrangerAndForZero() public {
        _addDevices(1);
        assertFalse(wallet.isSessionActive(_device(1).keyId()));
        assertFalse(wallet.isSessionActive(bytes32(0)));
    }

    /// @dev A key is listed iff its deadline is non-zero. A listed key with no deadline would be
    ///      read by the EntryPoint as "valid forever"; a deadline for an unlisted key would be a
    ///      grant nobody could revoke by id from the list.
    function test_listAndDeadlinesAreAlwaysSetTogether() public {
        _addDevices(3);
        vm.prank(owner);
        wallet.removeSession(_device(1).keyId());

        (bytes32[] memory ids, uint48[] memory untils) = wallet.getSessions();
        for (uint256 i = 0; i < ids.length; i++) {
            assertGt(untils[i], 0, "a listed key carries no deadline");
        }
        for (uint256 n = 0; n < 3; n++) {
            bytes32 id = _device(n).keyId();
            assertEq(_contains(ids, id), wallet.sessionValidUntil(id) != 0, "list and deadline disagree");
        }
    }

    /// @dev The id is the hash of the key's coordinates, so the app, the contract and an indexer all
    ///      compute the same one.
    function test_sessionKeyId_isTheHashOfThePublicKey() public view {
        PasskeyHelper.Passkey memory key = _device(0);
        assertEq(wallet.sessionKeyId(key.qx, key.qy), keccak256(abi.encode(key.qx, key.qy)));
        assertEq(wallet.sessionKeyId(key.qx, key.qy), key.keyId());
        assertTrue(wallet.sessionKeyId(key.qx, key.qy) != wallet.sessionKeyId(key.qy, key.qx), "order matters");
    }

    /// @dev A key removed and granted again is a fresh grant.
    function test_removedKey_canBeGrantedAgain() public {
        PasskeyHelper.Passkey memory key = _device(0);
        _addDevices(1);
        vm.prank(owner);
        wallet.removeSession(key.keyId());
        assertFalse(wallet.isSessionActive(key.keyId()));

        uint48 deadline = uint48(block.timestamp + 3 days);
        vm.prank(owner);
        wallet.addSession(key.qx, key.qy, deadline);
        assertTrue(wallet.isSessionActive(key.keyId()));
        assertEq(wallet.sessionValidUntil(key.keyId()), deadline);
    }

    /*//////////////////////////////////////////////////////////////
                    NET-VALUE METERING (the spending cap)
    //////////////////////////////////////////////////////////////*/

    function test_transfer_isMeteredAtOraclePrice() public {
        uint256 amount = 1000e6;
        int256 expectedUsd = oracle.getPrice(address(usdc), amount);

        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, amount)));

        assertEq(usdc.balanceOf(kani), amount);
        assertEq(_spentInWindow(), expectedUsd);
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT - expectedUsd);
    }

    function test_transfers_accumulateAcrossTransactions() public {
        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1000e6)));
        _ownerExecute(address(dai), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 500e18)));

        int256 expected = oracle.getPrice(address(usdc), 1000e6) + oracle.getPrice(address(dai), 500e18);
        assertEq(_spentInWindow(), expected);
    }

    function test_budgetExceeded_revertsWholeTransaction() public {
        vm.prank(owner);
        wallet.setDailyLimit(100e18); // tiny cap

        uint256 balanceBefore = usdc.balanceOf(address(wallet));
        int256 attemptedSpend = oracle.getPrice(address(usdc), 1000e6);
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, attemptedSpend, int256(100e18)
            )
        );
        wallet.execute(
            bytes32(0), abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.transfer, (kani, 1000e6)))
        );

        // whole tx reverted: nothing moved, nothing metered
        assertEq(usdc.balanceOf(address(wallet)), balanceBefore);
        assertEq(_spentInWindow(), 0);
    }

    function test_unwatchedToken_notMetered() public {
        _ownerExecute(address(weth), 0, abi.encodeCall(MockWeth.transfer, (kani, 100e18)));
        assertEq(weth.balanceOf(kani), 100e18);
        assertEq(_spentInWindow(), 0);
    }

    function test_nativeEth_isMetered() public {
        // Native value is metered: a plain 1-ETH send is priced through the address(0) sentinel and
        // charged to the window. This is the isolated native-decrease branch (no offsetting inflow).
        uint256 kaniBefore = kani.balance;
        int256 expectedUsd = oracle.getPrice(address(0), 1 ether);
        assertGt(expectedUsd, 0, "native price should be positive");

        _ownerExecute(kani, 1 ether, "");

        assertEq(kani.balance, kaniBefore + 1 ether);
        assertEq(_spentInWindow(), expectedUsd, "native send not metered");
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT - expectedUsd);
    }

    function test_nativeInflow_creditsAgainstWatchedOutflow() public {
        // A watched-token outflow plus a native INFLOW (unwrap) in one transaction: the native
        // inflow is priced through the address(0) sentinel and credited against the outflow,
        // exercising postCheck's native-INCREASE branch. Sized so the net stays positive, so the
        // credit is observable (without it, spend would be the full USDC value).
        vm.deal(address(weth), 10 ether); // back the mock so withdraw() can pay the wallet
        uint256 usdcOut = 3_000e6; // kept under the $5k window cap even after the native credit
        usdc.mint(address(wallet), usdcOut);

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, usdcOut)));
        execs[1] = Execution(address(weth), 0, abi.encodeCall(MockWeth.withdraw, (1 ether)));
        _ownerExecuteBatch(execs);

        int256 netUsd = oracle.getPrice(address(usdc), usdcOut) - oracle.getPrice(address(0), 1 ether);
        assertGt(netUsd, 0, "test mis-sized: expected a positive net after the native credit");
        assertEq(_spentInWindow(), netUsd, "native inflow not credited against watched outflow");
    }

    function test_inflowOffsetsOutflow_withinSameTransaction() public {
        // Out 1000 USDC, in 400 USDC (minted mid-batch): net outflow 600 is what gets metered.
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1000e6)));
        execs[1] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.mint, (address(wallet), 400e6)));

        _ownerExecuteBatch(execs);

        assertEq(_spentInWindow(), oracle.getPrice(address(usdc), 600e6));
    }

    function test_crossTokenNetting_swapLikeBatchNetsOut() public {
        // Simulates a swap: 1000 USDC out, DAI of roughly equal USD value in. The net USD delta
        // is what's metered -- compute it exactly from the oracle rather than hardcoding.
        uint256 usdcOut = 1000e6;
        uint256 daiIn = 800e18;
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, usdcOut)));
        execs[1] = Execution(address(dai), 0, abi.encodeCall(ERC20Mock.mint, (address(wallet), daiIn)));

        _ownerExecuteBatch(execs);

        int256 netUsd = oracle.getPrice(address(usdc), usdcOut) - oracle.getPrice(address(dai), daiIn);
        assertEq(_spentInWindow(), netUsd > 0 ? netUsd : int256(0));
    }

    function test_netInflow_neverBanksCreditForLaterTransactions() public {
        // Tx 1: net INFLOW (mint > out) => spent stays 0, no credit accrues.
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.mint, (address(wallet), 5000e6)));
        execs[1] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1000e6)));
        _ownerExecuteBatch(execs);
        assertEq(_spentInWindow(), 0, "net inflow must add nothing");

        // Tx 2: a plain outflow is charged in full -- the earlier inflow bought no headroom.
        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1000e6)));
        assertEq(_spentInWindow(), oracle.getPrice(address(usdc), 1000e6));
    }

    function test_windowRolls_restoringFullBudget() public {
        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 4000e6)));
        int256 firstSpend = _spentInWindow();
        assertGt(firstSpend, 0);

        vm.warp(block.timestamp + WINDOW + 1);
        // Expired-but-unrolled window already reports a full budget.
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT);

        // Feeds went stale during the warp; refresh the one we're about to move.
        MockV3Aggregator(config.usdcUsdPriceFeed).updateAnswer(MockV3Aggregator(config.usdcUsdPriceFeed).latestAnswer());

        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1000e6)));
        assertEq(_spentInWindow(), oracle.getPrice(address(usdc), 1000e6), "window did not reset");
    }

    function test_staleFeed_blocksOnlyTheMovedToken() public {
        // Make every feed stale, then refresh ONLY usdc's.
        vm.warp(block.timestamp + 365 days);
        MockV3Aggregator(config.usdcUsdPriceFeed).updateAnswer(MockV3Aggregator(config.usdcUsdPriceFeed).latestAnswer());

        // Moving usdc works -- dai's stale feed is irrelevant because dai never moved.
        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 100e6)));
        assertEq(usdc.balanceOf(kani), 100e6);

        // Moving dai now trips its stale feed, and the error names that feed.
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SHOracle.PriceOracle_StalePrice.selector, config.daiUsdPriceFeed));
        wallet.execute(
            bytes32(0), abi.encodePacked(address(dai), uint256(0), abi.encodeCall(ERC20Mock.transfer, (kani, 100e18)))
        );
    }

    /*//////////////////////////////////////////////////////////////
                      APPROVALS (no-standing rule)
    //////////////////////////////////////////////////////////////*/

    function test_unlimitedApprove_rejectedUpFront() public {
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_UnlimitedApprovalRejected.selector);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(
                address(usdc), uint256(0), abi.encodeCall(ERC20Mock.approve, (address(spender), type(uint256).max))
            )
        );
    }

    function test_approveOnUnpricedToken_rejected() public {
        ERC20Mock unpriced = new ERC20Mock("Unpriced", "UNP", 18);
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(unpriced))
        );
        wallet.execute(
            bytes32(0),
            abi.encodePacked(
                address(unpriced), uint256(0), abi.encodeCall(ERC20Mock.approve, (address(spender), 100e18))
            )
        );
    }

    function test_standingApproval_revertsWithResidual() public {
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_StandingApprovalNotAllowed.selector,
                address(usdc),
                address(spender),
                uint256(100e6)
            )
        );
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.approve, (address(spender), 100e6)))
        );
        assertEq(usdc.allowance(address(wallet), address(spender)), 0, "approval survived the revert");
    }

    function test_approveFullyConsumedInSameTx_passesAndMetersOutflow() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.approve, (address(spender), 100e6)));
        execs[1] =
            Execution(address(spender), 0, abi.encodeCall(MockSpender.pull, (address(usdc), address(wallet), 100e6)));

        _ownerExecuteBatch(execs);

        assertEq(usdc.allowance(address(wallet), address(spender)), 0);
        assertEq(usdc.balanceOf(address(spender)), 100e6);
        assertEq(_spentInWindow(), oracle.getPrice(address(usdc), 100e6), "consumed approval outflow not metered");
    }

    function test_partiallyConsumedApproval_reverts() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.approve, (address(spender), 100e6)));
        execs[1] =
            Execution(address(spender), 0, abi.encodeCall(MockSpender.pull, (address(usdc), address(wallet), 60e6)));

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_StandingApprovalNotAllowed.selector,
                address(usdc),
                address(spender),
                uint256(40e6)
            )
        );
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));
    }

    function test_zeroApprove_notTrackedAndPasses() public {
        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.approve, (address(spender), 0)));
        assertEq(usdc.allowance(address(wallet), address(spender)), 0);
    }

    function test_approveThenRevokeInSameTx_passes() public {
        // Residual-based check: granting then zeroing within one tx leaves nothing standing.
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.approve, (address(spender), 100e6)));
        execs[1] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.approve, (address(spender), 0)));

        _ownerExecuteBatch(execs);
        assertEq(usdc.allowance(address(wallet), address(spender)), 0);
    }

    /*//////////////////////////////////////////////////////////////
             TRUSTED SPENDERS (unpriced-approval exemption, Option C)
    //////////////////////////////////////////////////////////////*/

    function test_addTrustedSpender_lifecycleAndEvents() public {
        address venue = makeAddr("venue");
        // Nothing is trusted at deploy, so the list starts empty.
        assertEq(wallet.getConfig().trustedSpenders.length, 0);

        vm.expectEmit(true, true, false, false, address(module));
        emit SpendingLimitModule.TrustedSpenderAdded(address(wallet), venue);
        vm.prank(owner);
        wallet.addTrustedSpender(venue);

        assertTrue(wallet.isTrustedSpender(venue));
        SpendingLimitModule.Config memory cfg = wallet.getConfig();
        assertEq(cfg.trustedSpenders.length, 1);
        assertEq(cfg.trustedSpenders[0], venue);

        // idempotent
        vm.prank(owner);
        wallet.addTrustedSpender(venue);
        assertEq(wallet.getConfig().trustedSpenders.length, 1);

        vm.expectEmit(true, true, false, false, address(module));
        emit SpendingLimitModule.TrustedSpenderRemoved(address(wallet), venue);
        vm.prank(owner);
        wallet.removeTrustedSpender(venue);
        assertFalse(wallet.isTrustedSpender(venue));
        assertEq(wallet.getConfig().trustedSpenders.length, 0);
    }

    function test_addTrustedSpender_zeroReverts() public {
        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_InvalidTrustedSpender.selector);
        wallet.addTrustedSpender(address(0));
    }

    function test_trustedSpender_management_nonOwnerReverts() public {
        vm.startPrank(rando);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, rando));
        wallet.addTrustedSpender(address(spender));
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, rando));
        wallet.removeTrustedSpender(address(spender));
        vm.stopPrank();
    }

    function test_trustedSpender_capEnforced() public {
        vm.startPrank(owner);
        for (uint256 i = 1; i <= 16; i++) {
            wallet.addTrustedSpender(address(uint160(0x1000 + i)));
        }
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_TooManyTrustedSpenders.selector);
        wallet.addTrustedSpender(address(uint160(0x2000)));
        vm.stopPrank();
    }

    /// @notice The core Option-C behavior: an approval on an UNPRICED token (an LP-token stand-in)
    ///         to a TRUSTED spender is allowed as long as it's consumed to zero in the same tx.
    function test_trustedSpender_allowsUnpricedApprovalConsumedInSameTx() public {
        ERC20Mock lp = new ERC20Mock("LP", "LP", 18); // no oracle feed => unpriced
        lp.mint(address(wallet), 100e18);

        vm.prank(owner);
        wallet.addTrustedSpender(address(spender));

        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(lp), 0, abi.encodeCall(ERC20Mock.approve, (address(spender), 100e18)));
        execs[1] =
            Execution(address(spender), 0, abi.encodeCall(MockSpender.pull, (address(lp), address(wallet), 100e18)));

        _ownerExecuteBatch(execs);

        assertEq(lp.allowance(address(wallet), address(spender)), 0);
        assertEq(lp.balanceOf(address(spender)), 100e18);
        assertEq(_spentInWindow(), 0, "unpriced/unwatched token must not be metered");
    }

    /// @notice The same unpriced approval to an UNTRUSTED spender still reverts TokenNotPriced.
    function test_untrustedSpender_unpricedApprovalReverts() public {
        ERC20Mock lp = new ERC20Mock("LP", "LP", 18);
        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(lp))
        );
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(lp), uint256(0), abi.encodeCall(ERC20Mock.approve, (address(spender), 100e18)))
        );
    }

    /// @notice Trusting a spender does NOT relax the unlimited-approval ban.
    function test_trustedSpender_unlimitedStillRejected() public {
        ERC20Mock lp = new ERC20Mock("LP", "LP", 18);
        vm.prank(owner);
        wallet.addTrustedSpender(address(spender));

        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_UnlimitedApprovalRejected.selector);
        wallet.execute(
            bytes32(0),
            abi.encodePacked(
                address(lp), uint256(0), abi.encodeCall(ERC20Mock.approve, (address(spender), type(uint256).max))
            )
        );
    }

    /// @notice Trusting a spender does NOT relax the no-standing-approval rule: an unconsumed
    ///         allowance to a trusted spender still reverts the whole transaction.
    function test_trustedSpender_standingApprovalStillReverts() public {
        ERC20Mock lp = new ERC20Mock("LP", "LP", 18);
        vm.prank(owner);
        wallet.addTrustedSpender(address(spender));

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_StandingApprovalNotAllowed.selector,
                address(lp),
                address(spender),
                uint256(100e18)
            )
        );
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(lp), uint256(0), abi.encodeCall(ERC20Mock.approve, (address(spender), 100e18)))
        );
    }

    function test_removeTrustedSpender_reinstatesPricedRequirement() public {
        ERC20Mock lp = new ERC20Mock("LP", "LP", 18);
        vm.startPrank(owner);
        wallet.addTrustedSpender(address(spender));
        wallet.removeTrustedSpender(address(spender));
        vm.stopPrank();

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(lp))
        );
        wallet.execute(
            bytes32(0),
            abi.encodePacked(address(lp), uint256(0), abi.encodeCall(ERC20Mock.approve, (address(spender), 1e18)))
        );
    }

    function test_uninstall_clearsTrustedSpenders() public {
        vm.prank(owner);
        wallet.addTrustedSpender(address(spender));
        assertTrue(wallet.isTrustedSpender(address(spender)));

        vm.prank(owner);
        wallet.uninstallModule(MODULE_TYPE_HOOK, address(module), "");
        assertFalse(module.isTrustedSpender(address(wallet), address(spender)));

        address[] memory watched = new address[](0);
        vm.prank(owner);
        wallet.installModule(MODULE_TYPE_HOOK, address(module), abi.encode(DAILY_LIMIT, WINDOW, watched));
        assertFalse(wallet.isTrustedSpender(address(spender)));
        assertEq(wallet.getConfig().trustedSpenders.length, 0);
    }

    /*//////////////////////////////////////////////////////////////
                    HARNESS: CALLDATA DECODE / CLASSIFY
    //////////////////////////////////////////////////////////////*/

    function test_decodeExecuteCalldata_recognizesExecuteShapes() public view {
        bytes memory payload =
            abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));

        (bool isExecute, bytes32 mode, bytes memory decoded) =
            harness.decodeExecuteCalldata(abi.encodeCall(IERC7579Execution.execute, (bytes32(0), payload)));
        assertTrue(isExecute);
        assertEq(mode, bytes32(0));
        assertEq(decoded, payload);

        // executeFromExecutor has the identical arg shape and must also be recognized.
        (isExecute,,) =
            harness.decodeExecuteCalldata(abi.encodeCall(IERC7579Execution.executeFromExecutor, (bytes32(0), payload)));
        assertTrue(isExecute);
    }

    function test_decodeExecuteCalldata_rejectsNonExecute() public view {
        (bool isExecute,,) = harness.decodeExecuteCalldata(hex"deadbeef");
        assertFalse(isExecute, "short calldata accepted");

        (isExecute,,) = harness.decodeExecuteCalldata(abi.encodeCall(ERC20Mock.transfer, (kani, uint256(1e18))));
        assertFalse(isExecute, "non-execute selector accepted");
    }

    function test_isApproveCandidate_classification() public view {
        assertFalse(harness.isApproveCandidate(hex"01"));
        assertFalse(harness.isApproveCandidate(abi.encodeCall(ERC20Mock.transfer, (kani, uint256(1)))));
        assertFalse(harness.isApproveCandidate(abi.encodeCall(ERC20Mock.approve, (kani, uint256(0)))));
        assertFalse(harness.isApproveCandidate(abi.encodeCall(ERC20Mock.approve, (kani, type(uint256).max))));
        assertTrue(harness.isApproveCandidate(abi.encodeCall(ERC20Mock.approve, (kani, uint256(5e6)))));
    }

    function test_validateApproval_pathsAndReverts() public {
        (bool isApproval, address approvedSpender) = harness.validateApproval(
            address(this), address(usdc), abi.encodeCall(ERC20Mock.approve, (kani, uint256(5e6)))
        );
        assertTrue(isApproval);
        assertEq(approvedSpender, kani);

        (isApproval,) = harness.validateApproval(
            address(this), address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, uint256(1)))
        );
        assertFalse(isApproval);

        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_UnlimitedApprovalRejected.selector);
        harness.validateApproval(
            address(this), address(usdc), abi.encodeCall(ERC20Mock.approve, (kani, type(uint256).max))
        );

        ERC20Mock unpriced = new ERC20Mock("Unpriced", "UNP", 18);
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_TokenNotPriced.selector, address(unpriced))
        );
        harness.validateApproval(
            address(this), address(unpriced), abi.encodeCall(ERC20Mock.approve, (kani, uint256(1e18)))
        );
    }

    function test_collectApprovals_batchCollectsAllApprovePairs() public view {
        Execution[] memory execs = new Execution[](3);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.approve, (kani, uint256(1e6))));
        execs[1] = Execution(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, uint256(1e6))));
        execs[2] = Execution(address(dai), 0, abi.encodeCall(ERC20Mock.approve, (rando, uint256(2e18))));

        (address[] memory tokens, address[] memory spenders) = harness.collectApprovals(
            address(this), true, bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs)
        );

        assertEq(tokens.length, 2);
        assertEq(tokens[0], address(usdc));
        assertEq(spenders[0], kani);
        assertEq(tokens[1], address(dai));
        assertEq(spenders[1], rando);
    }

    /*//////////////////////////////////////////////////////////////
                     ACCOUNT VIEWS / OWNER FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    function test_getUsdValue_matchesOracle() public view {
        assertEq(wallet.getUsdValue(address(usdc), 1000e6), oracle.getPrice(address(usdc), 1000e6));
    }

    function test_registryViews() public view {
        assertEq(wallet.getAgentId(), feeRegistry.agentId());
    }

    function test_withdraw_erc20AndEth() public {
        vm.startPrank(owner);
        wallet.withdraw(address(usdc), 1000e6, kani);
        wallet.withdraw(address(0), 1 ether, kani);
        vm.stopPrank();

        assertEq(usdc.balanceOf(kani), 1000e6);
        assertEq(kani.balance, 1 ether);
    }

    function test_withdraw_reverts() public {
        vm.startPrank(owner);
        vm.expectRevert(SessionHandler.SessionHandler_InvalidRecipient.selector);
        wallet.withdraw(address(usdc), 1, address(0));

        vm.expectRevert(SessionHandler.SessionHandler_NotEnoughBalance.selector);
        wallet.withdraw(address(usdc), type(uint128).max, kani);

        vm.expectRevert(SessionHandler.SessionHandler_NotEnoughBalance.selector);
        wallet.withdraw(address(0), 1000 ether, kani);
        vm.stopPrank();

        vm.prank(rando);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, rando));
        wallet.withdraw(address(usdc), 1, kani);
    }

    function test_pause_blocksExecuteUntilUnpaused() public {
        vm.prank(owner);
        wallet.pause();

        vm.prank(owner);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        wallet.execute(
            bytes32(0), abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)))
        );

        vm.prank(owner);
        wallet.unpause();
        _ownerExecute(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        assertEq(usdc.balanceOf(kani), 1e6);
    }
}

/**
 * @notice Minimal spender used to consume allowances mid-batch, exercising the module's
 *         "approval must be spent to exactly zero in the same transaction" rule.
 */
contract MockSpender {
    function pull(address token, address from, uint256 amount) external {
        IERC20(token).transferFrom(from, address(this), amount);
    }
}
