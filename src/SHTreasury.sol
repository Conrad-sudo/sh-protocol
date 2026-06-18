/* SPDX-License-Identifier: BUSL-1.1
 * Licensor: Conrad Japhet
 * Licensed Work: SHTreasury.sol
 * Change Date: 2029-06-12
 * Change License: MIT
*/
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {SHRegistry} from "./SHRegistry.sol";

/**
 * @title SHTreasury
 * @author Conrad Japhet
 * @notice Receives protocol fees from SessionHandler wallets and administers the SHRegistry.
 * @dev Deploys its own SHRegistry in the constructor, so `address(this)` is the canonical
 *      treasury address from day one — no post-deployment ownership transfers required.
 *
 *      Fee flow:
 *        SessionHandler.execute() → payable(FEE_REGISTRY.treasury()).call{value: fee}()
 *                                 → SHTreasury.receive()
 *
 *      Admin flow:
 *        Protocol operator → SHTreasury.set*() → SHRegistry.set*()
 */
contract SHTreasury is Ownable, ReentrancyGuard {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when a withdrawal ETH transfer fails.
    error SHTreasury_WithdrawalFailed();
    /// @dev Thrown when the requested withdrawal amount exceeds the contract balance.
    error SHTreasury_InsufficientBalance();
    /// @dev Thrown when a zero withdrawal amount is requested.
    error SHTreasury_InvalidAmount();
    /// @dev Thrown when address(0) is passed as the withdrawal recipient.
    error SHTreasury_InvalidRecipient();

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice The SHRegistry deployed and owned by this treasury.
    address public immutable REGISTRY;

    /// @notice Cumulative ETH received as protocol fees since deployment.
    uint256 public totalFeesCollected;

    /*//////////////////////////////////////////////////////////////
                                  EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when accumulated fees are withdrawn to a recipient.
    /// @param recipient The address that received the ETH.
    /// @param amount    The amount withdrawn in wei.
    event FeesWithdrawn(address indexed recipient, uint256 amount);

    /*//////////////////////////////////////////////////////////////
                               CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Deploys the treasury and its SHRegistry in one transaction.
     * @dev The SHRegistry is deployed with `address(this)` as the treasury, so fees
     *      flow here from the moment the first SessionHandler goes live. The SHRegistry's
     *      Ownable owner is also set to `address(this)`, so all registry admin passes
     *      through this contract.
     * @param initialFee     Starting protocol fee in wei. Must not exceed SHRegistry.MAX_PROTOCOL_FEE.
     * @param priceOracle    Address of the deployed SHOracle. Must not be address(0).
     * @param initialAgentId Id of the SessionHandler ERC-4337 AI agent on the ERC-8004 Identity Registry.
     * @param uniswapRouter  Uniswap V2 Router address. May be address(0) on chains without Uniswap V2.
     */
    constructor(
        uint256 initialFee,
        address priceOracle,
        uint256 initialAgentId,
        address uniswapRouter
    ) Ownable(msg.sender) {
        SHRegistry registry = new SHRegistry(
            initialFee, address(this), priceOracle, initialAgentId, uniswapRouter
        );
        REGISTRY = address(registry);
    }

    /*//////////////////////////////////////////////////////////////
                            RECEIVE FUNCTION
    //////////////////////////////////////////////////////////////*/

    /// @notice Accepts ETH fee payments from SessionHandler wallets.
    receive() external payable {
        totalFeesCollected += msg.value;
    }

    /*//////////////////////////////////////////////////////////////
                           EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Withdraws ETH fees to a specified recipient. Only callable by the owner.
     * @param recipient Address to send ETH to. Must not be address(0).
     * @param amount    Amount in wei to withdraw. Must not exceed contract balance.
     */
    function withdraw(address recipient, uint256 amount) external onlyOwner nonReentrant {
        if (recipient == address(0)) revert SHTreasury_InvalidRecipient();
        if (amount == 0) revert SHTreasury_InvalidAmount();
        if (address(this).balance < amount) revert SHTreasury_InsufficientBalance();
        (bool success,) = payable(recipient).call{value: amount}("");
        if (!success) revert SHTreasury_WithdrawalFailed();
        emit FeesWithdrawn(recipient, amount);
    }

    /**
     * @notice Withdraws the entire ETH balance to a recipient. Only callable by the owner.
     * @param recipient Address to send ETH to. Must not be address(0).
     */
    function withdrawAll(address recipient) external onlyOwner nonReentrant {
        if (recipient == address(0)) revert SHTreasury_InvalidRecipient();
        uint256 balance = address(this).balance;
        if (balance == 0) revert SHTreasury_InsufficientBalance();
        (bool success,) = payable(recipient).call{value: balance}("");
        if (!success) revert SHTreasury_WithdrawalFailed();
        emit FeesWithdrawn(recipient, balance);
    }

    /*//////////////////////////////////////////////////////////////
                         REGISTRY ADMIN
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Updates the protocol fee charged on every session-key execution. Only callable by the owner.
     * @param newFee The new fee in wei. Must not exceed SHRegistry.MAX_PROTOCOL_FEE.
     */
    function setProtocolFee(uint256 newFee) external onlyOwner {
        SHRegistry(REGISTRY).setProtocolFee(newFee);
    }

    /**
     * @notice Updates the canonical SHOracle used by all SessionHandler wallets. Only callable by the owner.
     * @param newOracle The new SHOracle address. Must not be address(0).
     */
    function setPriceOracle(address newOracle) external onlyOwner {
        SHRegistry(REGISTRY).setPriceOracle(newOracle);
    }

    /**
     * @notice Redirects future fee payments to a new treasury address. Only callable by the owner.
     * @dev Use this when migrating to a new treasury contract. After calling this, fees will no
     *      longer flow to this contract — ensure the new treasury is ready before calling.
     * @param newTreasury The new treasury address. Must not be address(0).
     */
    function setTreasury(address newTreasury) external onlyOwner {
        SHRegistry(REGISTRY).setTreasury(newTreasury);
    }


    /**
     * @notice Updates the registered agentId for the SessionHandler Protocol. Only callable by the owner.
     * @dev Reverts if newId is 0 or equal to the current agentId.
     * @param newId The new agentId. Must not be 0 and must differ from the current agentId.
     */
    function setAgentId(uint256 newId) external onlyOwner {
        SHRegistry(REGISTRY).setAgentId(newId);
    }

    /**
     * @notice Updates the Uniswap V2 Router address in the registry. Only callable by the owner.
     * @param newRouter The new Uniswap V2 Router address.
     */
    function setUniswapRouter(address newRouter) external onlyOwner {
        SHRegistry(REGISTRY).setUniswapRouter(newRouter);
    }

    /**
     * @notice Updates the SHValueInterpreter used by all SessionHandler wallets. Only callable by the owner.
     * @param newInterpreter The new SHValueInterpreter address. Must not be address(0).
     */
    function setCallValueInterpreter(address newInterpreter) external onlyOwner {
        SHRegistry(REGISTRY).setCallValueInterpreter(newInterpreter);
    }
}
