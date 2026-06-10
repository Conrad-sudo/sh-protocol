// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IIdentityRegistry {

    // ── Structs ───────────────────────────────────────────────────────────────

    struct MetadataEntry {
        string metadataKey;
        bytes metadataValue;
    }

    // ── Events ────────────────────────────────────────────────────────────────

    event Registered(uint256 indexed agentId, string agentURI, address indexed owner);
    event MetadataSet(
        uint256 indexed agentId,
        string indexed indexedMetadataKey,
        string metadataKey,
        bytes metadataValue
    );
    event URIUpdated(uint256 indexed agentId, string newURI, address indexed updatedBy);

    // ── Registration ──────────────────────────────────────────────────────────

    function register() external returns (uint256 agentId);
    function register(string memory agentURI) external returns (uint256 agentId);
    function register(string memory agentURI, MetadataEntry[] memory metadata) external returns (uint256 agentId);

    // ── URI & Metadata ────────────────────────────────────────────────────────

    function setAgentURI(uint256 agentId, string calldata newURI) external;
    function getMetadata(uint256 agentId, string memory metadataKey) external view returns (bytes memory);
    function setMetadata(uint256 agentId, string memory metadataKey, bytes memory metadataValue) external;

    // ── Agent wallet ──────────────────────────────────────────────────────────

    function getAgentWallet(uint256 agentId) external view returns (address);
    function setAgentWallet(uint256 agentId, address newWallet, uint256 deadline, bytes calldata signature) external;
    function unsetAgentWallet(uint256 agentId) external;

    // ── ERC-721 ───────────────────────────────────────────────────────────────

    function tokenURI(uint256 tokenId) external view returns (string memory);
    function ownerOf(uint256 tokenId) external view returns (address);
    function balanceOf(address owner) external view returns (uint256);
    function isAuthorizedOrOwner(address spender, uint256 agentId) external view returns (bool);

    // ── Utility ───────────────────────────────────────────────────────────────

    function getVersion() external pure returns (string memory);
}
