// SPDX-License-Identifier: MIT
pragma solidity 0.8.33;

import {Vm} from "forge-std/Vm.sol";
import {Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils, Mode, CallType} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";

/**
 * @title ContactPassHelper
 * @notice Builds the owner-signed contact passes PayeeGuard reads, and appends them to `execute` calldata
 *         the way Mitfah's app does: execute(...) || pass_1 .. pass_n || uint8 n || "MFP1".
 * @dev {attach} finds the payees a call names the way the guard does (a plain send's target; the
 *      recipient of transfer, transferFrom and the safe transfers) and signs one pass for each, so a test
 *      that only means to send to someone keeps working. A test about the contact rule itself builds its
 *      passes by hand with {pass} and {append}.
 */
library ContactPassHelper {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    bytes4 internal constant MAGIC = 0x4d465031;
    bytes32 internal constant TYPEHASH = keccak256("Contact(string name,address payee,uint64 issuedAt)");
    bytes32 internal constant NAME_HASH = keccak256("contact");

    function domainSeparator() internal pure returns (bytes32) {
        return keccak256(
            abi.encode(keccak256("EIP712Domain(string name,string version)"), keccak256("Mitfah"), keccak256("2"))
        );
    }

    function digest(address payee, uint64 issuedAt, bytes32 nameHash) internal pure returns (bytes32) {
        return keccak256(
            abi.encodePacked("\x19\x01", domainSeparator(), keccak256(abi.encode(TYPEHASH, nameHash, payee, issuedAt)))
        );
    }

    /// @dev A pass for `payee`, signed by `signerKey`, issued at `issuedAt`.
    function pass(uint256 signerKey, address payee, uint64 issuedAt) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, digest(payee, issuedAt, NAME_HASH));
        return abi.encodePacked(payee, issuedAt, NAME_HASH, r, s, v);
    }

    /// @dev A pass for `payee` issued now.
    function pass(uint256 signerKey, address payee) internal view returns (bytes memory) {
        return pass(signerKey, payee, uint64(block.timestamp));
    }

    /// @dev `callData` with `passes` and the footer appended.
    function append(bytes memory callData, bytes[] memory passes) internal pure returns (bytes memory out) {
        out = callData;
        for (uint256 i = 0; i < passes.length; i++) {
            out = bytes.concat(out, passes[i]);
        }
        out = bytes.concat(out, bytes1(uint8(passes.length)), MAGIC);
    }

    /// @dev `callData` (an `execute` call from `account`) with a pass, signed by `signerKey`, for every
    ///      payee it names other than `account`.
    function attach(bytes memory callData, address account, uint256 signerKey) internal view returns (bytes memory) {
        address[] memory payees = payeesOf(callData, account);
        bytes[] memory passes = new bytes[](payees.length);
        for (uint256 i = 0; i < payees.length; i++) {
            passes[i] = pass(signerKey, payees[i]);
        }
        return append(callData, passes);
    }

    /// @dev The distinct payees other than `account` that an `execute` call names.
    function payeesOf(bytes memory callData, address account) internal pure returns (address[] memory payees) {
        (bytes32 mode, bytes memory executionCalldata) = abi.decode(_slice(callData, 4), (bytes32, bytes));
        (CallType callType,,,) = ERC7579Utils.decodeMode(Mode.wrap(mode));
        address[] memory targets;
        bytes[] memory datas;
        if (callType == ERC7579Utils.CALLTYPE_BATCH) {
            // A test may pair batch mode with a payload that is no batch (to test the refusal):
            // name no payees then, rather than fail to decode it.
            if (executionCalldata.length < 64 || uint256(bytes32(executionCalldata)) != 0x20) return new address[](0);
            Execution[] memory batch = abi.decode(executionCalldata, (Execution[]));
            targets = new address[](batch.length);
            datas = new bytes[](batch.length);
            for (uint256 i = 0; i < batch.length; i++) {
                targets[i] = batch[i].target;
                datas[i] = batch[i].callData;
            }
        } else {
            targets = new address[](1);
            datas = new bytes[](1);
            targets[0] = address(bytes20(executionCalldata));
            datas[0] = _slice(executionCalldata, 52);
        }
        payees = new address[](targets.length);
        uint256 count;
        for (uint256 i = 0; i < targets.length; i++) {
            address payee = _payeeOf(targets[i], datas[i]);
            if (payee == address(0) || payee == account) continue;
            bool seen;
            for (uint256 j = 0; j < count; j++) {
                if (payees[j] == payee) seen = true;
            }
            if (!seen) payees[count++] = payee;
        }
        assembly {
            mstore(payees, count)
        }
    }

    function _payeeOf(address target, bytes memory data) private pure returns (address) {
        if (data.length == 0) return target;
        if (data.length < 68) return address(0);
        bytes4 selector = bytes4(data);
        if (selector == 0xa9059cbb) return abi.decode(_slice(data, 4), (address));
        if (
            selector == 0x23b872dd || selector == 0x42842e0e || selector == 0xb88d4fde || selector == 0xf242432a
                || selector == 0x2eb2c2d6
        ) {
            (, address to) = abi.decode(_slice(data, 4), (address, address));
            return to;
        }
        return address(0);
    }

    function _slice(bytes memory data, uint256 from) private pure returns (bytes memory out) {
        out = new bytes(data.length - from);
        for (uint256 i = 0; i < out.length; i++) {
            out[i] = data[from + i];
        }
    }
}
