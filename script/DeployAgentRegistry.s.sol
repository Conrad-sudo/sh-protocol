//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;
import {Script, console} from "forge-std/Script.sol";
import {AgentIdentityRegistry} from "../src/AgentIdentityRegistry.sol";



contract DeployAgentRegistry is Script {

    function run() external returns (AgentIdentityRegistry) {
        vm.startBroadcast();
        AgentIdentityRegistry registry = new AgentIdentityRegistry();
        vm.stopBroadcast();
        return registry;
    }
}