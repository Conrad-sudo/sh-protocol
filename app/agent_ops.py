"""
An agent's trade as AgentModule (src/AgentModule.sol) and the wallet read it: the encodings shared
by the code that prices one (bundler.quote_agent_op), the code that prepares and sends one
(agents.py) and the tests. Nothing here reads the chain.

An agent op differs from a passkey's in three places, and only there:

  - Its nonce key names the module. ERC-4337 v0.7 splits a nonce into a 192-bit key and a 64-bit
    sequence; the wallet reads the key's top 20 bytes as the validator module the op is for, and
    Mitfah puts a random 4-byte lane after them (agent_nonce), so an agent's trades never queue
    behind one another, as a passkey's random nonce keys don't.
  - Its callData is executeUserOp's selector followed by the wallet's own execute() calldata
    (agent_call_data). That shape makes the EntryPoint call the wallet's executeUserOp, which runs
    AgentModule's preExecute and postExecute around the calls -- where the agent's own limit is
    charged.
  - Its signature is 95 bytes: agent (20) | epoch (4) | deadline (6) | r (32) | s (32) | v (1)
    (pack_signature). The last 65 are the agent's own ECDSA signature over the EIP-712 digest of
    AgentOperation(userOpHash, deadline, epoch) in the module's domain (operation_digest), which an
    agent signs as typed data (typed_data, eth_signTypedData_v4). The deadline is the last moment
    the op may run; the epoch is the agent's, which changes each time it is added, so a removed
    agent's signatures die with it.

The signature check here (recover_agent) mirrors OpenZeppelin's ECDSA.tryRecover, which the module
uses: v must be 27 or 28 and s in the lower half of the curve order. A signature the module would
refuse is refused here first, before a trade is claimed.
"""
import secrets

from eth_abi import encode as abi_encode
from eth_keys import keys
from eth_keys.exceptions import BadSignature
from eth_utils import keccak, to_checksum_address

# IAccountExecute.executeUserOp(PackedUserOperation, bytes32): the EntryPoint calls it instead of
# forwarding the callData whenever an op's callData starts with this selector.
EXECUTE_USER_OP_SELECTOR = keccak(
    text="executeUserOp((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes),bytes32)"
)[:4]

# AgentModule's EIP-712 domain and the one type its agents sign (OPERATION_TYPEHASH there).
DOMAIN_NAME = "AgentModule"
DOMAIN_VERSION = "1"
DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
OPERATION_TYPE = "AgentOperation(bytes32 userOpHash,uint48 deadline,uint32 epoch)"
OPERATION_TYPEHASH = keccak(text=OPERATION_TYPE)

# AgentModule.SIGNATURE_LENGTH: what the wallet routes to the module by length, and the only length
# the module accepts.
SIGNATURE_LENGTH = 95
ECDSA_LENGTH = 65

# Where AgentModule keeps its agents: `_agents`, mapping(account => mapping(agent => Agent)), is
# storage slot 2, after EIP712's two fallback strings. An Agent is two slots: limitUsd (low 16 bytes)
# and spentUsd (high 16) in the first; validUntil (6 bytes), windowStart (4), epoch (4) and position
# (1), from the low end, in the second. Only the gas estimate uses it, to plant a throwaway agent
# (bundler.quote_agent_op), and it checks the layout on the live contract before trusting it.
AGENTS_SLOT = 2

# Half the secp256k1 order: OpenZeppelin's ECDSA refuses a signature whose s is above it (the
# malleable twin of a valid one).
_HALF_ORDER = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0

_UINT32, _UINT48 = 2**32, 2**48


def domain_separator(module: str, chain_id: int) -> bytes:
    """AgentModule's EIP-712 domain separator on `chain_id`, for the module at `module`."""
    return keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [DOMAIN_TYPEHASH, keccak(text=DOMAIN_NAME), keccak(text=DOMAIN_VERSION), chain_id, to_checksum_address(module)],
    ))


def operation_digest(module: str, chain_id: int, user_op_hash: bytes, deadline: int, epoch: int) -> bytes:
    """
    What the agent signs for one op: AgentModule.hashOperation(userOpHash, deadline, epoch).

    @param user_op_hash  The op's hash, as the EntryPoint computes it (userop.hash_user_op).
    @param deadline      The last moment the op may run, in Unix seconds. Non-zero, 48 bits.
    @param epoch         The agent's epoch, from AgentModule.getAgent. Non-zero, 32 bits.
    """
    if len(user_op_hash) != 32:
        raise ValueError("a userOpHash is 32 bytes")
    if not 0 < deadline < _UINT48 or not 0 < epoch < _UINT32:
        raise ValueError("deadline and epoch must be non-zero and fit 48 and 32 bits")
    struct_hash = keccak(abi_encode(["bytes32", "bytes32", "uint48", "uint32"],
                                    [OPERATION_TYPEHASH, user_op_hash, deadline, epoch]))
    return keccak(b"\x19\x01" + domain_separator(module, chain_id) + struct_hash)


