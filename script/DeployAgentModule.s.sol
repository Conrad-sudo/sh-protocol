// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {AgentModule} from "../src/AgentModule.sol";

/**
 * @title DeployAgentModule
 * @notice Deploys AgentModule on its own, on a chain whose protocol is already deployed.
 * @dev AgentModule has no owner, no constructor arguments and no protocol addresses (it reads all it
 *      needs from each wallet), and no other contract refers to it, so adding it needs no protocol
 *      redeploy. Owners install it on their own wallets with `installModule(2, module, "")`.
 *      Broadcasts as whatever signer the command line names (`make deploy-agent-module ARGS=…` passes
 *      the same one as `make deploy`); who deploys it doesn't matter.
 */
contract DeployAgentModule is Script {
    /// @return agents The deployed module.
    function run() external returns (AgentModule agents) {
        vm.startBroadcast();
        agents = new AgentModule();
        vm.stopBroadcast();
        console2.log("AgentModule:", address(agents));
    }
}
