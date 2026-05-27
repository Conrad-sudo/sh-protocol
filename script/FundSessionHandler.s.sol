//SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {Script, console} from "forge-std/Script.sol";

contract FundSessionHandler is Script {
    function run() external {
        vm.startBroadcast();
        (bool success,) = payable(0xd3384611b82ad4B456Ce735B5051c072942d36C2).call{value: 10 ether}("");
        require(success, "Failed to fund the address");
        vm.stopBroadcast();
    }
}
