//SPDX-License-Identifier:MIT
pragma solidity ^0.8.24;

import {Test, stdError} from "forge-std/Test.sol";
import {PackedUserOperation} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "@account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {PackedUserOperation as OZPackedUserOperation} from "@openzeppelin/contracts/interfaces/draft-IERC4337.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {SessionHandler} from "../../src/SessionHandler.sol";
import {SHFactory} from "../../src/SHFactory.sol";
import {SHRegistry} from "../../src/SHRegistry.sol";
import {SHOracle} from "../../src/SHOracle.sol";
import {SpendingLimitModule} from "../../src/SpendingLimitModule.sol";
import {ERC20Mock} from "../../src/mocks/ERC20Mock.sol";
import {WebAuthnNative} from "../../src/utils/WebAuthnNative.sol";
import {MockV3Aggregator} from "../../src/mocks/MockV3Aggregator.sol";
import {HelperConfig} from "../../script/HelperConfig.s.sol";
import {DeploySHProtocol} from "../../script/DeploySHProtocol.s.sol";
import {SendPackedUserOp} from "../../script/SendPackedUserOp.s.sol";
import {PasskeyHelper} from "../utils/PasskeyHelper.sol";
import {SHTreasury} from "../../src/SHTreasury.sol";

/// @dev Calls the library the way the account does, so a test can see what it does on its own.
contract WebAuthnNativeHarness {
    function verify(bytes32 challenge, WebAuthnNative.WebAuthnAuth memory auth, bytes32 qx, bytes32 qy, bool requireUV)
        external
        view
        returns (bool)
    {
        return WebAuthnNative.verify(abi.encodePacked(challenge), auth, qx, qy, requireUV);
    }

    function tryDecode(bytes calldata blob) external pure returns (bool ok) {
        (ok,) = WebAuthnNative.tryDecodeAuth(blob);
    }
}

/**
 * @title PasskeyValidationTest
 * @notice How SessionHandler validates a UserOp signed by a passkey: what it accepts, and the many
 *         ways a signature can be wrong, every one of which must come back as SIG_VALIDATION_FAILED
 *         and NONE of which may revert.
 * @dev Two ways in. Calling `validateUserOp` as the EntryPoint shows the validation data itself (1 =
 *      failed, `validUntil << 160` = accepted until then). Going through the real `EntryPoint.handleOps`
 *      shows what a user would see (`AA22 expired`, `AA24 signature error`).
 *
 *      Why "never reverts" gets this much attention: a revert in validation fails the WHOLE bundle
 *      (FailedOp), where a plain failure only drops that op. The signature is attacker-controlled
 *      input, so every shape of garbage has to end in a return value.
 */
