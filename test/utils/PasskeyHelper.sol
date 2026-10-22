// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Vm} from "forge-std/Vm.sol";
import {Base64} from "@openzeppelin/contracts/utils/Base64.sol";
import {P256} from "@openzeppelin/contracts/utils/cryptography/P256.sol";
import {WebAuthnNative} from "../../src/utils/WebAuthnNative.sol";

/**
 * @title PasskeyHelper
 * @notice A SOFT passkey for tests: a P-256 key pair plus the code a browser and an authenticator
 *         would run to turn "sign this challenge" into the signature SessionHandler expects, so a
 *         test can approve a UserOp the way a user's phone does.
 * @dev Everything is built from the WebAuthn spec, not from OpenZeppelin's verifier, so a test fails
 *      when the contract and the real world disagree, not only when the contract and this file do.
 *      The pieces, in the order an authenticator produces them:
 *        - authenticatorData = sha256(rpId) || flags || signCount (4 bytes). Flags 0x05 is UP | UV:
 *          the user was present AND verified (Face ID, fingerprint, PIN).
 *        - clientDataJSON = what the BROWSER writes: the type, the challenge as base64url without
 *          padding, the origin.
 *        - the signature is ECDSA over sha256(authenticatorData || sha256(clientDataJSON)).
 *      OpenZeppelin refuses a signature whose `s` is above N/2 (the malleable twin), so {assertionOver}
 *      folds `s` down. A test that wants the rejected twin calls {withHighS}.
 * @dev The wire format {SessionHandler} reads is `qx || qy || abi.encode(r, s, challengeIndex,
 *      typeIndex, authenticatorData, clientDataJSON)` -- six values in a row, NOT `abi.encode(auth)` of
 *      the struct, which puts one extra offset word in front. {encode} writes the former and
 *      {encodeStructValue} the latter, so a test can show the latter is refused.
 *
 *      Use it as `using PasskeyHelper for PasskeyHelper.Passkey;`, then `key.sign(challenge)`.
 */
