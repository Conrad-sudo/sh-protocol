// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {HelperConfig} from "../../script/HelperConfig.s.sol";

/// @dev Every ERC-20 a NetworkConfig names, address(0) where the network has none. Listed from the
///      struct on purpose, NOT from DeploySHProtocol, so tests can check the script against it: a
///      token the script forgets to hand the oracle is unpriced, and the app still offers it.
///      A new token field in NetworkConfig needs adding here too.
function configuredTokens(HelperConfig.NetworkConfig memory c) pure returns (address[22] memory) {
    return [
        c.usdc, c.dai, c.usdt, c.weth, c.aave, c.link, c.oneinch, c.ape, c.arb, c.wbnb, c.wbtc, c.comp,
        c.crv, c.ens, c.sand, c.sushi, c.wtao, c.uni, c.yfi, c.wavax, c.imx, c.cake
    ];
}
