// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {AppCheckBase} from "./AppCheckBase.sol";
import {IAppCheck} from "../interfaces/IAppCheck.sol";
import {IReputationRegistry} from "../interfaces/IReputationRegistry.sol";

/**
 * @title ReputationCheck
 * @author Conrad Japhet
 * @notice The app check for the ERC-8004 Reputation Registry: the reviews a wallet leaves for an agent.
 * @dev `giveFeedback`, `revokeFeedback` and `appendResponse`, carrying no value, move nothing of the
 *      wallet's: they only record what it said. Anything else is refused.
 */
contract ReputationCheck is AppCheckBase {
    /// @param reputationRegistry This chain's ERC-8004 Reputation Registry.
    constructor(address reputationRegistry) AppCheckBase(reputationRegistry) {}

    /// @inheritdoc IAppCheck
    function inspect(address, uint256 value, bytes calldata data)
        external
        view
        returns (address[] memory, Trade[] memory, PoolPrice[] memory)
    {
        bytes4 selector = data.length >= 4 ? bytes4(data[:4]) : bytes4(0);
        bool known = selector == IReputationRegistry.giveFeedback.selector
            || selector == IReputationRegistry.revokeFeedback.selector
            || selector == IReputationRegistry.appendResponse.selector;
        if (!known || value != 0) revert AppCheck_UnsupportedCall(app, selector);
        return _finish(_newReport(0));
    }
}
