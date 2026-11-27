"""
Checks the contact passes Mitfah attaches to every passkey op it builds (userop.contact_passes), against what
the wallet's PayeeGuard reads (src/PayeeGuard.sol): the typed data the owner signs, the pass layout, the
footer, which contacts get a pass, that an unsigned contact can't be paid, and that agent trades carry none. Throwaway database only;
nothing touches a chain.

The digest the owner signs is pinned to the contract's: PayeeGuardTest.test_digest_matchesTheApp checks
the same inputs give the same digest on chain. Change one and both must change.

Run: make contact-passes-test   (or: python app/tests/test_contact_passes.py)
"""
import tempfile

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

import db                           # noqa: E402
from eth_account import Account     # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak        # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402

import auth                         # noqa: E402
import userop                       # noqa: E402
from tools import common            # noqa: E402

BOB = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
CAROL = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
USDC = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"

def _fresh_db():
    if hasattr(db._local, "db"):
        db._local.db.close()
        del db._local.db
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    db.DB_PATH = handle.name
    db.init_db()


def _digest(name: str, payee: str, issued_at: int) -> bytes:
    typed = auth.contact_typed_data(name, payee, issued_at)
    full = {**typed, "types": {"EIP712Domain": auth._DOMAIN_TYPE, **typed["types"]}}
    signable = encode_typed_data(full_message=full)
    # EIP-712: keccak256("\x19\x01" || domainSeparator || structHash)
    return keccak(b"\x19" + signable.version + signable.header + signable.body)


def _signed(owner, name: str, payee: str, issued_at: int) -> str:
    typed = auth.contact_typed_data(name, payee, issued_at)
    return Account.sign_typed_data(owner.key, typed["domain"], typed["types"], typed["message"]).signature.hex()


def test_digest_is_the_contracts():
    print("\n[1] the typed data the owner signs is the one the wallet checks")
    domain_separator = keccak(
        keccak(text="EIP712Domain(string name,string version)")
        + keccak(text="Mitfah")
        + keccak(text="2")
    )
    type_hash = keccak(text="Contact(string name,address payee,uint64 issuedAt)")
    struct_hash = keccak(
        type_hash + keccak(text="sandy") + bytes(12) + bytes.fromhex(BOB[2:]) + (1_800_000_000).to_bytes(32, "big")
    )
    by_hand = keccak(b"\x19\x01" + domain_separator + struct_hash)
    check("eth_account's digest of auth's typed data is the contract's formula",
          _digest("sandy", BOB, 1_800_000_000) == by_hand, by_hand.hex())
    check("and equals the digest pinned in PayeeGuardTest",
          "0x" + by_hand.hex() == PINNED_DIGEST_FROM_SOLIDITY, "0x" + by_hand.hex())


def test_pass_layout():
    print("\n[2] a pass is laid out as PayeeGuard reads it")
    owner = Account.create()
    signature = _signed(owner, "sandy", BOB, 1_800_000_000)
    p = userop.encode_contact_pass(BOB, 1_800_000_000, "sandy", signature)
    check("125 bytes", len(p) == userop.PASS_LENGTH == 125, str(len(p)))
    check("payee first", p[:20] == bytes.fromhex(BOB[2:]))
    check("then issuedAt, 8 bytes big-endian", int.from_bytes(p[20:28], "big") == 1_800_000_000)
    check("then keccak256(name)", p[28:60] == keccak(text="sandy"))
    check("then r, s, v as signed", p[60:] == bytes.fromhex(signature[2:] if signature.startswith("0x") else signature))
    digest = _digest("sandy", BOB, 1_800_000_000)
    v, r, s = p[124], int.from_bytes(p[60:92], "big"), int.from_bytes(p[92:124], "big")
    recovered = Account._recover_hash(digest, vrs=(v, r, s))
    check("and the signature in it recovers to the owner over that digest", recovered == owner.address, recovered)


def test_which_contacts_get_a_pass():
    print("\n[3] every signed contact the calls name gets a pass, and no one else")
    _fresh_db()
    owner = Account.create()
    user_id = _make_user(owner)
    now = 1_800_000_000
    db.save_contact(user_id, "bob", BOB, now, "0x" + _signed(owner, "bob", BOB, now).removeprefix("0x"))
    db.save_contact(user_id, "carol", CAROL, now, "0x" + _signed(owner, "carol", CAROL, now).removeprefix("0x"))

    transfer_to_bob = bytes.fromhex("a9059cbb") + bytes(12) + bytes.fromhex(BOB[2:]) + (5).to_bytes(32, "big")
    footer = userop.contact_passes(user_id, [(USDC, 0, transfer_to_bob)])
    check("one pass, then the count and the magic", len(footer) == 125 + 5 and footer[-5] == 1
          and footer[-4:] == userop.PASSES_MAGIC, footer[-5:].hex())
    check("and it is bob's", footer[:20] == bytes.fromhex(BOB[2:]))

    footer = userop.contact_passes(user_id, [(CAROL, 10**15, b""), (USDC, 0, transfer_to_bob)])
    check("a plain send's target is found too", footer[-5] == 2, str(footer[-5]))

    footer = userop.contact_passes(user_id, [(USDC, 0, bytes.fromhex("095ea7b3") + bytes(64))])
    check("a call naming no contact carries an empty footer", footer == b"\x00" + userop.PASSES_MAGIC, footer.hex())

    # A contact from before the wallet checked them has no signature: it gets no pass.
    db.get_db().execute("INSERT INTO contacts (user_id, name, address) VALUES (?, ?, ?)", (user_id, "old", CAROL[:-4] + "0000"))
    db.get_db().commit()
    old_addr = CAROL[:-4] + "0000"
    footer = userop.contact_passes(user_id, [(old_addr, 1, b"")])
    check("an unsigned contact gets no pass", footer[-5] == 0, str(footer[-5]))


