//SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {Test, console} from "forge-std/Test.sol";
//Account abstraction Imports
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {DeploySessionHandler} from "../../script/DeploySessionHandler.s.sol";
import {PriceOracle} from "../../src/PriceOracle.sol";
import {IERC20} from "@openzeppelin/contracts/interfaces/IERC20.sol";

contract SessionHandlerSepoliaTest is Test {
    PriceOracle oracle;
    HelperConfig.NetworkConfig config;
    SessionHandler sessionHandler;
    SendPackedUserOp sendPackedUserOp;
    address user;
    uint256 privateKey;
    address kani = makeAddr("kani");
    address bundler = makeAddr("bundler");
    uint256 constant BUDGET = 5000e18;

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

    /**
     * @dev Adds a standard ERC20 transfer session for `user` on the `usdc` mock token.
     *      Grants access to ERC20Mock.transfer only, within a 1-day window, with BUDGET spending limit.
     *      Used by tests that need a valid active session without ETH value transfers.
     */
    modifier linkSessionAdded() {
        address sessionKey = user;
        address target = config.link;
        bytes4[] memory sel = new bytes4[](3);
        sel[0] = IERC20.transfer.selector;
        sel[1] = IERC20.transferFrom.selector;
        sel[2] = IERC20.approve.selector;

        uint48 validFrom = uint48(block.timestamp );
        uint48 validUntil = uint48(block.timestamp + 1 days);
        uint256 spendingLimit = BUDGET;

        vm.prank(sessionHandler.owner());
        sessionHandler.addSessionKey(sessionKey, target, sel, validFrom, validUntil, spendingLimit);

        _;
    }

    function setUp() external {
        DeploySessionHandler deployer = new DeploySessionHandler();
        (sessionHandler, config, oracle) = deployer.run();
        (user, privateKey) = makeAddrAndKey("user");
        sendPackedUserOp = new SendPackedUserOp();
         vm.deal(address(sessionHandler), 10 ether);
        deal(config.link,address(sessionHandler), 10000e18);
        
    }



    /**
     * @notice Sending native ETH via a session key deducts the correct USD value from the session budget
     * @dev Constructs a UserOp with empty calldata and value == 1 ether targeting an EOA.
     *      The ETH-send session (target == address(0)) must validate successfully and charge the
     *      oracle-priced USD equivalent against the spending limit. Verifies both the remaining
     *      budget and the recipient's post-transfer balance.
     */
    function testSendingEthWithSession() public ethSessionAdded {
       
       address dest= kani;
        uint256 value = 1 ether;
        uint256 valueInUSD=oracle.getUSDValue(address(0), value);


        bytes memory data = ""; // No data needed for native ETH transfer
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);
        PackedUserOperation[] memory PackedUserOp = new PackedUserOperation[](1);
        (PackedUserOperation memory userOp, ,) =sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        
        PackedUserOp[0] = userOp;
        
        
        vm.warp(block.timestamp + 10 minutes); // Ensure we're within the session's validity period
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(PackedUserOp, payable(user)); 
        
        uint256 remainingBudget = sessionHandler.getRemainingBudget(user);
        uint256 expectedRemainingBudget = BUDGET -valueInUSD;
        assertEq(remainingBudget, expectedRemainingBudget);
        assertEq(kani.balance, value);
      

    }

    /**
     * @notice EntryPoint must be able to execute a session-key-signed UserOp end-to-end
     * @dev Warps 5 seconds forward to ensure block.timestamp > validFrom.
     *      Submits a handleOps call as a bundler with a session-key-signed transfer operation.
     *      Verifies the ERC20 balance of user increases by amountToTransfer.
     */
    function testTransferERC20WithSession() public linkSessionAdded {
        address dest = config.link;
        uint256 amountToTransfer = 20e18;
        uint256 value = 0;

        PackedUserOperation[] memory PackedUserOp = new PackedUserOperation[](1);
        bytes memory data = abi.encodeWithSelector(IERC20.transfer.selector, user, amountToTransfer);
        bytes memory callData = abi.encodeWithSelector(SessionHandler.execute.selector, dest, value, data);

        (PackedUserOperation memory userOp,,) =
            sendPackedUserOp.generateSignedUserOp(address(sessionHandler), config, callData, user, privateKey);
        PackedUserOp[0] = userOp;

        vm.warp(block.timestamp + 10 minutes);
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(PackedUserOp, payable(user));
    

        assertEq(IERC20(config.link).balanceOf(user), amountToTransfer);
    }



    
}