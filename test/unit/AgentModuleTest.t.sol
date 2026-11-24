//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {
    MODULE_TYPE_VALIDATOR,
    MODULE_TYPE_EXECUTOR,
    IERC7579Execution,
    IERC7579ModuleConfig,
    Execution
} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {IAccountExecute} from "@openzeppelin/contracts/interfaces/draft-IERC4337.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {AgentModule} from "../../src/AgentModule.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {IUserOpHooks} from "../../src/interfaces/IUserOpHooks.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {PasskeyHelper} from "../utils/PasskeyHelper.sol";

/// @dev Some other protocol the agent deposits into: pulls the approved amount from the caller.
contract MockVault {
    function deposit(IERC20 token, uint256 amount) external {
        require(token.transferFrom(msg.sender, address(this), amount), "pull failed");
    }
}

/// @dev An exchange that takes one token and mints another: how an agent buys a token the limit
///      doesn't count.
contract MockSwap {
    function swap(IERC20 tokenIn, uint256 amountIn, ERC20Mock tokenOut, uint256 amountOut) external {
        require(tokenIn.transferFrom(msg.sender, address(this), amountIn), "pull failed");
        tokenOut.mint(msg.sender, amountOut);
    }
}

/// @dev A validator that approves every op and counts its hooks, so the wallet's own rules for
///      module ops can be tested apart from AgentModule's. Only an owner can install one.
contract YesValidator {
    uint256 public pre;
    uint256 public post;
    bool public refuseLoudly;

    function onInstall(bytes calldata) external {}

    function onUninstall(bytes calldata) external {}

    function isModuleType(uint256 moduleTypeId) external pure returns (bool) {
        return moduleTypeId == MODULE_TYPE_VALIDATOR;
    }

    function validateUserOp(PackedUserOperation calldata, bytes32) external view returns (uint256) {
        require(!refuseLoudly, "YesValidator says no");
        return 0;
    }

    function isValidSignatureWithSender(address, bytes32, bytes calldata) external pure returns (bytes4) {
        return 0xffffffff;
    }

    function preExecute(PackedUserOperation calldata, bytes32) external {
        pre++;
    }

    function postExecute(PackedUserOperation calldata, bytes32) external {
        post++;
    }

    function setRefuseLoudly(bool refuse) external {
        refuseLoudly = refuse;
    }
}

/**
 * @title AgentModuleTest
 * @notice AgentModule on a real SessionHandler wallet, every agent trade a real UserOp through the
 *         EntryPoint: only the owner adds or raises an agent; the owner or a passkey removes one; the
 *         agent's signature covers the whole op, with a deadline and an epoch; the three rules (native
 *         metered, no uncounted token leaves, no call to the module); the agent's own limit, its fee
 *         included, on the wallet's window; every trade still bound by the wallet's guard, fee, pause,
 *         gas ceilings and USD limit. And the wallet's side: which ops reach a validator module at
 *         all, and that such an op runs only through executeUserOp, with the module's hooks around a
 *         guarded, hooked self-call to execute.
 * @dev Every attack here is tested as a refusal. Mocks price USDC and DAI at $1 and ETH at the
 *      HelperConfig figure; expected values are read from the oracle, never hardcoded.
 * @dev A refusal in VALIDATION reverts handleOps itself (FailedOp / FailedOpWithRevert). One in
 *      EXECUTION does not: the EntryPoint absorbs it, emits UserOperationRevertReason and still
 *      charges the op's gas, so those tests watch for that event and check nothing moved.
 */
