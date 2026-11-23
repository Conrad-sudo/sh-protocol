//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {MODULE_TYPE_EXECUTOR, Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {AgentModule} from "../../src/AgentModule.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
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

/// @dev Called by an agent's trade, it tries to start a second trade from inside the first and
///      records how that was refused.
contract Reenterer {
    AgentModule public immutable AGENTS;
    bytes private _inner;
    bytes4 public caught;

    constructor(AgentModule agents) {
        AGENTS = agents;
    }

    function arm(bytes memory inner) external {
        _inner = inner;
    }

    function poke() external {
        (bool ok, bytes memory reason) = address(AGENTS).call(_inner);
        if (!ok && reason.length >= 4) caught = bytes4(reason);
    }
}

/**
 * @title AgentModuleTest
 * @notice AgentModule on a real SessionHandler wallet: only the owner adds or raises an agent; the
 *         owner or a passkey removes one; signatures, nonces, deadlines and epochs; the three rules
 *         (native metered, no uncounted token leaves, no call to the module); the agent's own limit,
 *         fees included, on the wallet's window; and every trade still bound by the wallet's guard,
 *         fee, pause and USD limit.
 * @dev Every attack here is tested as a refusal. Mocks price USDC and DAI at $1 and ETH at the
 *      HelperConfig figure; expected values are read from the oracle, never hardcoded.
 */
