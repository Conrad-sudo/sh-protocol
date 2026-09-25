// SPDX-License-Identifier: MIT
pragma solidity ^0.8.33;

import {Test} from "forge-std/Test.sol";
import {Execution} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {
    ERC7579Utils, Mode, CallType, ExecType, ModeSelector, ModePayload
} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @dev Owner signs ONCE off-chain; verified on-chain from calldata, no external service.
struct SessionGrant {
    address account;      // the wallet (also EIP-712 verifyingContract)
    address sessionKey;   // must equal currentSession
    bytes32 callsRoot;    // merkle root over allowed (target, selector) leaves
    uint48  validUntil;   // grant expiry
    uint256 grantNonce;   // owner-bumpable => instant revoke
}

/// @notice Reference harness for sh-protocol#1 — an EIP-712 per-key grant that extends the
///         account-side allowlist from address-granular to (target, selector)-granular, verified
///         from calldata with no service in the loop, against the SAME ERC-7579 decode path that
///         `SessionHandler._guardSessionExecution` uses (decodeMode -> SINGLE/BATCH/DELEGATECALL).
///         Additive: this does NOT modify SessionHandler; it demonstrates the shape so the logic can
///         be lifted into `_guardSessionExecution` (owner-driven `execute` stays unrestricted).
contract MockGrantedAccount is EIP712, Ownable {
    using ERC7579Utils for *;

    error BadGrantDomain();
    error BadGrantKey();
    error GrantExpired();
    error GrantRevoked();
    error BadGrantSig();
    error SelectorNotGranted(address target, bytes4 selector);
    error SessionDelegateCallForbidden();

    bytes32 private constant _GRANT_TYPEHASH = keccak256(
        "SessionGrant(address account,address sessionKey,bytes32 callsRoot,uint48 validUntil,uint256 grantNonce)"
    );

    address public currentSession;
    uint256 public sessionGrantNonce;

    constructor(address owner_, address session_) EIP712("SessionHandler", "1") Ownable(owner_) {
        currentSession = session_;
    }

    function bumpGrantNonce() external onlyOwner { sessionGrantNonce++; }

    function grantDigest(SessionGrant calldata g) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(
            _GRANT_TYPEHASH, g.account, g.sessionKey, g.callsRoot, g.validUntil, g.grantNonce
        )));
    }

    function leaf(address target, bytes4 selector) public pure returns (bytes32) {
        return keccak256(bytes.concat(bytes20(target), bytes4(selector)));
    }

    function _sel(bytes calldata cd) private pure returns (bytes4) {
        return cd.length >= 4 ? bytes4(cd[:4]) : bytes4(0);
    }

    function _verify(SessionGrant calldata g, address target, bytes4 selector, bytes32[] calldata proof)
        private pure
    {
        if (!MerkleProof.verifyCalldata(proof, g.callsRoot, keccak256(bytes.concat(bytes20(target), bytes4(selector)))))
            revert SelectorNotGranted(target, selector);
    }

    /// @dev Same signature shape as `_guardSessionExecution`, plus the owner-signed grant + merkle proofs.
    function guardGrantedExecution(
        Mode mode,
        bytes calldata executionCalldata,
        SessionGrant calldata g,
        bytes calldata ownerSig,
        bytes32[][] calldata proofs
    ) external view {
        if (g.account != address(this))       revert BadGrantDomain();
        if (g.sessionKey != currentSession)     revert BadGrantKey();
        if (block.timestamp > g.validUntil)     revert GrantExpired();
        if (g.grantNonce != sessionGrantNonce)  revert GrantRevoked();
        if (ECDSA.recover(grantDigest(g), ownerSig) != owner()) revert BadGrantSig();

        (CallType callType,,,) = ERC7579Utils.decodeMode(mode);
        if (callType == ERC7579Utils.CALLTYPE_SINGLE) {
            (address target,, bytes calldata cd) = ERC7579Utils.decodeSingle(executionCalldata);
            _verify(g, target, _sel(cd), proofs[0]);
        } else if (callType == ERC7579Utils.CALLTYPE_BATCH) {
            Execution[] calldata batch = ERC7579Utils.decodeBatch(executionCalldata);
            for (uint256 i; i < batch.length; ++i) {
                _verify(g, batch[i].target, _sel(batch[i].callData), proofs[i]);
            }
        } else if (callType == ERC7579Utils.CALLTYPE_DELEGATECALL) {
            revert SessionDelegateCallForbidden();
        }
    }
}