library PasskeyHelper {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    /// @dev A soft passkey: its private key (only a test ever holds one) and public key.
    struct Passkey {
        uint256 privateKey;
        bytes32 qx;
        bytes32 qy;
    }

    /// @dev The relying party every assertion here is made for. The server's default in development.
    string internal constant RP_ID = "localhost";
    /// @dev The origin the "browser" reports.
    string internal constant ORIGIN = "http://localhost:3000";

    /// @dev authenticatorData flag bits (WebAuthn level 2, 6.1): user present, user verified, backup
    ///      eligible, backup state.
    bytes1 internal constant UP = 0x01;
    bytes1 internal constant UV = 0x04;
    bytes1 internal constant BE = 0x08;
    bytes1 internal constant BS = 0x10;
    /// @dev What a platform authenticator reports after Face ID / fingerprint: present AND verified.
    bytes1 internal constant UP_UV = 0x05;

    /// @dev An op deadline later than any key's, for tests about something else: the key's own
    ///      deadline is then the op's whole validity window.
    uint48 internal constant LATEST_OP_DEADLINE = type(uint48).max;

    /*//////////////////////////////////////////////////////////////
                                 NONCES
    //////////////////////////////////////////////////////////////*/

    /// @dev The EntryPoint nonce key a passkey op must use: the op's own deadline in the top 48 bits,
    ///      then a 144-bit lane (the app draws it at random). SessionHandler refuses a passkey op
    ///      whose deadline is 0, so nonce key 0 is for the owner only.
    function nonceKey(uint48 opDeadline, uint144 lane) internal pure returns (uint192) {
        return (uint192(opDeadline) << 144) | lane;
    }

    /*//////////////////////////////////////////////////////////////
                                  KEYS
    //////////////////////////////////////////////////////////////*/

    /// @dev A passkey with the given private key, 0 < privateKey < the curve order.
    function fromPrivateKey(uint256 privateKey) internal pure returns (Passkey memory key) {
        (uint256 x, uint256 y) = vm.publicKeyP256(privateKey);
        key = Passkey({privateKey: privateKey, qx: bytes32(x), qy: bytes32(y)});
    }

    /// @dev A passkey derived from a name, so a test can have "alice's phone" and "bob's laptop" and
    ///      get the same keys on every run.
    function create(string memory label) internal pure returns (Passkey memory) {
        return fromPrivateKey(uint256(keccak256(bytes(label))) % (P256.N - 1) + 1);
    }

    /// @dev The id SessionHandler files this key under: keccak256(abi.encode(qx, qy)).
    function keyId(Passkey memory key) internal pure returns (bytes32) {
        return keccak256(abi.encode(key.qx, key.qy));
    }

    /*//////////////////////////////////////////////////////////////
                         THE PIECES OF AN ASSERTION
    //////////////////////////////////////////////////////////////*/

    /// @dev sha256(rpId) || flags || signCount, 37 bytes.
    function authenticatorData(string memory rpId, bytes1 flags, uint32 signCount)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(sha256(bytes(rpId)), flags, signCount);
    }

    /// @dev The client data a browser writes for `navigator.credentials.get`, in the field order
    ///      Firefox and Safari use (Chrome appends a field, see {clientDataJSONChrome}).
    function clientDataJSON(string memory type_, bytes32 challenge, string memory origin)
        internal
        pure
        returns (string memory)
    {
        return string.concat(
            '{"type":"',
            type_,
            '","challenge":"',
            Base64.encodeURL(abi.encodePacked(challenge)),
            '","origin":"',
            origin,
            '","crossOrigin":false}'
        );
    }

    /// @dev The same with the extra key Chrome puts at the end, which no verifier may choke on.
    function clientDataJSONChrome(bytes32 challenge, string memory origin) internal pure returns (string memory) {
        return string.concat(
            '{"type":"webauthn.get","challenge":"',
            Base64.encodeURL(abi.encodePacked(challenge)),
            '","origin":"',
            origin,
            '","crossOrigin":false,"other_keys_can_be_added_here":'
            '"do not compare clientDataJSON against a template. See https://goo.gl/yabPex"}'
        );
    }

    /// @dev Signs `authData || sha256(json)` as an authenticator does, and returns the six values
    ///      the contract wants. `s` is the LOW one; the two indices are found by searching the JSON,
    ///      which is how a server computes them from whatever a browser sent.
    function assertionOver(Passkey memory key, bytes memory authData, string memory json)
        internal
        pure
        returns (WebAuthnNative.WebAuthnAuth memory auth)
    {
        bytes32 digest = sha256(abi.encodePacked(authData, sha256(bytes(json))));
        (bytes32 r, bytes32 s) = vm.signP256(key.privateKey, digest);
        if (uint256(s) > P256.N / 2) s = bytes32(P256.N - uint256(s));
        auth = WebAuthnNative.WebAuthnAuth({
            r: r,
            s: s,
            challengeIndex: indexOf(bytes(json), bytes('"challenge":"')),
            typeIndex: indexOf(bytes(json), bytes('"type":"')),
            authenticatorData: authData,
            clientDataJSON: json
        });
    }

    /// @dev The honest assertion for `challenge`, with the flags the authenticator reports.
    function assertion(Passkey memory key, bytes32 challenge, bytes1 flags)
        internal
        pure
        returns (WebAuthnNative.WebAuthnAuth memory)
    {
        return assertionOver(
            key, authenticatorData(RP_ID, flags, 0), clientDataJSON("webauthn.get", challenge, ORIGIN)
        );
    }

    /// @dev The honest assertion: user present and verified.
    function assertion(Passkey memory key, bytes32 challenge)
        internal
        pure
        returns (WebAuthnNative.WebAuthnAuth memory)
    {
        return assertion(key, challenge, UP_UV);
    }

    /// @dev The malleable twin of a signature: `s` replaced by N - s, which is HIGH and still verifies
    ///      mathematically. OpenZeppelin refuses it; a server must never send it.
    function withHighS(WebAuthnNative.WebAuthnAuth memory auth)
        internal
        pure
        returns (WebAuthnNative.WebAuthnAuth memory)
    {
        auth.s = bytes32(P256.N - uint256(auth.s));
        return auth;
    }

    /*//////////////////////////////////////////////////////////////
                               WIRE FORMAT
    //////////////////////////////////////////////////////////////*/

    /// @dev `qx || qy || abi.encode(r, s, challengeIndex, typeIndex, authenticatorData, clientDataJSON)`:
    ///      the UserOp signature SessionHandler accepts from a passkey.
    function encode(Passkey memory key, WebAuthnNative.WebAuthnAuth memory auth)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(
            key.qx,
            key.qy,
            abi.encode(
                auth.r, auth.s, auth.challengeIndex, auth.typeIndex, auth.authenticatorData, auth.clientDataJSON
            )
        );
    }

    /// @dev The WRONG layout: `abi.encode(auth)` of the struct value puts a 0x20 offset word in front
    ///      of the six values. The contract must refuse it (it never reverts), and a test uses this
    ///      to pin what the server has to send.
    function encodeStructValue(Passkey memory key, WebAuthnNative.WebAuthnAuth memory auth)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(key.qx, key.qy, abi.encode(auth));
    }

    /// @dev The signature of an honest passkey approving `challenge` (a UserOp hash).
    function sign(Passkey memory key, bytes32 challenge) internal pure returns (bytes memory) {
        return encode(key, assertion(key, challenge));
    }

    /*//////////////////////////////////////////////////////////////
                                 UTILITIES
    //////////////////////////////////////////////////////////////*/

    /// @dev Index of the first occurrence of `needle` in `haystack`, or `haystack.length` when it does
    ///      not occur (an index that is out of range on purpose: the contract must reject it).
    function indexOf(bytes memory haystack, bytes memory needle) internal pure returns (uint256) {
        if (needle.length == 0 || haystack.length < needle.length) return haystack.length;
        for (uint256 i = 0; i + needle.length <= haystack.length; i++) {
            bool found = true;
            for (uint256 j = 0; j < needle.length; j++) {
                if (haystack[i + j] != needle[j]) {
                    found = false;
                    break;
                }
            }
            if (found) return i;
        }
        return haystack.length;
    }
}
