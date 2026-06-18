/* SPDX-License-Identifier: BUSL-1.1
 * Licensor: Conrad Japhet
 * Licensed Work: SHValueInterpreter.sol
 * Change Date: 2029-06-12
 * Change License: MIT
*/
pragma solidity ^0.8.24;

import {SHOracle} from "./SHOracle.sol";
import {SHRegistry} from "./SHRegistry.sol";
import {IUniswapV2Router01} from "lib/v2-periphery/contracts/interfaces/IUniswapV2Router01.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IWETH} from "./interfaces/IWETH.sol";

/**
 * @title SHValueInterpreter
 * @author Conrad Japhet
 * @notice Decodes session-key calldata and converts the involved token amounts to USD.
 * @dev Called by SessionHandler.execute() to compute the debit or credit USD value of a
 *      session-key operation. Reads the SHOracle and uniswapRouter addresses from
 *      SHRegistry at call time so both can be updated without redeploying any wallets.
 *
 *      Supported operations:
 *      - Native ETH sends (value > 0)
 *      - ERC-20 transfer / transferFrom
 *      - Uniswap V2 swaps, addLiquidity, removeLiquidity variants
 *      - WETH deposit (ETH component excluded — only token side is counted)
 *
 *      Returns (debitValueInUsd, creditValueInUsd). creditValueInUsd is non-zero only for
 *      removeLiquidity variants, where the session budget is credited rather than charged.
 */
contract SHValueInterpreter {

    /// @dev Anvil's default chainId. The zero-router guard is skipped on this chain since
    ///      Uniswap V2 has no local deployment there.
    uint256 private constant ANVIL_CHAIN_ID = 31337;
    /// @dev Sepolia's chainId. The zero-router guard is skipped here too — no official
    ///      Uniswap V2 deployment exists on Sepolia.
    uint256 private constant SEPOLIA_CHAIN_ID = 11155111;

    error SHValueInterpreter_ZeroAddressOnRouter();

    /// @dev SHRegistry supplying priceOracle and uniswapRouter at runtime.
    SHRegistry private immutable REGISTRY;

    constructor(address registry) {
        REGISTRY = SHRegistry(registry);
    }

    /**
     * @notice Computes the USD debit and credit values for a session-key call.
     * @dev Called from SessionHandler.execute() where external storage reads are unrestricted.
     *      Assembly offsets assume standard ABI encoding: 32-byte length prefix on `data`,
     *      followed by selector (4 bytes) and 32-byte-aligned parameters.
     * @param dest     Target contract address.
     * @param value    Native ETH forwarded with the call (wei).
     * @param data     Full calldata including the 4-byte selector.
     * @param selector The 4-byte function selector extracted from `data`.
     * @return debitValueInUsd  USD value to charge against the session budget (18 decimals).
     * @return creditValueInUsd USD value to credit back to the session budget (18 decimals).
     *                          Non-zero only for removeLiquidity variants.
     */
    function computeUsdValue(address dest, uint256 value, bytes memory data, bytes4 selector)
        external
        view
        returns (uint256 debitValueInUsd, uint256 creditValueInUsd)
    {   


        address uniswapRouter = REGISTRY.uniswapRouter();
        if (uniswapRouter == address(0) && block.chainid != ANVIL_CHAIN_ID && block.chainid != SEPOLIA_CHAIN_ID) {
            revert SHValueInterpreter_ZeroAddressOnRouter();
        }

        SHOracle oracle = SHOracle(REGISTRY.priceOracle());
        
        address token;
        uint256 extractedValue;

        // swapExactETHForTokens and swapETHForExactTokens forward ETH as `value` with no token input
        // parameter, so their USD cost is fully captured here. No additional interpreter branch needed.
        if (selector != IWETH.deposit.selector) {
            debitValueInUsd += oracle.getUsdValue(address(0), value);
        }

        if (data.length >= 68 && dest != uniswapRouter) {
            if (selector == IERC20.transfer.selector) {
                assembly {
                    extractedValue := mload(add(data, 68))
                }
            }
            if (selector == IERC20.transferFrom.selector) {
                assembly {
                    extractedValue := mload(add(data, 100))
                }
            }
            debitValueInUsd += oracle.getUsdValue(dest, extractedValue);
        }

        if (data.length >= 68 && dest == uniswapRouter) {
            if (selector == IUniswapV2Router01.swapTokensForExactETH.selector) {
                assembly {
                    extractedValue := mload(add(data, 36))
                }
                debitValueInUsd += oracle.getUsdValue(address(0), extractedValue);
            } else if (
                selector == IUniswapV2Router01.swapExactTokensForTokens.selector
                    || selector == IUniswapV2Router01.swapTokensForExactTokens.selector
                    || selector == IUniswapV2Router01.swapExactTokensForETH.selector
            ) {
                address tokenIn;
                address tokenOut;
                assembly {
                    extractedValue := mload(add(data, 36))
                    let paramsBase := add(data, 36)
                    let pathOffset := mload(add(paramsBase, 64))
                    let pathPtr := add(paramsBase, pathOffset)
                    let pathLen := mload(pathPtr)
                    tokenIn := mload(add(pathPtr, 32))
                    tokenOut := mload(add(pathPtr, add(32, mul(sub(pathLen, 1), 32))))
                }
                token = (
                    selector == IUniswapV2Router01.swapExactTokensForTokens.selector
                        || selector == IUniswapV2Router01.swapExactTokensForETH.selector
                ) ? tokenIn : tokenOut;
                debitValueInUsd += oracle.getUsdValue(token, extractedValue);
            } else if (selector == IUniswapV2Router01.addLiquidity.selector && data.length >= 132) {
                address tokenA;
                address tokenB;
                uint256 amountADesired;
                uint256 amountBDesired;
                assembly {
                    tokenA := mload(add(data, 36))
                    tokenB := mload(add(data, 68))
                    amountADesired := mload(add(data, 100))
                    amountBDesired := mload(add(data, 132))
                }
                debitValueInUsd += oracle.getUsdValue(tokenA, amountADesired);
                debitValueInUsd += oracle.getUsdValue(tokenB, amountBDesired);
            } else if (selector == IUniswapV2Router01.addLiquidityETH.selector) {
                uint256 amountTokenDesired;
                assembly {
                    token := mload(add(data, 36))
                    amountTokenDesired := mload(add(data, 68))
                }
                debitValueInUsd += oracle.getUsdValue(token, amountTokenDesired);
            } else if (selector == IUniswapV2Router01.removeLiquidity.selector && data.length >= 164) {
                address tokenA;
                address tokenB;
                uint256 amountAMin;
                uint256 amountBMin;
                assembly {
                    tokenA := mload(add(data, 36))
                    tokenB := mload(add(data, 68))
                    amountAMin := mload(add(data, 132))
                    amountBMin := mload(add(data, 164))
                }
                creditValueInUsd += oracle.getUsdValue(tokenA, amountAMin);
                creditValueInUsd += oracle.getUsdValue(tokenB, amountBMin);
            } else if (selector == IUniswapV2Router01.removeLiquidityETH.selector && data.length >= 132) {
                uint256 amountTokenMin;
                uint256 amountEthMin;
                assembly {
                    token := mload(add(data, 36))
                    amountTokenMin := mload(add(data, 100))
                    amountEthMin := mload(add(data, 132))
                }
                creditValueInUsd += oracle.getUsdValue(token, amountTokenMin);
                creditValueInUsd += oracle.getUsdValue(address(0), amountEthMin);
            }
        }
    }
}
