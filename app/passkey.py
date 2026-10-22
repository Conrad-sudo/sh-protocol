"""
Passkeys as the wallet's session keys: the WebAuthn settings this server checks against, the bytes a
passkey's approval is packed into for the wallet, and a passkey held in software for simulations and
tests.

A session key used to be an Ethereum key this server held (encrypted in Vault) and signed with
whenever the user said yes. It is now a passkey -- WebAuthn, ES256, a P-256 key -- that lives on the
user's own device and signs only after Face ID, a fingerprint or a PIN (user verification, UV).
The server holds no session key at all: it builds the UserOperation, hands its hash to the browser
as the WebAuthn challenge, checks the assertion that comes back, and packs it into the signature
SessionHandler verifies on chain with OpenZeppelin's WebAuthn library.

What the wallet reads, exactly (spec 2):

    signature = qx (32 bytes) || qy (32 bytes) || abi.encode(r, s, challengeIndex, typeIndex,
                                                              authenticatorData, clientDataJSON)

The tail is the six fields of OZ's WebAuthnAuth encoded as six separate values -- the encoding OZ's
own tests hand to WebAuthn.tryDecodeAuth, which reads the struct in place from the first byte. A
Solidity `abi.encode(auth)` of the struct would put a 32-byte offset word in front, and
tryDecodeAuth would then read that word as `r`. `r` and `s` come from the DER signature the
authenticator returns, with `s` folded into the lower half of the curve order: OZ's P256 refuses a
high `s`, and both halves verify the same message. challengeIndex and typeIndex are where
`"challenge":"<base64url>"` and `"type":"webauthn.get"` start in clientDataJSON, which is what OZ's
_validateChallenge and _validateExpectedTypeHash compare at.

Nothing here talks to the chain or the database.
"""
import hashlib
import json
import os
import secrets
from urllib.parse import urlparse

import cbor2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from eth_abi import encode as abi_encode
from eth_utils import keccak
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.cose import COSEAlgorithmIdentifier, COSECRV
from webauthn.helpers.decode_credential_public_key import DecodedEC2PublicKey, decode_credential_public_key
from webauthn.helpers.exceptions import WebAuthnException

# The order of the P-256 group. A valid ECDSA `s` lies in [1, N), and OZ's P256 accepts only the
# lower half of that range.
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_HALF_N = P256_N // 2

# authenticatorData's flag bits (WebAuthn 6.1): user present, user verified, backup eligible,
# backed up, attested credential data included.
FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40

# OZ WebAuthn.WebAuthnAuth, field by field: (r, s, challengeIndex, typeIndex, authenticatorData,
# clientDataJSON). clientDataJSON is a `string` there; `bytes` encodes identically and keeps the
# exact bytes the authenticator signed, with no round trip through a Python str.
_AUTH_TYPES = ["bytes32", "bytes32", "uint256", "uint256", "bytes", "bytes"]

# What OZ's checks look for in clientDataJSON, byte for byte. No space after either colon: a browser
# writes clientDataJSON compactly, and the contract compares bytes, not parsed JSON.
_TYPE_GET = b'"type":"webauthn.get"'
_CHALLENGE_KEY = b'"challenge":"'

# How long the browser is told to wait for the user, in milliseconds (the spec's timeout).
WEBAUTHN_TIMEOUT_MS = 60_000


class PasskeyError(ValueError):
    """A passkey or an approval the wallet can't use, with a sentence the user can be shown."""


# ── The relying party: who the passkeys belong to ────────────────────────────


def rp_id() -> str:
    """
    The WebAuthn relying party id every passkey is made for and checked against: WEBAUTHN_RP_ID, or
    else the host of the first SIWE_DOMAIN entry without its port -- `localhost` in development,
    `mitfah.com` in production. Read on every call, so a test can set it.

    The same site the sign-in names, on purpose: a passkey is bound to its rp id for life, so it has
    to be the domain the web app is served from, and SIWE_DOMAIN already says what that is.
    """
    explicit = os.getenv("WEBAUTHN_RP_ID", "").strip()
    if explicit:
        return explicit.lower()
    first = next((d.strip() for d in os.getenv("SIWE_DOMAIN", "localhost:3000").split(",") if d.strip()), "localhost")
    return first.split(":")[0].lower()


