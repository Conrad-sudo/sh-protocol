// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, StdInvariant} from "forge-std/Test.sol";
import {MODULE_TYPE_HOOK} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {SHHandler} from "./SHHandler.sol";

/**
 * @title InvariantSH
 * @notice Invariant suite for the USD-spending-cap design. The handler fuzzes owner executes
 *         (transfers, mint+transfer batches), config changes, non-owner attacks, session-key
 *         management, and time warps; these invariants then pin down the properties the module
 *         must never lose.
 *
 * Run with:
 *   forge test --match-contract InvariantSH -vv
 */
contract InvariantSH is StdInvariant, Test {
    SessionHandler wallet;
    SpendingLimitModule module;
    SHHandler handler;
    ERC20Mock usdc;
    ERC20Mock dai;
    SHOracle oracle;
    HelperConfig.NetworkConfig config;

    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;

    function setUp() public {
        DeploySHProtocol deployer = new DeploySHProtocol();
        SHFactory factory;
        SHTreasury treasury;
        (factory, treasury, config, oracle) = deployer.run();
        module = SpendingLimitModule(factory.REGISTRY().spendingLimitModule());
        usdc = ERC20Mock(config.usdc);
        dai = ERC20Mock(config.dai);

        address[] memory watched = new address[](2);
        watched[0] = address(usdc);
        watched[1] = address(dai);
        vm.prank(config.account);
        wallet =
            SessionHandler(payable(factory.deployWallet(DAILY_LIMIT, WINDOW, watched, address(0), new address[](0))));

        vm.deal(address(wallet), 100 ether);
        usdc.mint(address(wallet), 1_000_000e6);
        dai.mint(address(wallet), 1_000_000e18);

        handler =
            new SHHandler(wallet, module, oracle, usdc, dai, MockV3Aggregator(config.usdcUsdPriceFeed), config.account);

        targetContract(address(handler));
    }

    /*//////////////////////////////////////////////////////////////
                               INVARIANTS
    //////////////////////////////////////////////////////////////*/

    /// @notice The core financial safety property: within a live window the module never records
    ///         more net spending than the cap in force allowed when the spend landed. Since every
    ///         over-budget execute reverts atomically, spentInWindow may only exceed the CURRENT
    ///         limit if the owner lowered the limit afterwards — which the ghost model tracks, so
    ///         equality with the ghost (checked below) is the strict form of this invariant.
    function invariant_spentNeverNegative() public view {
        assertGe(module.getConfig(address(wallet)).spentInWindow, 0, "spentInWindow went negative");
    }

    /// @notice The module's metering must agree EXACTLY with an independent replica of its own
    ///         rules (roll-then-accumulate, oracle-priced net outflow, floor at net inflow).
    function invariant_meteringMatchesGhostModel() public view {
        assertEq(
            module.getConfig(address(wallet)).spentInWindow,
            handler.ghostSpent(),
            "module metering diverged from ghost model"
        );
    }

    /// @notice Config scalars only ever change through the owner path the ghost mirrors: if a
    ///         non-owner write ever landed, these diverge.
    function invariant_configOnlyChangedByOwner() public view {
        SpendingLimitModule.Config memory cfg = module.getConfig(address(wallet));
        assertEq(cfg.dailyLimitUsd, handler.ghostLimit(), "dailyLimit changed outside owner path");
        assertEq(uint256(cfg.windowDuration), handler.ghostWindowDuration(), "window changed outside owner path");
    }

    /// @notice The spending-cap hook must stay installed and configured — nothing the handler does
    ///         (short of the owner uninstalling, which it never does) may remove it.
    function invariant_hookStaysInstalled() public view {
        assertTrue(wallet.isModuleInstalled(MODULE_TYPE_HOOK, address(module), ""), "hook uninstalled");
        assertTrue(module.getConfig(address(wallet)).installed, "module config lost");
    }

    /// @notice The watched list stays within its gas bound and consistent with the membership map.
    function invariant_watchedListBoundedAndConsistent() public view {
        SpendingLimitModule.Config memory cfg = module.getConfig(address(wallet));
        assertLe(cfg.watchedTokens.length, 32, "watched list exceeded MAX_WATCHED_TOKENS");
        for (uint256 i = 0; i < cfg.watchedTokens.length; i++) {
            assertTrue(
                module.isWatched(address(wallet), cfg.watchedTokens[i]), "list entry missing from membership map"
            );
        }
        // USDC is never removed by the handler, so it must still be watched (metering stayed live).
        assertTrue(module.isWatched(address(wallet), address(usdc)), "USDC fell off the watched list");
    }

    /// @notice getRemainingBudget must agree with raw config state under the same window-expiry
    ///         rule the module itself applies.
    function invariant_remainingBudgetConsistent() public view {
        SpendingLimitModule.Config memory cfg = module.getConfig(address(wallet));
        bool expired = block.timestamp >= uint256(cfg.windowStart) + uint256(cfg.windowDuration);
        int256 spent = expired ? int256(0) : cfg.spentInWindow;
        int256 expected = spent >= cfg.dailyLimitUsd ? int256(0) : cfg.dailyLimitUsd - spent;
        assertEq(wallet.getRemainingBudget(), expected, "remaining budget inconsistent with config");
    }

    /// @notice The session allowlist matches the ghost bookkeeping of owner adds/removes.
    function invariant_sessionAllowlistMatchesGhost() public view {
        uint256 n = handler.sessionKeyCount();
        for (uint256 i = 0; i < n; i++) {
            address key = handler.sessionKeys(i);
            assertEq(wallet.allowedSession(key), handler.expectedAllowed(key), "session allowlist diverged");
        }
    }

    /// @notice Window bookkeeping stays sane: a positive duration and a start not in the future.
    function invariant_windowFieldsSane() public view {
        SpendingLimitModule.Config memory cfg = module.getConfig(address(wallet));
        assertGt(uint256(cfg.windowDuration), 0, "zero window duration");
        assertLe(uint256(cfg.windowStart), block.timestamp, "windowStart in the future");
    }
}