contract SessionGrantReferenceTest is Test {
    MockGrantedAccount acct;
    uint256 ownerPk = 0xA11CE;
    address owner;
    address session = address(0x5E5510);
    address target = address(0xDEF1);

    bytes4 constant SEL_A = 0x11111111;
    bytes4 constant SEL_B = 0x22222222;
    bytes4 constant SEL_C = 0x33333333; // NOT granted

    bytes32 leafA;
    bytes32 leafB;
    bytes32 root;

    function setUp() public {
        owner = vm.addr(ownerPk);
        acct = new MockGrantedAccount(owner, session);
        leafA = acct.leaf(target, SEL_A);
        leafB = acct.leaf(target, SEL_B);
        root = _commutative(leafA, leafB);
    }

    function _commutative(bytes32 a, bytes32 b) internal pure returns (bytes32) {
        return a < b ? keccak256(abi.encodePacked(a, b)) : keccak256(abi.encodePacked(b, a));
    }
    function _grant(address account, uint48 validUntil, uint256 nonce) internal view returns (SessionGrant memory) {
        return SessionGrant({account: account, sessionKey: session, callsRoot: root, validUntil: validUntil, grantNonce: nonce});
    }
    function _sign(uint256 pk, SessionGrant memory g) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, acct.grantDigest(g));
        return abi.encodePacked(r, s, v);
    }
    function _batchMode() internal pure returns (Mode) {
        return ERC7579Utils.encodeMode(ERC7579Utils.CALLTYPE_BATCH, ERC7579Utils.EXECTYPE_DEFAULT, ModeSelector.wrap(bytes4(0)), ModePayload.wrap(bytes22(0)));
    }
    function _execs(bytes4 sel) internal view returns (bytes memory) {
        Execution[] memory e = new Execution[](1);
        e[0] = Execution({target: target, value: 0, callData: abi.encodePacked(sel, uint256(1))});
        return ERC7579Utils.encodeBatch(e);
    }
    function _proof1(bytes32 sibling) internal pure returns (bytes32[][] memory p) {
        p = new bytes32[][](1); p[0] = new bytes32[](1); p[0][0] = sibling;
    }

    function test_grantedSelector_passes() public view {
        SessionGrant memory g = _grant(address(acct), uint48(block.timestamp + 1 days), 0);
        acct.guardGrantedExecution(_batchMode(), _execs(SEL_A), g, _sign(ownerPk, g), _proof1(leafB));
    }

    function test_ungrantedSelector_reverts() public {
        SessionGrant memory g = _grant(address(acct), uint48(block.timestamp + 1 days), 0);
        Mode m = _batchMode(); bytes memory ec = _execs(SEL_C); bytes memory sig = _sign(ownerPk, g); bytes32[][] memory pr = _proof1(leafB);
        vm.expectRevert(abi.encodeWithSelector(MockGrantedAccount.SelectorNotGranted.selector, target, SEL_C));
        acct.guardGrantedExecution(m, ec, g, sig, pr);
    }

    function test_mixedBatch_reverts_atomic() public {
        SessionGrant memory g = _grant(address(acct), uint48(block.timestamp + 1 days), 0);
        Execution[] memory e = new Execution[](2);
        e[0] = Execution({target: target, value: 0, callData: abi.encodePacked(SEL_A, uint256(1))});
        e[1] = Execution({target: target, value: 0, callData: abi.encodePacked(SEL_C, uint256(1))});
        bytes memory ec = ERC7579Utils.encodeBatch(e);
        bytes memory sig = _sign(ownerPk, g);
        bytes32[][] memory pr = new bytes32[][](2);
        pr[0] = new bytes32[](1); pr[0][0] = leafB;
        pr[1] = new bytes32[](1); pr[1][0] = leafB;
        Mode m = _batchMode();
        vm.expectRevert(abi.encodeWithSelector(MockGrantedAccount.SelectorNotGranted.selector, target, SEL_C));
        acct.guardGrantedExecution(m, ec, g, sig, pr);
    }

    function test_expiry_inclusive() public {
        uint48 exp = uint48(block.timestamp + 100);
        SessionGrant memory g = _grant(address(acct), exp, 0);
        Mode m = _batchMode(); bytes memory ec = _execs(SEL_A); bytes memory sig = _sign(ownerPk, g); bytes32[][] memory pr = _proof1(leafB);
        vm.warp(exp);
        acct.guardGrantedExecution(m, ec, g, sig, pr); // == validUntil: passes
        vm.warp(uint256(exp) + 1);
        vm.expectRevert(MockGrantedAccount.GrantExpired.selector);
        acct.guardGrantedExecution(m, ec, g, sig, pr);
    }

    function test_wrongAccount_reverts() public {
        SessionGrant memory g = _grant(address(0xBEEF), uint48(block.timestamp + 1 days), 0);
        Mode m = _batchMode(); bytes memory ec = _execs(SEL_A); bytes memory sig = _sign(ownerPk, g); bytes32[][] memory pr = _proof1(leafB);
        vm.expectRevert(MockGrantedAccount.BadGrantDomain.selector);
        acct.guardGrantedExecution(m, ec, g, sig, pr);
    }

    function test_staleNonce_reverts() public {
        SessionGrant memory g = _grant(address(acct), uint48(block.timestamp + 1 days), 0);
        Mode m = _batchMode(); bytes memory ec = _execs(SEL_A); bytes memory sig = _sign(ownerPk, g); bytes32[][] memory pr = _proof1(leafB);
        vm.prank(owner); acct.bumpGrantNonce();
        vm.expectRevert(MockGrantedAccount.GrantRevoked.selector);
        acct.guardGrantedExecution(m, ec, g, sig, pr);
    }

    function test_badSignature_reverts() public {
        SessionGrant memory g = _grant(address(acct), uint48(block.timestamp + 1 days), 0);
        Mode m = _batchMode(); bytes memory ec = _execs(SEL_A); bytes memory badSig = _sign(0xB0B, g); bytes32[][] memory pr = _proof1(leafB);
        vm.expectRevert(MockGrantedAccount.BadGrantSig.selector);
        acct.guardGrantedExecution(m, ec, g, badSig, pr);
    }

    function test_delegatecall_forbidden() public {
        SessionGrant memory g = _grant(address(acct), uint48(block.timestamp + 1 days), 0);
        Mode m = ERC7579Utils.encodeMode(ERC7579Utils.CALLTYPE_DELEGATECALL, ERC7579Utils.EXECTYPE_DEFAULT, ModeSelector.wrap(bytes4(0)), ModePayload.wrap(bytes22(0)));
        bytes memory sig = _sign(ownerPk, g); bytes32[][] memory pr = _proof1(leafB);
        vm.expectRevert(MockGrantedAccount.SessionDelegateCallForbidden.selector);
        acct.guardGrantedExecution(m, hex"", g, sig, pr);
    }
}