def allowed_origins() -> list[str]:
    """
    The origins an approval may come from: WEBAUTHN_ORIGINS (a comma list), or else WEB_APP_URL and
    CORS_ORIGINS -- the web app's own address. An origin is scheme://host[:port], with no path.

    Checked on every approval because the rp id alone admits any page under that domain, and the
    origin is what the browser itself vouches for in clientDataJSON.
    """
    explicit = os.getenv("WEBAUTHN_ORIGINS", "").strip()
    if explicit:
        candidates = explicit.split(",")
    else:
        candidates = [os.getenv("WEB_APP_URL", ""), *os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")]
    origins = []
    for candidate in candidates:
        parsed = urlparse(candidate.strip())
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}".lower()
            if origin not in origins:
                origins.append(origin)
    return origins or ["http://localhost:3000"]


# ── Keys ──────────────────────────────────────────────────────────────────────


def key_id(qx: bytes, qy: bytes) -> str:
    """
    The id the wallet files a passkey under: keccak256(abi.encode(qx, qy)), as SessionHandler's
    sessionKeyId computes it. abi.encode of two bytes32 is just the 64 bytes side by side.

    @return  0x-prefixed lowercase hex.
    """
    return "0x" + keccak(bytes(qx) + bytes(qy)).hex()


def es256_public_key(cose_key: bytes) -> tuple[bytes, bytes]:
    """
    The (x, y) of a passkey's public key, from the COSE key its registration carried -- refusing
    anything the wallet couldn't verify.

    ES256 only (P-256 with SHA-256): it is the one curve with a precompile on every chain the wallet
    runs on, and the one OZ's WebAuthn verifies. The point is checked to be on the curve here,
    because SessionHandler refuses an invalid one too (SessionHandler_InvalidSessionKey) -- better
    to say so at registration than after the owner has paid for a grant that reverts.

    @param cose_key  The credential's public key, COSE-encoded (CBOR).
    @return          (x, y), 32 bytes each, big-endian.
    @raises PasskeyError  If it is not a valid ES256 P-256 key.
    """
    try:
        decoded = decode_credential_public_key(cose_key)
    except (WebAuthnException, ValueError, KeyError, TypeError, IndexError) as exc:
        raise PasskeyError("That passkey's public key couldn't be read.") from exc
    if (
        not isinstance(decoded, DecodedEC2PublicKey)
        or decoded.alg != COSEAlgorithmIdentifier.ECDSA_SHA_256
        or decoded.crv != COSECRV.P256
    ):
        raise PasskeyError("This wallet only accepts ES256 (P-256) passkeys. Try another device or browser.")
    x, y = bytes(decoded.x), bytes(decoded.y)
    if len(x) != 32 or len(y) != 32:
        raise PasskeyError("That passkey's public key isn't a P-256 key.")
    try:
        ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
    except ValueError as exc:
        raise PasskeyError("That passkey's public key isn't a valid P-256 point.") from exc
    return x, y


# ── The signature the wallet verifies ─────────────────────────────────────────


def der_to_rs(der: bytes) -> tuple[int, int]:
    """
    (r, s) from the DER signature an authenticator returns, with `s` in the lower half of the
    order. (r, N - s) signs the same message as (r, s); OZ's P256 accepts only the low one, so a
    high `s` -- which an authenticator returns about half the time -- is folded down here.

    @raises PasskeyError  If it isn't a DER-encoded ECDSA signature with r and s in range.
    """
    try:
        r, s = decode_dss_signature(bytes(der))
    except ValueError as exc:
        raise PasskeyError("The passkey's signature couldn't be read.") from exc
    if not (0 < r < P256_N and 0 < s < P256_N):
        raise PasskeyError("The passkey's signature is out of range.")
    if s > P256_HALF_N:
        s = P256_N - s
    return r, s