def typed_data(module: str, chain_id: int, user_op_hash: bytes, deadline: int, epoch: int) -> dict:
    """
    The same message as operation_digest, as EIP-712 typed data: what an agent hands its signer
    (eth_signTypedData_v4, or eth_account's sign_typed_data(full_message=...)).
    """
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "AgentOperation": [
                {"name": "userOpHash", "type": "bytes32"},
                {"name": "deadline", "type": "uint48"},
                {"name": "epoch", "type": "uint32"},
            ],
        },
        "primaryType": "AgentOperation",
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(module),
        },
        "message": {"userOpHash": "0x" + user_op_hash.hex(), "deadline": deadline, "epoch": epoch},
    }


def nonce_key(module: str, lane: int) -> int:
    """The 192-bit nonce key of an agent op: the module's address, then a 4-byte lane."""
    if not 0 <= lane < _UINT32:
        raise ValueError("a lane is 4 bytes")
    return (int(to_checksum_address(module), 16) << 32) | lane


def agent_nonce(module: str, lane: int | None = None, sequence: int = 0) -> int:
    """
    An agent op's nonce: nonce_key(module, lane) and a sequence. A random lane when none is given:
    every trade takes a key nobody has used, so its sequence is 0 and nothing waits on another trade.
    """
    if lane is None:
        lane = secrets.randbits(32)
    return (nonce_key(module, lane) << 64) | sequence


def module_of_nonce(nonce: int) -> str:
    """The module an op's nonce key names: the wallet's own reading of it (_extractUserOpValidator)."""
    return to_checksum_address((nonce >> 96).to_bytes(20, "big"))


def agent_call_data(execute_calldata: bytes) -> bytes:
    """An agent op's callData: executeUserOp's selector, then the wallet's execute() calldata."""
    return EXECUTE_USER_OP_SELECTOR + execute_calldata


def pack_signature(agent: str, epoch: int, deadline: int, ecdsa: bytes) -> bytes:
    """
    The 95-byte signature the wallet hands AgentModule: agent | epoch | deadline | r | s | v.

    @param ecdsa  The agent's 65-byte signature of operation_digest (normalize_ecdsa).
    """
    if len(ecdsa) != ECDSA_LENGTH:
        raise ValueError("an ECDSA signature is 65 bytes")
    if not 0 < deadline < _UINT48 or not 0 < epoch < _UINT32:
        raise ValueError("deadline and epoch must be non-zero and fit 48 and 32 bits")
    packed = (
        bytes.fromhex(to_checksum_address(agent)[2:]) + epoch.to_bytes(4, "big") + deadline.to_bytes(6, "big") + ecdsa
    )
    assert len(packed) == SIGNATURE_LENGTH
    return packed


def budget_signature(agent: str, epoch: int, deadline: int) -> bytes:
    """
    A signature exactly as long as an agent's, for sizing preVerificationGas before the agent has
    signed. Its r and s are non-zero bytes throughout, so the calldata it is counted as costs at least
    what the real one will.
    """
    return pack_signature(agent, epoch, deadline, b"\xff" * 64 + b"\x1c")


def normalize_ecdsa(signature: str | bytes) -> bytes:
    """
    An agent's signature as the module will read it: 65 bytes, r | s | v, with v 27 or 28. A v of 0
    or 1 (some signers' habit) is moved up by 27; it is the same signature.

    @raises ValueError  If it is not 65 bytes of hex, or the module would refuse it (another v, or s
                        in the upper half of the curve order).
    """
    if isinstance(signature, str):
        text = signature.strip()
        text = text[2:] if text.lower().startswith("0x") else text
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError("it isn't hex") from exc
    else:
        raw = bytes(signature)
    if len(raw) != ECDSA_LENGTH:
        raise ValueError(f"it is {len(raw)} bytes; an Ethereum signature is 65 (r, s, v)")
    v = raw[64]
    if v in (0, 1):
        v += 27
    if v not in (27, 28):
        raise ValueError(f"its v is {raw[64]}; it must be 27 or 28")
    if int.from_bytes(raw[32:64], "big") > _HALF_ORDER:
        raise ValueError("its s is in the upper half of the curve order, which the wallet refuses")
    return raw[:64] + bytes([v])


def recover_agent(digest: bytes, ecdsa: bytes) -> str:
    """
    Who signed `digest`, as the module will recover it.

    @param ecdsa  From normalize_ecdsa.
    @raises ValueError  If no key could have made it.
    """
    signature = normalize_ecdsa(ecdsa)
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:64], "big")
    try:
        public_key = keys.Signature(vrs=(signature[64] - 27, r, s)).recover_public_key_from_msg_hash(digest)
    except (BadSignature, ValueError) as exc:
        raise ValueError("no key could have made it") from exc
    return public_key.to_checksum_address()


def record_slot(account: str, agent: str) -> int:
    """The first of the two storage slots holding `agent`'s record for `account` on AgentModule."""
    outer = keccak(abi_encode(["address", "uint256"], [to_checksum_address(account), AGENTS_SLOT]))
    return int.from_bytes(keccak(abi_encode(["address", "bytes32"], [to_checksum_address(agent), outer])), "big")


def record_words(limit: int, valid_until: int, epoch: int, position: int, spent: int = 0, window_start: int = 0) -> tuple[int, int]:
    """An Agent record's two storage words, packed as Solidity packs the struct (see AGENTS_SLOT)."""
    first = (spent << 128) | limit
    second = (position << 112) | (epoch << 80) | (window_start << 48) | valid_until
    return first, second
