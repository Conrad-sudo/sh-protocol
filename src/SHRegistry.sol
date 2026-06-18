/* SPDX-License-Identifier: BUSL-1.1
 * Licensor: Conrad Japhet
 * Licensed Work: SHRegistry.sol
 * Change Date: 2029-06-12
 * Change License: MIT
*/
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title SHRegistry
 * @author Conrad Japhet
 * @notice Central configuration registry for the SessionHandler Protocol. Stores the
 *         protocol fee, treasury address, price oracle, agent identity, Uniswap router,
 *         and call-value interpreter used across all deployed SessionHandler wallets.
 * @dev SessionHandler wallets read all protocol parameters from this contract at
 *      execution time rather than storing them as immutables, so any update here
 *      propagates instantly to every deployed wallet without redeployment.
 *
 *      Owned by the treasury operator. protocolFee is capped at MAX_PROTOCOL_FEE
 *      to bound the worst-case impact of a compromised owner key.
 */
contract SHRegistry is Ownable {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @dev Thrown when a proposed protocolFee exceeds MAX_PROTOCOL_FEE.
    error SHRegistry_FeeTooHigh();
    /// @dev Thrown when address(0) is passed as the treasury address.
    error SHRegistry_InvalidTreasury();
    /// @dev Thrown when address(0) is passed as the price oracle address.
    error SHRegistry_InvalidPriceOracle();
    /// @dev thrown when agentId is 0
    error SHRegistry_InvalidAgentId();
    /// @dev Thrown when address(0) is passed as the call value interpreter.
    error SHRegistry_InvalidCallValueInterpreter();

    /*//////////////////////////////////////////////////////////////
                             STATE VARIABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Maximum protocol fee that can ever be set, protecting wallet owners from runaway fees.
    uint256 public constant MAX_PROTOCOL_FEE = 0.001 ether;

    /// @notice Flat ETH fee charged on every session-key execution across all wallets.
    uint256 public protocolFee;

    /// @notice Address that receives protocol fees collected by SessionHandler wallets.
    address public treasury;

    /// @notice Canonical SHOracle used by all SessionHandler wallets for USD spending limit enforcement.
    address public priceOracle;

    /// @notice Id of the SessionHandler ERC-4337 AI agent registered on ERC-8004 Identity Registery
    uint256 public agentId;

    /// @notice Uniswap V2 Router address used for swap and liquidity calldata parsing.
    /// @dev May be address(0) on chains where Uniswap V2 is not deployed.
    address public uniswapRouter;

    /// @notice SHValueInterpreter used by all SessionHandler wallets to compute USD spend values.
    address public callValueInterpreter;

    /*//////////////////////////////////////////////////////////////
                                  EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when the protocol fee is updated.
    /// @param oldFee The previous fee in wei.
    /// @param newFee The new fee in wei.
    event ProtocolFeeUpdated(uint256 oldFee, uint256 newFee);

    /// @notice Emitted when the treasury address is updated.
    /// @param oldTreasury The previous treasury address.
    /// @param newTreasury The new treasury address.
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);

    /// @notice Emitted when the price oracle address is updated.
    /// @param oldOracle The previous price oracle address.
    /// @param newOracle The new price oracle address.
    event PriceOracleUpdated(address indexed oldOracle, address indexed newOracle);

    /// @notice Emitted when the registered agentId is updated.
    /// @param oldId The previous agentId.
    /// @param newId The new agentId.
    event AgentIdUpdated(uint256 indexed oldId, uint256 indexed newId);

    /// @notice Emitted when the Uniswap V2 Router address is updated.
    /// @param oldRouter The previous router address.
    /// @param newRouter The new router address.
    event UniswapRouterUpdated(address indexed oldRouter, address indexed newRouter);

    /// @notice Emitted when the call value interpreter address is updated.
    /// @param oldInterpreter The previous interpreter address.
    /// @param newInterpreter The new interpreter address.
    event CallValueInterpreterUpdated(address indexed oldInterpreter, address indexed newInterpreter);

    /*//////////////////////////////////////////////////////////////
                               CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Deploys the SHRegistry with an initial fee and protocol addresses.
     * @param initialFee           Starting protocol fee in wei. Must not exceed MAX_PROTOCOL_FEE.
     * @param initialTreasury      Address that will receive protocol fees. Must not be address(0).
     * @param initialOracle        Address of the deployed SHOracle. Must not be address(0).
     * @param initialAgentId       Id of the SessionHandler agent on the ERC-8004 Identity Registry. Must not be 0.
     * @param initialUniswapRouter Uniswap V2 Router address. May be address(0) on chains without Uniswap V2.
     */
    constructor(
        uint256 initialFee,
        address initialTreasury,
        address initialOracle,
        uint256 initialAgentId,
        address initialUniswapRouter
    ) Ownable(msg.sender) {
        if (initialFee > MAX_PROTOCOL_FEE) revert SHRegistry_FeeTooHigh();
        if (initialTreasury == address(0)) revert SHRegistry_InvalidTreasury();
        if (initialOracle == address(0)) revert SHRegistry_InvalidPriceOracle();
       
        protocolFee = initialFee;
        treasury = initialTreasury;
        priceOracle = initialOracle;
        agentId = initialAgentId;
        uniswapRouter = initialUniswapRouter;
    }

    /*//////////////////////////////////////////////////////////////
                            EXTERNAL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Updates the protocol fee charged on every session-key execution. Only callable by the owner.
     * @param newFee The new fee in wei. Must not exceed MAX_PROTOCOL_FEE.
     */
    function setProtocolFee(uint256 newFee) external onlyOwner {
        if (newFee > MAX_PROTOCOL_FEE) revert SHRegistry_FeeTooHigh();
        uint256 oldFee = protocolFee;
        protocolFee = newFee;
        emit ProtocolFeeUpdated(oldFee, newFee);
    }

    /**
     * @notice Updates the treasury address that receives protocol fees. Only callable by the owner.
     * @param newTreasury The new treasury address. Must not be address(0).
     */
    function setTreasury(address newTreasury) external onlyOwner {
        if (newTreasury == address(0)) revert SHRegistry_InvalidTreasury();
        address oldTreasury = treasury;
        treasury = newTreasury;
        emit TreasuryUpdated(oldTreasury, newTreasury);
    }

    /**
     * @notice Updates the canonical SHOracle used by all SessionHandler wallets. Only callable by the owner.
     * @dev Existing wallets will read the new oracle address on their next execution — no redeployment needed.
     * @param newOracle The new SHOracle address. Must not be address(0).
     */
    function setPriceOracle(address newOracle) external onlyOwner {
        if (newOracle == address(0)) revert SHRegistry_InvalidPriceOracle();
        address oldOracle = priceOracle;
        priceOracle = newOracle;
        emit PriceOracleUpdated(oldOracle, newOracle);
    }

    /**
     * @notice Updates the registered agentId for the SessionHandler Protocol on the ERC-8004 Identity Registry. Only callable by the owner.
     * @dev Reverts if newId is 0 or equal to the current agentId.
     * @param newId The new agentId. Must not be 0 and must differ from the current agentId.
     */
    function setAgentId(uint256 newId) external onlyOwner {
        uint256 oldId = agentId;
        agentId = newId;
        emit AgentIdUpdated(oldId, newId);
    }

    /**
     * @notice Updates the Uniswap V2 Router address. Only callable by the owner.
     * @dev Pass address(0) to disable Uniswap calldata parsing on chains where it is unavailable.
     * @param newRouter The new Uniswap V2 Router address.
     */
    function setUniswapRouter(address newRouter) external onlyOwner {
        address old = uniswapRouter;
        uniswapRouter = newRouter;
        emit UniswapRouterUpdated(old, newRouter);
    }

    /**
     * @notice Updates the SHValueInterpreter used by all SessionHandler wallets. Only callable by the owner.
     * @dev Existing wallets will use the new interpreter on their next execution — no redeployment needed.
     * @param newInterpreter The new SHValueInterpreter address. Must not be address(0).
     */
    function setCallValueInterpreter(address newInterpreter) external onlyOwner {
        if (newInterpreter == address(0)) revert SHRegistry_InvalidCallValueInterpreter();
        address old = callValueInterpreter;
        callValueInterpreter = newInterpreter;
        emit CallValueInterpreterUpdated(old, newInterpreter);
    }

}
