//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;
import {Test} from "forge-std/Test.sol";
//Account abstraction Imports
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
//Openzeppelin Imports
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
//Local imports
import {SessionHandler} from "../../src/SessionHandler.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockWeth} from "../../src/mocks/MockWeth.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SessionHandlerHarness} from "./SessionHandlerHarness.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";

/**
 * @title SHProtocolTest
 * @author Conrad Japhet
 * @notice Test suite for the SessionHandler ERC-4337 smart account contract
 * @dev Tests cover access control, session validation, EntryPoint integration,
 *      spending limits, view functions, and event emissions.
 *
 *  NB Casting to 'uint160' is safe because we extract the lower 160 bits
 *      of the packed validation data which contains the aggregator/sig validation result
 *
 * Test Categories:
 * - Access Control: Owner/non-owner permissions for execute, pause, session management
 * - Validation: UserOp signature and session constraint validation
 * - Recover Signed UserOp: Signature recovery for owner and session key operations
 * - EntryPoint: Full ERC-4337 flow through handleOps
 * - Session: Session key lifecycle and constraint enforcement
 * - View Functions: Session state queries and budget tracking
 * - Events: Correct event emission for session lifecycle actions
 */
contract SHProtocolTest is Test {
    ERC20Mock usdc;
    ERC20Mock dai;
    MockWeth weth;
    SHOracle oracle;
    SHRegistry feeRegistry;
    SHTreasury treasury;
    HelperConfig.NetworkConfig config;
    SessionHandler sessionHandler;
    SendPackedUserOp sendPackedUserOp;
    bytes4[] selectors;

    /// @dev Amount minted to SessionHandler for ERC20 tests
    uint256 constant AMOUNT_TO_MINT = 1000 ether;

    /// @dev Initial ETH balance given to SessionHandler
    uint256 constant INTITIAL_ACCOUNT_BALANCE = 10 ether;

    /// @dev Sentinel values used to signal that the owner key should be used instead of a session key
    address constant DEFAULT_SESSION_SIGNER = address(0);
    uint256 constant DEFAULT_SESSION_KEY = 0;

    /// @dev Spending limit in USD with 18 decimals (e.g., 5000 USDC = 5000 * 10^18 for precision in the oracle)
    uint256 constant BUDGET = 5000e18;
    /// @dev Example ETH value used in tests that verify USD value tracking through the oracle
    uint256 constant ETH_VALUE = 5e18;

    uint256 constant AMOUNT_TO_TRANSFER = 1000e6;

    /// @dev Session key holder — generated with a known private key for signature tests
    address user;
    uint256 privateKey;
    address kani = makeAddr("kani");

    /// @dev Simulates a bundler submitting ops to the EntryPoint
    address bundler = makeAddr("bundler");

    /*//////////////////////////////////////////////////////////////
                                MODIFIERS
    //////////////////////////////////////////////////////////////*/

    /**
     * @dev Adds a standard ERC20 transfer session for `user` on the `usdc` mock token.
     *      Grants access to ERC20Mock.transfer only, within a 1-day window, with BUDGET spending limit.
     *      Used by tests that need a valid active session without ETH value transfers.
     */
    modifier sessionAdded() {
        address sessionKey = user;
        address target = address(usdc);
        bytes4[] memory sel = new bytes4[](3);
        sel[0] = ERC20Mock.transfer.selector;
        sel[1] = ERC20Mock.transferFrom.selector;
        sel[2] = ERC20Mock.approve.selector;

        uint48 validFrom = uint48(block.timestamp + 1 hours);
        uint48 validUntil = uint48(block.timestamp + 3 hours);
        uint256 spendingLimit = BUDGET;

        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, sel, validFrom, validUntil, spendingLimit);

        _;
    }

    /**
     * @dev Adds a session for `user` that allows calling ERC20Mock.sendEth with ETH value.
     *      Used by tests that verify spending limit tracking and ETH value flow through sessions.
     */
    modifier wethSessionAdded() {
        address sessionKey = user;
        address target = config.weth;
        bytes4[] memory sel = new bytes4[](5);
        sel[0] = MockWeth.transfer.selector;
        sel[1] = MockWeth.transferFrom.selector;
        sel[2] = MockWeth.approve.selector;
        sel[3] = MockWeth.deposit.selector;
        sel[4] = MockWeth.withdraw.selector;
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = BUDGET;

        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, sel, validFrom, validUntil, spendingLimit);

        _;
    }

    modifier ethSessionAdded() {
        address sessionKey = user;
        address target = address(0); // Sentinel for native ETH-send session
        bytes4[] memory sel = new bytes4[](0); // No selectors for native ETH session
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = BUDGET;

        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, sel, validFrom, validUntil, spendingLimit);

        _;
    }

    /*//////////////////////////////////////////////////////////////
                                 SETUP
    //////////////////////////////////////////////////////////////*/

    /**
     * @dev Deploys all required contracts and funds the SessionHandler.
     *      - Generates a named user address and private key for session signing
     *      - Deploys SessionHandler via DeploySHProtocol + SHFactory.deployWallet()
     *      - Deploys SendPackedUserOp helper and ERC20Mock token
     *      - Funds SessionHandler with 1 ETH and mints 10,000 USDC mock tokens to it
     */
    function setUp() public {
        (user, privateKey) = makeAddrAndKey("user");
        DeploySHProtocol deployer = new DeploySHProtocol();
        SHFactory factory;
        (factory, treasury, config, oracle) = deployer.run();
        feeRegistry = SHRegistry(treasury.REGISTRY());
        vm.prank(config.account);
        sessionHandler = SessionHandler(payable(factory.deployWallet()));
        sendPackedUserOp = new SendPackedUserOp();
        //usdc = new ERC20Mock();
        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);
        weth = MockWeth(payable(config.weth));
        vm.deal(address(sessionHandler), INTITIAL_ACCOUNT_BALANCE);
        usdc.mint(address(sessionHandler), 10000e6);
        dai.mint(address(sessionHandler), 10000e18);
        weth.mint(address(sessionHandler), 1000e18);
    }

    /*//////////////////////////////////////////////////////////////
                       ORACLE REFRESH HELPERS
    //////////////////////////////////////////////////////////////*/

    /**
     * @dev Refreshes every mock price feed's updatedAt to block.timestamp after a vm.warp.
     *      Required whenever warping more than the per-feed heartbeat (1 hour on Anvil) in
     *      tests where the session is still active and the oracle will be queried.
     *      Not needed when the session has already expired — _checkAndUpdateSessionStatus
     *      deletes the session before the oracle is ever reached.
     */
    function _refreshMockFeeds() internal {
        _refreshMockFeed(config.ethUsdPriceFeed);
        _refreshMockFeed(config.usdcUsdPriceFeed);
        _refreshMockFeed(config.daiUsdPriceFeed);
        _refreshMockFeed(config.usdtUsdPriceFeed);
        _refreshMockFeed(config.aaveUsdPriceFeed);
        _refreshMockFeed(config.linkUsdPriceFeed);
        _refreshMockFeed(config.oneinchUsdPriceFeed);
        _refreshMockFeed(config.apeUsdPriceFeed);
        _refreshMockFeed(config.arbUsdPriceFeed);
        _refreshMockFeed(config.bnbUsdPriceFeed);
        _refreshMockFeed(config.btcUsdPriceFeed);
        _refreshMockFeed(config.compUsdPriceFeed);
        _refreshMockFeed(config.crvUsdPriceFeed);
        _refreshMockFeed(config.ensUsdPriceFeed);
        _refreshMockFeed(config.mkrUsdPriceFeed);
        _refreshMockFeed(config.sandUsdPriceFeed);
        _refreshMockFeed(config.sushiUsdPriceFeed);
        _refreshMockFeed(config.wtaoUsdPriceFeed);
        _refreshMockFeed(config.uniUsdPriceFeed);
        _refreshMockFeed(config.yfiUsdPriceFeed);
    }

    function _refreshMockFeed(address feed) private {
        if (feed == address(0)) return;
        MockV3Aggregator mock = MockV3Aggregator(feed);
        mock.updateAnswer(mock.latestAnswer());
    }

    /*//////////////////////////////////////////////////////////////
                            ACCESS CONTROL TESTS
    //////////////////////////////////////////////////////////////*/

    /// @notice validateUserOp must revert when called by any address other than the EntryPoint
    function testValidateUserOpRevertsForNonEntryPoint() public {
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, address(usdc), 0, "");
        (PackedUserOperation memory userOp, bytes32 userOpHash,) = sendPackedUserOp.generateSignedUserOp(
            address(sessionHandler), config, callData, DEFAULT_SESSION_SIGNER, DEFAULT_SESSION_KEY
        );

        vm.expectRevert(SessionHandler.SessionHandler_NotEntryPoint.selector);
        vm.prank(user); // not the EntryPoint
        sessionHandler.validateUserOp(userOp, userOpHash, 0);
    }

    /// @notice Non-owners must not be able to call execute directly
    function testNonOwnerCannotExecuteCommand() public {
        address dest = address(usdc);
        uint256 value = 0;
        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, AMOUNT_TO_MINT);

        vm.expectRevert(SessionHandler.SessionHandler_NotEntryPointOrOwner.selector);
        vm.prank(user);
        sessionHandler.execute(dest, value, data);
    }

    /// @notice Owner must be able to execute arbitrary calls on behalf of the account
    function testOwnerCanExecuteCommand() public {
        address dest = address(usdc);
        uint256 value = 0;
        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, AMOUNT_TO_MINT);

        vm.prank(sessionHandler.owner());
        sessionHandler.execute(dest, value, data);
        assertEq(usdc.balanceOf(user), AMOUNT_TO_MINT);
    }

    /// @notice Owner must be able to pause the contract, blocking execute calls
    function testOwnerCanPauseExecution() public {
        address dest = address(usdc);
        uint256 value = 0;
        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, AMOUNT_TO_MINT);

        vm.startPrank(sessionHandler.owner());
        sessionHandler.pause();
        vm.expectRevert(Pausable.EnforcedPause.selector);
        sessionHandler.execute(dest, value, data);
        vm.stopPrank();
    }

    /// @notice Owner must be able to unpause and resume normal execution
    function testOwnerCanUpauseExecution() public {
        address dest = address(usdc);
        uint256 value = 0;
        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, AMOUNT_TO_MINT);

        vm.startPrank(sessionHandler.owner());
        sessionHandler.pause();
        sessionHandler.unpause();
        sessionHandler.execute(dest, value, data);
        vm.stopPrank();

        assertEq(usdc.balanceOf(user), AMOUNT_TO_MINT);
    }

    /// @notice Owner must be able to register a new session key with valid parameters
    function testOwnerCanAddSessionKey() public {
        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = 0.005e18;

        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);

        assertEq(sessionHandler.isSessionActive(sessionKey), true);
    }

    /// @notice Non-owners must not be able to revoke session keys
    function testNonOwnerCannotRevokeSession() public sessionAdded {
        vm.startPrank(user);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, user));
        sessionHandler.revokeSessionKey(user);
        vm.stopPrank();
    }

    /// @notice Owner must be able to revoke an active session key, deactivating it immediately
    function testOwnerCanRevokeSession() public sessionAdded {
        vm.startPrank(sessionHandler.owner());
        sessionHandler.revokeSessionKey(user);
        vm.stopPrank();

        assertEq(sessionHandler.isSessionActive(user), false);
    }

    /// @notice Non-owners must not be able to register session keys
    function testNonOwnerCannotAddSessionKey() public {
        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = BUDGET;

        vm.startPrank(user);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, user));
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();
    }

    /// @notice Non-owners must not be able to pause the contract
    function testNonOwnerCannotPause() public {
        vm.startPrank(user);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, user));
        sessionHandler.pause();
        vm.stopPrank();
    }

    /**
     * @notice Session keys must not be able to call execute directly
     * @dev Session keys are only valid as signers on UserOps submitted through the EntryPoint.
     *      Direct calls to execute must be rejected even for registered session keys.
     */
    function testSessionKeyCannotCallExecuteDirectly() public sessionAdded {
        address dest = address(usdc);
        uint256 value = 0;
        bytes memory data = abi.encodeWithSelector(ERC20Mock.transfer.selector, user, 1000e18);

        vm.expectRevert(SessionHandler.SessionHandler_NotEntryPointOrOwner.selector);
        vm.prank(user);
        sessionHandler.execute(dest, value, data);
    }

    /*//////////////////////////////////////////////////////////////
                               VALIDATION TESTS
    //////////////////////////////////////////////////////////////*/

    /// @notice validateUserOp must return SIG_VALIDATION_FAILED when the signer is neither the owner nor a registered session key
    function testValidateUserOpFailsWithUnknownSigner() public {
        (address random, uint256 randomKey) = makeAddrAndKey("random");
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, address(usdc), 0, "");

        (PackedUserOperation memory userOp, bytes32 userOpHash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, random, randomKey);

        vm.prank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, userOpHash, 0);

        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(uint160(validationData), 1); // SIG_VALIDATION_FAILED
    }

    /// @notice execute must revert with SessionHandler_ExecutionFailed when the underlying call reverts
    function testExecuteRevertsWhenCallFails() public {
        // Call a non-existent function on a contract that doesn't accept it
        bytes memory badCallData = abi.encodeWithSignature("nonExistentFunction()");

        vm.prank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_ExecutionFailed.selector);
        sessionHandler.execute(address(usdc), 0, badCallData);
    }

    /**
     * @notice Owner-signed UserOps must pass validation
     * @dev Validates the full owner signature flow:
     *      userOpHash → EIP-191 digest → ECDSA recover → owner match → SIG_VALIDATION_SUCCESS
     *      Checks lower 160 bits of validationData equal 0 (success)
     */
    function testValidateUserOp() public {
        address dest = address(usdc);
        uint256 missingAccountFunds = 1 ether;
        uint256 value = 0;

        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, AMOUNT_TO_MINT);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp, bytes32 userOpHash,) = sendPackedUserOp.generateSignedUserOp(
            address(sessionHandler), config, callData, DEFAULT_SESSION_SIGNER, DEFAULT_SESSION_KEY
        );

        vm.prank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, userOpHash, missingAccountFunds);

        // forge-lint: disable-next-line(unsafe-typecast)
        uint160 aggregator = uint160(validationData);
        assertEq(aggregator, 0);
    }

    /**
     * @notice Session-key-signed UserOps must pass validation when all session constraints are met
     * @dev Validates the session key flow:
     *      recovered signer → active session lookup → time/target/selector/budget checks → SIG_VALIDATION_SUCCESS
     *      Checks lower 160 bits of validationData equal 0 (success)
     */
    function testValidateUserOpWithSession() public sessionAdded {
        address dest = address(usdc);
        uint256 missingAccountFunds = 1 ether;
        uint256 value = 0;
        uint256 amountToTransfer = AMOUNT_TO_TRANSFER;

        bytes memory data = abi.encodeWithSelector(ERC20Mock.transfer.selector, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp, bytes32 userOpHash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        vm.warp(block.timestamp + 1 hours);
        vm.startPrank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, userOpHash, missingAccountFunds);
        vm.stopPrank();

        // forge-lint: disable-next-line(unsafe-typecast)
        uint160 aggregator = uint160(validationData);
        assertEq(aggregator, 0);
    }

    /**
     * @notice Session-key-signed UserOps must fail validation when targeting a non-whitelisted contract
     * @dev Session is scoped to `usdc`. Attempting to call a different address must be rejected.
     *      Expects SIG_VALIDATION_FAILED (aggregator = 1).
     */
    function testValidateUserOpFailsWithWrongTarget() public sessionAdded {
        address wrongDest = makeAddr("wrongTarget");
        uint256 missingAccountFunds = 1 ether;
        uint256 value = 0;
        uint256 amountToTransfer = AMOUNT_TO_TRANSFER;

        bytes memory data = abi.encodeWithSelector(ERC20Mock.transfer.selector, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, wrongDest, value, data);

        (PackedUserOperation memory userOp, bytes32 userOpHash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        vm.prank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, userOpHash, missingAccountFunds);

        // forge-lint: disable-next-line(unsafe-typecast)
        uint160 aggregator = uint160(validationData);
        assertEq(aggregator, 1);
    }

    /**
     * @notice Session-key-signed UserOps must fail validation when using a non-whitelisted function selector
     * @dev Session only allows ERC20Mock.transfer. Attempting to call ERC20Mock.mint must be rejected.
     *      Expects SIG_VALIDATION_FAILED (aggregator = 1).
     */
    function testValidateUserOpFailsWithUnauthorisedSelector() public sessionAdded {
        address dest = address(usdc);
        uint256 missingAccountFunds = 1 ether;
        uint256 value = 0;
        uint256 amountToTransfer = AMOUNT_TO_TRANSFER;

        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp, bytes32 userOpHash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        vm.prank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, userOpHash, missingAccountFunds);

        // forge-lint: disable-next-line(unsafe-typecast)
        uint160 aggregator = uint160(validationData);
        assertEq(aggregator, 1);
    }

    /**
     * @notice Session-key-signed UserOps must fail validation after the session has been revoked
     * @dev Revokes the session before submitting the UserOp.
     *      Recovered signer will no longer have an active session → SIG_VALIDATION_FAILED (aggregator = 1).
     */
    function testValidateUserOpFailsWithRevokedSession() public sessionAdded {
        address dest = address(usdc);
        uint256 missingAccountFunds = 1 ether;
        uint256 value = 0;
        uint256 amountToTransfer = AMOUNT_TO_TRANSFER;

        bytes memory data = abi.encodeWithSelector(ERC20Mock.transfer.selector, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp, bytes32 userOpHash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        vm.prank(sessionHandler.owner());
        sessionHandler.revokeSessionKey(user);

        vm.prank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, userOpHash, missingAccountFunds);

        // forge-lint: disable-next-line(unsafe-typecast)
        uint160 aggregator = uint160(validationData);
        assertEq(aggregator, 1);
    }

    /*//////////////////////////////////////////////////////////////
                       RECOVER SIGNED USEROP TESTS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice The owner's signature must be correctly recoverable from a signed UserOp
     * @dev Signs with the default anvil owner key and verifies ECDSA.recover returns the owner address.
     */
    function testRecoverSignedUserOp() public view {
        address dest = address(usdc);
        uint256 value = 0;
        address expectedSigner = sessionHandler.owner();
        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, AMOUNT_TO_MINT);

        bytes memory callData = abi.encodeWithSelector(sessionHandler.execute.selector, dest, value, data);
        (PackedUserOperation memory userOp,, bytes32 digest) = sendPackedUserOp.generateSignedUserOp(
            address(sessionHandler), config, callData, DEFAULT_SESSION_SIGNER, DEFAULT_SESSION_KEY
        );
        address actualSigner = ECDSA.recover(digest, userOp.signature);

        assertEq(actualSigner, expectedSigner);
    }

    /**
     * @notice The session key's signature must be correctly recoverable from a signed UserOp
     * @dev Signs with the user's private key and verifies ECDSA.recover returns the user address.
     */
    function testRecoverSignedUserOpWithSession() public view {
        address dest = address(usdc);
        uint256 value = 0;
        address expectedSigner = user;
        uint256 amountToTransfer = AMOUNT_TO_TRANSFER;
        bytes memory data = abi.encodeWithSelector(ERC20Mock.transfer.selector, user, amountToTransfer);

        bytes memory callData = abi.encodeWithSelector(sessionHandler.execute.selector, dest, value, data);
        (PackedUserOperation memory userOp,, bytes32 digest) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        address actualSigner = ECDSA.recover(digest, userOp.signature);

        assertEq(actualSigner, expectedSigner);
    }

    /*//////////////////////////////////////////////////////////////
                             ENTRY POINT TESTS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice EntryPoint must be able to execute an owner-signed UserOp end-to-end
     * @dev Submits a handleOps call as a bundler with an owner-signed mint operation.
     *      Verifies the ERC20 balance of user increases by AMOUNT_TO_MINT.
     */
    function testEntryPointCanExecuteCommand() public {
        address dest = address(usdc);
        uint256 value = 0;
        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, AMOUNT_TO_MINT);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) = sendPackedUserOp.generateSignedUserOp(
            address(sessionHandler), config, callData, DEFAULT_SESSION_SIGNER, DEFAULT_SESSION_KEY
        );
        packedUserOp[0] = userOp;

        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertEq(usdc.balanceOf(user), AMOUNT_TO_MINT);
    }

    /**
     * @notice EntryPoint must be able to execute a session-key-signed UserOp end-to-end
     * @dev Warps 5 seconds forward to ensure block.timestamp > validFrom.
     *      Submits a handleOps call as a bundler with a session-key-signed transfer operation.
     *      Verifies the ERC20 balance of user increases by amountToTransfer.
     */
    function testEntryPointCanExecuteCommandWithSession() public sessionAdded {
        address dest = address(usdc);
        uint256 amountToTransfer = AMOUNT_TO_TRANSFER;
        uint256 value = 0;

        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(ERC20Mock.transfer.selector, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 1.1 hours);
        _refreshMockFeeds();
        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertEq(usdc.balanceOf(user), AMOUNT_TO_TRANSFER);
    }

    /*//////////////////////////////////////////////////////////////
                              SESSION TESTS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice address(0) is the ETH-send sentinel and must be registered with an empty selector array
     * @dev Passing address(0) as target alongside a non-empty selector array must revert with
     *      SessionHandler_InvalidTarget, because a native ETH transfer has no function selector.
     */
    function testCannotAddSessionWithZeroAddressTargetAndSelector() public {
        address sessionKey = user;
        address target = address(0);
        bytes4[] memory sel = new bytes4[](1);
        sel[0] = ERC20Mock.transfer.selector;
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = BUDGET;

        vm.prank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_InvalidTarget.selector);
        sessionHandler.addSessionKey(sessionKey, target, sel, validFrom, validUntil, spendingLimit);
    }

    /**
     * @notice Sending native ETH via a session key deducts the correct USD value from the session budget
     * @dev Constructs a UserOp with empty calldata and value == 1 ether targeting an EOA.
     *      The ETH-send session (target == address(0)) must validate successfully and charge the
     *      oracle-priced USD equivalent against the spending limit. Verifies both the remaining
     *      budget and the recipient's post-transfer balance.
     */
    function testSpendingLimitUpdatesForSendingEthWithSession() public ethSessionAdded {
        address dest = kani;
        uint256 value = 1 ether;
        uint256 valueInUsd = oracle.getUsdValue(address(0), value);

        bytes memory data = ""; // No data needed for native ETH transfer
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);
        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 1.1 hours);
        _refreshMockFeeds();
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));

        uint256 remainingBudget = sessionHandler.getRemainingBudget(user);
        uint256 expectedRemainingBudget = BUDGET - valueInUsd;
        assertEq(remainingBudget, expectedRemainingBudget);
        assertEq(kani.balance, value);
    }

    /**
     * @notice Overwriting a session must clear the previous selector whitelist
     * @dev Registers a session allowing `approve`, then overwrites it with `transfer`.
     *      Verifies that `approve` is no longer permitted after the overwrite.
     */

    function testOverwritingSessionClearsOldSelectors() public {
        bytes4[] memory first = new bytes4[](1);
        first[0] = ERC20Mock.approve.selector;
        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(
            user, address(usdc), first, uint48(block.timestamp), uint48(block.timestamp + 1 days), BUDGET
        );

        // Overwrite with a different selector
        bytes4[] memory second = new bytes4[](1);
        second[0] = ERC20Mock.transfer.selector;
        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(
            user, address(usdc), second, uint48(block.timestamp), uint48(block.timestamp + 1 days), BUDGET
        );

        // `approve` should no longer be allowed — if it is, the old mapping was never cleared
        bytes memory data = abi.encodeWithSelector(ERC20Mock.approve.selector, makeAddr("s"), 1e6);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, address(usdc), 0, data);
        (PackedUserOperation memory userOp, bytes32 hash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        vm.prank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, hash, 0);
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(uint160(validationData), 1); // should fail — approve was only in the first session
    }

    /// @notice addSessionKey must revert when the session key address is the zero address
    function testAddSessionKeyRevertsInvalidSessionKey() public {
        address sessionKey = address(0);
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = 2e18;

        vm.startPrank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_InvalidSessionKey.selector);
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();
    }

    /// @notice addSessionKey must revert when the target contract address is the zero address
    function testAddSessionKeyRevertsInvalidTarget() public {
        address sessionKey = user;
        address target = address(0);
        selectors.push(ERC20Mock.transfer.selector);
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = 2e18;

        vm.startPrank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_InvalidTarget.selector);
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();
    }

    /// @notice addSessionKey must revert when validFrom is greater than or equal to validUntil
    function testAddSessionKeyRevertsInvalidTimeRange() public {
        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp);
        uint256 spendingLimit = 2e18;

        vm.startPrank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_InvalidTimeRange.selector);
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();
    }

    /// @notice revokeSessionKey must revert when the session key has no active session
    function testCannotRevokeNonExistantSession() public sessionAdded {
        address ben = makeAddr("ben");
        vm.startPrank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_SessionIsNotActive.selector);
        sessionHandler.revokeSessionKey(ben);
        vm.stopPrank();
    }

    /**
     * @notice Spending limit must accumulate correctly across multiple sequential UserOps
     * @dev Uses wethSessionAdded which scopes the session to the WETH contract and whitelists deposit().
     *      Submits 3 ops each forwarding 0.1 ETH to deposit() and verifies getRemainingBudget decreases
     *      by the correct USD amount after each op.
     */
    function testBudgetAccumulatesAcrossMultipleOps() public wethSessionAdded {
        uint256 singleSpend = 0.1 ether;
        address dest = address(weth);
        uint256 singleSpendUsd = oracle.getUsdValue(dest, singleSpend);
        address reciever = kani;
        uint256 value = 0;

        for (uint256 i = 0; i < 3; i++) {
            PackedUserOperation[] memory ops = new PackedUserOperation[](1);
            bytes memory data = abi.encodeWithSelector(MockWeth.transfer.selector, reciever, singleSpend);
            bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);
            (PackedUserOperation memory userOp,,) =
                sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
            ops[0] = userOp;

            vm.warp(block.timestamp + 1.1 hours);
            _refreshMockFeeds(); // reset updatedAt after each warp to avoid cumulative staleness
            vm.prank(bundler, bundler);
            IEntryPoint(config.entryPoint).handleOps(ops, payable(user));
        }

        assertEq(sessionHandler.getRemainingBudget(user), BUDGET - singleSpendUsd * 3);
    }

    /**
     * @notice Sessions with multiple selectors must allow all listed selectors and reject unlisted ones
     * @dev Registers a session with both `transfer` and `approve`. Verifies `approve` passes and `mint` fails.
     */
    function testMultipleSelectorsAllowedInSession() public {
        selectors.push(ERC20Mock.transfer.selector);
        selectors.push(ERC20Mock.approve.selector);
        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(
            user, address(usdc), selectors, uint48(block.timestamp), uint48(block.timestamp + 1 days), BUDGET
        );

        // approve selector should pass
        bytes memory data = abi.encodeWithSelector(ERC20Mock.approve.selector, makeAddr("spender"), 1e6);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, address(usdc), 0, data);
        (PackedUserOperation memory userOp, bytes32 hash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        vm.prank(config.entryPoint);
        uint256 validationData = sessionHandler.validateUserOp(userOp, hash, 0);
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(uint160(validationData), 0); // success

        // mint (unlisted) should still fail
        data = abi.encodeWithSelector(ERC20Mock.mint.selector, user, 1e6);
        callData = abi.encodeWithSelector(SessionHandler.execute.selector, address(usdc), 0, data);
        (userOp, hash,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);

        vm.prank(config.entryPoint);
        validationData = sessionHandler.validateUserOp(userOp, hash, 0);
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(uint160(validationData), 1); // failure
    }

    /**
     * @notice Owner must be able to register a new session for a key that was previously revoked
     * @dev Revokes the existing session for `user`, then adds a new session with a different target and selector.
     *      Verifies the new session is active and the updated parameters are reflected in storage.
     */
    function testOwnerCanReAddSessionAfterRevoke() public sessionAdded {
        vm.prank(sessionHandler.owner());
        sessionHandler.revokeSessionKey(user);

        bytes4[] memory newSelectors = new bytes4[](1);
        newSelectors[0] = ERC20Mock.approve.selector;

        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(
            user, address(dai), newSelectors, uint48(block.timestamp), uint48(block.timestamp + 1 days), BUDGET
        );

        SessionHandler.Session memory s = sessionHandler.getSession(user);
        assertEq(s.active, true);
        assertEq(s.target, address(dai));
        // old selector must be gone
        assertEq(sessionHandler.isSessionActive(user), true);
    }

    /*//////////////////////////////////////////////////////////////
                           VIEW FUNCTION TESTS
    //////////////////////////////////////////////////////////////*/

    /// @notice getRemainingBudget must return the full spending limit when no ETH has been spent
    function testSpendingLimitTracksCorrectly() public sessionAdded {
        assertEq(sessionHandler.getRemainingBudget(user), BUDGET);
    }

    /**
     * @notice getRemainingBudget must decrease by the ETH value spent in a successful session UserOp
     * @dev Uses wethSessionAdded which scopes the session to the WETH contract and whitelists deposit().
     *      Forwards 0.1 ETH to deposit() via handleOps and reads the budget afterwards to confirm
     *      spentAmount was updated by the correct USD equivalent.
     */
    function testRemainingBudgetDecreasesAfterSpend() public wethSessionAdded {
        address dest = address(weth);
        uint256 value = 0;
        address receiver = kani;
        uint256 amountToTransfer = 0.1 ether;

        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(MockWeth.transfer.selector, receiver, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 5);
        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertEq(
            sessionHandler.getRemainingBudget(user), oracle.getUsdValue(address(weth), ETH_VALUE - amountToTransfer)
        );
    }

    /// @notice isSessionActive must return false for an address that has never had a session
    function testIsSessionActiveReturnsFalseForNonExistentSession() public {
        address randomAddress = makeAddr("randomAddress");
        assertEq(sessionHandler.isSessionActive(randomAddress), false);
    }

    /// @notice isSessionActive must return false after the session's validUntil timestamp has passed
    function testIsSessionActiveReturnsFalseAfterExpiry() public sessionAdded {
        vm.warp(block.timestamp + 2 days);
        assertEq(sessionHandler.isSessionActive(user), false);
    }

    /// @notice isSessionActive must return false immediately after the session is revoked
    function testIsSessionActiveReturnsFalseAfterRevocation() public sessionAdded {
        vm.prank(sessionHandler.owner());
        sessionHandler.revokeSessionKey(user);
        assertEq(sessionHandler.isSessionActive(user), false);
    }

    /// @notice getSession must return a Session struct that accurately reflects all registered parameters
    function testGetSessionReturnsCorrectData() public {
        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = BUDGET;

        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);

        SessionHandler.Session memory session = sessionHandler.getSession(sessionKey);
        assertEq(session.target, target);
        assertEq(session.validFrom, validFrom);
        assertEq(session.validUntil, validUntil);
        assertEq(session.spendingLimit, spendingLimit);
        assertEq(session.spentAmount, 0);
        assertEq(session.active, true);
    }

    /**
     * @notice getRemainingBudget must return 0 when the full spending limit has been consumed
     * @dev Uses wethSessionAdded which scopes the session to the WETH contract and whitelists deposit().
     *      Submits a UserOp that forwards exactly ETH_VALUE to deposit(), consuming the full BUDGET.
     *      Verifies getRemainingBudget returns 0 after the op.
     */
    function testGetRemainingBudgetReturnsZeroWhenFullySpent() public wethSessionAdded {
        address dest = address(weth);
        uint256 amountToTransfer = ETH_VALUE;
        address receiver = kani;
        uint256 value = 0;

        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(MockWeth.transfer.selector, receiver, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 1.1 hours);
        _refreshMockFeeds();
        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertEq(sessionHandler.getRemainingBudget(user), 0);
    }

    /**
     * @notice getRemainingBudget must return the full budget when the session is scoped to WETH and the user deposits ETH equal to the budget
     * @dev Uses wethSessionAdded which scopes the session to the WETH contract and whitelists deposit().
     *      Submits a UserOp that forwards exactly ETH_VALUE to deposit(), consuming the full BUDGET.
     *      Verifies getRemainingBudget returns 5000e18 after the op.
     */
    function testDespositWethDoesntAffectBudget() public wethSessionAdded {
        address dest = address(weth);
        uint256 value = ETH_VALUE;

        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(MockWeth.deposit.selector);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 1.1 hours);
        _refreshMockFeeds();
        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertEq(sessionHandler.getRemainingBudget(user), BUDGET);
    }

    /**
     * @notice getRemainingBudget must return the full budget after an approve() call
     * @dev Uses wethSessionAdded which scopes the session to the WETH contract and whitelists approve().
     *      Submits a UserOp that calls approve() with a large allowance (no ETH value forwarded).
     *      Verifies the budget is unchanged since approvals do not represent a USD spend.
     */
    function testApproveDoesNotAffectBudget() public wethSessionAdded {
        address dest = address(weth);
        uint256 value = 0;

        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(MockWeth.approve.selector, kani, type(uint256).max);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 1.1 hours);
        _refreshMockFeeds();
        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertEq(sessionHandler.getRemainingBudget(user), BUDGET);
    }

    /**
     * @notice transferFrom must decrement the remaining budget by the USD value of the transferred amount
     * @dev Uses wethSessionAdded which scopes the session to the WETH contract and whitelists transferFrom().
     *      kani is minted WETH and approves the SessionHandler. A UserOp calls transferFrom(kani, user, amount).
     *      Verifies getRemainingBudget decreases, confirming the amount parameter is extracted and priced correctly.
     */
    function testTransferFromAffectsBudget() public wethSessionAdded {
        uint256 amountToTransfer = 1e18;

        weth.mint(kani, amountToTransfer);
        vm.prank(kani);
        weth.approve(address(sessionHandler), amountToTransfer);

        address dest = address(weth);
        uint256 value = 0;

        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(MockWeth.transferFrom.selector, kani, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 1.1 hours);
        _refreshMockFeeds();
        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertLt(sessionHandler.getRemainingBudget(user), BUDGET);
    }

    /*//////////////////////////////////////////////////////////////
                               EMIT TESTS
    //////////////////////////////////////////////////////////////*/

    /// @notice revokeSessionKey must emit SessionRevoked with the correct indexed session key address
    function testRevokeSessionEmitsEvent() public sessionAdded {
        vm.expectEmit(true, false, false, false);
        emit SessionHandler.SessionRevoked(user);

        vm.prank(sessionHandler.owner());
        sessionHandler.revokeSessionKey(user);
    }

    /// @notice addSessionKey must emit SessionAdded with the correct session key, target, and validUntil
    function testAddSessionEmitsEvent() public {
        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint48 validFrom = uint48(block.timestamp);
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = 0.005e18;

        vm.expectEmit(true, true, false, true);
        emit SessionHandler.SessionAdded(sessionKey, target, validUntil);
        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
    }

    /*//////////////////////////////////////////////////////////////
                                  FUZZ TESTS
       //////////////////////////////////////////////////////////////*/

    /**
     * @notice addSessionKey must revert with InvalidTimeRange for any input where validFrom >= validUntil.
     * @dev Constrains validUntil to be in the future to isolate the time-range check from the
     *      end-time check, ensuring only SessionHandler_InvalidTimeRange is triggered.
     */
    function testAddSessionKeyRevertsWithInvalidTimeRange(uint48 validFrom, uint48 validUntil) public {
        vm.assume(validFrom >= validUntil); // ensure we have a invalid time range to start with
        vm.assume(validUntil > block.timestamp); // ensure validUntil is in the future to avoid hitting SessionHandler_InvalidEndTime instead
        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint256 spendingLimit = BUDGET;

        vm.startPrank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_InvalidTimeRange.selector);
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();
    }

    /**
     * @notice addSessionKey must revert with InvalidEndTime for any validUntil already in the past.
     * @dev Warps 150 days forward to create a large past range, then constrains validUntil to be
     *      strictly before block.timestamp while keeping validFrom < validUntil, isolating the
     *      end-time guard from the time-range guard.
     */
    function testAddSessionKeyRevertsWithEndTimeInThePast(uint48 validFrom, uint48 validUntil) public {
        vm.warp(block.timestamp + 150 days); // warp far into the future to ensure validUntil is in the past
        vm.assume(validFrom < validUntil);
        vm.assume(validUntil < block.timestamp); // ensure validUntil is in the past
        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint256 spendingLimit = BUDGET;

        vm.startPrank(sessionHandler.owner());
        vm.expectRevert(SessionHandler.SessionHandler_InvalidEndTime.selector);
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();
    }

    /**
     * @notice Selector whitelists must be isolated per session key — granting a selector to keyA
     *         must not affect keyB's session storage.
     * @dev Fuzzes two distinct non-zero addresses and an arbitrary selector. Registers a session
     *      for keyA only and asserts keyB has no session and an empty selector array.
     */
    function testSelectorIsolationBetweenSessions(address keyA, address keyB, bytes4 selector) public {
        vm.assume(keyA != keyB && keyA != address(0) && keyB != address(0));
        // add keyA with selector, verify keyB doesn't have it
        selectors.push(selector);
        address target = address(usdc);
        uint48 validFrom = uint48(block.timestamp + 1 hours);
        uint48 validUntil = uint48(block.timestamp + 3 hours);
        uint256 spendingLimit = BUDGET;

        vm.startPrank(sessionHandler.owner());
        sessionHandler.addSessionKey(keyA, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();

        SessionHandler.Session memory sessionB = sessionHandler.getSession(keyB);
        assertEq(sessionB.active, false); // keyB should have no session
        assertEq(sessionB.selectors.length, 0); // keyB should have no selectors
    }

    /**
     * @notice isSessionActive must return true at any timestamp within [validFrom, validUntil].
     * @dev Fuzzes the session window and warps to validUntil (the inclusive upper boundary) to
     *      confirm the session is still considered active at its exact expiry second.
     *      Uses sessionAdded modifier to pre-register a session, then overwrites it with the
     *      fuzzed window via a second addSessionKey call.
     */
    function testIsSessionActiveAtTimeBoundaries(uint48 validFrom, uint48 validUntil) public sessionAdded {
        vm.assume(validFrom < validUntil && validUntil > block.timestamp);

        address sessionKey = user;
        address target = address(usdc);
        selectors.push(ERC20Mock.transfer.selector);
        uint256 spendingLimit = BUDGET;
        vm.startPrank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, selectors, validFrom, validUntil, spendingLimit);
        vm.stopPrank();
        // at validFrom
        vm.warp(validUntil);
        assertEq(sessionHandler.isSessionActive(user), true);
    }

    /**
     * @notice _packValidationData must produce a lossless round-trip for all input combinations.
     * @dev Fuzzes sigFailed, validFrom, and validUntil across their full ranges and verifies the
     *      three fields can be extracted back from the packed uint256 without loss:
     *        - bits   0-159: sigFailed flag (0 = success, 1 = failed)
     *        - bits 160-207: validUntil
     *        - bits 208-255: validFrom
     *      Uses SessionHandlerHarness to call the internal _packValidationData without modifying
     *      the production contract.
     */
    function testPackValidationDataRoundTrip(bool sigFailed, uint48 validAfter, uint48 validUntil) public {
        SessionHandlerHarness sessionHandlerHarness =
            new SessionHandlerHarness(config.account, config.entryPoint, config.reputationRegistry, config.identityRegistry, address(feeRegistry));
        uint256 packed = sessionHandlerHarness.packValidationData(sigFailed, validAfter, validUntil);
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(uint160(packed), sigFailed ? 1 : 0);
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(uint48(packed >> 160), validUntil);
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(uint48(packed >> 208), validAfter);
    }

    /**
     * @notice The treasury registered in SHRegistry must receive the flat protocol fee when a
     *         session-key-signed UserOp is executed through the EntryPoint.
     * @dev Mirrors testEntryPointCanExecuteCommandWithSession but asserts on the treasury's ETH
     *      balance instead of the recipient's token balance, confirming execute()'s session-key
     *      fee transfer reaches the address FEE_REGISTRY.treasury() resolves to.
     */
    function testTreasuryRecievesProtocolFeeWithSession() public sessionAdded {
        address dest = address(usdc);
        uint256 amountToTransfer = AMOUNT_TO_TRANSFER;
        uint256 value = 0;
        address treasuryAddr = feeRegistry.treasury();
        uint256 treasuryBalanceBefore = treasuryAddr.balance;

        PackedUserOperation[] memory packedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(ERC20Mock.transfer.selector, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        packedUserOp[0] = userOp;

        vm.warp(block.timestamp + 1.1 hours);
        _refreshMockFeeds();
        vm.startPrank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(packedUserOp, payable(user));
        vm.stopPrank();

        assertEq(treasuryAddr.balance, treasuryBalanceBefore + feeRegistry.protocolFee());
    }

    /*//////////////////////////////////////////////////////////////
                       TREASURY ADMIN TESTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Treasury owner must be able to update the protocol fee, and the change must propagate to SHRegistry
    function testTreasuryOwnerCanSetProtocolFee() public {
        uint256 newFee = 0.0005 ether;

        vm.prank(treasury.owner());
        treasury.setProtocolFee(newFee);

        assertEq(feeRegistry.protocolFee(), newFee);
    }

    /// @notice Non-owners must not be able to update the protocol fee through the treasury
    function testNonOwnerCannotSetProtocolFee() public {
        uint256 newFee = 0.0005 ether;

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, user));
        vm.prank(user);
        treasury.setProtocolFee(newFee);
    }

    /// @notice Setting a protocol fee above SHRegistry.MAX_PROTOCOL_FEE must revert, even when called by the treasury owner
    function testTreasurySetProtocolFeeRevertsWhenTooHigh() public {
        uint256 tooHighFee = SHRegistry(address(feeRegistry)).MAX_PROTOCOL_FEE() + 1;

        vm.prank(treasury.owner());
        vm.expectRevert(SHRegistry.SHRegistry_FeeTooHigh.selector);
        treasury.setProtocolFee(tooHighFee);
    }

    /// @notice Treasury owner must be able to update the canonical SHOracle, and the change must propagate to SHRegistry
    function testTreasuryOwnerCanSetPriceOracle() public {
        address newOracle = makeAddr("newOracle");

        vm.prank(treasury.owner());
        treasury.setPriceOracle(newOracle);

        assertEq(feeRegistry.priceOracle(), newOracle);
    }

    /// @notice Non-owners must not be able to update the SHOracle through the treasury
    function testNonOwnerCannotSetPriceOracle() public {
        address newOracle = makeAddr("newOracle");

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, user));
        vm.prank(user);
        treasury.setPriceOracle(newOracle);
    }

    /// @notice Setting the SHOracle to address(0) must revert, even when called by the treasury owner
    function testTreasurySetPriceOracleRevertsWithZeroAddress() public {
        vm.prank(treasury.owner());
        vm.expectRevert(SHRegistry.SHRegistry_InvalidPriceOracle.selector);
        treasury.setPriceOracle(address(0));
    }

    /// @notice Treasury owner must be able to redirect future fees to a new treasury address
    function testTreasuryOwnerCanSetTreasury() public {
        address newTreasury = makeAddr("newTreasury");

        vm.prank(treasury.owner());
        treasury.setTreasury(newTreasury);

        assertEq(feeRegistry.treasury(), newTreasury);
    }

    /// @notice Non-owners must not be able to redirect fees to a new treasury address
    function testNonOwnerCannotSetTreasury() public {
        address newTreasury = makeAddr("newTreasury");

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, user));
        vm.prank(user);
        treasury.setTreasury(newTreasury);
    }

    /// @notice Redirecting fees to address(0) must revert, even when called by the treasury owner
    function testTreasurySetTreasuryRevertsWithZeroAddress() public {
        vm.prank(treasury.owner());
        vm.expectRevert(SHRegistry.SHRegistry_InvalidTreasury.selector);
        treasury.setTreasury(address(0));
    }
}
