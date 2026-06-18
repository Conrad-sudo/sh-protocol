/* SPDX-License-Identifier: BUSL-1.1
 * Licensor: Conrad Japhet
 * Licensed Work: SHFactory.sol
 * Change Date: 2029-06-12
 * Change License: MIT
*/
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SessionHandler} from "./SessionHandler.sol";

contract SHFactory is Ownable, Pausable {
    error SHFactory_FundTransferFailed();

    /// @notice The SHRegistry address that deployed SessionHandlers read protocol configuration from.
    address private immutable REGISTRY;
    /// @notice The canonical ERC-4337 EntryPoint baked into every deployed SessionHandler.
    address private immutable ENTRY_POINT;
    /// @notice The Reputation Registry baked into every deployed SessionHandler.
    address private immutable REPUTATION_REGISTRY;
    /// @notice The ERC-8004 Identity Registry baked into every deployed SessionHandler.
    address private immutable IDENTITY_REGISTRY;

    event WalletDeployed(address indexed walletAddress, address indexed owner);

    /**
     * @param _entryPoint         The canonical ERC-4337 EntryPoint address.
     * @param _feeRegistry        The SHRegistry address that deployed Session Handlers read
     *                            protocol configuration (fee, treasury, oracle, etc.) from.
     * @param _reputationRegistry The Reputation Registry address.
     * @param _identityRegistry   The ERC-8004 Identity Registry address.
     */
    constructor(address _entryPoint, address _feeRegistry, address _reputationRegistry, address _identityRegistry) Ownable(msg.sender) {
        ENTRY_POINT = _entryPoint;
        REGISTRY = _feeRegistry;
        REPUTATION_REGISTRY = _reputationRegistry;
        IDENTITY_REGISTRY = _identityRegistry;
    }

    /// @notice Pauses the contract, disabling execute(). Only callable by the owner.
    function pause() public onlyOwner {
        _pause();
    }

    /// @notice Unpauses the contract, re-enabling execute(). Only callable by the owner.
    function unpause() public onlyOwner {
        _unpause();
    }

    function deployWallet() external payable whenNotPaused returns (address) {
        SessionHandler sessionHandler =
            new SessionHandler(msg.sender, ENTRY_POINT, REPUTATION_REGISTRY, IDENTITY_REGISTRY, REGISTRY);

        (bool success,) = payable(address(sessionHandler)).call{value: msg.value}("");
        if (!success) {
            revert SHFactory_FundTransferFailed();
        }

        emit WalletDeployed(address(sessionHandler), msg.sender);
        return address(sessionHandler);
    }
}

