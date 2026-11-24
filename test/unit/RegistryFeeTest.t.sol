//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {MODULE_TYPE_EXECUTOR, MODULE_TYPE_VALIDATOR} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {RecurringPaymentModule} from "../../src/RecurringPaymentModule.sol";
import {AgentModule} from "../../src/AgentModule.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";

/**
 * @title RegistryFeeTest
 * @notice SHRegistry's two fees (protocol and keeper), stored in gwei beside the treasury, and the
 *         RecurringPaymentModule setter. Kept out of SHAdminTest, which sits near solc's legacy size
 *         limit ("Tag too large for reserved space").
 */
contract RegistryFeeTest is Test {
    SHFactory factory;
    SHTreasury treasury;
    SHOracle oracle;
    SHRegistry registry;
    HelperConfig.NetworkConfig config;
    address operator;
    address rando = makeAddr("rando");

    /// @dev The slot holding `treasury`, `_protocolFeeGwei` and `_keeperFeeGwei`
    ///      (`forge inspect SHRegistry storage`).
    uint256 constant FEE_SLOT = 7;

    function setUp() public {
        (factory, treasury, config, oracle) = new DeploySHProtocol().run();
        registry = factory.REGISTRY();
        operator = treasury.owner();
    }

    /*//////////////////////////////////////////////////////////////
                         DEPLOYED STATE + PACKING
    //////////////////////////////////////////////////////////////*/

    /// @notice The deploy script leaves both fees and all four bounds as whole gwei, in order.
    function test_deploy_feesAreWholeGweiAndInRange() public view {
        uint256[6] memory values = [
            registry.MIN_PROTOCOL_FEE(),
            registry.protocolFee(),
            registry.MAX_PROTOCOL_FEE(),
            registry.MIN_KEEPER_FEE(),
            registry.keeperFee(),
            registry.MAX_KEEPER_FEE()
        ];
        for (uint256 i = 0; i < values.length; i++) {
            assertEq(values[i] % 1 gwei, 0, "not whole gwei");
        }
        assertLe(values[0], values[1]);
        assertLe(values[1], values[2]);
        assertLe(values[3], values[4]);
        assertLe(values[4], values[5]);
        assertGt(registry.keeperFee(), 0, "keeper fee deployed as zero");
        assertEq(registry.getFee(), registry.protocolFee());
    }

    /// @notice The treasury and both fees share ONE slot, which is what makes the wallet's fee path a
    ///         single storage read.
    function test_feeSlot_packsTreasuryAndBothFees() public view {
        uint256 raw = uint256(vm.load(address(registry), bytes32(FEE_SLOT)));
        assertEq(address(uint160(raw)), registry.treasury(), "treasury not in the fee slot");
        assertEq(uint256(uint48(raw >> 160)) * 1 gwei, registry.protocolFee(), "protocol fee not packed");
        assertEq(uint256(uint48(raw >> 208)) * 1 gwei, registry.keeperFee(), "keeper fee not packed");
    }

    function test_feeConfig_returnsTreasuryAndBothFeesInWei() public view {
        (address recipient, uint256 fee, uint256 keeper) = registry.feeConfig();
        assertEq(recipient, registry.treasury());
        assertEq(fee, registry.protocolFee());
        assertEq(keeper, registry.keeperFee());
    }

    /*//////////////////////////////////////////////////////////////
                               SETTERS
    //////////////////////////////////////////////////////////////*/

    function test_setKeeperFee_throughTreasury() public {
        uint256 oldFee = registry.keeperFee();
        uint256 newFee = registry.MAX_KEEPER_FEE();

        vm.expectEmit(address(registry));
        emit SHRegistry.KeeperFeeUpdated(oldFee, newFee);
        vm.prank(operator);
        treasury.setKeeperFee(newFee);

        assertEq(registry.keeperFee(), newFee);
        (,, uint256 keeper) = registry.feeConfig();
        assertEq(keeper, newFee);
        assertEq(registry.protocolFee(), registry.getFee(), "protocol fee disturbed");
    }

    function test_setKeeperFee_boundsInclusiveAndEnforced() public {
        uint256 minFee = registry.MIN_KEEPER_FEE();
        uint256 maxFee = registry.MAX_KEEPER_FEE();
        vm.startPrank(operator);
        treasury.setKeeperFee(minFee);
        treasury.setKeeperFee(maxFee);
        vm.expectRevert(SHRegistry.SHRegistry_FeeNotInRange.selector);
        treasury.setKeeperFee(maxFee + 1 gwei);
        if (minFee >= 1 gwei) {
            vm.expectRevert(SHRegistry.SHRegistry_FeeNotInRange.selector);
            treasury.setKeeperFee(minFee - 1 gwei);
        }
        vm.stopPrank();
    }

    /// @notice Inside the bounds but not a whole gwei: refused, never silently rounded.
    function test_setFees_rejectNonWholeGwei() public {
        uint256 keeperFee = registry.MIN_KEEPER_FEE() + 1;
        uint256 protocolFee = registry.MIN_PROTOCOL_FEE() + 1;
        vm.startPrank(operator);
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_FeeNotWholeGwei.selector, keeperFee));
        treasury.setKeeperFee(keeperFee);
        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_FeeNotWholeGwei.selector, protocolFee));
        treasury.setProtocolFee(protocolFee);
        vm.stopPrank();
    }

    function test_setKeeperFee_onlyOwner() public {
        uint256 fee = registry.keeperFee();
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando));
        vm.prank(rando);
        treasury.setKeeperFee(fee);

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando));
        vm.prank(rando);
        registry.setKeeperFee(fee);
    }

    /// @notice A new module reaches wallets deployed afterwards only; existing wallets keep theirs.
    function test_setRecurringPaymentModule_onlyAffectsNewWallets() public {
        address oldModule = registry.recurringPaymentModule();
        address[] memory none = new address[](0);
        vm.prank(rando);
        SessionHandler before =
            SessionHandler(payable(factory.deployWallet(1e18, 1 days, none, true, bytes32(0), bytes32(0), 0, none)));

        RecurringPaymentModule replacement = new RecurringPaymentModule();
        vm.expectEmit(address(registry));
        emit SHRegistry.RecurringPaymentModuleUpdated(oldModule, address(replacement));
        vm.prank(operator);
        treasury.setRecurringPaymentModule(address(replacement));

        vm.prank(rando);
        SessionHandler afterChange =
            SessionHandler(payable(factory.deployWallet(1e18, 1 days, none, true, bytes32(0), bytes32(0), 0, none)));

        assertTrue(before.isModuleInstalled(MODULE_TYPE_EXECUTOR, oldModule, ""));
        assertFalse(before.isModuleInstalled(MODULE_TYPE_EXECUTOR, address(replacement), ""));
        assertTrue(afterChange.isModuleInstalled(MODULE_TYPE_EXECUTOR, address(replacement), ""));
        assertFalse(afterChange.isModuleInstalled(MODULE_TYPE_EXECUTOR, oldModule, ""));
    }

    function test_setRecurringPaymentModule_rejectsZeroAndStrangers() public {
        vm.expectRevert(SHRegistry.SHRegistry_InvalidRecurringPaymentModule.selector);
        vm.prank(operator);
        treasury.setRecurringPaymentModule(address(0));

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando));
        vm.prank(rando);
        treasury.setRecurringPaymentModule(makeAddr("m"));
    }

    /// @notice The same for the agent validator: a new module reaches wallets deployed afterwards
    ///         only; existing wallets keep theirs, and their agents with it.
    function test_setAgentModule_onlyAffectsNewWallets() public {
        address oldModule = registry.agentModule();
        address[] memory none = new address[](0);
        vm.prank(rando);
        SessionHandler before =
            SessionHandler(payable(factory.deployWallet(1e18, 1 days, none, true, bytes32(0), bytes32(0), 0, none)));

        AgentModule replacement = new AgentModule();
        vm.expectEmit(address(registry));
        emit SHRegistry.AgentModuleUpdated(oldModule, address(replacement));
        vm.prank(operator);
        treasury.setAgentModule(address(replacement));

        vm.prank(rando);
        SessionHandler afterChange =
            SessionHandler(payable(factory.deployWallet(1e18, 1 days, none, true, bytes32(0), bytes32(0), 0, none)));

        assertTrue(before.isModuleInstalled(MODULE_TYPE_VALIDATOR, oldModule, ""));
        assertFalse(before.isModuleInstalled(MODULE_TYPE_VALIDATOR, address(replacement), ""));
        assertTrue(afterChange.isModuleInstalled(MODULE_TYPE_VALIDATOR, address(replacement), ""));
        assertFalse(afterChange.isModuleInstalled(MODULE_TYPE_VALIDATOR, oldModule, ""));
    }

    function test_setAgentModule_rejectsZeroAndStrangers() public {
        vm.expectRevert(SHRegistry.SHRegistry_InvalidAgentModule.selector);
        vm.prank(operator);
        treasury.setAgentModule(address(0));

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando));
        vm.prank(rando);
        treasury.setAgentModule(makeAddr("m"));

        // Only the treasury (the registry's owner) may set it on the registry itself.
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando));
        vm.prank(rando);
        registry.setAgentModule(makeAddr("m"));
    }

    function test_treasuryPassthroughs_refuseBeforeRegistryIsSet() public {
        SHTreasury fresh = new SHTreasury();
        vm.expectRevert(SHTreasury.SHTreasury_RegistryNotSet.selector);
        fresh.setKeeperFee(1 gwei);
        vm.expectRevert(SHTreasury.SHTreasury_RegistryNotSet.selector);
        fresh.setRecurringPaymentModule(makeAddr("m"));
        vm.expectRevert(SHTreasury.SHTreasury_RegistryNotSet.selector);
        fresh.setAgentModule(makeAddr("m"));
    }

    /*//////////////////////////////////////////////////////////////
                             CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/

    /// @notice Unlike the protocol fee, the keeper fee may have a zero floor and start at zero.
    function test_constructor_keeperFeeMayBeZero() public {
        SHRegistry r = _registry(_protocolBounds(), SHRegistry.FeeBounds(0, 0, 1e15));
        assertEq(r.keeperFee(), 0);
        assertEq(r.MIN_KEEPER_FEE(), 0);
    }

    function test_constructor_rejectsUnusableKeeperBounds() public {
        SHRegistry.FeeBounds memory p = _protocolBounds();
        uint256 storable = registry.MAX_STORABLE_FEE();

        vm.expectRevert(SHRegistry.SHRegistry_InvalidFeeBounds.selector);
        _registry(p, SHRegistry.FeeBounds(2e15, 2e15, 1e15)); // floor above ceiling

        vm.expectRevert(SHRegistry.SHRegistry_InvalidFeeBounds.selector);
        _registry(p, SHRegistry.FeeBounds(1e15, 1e15, storable + 1 gwei)); // past the uint48-gwei slot

        vm.expectRevert(SHRegistry.SHRegistry_InvalidFeeBounds.selector);
        _registry(p, SHRegistry.FeeBounds(1e15, 1, 1e15)); // floor not whole gwei

        vm.expectRevert(SHRegistry.SHRegistry_InvalidFeeBounds.selector);
        _registry(p, SHRegistry.FeeBounds(1e15, 0, 1e15 + 1)); // ceiling not whole gwei

        vm.expectRevert(SHRegistry.SHRegistry_FeeNotInRange.selector);
        _registry(p, SHRegistry.FeeBounds(1e15 + 1 gwei, 0, 1e15)); // start above ceiling

        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_FeeNotWholeGwei.selector, 5e14 + 1));
        _registry(p, SHRegistry.FeeBounds(5e14 + 1, 0, 1e15)); // start not whole gwei
    }

    /// @notice The protocol fee keeps its non-zero floor, and its bounds follow the same gwei rules.
    function test_constructor_protocolBoundsFollowGweiRules() public {
        SHRegistry.FeeBounds memory k = SHRegistry.FeeBounds(0, 0, 1e15);

        vm.expectRevert(SHRegistry.SHRegistry_InvalidFeeBounds.selector);
        _registry(SHRegistry.FeeBounds(1e14, 5e12 + 1, 1e14), k);

        vm.expectRevert(SHRegistry.SHRegistry_InvalidFeeBounds.selector);
        _registry(SHRegistry.FeeBounds(5e12, 5e12, 1e14 - 1), k);

        vm.expectRevert(abi.encodeWithSelector(SHRegistry.SHRegistry_FeeNotWholeGwei.selector, 5e12 + 1));
        _registry(SHRegistry.FeeBounds(5e12 + 1, 5e12, 1e14), k);

        // The largest storable ceiling is accepted, and a fee at it round-trips exactly.
        uint256 storable = registry.MAX_STORABLE_FEE();
        SHRegistry big = _registry(SHRegistry.FeeBounds(storable, 1 gwei, storable), k);
        assertEq(big.protocolFee(), storable);
    }

    /*//////////////////////////////////////////////////////////////
                               HELPERS
    //////////////////////////////////////////////////////////////*/

    function _protocolBounds() internal view returns (SHRegistry.FeeBounds memory) {
        return SHRegistry.FeeBounds(registry.protocolFee(), registry.MIN_PROTOCOL_FEE(), registry.MAX_PROTOCOL_FEE());
    }

    function _registry(SHRegistry.FeeBounds memory p, SHRegistry.FeeBounds memory k) internal returns (SHRegistry) {
        return new SHRegistry(
            address(this),
            p,
            k,
            address(treasury),
            address(oracle),
            config.reputationRegistry,
            config.identityRegistry,
            config.entryPoint,
            0
        );
    }
}