contract AgentModuleTest is Test {
    using PasskeyHelper for PasskeyHelper.Passkey;

    /// @dev The signed type, as a struct, for Forge's EIP-712 encoder.
    struct AgentExecution {
        address account;
        bytes32 mode;
        bytes executionCalldata;
        uint256 nonce;
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

    PasskeyHelper.Passkey sessionKey;
    address owner;
    address agent;
    uint256 agentKey;
    address payee = makeAddr("payee");
    address relayer = makeAddr("relayer");
    address stranger = makeAddr("stranger");
    address bundler = makeAddr("bundler");

    function setUp() public {
        vm.warp(START);
        sessionKey = PasskeyHelper.create("sessionKey");

        (factory, treasury, config, oracle) = new DeploySHProtocol().run();
        registry = factory.REGISTRY();
        module = SpendingLimitModule(registry.spendingLimitModule());
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

        agents = new AgentModule();
        (agent, agentKey) = makeAddrAndKey("agent");
        vm.startPrank(owner);
        wallet.installModule(MODULE_TYPE_EXECUTOR, address(agents), "");
        agents.addAgent(address(wallet), agent, AGENT_LIMIT, uint48(block.timestamp + 30 days));
        vm.stopPrank();
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
        wallet.uninstallModule(MODULE_TYPE_EXECUTOR, address(agents), "");
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
        wallet.uninstallModule(MODULE_TYPE_EXECUTOR, address(agents), "");
        assertEq(agents.getAgents(address(wallet)).length, 0);

        vm.startPrank(owner);
        wallet.installModule(MODULE_TYPE_EXECUTOR, address(agents), "");
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

    /// @notice A signed trade sent by someone else: the payee is paid, the wallet's limit and the
    ///         agent's limit are both charged, the agent's with the fee on top, and the treasury gets
    ///         the protocol fee plus the keeper fee.
    function test_trade_signedAndRelayed() public {
        uint256 amount = 100e6;
        uint256 value = _usd(address(usdc), amount);
        uint256 fee = _feeUsd();
        assertGt(fee, 0, "these tests assume a fee");
        uint256 treasuryBefore = address(treasury).balance;

        bytes memory calls = _transferUsdc(payee, amount);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 0);
        vm.expectEmit(address(agents));
        emit AgentModule.AgentExecuted(address(wallet), agent, 0, value, fee, value + fee);
        vm.prank(relayer);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);

        assertEq(usdc.balanceOf(payee), amount);
        assertEq(_walletSpent(), value, "the wallet's limit");
        assertEq(_agentSpent(), value + fee, "the agent's limit");
        assertEq(address(treasury).balance, treasuryBefore + registry.getFee() + registry.keeperFee(), "fees");
        assertTrue(agents.isNonceUsed(address(wallet), agent, 0));
    }

    /// @notice The agent may send its own trade, with no signature.
    function test_trade_agentSendsItself() public {
        bytes memory calls = _transferUsdc(payee, 1e6);
        vm.prank(agent);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, uint48(block.timestamp), "");
        assertEq(usdc.balanceOf(payee), 1e6);
    }

    function test_trade_strangerWithoutSignatureRefused() public {
        bytes memory calls = _transferUsdc(stranger, 1e6);
        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        vm.prank(stranger);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, uint48(block.timestamp), "");
    }

    /// @notice Any protocol: approve a counted token to some vault and deposit, in one batch. The
    ///         deposit leaves the wallet, so both limits are charged.
    function test_trade_depositIntoAnotherProtocolIsCharged() public {
        uint256 amount = 200e6;
        Execution[] memory execs = new Execution[](2);
        execs[0] = Execution(address(usdc), 0, abi.encodeCall(IERC20.approve, (address(vault), amount)));
        execs[1] = Execution(address(vault), 0, abi.encodeCall(MockVault.deposit, (IERC20(address(usdc)), amount)));
        _run(BATCH, ERC7579Utils.encodeBatch(execs), 0);

        uint256 value = _usd(address(usdc), amount);
        assertEq(usdc.balanceOf(address(vault)), amount);
        assertEq(_walletSpent(), value);
        assertEq(_agentSpent(), value + _feeUsd());
    }

    /// @notice Plain ETH sent anywhere is charged, since native metering must be on.
    function test_trade_nativeSendIsCharged() public {
        _run(SINGLE, abi.encodePacked(payee, uint256(0.1 ether), bytes("")), 0);
        uint256 value = _usd(address(0), 0.1 ether);
        assertEq(payee.balance, 0.1 ether);
        assertEq(_walletSpent(), value);
        assertEq(_agentSpent(), value + _feeUsd());
    }

    /// @notice The owner's own trades and a passkey's don't use the agent's limit.
    function test_trade_ownersSpendIsNotTheAgents() public {
        _run(SINGLE, _transferUsdc(payee, 10e6), 0);
        uint256 agentSpent = _agentSpent();
        vm.prank(owner);
        wallet.execute(SINGLE, _transferUsdc(payee, 500e6));
        assertEq(_agentSpent(), agentSpent);
    }

    /*//////////////////////////////////////////////////////////////
                             SIGNATURES
    //////////////////////////////////////////////////////////////*/

    function test_sig_otherKeyRefused() public {
        bytes memory calls = _transferUsdc(stranger, 1e6);
        uint48 deadline = uint48(block.timestamp + 1 hours);
        (, uint256 otherKey) = makeAddrAndKey("other");
        bytes memory sig = _sign(otherKey, address(wallet), SINGLE, calls, 0, deadline, 1);
        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);
    }

    /// @notice What was signed is what runs: a bigger amount, another wallet, another mode or another
    ///         nonce than the agent signed are all refused.
    function test_sig_coversEveryField() public {
        bytes memory calls = _transferUsdc(payee, 1e6);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 0);

        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, _transferUsdc(payee, 2e6), 0, deadline, sig);

        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 1, deadline, sig);

        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline + 1, sig);

        bytes memory forOther = _sign(agentKey, makeAddr("otherWallet"), SINGLE, calls, 0, deadline, 1);
        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, forOther);

        bytes memory forEpoch2 = _sign(agentKey, address(wallet), SINGLE, calls, 0, deadline, 2);
        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, forEpoch2);

        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, hex"1234");
    }

    function test_sig_otherChainRefused() public {
        bytes memory calls = _transferUsdc(payee, 1e6);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 0);
        vm.chainId(block.chainid + 1);
        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);
    }

    function test_sig_otherModuleRefused() public {
        AgentModule other = new AgentModule();
        bytes memory calls = _transferUsdc(payee, 1e6);
        uint48 deadline = uint48(block.timestamp + 1 hours);
        bytes32 digest = other.hashExecution(address(wallet), SINGLE, calls, 0, deadline, 1);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(agentKey, digest);
        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, abi.encodePacked(r, s, v));
    }

    function test_sig_replayRefused_outOfOrderFine() public {
        bytes memory calls = _transferUsdc(payee, 1e6);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 5);
        agents.execute(address(wallet), agent, SINGLE, calls, 5, deadline, sig);

        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_NonceUsed.selector, 5));
        agents.execute(address(wallet), agent, SINGLE, calls, 5, deadline, sig);

        // A lower nonce, and one in another word of the bitmap, still work.
        _run(SINGLE, calls, 3);
        _run(SINGLE, calls, 300);
        assertTrue(agents.isNonceUsed(address(wallet), agent, 3));
        assertTrue(agents.isNonceUsed(address(wallet), agent, 300));
        assertFalse(agents.isNonceUsed(address(wallet), agent, 4));
        assertEq(usdc.balanceOf(payee), 3e6);
    }

    function test_sig_deadlinePassedRefused() public {
        bytes memory calls = _transferUsdc(payee, 1e6);
        uint48 deadline = uint48(block.timestamp - 1);
        bytes memory sig = _sign(agentKey, address(wallet), SINGLE, calls, 0, deadline, 1);
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_DeadlinePassed.selector, deadline));
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);
    }

    /// @notice Removing an agent voids every signature it made, even when the same address is added
    ///         back: the epoch moved on.
    function test_sig_removalVoidsOldSignatures() public {
        bytes memory calls = _transferUsdc(payee, 1e6);
        (uint48 deadline, bytes memory oldSig) = _signed(SINGLE, calls, 9);

        vm.startPrank(owner);
        agents.removeAgent(address(wallet), agent);
        agents.addAgent(address(wallet), agent, AGENT_LIMIT, uint48(block.timestamp + 1 days));
        vm.stopPrank();

        vm.expectRevert(AgentModule.AgentModule_BadSignature.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 9, deadline, oldSig);

        _run(SINGLE, calls, 9);
        assertEq(usdc.balanceOf(payee), 1e6);
    }

    /// @notice hashExecution is plain EIP-712, checked against Forge's own encoder, so an agent's
    ///         ordinary signTypedData produces it.
    function test_hashExecution_isStandardEip712() public view {
        bytes memory calls = _transferUsdc(payee, 1e6);
        bytes32 structHash = vm.eip712HashStruct(
            "AgentExecution(address account,bytes32 mode,bytes executionCalldata,uint256 nonce,uint48 deadline,uint32 epoch)",
            abi.encode(AgentExecution(address(wallet), SINGLE, calls, 7, 1234, 1))
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
        assertEq(
            agents.hashExecution(address(wallet), SINGLE, calls, 7, 1234, 1),
            keccak256(abi.encodePacked("\x19\x01", domain, structHash))
        );
    }

    /*//////////////////////////////////////////////////////////////
                          WHO MAY TRADE, WHEN
    //////////////////////////////////////////////////////////////*/

    function test_trade_unknownAgentRefused() public {
        bytes memory calls = _transferUsdc(stranger, 1e6);
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, address(wallet), stranger)
        );
        vm.prank(stranger);
        agents.execute(address(wallet), stranger, SINGLE, calls, 0, uint48(block.timestamp), "");
    }

    function test_trade_expiredAgentRefused() public {
        bytes memory calls = _transferUsdc(payee, 1e6);
        _warp(START + 30 days + 1);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 0);
        vm.expectRevert(
            abi.encodeWithSelector(
                AgentModule.AgentModule_AgentExpired.selector, address(wallet), agent, uint48(START + 30 days)
            )
        );
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);
        (,,, bool active) = agents.getAgent(address(wallet), agent);
        assertFalse(active);
    }

    function test_trade_removedAgentRefused() public {
        vm.prank(owner);
        agents.removeAgent(address(wallet), agent);
        bytes memory calls = _transferUsdc(agent, 1e6);
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, address(wallet), agent));
        vm.prank(agent);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, uint48(block.timestamp), "");
    }

    function test_trade_uninstalledRefused() public {
        vm.prank(owner);
        wallet.uninstallModule(MODULE_TYPE_EXECUTOR, address(agents), "");
        bytes memory calls = _transferUsdc(agent, 1e6);
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_UnknownAgent.selector, address(wallet), agent));
        vm.prank(agent);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, uint48(block.timestamp), "");
    }

    function test_trade_pausedWalletRefused() public {
        vm.prank(owner);
        wallet.pause();
        bytes memory calls = _transferUsdc(agent, 1e6);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(agent);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, uint48(block.timestamp), "");
    }

    /// @notice Only a plain single call or a plain batch: no delegatecall, no "try" mode.
    function test_trade_otherModesRefused() public {
        bytes memory calls = _transferUsdc(agent, 1e6);
        bytes32[3] memory modes = [
            bytes32(uint256(0xff) << 248), // delegatecall
            bytes32(uint256(0x01) << 240), // single, try
            bytes32(uint256(0x0101) << 240) // batch, try
        ];
        for (uint256 i = 0; i < modes.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_UnsupportedMode.selector, modes[i]));
            vm.prank(agent);
            agents.execute(address(wallet), agent, modes[i], calls, i, uint48(block.timestamp), "");
        }
    }

    /// @notice The wallet's own guard still applies: an agent can't reach the limit module.
    function test_trade_walletGuardStillApplies() public {
        bytes memory calls = abi.encodePacked(
            address(module), uint256(0), abi.encodeCall(SpendingLimitModule.setDailyLimit, (type(int88).max))
        );
        vm.expectRevert(
            abi.encodeWithSelector(SessionHandler.SessionHandler_SessionRestrictedTarget.selector, address(module))
        );
        vm.prank(agent);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, uint48(block.timestamp), "");
    }

    /*//////////////////////////////////////////////////////////////
                   RULE 1: NATIVE ETH/BNB MUST BE METERED
    //////////////////////////////////////////////////////////////*/

    /// @notice Turning metering off afterwards stops the wallet's agents until it is back on.
    function test_rule1_tradesStopWhileNativeIsNotMetered() public {
        vm.prank(owner);
        wallet.setNativeMetering(false, address(0));
        bytes memory calls = abi.encodePacked(stranger, uint256(1 ether), bytes(""));
        vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_NativeNotMetered.selector, address(wallet)));
        vm.prank(agent);
        agents.execute(address(wallet), agent, SINGLE, calls, 0, uint48(block.timestamp), "");

        vm.prank(owner);
        wallet.setNativeMetering(true, address(0));
        _run(SINGLE, abi.encodePacked(payee, uint256(0.01 ether), bytes("")), 1);
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
        bytes memory calls = ERC7579Utils.encodeBatch(execs);
        vm.expectRevert(
            abi.encodeWithSelector(
                AgentModule.AgentModule_UncountedToken.selector, address(xyz), IERC20.transfer.selector
            )
        );
        vm.prank(agent);
        agents.execute(address(wallet), agent, BATCH, calls, 0, uint48(block.timestamp), "");
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
        _run(BATCH, ERC7579Utils.encodeBatch(execs), 0);
        assertEq(xyz.balanceOf(address(wallet)), xyzBefore + 50e18);
        assertEq(_walletSpent(), _usd(address(usdc), amount), "paid in full: the token bought isn't counted");

        _expectUncounted(address(xyz), abi.encodeCall(IERC20.approve, (address(exchange), 50e18)));
    }

    /*//////////////////////////////////////////////////////////////
                    RULE 3: NO CALL TO THE AGENT MODULE
    //////////////////////////////////////////////////////////////*/

    /// @notice An agent can't remove, raise or add agents through its own trade (it acts as the wallet,
    ///         which may otherwise remove).
    function test_rule3_agentCantReachTheModule() public {
        bytes[3] memory attempts = [
            abi.encodeCall(AgentModule.removeAgent, (address(wallet), agent)),
            abi.encodeCall(AgentModule.updateAgent, (address(wallet), agent, type(uint128).max, uint48(START + 1))),
            abi.encodeCall(AgentModule.onUninstall, (""))
        ];
        for (uint256 i = 0; i < attempts.length; i++) {
            bytes memory calls = abi.encodePacked(address(agents), uint256(0), attempts[i]);
            vm.expectRevert(abi.encodeWithSelector(AgentModule.AgentModule_TargetNotAllowed.selector, address(agents)));
            vm.prank(agent);
            agents.execute(address(wallet), agent, SINGLE, calls, i, uint48(block.timestamp), "");
        }
        assertEq(agents.getAgents(address(wallet)).length, 1);
    }

    /// @notice One trade can't start another from inside itself.
    function test_trade_noReentry() public {
        Reenterer reenterer = new Reenterer(agents);
        bytes memory innerCalls = _transferUsdc(stranger, 1e6);
        (uint48 deadline, bytes memory innerSig) = _signed(SINGLE, innerCalls, 1);
        reenterer.arm(
            abi.encodeCall(AgentModule.execute, (address(wallet), agent, SINGLE, innerCalls, 1, deadline, innerSig))
        );

        _run(SINGLE, abi.encodePacked(address(reenterer), uint256(0), abi.encodeCall(Reenterer.poke, ())), 0);

        assertEq(reenterer.caught(), ReentrancyGuardTransient.ReentrancyGuardReentrantCall.selector);
        assertEq(usdc.balanceOf(stranger), 0);
        assertFalse(agents.isNonceUsed(address(wallet), agent, 1));
    }

    /*//////////////////////////////////////////////////////////////
                           THE AGENT'S LIMIT
    //////////////////////////////////////////////////////////////*/

    function test_limit_agentLimitStopsATrade() public {
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, 50e18, uint48(block.timestamp + 1 days));
        uint256 value = _usd(address(usdc), 100e6);
        bytes memory calls = _transferUsdc(payee, 100e6);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 0);
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, value + _feeUsd(), 50e18)
        );
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);
        assertEq(usdc.balanceOf(payee), 0);
        assertEq(_walletSpent(), 0, "the wallet's limit kept a reverted trade");
        assertFalse(agents.isNonceUsed(address(wallet), agent, 0), "a reverted trade used its nonce");
    }

    /// @notice The wallet's own limit binds too, whichever is lower.
    function test_limit_walletLimitStillBinds() public {
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, type(uint128).max, uint48(block.timestamp + 1 days));
        uint256 amount = 6000e6;
        bytes memory calls = _transferUsdc(payee, amount);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 0);
        vm.expectRevert(
            abi.encodeWithSelector(
                SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector,
                int256(_usd(address(usdc), amount)),
                DAILY_LIMIT
            )
        );
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);
    }

    /// @notice Fees count: a leaked key sending empty trades runs out of limit instead of draining the
    ///         wallet's ETH in fees.
    function test_limit_feesCountSoEmptyTradesStop() public {
        uint256 fee = _feeUsd();
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, uint128(3 * fee + 1), uint48(block.timestamp + 1 days));
        bytes memory empty = abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(IERC20.balanceOf, (payee)));
        for (uint256 i = 0; i < 3; i++) {
            _run(SINGLE, empty, i);
        }
        assertEq(_agentSpent(), 3 * fee);
        assertEq(_walletSpent(), 0, "an empty trade moved the wallet's limit");

        (uint48 deadline, bytes memory sig) = _signed(SINGLE, empty, 3);
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, 4 * fee, 3 * fee + 1)
        );
        agents.execute(address(wallet), agent, SINGLE, empty, 3, deadline, sig);
    }

    /// @notice The agent's limit runs on the wallet's window: a new window gives it its whole limit
    ///         back, including when the window rolls inside the agent's own trade.
    function test_limit_startsOverWithTheWalletsWindow() public {
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, 150e18, uint48(block.timestamp + 30 days));
        _run(SINGLE, _transferUsdc(payee, 100e6), 0);

        bytes memory again = _transferUsdc(payee, 100e6);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, again, 1);
        vm.expectPartialRevert(AgentModule.AgentModule_AgentLimitExceeded.selector);
        agents.execute(address(wallet), agent, SINGLE, again, 1, deadline, sig);

        _warp(START + WINDOW);
        (, uint256 spent, uint256 remaining,) = agents.getAgent(address(wallet), agent);
        assertEq(spent, 0, "the view still counts the old window");
        assertEq(remaining, 150e18);

        // The window rolls in this trade's own preCheck.
        _run(SINGLE, again, 1);
        uint256 value = _usd(address(usdc), 100e6);
        assertEq(_agentSpent(), value + _feeUsd(), "the old window's spend was carried over");
        assertEq(_walletSpent(), value);
    }

    /// @notice A window the owner started (with a trade of their own) counts for the agent as well.
    function test_limit_windowStartedByAnotherTradeCounts() public {
        _run(SINGLE, _transferUsdc(payee, 100e6), 0);
        _warp(START + WINDOW + 1 hours);
        vm.prank(owner);
        wallet.execute(SINGLE, _transferUsdc(payee, 1e6));

        _run(SINGLE, _transferUsdc(payee, 10e6), 1);
        assertEq(_agentSpent(), _usd(address(usdc), 10e6) + _feeUsd());
    }

    /// @notice A limit lowered below what the agent spent stops it until the window starts over.
    function test_limit_loweredBelowSpent() public {
        _run(SINGLE, _transferUsdc(payee, 100e6), 0);
        vm.prank(owner);
        agents.updateAgent(address(wallet), agent, 10e18, uint48(block.timestamp + 1 days));
        (,, uint256 remaining,) = agents.getAgent(address(wallet), agent);
        assertEq(remaining, 0);

        bytes memory calls = _transferUsdc(payee, 1e6);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 1);
        vm.expectPartialRevert(AgentModule.AgentModule_AgentLimitExceeded.selector);
        agents.execute(address(wallet), agent, SINGLE, calls, 1, deadline, sig);
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
        bytes memory calls = _transferUsdc(payee, amount);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 0);

        if (value > uint256(DAILY_LIMIT)) {
            vm.expectRevert(
                abi.encodeWithSelector(
                    SpendingLimitModule.SpendingLimitModule_BudgetExceeded.selector, int256(value), DAILY_LIMIT
                )
            );
        } else if (value + fee > limit) {
            vm.expectRevert(
                abi.encodeWithSelector(AgentModule.AgentModule_AgentLimitExceeded.selector, value + fee, limit)
            );
        }
        agents.execute(address(wallet), agent, SINGLE, calls, 0, deadline, sig);

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

    /// @notice Gas of one agent trade (a USDC transfer, signed and relayed) as a whole transaction,
    ///         beside the same transfer as a recurring payment (RecurringPaymentTest: ~103k later
    ///         payment). Mocks are cheaper than real tokens and feeds. Read with -vv.
    function test_gas_signedTransfer() public {
        _run(SINGLE, _transferUsdc(payee, 1e6), 0); // payee's balance slot and nonce word exist now
        bytes memory calls = _transferUsdc(payee, 1e6);
        (uint48 deadline, bytes memory sig) = _signed(SINGLE, calls, 1);
        bytes memory data =
            abi.encodeCall(AgentModule.execute, (address(wallet), agent, SINGLE, calls, 1, deadline, sig));
        _cool();
        vm.prank(relayer);
        uint256 before = gasleft();
        agents.execute(address(wallet), agent, SINGLE, calls, 1, deadline, sig);
        uint256 execution = before - gasleft();
        console2.log("agent trade, ERC-20 transfer, signed and relayed (gas):", execution + 21_000 + _calldataGas(data));
    }

    /*//////////////////////////////////////////////////////////////
                               HELPERS
    //////////////////////////////////////////////////////////////*/

    function _transferUsdc(address to, uint256 amount) internal view returns (bytes memory) {
        return abi.encodePacked(address(usdc), uint256(0), abi.encodeCall(IERC20.transfer, (to, amount)));
    }

    function _sign(
        uint256 key,
        address account,
        bytes32 mode,
        bytes memory calls,
        uint256 nonce,
        uint48 deadline,
        uint32 epoch
    ) internal view returns (bytes memory) {
        bytes32 digest = agents.hashExecution(account, mode, calls, nonce, deadline, epoch);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return abi.encodePacked(r, s, v);
    }

    /// @dev The agent's signature over `calls` for this wallet at its current epoch, an hour to run.
    function _signed(bytes32 mode, bytes memory calls, uint256 nonce)
        internal
        view
        returns (uint48 deadline, bytes memory sig)
    {
        (AgentModule.Agent memory info,,,) = agents.getAgent(address(wallet), agent);
        deadline = uint48(block.timestamp + 1 hours);
        sig = _sign(agentKey, address(wallet), mode, calls, nonce, deadline, info.epoch);
    }

    /// @dev Signs and sends as the relayer.
    function _run(bytes32 mode, bytes memory calls, uint256 nonce) internal {
        (uint48 deadline, bytes memory sig) = _signed(mode, calls, nonce);
        vm.prank(relayer);
        agents.execute(address(wallet), agent, mode, calls, nonce, deadline, sig);
    }

    /// @dev Expects the agent's single call `callData` to `token` to be refused by rule 2. Its nonce is
    ///      derived from the call, so it is one the test hasn't used.
    function _expectUncounted(address token, bytes memory callData) internal {
        bytes memory calls = abi.encodePacked(token, uint256(0), callData);
        uint256 nonce = uint256(keccak256(calls));
        vm.expectRevert(
            abi.encodeWithSelector(AgentModule.AgentModule_UncountedToken.selector, token, bytes4(callData))
        );
        vm.prank(agent);
        agents.execute(address(wallet), agent, SINGLE, calls, nonce, uint48(block.timestamp), "");
    }

    function _feeUsd() internal view returns (uint256) {
        (, uint256 fee, uint256 keeperFee) = registry.feeConfig();
        return uint256(oracle.getPrice(address(0), fee + keeperFee));
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
        (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOp(
            address(wallet), config, dest, 0, data, PasskeyHelper.nonceKey(PasskeyHelper.LATEST_OP_DEADLINE, 0)
        );
        op.signature = sessionKey.sign(hash);
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

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
        vm.cool(config.ethUsdPriceFeed);
        vm.cool(config.usdcUsdPriceFeed);
        vm.cool(config.daiUsdPriceFeed);
    }

    function _calldataGas(bytes memory data) internal pure returns (uint256 gas) {
        for (uint256 i = 0; i < data.length; i++) {
            gas += data[i] == 0 ? 4 : 16;
        }
    }
}