def test_too_many_payees():
    print("\n[4] more contacts than one op can carry passes for is refused before anything is signed")
    _fresh_db()
    owner = Account.create()
    user_id = _make_user(owner)
    executions = []
    for i in range(userop.MAX_PASSES + 1):
        address = "0x" + (i + 1).to_bytes(20, "big").hex()
        db.save_contact(user_id, f"c{i}", address, 1_800_000_000, "0x" + "11" * 65)
        executions.append((address, 1, b""))
    try:
        userop.contact_passes(user_id, executions)
        check("refused", False)
    except userop.TooManyPayees:
        check("refused with TooManyPayees", True)


def test_unsigned_contacts_cant_be_paid():
    print("\n[5] the assistant refuses an unsigned contact, saying how to fix it")
    _fresh_db()
    owner = Account.create()
    user_id = _make_user(owner)
    db.get_db().execute("INSERT INTO contacts (user_id, name, address) VALUES (?, ?, ?)", (user_id, "sam", BOB))
    db.get_db().commit()
    try:
        common._resolve_contact(user_id, "sam")
        check("refused", False)
    except ToolException as err:
        check("refused", True)
        check("and the message says to sign it again in Contacts", "Sign again" in str(err), str(err)[:160])
    db.save_contact(user_id, "sam", BOB, 1_800_000_000, "0x" + "22" * 65)
    check("once signed it resolves", common._resolve_contact(user_id, "sam") == BOB)


def test_agent_trades_carry_no_passes():
    print("\n[6] a passkey's calldata carries the passes; an agent's carries none (agents are exempt)")
    _fresh_db()
    owner = Account.create()
    user_id = _make_user(owner)
    now = 1_800_000_000
    db.save_contact(user_id, "bob", BOB, now, "0x" + _signed(owner, "bob", BOB, now).removeprefix("0x"))

    class _Wallet:  # stands in for the bound SessionHandler: only its execute() encoding is used
        def encode_abi(self, abi_element_identifier, args):
            return "0xe9ae5c53"

    real = (userop.load_session_handler, userop.load_entry_point)
    userop.load_session_handler = lambda user_id: _Wallet()
    userop.load_entry_point = lambda user_id: None
    try:
        transfer_to_bob = bytes.fromhex("a9059cbb") + bytes(12) + bytes.fromhex(BOB[2:]) + (5).to_bytes(32, "big")
        _, _, passkey_calldata = userop.build_execute_calldata(user_id, [(USDC, 0, transfer_to_bob)])
        _, _, agent_calldata = userop.build_execute_calldata(user_id, [(USDC, 0, transfer_to_bob)], with_passes=False)
    finally:
        userop.load_session_handler, userop.load_entry_point = real
    check("a passkey's ends with bob's pass and the magic",
          passkey_calldata.endswith(userop.PASSES_MAGIC.hex()) and len(passkey_calldata) == 10 + 2 * (125 + 5),
          passkey_calldata[-10:])
    check("an agent's is execute() alone", agent_calldata == "0xe9ae5c53", agent_calldata[-10:])


def _make_user(owner) -> int:
    """A users row owned by `owner`, the way SIWE sign-in makes one."""
    conn = db.get_db()
    cur = conn.execute("INSERT INTO users (owner_addr, created_at) VALUES (?, strftime('%s','now'))", (owner.address,))
    conn.commit()
    return cur.lastrowid


# The digest PayeeGuardTest.test_digest_matchesTheApp pins for Contact("sandy", BOB, 1_800_000_000).
PINNED_DIGEST_FROM_SOLIDITY = "0xbc69de402d981b481e44c0ba738d8550d96537e4919c0c1da3b576f2d41f139b"


if __name__ == "__main__":
    test_digest_is_the_contracts()
    test_pass_layout()
    test_which_contacts_get_a_pass()
    test_too_many_payees()
    test_unsigned_contacts_cant_be_paid()
    test_agent_trades_carry_no_passes()
    finish("All contact pass checks passed.")