def client_data_indices(client_data_json: bytes, challenge: bytes) -> tuple[int, int]:
    """
    (challengeIndex, typeIndex): where `"challenge":"<base64url(challenge)>"` and
    `"type":"webauthn.get"` start in clientDataJSON. OZ compares the bytes AT these offsets, so they
    are found by the same bytes, not by parsing the JSON: a clientDataJSON that only parses to the
    right values (a space after a colon, say) would pass a JSON check and fail on chain.

    @raises PasskeyError  If either is missing.
    """
    challenge_at = client_data_json.find(_CHALLENGE_KEY + bytes_to_base64url(challenge).encode() + b'"')
    type_at = client_data_json.find(_TYPE_GET)
    if challenge_at < 0 or type_at < 0:
        raise PasskeyError("The browser's approval isn't in a form the wallet can check.")
    return challenge_at, type_at


def pack_signature(
    qx: bytes,
    qy: bytes,
    r: int,
    s: int,
    challenge_index: int,
    type_index: int,
    authenticator_data: bytes,
    client_data_json: bytes,
) -> bytes:
    """
    The UserOperation signature for a passkey: qx || qy || abi.encode(WebAuthnAuth's six fields).
    See the module docstring for why the fields are encoded as six values rather than one struct.
    """
    return bytes(qx) + bytes(qy) + abi_encode(
        _AUTH_TYPES,
        [r.to_bytes(32, "big"), s.to_bytes(32, "big"), challenge_index, type_index,
         bytes(authenticator_data), bytes(client_data_json)],
    )


def contract_signature(
    qx: bytes, qy: bytes, challenge: bytes, authenticator_data: bytes, client_data_json: bytes, der: bytes
) -> bytes:
    """
    Packs a WebAuthn assertion into the signature the wallet verifies: der_to_rs, then
    client_data_indices, then pack_signature.

    @param challenge  The 32 bytes the browser was asked to sign: the op's userOpHash.
    @raises PasskeyError  If the assertion can't be packed.
    """
    r, s = der_to_rs(der)
    challenge_index, type_index = client_data_indices(client_data_json, challenge)
    return pack_signature(qx, qy, r, s, challenge_index, type_index, authenticator_data, client_data_json)


def packed_length(authenticator_data_len: int, client_data_json_len: int) -> int:
    """
    How long pack_signature's output is for these two lengths: qx, qy, six head words, and each
    dynamic field's length word plus its bytes padded to a whole word.
    """
    def padded(n: int) -> int:
        return -(-n // 32) * 32

    return 64 + 6 * 32 + 32 + padded(authenticator_data_len) + 32 + padded(client_data_json_len)


def _decode_auth(data: bytes):
    """
    WebAuthn.tryDecodeAuth, in Python: the six fields read in place, or None for an encoding the
    contract would refuse -- too short, an offset or a length that runs past the end. Every check
    is the library's own, in its order.
    """
    if len(data) < 0xC0:
        return None

    def word(at: int) -> int:
        return int.from_bytes(data[at:at + 32], "big")

    auth_data_at, client_data_at = word(0x80), word(0xA0)
    if len(data) - 0x20 < auth_data_at or len(data) - 0x20 < client_data_at:
        return None
    auth_data_len, client_data_len = word(auth_data_at), word(client_data_at)
    if len(data) - auth_data_at - 0x20 < auth_data_len or len(data) - client_data_at - 0x20 < client_data_len:
        return None
    return (
        int.from_bytes(data[0:32], "big"),
        int.from_bytes(data[32:64], "big"),
        word(0x40),
        word(0x60),
        data[auth_data_at + 32:auth_data_at + 32 + auth_data_len],
        data[client_data_at + 32:client_data_at + 32 + client_data_len],
    )


def verify_like_contract(challenge: bytes, signature: bytes, require_uv: bool = True) -> bool:
    """
    Whether SessionHandler would accept `signature` as a passkey's approval of `challenge` -- OZ's
    WebAuthn.verify (and tryDecodeAuth before it), check by check, in Python. Leaves out only the
    wallet's own lookup of the key's deadline.

    Not instead of the chain: the bundle is still simulated with the real signature before it goes
    out. This is the cheap check that names a packing fault as one, rather than as a bare AA24, and
    what the tests hold the packing to.

    @param challenge  The 32 bytes that were signed (the op's userOpHash).
    @param signature  The full UserOperation signature: qx || qy || the encoded fields.
    """
    if len(signature) == 65 or len(signature) < 64:
        return False  # 65 bytes is the owner's ECDSA path, not a passkey's
    qx, qy = signature[:32], signature[32:64]
    auth = _decode_auth(signature[64:])
    if auth is None:
        return False
    r, s, challenge_index, type_index, authenticator_data, client_data_json = auth
    # SessionHandler._authenticatePasskey's own guard, ahead of the library's checks.
    if challenge_index >= len(client_data_json) or type_index >= len(client_data_json):
        return False
    if len(authenticator_data) <= 36:
        return False
    if not (len(client_data_json) > type_index + 20 and client_data_json[type_index:type_index + 21] == _TYPE_GET):
        return False
    expected = _CHALLENGE_KEY + bytes_to_base64url(challenge).encode() + b'"'
    if client_data_json[challenge_index:challenge_index + len(expected)] != expected:
        return False
    flags = authenticator_data[32]
    if not flags & FLAG_UP or (require_uv and not flags & FLAG_UV):
        return False
    if not flags & FLAG_BE and flags & FLAG_BS:
        return False
    if not (0 < r < P256_N and 0 < s <= P256_HALF_N):
        return False
    try:
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(qx, "big"), int.from_bytes(qy, "big"), ec.SECP256R1()
        ).public_key()
        public_key.verify(
            encode_dss_signature(r, s),
            authenticator_data + hashlib.sha256(client_data_json).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
    except (ValueError, InvalidSignature):
        return False
    return True


# ── A passkey in software ─────────────────────────────────────────────────────


def _cose_es256(x: bytes, y: bytes) -> bytes:
    """A P-256 public key as a COSE key: {kty: EC2, alg: ES256, crv: P-256, x, y}."""
    return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: x, -3: y})


