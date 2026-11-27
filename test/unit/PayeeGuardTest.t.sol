// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

import {Test} from "forge-std/Test.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {
    IERC7579Execution,
    MODULE_TYPE_EXECUTOR,
    Execution
} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {PayeeGuard} from "../../src/PayeeGuard.sol";
import {AgentModule} from "../../src/AgentModule.sol";
import {IAppCheck} from "../../src/interfaces/IAppCheck.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockExecutorModule} from "../../src/mocks/MockExecutorModule.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {PasskeyHelper} from "../utils/PasskeyHelper.sol";
import {ContactPassHelper} from "../utils/ContactPassHelper.sol";
import {OpenAppCheck, ScriptedAppCheck} from "../utils/TestAppChecks.sol";

/// @dev Something for a passkey to call: an app stand-in whose check the test scripts.
contract MockApp {
    uint256 public pokes;

    function poke() external payable {
        pokes++;
    }
}

/**
 * @title PayeeGuardTest
 * @notice The contact rule and the trade floors, end to end through the wallet's spending hook: who is
 *         held to them, what a pass must be, the token rules, app checks and their delay, and the void.
 */
contract PayeeGuardTest is Test {
    using PasskeyHelper for PasskeyHelper.Passkey;

    uint256 constant ANVIL_OWNER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    int256 constant DAILY_LIMIT = 1_000_000e18;
    uint256 constant WINDOW = 1 days;
    bytes32 constant SINGLE = bytes32(0);
    bytes32 constant BATCH = bytes32(uint256(0x01) << 248);

    SHFactory factory;
    SHTreasury treasury;
    HelperConfig.NetworkConfig config;
    SHOracle oracle;
    SHRegistry registry;
    SpendingLimitModule module;
    PayeeGuard guard;
    SessionHandler wallet;
    ERC20Mock usdc;
    ERC20Mock dai;
    SendPackedUserOp sendPackedUserOp;
    PasskeyHelper.Passkey sessionKey;
    address owner;
    address bob = makeAddr("bob");
    address stranger = makeAddr("stranger");
    address bundler = makeAddr("bundler");
    uint256 strangerKey;

    function setUp() public {
        (stranger, strangerKey) = makeAddrAndKey("stranger");
        sessionKey = PasskeyHelper.create("sessionKey");
        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        registry = factory.REGISTRY();
        module = SpendingLimitModule(registry.spendingLimitModule());
        guard = module.PAYEE_GUARD();
        owner = config.account;
        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);
        sendPackedUserOp = new SendPackedUserOp();

        address[] memory watched = new address[](2);
        watched[0] = address(usdc);
        watched[1] = address(dai);
        vm.prank(owner);
        wallet = SessionHandler(
            payable(factory.deployWallet(
                    DAILY_LIMIT,
                    WINDOW,
                    watched,
                    true,
                    sessionKey.qx,
                    sessionKey.qy,
                    uint48(block.timestamp + 30 days),
                    new address[](0)
                ))
        );
        vm.deal(address(wallet), 10 ether);
        usdc.mint(address(wallet), 100_000e6);
        dai.mint(address(wallet), 100_000e18);
        vm.deal(bundler, 1 ether);
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    function _single(address target, uint256 value, bytes memory data) internal pure returns (bytes memory) {
        return abi.encodePacked(target, value, data);
    }

    function _transfer(ERC20Mock token, address to, uint256 amount) internal pure returns (bytes memory) {
        return _single(address(token), 0, abi.encodeCall(IERC20.transfer, (to, amount)));
    }

    function _one(bytes memory pass) internal pure returns (bytes[] memory passes) {
        passes = new bytes[](1);
        passes[0] = pass;
    }

    function _none() internal pure returns (bytes[] memory) {
        return new bytes[](0);
    }

    function _pass(address payee) internal view returns (bytes memory) {
        return ContactPassHelper.pass(ANVIL_OWNER_KEY, payee);
    }

    function _callData(bytes32 mode, bytes memory executionCalldata, bytes[] memory passes)
        internal
        pure
        returns (bytes memory)
    {
        return ContactPassHelper.append(abi.encodeCall(IERC7579Execution.execute, (mode, executionCalldata)), passes);
    }

    /// @dev Runs `callData` on the wallet as `caller`, bubbling any revert so vm.expectRevert sees it.
    function _call(address caller, bytes memory callData) internal {
        vm.prank(caller);
        (bool ok, bytes memory ret) = address(wallet).call(callData);
        if (!ok) {
            assembly {
                revert(add(ret, 32), mload(ret))
            }
        }
    }

    /// @dev What a passkey op runs: execute() as the EntryPoint, with `passes`.
    function _asPasskey(bytes32 mode, bytes memory executionCalldata, bytes[] memory passes) internal {
        _call(config.entryPoint, _callData(mode, executionCalldata, passes));
    }

    function _notAContact(address payee) internal pure returns (bytes memory) {
        return abi.encodeWithSelector(PayeeGuard.PayeeGuard_NotAContact.selector, payee);
    }

    /// @dev Registers `check` for `app` through the treasury, past the delay, and back to now.
    function _register(address app, address check) internal {
        uint256 start = vm.getBlockTimestamp();
        vm.startPrank(owner);
        treasury.proposeAppCheck(app, check);
        vm.warp(start + registry.APP_CHECK_TIMELOCK());
        treasury.commitAppCheck(app);
        vm.stopPrank();
        vm.warp(start);
    }

    /// @dev A MockApp with a scripted check registered for it.
    function _scriptedApp() internal returns (MockApp app, ScriptedAppCheck check) {
        app = new MockApp();
        check = new ScriptedAppCheck(address(app));
        _register(address(app), address(check));
    }

    function _poke(MockApp app) internal pure returns (bytes memory) {
        return _single(address(app), 0, abi.encodeCall(MockApp.poke, ()));
    }

    /*//////////////////////////////////////////////////////////////
                            CONTACT PASSES
    //////////////////////////////////////////////////////////////*/

    /// @notice THE property: a passkey's op cannot send native value to an address the owner never
    ///         signed for, whatever Mitfah's server put in it.
    function test_plainSend_toANonContact_isRefused() public {
        vm.expectRevert(_notAContact(stranger));
        _asPasskey(SINGLE, _single(stranger, 1 ether, ""), _none());
        assertEq(stranger.balance, 0);
    }

    function test_plainSend_toAContact_goesThrough() public {
        _asPasskey(SINGLE, _single(bob, 1 ether, ""), _one(_pass(bob)));
        assertEq(bob.balance, 1 ether);
    }

    /// @notice With no footer at all (calldata as it was before passes), a payment is refused the same.
    function test_noFooter_isNoPasses() public {
        vm.expectRevert(_notAContact(bob));
        _call(config.entryPoint, abi.encodeCall(IERC7579Execution.execute, (SINGLE, _single(bob, 1 ether, ""))));
    }

    /// @notice Only the wallet's OWNER can make a pass: one signed by anyone else (a hacked server's own
    ///         key, say) is no pass at all.
    function test_pass_signedBySomeoneElse_isRefused() public {
        bytes memory forged = ContactPassHelper.pass(strangerKey, stranger);
        vm.expectRevert(_notAContact(stranger));
        _asPasskey(SINGLE, _single(stranger, 1 ether, ""), _one(forged));
    }

    /// @notice A pass names one payee: bob's pass can't pay someone else.
    function test_pass_forAnotherPayee_doesNotCount() public {
        vm.expectRevert(_notAContact(stranger));
        _asPasskey(SINGLE, _single(stranger, 1 ether, ""), _one(_pass(bob)));
    }

    /// @notice Every byte of a pass is signed: a different name (or date) breaks the signature.
    function test_pass_tampered_isRefused() public {
        bytes memory pass = _pass(bob);
        pass[30] = bytes1(uint8(pass[30]) ^ 0x01); // inside the name hash
        vm.expectRevert(_notAContact(bob));
        _asPasskey(SINGLE, _single(bob, 1 ether, ""), _one(pass));

        pass = _pass(bob);
        pass[27] = bytes1(uint8(pass[27]) ^ 0x01); // inside issuedAt
        vm.expectRevert(_notAContact(bob));
        _asPasskey(SINGLE, _single(bob, 1 ether, ""), _one(pass));
    }

    /// @notice A pass may be dated at most a day ahead, so it can't be made to outlive a void.
    function test_pass_datedTooFarAhead_isRefused() public {
        uint64 limit = uint64(vm.getBlockTimestamp() + guard.MAX_ISSUE_AHEAD());
        _asPasskey(SINGLE, _single(bob, 1, ""), _one(ContactPassHelper.pass(ANVIL_OWNER_KEY, bob, limit)));

        vm.expectRevert(_notAContact(bob));
        _asPasskey(SINGLE, _single(bob, 1, ""), _one(ContactPassHelper.pass(ANVIL_OWNER_KEY, bob, limit + 1)));
    }

    /// @notice One pass serves every call in a batch that pays the same contact.
    function test_onePass_coversABatch() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(bob, 0.1 ether, "");
        execs[1] = Execution(address(usdc), 0, abi.encodeCall(IERC20.transfer, (bob, 5e6)));
        _asPasskey(BATCH, ERC7579Utils.encodeBatch(execs), _one(_pass(bob)));
        assertEq(bob.balance, 0.1 ether);
        assertEq(usdc.balanceOf(bob), 5e6);
    }

    /// @notice A batch is refused whole if any one call pays a non-contact.
    function test_batch_withOneNonContact_isRefusedWhole() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(bob, 0.1 ether, "");
        execs[1] = Execution(stranger, 0.1 ether, "");
        vm.expectRevert(_notAContact(stranger));
        _asPasskey(BATCH, ERC7579Utils.encodeBatch(execs), _one(_pass(bob)));
        assertEq(bob.balance, 0);
    }

    /// @notice Moving value to the wallet itself needs no pass.
    function test_payingTheWalletItself_needsNoPass() public {
        _asPasskey(SINGLE, _transfer(usdc, address(wallet), 1e6), _none());
    }

    function test_footer_withMoreThanTheMaximum_isRefused() public {
        bytes[] memory passes = new bytes[](guard.MAX_PASSES() + 1);
        for (uint256 i = 0; i < passes.length; i++) {
            passes[i] = _pass(bob);
        }
        vm.expectRevert(PayeeGuard.PayeeGuard_BadPasses.selector);
        _asPasskey(SINGLE, _single(bob, 1, ""), passes);
    }

    /// @notice A footer claiming more passes than the calldata holds is refused, not read out of bounds.
    function test_footer_claimingMoreThanTheCalldataHolds_isRefused() public {
        bytes memory callData = abi.encodePacked(
            abi.encodeCall(IERC7579Execution.execute, (SINGLE, _single(bob, 1, ""))), bytes1(uint8(8)), bytes4(0x4d465031)
        );
        // 8 passes would be 1000 bytes; the whole calldata is far shorter.
        vm.expectRevert(PayeeGuard.PayeeGuard_BadPasses.selector);
        _call(config.entryPoint, callData);
    }

    /// @notice A pass is only as good as its signer still owning the wallet: hand the wallet over and
    ///         the old owner's passes stop working.
    function test_ownerChange_voidsTheOldOwnersPasses() public {
        bytes memory pass = _pass(bob);
        vm.prank(owner);
        wallet.transferOwnership(stranger);
        vm.expectRevert(_notAContact(bob));
        _asPasskey(SINGLE, _single(bob, 1, ""), _one(pass));
    }

    /// @notice The digest the owner signs is the one Mitfah's app asks for: pinned with
    ///         app/tests/test_contact_passes.py on the same inputs. Change one, change both.
    function test_digest_matchesTheApp() public view {
        assertEq(
            guard.contactDigest(0x70997970C51812dc3A010C7d01b50e0d17dc79C8, 1_800_000_000, keccak256("sandy")),
            0xbc69de402d981b481e44c0ba738d8550d96537e4919c0c1da3b576f2d41f139b
        );
    }

    /*//////////////////////////////////////////////////////////////
                                 VOID
    //////////////////////////////////////////////////////////////*/

    /// @notice After a void, every pass issued until then is refused; one issued later works.
    function test_void_refusesOlderPassesAndAcceptsNewerOnes() public {
        bytes memory old = _pass(bob);
        vm.warp(vm.getBlockTimestamp() + 10);
        vm.expectEmit(address(module));
        emit SpendingLimitModule.ContactPassesVoided(address(wallet), uint64(vm.getBlockTimestamp()));
        vm.prank(owner);
        module.voidContactPasses(address(wallet));
        assertEq(module.contactsVoidedAt(address(wallet)), vm.getBlockTimestamp());

        vm.expectRevert(_notAContact(bob));
        _asPasskey(SINGLE, _single(bob, 1, ""), _one(old));

        // Issued in the very second of the void: still void.
        vm.expectRevert(_notAContact(bob));
        _asPasskey(SINGLE, _single(bob, 1, ""), _one(_pass(bob)));

        vm.warp(vm.getBlockTimestamp() + 1);
        _asPasskey(SINGLE, _single(bob, 1, ""), _one(_pass(bob)));
        assertEq(bob.balance, 1);
    }

    /// @notice Only the owner, calling directly, can void. The wallet itself (where a passkey would act)
    ///         can't, and nor can anyone else.
    function test_void_onlyTheOwner() public {
        vm.expectRevert(
            abi.encodeWithSelector(SpendingLimitModule.SpendingLimitModule_NotOwner.selector, address(wallet), stranger)
        );
        vm.prank(stranger);
        module.voidContactPasses(address(wallet));

        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_NotOwner.selector, address(wallet), address(wallet)
            )
        );
        vm.prank(address(wallet));
        module.voidContactPasses(address(wallet));

        vm.expectRevert(SpendingLimitModule.SpendingLimitModule_NotInstalled.selector);
        vm.prank(owner);
        module.voidContactPasses(stranger);
    }

    /// @notice A passkey can't void through its own op either: the wallet's guard keeps every non-owner
    ///         call off the module.
    function test_void_unreachableByAPasskey() public {
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(module))
        );
        _asPasskey(
            SINGLE,
            _single(address(module), 0, abi.encodeCall(SpendingLimitModule.voidContactPasses, (address(wallet)))),
            _none()
        );
    }

    /// @notice A void is per wallet: voiding one leaves every other wallet's passes as they were (on
    ///         another chain the owner's wallet is a different wallet, so each chain needs its own void).
    function test_void_isPerWallet() public {
        vm.prank(stranger);
        SessionHandler second = SessionHandler(
            payable(factory.deployWallet(
                    DAILY_LIMIT, WINDOW, new address[](0), true, bytes32(0), bytes32(0), 0, new address[](0)
                ))
        );
        vm.deal(address(second), 1 ether);
        bytes memory strangersPass = ContactPassHelper.pass(strangerKey, bob);
        vm.warp(vm.getBlockTimestamp() + 1);
        vm.prank(owner);
        module.voidContactPasses(address(wallet));

        assertEq(module.contactsVoidedAt(address(second)), 0);
        vm.prank(config.entryPoint);
        (bool ok,) = address(second).call(_callData(SINGLE, _single(bob, 1, ""), _one(strangersPass)));
        assertTrue(ok, "another wallet's pass stopped working");
    }

    /*//////////////////////////////////////////////////////////////
                              TOKEN RULES
    //////////////////////////////////////////////////////////////*/

    function test_transfer_toANonContact_isRefused() public {
        vm.expectRevert(_notAContact(stranger));
        _asPasskey(SINGLE, _transfer(usdc, stranger, 1e6), _none());
    }

    function test_transfer_toAContact_goesThrough() public {
        _asPasskey(SINGLE, _transfer(usdc, bob, 1e6), _one(_pass(bob)));
        assertEq(usdc.balanceOf(bob), 1e6);
    }

    /// @notice transferFrom moves tokens someone approved to the wallet: the recipient is held, not the
    ///         source.
    function test_transferFrom_isHeldByItsRecipient() public {
        usdc.mint(stranger, 10e6);
        vm.prank(stranger);
        usdc.approve(address(wallet), 10e6);

        vm.expectRevert(_notAContact(stranger));
        _asPasskey(
            SINGLE,
            _single(address(usdc), 0, abi.encodeCall(IERC20.transferFrom, (stranger, stranger, 1e6))),
            _none()
        );
        _asPasskey(
            SINGLE,
            _single(address(usdc), 0, abi.encodeCall(IERC20.transferFrom, (stranger, address(wallet), 1e6))),
            _none()
        );
        assertEq(usdc.balanceOf(stranger), 9e6);
    }

    /// @notice An allowance goes only to a trusted spender: an attacker's contract can't be handed one
    ///         to pull from in the same transaction.
    function test_approve_toAnUntrustedSpender_isRefused() public {
        vm.expectRevert(
            abi.encodeWithSelector(PayeeGuard.PayeeGuard_UntrustedSpender.selector, address(usdc), stranger)
        );
        _asPasskey(SINGLE, _single(address(usdc), 0, abi.encodeCall(IERC20.approve, (stranger, 1e6))), _none());

        vm.expectRevert(
            abi.encodeWithSelector(PayeeGuard.PayeeGuard_UntrustedSpender.selector, address(usdc), stranger)
        );
        _asPasskey(
            SINGLE, _single(address(usdc), 0, abi.encodeWithSignature("increaseAllowance(address,uint256)", stranger, 1)), _none()
        );
    }

    function test_approve_toATrustedSpender_passesTheGuard() public {
        vm.prank(owner);
        wallet.addTrustedSpender(bob);
        // A zero approval, so the limit module has nothing left standing to refuse.
        _asPasskey(SINGLE, _single(address(usdc), 0, abi.encodeCall(IERC20.approve, (bob, 0))), _none());
    }

    /// @notice The guard's own decisions for calls a mock token can't run, asked directly.
    function test_guardDirectly_tokenEdgeCases() public {
        bytes memory clearNftApproval = _callData(
            SINGLE, _single(address(usdc), 0, abi.encodeWithSignature("approve(address,uint256)", address(0), 7)), _none()
        );
        bytes memory revoke = _callData(
            SINGLE,
            _single(address(usdc), 0, abi.encodeWithSignature("setApprovalForAll(address,bool)", stranger, false)),
            _none()
        );
        bytes memory grant = _callData(
            SINGLE,
            _single(address(usdc), 0, abi.encodeWithSignature("setApprovalForAll(address,bool)", stranger, true)),
            _none()
        );
        vm.startPrank(address(module));
        guard.check(address(wallet), 0, clearNftApproval);
        guard.check(address(wallet), 0, revoke);
        vm.expectRevert(abi.encodeWithSelector(PayeeGuard.PayeeGuard_ApprovalForAll.selector, address(usdc)));
        guard.check(address(wallet), 0, grant);
        vm.stopPrank();
    }

    /// @notice NFT-style transfers are held to their recipient too.
    function test_safeTransfers_areHeldByTheirRecipient() public {
        bytes[3] memory calls = [
            abi.encodeWithSignature("safeTransferFrom(address,address,uint256)", address(wallet), stranger, 1),
            abi.encodeWithSignature(
                "safeTransferFrom(address,address,uint256,bytes)", address(wallet), stranger, 1, bytes("")
            ),
            abi.encodeWithSignature(
                "safeTransferFrom(address,address,uint256,uint256,bytes)", address(wallet), stranger, 1, 1, bytes("")
            )
        ];
        vm.startPrank(address(module));
        for (uint256 i = 0; i < calls.length; i++) {
            vm.expectRevert(_notAContact(stranger));
            guard.check(address(wallet), 0, _callData(SINGLE, _single(address(usdc), 0, calls[i]), _none()));
        }
        vm.stopPrank();
    }

    function test_tokenFunction_carryingValue_isRefused() public {
        vm.expectRevert(abi.encodeWithSelector(PayeeGuard.PayeeGuard_ValueOnTokenCall.selector, address(usdc)));
        _asPasskey(
            SINGLE, _single(address(usdc), 1, abi.encodeCall(IERC20.transfer, (bob, 1e6))), _one(_pass(bob))
        );
    }

    /*//////////////////////////////////////////////////////////////
                         EVERYTHING ELSE: REFUSED
    //////////////////////////////////////////////////////////////*/

    /// @notice A call to a contract with no app check is refused, whatever it is.
    function test_callToAnAppWithoutACheck_isRefused() public {
        MockApp app = new MockApp();
        vm.expectRevert(
            abi.encodeWithSelector(PayeeGuard.PayeeGuard_CallNotAllowed.selector, address(app), MockApp.poke.selector)
        );
        _asPasskey(SINGLE, _poke(app), _none());
    }

    /// @notice So does calldata too short for a selector.
    function test_shortCalldata_isRefused() public {
        MockApp app = new MockApp();
        vm.expectRevert(
            abi.encodeWithSelector(PayeeGuard.PayeeGuard_CallNotAllowed.selector, address(app), bytes4(0))
        );
        _asPasskey(SINGLE, _single(address(app), 0, hex"0102"), _none());
    }

    /// @notice A contract's own address with no calldata is a payee like any other: it must be a contact.
    function test_plainSendToAContract_needsAPass() public {
        MockApp app = new MockApp();
        vm.expectRevert(_notAContact(address(app)));
        _asPasskey(SINGLE, _single(address(app), 1, ""), _none());
    }

    /// @notice The passkey stop: removeAgent for this wallet on the agent module it has installed. For
    ///         another wallet, or on a contract the wallet didn't install, it is just another call.
    function test_removeAgent_onlyForThisWalletOnItsOwnModule() public {
        AgentModule agents = AgentModule(registry.agentModule());
        address agent = makeAddr("agent");
        vm.prank(owner);
        agents.addAgent(address(wallet), agent, 1e18, uint48(block.timestamp + 1 days));

        _asPasskey(SINGLE, _single(address(agents), 0, abi.encodeCall(AgentModule.removeAgent, (address(wallet), agent))), _none());
        (,,, bool active) = agents.getAgent(address(wallet), agent);
        assertFalse(active, "the passkey stop did not remove the agent");

        bytes memory forOther = abi.encodeCall(AgentModule.removeAgent, (stranger, agent));
        vm.expectRevert(
            abi.encodeWithSelector(PayeeGuard.PayeeGuard_CallNotAllowed.selector, address(agents), bytes4(forOther))
        );
        _asPasskey(SINGLE, _single(address(agents), 0, forOther), _none());

        MockApp notInstalled = new MockApp();
        bytes memory onOther = abi.encodeCall(AgentModule.removeAgent, (address(wallet), agent));
        vm.expectRevert(
            abi.encodeWithSelector(
                PayeeGuard.PayeeGuard_CallNotAllowed.selector, address(notInstalled), bytes4(onOther)
            )
        );
        _asPasskey(SINGLE, _single(address(notInstalled), 0, onOther), _none());
    }

    function test_guardDirectly_refusesNonExecutionsAndDelegatecall() public {
        vm.startPrank(address(module));
        vm.expectRevert(PayeeGuard.PayeeGuard_NotAnExecution.selector);
        guard.check(address(wallet), 0, abi.encodeCall(SessionHandler.pause, ()));

        vm.expectRevert(PayeeGuard.PayeeGuard_UnsupportedCallType.selector);
        guard.check(address(wallet), 0, _callData(bytes32(uint256(0xff) << 248), abi.encodePacked(bob, ""), _none()));
        vm.stopPrank();
    }

    /*//////////////////////////////////////////////////////////////
                          WHO IS HELD TO IT
    //////////////////////////////////////////////////////////////*/

    /// @notice The owner's own direct call is not held: the owner is who signs passes in the first place.
    function test_ownerDirectCall_isNotHeld() public {
        vm.prank(owner);
        wallet.execute(SINGLE, _single(stranger, 1 ether, ""));
        assertEq(stranger.balance, 1 ether);
    }

    /// @notice An executor the owner installed is not held either: recurring payments pay the payee the
    ///         owner set in a direct transaction.
    function test_executor_isNotHeld() public {
        MockExecutorModule executor = new MockExecutorModule();
        vm.prank(owner);
        wallet.installModule(MODULE_TYPE_EXECUTOR, address(executor), "");
        executor.callExecute(address(wallet), SINGLE, _transfer(usdc, stranger, 1e6));
        assertEq(usdc.balanceOf(stranger), 1e6);
    }

    /// @notice An agent's op runs as the wallet calling itself, and is not held (the owner's call,
    ///         2026-11-27): it may pay a stranger with no pass. Its own USD limit bounds it.
    function test_selfCall_isNotHeld() public {
        _call(address(wallet), _callData(SINGLE, _single(stranger, 1 ether, ""), _none()));
        assertEq(stranger.balance, 1 ether);
    }

    /// @notice Through a real passkey UserOp: the op that names a stranger executes nothing; the one
    ///         carrying bob's pass pays bob.
    function test_fullUserOp_paysOnlyAContact() public {
        PackedUserOperation memory op = _passkeyOp(_callData(SINGLE, _single(stranger, 1 ether, ""), _none()), 1);
        _handle(op);
        assertEq(stranger.balance, 0, "a passkey op paid a stranger");

        op = _passkeyOp(_callData(SINGLE, _single(bob, 1 ether, ""), _one(_pass(bob))), 2);
        _handle(op);
        assertEq(bob.balance, 1 ether, "a passkey op could not pay a contact");
    }

    function _passkeyOp(bytes memory callData, uint144 lane) internal view returns (PackedUserOperation memory op) {
        bytes32 hash;
        (op, hash) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet), config, callData, PasskeyHelper.nonceKey(PasskeyHelper.LATEST_OP_DEADLINE, lane)
        );
        op.signature = sessionKey.sign(hash);
    }

    function _handle(PackedUserOperation memory op) internal {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /*//////////////////////////////////////////////////////////////
                              APP CHECKS
    //////////////////////////////////////////////////////////////*/

    /// @notice Whoever an app check names as a payee is held like a plain payee.
    function test_appCheck_payeesAreHeld() public {
        (MockApp app, ScriptedAppCheck check) = _scriptedApp();
        check.setPayee(stranger);
        vm.expectRevert(_notAContact(stranger));
        _asPasskey(SINGLE, _poke(app), _none());

        check.setPayee(bob);
        _asPasskey(SINGLE, _poke(app), _one(_pass(bob)));
        check.setPayee(address(wallet));
        _asPasskey(SINGLE, _poke(app), _none());
        assertEq(app.pokes(), 2);
    }

    /// @notice A trade must get back at least 88% of what it puts in, at oracle prices.
    function test_appCheck_tradeFloor() public {
        (MockApp app, ScriptedAppCheck check) = _scriptedApp();
        uint256 usdcIn = 1000e6;
        int256 inUsd = oracle.getPrice(address(usdc), usdcIn);
        // The dai that is worth exactly 88% of it, and one wei of dai less.
        uint256 daiAtFloor = _amountWorth(address(dai), (inUsd * 8800) / 10_000);

        check.setTrade(IAppCheck.Trade(address(usdc), usdcIn, address(dai), daiAtFloor));
        _asPasskey(SINGLE, _poke(app), _none());

        check.setTrade(IAppCheck.Trade(address(usdc), usdcIn, address(dai), daiAtFloor / 2));
        vm.expectRevert();
        _asPasskey(SINGLE, _poke(app), _none());
    }

    /// @notice Below the floor the error names the trade and both values.
    function test_appCheck_tradeBelowFloor_exactError() public {
        (MockApp app, ScriptedAppCheck check) = _scriptedApp();
        check.setTrade(IAppCheck.Trade(address(usdc), 1000e6, address(dai), 1e18));
        vm.expectRevert(
            abi.encodeWithSelector(
                PayeeGuard.PayeeGuard_TradeBelowFloor.selector,
                address(usdc),
                address(dai),
                oracle.getPrice(address(usdc), 1000e6),
                oracle.getPrice(address(dai), 1e18)
            )
        );
        _asPasskey(SINGLE, _poke(app), _none());
    }

    /// @notice A token the oracle can't price has no floor (the owner's call): the trade goes through.
    function test_appCheck_unpricedToken_hasNoFloor() public {
        (MockApp app, ScriptedAppCheck check) = _scriptedApp();
        ERC20Mock unpriced = new ERC20Mock("Unpriced", "UNP", 18);
        check.setTrade(IAppCheck.Trade(address(usdc), 1000e6, address(unpriced), 1));
        _asPasskey(SINGLE, _poke(app), _none());
    }

    /// @notice A pool must be priced within 12% of the oracle, either way; an empty one has no price.
    function test_appCheck_poolPrice() public {
        (MockApp app, ScriptedAppCheck check) = _scriptedApp();
        uint256 usdcAmount = 1000e6;
        uint256 fairDai = _amountWorth(address(dai), oracle.getPrice(address(usdc), usdcAmount));

        check.setPrice(IAppCheck.PoolPrice(address(usdc), usdcAmount, address(dai), fairDai));
        _asPasskey(SINGLE, _poke(app), _none());

        check.setPrice(IAppCheck.PoolPrice(address(usdc), usdcAmount, address(dai), fairDai * 80 / 100));
        vm.expectRevert();
        _asPasskey(SINGLE, _poke(app), _none());

        check.setPrice(IAppCheck.PoolPrice(address(usdc), usdcAmount, address(dai), fairDai * 120 / 100));
        vm.expectRevert();
        _asPasskey(SINGLE, _poke(app), _none());

        check.setPrice(IAppCheck.PoolPrice(address(usdc), 0, address(dai), fairDai));
        vm.expectRevert(abi.encodeWithSelector(PayeeGuard.PayeeGuard_EmptyPool.selector, address(usdc), address(dai)));
        _asPasskey(SINGLE, _poke(app), _none());
    }

    /// @dev How much of `token` is worth `usd` (18-decimal) at the oracle's price, rounded up.
    function _amountWorth(address token, int256 usd) internal view returns (uint256) {
        int256 unit = oracle.getPrice(token, 10 ** ERC20Mock(token).decimals());
        return (uint256(usd) * 10 ** ERC20Mock(token).decimals() + uint256(unit) - 1) / uint256(unit);
    }

    /*//////////////////////////////////////////////////////////////
                        APP CHECKS: THE DELAY
    //////////////////////////////////////////////////////////////*/

    /// @notice A proposed check does nothing for two days; then it binds every wallet.
    function test_appCheck_bindsOnlyAfterTheDelay() public {
        MockApp app = new MockApp();
        OpenAppCheck check = new OpenAppCheck(address(app));
        uint256 start = vm.getBlockTimestamp();
        vm.prank(owner);
        treasury.proposeAppCheck(address(app), address(check));
        (address pending, uint48 eta) = registry.pendingAppCheck(address(app));
        assertEq(pending, address(check));
        assertEq(eta, start + registry.APP_CHECK_TIMELOCK());

        vm.expectRevert(
            abi.encodeWithSelector(PayeeGuard.PayeeGuard_CallNotAllowed.selector, address(app), MockApp.poke.selector)
        );
        _asPasskey(SINGLE, _poke(app), _none());

        vm.warp(start + registry.APP_CHECK_TIMELOCK() - 1);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_TimelockNotElapsed.selector, uint256(eta)));
        treasury.commitAppCheck(address(app));

        vm.warp(start + registry.APP_CHECK_TIMELOCK());
        vm.prank(owner);
        treasury.commitAppCheck(address(app));
        assertEq(registry.appCheck(address(app)), address(check));
        vm.warp(start);
        _asPasskey(SINGLE, _poke(app), _none());
        assertEq(app.pokes(), 1);
    }

    /// @notice Removing a check is instant: the next call to the app is refused.
    function test_appCheck_removalIsInstant() public {
        (MockApp app,) = _scriptedApp();
        _asPasskey(SINGLE, _poke(app), _none());

        vm.expectEmit(address(registry));
        emit SHRegistry.AppCheckUpdated(address(app), registry.appCheck(address(app)), address(0));
        vm.prank(owner);
        treasury.removeAppCheck(address(app));

        vm.expectRevert(
            abi.encodeWithSelector(PayeeGuard.PayeeGuard_CallNotAllowed.selector, address(app), MockApp.poke.selector)
        );
        _asPasskey(SINGLE, _poke(app), _none());

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_NoAppCheck.selector, address(app)));
        treasury.removeAppCheck(address(app));
    }

    function test_appCheck_proposalCanBeCancelled() public {
        MockApp app = new MockApp();
        vm.startPrank(owner);
        treasury.proposeAppCheck(address(app), address(new OpenAppCheck(address(app))));
        treasury.cancelAppCheck(address(app));
        vm.warp(vm.getBlockTimestamp() + registry.APP_CHECK_TIMELOCK());
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_NoPendingAppCheck.selector, address(app)));
        treasury.commitAppCheck(address(app));
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_NoPendingAppCheck.selector, address(app)));
        treasury.cancelAppCheck(address(app));
        vm.stopPrank();
    }

    /// @notice A second proposal restarts the clock, so a harmless one can't be pre-aged and swapped.
    function test_appCheck_replacingAProposalRestartsTheDelay() public {
        MockApp app = new MockApp();
        uint256 start = vm.getBlockTimestamp();
        vm.startPrank(owner);
        treasury.proposeAppCheck(address(app), address(new OpenAppCheck(address(app))));
        vm.warp(start + registry.APP_CHECK_TIMELOCK() - 1);
        treasury.proposeAppCheck(address(app), address(new ScriptedAppCheck(address(app))));
        vm.warp(start + registry.APP_CHECK_TIMELOCK());
        vm.expectRevert();
        treasury.commitAppCheck(address(app));
        vm.stopPrank();
    }

    /// @notice A check must have code and must name the app it is registered under.
    function test_appCheck_wiringIsValidated() public {
        MockApp app = new MockApp();
        MockApp other = new MockApp();
        OpenAppCheck forOther = new OpenAppCheck(address(other));
        vm.startPrank(owner);
        vm.expectRevert(
            abi.encodeWithSelector(SHRegistry.SHRegistry_InvalidAppCheck.selector, address(app), address(forOther))
        );
        treasury.proposeAppCheck(address(app), address(forOther));
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_InvalidAppCheck.selector, address(app), bob));
        treasury.proposeAppCheck(address(app), bob);
        vm.expectRevert(
            abi.encodeWithSelector(SHRegistry.SHRegistry_InvalidAppCheck.selector, address(0), address(forOther))
        );
        treasury.proposeAppCheck(address(0), address(forOther));
        vm.stopPrank();
    }

    /// @notice The deploy script registered this chain's starting checks: on a bare Anvil node, the
    ///         wrapped native token's and the reputation registry's.
    function test_deploy_registeredTheLocalChecks() public view {
        assertTrue(registry.appCheck(config.weth) != address(0), "no check for WETH");
        assertTrue(registry.appCheck(config.reputationRegistry) != address(0), "no check for the reputation registry");
    }

    /*//////////////////////////////////////////////////////////////
                                 GAS
    //////////////////////////////////////////////////////////////*/

    /// @notice What the contact rule costs a payment: the same ERC-20 transfer to the wallet itself
    ///         (no pass to check) and to a contact (one pass).
    function test_gas_contactPassOnATransfer() public {
        bytes memory toSelf = _callData(SINGLE, _transfer(usdc, address(wallet), 1e6), _none());
        bytes memory toBob = _callData(SINGLE, _transfer(usdc, bob, 1e6), _one(_pass(bob)));
        // Warm both balances first so the comparison is only the guard's work.
        usdc.mint(bob, 1);

        vm.prank(config.entryPoint);
        uint256 before = gasleft();
        (bool ok,) = address(wallet).call(toSelf);
        uint256 selfGas = before - gasleft();
        assertTrue(ok);

        vm.prank(config.entryPoint);
        before = gasleft();
        (ok,) = address(wallet).call(toBob);
        uint256 bobGas = before - gasleft();
        assertTrue(ok);

        emit log_named_uint("execute gas, transfer needing no pass", selfGas);
        emit log_named_uint("execute gas, transfer to a contact with one pass", bobGas);
        assertLt(bobGas, selfGas + 25_000, "a pass costs far more than expected");
    }
}
