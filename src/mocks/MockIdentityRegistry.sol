// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {ERC721URIStorage} from "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {IERC1271} from "@openzeppelin/contracts/interfaces/IERC1271.sol";
import {IIdentityRegistry} from "../interfaces/IIdentityRegistry.sol";

/// @notice Non-upgradeable mock of the ERC-8004 Identity Registry, for local/unit testing only.
contract MockIdentityRegistry is ERC721URIStorage, EIP712, IIdentityRegistry {
    bytes32 private constant AGENT_WALLET_SET_TYPEHASH =
        keccak256("AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)");
    bytes4 private constant ERC1271_MAGICVALUE = 0x1626ba7e;
    uint256 private constant MAX_DEADLINE_DELAY = 5 minutes;
    bytes32 private constant RESERVED_AGENT_WALLET_KEY_HASH = keccak256("agentWallet");

    uint256 private _lastId;
    mapping(uint256 => mapping(string => bytes)) private _metadata;

    constructor() ERC721("AgentIdentity", "AGENT") EIP712("ERC8004IdentityRegistry", "1") {}

    function register() external returns (uint256 agentId) {
        agentId = _lastId++;
        _metadata[agentId]["agentWallet"] = abi.encodePacked(msg.sender);
        _safeMint(msg.sender, agentId);
        emit Registered(agentId, "", msg.sender);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(msg.sender));
    }

    function register(string memory agentURI) external returns (uint256 agentId) {
        agentId = _lastId++;
        _metadata[agentId]["agentWallet"] = abi.encodePacked(msg.sender);
        _safeMint(msg.sender, agentId);
        _setTokenURI(agentId, agentURI);
        emit Registered(agentId, agentURI, msg.sender);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(msg.sender));
    }

    function register(string memory agentURI, MetadataEntry[] memory metadata) external returns (uint256 agentId) {
        agentId = _lastId++;
        _metadata[agentId]["agentWallet"] = abi.encodePacked(msg.sender);
        _safeMint(msg.sender, agentId);
        _setTokenURI(agentId, agentURI);
        emit Registered(agentId, agentURI, msg.sender);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(msg.sender));

        for (uint256 i; i < metadata.length; i++) {
            require(keccak256(bytes(metadata[i].metadataKey)) != RESERVED_AGENT_WALLET_KEY_HASH, "reserved key");
            _metadata[agentId][metadata[i].metadataKey] = metadata[i].metadataValue;
            emit MetadataSet(agentId, metadata[i].metadataKey, metadata[i].metadataKey, metadata[i].metadataValue);
        }
    }

    function getMetadata(uint256 agentId, string memory metadataKey) external view returns (bytes memory) {
        return _metadata[agentId][metadataKey];
    }

    function setMetadata(uint256 agentId, string memory metadataKey, bytes memory metadataValue) external {
        address agentOwner = _ownerOf(agentId);
        require(
            msg.sender == agentOwner || isApprovedForAll(agentOwner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        require(keccak256(bytes(metadataKey)) != RESERVED_AGENT_WALLET_KEY_HASH, "reserved key");
        _metadata[agentId][metadataKey] = metadataValue;
        emit MetadataSet(agentId, metadataKey, metadataKey, metadataValue);
    }

    function setAgentURI(uint256 agentId, string calldata newURI) external {
        address owner = ownerOf(agentId);
        require(
            msg.sender == owner || isApprovedForAll(owner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        _setTokenURI(agentId, newURI);
        emit URIUpdated(agentId, newURI, msg.sender);
    }

    function getAgentWallet(uint256 agentId) external view returns (address) {
        return address(bytes20(_metadata[agentId]["agentWallet"]));
    }

    function setAgentWallet(uint256 agentId, address newWallet, uint256 deadline, bytes calldata signature) external {
        address owner = ownerOf(agentId);
        require(
            msg.sender == owner || isApprovedForAll(owner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        require(newWallet != address(0), "bad wallet");
        require(block.timestamp <= deadline, "expired");
        require(deadline <= block.timestamp + MAX_DEADLINE_DELAY, "deadline too far");

        // forge-lint: disable-next-line(asm-keccak256)
        bytes32 structHash = keccak256(abi.encode(AGENT_WALLET_SET_TYPEHASH, agentId, newWallet, owner, deadline));
        bytes32 digest = _hashTypedDataV4(structHash);

        // Try ECDSA first (EOAs + EIP-7702 delegated EOAs)
        (address recovered, ECDSA.RecoverError err,) = ECDSA.tryRecover(digest, signature);
        if (err != ECDSA.RecoverError.NoError || recovered != newWallet) {
            // ECDSA failed, try ERC1271 (smart contract wallets)
            (bool ok, bytes memory res) = newWallet.staticcall(abi.encodeCall(IERC1271.isValidSignature, (digest, signature)));
            require(ok && res.length >= 32 && abi.decode(res, (bytes4)) == ERC1271_MAGICVALUE, "invalid wallet sig");
        }

        _metadata[agentId]["agentWallet"] = abi.encodePacked(newWallet);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(newWallet));
    }

    function unsetAgentWallet(uint256 agentId) external {
        address owner = ownerOf(agentId);
        require(
            msg.sender == owner || isApprovedForAll(owner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        _metadata[agentId]["agentWallet"] = "";
        emit MetadataSet(agentId, "agentWallet", "agentWallet", "");
    }

    /// @dev Clears the verified agentWallet on transfer so it doesn't persist to the new owner.
    function _update(address to, uint256 tokenId, address auth) internal override returns (address) {
        address from = _ownerOf(tokenId);

        if (from != address(0) && to != address(0)) {
            _metadata[tokenId]["agentWallet"] = "";
            emit MetadataSet(tokenId, "agentWallet", "agentWallet", "");
        }

        return super._update(to, tokenId, auth);
    }

    function isAuthorizedOrOwner(address spender, uint256 agentId) external view returns (bool) {
        address owner = ownerOf(agentId);
        return _isAuthorized(owner, spender, agentId);
    }

    function getVersion() external pure returns (string memory) {
        return "1.0.0-mock";
    }

    function tokenURI(uint256 tokenId) public view override(ERC721URIStorage, IIdentityRegistry) returns (string memory) {
        return super.tokenURI(tokenId);
    }

    function ownerOf(uint256 tokenId) public view override(ERC721, IERC721, IIdentityRegistry) returns (address) {
        return super.ownerOf(tokenId);
    }

    function balanceOf(address owner) public view override(ERC721, IERC721, IIdentityRegistry) returns (uint256) {
        return super.balanceOf(owner);
    }
}