class SoftPasskey:
    """
    A passkey held in software: a P-256 key that answers WebAuthn's two ceremonies -- making a
    credential, and asserting with it -- byte for byte as a browser and a platform authenticator
    would. The JSON it returns is what navigator.credentials.create() / .get() give the web app
    (RegistrationResponseJSON / AuthenticationResponseJSON), and py_webauthn verifies it as it
    would a phone's.

    NEVER a user's key. It exists for two things: the bundler signs a SIMULATED op with a fresh one
    (bundler.quote_user_op), planting it in the wallet for the length of one eth_call; and the
    tests, and deploy_wallet.py's harness, sign as a user's device would, so a confirmation can be
    driven end to end without a phone.

    @param private_key     The P-256 key; a fresh one when None.
    @param credential_id   The credential's id; 16 random bytes when None.
    @param rp_id           The relying party it answers for (its rpIdHash).
    @param origin          The page it says it was asked from (clientDataJSON's origin).
    @param backed_up       Whether it reports itself as a synced passkey (BE and BS), as iCloud
                           Keychain and Google Password Manager passkeys do.
    """

    def __init__(
        self,
        private_key: ec.EllipticCurvePrivateKey | None = None,
        *,
        credential_id: bytes | None = None,
        rp_id: str = "localhost",
        origin: str = "http://localhost:3000",
        backed_up: bool = True,
    ):
        self.private_key = private_key or ec.generate_private_key(ec.SECP256R1())
        self.credential_id = credential_id or secrets.token_bytes(16)
        self.rp_id = rp_id
        self.origin = origin
        self.backed_up = backed_up
        self.sign_count = 0
        numbers = self.private_key.public_key().public_numbers()
        self.qx = numbers.x.to_bytes(32, "big")
        self.qy = numbers.y.to_bytes(32, "big")

    @classmethod
    def from_secret(cls, secret: bytes, **kwargs) -> "SoftPasskey":
        """
        The same passkey every time for the same secret: its key and credential id are derived
        from it. For deploy_wallet.py's harness, which must sign again in a later process without
        storing a key anywhere.
        """
        scalar = int.from_bytes(hashlib.sha256(b"mitfah-soft-passkey-key" + secret).digest(), "big") % (P256_N - 1) + 1
        credential_id = hashlib.sha256(b"mitfah-soft-passkey-credential" + secret).digest()[:16]
        return cls(ec.derive_private_key(scalar, ec.SECP256R1()), credential_id=credential_id, **kwargs)

    @property
    def key_id(self) -> str:
        """The id the wallet files this key under (key_id)."""
        return key_id(self.qx, self.qy)

    @property
    def credential_id_b64(self) -> str:
        return bytes_to_base64url(self.credential_id)

    def cose_public_key(self) -> bytes:
        return _cose_es256(self.qx, self.qy)

    def _flags(self, user_verified: bool, attested: bool = False) -> int:
        flags = FLAG_UP | (FLAG_UV if user_verified else 0) | (FLAG_AT if attested else 0)
        return flags | (FLAG_BE | FLAG_BS if self.backed_up else 0)

    def _rp_id_hash(self, rp_id: str | None = None) -> bytes:
        return hashlib.sha256((rp_id or self.rp_id).encode()).digest()

    def registration_response(
        self, challenge: bytes, *, user_verified: bool = True, cose_key: bytes | None = None, origin: str | None = None
    ) -> dict:
        """
        What navigator.credentials.create() returns for this key: attestation "none", as the API
        asks for.

        @param challenge  The registration options' challenge (raw bytes).
        @param cose_key   A different public key to claim, for a test of what the API refuses.
        """
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": bytes_to_base64url(challenge),
             "origin": origin or self.origin, "crossOrigin": False},
            separators=(",", ":"),
        ).encode()
        authenticator_data = (
            self._rp_id_hash()
            + bytes([self._flags(user_verified, attested=True)])
            + self.sign_count.to_bytes(4, "big")
            + b"\x00" * 16                                   # aaguid: none, as attestation "none" reports
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + (cose_key or self.cose_public_key())
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authenticator_data})
        return {
            "id": self.credential_id_b64,
            "rawId": self.credential_id_b64,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def _client_data_get(self, challenge: bytes, origin: str | None, pad_to: int) -> bytes:
        """
        clientDataJSON for an assertion, as a browser writes it. Padded to `pad_to` bytes, when that
        is longer, with the extra member Chrome adds to some (other_keys_can_be_added_here): the
        bundler simulates with an approval as long as the longest it budgets for.
        """
        fields = {"type": "webauthn.get", "challenge": bytes_to_base64url(challenge),
                  "origin": origin or self.origin, "crossOrigin": False}
        compact = json.dumps(fields, separators=(",", ":")).encode()
        padding = pad_to - len(compact) - len(b',"other_keys_can_be_added_here":""')
        if padding > 0:
            fields["other_keys_can_be_added_here"] = "x" * padding
            compact = json.dumps(fields, separators=(",", ":")).encode()
        return compact

    def assertion(
        self,
        challenge: bytes,
        *,
        user_verified: bool = True,
        origin: str | None = None,
        rp_id: str | None = None,
        pad_client_data_to: int = 0,
        user_handle: bytes | None = None,
    ) -> dict:
        """
        What navigator.credentials.get() returns when this passkey approves `challenge`.

        @param user_verified       False for an approval without Face ID / PIN (UP only): the API
                                   and the wallet must both refuse it.
        @param origin, rp_id       Another page or site to claim, for tests of what is refused.
        @param pad_client_data_to  Make clientDataJSON this long (see _client_data_get).
        """
        client_data = self._client_data_get(challenge, origin, pad_client_data_to)
        authenticator_data = self._rp_id_hash(rp_id) + bytes([self._flags(user_verified)]) + self.sign_count.to_bytes(4, "big")
        der = self.private_key.sign(authenticator_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        response = {
            "clientDataJSON": bytes_to_base64url(client_data),
            "authenticatorData": bytes_to_base64url(authenticator_data),
            "signature": bytes_to_base64url(der),
        }
        if user_handle is not None:
            response["userHandle"] = bytes_to_base64url(user_handle)
        return {
            "id": self.credential_id_b64,
            "rawId": self.credential_id_b64,
            "type": "public-key",
            "response": response,
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def contract_signature(self, challenge: bytes, *, pad_client_data_to: int = 0) -> bytes:
        """
        This passkey's approval of `challenge`, packed as the wallet reads it: an assertion, then
        contract_signature.
        """
        response = self.assertion(challenge, pad_client_data_to=pad_client_data_to)["response"]
        return contract_signature(
            self.qx,
            self.qy,
            challenge,
            base64url_to_bytes(response["authenticatorData"]),
            base64url_to_bytes(response["clientDataJSON"]),
            base64url_to_bytes(response["signature"]),
        )
