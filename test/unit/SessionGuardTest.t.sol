//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {MODULE_TYPE_HOOK, MODULE_TYPE_EXECUTOR, Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {PackedUserOperation as OZPackedUserOperation} from "@openzeppelin/contracts/interfaces/draft-IERC4337.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockExecutorModule} from "../../src/mocks/MockExecutorModule.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {DECIMALS, ETH_USD_PRICE} from "../../script/Constants.s.sol";

/**
 * @title SessionGuardTest
 * @notice End-to-end proof of SessionHandler's session-execution guard: a session-key UserOp that
 *         tries to reach the account's own admin surface (uninstall the hook, raise the cap, mint
 *         more session keys) fails at execution, while a legitimate external call sails through the
 *         EntryPoint and is metered by the spending-cap hook.
 * @dev Two layers of coverage:
 *      - Full ERC-4337 flow (EntryPoint.handleOps): the inner execution revert does NOT bubble out
 *        of handleOps (the EntryPoint absorbs it and emits UserOperationRevertReason), so those
 *        tests assert the admin state is UNCHANGED afterwards — which is the property that matters.
 *      - Direct EntryPoint-pranked execute() calls: prove the exact custom error the guard raises.
 */
contract SessionGuardTest is Test {
    /// @dev A deadline every {SessionHandler-addSession} call in this file can use: comfortably in
    ///      the future, comfortably inside MAX_SESSION_TTL. Recomputed per call so a test that warps
    ///      time still grants a live key.
    function _sessionDeadline() internal view returns (uint48) {
        return uint48(block.timestamp + 30 days);
    }

    SessionHandler wallet;
    SpendingLimitModule module;
    SHOracle oracle;
    SHTreasury treasury;
    SHRegistry registry;
    SHFactory factory;
    HelperConfig.NetworkConfig config;
    SendPackedUserOp sendPackedUserOp;
    ERC20Mock usdc;
    ERC20Mock dai;

    /// @dev Per-window USD cap (18 decimals) configured at wallet deployment.
    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;

    /// @dev Anvil's default private key for config.account (the wallet owner on the local chain).
    uint256 constant ANVIL_OWNER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    address owner;
    address sessionKey;
    uint256 sessionKeyPk;
    address kani = makeAddr("kani");
    address attacker = makeAddr("attacker");
    address bundler = makeAddr("bundler");

    function setUp() public {
        (sessionKey, sessionKeyPk) = makeAddrAndKey("sessionKey");

        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        registry = factory.REGISTRY();
        module = SpendingLimitModule(registry.spendingLimitModule());
        owner = config.account;

        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);

        address[] memory watched = new address[](2);
        watched[0] = address(usdc);
        watched[1] = address(dai);
        vm.prank(owner);
        wallet =
            SessionHandler(payable(factory.deployWallet(DAILY_LIMIT, WINDOW, watched, address(0), 0, new address[](0))));

        sendPackedUserOp = new SendPackedUserOp();

        vm.deal(address(wallet), 10 ether);
        usdc.mint(address(wallet), 10_000e6);
        dai.mint(address(wallet), 10_000e18);

        vm.prank(owner);
        wallet.addSession(sessionKey, _sessionDeadline());
    }

    /*//////////////////////////////////////////////////////////////
                       DEPLOY-TIME SESSION-KEY SEEDING
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The whole point of seeding the key in {SHFactory-deployWallet}: ONE owner-signed
     *         transaction produces a wallet a session key can immediately drive. Note there is no
     *         `addSession` call anywhere in this test -- setUp's wallet is untouched and this one is
     *         deployed fresh with the key already authorized.
     */
    function test_deployWallet_seededSessionKeyExecutesImmediately() public {
        address[] memory watched = new address[](1);
        watched[0] = address(usdc);

        vm.prank(kani);
        SessionHandler w = SessionHandler(
            payable(
                factory.deployWallet(
                    DAILY_LIMIT, WINDOW, watched, sessionKey, _sessionDeadline(), new address[](0)
                )
            )
        );

        assertTrue(w.isSessionActive(sessionKey), "key should be authorized by deployWallet alone");

        vm.deal(address(w), 10 ether);
        usdc.mint(address(w), 1_000e6);

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(w),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (attacker, 100e6)),
            sessionKey,
            sessionKeyPk
        );
        _handleOps(userOp);

        assertEq(usdc.balanceOf(attacker), 100e6, "seeded session key could not spend");
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    /// @dev Signs a single-call UserOp with the session key and submits it through the EntryPoint.

    function _sendSessionOp(address dest, bytes memory data) internal {
        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(wallet), config, dest, 0, data, sessionKey, sessionKeyPk);
        _handleOps(userOp);
    }

    /*
    function _sendSessionOp(address dest, bytes memory data) internal {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        (PackedUserOperation memory userOp,,)= sendPackedUserOp.generateSignedUserOp(address(wallet), config, dest, 0, data, sessionKey, signerKeyPk);
        ops[0]=userOp;

        vm.prank(bundler,bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));

    }
    */

    function _handleOps(PackedUserOperation memory userOp) internal {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        // Two-arg prank: EntryPoint v0.9's nonReentrant requires tx.origin == msg.sender (EOA bundler).
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /// @dev Packs (dest, value, data) into ERC-7579 single-call executionCalldata.
    function _encodeSingle(address dest, uint256 value, bytes memory data) internal pure returns (bytes memory) {
        return abi.encodePacked(dest, value, data);
    }

    /*//////////////////////////////////////////////////////////////
              LEGIT SESSION FLOW (the guard must NOT interfere)
    //////////////////////////////////////////////////////////////*/

    /// @notice A session-key UserOp making a legitimate external call succeeds end-to-end through
    ///         the EntryPoint, and the spending-cap hook meters it.
    function test_sessionOp_legitTransfer_succeedsAndIsMetered() public {
        uint256 amount = 1000e6;
        int256 expectedUsd = oracle.getPrice(address(usdc), amount);

        _sendSessionOp(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, amount)));

        assertEq(usdc.balanceOf(kani), amount, "transfer did not execute");
        assertEq(wallet.getConfig().spentInWindow, expectedUsd, "outflow not metered");
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT - expectedUsd, "remaining budget wrong");
    }

    /// @notice An owner-signed UserOp through the EntryPoint also validates (owner is always an
    ///         authorized signer) and executes external calls fine.
    function test_ownerSignedOp_legitTransfer_succeeds() public {
        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 500e6)),
            owner,
            ANVIL_OWNER_KEY
        );
        _handleOps(userOp);
        assertEq(usdc.balanceOf(kani), 500e6, "owner-signed transfer did not execute");
    }

    /*//////////////////////////////////////////////////////////////
        ADMIN-SURFACE ESCALATION ATTEMPTS (must fail, state intact)
    //////////////////////////////////////////////////////////////*/

    /// @notice A session key must not be able to uninstall the spending-cap hook via a self-call.
    ///         The inner execution reverts inside the EntryPoint; the hook stays installed.
    function test_sessionOp_cannotUninstallHook() public {
        bytes memory data = abi.encodeCall(SessionHandler.uninstallModule, (MODULE_TYPE_HOOK, address(module), ""));

        _sendSessionOp(address(wallet), data);

        assertTrue(
            wallet.isModuleInstalled(MODULE_TYPE_HOOK, address(module), ""), "hook was uninstalled by a session key"
        );
        assertTrue(module.getConfig(address(wallet)).installed, "module config was wiped");
    }

    /// @notice A session key must not be able to raise its own cap by calling the module directly.
    function test_sessionOp_cannotRaiseOwnCap() public {
        bytes memory data = abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(1_000_000_000e18)));

        _sendSessionOp(address(module), data);

        assertEq(wallet.getConfig().dailyLimitUsd, DAILY_LIMIT, "session key raised its own cap");
    }

    /// @notice A session key must not be able to authorize more session keys via a self-call.
    function test_sessionOp_cannotMintMoreSessionKeys() public {
        _sendSessionOp(address(wallet), abi.encodeCall(SessionHandler.addSession, (attacker, _sessionDeadline())));

        assertEq(wallet.currentSession(), sessionKey, "session key minted another session key");
        assertFalse(wallet.isSessionActive(attacker), "attacker key became active");
    }

    /// @notice A batch hiding one restricted sub-call among legit ones must revert ATOMICALLY:
    ///         neither the transfer nor the cap change may land.
    function test_sessionOp_batchWithRestrictedTarget_revertsAtomically() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] =
            Execution({target: address(usdc), value: 0, callData: abi.encodeCall(ERC20Mock.transfer, (kani, 100e6))});
        execs[1] = Execution({
            target: address(module),
            value: 0,
            callData: abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(1_000_000_000e18)))
        });

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedBatchUserOp(address(wallet), config, execs, sessionKey, sessionKeyPk);
        _handleOps(userOp);

        assertEq(usdc.balanceOf(kani), 0, "batch partially executed despite restricted target");
        assertEq(wallet.getConfig().dailyLimitUsd, DAILY_LIMIT, "cap changed through batch smuggling");
    }

    /*//////////////////////////////////////////////////////////////
              EXACT GUARD ERRORS (direct EntryPoint-pranked calls)
    //////////////////////////////////////////////////////////////*/

    /// @notice The guard reverts with SessionRestrictedTarget(module) when a non-owner execution
    ///         targets the spending-limit module.
    function test_guard_revertsOnModuleTarget() public {
        bytes memory executionCalldata =
            _encodeSingle(address(module), 0, abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(1e18))));

        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(module))
        );
        wallet.execute(bytes32(0), executionCalldata);
    }

    /// @notice The guard reverts with SessionRestrictedTarget(account) when a non-owner execution
    ///         targets the account itself.
    function test_guard_revertsOnSelfTarget() public {
        bytes memory executionCalldata =
            _encodeSingle(address(wallet), 0, abi.encodeCall(SessionHandler.addSession, (attacker, _sessionDeadline())));

        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(wallet))
        );
        wallet.execute(bytes32(0), executionCalldata);
    }

    /// @notice The guard rejects delegatecall mode outright for non-owner executions.
    function test_guard_revertsOnDelegatecall() public {
        // Delegate executionCalldata layout: target ++ data (no value field).
        bytes memory executionCalldata =
            abi.encodePacked(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        bytes32 delegatecallMode = bytes32(uint256(0xff) << 248); // CALLTYPE_DELEGATECALL in top byte

        vm.prank(config.entryPoint);
        vm.expectRevert(SessionHandler.SessionHandler_SessionDelegateCallForbidden.selector);
        wallet.execute(delegatecallMode, executionCalldata);
    }

    /*//////////////////////////////////////////////////////////////
                               OWNER PATH
    //////////////////////////////////////////////////////////////*/

    /// @notice The module's own admin guard blocks reconfiguration through execute() for EVERYONE,
    ///         owner included: preCheck sees msg.sender == the account whether the owner called
    ///         execute() directly or a session key drove it through the EntryPoint, so it cannot
    ///         tell the two apart and refuses both. This is by design — the owner has no reason to
    ///         reach the module through execute() when the direct passthroughs exist (see
    ///         {test_ownerPassthroughs_unaffected}), so nothing legitimate is lost.
    /// @dev SessionHandler's own {_guardSessionExecution} is still skipped for the owner (it would
    ///      raise SessionRestrictedTarget instead); the error asserted here proves the revert comes
    ///      from the module's hook, one layer deeper.
    function test_ownerExecute_blockedByModuleAdminGuard() public {
        bytes memory executionCalldata =
            _encodeSingle(address(module), 0, abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(7000e18))));

        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_AdminExecution.selector);
        wallet.execute(bytes32(0), executionCalldata);

        assertEq(wallet.getConfig().dailyLimitUsd, DAILY_LIMIT, "cap changed despite the admin guard");
    }

    /// @notice The same block applies when the module setter is buried in a BATCH alongside
    ///         otherwise-innocent calls — the guard scans every sub-call, not just the first.
    function test_ownerExecuteBatch_blockedByModuleAdminGuard() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] =
            Execution({target: address(usdc), value: 0, callData: abi.encodeCall(ERC20Mock.transfer, (kani, 1e6))});
        execs[1] = Execution({
            target: address(module),
            value: 0,
            callData: abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(7000e18)))
        });

        vm.prank(owner);
        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_AdminExecution.selector);
        wallet.execute(bytes32(uint256(0x01) << 248), ERC7579Utils.encodeBatch(execs));

        assertEq(wallet.getConfig().dailyLimitUsd, DAILY_LIMIT, "cap changed despite the admin guard");
        assertEq(usdc.balanceOf(kani), 0, "batch was not reverted atomically");
    }

    /// @notice The owner's passthroughs remain the normal admin path and are untouched by the guard.
    function test_ownerPassthroughs_unaffected() public {
        vm.startPrank(owner);
        wallet.setDailyLimit(1234e18);
        wallet.removeSession();
        vm.stopPrank();

        assertEq(wallet.getConfig().dailyLimitUsd, int256(1234e18));
        assertFalse(wallet.isSessionActive(sessionKey));
    }

    /*//////////////////////////////////////////////////////////////
                         SIGNER AUTHORIZATION
    //////////////////////////////////////////////////////////////*/

    /// @notice A UserOp signed by a never-authorized key fails validation (AA24) before execution.
    function test_unknownSigner_failsValidation() public {
        (address rando, uint256 randoPk) = makeAddrAndKey("rando");
        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet), config, address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)), rando, randoPk
        );

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /*//////////////////////////////////////////////////////////////
                        SESSION-KEY EXPIRY (AA22)
    //////////////////////////////////////////////////////////////*/

    /// @dev Re-stamps every mock feed at its current answer. A warp far enough to expire a session
    ///      key is also far past every heartbeat, and a stale feed reverts the op inside the hook's
    ///      postCheck -- which would make these tests pass or fail for the wrong reason.
    function _refreshFeeds() internal {
        MockV3Aggregator(config.usdcUsdPriceFeed).updateAnswer(
            MockV3Aggregator(config.usdcUsdPriceFeed).latestAnswer()
        );
        MockV3Aggregator(config.daiUsdPriceFeed).updateAnswer(
            MockV3Aggregator(config.daiUsdPriceFeed).latestAnswer()
        );
        MockV3Aggregator(config.ethUsdPriceFeed).updateAnswer(
            MockV3Aggregator(config.ethUsdPriceFeed).latestAnswer()
        );
    }

    /// @dev Warps to `to` and re-stamps the feeds, so only the session key's deadline has moved.
    function _warpTo(uint256 to) internal {
        vm.warp(to);
        _refreshFeeds();
    }

    /// @dev Builds a transfer op signed by `sessionKey` and expects handleOps to fail with `reason`.
    function _expectSessionOpFailsWith(bytes memory reason) internal {
        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            sessionKey,
            sessionKeyPk
        );

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        vm.prank(bundler, bundler);
        vm.expectRevert(reason);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /**
     * @notice Once past its deadline a session key is refused by the EntryPoint with AA22, NOT AA24.
     *         The distinction is the point of returning a validity window instead of checking the
     *         clock inside validation: the signature was fine, the grant ran out, and the bot's
     *         revert decoding can tell the user which it was.
     */
    function test_expiredSessionKey_failsWithAA22() public {
        _sendSessionOp(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));

        _warpTo(uint256(wallet.currentSessionValidUntil()) + 1);

        _expectSessionOpFailsWith(
            abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA22 expired or not due")
        );
    }

    /// @notice The key is still accepted in the second that equals its deadline, matching the
    ///         EntryPoint's own `block.timestamp > validUntil` comparison and {isSessionActive}.
    function test_sessionKey_validInTheDeadlineSecond() public {
        _warpTo(uint256(wallet.currentSessionValidUntil()));
        assertTrue(wallet.isSessionActive(sessionKey), "view disagrees with the chain at the boundary");

        uint256 before = usdc.balanceOf(kani);
        _sendSessionOp(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        assertEq(usdc.balanceOf(kani), before + 1e6, "op rejected in the deadline second");
    }

    /// @notice Renewing the key after it lapsed brings it straight back -- no new key needed.
    function test_expiredSessionKey_worksAgainAfterRenewal() public {
        _warpTo(uint256(wallet.currentSessionValidUntil()) + 1);
        _expectSessionOpFailsWith(
            abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA22 expired or not due")
        );

        vm.prank(owner);
        wallet.addSession(sessionKey, _sessionDeadline());

        uint256 before = usdc.balanceOf(kani);
        _sendSessionOp(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        assertEq(usdc.balanceOf(kani), before + 1e6, "renewed key still refused");
    }

    /// @notice Granting a DIFFERENT key evicts the live one, and the evicted key fails AA24 (it is
    ///         no longer this wallet's key at all) rather than AA22.
    function test_evictedSessionKey_failsWithAA24() public {
        vm.prank(owner);
        wallet.addSession(makeAddr("replacementKey"), _sessionDeadline());

        _expectSessionOpFailsWith(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
    }

    /**
     * @notice The owner carries NO validity window: an owner-signed UserOp still runs after the
     *         session key has expired. The wallet must never become unusable to its owner because
     *         a delegated key ran out.
     */
    function test_ownerOp_unaffectedByExpiry() public {
        _warpTo(uint256(wallet.currentSessionValidUntil()) + 365 days);

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            owner,
            ANVIL_OWNER_KEY
        );

        uint256 before = usdc.balanceOf(kani);
        _handleOps(userOp);
        assertEq(usdc.balanceOf(kani), before + 1e6, "owner op blocked by the session key's expiry");
    }

    /// @notice After removeSession, a previously working session key fails validation (AA24).
    function test_removedSessionKey_failsValidation() public {
        vm.prank(owner);
        wallet.removeSession();

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            sessionKey,
            sessionKeyPk
        );

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = userOp;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /*//////////////////////////////////////////////////////////////
          GAS AS AN UNMETERED VALUE PATH (THREAT_MODEL §3.12)
    //////////////////////////////////////////////////////////////*/

    /// @dev Re-signs a UserOp after its gas fields have been mutated, so the EntryPoint hash still
    ///      matches. Mirrors SendPackedUserOp's EIP-191-over-userOpHash scheme.
    function _resign(PackedUserOperation memory userOp, uint256 pk) internal view returns (PackedUserOperation memory) {
        bytes32 digest = MessageHashUtils.toEthSignedMessageHash(IEntryPoint(config.entryPoint).getUserOpHash(userOp));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        userOp.signature = abi.encodePacked(r, s, v);
        return userOp;
    }

    /// @dev A legitimate session op, with its gas fees rewritten to `feePerGas` on both halves of
    ///      `gasFees` (so the packing order cannot affect the result) and re-signed.
    function _inflatedGasOp(uint128 feePerGas) internal view returns (PackedUserOperation memory userOp) {
        (userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            sessionKey,
            sessionKeyPk
        );
        userOp.gasFees = bytes32(uint256(feePerGas) << 128 | uint256(feePerGas));
        return _resign(userOp, sessionKeyPk);
    }

    /// @dev The account-abstraction and OpenZeppelin PackedUserOperation structs are field-identical
    ///      but distinct Solidity types; SessionHandler's validateUserOp takes OZ's.
    function _toOz(PackedUserOperation memory op) internal pure returns (OZPackedUserOperation memory) {
        return OZPackedUserOperation({
            sender: op.sender,
            nonce: op.nonce,
            initCode: op.initCode,
            callData: op.callData,
            accountGasLimits: op.accountGasLimits,
            preVerificationGas: op.preVerificationGas,
            gasFees: op.gasFees,
            paymasterAndData: op.paymasterAndData,
            signature: op.signature
        });
    }

    /// @dev The gas cost SessionHandler prices a UserOp at: (verification + call + preVerification)
    ///      gas limits multiplied by maxFeePerGas.
    function _expectedCost(PackedUserOperation memory userOp) internal pure returns (uint256) {
        uint256 verificationGasLimit = uint256(userOp.accountGasLimits) >> 128;
        uint256 callGasLimit = uint128(uint256(userOp.accountGasLimits));
        uint256 maxFeePerGas = uint128(uint256(userOp.gasFees));
        return (verificationGasLimit + callGasLimit + userOp.preVerificationGas) * maxFeePerGas;
    }

    /// @notice A UserOp whose own gas parameters price it above the ceiling fails validation, so a
    ///         session key cannot have the account pay unbounded "gas" to a bundler it controls.
    function test_validateUserOp_rejectsOpPricedAboveCeiling() public {
        PackedUserOperation memory userOp = _inflatedGasOp(1e12); // 1000 gwei
        uint256 cost = _expectedCost(userOp);
        uint256 ceiling = wallet.maxOpGasCost(); // read BEFORE the prank, which only lasts one call
        assertGt(cost, ceiling, "fixture should exceed the ceiling");

        vm.prank(config.entryPoint);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_OpGasCostTooHigh.selector, cost, ceiling));
        wallet.validateUserOp(_toOz(userOp), bytes32(0), 0);
    }

    /// @notice THE DEPOSIT BYPASS: bounding only the prefund top-up is not enough. The EntryPoint
    ///         debits the FULL requiredPrefund from the account's deposit and asks for a top-up of
    ///         `requiredPrefund - deposit`, so a wallet already carrying a deposit is billed with
    ///         `missingAccountFunds == 0`. The ceiling must therefore price the op itself.
    function test_validateUserOp_ceilingBindsEvenWhenDepositCoversTheOp() public {
        // Wallet holds a deposit big enough that the EntryPoint would ask for no top-up at all.
        IEntryPoint(config.entryPoint).depositTo{value: 60 ether}(address(wallet));
        assertGt(IEntryPoint(config.entryPoint).balanceOf(address(wallet)), 0);

        PackedUserOperation memory userOp = _inflatedGasOp(1e12);
        uint256 cost = _expectedCost(userOp);
        uint256 ceiling = wallet.maxOpGasCost();

        // missingAccountFunds == 0 — the path a top-up-only bound would wave straight through.
        vm.prank(config.entryPoint);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_OpGasCostTooHigh.selector, cost, ceiling));
        wallet.validateUserOp(_toOz(userOp), bytes32(0), 0);
    }

    /// @notice `validateUserOp` is also reachable in the EXECUTION phase (the EntryPoint forwards a
    ///         UserOp's callData to the account with msg.sender == EntryPoint), where BOTH the op
    ///         struct and `missingAccountFunds` are caller-chosen. The prefund bound is what stops
    ///         the account emptying its balance into its EntryPoint deposit. Note the signature is
    ///         never even checked here: _validateUserOp RETURNS failure rather than reverting.
    function test_payPrefund_rejectsOversizedPrefundRequest() public {
        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            sessionKey,
            sessionKeyPk
        );

        uint256 balanceBefore = address(wallet).balance;
        uint256 ceiling = wallet.maxOpGasCost();
        vm.prank(config.entryPoint);
        vm.expectRevert(abi.encodeWithSelector(SessionHandler.SessionHandler_PrefundTooHigh.selector, 5 ether, ceiling));
        wallet.validateUserOp(_toOz(userOp), bytes32(0), 5 ether);
        assertEq(address(wallet).balance, balanceBefore, "no ETH should have moved");
    }

    /// @notice A prefund within the ceiling is still paid, so ordinary bundling is unaffected.
    function test_payPrefund_allowsPrefundWithinCeiling() public {
        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            sessionKey,
            sessionKeyPk
        );

        uint256 balanceBefore = address(wallet).balance;
        vm.prank(config.entryPoint);
        wallet.validateUserOp(_toOz(userOp), bytes32(0), 0.01 ether);
        assertEq(address(wallet).balance, balanceBefore - 0.01 ether, "prefund not paid");
    }

    /// @notice A paused wallet fails in VALIDATION, so it never reaches _payPrefund and pays nothing.
    function test_validateUserOp_pausedWalletPaysNothing() public {
        vm.prank(owner);
        wallet.pause();

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            sessionKey,
            sessionKeyPk
        );

        uint256 balanceBefore = address(wallet).balance;
        vm.prank(config.entryPoint);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        wallet.validateUserOp(_toOz(userOp), bytes32(0), 0.01 ether);
        assertEq(address(wallet).balance, balanceBefore, "paused wallet must not pay prefund");
    }

    function test_setMaxOpGasCost_ownerOnlyAndNonZero() public {
        vm.prank(attacker);
        vm.expectRevert();
        wallet.setMaxOpGasCost(1 ether);

        vm.prank(owner);
        vm.expectRevert(SessionHandler.SessionHandler_InvalidMaxOpGasCost.selector);
        wallet.setMaxOpGasCost(0);

        vm.prank(owner);
        wallet.setMaxOpGasCost(1 ether);
        assertEq(wallet.maxOpGasCost(), 1 ether);
    }

    /// @notice {maxOpGasCost} is a uint80 (so it packs beside `_paused`): the setter must refuse a
    ///         value that does not fit rather than silently truncate it, and still accept the largest
    ///         value that does.
    function test_setMaxOpGasCost_rejectsValuesAboveUint80() public {
        uint256 limit = type(uint80).max;

        vm.prank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_MaxOpGasCostTooHigh.selector, limit + 1, limit)
        );
        wallet.setMaxOpGasCost(limit + 1);

        vm.prank(owner);
        wallet.setMaxOpGasCost(limit);
        assertEq(wallet.maxOpGasCost(), limit);
    }

    /// @notice MaxOpGasCostUpdated must report the PREVIOUS ceiling as oldMax, not the new one.
    /// @dev Regression test: the emit ran after the assignment, so both arguments carried the new
    ///      value and the old ceiling was unrecoverable by anything indexing the event.
    function test_setMaxOpGasCost_emitsPreviousValueAsOldMax() public {
        vm.prank(owner);
        wallet.setMaxOpGasCost(1 ether);

        vm.expectEmit(true, true, true, true, address(wallet));
        emit SessionHandler.MaxOpGasCostUpdated(1 ether, 2 ether);

        vm.prank(owner);
        wallet.setMaxOpGasCost(2 ether);
    }

    /*//////////////////////////////////////////////////////////////
                  PROTOCOL FEE (flat wei, native-paid)
    //////////////////////////////////////////////////////////////*/

    /// @dev Every test here drives execute() as the EntryPoint rather than through handleOps, so a
    ///      revert surfaces as its own error instead of being absorbed into UserOperationRevertReason,
    ///      and the wallet's balance moves only by the fee — no prefund noise.

    /// @notice A session-key execution transfers the fee to the registry's treasury, in the wei
    ///         amount getFee() quotes at the time of the call.
    function test_sessionExecution_paysFeeToTreasury() public {
        uint256 expectedFee = registry.getFee();
        uint256 treasuryBefore = address(treasury).balance;
        uint256 walletBefore = address(wallet).balance;

        vm.expectEmit(true, false, false, true, address(wallet));
        emit SessionHandler.ProtocolFeePaid(address(treasury), expectedFee);
        vm.prank(config.entryPoint);
        wallet.execute(bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6))));

        assertEq(address(treasury).balance, treasuryBefore + expectedFee, "treasury did not receive the fee");
        assertEq(address(wallet).balance, walletBefore - expectedFee, "wallet paid something other than the fee");
        assertEq(treasury.totalFeesCollected(), expectedFee, "receive() did not tally the fee");
    }

    /// @notice A flat wei fee, observed end-to-end: double the ETH price and the same execution
    ///         costs the same wei.
    function test_sessionExecution_feeChargedUnchangedWhenEthPriceDoubles() public {
        uint256 feeAtBasePrice = _feeChargedByOneSessionExecution();

        MockV3Aggregator doubled = new MockV3Aggregator(DECIMALS, ETH_USD_PRICE * 2);
        vm.prank(owner);
        treasury.setFeed(address(oracle), address(0), address(doubled), config.ethHeartbeat);

        assertEq(_feeChargedByOneSessionExecution(), feeAtBasePrice, "fee re-priced with ETH");
    }

    /// @notice The fee is deliberately OUTSIDE the metered window. `_extractFee` runs before
    ///         `_execute`, and the hook's preCheck→postCheck window opens inside `_execute`, so the
    ///         fee transfer is never charged against the account's USD cap: the cap meters what the
    ///         USER spends, and a protocol fee is not the user's spend. See THREAT_MODEL §3.12.
    function test_fee_isNotChargedAgainstTheSpendingCap() public {
        uint256 amount = 1000e6;
        int256 tokenUsd = oracle.getPrice(address(usdc), amount);
        uint256 fee = registry.getFee();
        assertGt(fee, 0, "fixture must actually charge a fee for this to prove anything");

        vm.prank(config.entryPoint);
        wallet.execute(bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, amount))));

        // The fee really did leave the account...
        assertEq(address(treasury).balance, fee, "fee was not paid");
        // ...but the cap saw only the token outflow, to the wei.
        assertEq(wallet.getConfig().spentInWindow, tokenUsd, "fee leaked into the metered spend");
        assertEq(wallet.getRemainingBudget(), DAILY_LIMIT - tokenUsd, "budget consumed by more than the transfer");
    }

    /// @notice Owner-initiated executions pay no fee at all — `execute` charges only when
    ///         msg.sender != owner().
    function test_ownerExecution_paysNoFee() public {
        vm.prank(owner);
        wallet.execute(bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6))));

        assertEq(address(treasury).balance, 0, "owner path charged a fee");
        assertEq(treasury.totalFeesCollected(), 0);
    }

    /// @notice The fee path makes no oracle call, so a session-key ERC-20 transfer — which moves no
    ///         native value of its own — goes through with the ETH/USD feed stale, and still pays
    ///         the fee.
    /// @dev Only the native feed is left stale: the token feeds are re-stamped after the warp, so
    ///      the only thing that could have consulted the native feed is `_extractFee`.
    function test_sessionExecution_erc20OnlyTransfer_survivesAStaleNativeFeed() public {
        _staleNativeFeedOnly();
        uint256 fee = registry.getFee();

        vm.prank(config.entryPoint);
        wallet.execute(bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6))));

        assertEq(usdc.balanceOf(kani), 1e6, "transfer blocked by a feed the fee no longer reads");
        assertEq(address(treasury).balance, fee, "fee was not paid");
    }

    /// @notice A wallet that cannot cover the fee is refused before anything executes, rather than
    ///         executing and leaving the treasury short.
    function test_sessionExecution_revertsWhenWalletCannotCoverTheFee() public {
        vm.deal(address(wallet), registry.getFee() - 1);

        vm.prank(config.entryPoint);
        vm.expectRevert(SessionHandler.SessionHandler_NotEnoughBalance.selector);
        wallet.execute(bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6))));

        assertEq(usdc.balanceOf(kani), 0, "transfer executed without the fee being paid");
    }

    /// @dev Runs one session-key ERC-20 transfer and returns the wei it cost the wallet, which is
    ///      exactly the fee — the call moves no native value of its own.
    function _feeChargedByOneSessionExecution() internal returns (uint256) {
        uint256 balanceBefore = address(wallet).balance;
        vm.prank(config.entryPoint);
        wallet.execute(bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6))));
        return balanceBefore - address(wallet).balance;
    }

    /// @dev Warps past every feed's heartbeat, then re-stamps the watched TOKEN feeds so native is
    ///      the only stale one. MockV3Aggregator records updatedAt at write time, so re-answering
    ///      with the same value is enough to refresh it.
    function _staleNativeFeedOnly() internal {
        skip(config.ethHeartbeat + 1);
        MockV3Aggregator usdcFeed = MockV3Aggregator(config.usdcUsdPriceFeed);
        MockV3Aggregator daiFeed = MockV3Aggregator(config.daiUsdPriceFeed);
        usdcFeed.updateAnswer(usdcFeed.latestAnswer());
        daiFeed.updateAnswer(daiFeed.latestAnswer());
    }

    /*//////////////////////////////////////////////////////////////
                  ENTRYPOINT AS A RESTRICTED TARGET
    //////////////////////////////////////////////////////////////*/

    /// @notice A session key cannot withdraw the account's EntryPoint deposit. That ETH would leave
    ///         the EntryPoint for the attacker directly, so `account.balance` never changes and the
    ///         spending-cap hook would meter a $0 spend.
    function test_guard_revertsOnEntryPointTarget() public {
        bytes memory executionCalldata = _encodeSingle(
            config.entryPoint, 0, abi.encodeWithSignature("withdrawTo(address,uint256)", attacker, 1 ether)
        );

        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, config.entryPoint)
        );
        wallet.execute(bytes32(0), executionCalldata);
    }

    /// @notice The owner is NOT locked out: a direct owner call skips the guard entirely, so a
    ///         deposit stranded by the restriction above is always recoverable.
    function test_ownerCanStillWithdrawEntryPointDeposit() public {
        IEntryPoint(config.entryPoint).depositTo{value: 1 ether}(address(wallet));
        uint256 ownerBalanceBefore = owner.balance;

        vm.prank(owner);
        wallet.execute(
            bytes32(0),
            _encodeSingle(config.entryPoint, 0, abi.encodeWithSignature("withdrawTo(address,uint256)", owner, 1 ether))
        );

        assertEq(owner.balance, ownerBalanceBefore + 1 ether, "owner could not recover the deposit");
        assertEq(IEntryPoint(config.entryPoint).balanceOf(address(wallet)), 0);
    }

    /*//////////////////////////////////////////////////////////////
             SESSION TARGET ALLOWLIST (THREAT_MODEL §3.13)
    //////////////////////////////////////////////////////////////*/

    function test_allowlist_cannotEnableWhileEmpty() public {
        vm.prank(owner);
        vm.expectRevert(SessionHandler.SessionHandler_EmptyAllowlist.selector);
        wallet.toggleAllowList(true);
    }

    function test_allowlist_offByDefault_allowsAnyExternalTarget() public {
        assertFalse(wallet.sessionAllowlistEnabled());
        _sendSessionOp(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        assertEq(usdc.balanceOf(kani), 1e6);
    }

    function test_allowlist_confinesSessionKeyToAllowedTargets() public {
        vm.startPrank(owner);
        wallet.addAllowedTarget(address(usdc));
        wallet.toggleAllowList(true);
        vm.stopPrank();

        // Allowed target still works end-to-end.
        _sendSessionOp(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        assertEq(usdc.balanceOf(kani), 1e6, "allowed target should execute");

        // A target that is not on the list is refused by the guard.
        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(dai))
        );
        wallet.execute(bytes32(0), _encodeSingle(address(dai), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e18))));
    }

    function test_allowlist_doesNotRestrictTheOwner() public {
        vm.startPrank(owner);
        wallet.addAllowedTarget(address(usdc));
        wallet.toggleAllowList(true);
        // dai is NOT allowlisted, but the owner's direct call skips the guard.
        wallet.execute(bytes32(0), _encodeSingle(address(dai), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e18))));
        vm.stopPrank();

        assertEq(dai.balanceOf(kani), 1e18);
    }

    function test_allowlist_batchAddAndRemoveTracksCount() public {
        address[] memory targets = new address[](2);
        targets[0] = address(usdc);
        targets[1] = address(dai);

        vm.startPrank(owner);
        wallet.addAllowedTargets(targets);
        assertEq(wallet.allowedTargetCount(), 2);
        // Adding a duplicate must not double-count, or the emptiness guard could never be reached.
        wallet.addAllowedTarget(address(usdc));
        assertEq(wallet.allowedTargetCount(), 2);

        wallet.removeAllowedTarget(address(usdc));
        assertEq(wallet.allowedTargetCount(), 1);
        assertFalse(wallet.sessionTargetAllowlist(address(usdc)));
        // Removing something absent is a no-op, not an underflow.
        wallet.removeAllowedTarget(address(usdc));
        assertEq(wallet.allowedTargetCount(), 1);
        vm.stopPrank();
    }

    /// @notice An allowlist emptied while enforced fails CLOSED — every session execution reverts —
    ///         rather than silently reopening every target.
    function test_allowlist_emptiedWhileEnabledFailsClosed() public {
        vm.startPrank(owner);
        wallet.addAllowedTarget(address(usdc));
        wallet.toggleAllowList(true);
        wallet.removeAllowedTarget(address(usdc));
        vm.stopPrank();

        assertTrue(wallet.sessionAllowlistEnabled());
        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(usdc))
        );
        wallet.execute(bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6))));
    }

    function test_allowlist_rejectsZeroTarget() public {
        vm.prank(owner);
        vm.expectRevert(SessionHandler.SessionHandler_InvalidAllowedTarget.selector);
        wallet.addAllowedTarget(address(0));
    }

    /*//////////////////////////////////////////////////////////////
              EXECUTOR MODULES (executeFromExecutor) -- the guard
    //////////////////////////////////////////////////////////////*/

    /**
     * @dev Until 2026-09-14 `executeFromExecutor` ran NO guard: it charged the fee and executed.
     *      Nothing in the repo drove that path, which is how the gap survived. An installed executor
     *      could therefore reach everything a session key is blocked from -- most consequentially
     *      `ENTRY_POINT.withdrawTo`, which moves the account's 4337 deposit without touching
     *      `account.balance`, so the hook metered a $0 spend.
     *
     *      It was never a session-key hole (installing a module is `onlyOwner`, and none is installed
     *      at deploy), but it contradicted the guarantee THREAT_MODEL 3.5 makes for "every non-owner
     *      path". These tests hold that guarantee to its word.
     */
    function _installExecutor() internal returns (MockExecutorModule executor) {
        executor = new MockExecutorModule();
        vm.prank(owner);
        wallet.installModule(MODULE_TYPE_EXECUTOR, address(executor), "");
        assertTrue(wallet.isModuleInstalled(MODULE_TYPE_EXECUTOR, address(executor), ""), "executor not installed");
    }

    /// @notice THE finding. An executor must not be able to drain the account's EntryPoint deposit --
    ///         the one outflow the spending cap structurally cannot see.
    function test_executor_cannotDrainEntryPointDeposit() public {
        MockExecutorModule executor = _installExecutor();
        IEntryPoint(config.entryPoint).depositTo{value: 1 ether}(address(wallet));
        uint256 depositBefore = IEntryPoint(config.entryPoint).balanceOf(address(wallet));

        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, config.entryPoint)
        );
        executor.callExecute(
            address(wallet),
            bytes32(0),
            _encodeSingle(
                config.entryPoint, 0, abi.encodeWithSignature("withdrawTo(address,uint256)", attacker, 1 ether)
            )
        );

        // The state assertion is the one that matters -- a revert could come from anywhere.
        assertEq(IEntryPoint(config.entryPoint).balanceOf(address(wallet)), depositBefore, "deposit moved");
        assertEq(attacker.balance, 0, "attacker received the deposit");
    }

    /// @notice An executor cannot reach the account's own admin surface to mint itself a session key.
    function test_executor_cannotReachAccountAdminSurface() public {
        MockExecutorModule executor = _installExecutor();

        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(wallet))
        );
        executor.callExecute(
            address(wallet), bytes32(0), _encodeSingle(address(wallet), 0, abi.encodeCall(SessionHandler.addSession, (attacker, _sessionDeadline())))
        );

        assertEq(wallet.currentSession(), sessionKey, "attacker gained a session key");
    }

    /// @notice An executor cannot reach the module's cap setters, which key by msg.sender.
    function test_executor_cannotReachSpendingLimitModule() public {
        MockExecutorModule executor = _installExecutor();
        int256 limitBefore = wallet.getConfig().dailyLimitUsd;

        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(module))
        );
        executor.callExecute(
            address(wallet),
            bytes32(0),
            _encodeSingle(address(module), 0, abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(1_000_000e18))))
        );

        assertEq(wallet.getConfig().dailyLimitUsd, limitBefore, "cap was raised");
    }

    /// @notice An executor cannot delegatecall, which would run arbitrary code as the account and
    ///         reach the admin surface whatever target was encoded.
    function test_executor_cannotDelegatecall() public {
        MockExecutorModule executor = _installExecutor();
        bytes memory executionCalldata =
            abi.encodePacked(address(usdc), abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)));
        bytes32 delegatecallMode = bytes32(uint256(0xff) << 248);

        vm.expectRevert(SessionHandler.SessionHandler_SessionDelegateCallForbidden.selector);
        executor.callExecute(address(wallet), delegatecallMode, executionCalldata);
    }

    /// @notice The decision taken with this fix: executors are bound by `sessionTargetAllowlist` too,
    ///         not only session keys. An owner who narrowed the wallet to a fixed set of venues meant
    ///         to constrain automated spending, and an executor IS automated spending.
    function test_executor_isBoundByTheTargetAllowlist() public {
        MockExecutorModule executor = _installExecutor();
        vm.startPrank(owner);
        wallet.addAllowedTarget(address(dai));
        wallet.toggleAllowList(true);
        vm.stopPrank();

        // dai is allowed; usdc is not.
        executor.callExecute(
            address(wallet), bytes32(0), _encodeSingle(address(dai), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e18)))
        );
        assertEq(dai.balanceOf(kani), 1e18, "allowed target was blocked");

        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(usdc))
        );
        executor.callExecute(
            address(wallet), bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)))
        );
    }

    /// @notice The capability still works. Without this, the guard could over-block every executor
    ///         call and every test above would still pass.
    function test_executor_legitTransferSucceedsIsMeteredAndPaysTheFee() public {
        MockExecutorModule executor = _installExecutor();
        uint256 amount = 1000e6;
        int256 expectedUsd = oracle.getPrice(address(usdc), amount);
        uint256 expectedFee = registry.getFee();
        uint256 treasuryBefore = address(treasury).balance;

        executor.callExecute(
            address(wallet), bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, amount)))
        );

        assertEq(usdc.balanceOf(kani), amount, "executor transfer did not execute");
        assertEq(wallet.getConfig().spentInWindow, expectedUsd, "executor outflow not metered");
        assertEq(address(treasury).balance, treasuryBefore + expectedFee, "executor path paid no fee");
    }

    /// @notice A module that is NOT installed cannot use the path at all -- `onlyModule` is the outer
    ///         gate, and the guard added above is the inner one.
    function test_executor_uninstalledModuleCannotExecute() public {
        MockExecutorModule rogue = new MockExecutorModule();

        vm.expectRevert();
        rogue.callExecute(
            address(wallet), bytes32(0), _encodeSingle(address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)))
        );

        assertEq(usdc.balanceOf(kani), 0, "uninstalled module moved funds");
    }

    /*//////////////////////////////////////////////////////////////
                       EXECTYPE IS NOT A WAY AROUND
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The guard fires on a restricted target even in EXECTYPE_TRY mode.
     * @dev Worth pinning explicitly, because the mode word's SECOND byte is the ExecType and nothing
     *      here inspects it. With EXECTYPE_TRY (0x01) `ERC7579Utils._call` emits `ERC7579TryExecuteFail`
     *      instead of bubbling, so a sub-call that fails leaves the transaction SUCCESSFUL. That makes
     *      it a natural place to suspect a bypass -- and a natural way to write a test that passes for
     *      the wrong reason.
     *
     *      It is not a bypass: {_guardSessionExecution} runs BEFORE {_execute}, so it reverts on the
     *      target regardless of how failures inside `_execute` would have been handled. The ExecType
     *      only ever governs what happens once execution has already been allowed to start.
     *
     *      RULE FOR THIS FILE: for any path where a failure might be swallowed -- try-mode, or an
     *      inner revert absorbed by `handleOps` -- assert the STATE never changed. Never rely on
     *      `vm.expectRevert` alone.
     */
    function test_guard_firesEvenInTryMode() public {
        bytes32 tryMode = bytes32(uint256(0x01) << 240); // CALLTYPE_SINGLE, EXECTYPE_TRY

        vm.prank(config.entryPoint);
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, config.entryPoint)
        );
        wallet.execute(
            tryMode,
            _encodeSingle(
                config.entryPoint, 0, abi.encodeWithSignature("withdrawTo(address,uint256)", attacker, 1 ether)
            )
        );

        assertEq(attacker.balance, 0, "attacker received value");
    }

    /// @notice The same for an executor: try-mode does not slip past the newly-added guard either.
    function test_executor_guardFiresEvenInTryMode() public {
        MockExecutorModule executor = _installExecutor();
        bytes32 tryMode = bytes32(uint256(0x01) << 240);
        int256 limitBefore = wallet.getConfig().dailyLimitUsd;

        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(module))
        );
        executor.callExecute(
            address(wallet),
            tryMode,
            _encodeSingle(address(module), 0, abi.encodeCall(SpendingLimitModule.setDailyLimit, (int256(1_000_000e18))))
        );

        assertEq(wallet.getConfig().dailyLimitUsd, limitBefore, "cap was raised in try mode");
    }
}
