// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

/// @dev Standard Chainlink price feed interface. Vendored locally (rather than pulling in the
///      full chainlink-brownie-contracts submodule) since this is the only piece we need, keeping
///      the audit surface small.
interface AggregatorV3Interface {
    function decimals() external view returns (uint8);

    function description() external view returns (string memory);

    function version() external view returns (uint256);

    function getRoundData(uint80 _roundId)
        external
        view
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound);

    function latestRoundData()
        external
        view
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound);
}