contract AgentModuleTest is Test {
    using PasskeyHelper for PasskeyHelper.Passkey;

    /// @dev The signed type, as a struct, for Forge's EIP-712 encoder.
    struct AgentOperation {
        bytes32 userOpHash;
        uint48 deadline;
        uint32 epoch;
    }

    SessionHandler wallet;
    AgentModule agents;
    SpendingLimitModule module;
    SHOracle oracle;
    SHTreasury treasury;
    SHRegistry registry;
    SHFactory factory;
    HelperConfig.NetworkConfig config;
    SendPackedUserOp sendPackedUserOp;
    ERC20Mock usdc;
    ERC20Mock dai;
    /// @dev A token the wallet holds but its USD limit doesn't count.
    ERC20Mock xyz;
    MockVault vault;
    MockSwap exchange;

    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;
    uint128 constant AGENT_LIMIT = 1000e18;
    /// @dev Fixed start, set before the mock feeds are made, so warps are measured from a known time.
    uint256 constant START = 1_000_000;
    bytes32 constant SINGLE = bytes32(0);
    bytes32 constant BATCH = bytes32(uint256(0x01) << 248);
    /// @dev Anvil's first default account: the owner HelperConfig deploys with locally.
    uint256 constant ANVIL_OWNER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    PasskeyHelper.Passkey sessionKey;
    address owner;
    address agent;
    uint256 agentKey;
    address payee = makeAddr("payee");
    address stranger = makeAddr("stranger");
    address bundler = makeAddr("bundler");

    function setUp() public {
        vm.warp(START);
        sessionKey = PasskeyHelper.create("sessionKey");

        (factory, treasury, config, oracle) = new DeploySHProtocol().run();
        registry = factory.REGISTRY();
        module = SpendingLimitModule(registry.spendingLimitModule());
        agents = AgentModule(registry.agentModule());
        owner = config.account;
        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);

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

        sendPackedUserOp = new SendPackedUserOp();
        vm.deal(address(wallet), 10 ether);
        usdc.mint(address(wallet), 100_000e6);
        xyz = new ERC20Mock("Uncounted", "XYZ", 18);
        xyz.mint(address(wallet), 1000e18);
        vault = new MockVault();
        exchange = new MockSwap();

        (agent, agentKey) = makeAddrAndKey("agent");
        vm.prank(owner);
        agents.addAgent(address(wallet), agent, AGENT_LIMIT, uint48(block.timestamp + 30 days));
    }

    /*//////////////////////////////////////////////////////////////
                         EVERY WALLET HAS IT
    //////////////////////////////////////////////////////////////*/

    /// @notice The factory installs the registry's AgentModule on every wallet, as a validator only.
    function test_install_everyWalletHasItAsAValidator() public view {
        assertTrue(address(agents) != address(0));
        assertTrue(wallet.isModuleInstalled(MODULE_TYPE_VALIDATOR, address(agents), ""));
        assertFalse(wallet.isModuleInstalled(MODULE_TYPE_EXECUTOR, address(agents), ""));
        assertTrue(agents.isModuleType(MODULE_TYPE_VALIDATOR));
        assertFalse(agents.isModuleType(MODULE_TYPE_EXECUTOR));
    }

    /*//////////////////////////////////////////////////////////////
                              ADDING AGENTS
    //////////////////////////////////////////////////////////////*/

    function test_add_ownerAddsAnAgent() public view {
        address[] memory list = agents.getAgents(address(wallet));
        assertEq(list.length, 1);
        assertEq(list[0], agent);
        (AgentModule.Agent memory info, uint256 spent, uint256 remaining, bool active) =
            agents.getAgent(address(wallet), agent);
        assertEq(info.limitUsd, AGENT_LIMIT);
        assertEq(info.validUntil, START + 30 days);
        assertEq(info.epoch, 1);
        assertEq(info.position, 1);
        assertEq(spent, 0);
        assertEq(remaining, AGENT_LIMIT);
        assertTrue(active);
    }

    function test_add_emits() public {
        address second = makeAddr("second");
        vm.expectEmit(address(agents));
        emit AgentModule.AgentAdded(address(wallet), second, 50e18, uint48(block.timestamp + 7 days), 1);
        vm.prank(owner);
        agents.addAgent(address(wallet), second, 50e18, uint48(block.timestamp + 7 days));
    }

    function test_add_strangerCannot() public {
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_NotOwner.selector, address(wallet), stranger));
        vm.prank(stranger);
        agents.addAgent(address(wallet), stranger, AGENT_LIMIT, uint48(block.timestamp + 1 days));
    }

    /// @notice A passkey acts AS the wallet, which is not its owner: it can't add an agent. Exact error,
    ///         with the EntryPoint calling execute as it does for a UserOp.
    function test_add_passkeyCannot_exactError() public {
        bytes memory call = abi.encodeCall(
            AgentModule.addAgent, (address(wallet), stranger, AGENT_LIMIT, uint48(block.timestamp + 1 days))
        );
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_NotOwner.selector, address(wallet), address(wallet))
        );
        vm.prank(config.entryPoint);
        wallet.execute(SINGLE, abi.encodePacked(address(agents), uint256(0), call));
    }

    /// @notice The same through a real passkey-signed UserOp: handleOps absorbs the revert, so the
    ///         proof is that no agent was added.
    function test_add_passkeyCannot_fullUserOp() public {
        _passkeyOp(
            address(agents),
            abi.encodeCall(
                AgentModule.addAgent, (address(wallet), stranger, AGENT_LIMIT, uint48(block.timestamp + 1 days))
            )
        );
        assertEq(agents.getAgents(address(wallet)).length, 1, "a passkey added an agent");
        (,,, bool active) = agents.getAgent(address(wallet), stranger);
        assertFalse(active);
    }

    function test_add_selfOwnedWalletRefused() public {
        vm.prank(owner);
        wallet.transferOwnership(address(wallet));
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_NotOwner.selector, address(wallet), address(wallet))
        );
        vm.prank(address(wallet));
        agents.addAgent(address(wallet), stranger, AGENT_LIMIT, uint48(block.timestamp + 1 days));
    }

    function test_add_refusedWhenNotInstalled() public {
        vm.prank(owner);
        wallet.uninstallModule(MODULE_TYPE_VALIDATOR, address(agents), "");
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_NotInstalled.selector, address(wallet)));
        vm.prank(owner);
        agents.addAgent(address(wallet), stranger, AGENT_LIMIT, uint48(block.timestamp + 1 days));
    }

    /// @notice Rule 1: no agent while the wallet's limit leaves ETH out.
    function test_add_refusedWhileNativeIsNotMetered() public {
        vm.startPrank(owner);
        wallet.setNativeMetering(false, address(0));
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_NativeNotMetered.selector, address(wallet)));
        agents.addAgent(address(wallet), stranger, AGENT_LIMIT, uint48(block.timestamp + 1 days));
        vm.stopPrank();
    }

    function test_add_refusesBadAgents() public {
        address[3] memory bad = [address(0), address(wallet), address(agents)];
        for (uint256 i = 0; i < bad.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_InvalidAgent.selector, bad[i]));
            vm.prank(owner);
            agents.addAgent(address(wallet), bad[i], AGENT_LIMIT, uint48(block.timestamp + 1 days));
        }
    }

    function test_add_refusesBadTerms() public {
        uint256 latest = block.timestamp + agents.MAX_VALIDITY();
        vm.startPrank(owner);
        vm.expectRevert(AgentModule.AgentModule_InvalidLimit.selector);
        agents.addAgent(address(wallet), stranger, 0, uint48(block.timestamp + 1 days));

        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_InvalidExpiry.selector, uint48(block.timestamp), latest)
        );
        agents.addAgent(address(wallet), stranger, AGENT_LIMIT, uint48(block.timestamp));

        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_InvalidExpiry.selector, uint48(latest + 1), latest)
        );
        agents.addAgent(address(wallet), stranger, AGENT_LIMIT, uint48(latest + 1));

        // Exactly 90 days is allowed.
        agents.addAgent(address(wallet), stranger, AGENT_LIMIT, uint48(latest));
        vm.stopPrank();
    }

    function test_add_refusesTheSameAgentTwice() public {
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_AlreadyAdded.selector, address(wallet), agent));
        vm.prank(owner);
        agents.addAgent(address(wallet), agent, AGENT_LIMIT, uint48(block.timestamp + 1 days));
    }

    function test_add_atMostThree() public {
        vm.startPrank(owner);
        agents.addAgent(address(wallet), makeAddr("two"), AGENT_LIMIT, uint48(block.timestamp + 1 days));
        agents.addAgent(address(wallet), makeAddr("three"), AGENT_LIMIT, uint48(block.timestamp + 1 days));
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_TooManyAgents.selector, address(wallet)));
        agents.addAgent(address(wallet), makeAddr("four"), AGENT_LIMIT, uint48(block.timestamp + 1 days));
        vm.stopPrank();
        assertEq(agents.getAgents(address(wallet)).length, 3);
    }

    /// @notice onInstall takes nothing: called directly by anything acting as the wallet, it adds no agent.
    function test_onInstall_addsNothing() public {
        vm.prank(address(wallet));
        agents.onInstall(abi.encode(stranger, AGENT_LIMIT, uint48(block.timestamp + 1 days)));
        assertEq(agents.getAgents(address(wallet)).length, 1);
    }

    /*//////////////////////////////////////////////////////////////
                         UPDATING / REMOVING
    //////////////////////////////////////////////////////////////*/

    function test_update_ownerChangesTerms() public {
        uint48 until = uint48(block.timestamp + 60 days);
        vm.expectEmit(address(agents));
        emit AgentModule.AgentUpdated(address(wallet), agent, 20e18, until);
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, 20e18, until);
        (AgentModule.Agent memory info,,,) = agents.getAgent(address(wallet), agent);
        assertEq(info.limitUsd, 20e18);
        assertEq(info.validUntil, until);
        assertEq(info.epoch, 1, "an update must not void the agent's signatures");
    }

    /// @notice A passkey can't raise an agent's limit.
    function test_update_passkeyCannot() public {
        bytes memory call = abi.encodeCall(
            AgentModule.updateAgent, (address(wallet), agent, type(uint128).max, uint48(block.timestamp + 1 days))
        );
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_NotOwner.selector, address(wallet), address(wallet))
        );
        vm.prank(config.entryPoint);
        wallet.execute(SINGLE, abi.encodePacked(address(agents), uint256(0), call));
    }

    function test_update_strangerAndUnknownRefused() public {
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_NotOwner.selector, address(wallet), stranger));
        vm.prank(stranger);
        agents.updateAgent(address(wallet), agent, 1e18, uint48(block.timestamp + 1 days));

        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, address(wallet), stranger)
        );
        vm.prank(owner);
        agents.updateAgent(address(wallet), stranger, 1e18, uint48(block.timestamp + 1 days));
    }

    function test_remove_owner() public {
        vm.expectEmit(address(agents));
        emit AgentModule.AgentRemoved(address(wallet), agent, owner);
        vm.prank(owner);
        agents.removeAgent(address(wallet), agent);
        assertEq(agents.getAgents(address(wallet)).length, 0);
        (AgentModule.Agent memory info,,, bool active) = agents.getAgent(address(wallet), agent);
        assertEq(info.position, 0);
        assertFalse(active);
    }

    /// @notice A passkey can stop an agent: through a real UserOp, no owner wallet needed.
    function test_remove_passkeyCan() public {
        _passkeyOp(address(agents), abi.encodeCall(AgentModule.removeAgent, (address(wallet), agent)));
        assertEq(agents.getAgents(address(wallet)).length, 0, "the passkey could not remove the agent");
    }

    function test_remove_strangerCannot() public {
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_NotOwner.selector, address(wallet), stranger));
        vm.prank(stranger);
        agents.removeAgent(address(wallet), agent);
    }

    function test_remove_unknownRefused() public {
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, address(wallet), stranger)
        );
        vm.prank(owner);
        agents.removeAgent(address(wallet), stranger);
    }

    /// @notice Swap-and-pop keeps every remaining agent findable, and frees a place.
    function test_remove_keepsTheOthersInPlace() public {
        address two = makeAddr("two");
        address three = makeAddr("three");
        vm.startPrank(owner);
        agents.addAgent(address(wallet), two, AGENT_LIMIT, uint48(block.timestamp + 1 days));
        agents.addAgent(address(wallet), three, AGENT_LIMIT, uint48(block.timestamp + 1 days));
        agents.removeAgent(address(wallet), agent);
        vm.stopPrank();

        address[] memory list = agents.getAgents(address(wallet));
        assertEq(list.length, 2);
        assertEq(list[0], three);
        assertEq(list[1], two);
        (AgentModule.Agent memory info3,,,) = agents.getAgent(address(wallet), three);
        (AgentModule.Agent memory info2,,,) = agents.getAgent(address(wallet), two);
        assertEq(info3.position, 1);
        assertEq(info2.position, 2);

        vm.startPrank(owner);
        agents.removeAgent(address(wallet), two);
        agents.addAgent(address(wallet), makeAddr("four"), AGENT_LIMIT, uint48(block.timestamp + 1 days));
        vm.stopPrank();
        assertEq(agents.getAgents(address(wallet)).length, 2);
    }

    /// @notice Uninstalling removes every agent; a reinstall starts with none, and an agent added again
    ///         gets a new epoch.
    function test_uninstall_removesEveryAgent() public {
        vm.prank(owner);
        agents.addAgent(address(wallet), makeAddr("two"), AGENT_LIMIT, uint48(block.timestamp + 1 days));

        vm.expectEmit(address(agents));
        emit AgentModule.AgentsCleared(address(wallet));
        vm.prank(owner);
        wallet.uninstallModule(MODULE_TYPE_VALIDATOR, address(agents), "");
        assertEq(agents.getAgents(address(wallet)).length, 0);

        vm.startPrank(owner);
        wallet.installModule(MODULE_TYPE_VALIDATOR, address(agents), "");
        assertEq(agents.getAgents(address(wallet)).length, 0, "agents came back with the reinstall");
        agents.addAgent(address(wallet), agent, AGENT_LIMIT, uint48(block.timestamp + 1 days));
        vm.stopPrank();
        (AgentModule.Agent memory info,,,) = agents.getAgent(address(wallet), agent);
        assertEq(info.epoch, 2);
    }

    /// @notice A stranger calling onUninstall directly clears only its own (empty) list.
    function test_onUninstall_byStrangerTouchesOnlyItself() public {
        vm.prank(stranger);
        agents.onUninstall("");
        assertEq(agents.getAgents(address(wallet)).length, 1);
    }

    /*//////////////////////////////////////////////////////////////
                           TRADES: THE BASICS
    //////////////////////////////////////////////////////////////*/

    /// @notice An agent's op through the EntryPoint: the payee is paid, the wallet's limit and the
    ///         agent's are both charged, the agent's with the protocol fee on top; the treasury gets
    ///         the protocol fee and no keeper fee; the wallet paid the op's gas, from its daily allowance.
    function test_trade_agentOp() public {
        uint256 amount = 100e6;
        uint256 value = _usd(address(usdc), amount);
        uint256 fee = _feeUsd();
        assertGt(fee, 0, "these tests assume a fee");
        uint256 treasuryBefore = address(treasury).balance;

        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, amount));
        vm.expectEmit(address(agents));
        emit AgentModule.AgentExecuted(address(wallet), agent, hash, value, fee, value + fee);
        _send(op);

        assertEq(usdc.balanceOf(payee), amount);
        assertEq(_walletSpent(), value, "the wallet's limit");
        assertEq(_agentSpent(), value + fee, "the agent's limit");
        assertEq(address(treasury).balance, treasuryBefore + registry.getFee(), "protocol fee only");
        assertGt(wallet.sessionGasSpent(), 0, "the wallet paid no gas");
        (, uint256 spent, uint256 remaining,) = agents.getAgent(address(wallet), agent);
        assertEq(spent, value + fee);
        assertEq(remaining, AGENT_LIMIT - value - fee);
    }

    /// @notice Any protocol: approve a counted token to some vault and deposit, in one batch. The
    ///         deposit leaves the wallet, so both limits are charged.
    function test_trade_depositIntoAnotherProtocolIsCharged() public {
        uint256 amount = 200e6;
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(IERC20.approve, (address(vault), amount)));
        execs[1] = Execution(address(vault), 0, abi.encodeCall(MockVault.deposit, (IERC20(address(usdc)), amount)));
        _run(BATCH, ERC7579Utils.encodeBatch(execs));

        uint256 value = _usd(address(usdc), amount);
        assertEq(usdc.balanceOf(address(vault)), amount);
        assertEq(_walletSpent(), value);
        assertEq(_agentSpent(), value + _feeUsd());
    }

    /// @notice Plain ETH sent anywhere is charged, since native metering must be on. The op's gas is
    ///         not: the hook meters only what the calls move.
    function test_trade_nativeSendIsCharged() public {
        _run(SINGLE, abi.encodePacked(payee, uint256(0.1 ether), bytes("")));
        uint256 value = _usd(address(0), 0.1 ether);
        assertEq(payee.balance, 0.1 ether);
        assertEq(_walletSpent(), value);
        assertEq(_agentSpent(), value + _feeUsd());
    }

    /// @notice The owner's own trades and a passkey's don't use the agent's limit.
    function test_trade_ownersAndPasskeysSpendIsNotTheAgents() public {
        _run(SINGLE, _transferUsdc(payee, 10e6));
        uint256 agentSpent = _agentSpent();
        vm.prank(owner);
        wallet.execute(SINGLE, _transferUsdc(payee, 500e6));
        _passkeyOp(address(usdc), abi.encodeCall(IERC20.transfer, (payee, 20e6)));
        assertEq(usdc.balanceOf(payee), 530e6);
        assertEq(_agentSpent(), agentSpent);
    }

    /// @notice Agents and passkeys share the wallet's daily gas allowance (the user's choice): an
    ///         agent op the allowance can't cover is refused in validation, before it costs anything.
    function test_trade_gasAllowanceIsSharedWithThePasskeys() public {
        _passkeyOp(address(usdc), abi.encodeCall(IERC20.transfer, (payee, 1e6)));
        uint256 afterPasskey = wallet.sessionGasSpent();
        assertGt(afterPasskey, 0);

        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        vm.prank(owner);
        wallet.setSessionGasBudget(afterPasskey + 1);
        vm.expectPartialRevert(IEntryPoint.FailedOpWithRevert.selector);
        _send(op);

        vm.prank(owner);
        wallet.setSessionGasBudget(1 ether);
        _send(op);
        assertGt(wallet.sessionGasSpent(), afterPasskey, "the agent's gas was not counted");
    }

    /// @notice Each op picks its own lane in the nonce key, so an agent's ops don't queue behind one
    ///         another: two in one bundle, on two lanes, both run.
    function test_trade_twoLanesInOneBundle() public {
        (PackedUserOperation memory a,) = _signedOpOnLane(SINGLE, _transferUsdc(payee, 1e6), 7);
        (PackedUserOperation memory b,) = _signedOpOnLane(SINGLE, _transferUsdc(payee, 2e6), 8);
        PackedUserOperation[] memory ops = new PackedUserOperation[](2);
        ops[0] = a;
        ops[1] = b;
        _sendAll(ops);
        assertEq(usdc.balanceOf(payee), 3e6);
        assertEq(_agentSpent(), _usd(address(usdc), 3e6) + 2 * _feeUsd());
    }

    /*//////////////////////////////////////////////////////////////
                             SIGNATURES
    //////////////////////////////////////////////////////////////*/

    /// @notice An op signed by any key but the agent's is refused, the owner's included.
    function test_sig_otherKeysRefused() public {
        (PackedUserOperation memory op, bytes32 hash) = _agentOp(SINGLE, _transferUsdc(stranger, 1e6), 0);
        uint48 deadline = uint48(block.timestamp + 1 hours);
        (, uint256 otherKey) = makeAddrAndKey("other");
        op.signature = _agentSignature(hash, otherKey, agent, 1, deadline);
        _expectFailedOp(op, "AA24 signature error");

        // The owner's key, in the agent's layout, is nobody here either.
        op.signature = _agentSignature(hash, ANVIL_OWNER_KEY, agent, 1, deadline);
        _expectFailedOp(op, "AA24 signature error");
    }

    /// @notice What was signed is what runs: the op's hash covers every field, so a different amount,
    ///         gas, nonce or wallet invalidates the signature; and the epoch, deadline and agent in the
    ///         signature itself can't be changed either.
    function test_sig_coversTheWholeOp() public {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        bytes memory good = op.signature;

        PackedUserOperation memory tampered = _copy(op);
        tampered.callData = _agentCallData(SINGLE, _transferUsdc(payee, 2e6));
        _expectFailedOp(tampered, "AA24 signature error");

        tampered = _copy(op);
        tampered.preVerificationGas += 1;
        _expectFailedOp(tampered, "AA24 signature error");

        tampered = _copy(op);
        tampered.signature = _withByte(good, 23, 0x02); // epoch 1 -> 2
        _expectFailedOp(tampered, "AA24 signature error");

        tampered = _copy(op);
        tampered.signature = _withByte(good, 29, bytes1(uint8(good[29]) ^ 0x01)); // deadline
        _expectFailedOp(tampered, "AA24 signature error");

        // Another of the wallet's agents named in front of this agent's signature.
        address two = makeAddr("two");
        vm.prank(owner);
        agents.addAgent(address(wallet), two, AGENT_LIMIT, uint48(block.timestamp + 1 days));
        tampered = _copy(op);
        bytes memory renamed = bytes.concat(good);
        assembly ("memory-safe") {
            let word := mload(add(renamed, 0x20))
            mstore(add(renamed, 0x20), or(shl(96, two), and(word, 0xffffffffffffffffffffffff)))
        }
        tampered.signature = renamed;
        _expectFailedOp(tampered, "AA24 signature error");

        _send(op);
        assertEq(usdc.balanceOf(payee), 1e6, "the untouched op did not run");
    }

    /// @notice Only the 95-byte layout reaches the agent check. 65 bytes is the owner's route and
    ///         256 or more a passkey's, whatever the nonce key says; any other length the module
    ///         refuses.
    function test_sig_wrongLengthRefused() public {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        bytes memory good = op.signature;
        uint256[5] memory lengths = [uint256(0), 64, 65, 94, 96];
        for (uint256 i = 0; i < lengths.length; i++) {
            PackedUserOperation memory tampered = _copy(op);
            tampered.signature = _resized(good, lengths[i]);
            _expectFailedOp(tampered, "AA24 signature error");
        }
    }

    function test_sig_otherChainRefused() public {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        vm.chainId(block.chainid + 1);
        _expectFailedOp(op, "AA24 signature error");
    }

    /// @notice A signature for another AgentModule (another domain) is refused.
    function test_sig_otherModuleRefused() public {
        AgentModule other = new AgentModule();
        (PackedUserOperation memory op, bytes32 hash) = _agentOp(SINGLE, _transferUsdc(payee, 1e6), 0);
        uint48 deadline = uint48(block.timestamp + 1 hours);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(agentKey, other.hashOperation(hash, deadline, 1));
        op.signature = abi.encodePacked(agent, uint32(1), deadline, r, s, v);
        _expectFailedOp(op, "AA24 signature error");
    }

    /// @notice The EntryPoint's nonce makes every op usable once.
    function test_sig_replayRefused() public {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        _send(op);
        _expectFailedOp(op, "AA25 invalid account nonce");
        assertEq(usdc.balanceOf(payee), 1e6);
    }

    /// @notice An op not sent by its deadline is dead.
    function test_sig_deadlinePassedRefused() public {
        (PackedUserOperation memory op, bytes32 hash) = _agentOp(SINGLE, _transferUsdc(payee, 1e6), 0);
        op.signature = _agentSignature(hash, agentKey, agent, 1, uint48(block.timestamp + 10 minutes));
        _warp(block.timestamp + 10 minutes + 1);
        _expectFailedOp(op, "AA22 expired or not due");
    }

    /// @notice A zero deadline would read as "valid forever" to the EntryPoint: refused outright.
    function test_sig_zeroDeadlineRefused() public {
        (PackedUserOperation memory op, bytes32 hash) = _agentOp(SINGLE, _transferUsdc(payee, 1e6), 0);
        op.signature = _agentSignature(hash, agentKey, agent, 1, 0);
        _expectFailedOp(op, "AA24 signature error");
    }

    /// @notice A deadline past the agent's expiry doesn't stretch it: the op dies with the agent.
    function test_sig_agentExpiryCapsTheDeadline() public {
        (PackedUserOperation memory op, bytes32 hash) = _agentOp(SINGLE, _transferUsdc(payee, 1e6), 0);
        op.signature = _agentSignature(hash, agentKey, agent, 1, uint48(START + 60 days));
        _warp(START + 30 days + 1);
        _expectFailedOp(op, "AA22 expired or not due");
    }

    /// @notice Removing an agent voids every op it signed, even when the same address is added back:
    ///         the epoch moved on.
    function test_sig_removalVoidsOldSignatures() public {
        (PackedUserOperation memory old,) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));

        vm.startPrank(owner);
        agents.removeAgent(address(wallet), agent);
        agents.addAgent(address(wallet), agent, AGENT_LIMIT, uint48(block.timestamp + 1 days));
        vm.stopPrank();

        _expectFailedOp(old, "AA24 signature error");
        _run(SINGLE, _transferUsdc(payee, 1e6));
        assertEq(usdc.balanceOf(payee), 1e6);
    }

    /// @notice hashOperation is plain EIP-712, checked against Forge's own encoder, so an agent's
    ///         ordinary signTypedData produces it.
    function test_hashOperation_isStandardEip712() public view {
        bytes32 userOpHash = keccak256("some op");
        bytes32 structHash = vm.eip712HashStruct(
            "AgentOperation(bytes32 userOpHash,uint48 deadline,uint32 epoch)",
            abi.encode(AgentOperation(userOpHash, 1234, 7))
        );
        bytes32 domain = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("AgentModule"),
                keccak256("1"),
                block.chainid,
                address(agents)
            )
        );
        assertEq(agents.hashOperation(userOpHash, 1234, 7), keccak256(abi.encodePacked("\x19\x01", domain, structHash)));
        assertEq(
            agents.OPERATION_TYPEHASH(), keccak256("AgentOperation(bytes32 userOpHash,uint48 deadline,uint32 epoch)")
        );
    }

    /// @notice An agent can never sign a message for the wallet (ERC-1271): not through the module's
    ///         prefix, not with a perfectly good agent signature.
    function test_sig_agentCantSignMessages() public view {
        bytes32 digest = keccak256("Permit2: let me take your USDC");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(agentKey, digest);
        bytes memory sig = abi.encodePacked(r, s, v);
        assertEq(wallet.isValidSignature(digest, abi.encodePacked(address(agents), sig)), bytes4(0xffffffff));
        assertEq(wallet.isValidSignature(digest, sig), bytes4(0xffffffff));
        assertEq(agents.isValidSignatureWithSender(address(this), digest, sig), bytes4(0xffffffff));
    }

    /*//////////////////////////////////////////////////////////////
                          WHO MAY TRADE, WHEN
    //////////////////////////////////////////////////////////////*/

    /// @notice A key the wallet never made an agent, signing as itself.
    function test_trade_unknownAgentRefused() public {
        (address unknown, uint256 unknownKey) = makeAddrAndKey("unknown");
        (PackedUserOperation memory op, bytes32 hash) = _agentOp(SINGLE, _transferUsdc(unknown, 1e6), 0);
        op.signature = _agentSignature(hash, unknownKey, unknown, 1, uint48(block.timestamp + 1 hours));
        _expectFailedOp(op, "AA24 signature error");

        // An unknown agent's record reads all zero, epoch included: naming epoch 0 changes nothing.
        op.signature = _agentSignature(hash, unknownKey, unknown, 0, uint48(block.timestamp + 1 hours));
        _expectFailedOp(op, "AA24 signature error");
    }

    function test_trade_removedAgentRefused() public {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(agent, 1e6));
        vm.prank(owner);
        agents.removeAgent(address(wallet), agent);
        _expectFailedOp(op, "AA24 signature error");
    }

    /// @notice Uninstalled, the module is never asked: no op signed by an agent validates.
    function test_trade_uninstalledRefused() public {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(agent, 1e6));
        vm.prank(owner);
        wallet.uninstallModule(MODULE_TYPE_VALIDATOR, address(agents), "");
        _expectFailedOp(op, "AA24 signature error");
    }

    function test_trade_pausedWalletRefused() public {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, _transferUsdc(agent, 1e6));
        vm.prank(owner);
        wallet.pause();
        _expectFailedOpWithRevert(op, abi.encodeWithSelector(Pausable.EnforcedPause.selector));
    }

    /// @notice Only a plain single call or a plain batch: no delegatecall, no "try" mode.
    function test_trade_otherModesRefused() public {
        bytes32[3] memory modes = [
            bytes32(uint256(0xff) << 248), // delegatecall
            bytes32(uint256(0x01) << 240), // single, try
            bytes32(uint256(0x0101) << 240) // batch, try
        ];
        for (uint256 i = 0; i < modes.length; i++) {
            (PackedUserOperation memory op,) = _signedOp(modes[i], _transferUsdc(agent, 1e6));
            _expectFailedOpWithRevert(
                op, abi.encodeWithSelector(AgentModule.AgentModule_UnsupportedMode.selector, modes[i])
            );
        }
    }

    /// @notice The wallet's own guard still applies: an agent can't reach the limit module or the
    ///         EntryPoint. Refused in execution, so nothing moves.
    function test_trade_walletGuardStillApplies() public {
        address[2] memory targets = [address(module), config.entryPoint];
        bytes[2] memory data = [
            abi.encodeCall(SpendingLimitModule.setDailyLimit, (type(int88).max)),
            abi.encodeWithSignature("withdrawTo(address,uint256)", stranger, 1)
        ];
        for (uint256 i = 0; i < targets.length; i++) {
            (PackedUserOperation memory op, bytes32 hash) =
                _signedOpOnLane(SINGLE, abi.encodePacked(targets[i], uint256(0), data[i]), uint32(i));
            _expectExecutionRevert(
                op,
                hash,
                abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, targets[i])
            );
        }
        assertEq(wallet.getConfig().dailyLimitUsd, DAILY_LIMIT);
        assertEq(_agentSpent(), 0, "a refused trade was charged");
    }

    /// @notice THE bypass to close: an agent-signed op whose callData calls `execute` directly would
    ///         run without the module's hooks, so without the agent's limit. The wallet refuses to ask
    ///         a module about any op that does not call executeUserOp.
    function test_trade_mustRunThroughExecuteUserOp() public {
        bytes memory direct = abi.encodeCall(IERC7579Execution.execute, (SINGLE, _transferUsdc(payee, 900e6)));
        (PackedUserOperation memory op, bytes32 hash) =
            sendPackedUserOp.generateUnsignedUserOpWithCallData(address(wallet), config, direct, _agentKey(0));
        op.signature = _agentSignature(hash, agentKey, agent, 1, uint48(block.timestamp + 1 hours));
        _expectFailedOp(op, "AA24 signature error");
        assertEq(usdc.balanceOf(payee), 0);
    }

    /// @notice Inside executeUserOp, the only call an agent may make is the wallet's `execute`.
    function test_trade_innerCallMustBeExecute() public {
        bytes[2] memory inners = [
            abi.encodeCall(IERC7579Execution.executeFromExecutor, (SINGLE, _transferUsdc(payee, 1e6))),
            abi.encodeCall(IERC7579ModuleConfig.uninstallModule, (MODULE_TYPE_VALIDATOR, address(agents), ""))
        ];
        for (uint256 i = 0; i < inners.length; i++) {
            (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
                address(wallet),
                config,
                abi.encodePacked(IAccountExecute.executeUserOp.selector, inners[i]),
                _agentKey(0)
            );
            op.signature = _agentSignature(hash, agentKey, agent, 1, uint48(block.timestamp + 1 hours));
            _expectFailedOpWithRevert(op, abi.encodeWithSelector(AgentModule.AgentModule_NotAnAgentOp.selector));
        }
    }

    /// @notice Every op in a bundle is validated before any runs. A passkey removing the agent earlier
    ///         in the same bundle still stops the agent's op, in execution (preExecute re-checks).
    function test_trade_removedEarlierInTheSameBundle() public {
        (PackedUserOperation memory removal,) =
            _passkeyOpUnsent(address(agents), abi.encodeCall(AgentModule.removeAgent, (address(wallet), agent)));
        (PackedUserOperation memory trade, bytes32 tradeHash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        PackedUserOperation[] memory ops = new PackedUserOperation[](2);
        ops[0] = removal;
        ops[1] = trade;

        vm.expectEmit(true, true, false, true, config.entryPoint);
        emit IEntryPoint.UserOperationRevertReason(
            tradeHash,
            address(wallet),
            trade.nonce,
            abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, address(wallet), agent)
        );
        _sendAll(ops);
        assertEq(usdc.balanceOf(payee), 0);
        assertEq(agents.getAgents(address(wallet)).length, 0);
    }

    /*//////////////////////////////////////////////////////////////
                      THE WALLET'S VALIDATOR ROUTE
    //////////////////////////////////////////////////////////////*/

    /// @notice A 65-byte signature is the owner's route, whatever validator the nonce key names: the
    ///         module is never asked and its hooks never run.
    function test_route_ownerOpNeverReachesAModule() public {
        YesValidator yes = _installYes();
        (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(IERC20.transfer, (payee, 1e6)),
            _keyFor(address(yes), 0)
        );
        op.signature = _ownerSign(hash);
        _send(op);
        assertEq(usdc.balanceOf(payee), 1e6);
        assertEq(yes.pre() + yes.post(), 0, "an owner op ran a module's hooks");
    }

    /// @notice executeUserOp runs only module ops: an owner's op that calls it fails in execution, also
    ///         with a nonce key naming an installed validator, whose hooks never run.
    function test_route_executeUserOpRefusesTheOwnersOp() public {
        YesValidator yes = _installYes();
        bytes memory callData = _agentCallData(SINGLE, _transferUsdc(payee, 1e6));
        uint192[2] memory keys = [uint192(0), _keyFor(address(yes), 0)];
        for (uint256 i = 0; i < keys.length; i++) {
            (PackedUserOperation memory op, bytes32 hash) =
                sendPackedUserOp.generateUnsignedUserOpWithCallData(address(wallet), config, callData, keys[i]);
            op.signature = _ownerSign(hash);
            _expectExecutionRevert(op, hash, abi.encodeWithSelector(SessionHandler.SessionHandler_NotModuleOp.selector));
        }
        assertEq(usdc.balanceOf(payee), 0);
        assertEq(yes.pre() + yes.post(), 0, "an owner op ran a module's hooks");
    }

    /// @notice A passkey op that calls executeUserOp, even with a nonce key naming an installed
    ///         validator, is a passkey op: executeUserOp refuses it and the module's hooks never run.
    function test_route_executeUserOpRefusesAPasskeyOp() public {
        YesValidator yes = _installYes();
        uint192 key = _keyFor(address(yes), 0);
        assertGt(uint48(uint256(key) >> 144), 0, "the passkey route needs a non-zero op deadline");
        (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet), config, _agentCallData(SINGLE, _transferUsdc(payee, 1e6)), key
        );
        op.signature = sessionKey.sign(hash);
        _expectExecutionRevert(op, hash, abi.encodeWithSelector(SessionHandler.SessionHandler_NotModuleOp.selector));
        assertEq(yes.pre() + yes.post(), 0, "a passkey op ran a module's hooks");
        assertEq(usdc.balanceOf(payee), 0);
    }

    /// @notice Nobody but the EntryPoint can call executeUserOp.
    function test_route_executeUserOpOnlyFromTheEntryPoint() public {
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        bytes memory call = abi.encodePacked(IAccountExecute.executeUserOp.selector, abi.encode(op, hash));
        address[2] memory callers = [stranger, owner];
        for (uint256 i = 0; i < callers.length; i++) {
            vm.prank(callers[i]);
            (bool ok, bytes memory reason) = address(wallet).call(call);
            assertFalse(ok);
            assertEq(reason, abi.encodeWithSignature("AccountUnauthorized(address)", callers[i]));
        }
        assertEq(usdc.balanceOf(payee), 0);
    }

    /// @notice Whatever a validator approves, inside executeUserOp the wallet runs nothing but its own
    ///         `execute`.
    function test_route_innerCallMustBeExecute() public {
        YesValidator yes = _installYes();
        bytes memory inner =
            abi.encodeCall(IERC7579ModuleConfig.uninstallModule, (MODULE_TYPE_VALIDATOR, address(agents), ""));
        (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet),
            config,
            abi.encodePacked(IAccountExecute.executeUserOp.selector, inner),
            _keyFor(address(yes), 0)
        );
        op.signature = hex"01";
        _expectExecutionRevert(op, hash, abi.encodeWithSelector(SessionHandler.SessionHandler_NotModuleOp.selector));
        assertTrue(wallet.isModuleInstalled(MODULE_TYPE_VALIDATOR, address(agents), ""));
        assertEq(yes.pre(), 0);
    }

    /// @notice Whatever a validator approves, an op it approves must call executeUserOp: one calling
    ///         `execute` directly would skip the module's hooks, so the module is not even asked.
    function test_route_moduleOpMustCallExecuteUserOp() public {
        YesValidator yes = _installYes();
        bytes memory direct = abi.encodeCall(IERC7579Execution.execute, (SINGLE, _transferUsdc(payee, 1e6)));
        (PackedUserOperation memory op,) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet), config, direct, _keyFor(address(yes), 0)
        );
        op.signature = hex"01";
        _expectFailedOp(op, "AA24 signature error");
    }

    /// @notice The self-call gives a module op everything a passkey op gets, in order: the module's
    ///         preExecute, the guard, the protocol fee, the spending-limit hook (which reads the calls
    ///         out of the execute calldata), then postExecute.
    function test_route_selfCallGetsTheGuardTheFeeAndTheHook() public {
        YesValidator yes = _installYes();
        uint256 treasuryBefore = address(treasury).balance;

        // A plain transfer: charged to the wallet's limit, fee paid, both hooks ran once.
        _yesOp(yes, SINGLE, _transferUsdc(payee, 10e6), 0, "");
        assertEq(usdc.balanceOf(payee), 10e6);
        assertEq(_walletSpent(), _usd(address(usdc), 10e6));
        assertEq(address(treasury).balance, treasuryBefore + registry.getFee());
        assertEq(yes.pre(), 1);
        assertEq(yes.post(), 1);

        // The guard: the wallet itself is off limits.
        _yesOp(
            yes,
            SINGLE,
            abi.encodePacked(address(wallet), uint256(0), abi.encodeCall(SessionHandler.pause, ())),
            1,
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(wallet))
        );

        // The hook read the calls: an approval left standing is refused, which it can only know
        // from the execute calldata.
        _yesOp(
            yes,
            SINGLE,
            abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(IERC20.approve, (stranger, 1e6))),
            2,
            abi.encodeWithSelector(
                    SpendingLimitModule.SpendingLimitModule_StandingApprovalNotAllowed.selector,
                    address(usdc),
                    stranger,
                    1e6
                )
        );
        assertEq(usdc.allowance(address(wallet), stranger), 0);
    }

    /// @notice A nonce key naming a validator the wallet never installed: nobody is asked, the op fails.
    function test_route_uninstalledValidatorFails() public {
        YesValidator notInstalled = new YesValidator();
        (PackedUserOperation memory op,) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet),
            config,
            _agentCallData(SINGLE, _transferUsdc(payee, 1e6)),
            _keyFor(address(notInstalled), 0)
        );
        op.signature = hex"01";
        _expectFailedOp(op, "AA24 signature error");
    }

    /// @notice A module's own revert in validation is passed on, with its reason.
    function test_route_moduleRevertIsPassedOn() public {
        YesValidator yes = _installYes();
        yes.setRefuseLoudly(true);
        (PackedUserOperation memory op,) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet), config, _agentCallData(SINGLE, _transferUsdc(payee, 1e6)), _keyFor(address(yes), 0)
        );
        op.signature = hex"01";
        _expectFailedOpWithRevert(op, abi.encodeWithSignature("Error(string)", "YesValidator says no"));
    }

    /*//////////////////////////////////////////////////////////////
                     THE MODULE'S EXECUTION HOOKS
    //////////////////////////////////////////////////////////////*/

    /// @notice postExecute with no preExecute open has nothing to charge from: refused.
    function test_hooks_postWithoutPreRefused() public {
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        vm.prank(address(wallet));
        (bool ok, bytes memory reason) = address(agents).call(_hookCall(IUserOpHooks.postExecute.selector, op, hash));
        assertFalse(ok);
        assertEq(reason, abi.encodeWithSelector(AgentModule.AgentModule_NoTradeOpen.selector, address(wallet)));
    }

    /// @notice A trade closes what it opened: after a real agent op, in the same transaction, a second
    ///         postExecute finds nothing open, so it can't charge from a stale note.
    function test_hooks_aTradeLeavesNothingOpen() public {
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        _send(op);
        vm.prank(address(wallet));
        (bool ok, bytes memory reason) = address(agents).call(_hookCall(IUserOpHooks.postExecute.selector, op, hash));
        assertFalse(ok);
        assertEq(reason, abi.encodeWithSelector(AgentModule.AgentModule_NoTradeOpen.selector, address(wallet)));
    }

    /// @notice The wallet itself (a passkey) calling the hooks outside an agent op can only ADD to the
    ///         agent's spend -- here, one fee -- never take any off.
    function test_hooks_walletCanOnlyAddToTheSpend() public {
        _run(SINGLE, _transferUsdc(payee, 50e6));
        uint256 spent = _agentSpent();
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));

        vm.startPrank(address(wallet));
        (bool okPre,) = address(agents).call(_hookCall(IUserOpHooks.preExecute.selector, op, hash));
        (bool okPost,) = address(agents).call(_hookCall(IUserOpHooks.postExecute.selector, op, hash));
        vm.stopPrank();
        assertTrue(okPre && okPost);
        assertEq(_agentSpent(), spent + _feeUsd());
    }

    /// @notice postExecute charges only an agent that still exists: one removed after preExecute (the
    ///         wallet itself doing both, outside any agent op) is refused, not written back.
    function test_hooks_postForARemovedAgentRefused() public {
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        vm.startPrank(address(wallet));
        (bool okPre,) = address(agents).call(_hookCall(IUserOpHooks.preExecute.selector, op, hash));
        agents.removeAgent(address(wallet), agent);
        (bool okPost, bytes memory reason) =
            address(agents).call(_hookCall(IUserOpHooks.postExecute.selector, op, hash));
        vm.stopPrank();
        assertTrue(okPre);
        assertFalse(okPost);
        assertEq(reason, abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, address(wallet), agent));
        (AgentModule.Agent memory info,,,) = agents.getAgent(address(wallet), agent);
        assertEq(info.spentUsd, 0, "a removed agent's record was written back");
    }

    /// @notice Anyone else calling the hooks only reaches its own (empty) records.
    function test_hooks_strangerReachesOnlyItself() public {
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        vm.prank(stranger);
        (bool ok, bytes memory reason) = address(agents).call(_hookCall(IUserOpHooks.preExecute.selector, op, hash));
        assertFalse(ok);
        assertEq(reason, abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, stranger, agent));
    }

    /*//////////////////////////////////////////////////////////////
                   RULE 1: NATIVE ETH/BNB MUST BE METERED
    //////////////////////////////////////////////////////////////*/

    /// @notice Turning metering off afterwards stops the wallet's agents until it is back on.
    function test_rule1_tradesStopWhileNativeIsNotMetered() public {
        vm.prank(owner);
        wallet.setNativeMetering(false, address(0));
        (PackedUserOperation memory op,) = _signedOp(SINGLE, abi.encodePacked(stranger, uint256(1 ether), bytes("")));
        _expectFailedOpWithRevert(
            op, abi.encodeWithSelector(AgentModule.AgentModule_NativeNotMetered.selector, address(wallet))
        );

        vm.prank(owner);
        wallet.setNativeMetering(true, address(0));
        _run(SINGLE, abi.encodePacked(payee, uint256(0.01 ether), bytes("")));
        assertEq(payee.balance, 0.01 ether);
    }

    /*//////////////////////////////////////////////////////////////
               RULE 2: NO UNCOUNTED TOKEN MAY LEAVE THE WALLET
    //////////////////////////////////////////////////////////////*/

    function test_rule2_uncountedTransferRefused() public {
        _expectUncounted(address(xyz), abi.encodeCall(IERC20.transfer, (stranger, 1e18)));
    }

    function test_rule2_uncountedApproveRefused() public {
        _expectUncounted(address(xyz), abi.encodeCall(IERC20.approve, (stranger, 1e18)));
    }

    /// @notice Every function on the list is refused on an uncounted token, whatever its arguments.
    function test_rule2_everyListedFunctionRefused() public {
        bytes4[24] memory selectors = [
            bytes4(0xa9059cbb),
            0x23b872dd,
            0x095ea7b3,
            0x39509351,
            0xd73dd623,
            0x42842e0e,
            0xb88d4fde,
            0xa22cb465,
            0xf242432a,
            0x2eb2c2d6,
            0x1296ee62,
            0x4000aea0,
            0xd8fbe994,
            0xc1d34b89,
            0x3177029f,
            0x9bd9bbc6,
            0x62ad1b83,
            0x959b8c3f,
            0x095bcdb6,
            0xfe99049a,
            0x426a8493,
            0x558a7297,
            0xb753a98c,
            0xbb35783b
        ];
        for (uint256 i = 0; i < selectors.length; i++) {
            _expectUncounted(address(xyz), abi.encodePacked(selectors[i], abi.encode(stranger, uint256(1))));
        }
    }

    /// @notice A refused call anywhere in a batch refuses the whole batch.
    function test_rule2_refusedInsideABatch() public {
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(IERC20.transfer, (payee, 1e6)));
        execs[1] = Execution(address(xyz), 0, abi.encodeCall(IERC20.transfer, (stranger, 1e18)));
        (PackedUserOperation memory op,) = _signedOp(BATCH, ERC7579Utils.encodeBatch(execs));
        _expectFailedOpWithRevert(
            op,
            abi.encodeWithSelector(
                AgentModule.AgentModule_UncountedToken.selector, address(xyz), IERC20.transfer.selector
            )
        );
        assertEq(usdc.balanceOf(payee), 0);
    }

    /// @notice A token capped in its own units is counted by the wallet, but not in the agent's USD:
    ///         refused too.
    function test_rule2_cappedTokenRefused() public {
        vm.prank(owner);
        wallet.setTokenCap(address(xyz), 100e18);
        _expectUncounted(address(xyz), abi.encodeCall(IERC20.transfer, (stranger, 1e18)));
    }

    /// @notice What the rule allows and what it costs: an agent may BUY a token the limit doesn't
    ///         count (paying with a counted one is charged in full), but can't sell it.
    function test_rule2_canBuyAnUncountedTokenButNotSellIt() public {
        uint256 amount = 100e6;
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(IERC20.approve, (address(exchange), amount)));
        execs[1] =
            Execution(address(exchange), 0, abi.encodeCall(MockSwap.swap, (IERC20(address(usdc)), amount, xyz, 50e18)));
        uint256 xyzBefore = xyz.balanceOf(address(wallet));
        _run(BATCH, ERC7579Utils.encodeBatch(execs));
        assertEq(xyz.balanceOf(address(wallet)), xyzBefore + 50e18);
        assertEq(_walletSpent(), _usd(address(usdc), amount), "paid in full: the token bought isn't counted");

        _expectUncounted(address(xyz), abi.encodeCall(IERC20.approve, (address(exchange), 50e18)));
    }

    /*//////////////////////////////////////////////////////////////
                    RULE 3: NO CALL TO THE AGENT MODULE
    //////////////////////////////////////////////////////////////*/

    /// @notice An agent can't remove, raise or add agents through its own trade (it acts as the wallet,
    ///         which may otherwise remove), nor touch the module's execution bookkeeping.
    function test_rule3_agentCantReachTheModule() public {
        bytes[5] memory attempts = [
            abi.encodeCall(AgentModule.removeAgent, (address(wallet), agent)),
            abi.encodeCall(AgentModule.updateAgent, (address(wallet), agent, type(uint128).max, uint48(START + 1))),
            abi.encodeCall(AgentModule.onUninstall, ("")),
            abi.encodePacked(IUserOpHooks.preExecute.selector, bytes32(0)),
            abi.encodePacked(IUserOpHooks.postExecute.selector, bytes32(0))
        ];
        for (uint256 i = 0; i < attempts.length; i++) {
            (PackedUserOperation memory op,) =
                _signedOpOnLane(SINGLE, abi.encodePacked(address(agents), uint256(0), attempts[i]), uint32(i));
            _expectFailedOpWithRevert(
                op, abi.encodeWithSelector(AgentModule.AgentModule_TargetNotAllowed.selector, address(agents))
            );
        }
        assertEq(agents.getAgents(address(wallet)).length, 1);
    }

    /*//////////////////////////////////////////////////////////////
                           THE AGENT'S LIMIT
    //////////////////////////////////////////////////////////////*/

    /// @notice Over the agent's limit, the trade reverts as a whole -- in execution, since what it costs
    ///         is only known once it ran -- so nothing moves, but the op (and its gas) is spent.
    function test_limit_agentLimitStopsATrade() public {
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, 50e18, uint48(block.timestamp + 1 days));
        uint256 value = _usd(address(usdc), 100e6);
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 100e6));
        _expectExecutionRevert(
            op,
            hash,
            abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, value + _feeUsd(), 50e18)
        );
        assertEq(usdc.balanceOf(payee), 0);
        assertEq(_walletSpent(), 0, "the wallet's limit kept a reverted trade");
        assertEq(_agentSpent(), 0);
        assertGt(wallet.sessionGasSpent(), 0, "the failed op cost no gas");
    }

    /// @notice The wallet's own limit binds too, whichever is lower.
    function test_limit_walletLimitStillBinds() public {
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, type(uint128).max, uint48(block.timestamp + 1 days));
        uint256 amount = 6000e6;
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, amount));
        _expectExecutionRevert(
            op,
            hash,
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector,
                int256(_usd(address(usdc), amount)),
                DAILY_LIMIT
            )
        );
        assertEq(usdc.balanceOf(payee), 0);
    }

    /// @notice Fees count: a leaked key sending empty trades runs out of limit instead of draining the
    ///         wallet's ETH in fees.
    function test_limit_feesCountSoEmptyTradesStop() public {
        uint256 fee = _feeUsd();
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, uint128(3 * fee + 1), uint48(block.timestamp + 1 days));
        bytes memory empty = abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(IERC20.balanceOf, (payee)));
        for (uint256 i = 0; i < 3; i++) {
            _run(SINGLE, empty);
        }
        assertEq(_agentSpent(), 3 * fee);
        assertEq(_walletSpent(), 0, "an empty trade moved the wallet's limit");

        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, empty);
        _expectExecutionRevert(
            op, hash, abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, 4 * fee, 3 * fee + 1)
        );
    }

    /// @notice The agent's limit runs on the wallet's window: a new window gives it its whole limit
    ///         back, including when the window rolls inside the agent's own trade.
    function test_limit_startsOverWithTheWalletsWindow() public {
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, 150e18, uint48(block.timestamp + 30 days));
        _run(SINGLE, _transferUsdc(payee, 100e6));

        (PackedUserOperation memory again, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 100e6));
        uint256 value = _usd(address(usdc), 100e6);
        _expectExecutionRevert(
            again,
            hash,
            abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, 2 * (value + _feeUsd()), 150e18)
        );

        _warp(START + WINDOW);
        (, uint256 spent, uint256 remaining,) = agents.getAgent(address(wallet), agent);
        assertEq(spent, 0, "the view still counts the old window");
        assertEq(remaining, 150e18);

        // The window rolls in this trade's own preCheck.
        _run(SINGLE, _transferUsdc(payee, 100e6));
        assertEq(_agentSpent(), value + _feeUsd(), "the old window's spend was carried over");
        assertEq(_walletSpent(), value);
    }

    /// @notice A window the owner started (with a trade of their own) counts for the agent as well.
    function test_limit_windowStartedByAnotherTradeCounts() public {
        _run(SINGLE, _transferUsdc(payee, 100e6));
        _warp(START + WINDOW + 1 hours);
        vm.prank(owner);
        wallet.execute(SINGLE, _transferUsdc(payee, 1e6));

        _run(SINGLE, _transferUsdc(payee, 10e6));
        assertEq(_agentSpent(), _usd(address(usdc), 10e6) + _feeUsd());
    }

    /// @notice A limit lowered below what the agent spent stops it until the window starts over.
    function test_limit_loweredBelowSpent() public {
        _run(SINGLE, _transferUsdc(payee, 100e6));
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, 10e18, uint48(block.timestamp + 1 days));
        (,, uint256 remaining,) = agents.getAgent(address(wallet), agent);
        assertEq(remaining, 0);

        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, 1e6));
        uint256 expected = _agentSpent() + _usd(address(usdc), 1e6) + _feeUsd();
        _expectExecutionRevert(
            op, hash, abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, expected, 10e18)
        );
    }

    /// @notice For any amount and agent limit: the trade goes through exactly when it fits both
    ///         limits, and then the agent is charged its value plus the fee.
    function testFuzz_limit_chargesExactlyWhatFits(uint256 amount, uint128 limit) public {
        amount = bound(amount, 1, 20_000e6);
        limit = uint128(bound(limit, 1, 10_000e18));
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, limit, uint48(block.timestamp + 1 days));

        uint256 value = _usd(address(usdc), amount);
        uint256 fee = _feeUsd();
        (PackedUserOperation memory op, bytes32 hash) = _signedOp(SINGLE, _transferUsdc(payee, amount));

        if (value > uint256(DAILY_LIMIT)) {
            _expectExecutionRevert(
                op,
                hash,
                abi.encodeWithSelector(
                    SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, int256(value), DAILY_LIMIT
                )
            );
        } else if (value + fee > limit) {
            _expectExecutionRevert(
                op,
                hash,
                abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, value + fee, limit)
            );
        } else {
            _send(op);
        }

        if (value <= uint256(DAILY_LIMIT) && value + fee <= limit) {
            assertEq(usdc.balanceOf(payee), amount);
            assertEq(_agentSpent(), value + fee);
        } else {
            assertEq(usdc.balanceOf(payee), 0);
            assertEq(_agentSpent(), 0);
        }
    }

    /*//////////////////////////////////////////////////////////////
                                 GAS
    //////////////////////////////////////////////////////////////*/

    /// @notice Gas of one whole handleOps for the same USDC transfer, sent by the agent and by a
    ///         passkey. Mocks are cheaper than real tokens and feeds. Read with -vv.
    function test_gas_agentOpBesideAPasskeyOp() public {
        // Warm-up: the payee's balance slot and both nonce keys exist afterwards.
        _run(SINGLE, _transferUsdc(payee, 1e6));
        _passkeyOp(address(usdc), abi.encodeCall(IERC20.transfer, (payee, 1e6)));

        // A fresh lane for each, as the app picks one: both pay for a new nonce key.
        (PackedUserOperation memory agentOp,) = _signedOpOnLane(SINGLE, _transferUsdc(payee, 1e6), 99);
        _cool();
        uint256 before = gasleft();
        _send(agentOp);
        uint256 agentGas = before - gasleft();

        (PackedUserOperation memory passkeyOp,) =
            _passkeyOpUnsent(address(usdc), abi.encodeCall(IERC20.transfer, (payee, 1e6)));
        _cool();
        before = gasleft();
        _send(passkeyOp);
        uint256 passkeyGas = before - gasleft();

        console2.log("agent op, ERC-20 transfer, handleOps (gas):  ", agentGas);
        console2.log("passkey op, ERC-20 transfer, handleOps (gas):", passkeyGas);
    }

    /*//////////////////////////////////////////////////////////////
                               HELPERS
    //////////////////////////////////////////////////////////////*/

    function _transferUsdc(address to, uint256 amount) internal view returns (bytes memory) {
        return abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(IERC20.transfer, (to, amount)));
    }

    /// @dev A nonce key naming `validator`, with a 4-byte lane under it.
    function _keyFor(address validator, uint32 lane) internal pure returns (uint192) {
        return uint192((uint256(uint160(validator)) << 32) | lane);
    }

    function _agentKey(uint32 lane) internal view returns (uint192) {
        return _keyFor(address(agents), lane);
    }

    /// @dev An agent op's callData: executeUserOp's selector, then the wallet's execute call.
    function _agentCallData(bytes32 mode, bytes memory calls) internal pure returns (bytes memory) {
        return abi.encodePacked(
            IAccountExecute.executeUserOp.selector, abi.encodeCall(IERC7579Execution.execute, (mode, calls))
        );
    }

    /// @dev An unsigned agent op for `calls` on `lane`, and its hash.
    function _agentOp(bytes32 mode, bytes memory calls, uint32 lane)
        internal
        view
        returns (PackedUserOperation memory op, bytes32 hash)
    {
        return sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet), config, _agentCallData(mode, calls), _agentKey(lane)
        );
    }

    /// @dev `signerAgent ‖ epoch ‖ deadline ‖ r ‖ s ‖ v`, signed with `key`.
    function _agentSignature(bytes32 hash, uint256 key, address signerAgent, uint32 epoch, uint48 deadline)
        internal
        view
        returns (bytes memory)
    {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, agents.hashOperation(hash, deadline, epoch));
        return abi.encodePacked(signerAgent, epoch, deadline, r, s, v);
    }

    /// @dev The agent's op for `calls` on `lane`, signed at its current epoch, an hour to run.
    function _signedOpOnLane(bytes32 mode, bytes memory calls, uint32 lane)
        internal
        view
        returns (PackedUserOperation memory op, bytes32 hash)
    {
        (op, hash) = _agentOp(mode, calls, lane);
        (AgentModule.Agent memory info,,,) = agents.getAgent(address(wallet), agent);
        op.signature = _agentSignature(hash, agentKey, agent, info.epoch, uint48(block.timestamp + 1 hours));
    }

    function _signedOp(bytes32 mode, bytes memory calls)
        internal
        view
        returns (PackedUserOperation memory op, bytes32 hash)
    {
        return _signedOpOnLane(mode, calls, 0);
    }

    /// @dev Signs and sends one agent op.
    function _run(bytes32 mode, bytes memory calls) internal {
        (PackedUserOperation memory op,) = _signedOp(mode, calls);
        _send(op);
    }

    function _send(PackedUserOperation memory op) internal {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        _sendAll(ops);
    }

    function _sendAll(PackedUserOperation[] memory ops) internal {
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /// @dev Expects handleOps to refuse `op` in validation with a plain FailedOp.
    function _expectFailedOp(PackedUserOperation memory op, string memory reason) internal {
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, reason));
        _send(op);
    }

    /// @dev Expects handleOps to refuse `op` in validation because the wallet reverted with `inner`.
    function _expectFailedOpWithRevert(PackedUserOperation memory op, bytes memory inner) internal {
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOpWithRevert.selector, 0, "AA23 reverted", inner));
        _send(op);
    }

    /// @dev Expects `op` to pass validation and revert in execution with `reason`.
    function _expectExecutionRevert(PackedUserOperation memory op, bytes32 hash, bytes memory reason) internal {
        vm.expectEmit(true, true, false, true, config.entryPoint);
        emit IEntryPoint.UserOperationRevertReason(hash, address(wallet), op.nonce, reason);
        _send(op);
    }

    /// @dev Expects the agent's single call `callData` to `token` to be refused by rule 2.
    function _expectUncounted(address token, bytes memory callData) internal {
        (PackedUserOperation memory op,) = _signedOp(SINGLE, abi.encodePacked(token, uint256(0), callData));
        _expectFailedOpWithRevert(
            op, abi.encodeWithSelector(AgentModule.AgentModule_UncountedToken.selector, token, bytes4(callData))
        );
    }

    /// @dev Installs a YesValidator on the wallet, as only the owner can.
    function _installYes() internal returns (YesValidator yes) {
        yes = new YesValidator();
        vm.prank(owner);
        wallet.installModule(MODULE_TYPE_VALIDATOR, address(yes), "");
    }

    /// @dev Sends a YesValidator op on `lane`; an empty `reason` expects success, any other the
    ///      execution revert it names.
    function _yesOp(YesValidator yes, bytes32 mode, bytes memory calls, uint32 lane, bytes memory reason) internal {
        (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOpWithCallData(
            address(wallet), config, _agentCallData(mode, calls), _keyFor(address(yes), lane)
        );
        op.signature = hex"01";
        if (reason.length == 0) _send(op);
        else _expectExecutionRevert(op, hash, reason);
    }

    /// @dev The calldata the wallet sends a module's hook: the selector, then (userOp, userOpHash).
    function _hookCall(bytes4 selector, PackedUserOperation memory op, bytes32 hash)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(selector, abi.encode(op, hash));
    }

    /// @dev The owner's 65-byte signature: EIP-191 over the op's hash.
    function _ownerSign(bytes32 hash) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) =
            vm.sign(ANVIL_OWNER_KEY, keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", hash)));
        return abi.encodePacked(r, s, v);
    }

    function _copy(PackedUserOperation memory op) internal pure returns (PackedUserOperation memory copy) {
        copy = PackedUserOperation({
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

    /// @dev `data` with byte `index` set to `value`, as a new copy.
    function _withByte(bytes memory data, uint256 index, bytes1 value) internal pure returns (bytes memory copy) {
        copy = bytes.concat(data);
        copy[index] = value;
    }

    /// @dev `data` cut or zero-padded to `length` bytes, as a new copy.
    function _resized(bytes memory data, uint256 length) internal pure returns (bytes memory copy) {
        copy = new bytes(length);
        for (uint256 i = 0; i < length && i < data.length; i++) {
            copy[i] = data[i];
        }
    }

    function _feeUsd() internal view returns (uint256) {
        (, uint256 fee,) = registry.feeConfig();
        return uint256(oracle.getPrice(address(0), fee));
    }

    function _usd(address token, uint256 amount) internal view returns (uint256) {
        return uint256(oracle.getPrice(token, amount));
    }

    function _agentSpent() internal view returns (uint256 spent) {
        (, spent,,) = agents.getAgent(address(wallet), agent);
    }

    function _walletSpent() internal view returns (uint256) {
        return uint256(int256(wallet.getConfig().spentInWindow));
    }

    /// @dev A real passkey UserOp from the wallet, through the EntryPoint.
    function _passkeyOp(address dest, bytes memory data) internal {
        (PackedUserOperation memory op,) = _passkeyOpUnsent(dest, data);
        _send(op);
    }

    /// @dev A passkey op on a fresh lane, signed but not sent.
    function _passkeyOpUnsent(address dest, bytes memory data)
        internal
        returns (PackedUserOperation memory op, bytes32 hash)
    {
        (op, hash) = sendPackedUserOp.generateUnsignedUserOp(
            address(wallet), config, dest, 0, data, PasskeyHelper.nonceKey(PasskeyHelper.LATEST_OP_DEADLINE, ++_lane)
        );
        op.signature = sessionKey.sign(hash);
    }

    uint144 private _lane;

    /// @dev Moves time and refreshes the feeds the wallet prices with, which would otherwise go stale.
    function _warp(uint256 to) internal {
        vm.warp(to);
        MockV3Aggregator(config.ethUsdPriceFeed).updateAnswer(MockV3Aggregator(config.ethUsdPriceFeed).latestAnswer());
        MockV3Aggregator(config.usdcUsdPriceFeed).updateAnswer(MockV3Aggregator(config.usdcUsdPriceFeed).latestAnswer());
        MockV3Aggregator(config.daiUsdPriceFeed).updateAnswer(MockV3Aggregator(config.daiUsdPriceFeed).latestAnswer());
    }

    /// @dev Cools what the test touched, so reads are priced as in a fresh transaction.
    function _cool() internal {
        vm.cool(address(wallet));
        vm.cool(address(agents));
        vm.cool(address(module));
        vm.cool(address(registry));
        vm.cool(address(oracle));
        vm.cool(address(usdc));
        vm.cool(address(dai));
        vm.cool(address(treasury));
        vm.cool(payee);
        vm.cool(config.entryPoint);
        vm.cool(config.ethUsdPriceFeed);
        vm.cool(config.usdcUsdPriceFeed);
        vm.cool(config.daiUsdPriceFeed);
    }
}