contract PasskeyValidationTest is Test {
    using PasskeyHelper for PasskeyHelper.Passkey;

    uint256 constant FAILED = 1; // ERC4337Utils.SIG_VALIDATION_FAILED
    int256 constant DAILY_LIMIT = 5000e18;
    uint256 constant WINDOW = 1 days;
    uint256 constant ANVIL_OWNER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    /// @dev How long the app gives an op: its confirmation link's life plus a few minutes.
    uint256 constant OP_TTL = 10 minutes;

    SessionHandler wallet;
    SHOracle oracle;
    SHRegistry registry;
    SHFactory factory;
    SHTreasury treasury;
    SpendingLimitModule module;
    HelperConfig.NetworkConfig config;
    SendPackedUserOp sendPackedUserOp;
    WebAuthnNativeHarness harness;
    ERC20Mock usdc;

    address owner;
    address kani = makeAddr("kani");
    address bundler = makeAddr("bundler");

    PasskeyHelper.Passkey alice; // the wallet's authorized passkey
    PasskeyHelper.Passkey bob; // a passkey the wallet has never heard of
    uint48 deadline;

    function setUp() public {
        DeploySHProtocol deployer = new DeploySHProtocol();
        (factory, treasury, config, oracle) = deployer.run();
        registry = factory.REGISTRY();
        module = SpendingLimitModule(registry.spendingLimitModule());
        owner = config.account;
        usdc = ERC20Mock(config.usdc);

        alice = PasskeyHelper.create("alice's phone");
        bob = PasskeyHelper.create("bob's laptop");
        deadline = uint48(block.timestamp + 30 days);

        address[] memory watched = new address[](1);
        watched[0] = address(usdc);
        vm.prank(owner);
        wallet = SessionHandler(
            payable(factory.deployWallet(
                    DAILY_LIMIT, WINDOW, watched, alice.qx, alice.qy, deadline, new address[](0)
                ))
        );

        sendPackedUserOp = new SendPackedUserOp();
        harness = new WebAuthnNativeHarness();

        vm.deal(address(wallet), 10 ether);
        usdc.mint(address(wallet), 10_000e6);
    }

    /*//////////////////////////////////////////////////////////////
                                HELPERS
    //////////////////////////////////////////////////////////////*/

    /// @dev A transfer op from the wallet, unsigned, with the hash a signer has to approve. Its own
    ///      deadline is later than any key's, so the key's deadline is the window.
    function _transferOp() internal view returns (PackedUserOperation memory op, bytes32 hash) {
        return _transferOpUntil(PasskeyHelper.LATEST_OP_DEADLINE);
    }

    /// @dev The same op with `opDeadline` as its own deadline (in its nonce key).
    function _transferOpUntil(uint48 opDeadline) internal view returns (PackedUserOperation memory op, bytes32 hash) {
        return _transferOpOnKey(PasskeyHelper.nonceKey(opDeadline, 0));
    }

    /// @dev The same op on a nonce key of the test's choosing.
    function _transferOpOnKey(uint192 nonceKey) internal view returns (PackedUserOperation memory op, bytes32 hash) {
        return sendPackedUserOp.generateUnsignedUserOp(
            address(wallet), config, address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)), nonceKey
        );
    }

    /// @dev The account-abstraction and OpenZeppelin PackedUserOperation structs are field-identical
    ///      but distinct Solidity types; SessionHandler's validateUserOp takes OZ's.
    function _toOz(PackedUserOperation memory op) internal pure returns (OZPackedUserOperation memory) {
        return OZPackedUserOperation({
            sender: op.sender,
            nonce: op.nonce,
            initCode: op.initCode,
            callData: op.callData,
            accountGasLimits: op.accountGasLimits,
            preVerificationGas: op.preVerificationGas,
            gasFees: op.gasFees,
            paymasterAndData: op.paymasterAndData,
            signature: op.signature
        });
    }

    /// @dev Validates `op` the way the EntryPoint does and returns the validation data. A revert here
    ///      fails the calling test: that is the property under test.
    function _validate(PackedUserOperation memory op, bytes32 hash) internal returns (uint256) {
        vm.prank(config.entryPoint);
        return wallet.validateUserOp(_toOz(op), hash, 0);
    }

    /// @dev The op signed by `key` with an honest assertion, validated.
    function _validateSignedBy(PasskeyHelper.Passkey memory key) internal returns (uint256) {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = key.sign(hash);
        return _validate(op, hash);
    }

    /// @dev The ids of the keys the wallet lists.
    function _listed() internal view returns (bytes32[] memory ids) {
        (ids,) = wallet.getSessions();
    }

    /// @dev What a valid key's validation data must be: success, bounded by its deadline.
    function _acceptedUntil(uint48 validUntil) internal pure returns (uint256) {
        return uint256(validUntil) << 160;
    }

    /// @dev Warps to `to` and re-stamps the mock feeds. A warp far enough to move a key's deadline is
    ///      also far past every feed's heartbeat, and a stale feed reverts the op inside the hook --
    ///      which the EntryPoint absorbs, so the test would fail for the wrong reason.
    function _warpTo(uint256 to) internal {
        vm.warp(to);
        MockV3Aggregator(config.usdcUsdPriceFeed).updateAnswer(MockV3Aggregator(config.usdcUsdPriceFeed).latestAnswer());
        MockV3Aggregator(config.ethUsdPriceFeed).updateAnswer(MockV3Aggregator(config.ethUsdPriceFeed).latestAnswer());
    }

    function _handleOps(PackedUserOperation memory op) internal {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        // Two-arg prank: EntryPoint v0.9's nonReentrant requires tx.origin == msg.sender (EOA bundler).
        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /*//////////////////////////////////////////////////////////////
                              WHAT IS ACCEPTED
    //////////////////////////////////////////////////////////////*/

    /// @notice An honest assertion by an authorized passkey is accepted, and its deadline is handed to
    ///         the EntryPoint as the op's validity window (that is how an expired key fails AA22).
    function test_validPasskeyOp_isAcceptedUntilItsDeadline() public {
        assertEq(_validateSignedBy(alice), _acceptedUntil(deadline));
    }

    /// @notice End to end through the real EntryPoint: the transfer runs and is metered.
    function test_validPasskeyOp_executesThroughTheEntryPoint() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.sign(hash);

        _handleOps(op);

        assertEq(usdc.balanceOf(kani), 1e6, "the passkey's transfer did not execute");
        assertGt(wallet.getConfig().spentInWindow, 0, "and it was not metered");
    }

    /// @notice A passkey is charged to the same gas budget as any key the owner did not sign for.
    function test_validPasskeyOp_isChargedToTheSessionGasBudget() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.sign(hash);

        vm.prank(config.entryPoint);
        wallet.validateUserOp(_toOz(op), hash, 0.01 ether);

        assertEq(wallet.sessionGasSpent(), 0.01 ether);
    }

    /// @notice A synced passkey (iCloud Keychain, Google Password Manager) reports backup eligible AND
    ///         backed up on top of present and verified: flags 0x1d. Such a key must work, or half of
    ///         the users could never sign.
    function test_syncedPasskeyFlags_areAccepted() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.encode(alice.assertion(hash, PasskeyHelper.UP_UV | PasskeyHelper.BE | PasskeyHelper.BS));
        assertEq(_validate(op, hash), _acceptedUntil(deadline));

        // Eligible but not (yet) backed up is fine too.
        op.signature = alice.encode(alice.assertion(hash, PasskeyHelper.UP_UV | PasskeyHelper.BE));
        assertEq(_validate(op, hash), _acceptedUntil(deadline));
    }

    /// @notice Authenticators differ in the signature counter (Apple always reports 0, others count
    ///         up) and it is not checked on chain: the EntryPoint nonce already stops a replay.
    function test_signatureCounter_isNotChecked() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory authData = PasskeyHelper.authenticatorData(PasskeyHelper.RP_ID, PasskeyHelper.UP_UV, 41_337);
        string memory json = PasskeyHelper.clientDataJSON("webauthn.get", hash, PasskeyHelper.ORIGIN);
        op.signature = alice.encode(alice.assertionOver(authData, json));

        assertEq(_validate(op, hash), _acceptedUntil(deadline));
    }

    /// @notice Chrome ends its clientDataJSON with a field no verifier may choke on.
    function test_chromeStyleClientDataJSON_isAccepted() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory authData = PasskeyHelper.authenticatorData(PasskeyHelper.RP_ID, PasskeyHelper.UP_UV, 0);
        op.signature = alice.encode(alice.assertionOver(authData, PasskeyHelper.clientDataJSONChrome(hash, PasskeyHelper.ORIGIN)));

        assertEq(_validate(op, hash), _acceptedUntil(deadline));
    }

    /// @notice The relying-party id and the origin are NOT checked on chain (a contract cannot know
    ///         which site the user meant; the browser enforces it and the server checks it). What binds
    ///         the signature to this op is the key, user verification and the challenge.
    function test_originAndRpId_areNotCheckedOnChain() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory authData = PasskeyHelper.authenticatorData("mitfah.com", PasskeyHelper.UP_UV, 0);
        string memory json = PasskeyHelper.clientDataJSON("webauthn.get", hash, "https://mitfah.com");
        op.signature = alice.encode(alice.assertionOver(authData, json));

        assertEq(_validate(op, hash), _acceptedUntil(deadline));
    }

    /// @notice The key's last second still validates; the EntryPoint (not this test) refuses the one
    ///         after it. Mirrors the owner-visible rule in {SessionHandler-isSessionActive}.
    function test_passkeyOp_worksInTheDeadlineSecond() public {
        _warpTo(deadline);
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.sign(hash);

        _handleOps(op);

        assertEq(usdc.balanceOf(kani), 1e6, "op rejected in the deadline second");
    }

    /*//////////////////////////////////////////////////////////////
          WHAT IS REFUSED (each returns FAILED; none may revert)
    //////////////////////////////////////////////////////////////*/

    /// @notice A key the wallet never authorized: a perfectly good WebAuthn signature, by the wrong
    ///         device. Refused after ONE storage read.
    function test_unknownKey_fails() public {
        assertEq(_validateSignedBy(bob), FAILED);
    }

    /// @notice A revoked key stops working at once.
    function test_removedKey_fails() public {
        assertEq(_validateSignedBy(alice), _acceptedUntil(deadline), "control: works before removal");

        vm.prank(owner);
        wallet.removeSession(alice.keyId());

        assertEq(_validateSignedBy(alice), FAILED);
    }

    /// @notice An authorized key's expired-but-still-listed state is the EntryPoint's call (AA22), but
    ///         the account itself still answers with the deadline so the EntryPoint can decide.
    function test_expiredKey_isStillAnsweredWithItsDeadline() public {
        vm.warp(uint256(deadline) + 1);
        assertEq(_validateSignedBy(alice), _acceptedUntil(deadline));
    }

    /// @notice Changing the JSON after it was signed breaks the signature (the hash of it is signed).
    function test_tamperedClientDataJSON_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        WebAuthnNative.WebAuthnAuth memory auth = alice.assertion(hash);
        auth.clientDataJSON = string.concat(auth.clientDataJSON, " "); // one byte more than was signed
        op.signature = alice.encode(auth);
        assertEq(_validate(op, hash), FAILED);

        // Same for a changed origin, with the challenge intact.
        auth = alice.assertion(hash);
        auth.clientDataJSON = PasskeyHelper.clientDataJSON("webauthn.get", hash, "http://evil.example");
        op.signature = alice.encode(auth);
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice Changing the challenge INSIDE the JSON fails the challenge check even before the
    ///         signature is looked at.
    function test_tamperedChallengeInTheJSON_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        WebAuthnNative.WebAuthnAuth memory auth = alice.assertion(hash);
        auth.clientDataJSON = PasskeyHelper.clientDataJSON("webauthn.get", bytes32(uint256(hash) ^ 1), PasskeyHelper.ORIGIN);
        op.signature = alice.encode(auth);
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice THE REPLAY: a real signature by an authorized key, over a DIFFERENT op. The challenge is
    ///         the op hash, so approving one op never approves another.
    function test_wrongChallenge_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        (, bytes32 otherHash) = sendPackedUserOp.generateUnsignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 2e6)),
            PasskeyHelper.nonceKey(PasskeyHelper.LATEST_OP_DEADLINE, 0)
        );
        assertTrue(hash != otherHash);

        op.signature = alice.sign(otherHash);
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice The same signature on the same op after the op's nonce moved on is a different op hash,
    ///         so a captured approval cannot be replayed at a later nonce either.
    function test_signatureDoesNotCarryToAnotherNonce() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory signature = alice.sign(hash);

        (PackedUserOperation memory sameCallDifferentNonce, bytes32 hashOfThat) =
            _transferOpOnKey(PasskeyHelper.nonceKey(PasskeyHelper.LATEST_OP_DEADLINE, 1));
        assertTrue(hash != hashOfThat);
        sameCallDifferentNonce.signature = signature;
        assertEq(_validate(sameCallDifferentNonce, hashOfThat), FAILED);
        op; // silence
    }

    /// @notice User verification is REQUIRED. A touch alone (flags UP) is a cryptographically valid
    ///         signature and must still be refused.
    function test_userVerificationOff_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.encode(alice.assertion(hash, PasskeyHelper.UP));
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice And a verified user who was not present (UV without UP) is refused as well.
    function test_userPresenceOff_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.encode(alice.assertion(hash, PasskeyHelper.UV));
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice "Backed up" without "backup eligible" cannot happen on a real authenticator.
    function test_backupStateWithoutEligibility_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.encode(alice.assertion(hash, PasskeyHelper.UP_UV | PasskeyHelper.BS));
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice The malleable twin (s above N/2) is refused even though it verifies mathematically;
    ///         the same assertion with the low s is accepted.
    function test_highS_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        WebAuthnNative.WebAuthnAuth memory auth = alice.assertion(hash);

        op.signature = alice.encode(auth);
        assertEq(_validate(op, hash), _acceptedUntil(deadline), "control: the low s is accepted");

        op.signature = alice.encode(PasskeyHelper.withHighS(auth));
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice An assertion for a registration ("webauthn.create") is not an authentication.
    function test_wrongClientDataType_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory authData = PasskeyHelper.authenticatorData(PasskeyHelper.RP_ID, PasskeyHelper.UP_UV, 0);
        string memory json = PasskeyHelper.clientDataJSON("webauthn.create", hash, PasskeyHelper.ORIGIN);
        op.signature = alice.encode(alice.assertionOver(authData, json));
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice Naming alice's public key but signing with bob's private key proves nothing: the
    ///         signature is checked against the key the signature NAMES, and that key is alice's.
    function test_signingWithAnotherKeyUnderAHeldKeyId_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        PasskeyHelper.Passkey memory impostor =
            PasskeyHelper.Passkey({privateKey: bob.privateKey, qx: alice.qx, qy: alice.qy});
        op.signature = impostor.sign(hash);
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice A 65-byte ECDSA signature by anyone but the owner is nobody. A session key used to be
    ///         an Ethereum address; it is not any more.
    function test_ecdsaSignatureByANonOwner_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        (, uint256 strangerPk) = makeAddrAndKey("stranger");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(strangerPk, MessageHashUtils.toEthSignedMessageHash(hash));
        op.signature = abi.encodePacked(r, s, v);
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice The wire format is six values in a row. `abi.encode(auth)` of the struct value has an
    ///         extra offset word in front, and must be refused (not revert) so a server that sends it
    ///         finds out in its first test.
    function test_structValueEncoding_isRefused() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        WebAuthnNative.WebAuthnAuth memory auth = alice.assertion(hash);

        op.signature = alice.encode(auth);
        assertEq(_validate(op, hash), _acceptedUntil(deadline), "control: the flat layout is accepted");

        op.signature = alice.encodeStructValue(auth);
        assertEq(op.signature.length, alice.encode(auth).length + 32, "the struct value adds one word");
        assertEq(_validate(op, hash), FAILED);
    }

    /// @notice Every shape of short or garbage signature ends in FAILED, not in a revert.
    function test_garbageSignatures_fail() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory good = alice.sign(hash);

        bytes[] memory samples = new bytes[](12);
        samples[0] = ""; //                                            nothing at all
        samples[1] = hex"01"; //                                       one byte
        samples[2] = new bytes(63); //                                 shorter than a public key
        samples[3] = new bytes(64); //                                 EXACTLY 64 bytes: a compact ECDSA signature's length
        samples[4] = abi.encodePacked(alice.qx, alice.qy); //          a held key and nothing else
        samples[5] = new bytes(65); //                                 65 zero bytes: the owner path, no signer
        samples[6] = new bytes(255); //                                one byte short of the smallest possible passkey signature
        samples[7] = new bytes(256); //                                the smallest size, all zero: key (0,0) is nobody
        samples[8] = abi.encodePacked(alice.qx, alice.qy, new bytes(192)); // a held key, an all-zero blob
        samples[9] = abi.encodePacked(alice.qx, alice.qy, new bytes(1000)); // ... a long one
        samples[10] = _truncate(good, good.length / 2); //             a good signature cut off in the middle
        samples[11] = _filled(0xff, 700); //                           noise

        for (uint256 i = 0; i < samples.length; i++) {
            op.signature = samples[i];
            assertEq(_validate(op, hash), FAILED, string.concat("sample ", vm.toString(i)));
        }
    }

    /// @notice A 64-byte ECDSA signature in EIP-2098 compact form, signed by the OWNER, is not an
    ///         owner signature here: the owner has exactly one encoding, 65 bytes. It is judged as a
    ///         passkey signature and fails.
    function test_compactOwnerSignature_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(ANVIL_OWNER_KEY, MessageHashUtils.toEthSignedMessageHash(hash));
        bytes32 vs = bytes32((uint256(v - 27) << 255) | uint256(s));

        op.signature = abi.encodePacked(r, s, v);
        assertEq(_validate(op, hash), 0, "control: the 65-byte form is the owner");

        op.signature = abi.encodePacked(r, vs);
        assertEq(op.signature.length, 64);
        assertEq(_validate(op, hash), FAILED);
    }

    /*//////////////////////////////////////////////////////////////
               INDICES IN THE ASSERTION (the revert hazards)
    //////////////////////////////////////////////////////////////*/

    /// @notice WHY the account bounds the two indices itself. OpenZeppelin's verifier adds
    ///         `challengeIndex` to a length with checked arithmetic, so an index near 2^256 panics. In
    ///         validation that would fail the whole bundle instead of dropping one op.
    function test_openZeppelinVerify_panicsOnAHugeChallengeIndex() public {
        (, bytes32 hash) = _transferOp();
        WebAuthnNative.WebAuthnAuth memory auth = alice.assertion(hash);
        auth.challengeIndex = type(uint256).max;

        vm.expectRevert(stdError.arithmeticError);
        harness.verify(hash, auth, alice.qx, alice.qy, true);
    }

    /// @notice ...and the account returns FAILED for the same assertion instead.
    function test_hugeIndices_failInsteadOfReverting() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        WebAuthnNative.WebAuthnAuth memory honest = alice.assertion(hash);
        uint256 length = bytes(honest.clientDataJSON).length;

        uint256[8] memory indices = [
            type(uint256).max,
            type(uint256).max - 1,
            type(uint256).max - 20,
            uint256(1) << 128,
            uint256(1) << 64,
            uint256(1) << 32,
            length,
            length + 1
        ];
        for (uint256 i = 0; i < indices.length; i++) {
            WebAuthnNative.WebAuthnAuth memory auth = alice.assertion(hash);
            auth.challengeIndex = indices[i];
            op.signature = alice.encode(auth);
            assertEq(_validate(op, hash), FAILED, string.concat("challengeIndex ", vm.toString(i)));

            auth = alice.assertion(hash);
            auth.typeIndex = indices[i];
            op.signature = alice.encode(auth);
            assertEq(_validate(op, hash), FAILED, string.concat("typeIndex ", vm.toString(i)));
        }

        // An in-range index that points at the wrong place fails the ordinary way.
        WebAuthnNative.WebAuthnAuth memory shifted = alice.assertion(hash);
        shifted.typeIndex += 1;
        op.signature = alice.encode(shifted);
        assertEq(_validate(op, hash), FAILED);
    }

    /// forge-config: default.fuzz.runs = 512
    /// @notice Any pair of indices: FAILED, or accepted when they happen to be the right ones.
    ///         Never a revert, never an out-of-gas.
    function testFuzz_anyIndices_neverRevert(uint256 challengeIndex, uint256 typeIndex) public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        WebAuthnNative.WebAuthnAuth memory auth = alice.assertion(hash);
        auth.challengeIndex = challengeIndex;
        auth.typeIndex = typeIndex;
        op.signature = alice.encode(auth);

        uint256 result = _validate(op, hash);
        assertTrue(result == FAILED || result == _acceptedUntil(deadline));
    }

    /// forge-config: default.fuzz.runs = 512
    /// @notice Arbitrary bytes, of any length, with and without a held key's coordinates in front.
    function testFuzz_garbageNeverReverts(bytes calldata noise, bool namesAHeldKey) public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = namesAHeldKey ? abi.encodePacked(alice.qx, alice.qy, noise) : noise;

        uint256 result = _validate(op, hash);
        assertTrue(result == FAILED || result == 0, "only the owner path may succeed, and not with noise");
    }

    /// forge-config: default.fuzz.runs = 512
    /// @notice A good signature with one byte changed: FAILED, or still accepted when the byte was
    ///         one the check does not read (ABI padding). Never a revert.
    function testFuzz_oneByteChanged_neverReverts(uint256 position, uint8 change) public {
        vm.assume(change != 0);
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory signature = alice.sign(hash);
        signature[position % signature.length] ^= bytes1(change);
        op.signature = signature;

        uint256 result = _validate(op, hash);
        assertTrue(result == FAILED || result == _acceptedUntil(deadline));
    }

    /// forge-config: default.fuzz.runs = 512
    /// @notice A good signature cut anywhere: FAILED, or accepted when only ABI padding was cut.
    function testFuzz_truncated_neverReverts(uint256 cut) public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        bytes memory signature = alice.sign(hash);
        op.signature = _truncate(signature, cut % (signature.length + 1));

        uint256 result = _validate(op, hash);
        assertTrue(result == FAILED || result == _acceptedUntil(deadline));
    }

    /*//////////////////////////////////////////////////////////////
               THE OP'S OWN DEADLINE (top 48 bits of the nonce)
    //////////////////////////////////////////////////////////////*/

    /// @notice The app's normal case: the op's deadline is minutes away and the key's is weeks away,
    ///         so the op's own deadline is the window the EntryPoint enforces.
    function test_opDeadline_shorterThanTheKey_isTheWindow() public {
        uint48 opDeadline = uint48(block.timestamp + OP_TTL);
        (PackedUserOperation memory op, bytes32 hash) = _transferOpUntil(opDeadline);
        op.signature = alice.sign(hash);
        assertEq(_validate(op, hash), _acceptedUntil(opDeadline));
    }

    /// @notice An op deadline past the key's cannot stretch the key: the key's deadline still ends it.
    function test_opDeadline_cannotOutliveTheKey() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOpUntil(deadline + 1);
        op.signature = alice.sign(hash);
        assertEq(_validate(op, hash), _acceptedUntil(deadline));
    }

    /// @notice A passkey op with NO deadline -- nonce key 0, or any lane under a zero deadline -- is
    ///         refused however good its signature: zero must never reach the EntryPoint, which reads
    ///         it as "valid forever". Through the EntryPoint that is AA24, like any refusal.
    function test_opDeadline_zero_fails() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOpOnKey(0);
        op.signature = alice.sign(hash);
        assertEq(_validate(op, hash), FAILED, "nonce key 0");

        (op, hash) = _transferOpOnKey(PasskeyHelper.nonceKey(0, type(uint144).max));
        op.signature = alice.sign(hash);
        assertEq(_validate(op, hash), FAILED, "a lane with no deadline");

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /// @notice The deadline is inside the signed op hash, so whoever relays a signed op cannot extend it.
    function test_opDeadline_cannotBeMovedAfterSigning() public {
        uint48 opDeadline = uint48(block.timestamp + OP_TTL);
        (PackedUserOperation memory op, bytes32 hash) = _transferOpUntil(opDeadline);
        op.signature = alice.sign(hash);

        (PackedUserOperation memory later, bytes32 laterHash) = _transferOpUntil(opDeadline + 1 days);
        later.signature = op.signature;
        assertEq(_validate(later, laterHash), FAILED);
    }

    /// @notice WHY IT EXISTS: an op that was approved but never sent dies with its own deadline while
    ///         the key lives on. The EntryPoint says AA22 "expired", as it does for an expired key; the
    ///         app tells the two apart by asking whether the key is still active.
    function test_expiredOp_failsWithAA22_whileTheKeyIsLive() public {
        uint48 opDeadline = uint48(block.timestamp + OP_TTL);
        (PackedUserOperation memory op, bytes32 hash) = _transferOpUntil(opDeadline);
        op.signature = alice.sign(hash);

        _warpTo(uint256(opDeadline) + 1);
        assertTrue(wallet.isSessionActive(alice.keyId()), "the key itself is still live");

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA22 expired or not due"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /// @notice ...and in its deadline second it still goes through.
    function test_opDeadline_worksInItsLastSecond() public {
        uint48 opDeadline = uint48(block.timestamp + OP_TTL);
        (PackedUserOperation memory op, bytes32 hash) = _transferOpUntil(opDeadline);
        op.signature = alice.sign(hash);

        _warpTo(opDeadline);
        _handleOps(op);

        assertEq(usdc.balanceOf(kani), 1e6, "op rejected in its deadline second");
    }

    /// @notice The owner's ops ignore the field: an owner op on a nonce key that reads as a long-past
    ///         deadline still validates, with no window.
    function test_ownerOp_ignoresTheOpDeadline() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOpUntil(1);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(ANVIL_OWNER_KEY, MessageHashUtils.toEthSignedMessageHash(hash));
        op.signature = abi.encodePacked(r, s, v);
        assertEq(_validate(op, hash), 0);
    }

    /// forge-config: default.fuzz.runs = 512
    /// @notice Any op deadline: zero is refused; otherwise the window is the earlier of the op's and
    ///         the key's.
    function testFuzz_opDeadline_windowIsTheEarlierOfTheTwo(uint48 opDeadline) public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOpUntil(opDeadline);
        op.signature = alice.sign(hash);

        uint256 expected = opDeadline == 0 ? FAILED : _acceptedUntil(opDeadline < deadline ? opDeadline : deadline);
        assertEq(_validate(op, hash), expected);
    }

    /*//////////////////////////////////////////////////////////////
                      THROUGH THE REAL ENTRYPOINT
    //////////////////////////////////////////////////////////////*/

    /// @notice Past its deadline a passkey is refused by the EntryPoint with AA22 "expired", NOT AA24:
    ///         the signature was fine, the grant ran out, and whoever reads the error can tell which.
    function test_expiredKey_failsWithAA22() public {
        vm.warp(uint256(deadline) + 1);
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = alice.sign(hash);

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA22 expired or not due"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /// @notice An unknown key, a removed key and a bad signature are all AA24 "signature error".
    function test_refusedSignatures_failWithAA24() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = bob.sign(hash);

        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));

        // A signature that is not even the right shape fails the same way, not with "AA23 reverted".
        op.signature = hex"deadbeef";
        ops[0] = op;
        vm.prank(bundler, bundler);
        vm.expectRevert(abi.encodeWithSelector(IEntryPoint.FailedOp.selector, 0, "AA24 signature error"));
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
    }

    /// @notice Two passkey ops with different nonce keys go through ONE bundle, in either order: a
    ///         second confirmation never waits behind the first. (The app draws a fresh random lane
    ///         for every passkey op; nothing here may assume sequential nonces.)
    function test_twoPasskeyOps_withDifferentNonceKeys_shareABundle() public {
        PackedUserOperation[] memory ops = new PackedUserOperation[](2);
        for (uint144 i = 0; i < 2; i++) {
            // The higher lane first, so the bundle is not in nonce-key order either.
            (PackedUserOperation memory op, bytes32 hash) = sendPackedUserOp.generateUnsignedUserOp(
                address(wallet),
                config,
                address(usdc),
                0,
                abi.encodeCall(ERC20Mock.transfer, (kani, (i + 1) * 1e6)),
                PasskeyHelper.nonceKey(uint48(block.timestamp + OP_TTL), 0xBEEF - i * 0x1000)
            );
            assertEq(op.nonce & type(uint64).max, 0, "each key starts at sequence 0");
            op.signature = alice.sign(hash);
            ops[i] = op;
        }

        vm.prank(bundler, bundler);
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));

        assertEq(usdc.balanceOf(kani), 3e6, "both ops executed");
    }

    /// @notice The owner is unchanged by all of this: 65-byte ECDSA over the EIP-191 envelope, no
    ///         deadline, works with no passkey at all.
    function test_ownerEcdsaPath_isUnchanged() public {
        vm.prank(owner);
        wallet.removeAllSessions();

        (PackedUserOperation memory op, bytes32 hash,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet),
            config,
            address(usdc),
            0,
            abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)),
            owner,
            ANVIL_OWNER_KEY
        );
        assertEq(op.signature.length, 65);
        assertEq(_validate(op, hash), 0, "owner: success, no validity window");

        vm.warp(block.timestamp + 365 days);
        assertEq(_validate(op, hash), 0, "owner has no deadline");
    }

    /*//////////////////////////////////////////////////////////////
          THE BUNDLER'S TRICK: ONE STORAGE OVERRIDE PLANTS A KEY
    //////////////////////////////////////////////////////////////*/

    /// @notice app/bundler.py simulates a quote for a user who has not signed yet by planting a
    ///         throwaway key's deadline with a single state override. This is that override, by slot
    ///         NUMBER: `sessionValidUntil` sits on slot 9, so a key's slot is
    ///         keccak256(abi.encode(keyId, uint256(9))). If this test breaks, the bundler's constant
    ///         has to move with it.
    function test_bundlerOverride_oneSlotPlantsAThrowawayKey() public {
        PasskeyHelper.Passkey memory throwaway = PasskeyHelper.create("throwaway quote key");
        assertEq(_validateSignedBy(throwaway), FAILED, "control: unknown before the override");
        assertEq(_listed().length, 1, "only alice is listed");

        bytes32 slot = keccak256(abi.encode(throwaway.keyId(), uint256(9)));
        uint48 farFuture = uint48(block.timestamp + 365 days);
        vm.store(address(wallet), slot, bytes32(uint256(farFuture)));

        assertEq(_validateSignedBy(throwaway), _acceptedUntil(farFuture), "the override alone authorizes the key");
        assertTrue(wallet.isSessionActive(throwaway.keyId()));
        assertEq(uint256(vm.load(address(wallet), slot)), farFuture);
    }

    /// @notice The slot a real grant writes is the same slot the override targets.
    function test_slot9_isSessionValidUntil() public view {
        bytes32 slot = keccak256(abi.encode(alice.keyId(), uint256(9)));
        assertEq(uint256(vm.load(address(wallet), slot)), deadline);
    }

    /*//////////////////////////////////////////////////////////////
               KNOWN ANSWER: A VECTOR FROM OUTSIDE FOUNDRY
    //////////////////////////////////////////////////////////////*/

    // Produced by an independent implementation (Python: cryptography + eth_abi, the way a browser's
    // authenticator and the server's py_webauthn path produce it), NOT by PasskeyHelper:
    //   - a Chrome-shaped clientDataJSON, with its trailing "other_keys_can_be_added_here" field
    //   - authenticatorData flags 0x1d (UP | UV | BE | BS, a synced platform passkey), counter 7
    //   - r, s from a DER signature, s folded to the low half
    // If this test and the helper-built ones disagree, the helper (or the contract) is wrong.
    bytes32 constant KAT_QX = 0xf9c7da81c978f2730be0893e293ef4c4e02861037758f8fb29ce061e5f85664e;
    bytes32 constant KAT_QY = 0x81aa9daf01329b2fcd2b7dad17b586bac2a99bce4346c18de286f302b5f2135d;
    bytes32 constant KAT_CHALLENGE = 0x86df308b28fa427997fe2cb041c34b196f21d57cff0ae8cec4a673a1082db0c5;
    bytes constant KAT_BLOB =
        hex"f539871422d34068c22d1b0d6dbe8aae40376c7ca701e55757cdb1baeb3926f2430af1697e19e5266f187f74a29791da8cc249cac99bb1f336832da0631c85b00000000000000000000000000000000000000000000000000000000000000017000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000c00000000000000000000000000000000000000000000000000000000000000120000000000000000000000000000000000000000000000000000000000000002549960de5880e8c687434170f6476605b8fe4aeb9a28632c7995cf3ba831d97631d0000000700000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000f37b2274797065223a22776562617574686e2e676574222c226368616c6c656e6765223a226874387769796a36516e6d585f69797751634e4c4757386831587a5f43756a4f784b5a7a6f516774734d55222c226f726967696e223a22687474703a2f2f6c6f63616c686f73743a33303030222c2263726f73734f726967696e223a66616c73652c226f746865725f6b6579735f63616e5f62655f61646465645f68657265223a22646f206e6f7420636f6d7061726520636c69656e74446174614a534f4e20616761696e737420612074656d706c6174652e205365652068747470733a2f2f676f6f2e676c2f796162506578227d00000000000000000000000000";

    /// @notice The outside vector decodes and verifies in the library on its own.
    function test_knownAnswer_libraryVerifiesTheOutsideVector() public view {
        assertTrue(harness.tryDecode(KAT_BLOB), "the flat ABI layout decodes");

        WebAuthnNative.WebAuthnAuth memory auth = abi.decode(
            abi.encodePacked(uint256(0x20), KAT_BLOB), // decode the struct, which needs its offset word
            (WebAuthnNative.WebAuthnAuth)
        );
        assertEq(auth.challengeIndex, 23);
        assertEq(auth.typeIndex, 1);
        assertEq(auth.authenticatorData.length, 37);
        assertTrue(harness.verify(KAT_CHALLENGE, auth, KAT_QX, KAT_QY, true));
        // ...for that challenge and no other.
        assertFalse(harness.verify(bytes32(uint256(KAT_CHALLENGE) + 1), auth, KAT_QX, KAT_QY, true));
    }

    /// @notice ...and through the account: the same bytes, as a UserOp signature, with the userOpHash
    ///         standing in for the challenge (validateUserOp takes the hash from its caller).
    function test_knownAnswer_accountAcceptsTheOutsideVector() public {
        vm.prank(owner);
        wallet.addSession(KAT_QX, KAT_QY, deadline);

        (PackedUserOperation memory op,) = _transferOp();
        op.signature = abi.encodePacked(KAT_QX, KAT_QY, KAT_BLOB);

        assertEq(_validate(op, KAT_CHALLENGE), _acceptedUntil(deadline));
        assertEq(_validate(op, bytes32(uint256(KAT_CHALLENGE) + 1)), FAILED, "bound to its challenge");
    }

    /*//////////////////////////////////////////////////////////////
                       GAS: PASSKEY VS OWNER VALIDATION
    //////////////////////////////////////////////////////////////*/

    // Each measurement is its own test, hence its own transaction, so no run is helped by another's
    // warm storage. The numbers are logged (run with -vv); the bounds are loose guards that
    // fail only if validation gets several times dearer, which the bundler's gas limits would feel.

    /// @dev Gas used by one `validateUserOp` call on the pranked EntryPoint, nothing else included.
    function _validationGas(PackedUserOperation memory op, bytes32 hash, uint256 prefund)
        internal
        returns (uint256 used)
    {
        OZPackedUserOperation memory ozOp = _toOz(op);
        vm.prank(config.entryPoint);
        uint256 before = gasleft();
        wallet.validateUserOp(ozOp, hash, prefund);
        used = before - gasleft();
    }

    function _ownerOp() internal view returns (PackedUserOperation memory op, bytes32 hash) {
        (op, hash,) = sendPackedUserOp.generateSignedUserOp(
            address(wallet), config, address(usdc), 0, abi.encodeCall(ERC20Mock.transfer, (kani, 1e6)), owner, ANVIL_OWNER_KEY
        );
    }

    function _passkeyOpSigned() internal view returns (PackedUserOperation memory op, bytes32 hash) {
        (op, hash) = _transferOp();
        op.signature = alice.sign(hash);
    }

    /// @notice What validating an owner op costs: the signature check alone (no prefund requested).
    function test_gas_ownerValidation() public {
        (PackedUserOperation memory op, bytes32 hash) = _ownerOp();
        uint256 used = _validationGas(op, hash, 0);
        emit log_named_uint("validateUserOp gas, owner (ECDSA), no prefund", used);
        assertLt(used, 60_000);
    }

    /// @notice What validating a passkey op costs: the full WebAuthn check, the P-256 precompile
    ///         included. The bundler's verificationGasLimit has to clear this with room to spare.
    function test_gas_passkeyValidation() public {
        (PackedUserOperation memory op, bytes32 hash) = _passkeyOpSigned();
        uint256 used = _validationGas(op, hash, 0);
        emit log_named_uint("validateUserOp gas, passkey (WebAuthn + P-256), no prefund", used);
        emit log_named_uint("passkey signature bytes", op.signature.length);
        assertLt(used, 150_000);
    }

    /// @notice The same two with a realistic prefund, as the EntryPoint asks when the wallet's deposit
    ///         is low. The passkey path also charges the session gas budget (one storage write) --
    ///         the owner path does not.
    function test_gas_ownerValidation_withPrefund() public {
        (PackedUserOperation memory op, bytes32 hash) = _ownerOp();
        uint256 used = _validationGas(op, hash, 0.01 ether);
        emit log_named_uint("validateUserOp gas, owner (ECDSA), 0.01 ETH prefund", used);
        assertLt(used, 100_000);
    }

    function test_gas_passkeyValidation_withPrefund() public {
        (PackedUserOperation memory op, bytes32 hash) = _passkeyOpSigned();
        uint256 used = _validationGas(op, hash, 0.01 ether);
        emit log_named_uint("validateUserOp gas, passkey, 0.01 ETH prefund (budget charged)", used);
        assertLt(used, 200_000);
    }

    /// @notice The cost of the cheapest refusal (a key the wallet does not hold): one storage read.
    function test_gas_unknownKeyIsRefusedCheaply() public {
        (PackedUserOperation memory op, bytes32 hash) = _transferOp();
        op.signature = bob.sign(hash);
        uint256 used = _validationGas(op, hash, 0);
        emit log_named_uint("validateUserOp gas, unknown passkey (refused), no prefund", used);
        assertLt(used, 60_000);
    }

    /// @dev The whole op, validation to refund, through the real EntryPoint.
    function _handleOpsGas(PackedUserOperation memory op) internal returns (uint256 used) {
        PackedUserOperation[] memory ops = new PackedUserOperation[](1);
        ops[0] = op;
        vm.prank(bundler, bundler);
        uint256 before = gasleft();
        IEntryPoint(config.entryPoint).handleOps(ops, payable(bundler));
        used = before - gasleft();
        assertEq(usdc.balanceOf(kani), 1e6, "the op must really have run");
    }

    function test_gas_wholeOp_owner() public {
        (PackedUserOperation memory op,) = _ownerOp();
        emit log_named_uint("handleOps gas, one transfer, owner", _handleOpsGas(op));
    }

    /// @notice Execution gas only: the passkey signature's extra calldata (about 480 bytes more than
    ///         an ECDSA signature, ~16 gas each) is paid for in the transaction's intrinsic gas, which
    ///         this in-test call does not include.
    function test_gas_wholeOp_passkey() public {
        (PackedUserOperation memory op,) = _passkeyOpSigned();
        emit log_named_uint("handleOps gas, one transfer, passkey", _handleOpsGas(op));
    }

    /*//////////////////////////////////////////////////////////////
                                 UTILITIES
    //////////////////////////////////////////////////////////////*/

    function _truncate(bytes memory data, uint256 length) internal pure returns (bytes memory out) {
        require(length <= data.length, "cut past the end");
        out = new bytes(length);
        for (uint256 i = 0; i < length; i++) {
            out[i] = data[i];
        }
    }

    function _filled(bytes1 value, uint256 length) internal pure returns (bytes memory out) {
        out = new bytes(length);
        for (uint256 i = 0; i < length; i++) {
            out[i] = value;
        }
    }
}
