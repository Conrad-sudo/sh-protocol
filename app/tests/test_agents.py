"""
Checks the backend of trading agents -- agent_ops.py, agents.py, agent_trades.py, routes/agents.py and
the keeper's look -- against a throwaway database:

  - what an agent signs, and how its signature is read: the EIP-712 message is AgentModule's own (pinned
    to one known answer that test/unit/AgentModuleTest.t.sol pins Solidity to as well), and a signature
    the module would refuse is refused before a trade is claimed;
  - an agent's trade stays out of the passkey flows: never a card, never opened or approved by a page;
  - a trade is sent once, and only with the agent's own signature;
  - Mitfah's account of what each agent spends, so the user hears of every trade made outside Mitfah
    and of none of Mitfah's own, in whatever order they land;
  - the owner's renaming, and the readable reasons a trade is refused.

Offline: no chain and no RPC. What the chain would answer is stood in for.

Run: make agents-test   (or: python app/tests/test_agents.py)
"""
import json
import os
import tempfile
import time
from types import SimpleNamespace

from checks import check, finish, sign_in   # first: it puts app/ on sys.path for the imports below

# A scratch database, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"

import db                                   # noqa: E402

db.DB_PATH = _tmp_db.name
db.init_db()

from eth_abi import encode as abi_encode    # noqa: E402
from eth_account import Account             # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402

import agent_ops                            # noqa: E402
import agent_trades                         # noqa: E402
import agents                               # noqa: E402
import api                                  # noqa: E402
import contract_errors                      # noqa: E402
import signing                              # noqa: E402
from bundler import FinalUserOp, UserOpQuote  # noqa: E402
from db.sign_requests import FAILED, OPEN, SENT  # noqa: E402

USD = 10**18
MODULE = "0x8b1cb45e2e7a101f9dafdf732acf616352069d7f"
WALLET = to_checksum_address("0x00000000000000000000000000000000000000ab")
# The known answer AgentModuleTest.test_hashOperation_knownAnswer pins Solidity to.
KAT_MODULE = "0x000000000000000000000000000000000000A6e7"
KAT_DIGEST = bytes.fromhex("cb2dfac9c9459ad66f846fc7c8ee70ebc5d7aa2f116bfd45768409668f13519e")
HALF_ORDER = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def raises(fn, kind=Exception, text: str = "") -> bool:
    """Whether fn() raises `kind` (with `text` in its message, when given)."""
    try:
        fn()
    except kind as e:
        return text in str(e)
    return False


# ── 1. What an agent signs ──────────────────────────────────────────────────────


def test_message():
    print("\n[1] What an agent signs: AgentModule's own EIP-712 message")
    digest = agent_ops.operation_digest(KAT_MODULE, 1, keccak(text="mitfah agent KAT"), 1_800_000_000, 7)
    check("the known answer, the same constant Solidity is pinned to", digest == KAT_DIGEST, digest.hex())
    check("the type is AgentModule's",
          agent_ops.OPERATION_TYPEHASH == keccak(text="AgentOperation(bytes32 userOpHash,uint48 deadline,uint32 epoch)"))

    user_op_hash = keccak(text="an op")
    typed = agent_ops.typed_data(MODULE, 11155111, user_op_hash, 1_900_000_000, 3)
    signer = Account.create()
    signed = signer.sign_typed_data(full_message=typed)
    expected = agent_ops.operation_digest(MODULE, 11155111, user_op_hash, 1_900_000_000, 3)
    check("an ordinary signTypedData of typed_data signs exactly that digest", signed.message_hash == expected)
    check("...and the typed data reads back to it", encode_typed_data(full_message=typed).body
          == keccak(abi_encode(["bytes32", "bytes32", "uint48", "uint32"],
                               [agent_ops.OPERATION_TYPEHASH, user_op_hash, 1_900_000_000, 3])))
    check("another chain is another digest",
          agent_ops.operation_digest(MODULE, 1, user_op_hash, 1_900_000_000, 3) != expected)
    check("another module is another digest",
          agent_ops.operation_digest(KAT_MODULE, 11155111, user_op_hash, 1_900_000_000, 3) != expected)
    check("a zero deadline or epoch is refused",
          raises(lambda: agent_ops.operation_digest(MODULE, 1, user_op_hash, 0, 3), ValueError)
          and raises(lambda: agent_ops.operation_digest(MODULE, 1, user_op_hash, 5, 0), ValueError))
    check("a hash that isn't 32 bytes is refused",
          raises(lambda: agent_ops.operation_digest(MODULE, 1, b"short", 5, 3), ValueError))

    abi = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
    entry = next(e for e in abi if e.get("type") == "function" and e["name"] == "executeUserOp")

    def type_of(param) -> str:
        if param["type"].startswith("tuple"):
            return "(" + ",".join(type_of(c) for c in param["components"]) + ")" + param["type"][5:]
        return param["type"]

    selector = keccak(text=f"executeUserOp({','.join(type_of(p) for p in entry['inputs'])})")[:4]
    check("executeUserOp's selector is the wallet's own, from its ABI",
          agent_ops.EXECUTE_USER_OP_SELECTOR == selector == bytes.fromhex("8dd7712f"), selector.hex())


# ── 2. Signatures ───────────────────────────────────────────────────────────────


def test_signatures():
    print("\n[2] The agent's signature, as the wallet and the module read it")
    agent = Account.create()
    digest = agent_ops.operation_digest(MODULE, 1, keccak(text="op"), 1_900_000_000, 4)
    ecdsa = bytes(agent.unsafe_sign_hash(digest).signature)
    packed = agent_ops.pack_signature(agent.address, 4, 1_900_000_000, ecdsa)
    check("packed: agent | epoch | deadline | r | s | v, 95 bytes",
          len(packed) == 95 and packed[:20] == bytes.fromhex(agent.address[2:])
          and int.from_bytes(packed[20:24], "big") == 4 and int.from_bytes(packed[24:30], "big") == 1_900_000_000
          and packed[30:] == ecdsa)
    budget = agent_ops.budget_signature(agent.address, 4, 1_900_000_000)
    check("the gas budget's stand-in is as long, with no zero byte in r or s",
          len(budget) == 95 and budget[:30] == packed[:30] and 0 not in budget[30:94])

    check("the agent's signature recovers to the agent", agent_ops.recover_agent(digest, ecdsa) == agent.address)
    check("...given as hex, with or without 0x",
          agent_ops.recover_agent(digest, agent_ops.normalize_ecdsa("0x" + ecdsa.hex())) == agent.address
          and agent_ops.normalize_ecdsa(ecdsa.hex()) == ecdsa)
    low_v = ecdsa[:64] + bytes([ecdsa[64] - 27])
    check("a v of 0 or 1 is read as 27 or 28 (the same signature)", agent_ops.normalize_ecdsa(low_v) == ecdsa)
    check("another digest recovers to someone else",
          agent_ops.recover_agent(agent_ops.operation_digest(MODULE, 1, keccak(text="other"), 1_900_000_000, 4), ecdsa)
          != agent.address)
    check("64 bytes are refused", raises(lambda: agent_ops.normalize_ecdsa(ecdsa[:64]), ValueError, "64 bytes"))
    check("a v of 29 is refused", raises(lambda: agent_ops.normalize_ecdsa(ecdsa[:64] + b"\x1d"), ValueError, "27 or 28"))
    s = int.from_bytes(ecdsa[32:64], "big")
    twin = ecdsa[:32] + (ORDER - s).to_bytes(32, "big") + bytes([55 - ecdsa[64]])
    check("the malleable twin (s in the upper half), which the module refuses, is refused",
          s <= HALF_ORDER and raises(lambda: agent_ops.normalize_ecdsa(twin), ValueError, "upper half"))
    check("not hex is refused", raises(lambda: agent_ops.normalize_ecdsa("0xnothex"), ValueError, "hex"))
    check("r = 0 recovers no key", raises(lambda: agent_ops.recover_agent(digest, b"\x00" * 32 + ecdsa[32:]), ValueError))


# ── 3. Nonce and call data ──────────────────────────────────────────────────────


def test_shape():
    print("\n[3] An agent op's nonce and call data")
    nonce = agent_ops.agent_nonce(MODULE, lane=0xA1B2C3D4, sequence=5)
    check("the nonce key names the module in its top 20 bytes",
          agent_ops.module_of_nonce(nonce).lower() == MODULE.lower())
    check("...then the lane, then the sequence",
          (nonce >> 64) & 0xFFFFFFFF == 0xA1B2C3D4 and nonce & (2**64 - 1) == 5
          and nonce >> 64 == agent_ops.nonce_key(MODULE, 0xA1B2C3D4))
    first, second = agent_ops.agent_nonce(MODULE), agent_ops.agent_nonce(MODULE)
    check("each trade takes a lane of its own, sequence 0",
          first != second and first & (2**64 - 1) == 0 and agent_ops.module_of_nonce(second).lower() == MODULE.lower())
    check("a lane past 4 bytes is refused", raises(lambda: agent_ops.nonce_key(MODULE, 2**32), ValueError))
    check("call data: executeUserOp's selector, then the wallet's execute()",
          agent_ops.agent_call_data(b"\xe9\xae\x5c\x53rest") == bytes.fromhex("8dd7712f") + b"\xe9\xae\x5c\x53rest")
    words = agent_ops.record_words(limit=1, valid_until=2, epoch=3, position=4, spent=5, window_start=6)
    check("an Agent record packs as Solidity packs it",
          words == ((5 << 128) | 1, (4 << 112) | (3 << 80) | (6 << 48) | 2), str(words))
    outer = keccak(abi_encode(["address", "uint256"], [WALLET, 2]))
    check("its slot is _agents[wallet][agent], the mapping at slot 2",
          agent_ops.record_slot(WALLET, MODULE) == int.from_bytes(keccak(abi_encode(["address", "bytes32"], [MODULE, outer])), "big"))


# ── 4. Mitfah's account of an agent's spend ─────────────────────────────────────


def test_accounts():
    print("\n[4] Mitfah's account of what an agent spent")
    agent = Account.create().address
    row = db.add_agent_row(7, 1, WALLET, agent, "Trader", (1, 100, 5 * USD))
    check("an agent is recorded with its name and what it had spent",
          row["name"] == "Trader" and (row["seen_epoch"], row["seen_window"], int(row["seen_spent"]), row["relayed"])
          == (1, 100, 5 * USD, "0"))
    again = db.add_agent_row(7, 1, WALLET, agent, None, (9, 9, 9))
    check("recording it again keeps its name and its account", again["name"] == "Trader" and again["seen_epoch"] == 1)
    check("...and a name given then replaces its own",
          db.add_agent_row(7, 1, WALLET, agent, "Bot", (9, 9, 9))["name"] == "Bot")

    check("a trade of Mitfah's in the row's window is added", db.add_relayed(1, WALLET, agent, 1, 100, 3 * USD))
    db.add_relayed(1, WALLET, agent, 1, 100, 4 * USD)
    check("...in any order, adding up", db.get_agent_row(1, WALLET, agent)["relayed"] == str(7 * USD))
    check("amounts past SQLite's integers add up exactly",
          db.add_relayed(1, WALLET, agent, 1, 100, 10**21)
          and db.get_agent_row(1, WALLET, agent)["relayed"] == str(7 * USD + 10**21))
    check("a trade from an older window is left alone",
          not db.add_relayed(1, WALLET, agent, 1, 99, USD) and db.get_agent_row(1, WALLET, agent)["relayed"] == str(7 * USD + 10**21))
    check("so is one of another epoch (removed and added again)", not db.add_relayed(1, WALLET, agent, 2, 100, USD))
    check("a trade in a later window starts that window, from nothing spent",
          db.add_relayed(1, WALLET, agent, 1, 200, 2 * USD)
          and (lambda r: (r["seen_window"], r["seen_spent"], r["relayed"]) == (200, "0", str(2 * USD)))(
              db.get_agent_row(1, WALLET, agent)))

    stale = db.get_agent_row(1, WALLET, agent)
    db.add_relayed(1, WALLET, agent, 1, 200, USD)
    check("a rebase decided from a row that changed since is not written",
          not db.rebase_agent(1, WALLET, agent, (1, 200, 3 * USD), was=stale))
    check("...from the row as it is, it is, and nothing stays relayed",
          db.rebase_agent(1, WALLET, agent, (1, 200, 3 * USD), was=db.get_agent_row(1, WALLET, agent))
          and db.get_agent_row(1, WALLET, agent)["relayed"] == "0")

    check("no trade in flight", not db.agent_trade_in_flight(1, WALLET, agent))
    tx = db.add_transaction(7, 1, WALLET, "agent", "Agent trade: transfer", "pending", user_op_hash="0x" + "11" * 32,
                            agent=agent)
    check("a pending trade of the agent's is in flight", db.agent_trade_in_flight(1, WALLET, agent))
    check("...and its History row names the agent", db.get_transaction(7, tx)["agent"] == agent)
    db.settle_transaction(tx, "confirmed", "0x" + "22" * 32, int(time.time()))
    check("once it settles, nothing is", not db.agent_trade_in_flight(1, WALLET, agent))
    other = db.add_transaction(7, 1, WALLET, "assistant", "Send 1 USDC", "pending", user_op_hash="0x" + "33" * 32)
    check("a row written without an agent has none", db.get_transaction(7, other)["agent"] is None)


# ── 5. The keeper's look ────────────────────────────────────────────────────────


def _state(address: str, epoch: int, window: int, spent: int, limit: int = 100 * USD) -> agents.AgentState:
    return agents.AgentState(address, limit, spent, max(limit - spent, 0), 2_000_000_000, True, epoch, window, spent)


def test_keeper_look():
    print("\n[5] The keeper's look: every trade made outside Mitfah, and none of Mitfah's own")
    chain, user = 56, 8
    told: list[tuple[int, int, str]] = []
    chain_now: dict[str, agents.AgentState | None] = {}
    code = {"wallet": b"\x01"}
    during_read: list = []
    saved = agents.module_for_wallet, agents.wallet_contract, agents.read_agent

    def read(module, wallet, agent, block_identifier="latest"):
        for hook in during_read:
            hook()
        return chain_now.get(agent)

    agents.module_for_wallet = lambda w3, wallet: "module"
    agents.wallet_contract = lambda w3, address: SimpleNamespace(address=address)
    agents.read_agent = read
    w3 = SimpleNamespace(eth=SimpleNamespace(get_code=lambda address: code["wallet"]))

    def look() -> dict:
        told.clear()
        return agents.check_agents(w3, chain, lambda u, c, text: told.append((u, c, text)))

    try:
        agent = Account.create().address
        db.add_agent_row(user, chain, WALLET, agent, "Trader", (1, 1_000, 10 * USD))

        chain_now[agent] = _state(agent, 1, 1_000, 10 * USD)
        out = look()
        check("nothing new: nobody is told", not told and out["checked"] == 1, str(out))

        chain_now[agent] = _state(agent, 1, 1_000, 25 * USD)
        look()
        check("spend no trade of Mitfah's explains: the owner is told, once",
              len(told) == 1 and told[0][:2] == (user, chain) and "$15.00 outside Mitfah" in told[0][2]
              and "$25.00 of its $100.00" in told[0][2] and "“Trader”" in told[0][2], str(told))
        look()
        check("...and not again", not told)

        db.add_relayed(chain, WALLET, agent, 1, 1_000, 4 * USD)
        db.add_relayed(chain, WALLET, agent, 1, 1_000, 6 * USD)
        chain_now[agent] = _state(agent, 1, 1_000, 35 * USD)
        look()
        check("spend Mitfah's own trades explain: nobody is told", not told, str(told))
        check("...and the account starts again from the chain",
              (lambda r: (int(r["seen_spent"]), r["relayed"]) == (35 * USD, "0"))(db.get_agent_row(chain, WALLET, agent)))

        db.add_relayed(chain, WALLET, agent, 1, 1_000, 5 * USD)
        chain_now[agent] = _state(agent, 1, 1_000, 42 * USD)
        look()
        check("Mitfah's trade and one made elsewhere: only the other is reported",
              len(told) == 1 and "$2.00 outside Mitfah" in told[0][2], str(told))

        tx = db.add_transaction(user, chain, WALLET, "agent", "Agent trade", "pending", user_op_hash="0x" + "44" * 32,
                                agent=agent)
        before = db.get_agent_row(chain, WALLET, agent)
        chain_now[agent] = _state(agent, 1, 1_000, 50 * USD)
        look()
        check("a trade of Mitfah's still landing: the look waits for it",
              not told and db.get_agent_row(chain, WALLET, agent)["seen_spent"] == before["seen_spent"])
        db.settle_transaction(tx, "confirmed", "0x" + "55" * 32, int(time.time()))
        db.add_relayed(chain, WALLET, agent, 1, 1_000, 8 * USD)
        look()
        check("...and once it is accounted for, says nothing", not told, str(told))

        chain_now[agent] = _state(agent, 1, 2_000, 7 * USD)
        look()
        check("spend in a later window none of Mitfah's trades reached: all of it is reported",
              len(told) == 1 and "$7.00 outside Mitfah" in told[0][2], str(told))

        db.add_relayed(chain, WALLET, agent, 1, 3_000, 3 * USD)
        chain_now[agent] = _state(agent, 1, 3_000, 3 * USD)
        look()
        check("a trade of Mitfah's that opened a new window explains it", not told, str(told))

        chain_now[agent] = _state(agent, 2, 3_000, 0)
        look()
        check("the agent removed and added again: accounted afresh, nobody told",
              not told and db.get_agent_row(chain, WALLET, agent)["seen_epoch"] == 2)

        chain_now[agent] = _state(agent, 2, 3_000, 9 * USD)
        during_read.append(lambda: db.add_relayed(chain, WALLET, agent, 2, 3_000, USD))
        look()
        during_read.clear()
        check("a trade of Mitfah's landing during the look: nothing is said, nothing is lost",
              not told and db.get_agent_row(chain, WALLET, agent)["relayed"] == str(USD))
        look()
        check("...the next look reports only what Mitfah's trade doesn't explain",
              len(told) == 1 and "$8.00 outside Mitfah" in told[0][2], str(told))

        chain_now[agent] = None
        out = look()
        check("an agent gone from the wallet is forgotten",
              out["gone"] == 1 and db.get_agent_row(chain, WALLET, agent) is None and not told, str(out))

        stale = Account.create().address
        db.add_agent_row(user, chain, WALLET, stale, None, (1, 0, 0))
        chain_now[stale] = _state(stale, 1, 0, 0)
        code["wallet"] = b""
        look()
        check("a wallet with no code (a fork restarted) leaves no rows behind", db.get_agent_row(chain, WALLET, stale) is None)
    finally:
        agents.module_for_wallet, agents.wallet_contract, agents.read_agent = saved


# ── 6. An agent's own calls ─────────────────────────────────────────────────────


def test_calls():
    print("\n[6] An agent's own calls: read, and described by code")
    to = "0x1111111111111111111111111111111111111111"
    parsed = agent_trades.parse_calls([
        {"to": to, "data": "0x095ea7b3" + "00" * 64, "value": "0"},
        {"to": to.upper().replace("0X", "0x"), "value": 5},
        {"to": to, "data": "", "value": "0x10"},
    ])
    check("calls read: address, value in wei (text, number or hex), calldata",
          [(t, v, len(d)) for t, v, d in parsed] == [(to, 0, 68), (to, 5, 0), (to, 16, 0)], str(parsed))
    for label, calls, text in [
        ("no calls", [], "at least one"),
        ("more than MAX_CALLS", [{"to": to}] * (agent_trades.MAX_CALLS + 1), "at most"),
        ("a `to` that isn't an address", [{"to": "bob"}], "Call 1: `to`"),
        ("calldata that isn't hex", [{"to": to}, {"to": to, "data": "0xzz"}], "Call 2: `data`"),
        ("a value that isn't a whole number", [{"to": to, "value": "1.5"}], "`value`"),
        ("a negative value", [{"to": to, "value": "-1"}], "negative"),
    ]:
        check(f"refused: {label}", raises(lambda calls=calls: agent_trades.parse_calls(calls), ToolException, text))

    usdc = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
    db.get_db().execute("INSERT OR REPLACE INTO supported_tokens (chain_id, ticker, address) VALUES (11155111, 'usdc', ?)",
                        (usdc,))
    db.get_db().commit()
    action = agent_trades.describe_calls(11155111, [
        (usdc, 0, bytes.fromhex("095ea7b3") + b"\x00" * 64),
        ("0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E", 0, bytes.fromhex("04e45aaf") + b"\x00" * 32),
        (to, 5 * 10**17, b""),
        (to, 0, bytes.fromhex("deadbeef")),
    ])
    check("described from the calls: listed tokens by ticker, common functions by name, value in ETH",
          action == "Agent trade: approve on USDC; exactInputSingle on 0x3bFA…e48E; send to 0x1111…1111 with 0.5 ETH; "
                    "0xdeadbeef on 0x1111…1111", action)


# ── 7. Out of the passkey flows ─────────────────────────────────────────────────


def _trade_request(user: int, chain: int, agent: str, *, expires_in: int = 300) -> tuple[dict, bytes]:
    """An agent trade's sign request, frozen as prepare_trade freezes one, with its digest."""
    quote = UserOpQuote("0x8dd7712f", 1, 1, 1, 1, 1, 1, 1, 1, 1, None, "sepolia")
    op = (WALLET, agent_ops.agent_nonce(MODULE), b"", bytes.fromhex("8dd7712f"), b"\x00" * 32, 1, b"\x00" * 32, b"", b"")
    final = FinalUserOp(op, keccak(text=f"op {time.time()}"), 1, 1)
    deadline = int(time.time()) + 600
    digest = agent_ops.operation_digest(MODULE, chain, final.user_op_hash, deadline, 1)
    frozen = {"action": "Agent trade: transfer on USDC", "calls": [], "cost": {}, "lp_pool": None, "direction": "out",
              "slippage_bps": None, "charged_usd": None, "quote": quote.__dict__, "agent": agent, "epoch": 1,
              "deadline": deadline, "module": MODULE, "wallet": WALLET, "app": "Bot"}
    trade_id = f"t-{keccak(text=str(time.time())).hex()[:10]}"
    request = db.open_sign_request(trade_id, user, chain, signing.AGENT, json.dumps(frozen), int(time.time()) + expires_in)
    sign_id = f"s-{trade_id}"
    db.save_sign_build(request["id"], sign_id, signing.op_json(final), "0x" + digest.hex())
    return db.get_sign_request(request["id"]), digest


def test_isolation():
    print("\n[7] An agent's trade is never a card, and no passkey page opens it")
    user, chain = 9, 11155111
    request, _ = _trade_request(user, chain, Account.create().address)
    check("it isn't among the requests waiting for a passkey", all(
        r["id"] != request["id"] for r in db.get_open_sign_requests(user, chain)))
    check("...though it is when asked for by its origin",
          [r["id"] for r in db.get_open_sign_requests(user, chain, signing.AGENT)] == [request["id"]])
    check("the web chat's card can't find it", raises(lambda: signing.find_by_quote(user, chain, request["quote_id"]),
                                                       signing.Expired))
    check("nor can the page, by its build", raises(lambda: signing.find_by_sign_id(request["sign_id"]), signing.Expired))
    check("a page can't build a passkey op for it", raises(lambda: signing.start(request), signing.Expired))
    check("nor have a passkey approve it", raises(lambda: signing.approve(request, {}), signing.Expired))


# ── 8. Sending: the agent's own signature, once ─────────────────────────────────


def test_send():
    print("\n[8] A trade is sent once, and only with the agent's own signature")
    user, chain = 10, 11155111
    db.save_wallet_address(user, chain, WALLET)
    agent = Account.create()
    request, digest = _trade_request(user, chain, agent.address)
    trade_id = request["quote_id"]
    sent: list = []
    saved = agent_trades._submit, agent_trades.landing.watch
    agent_trades._submit = lambda *args: (sent.append(args), SimpleNamespace(text="Sending — x", record=None))[1]
    agent_trades.landing.watch = lambda record, settle: None
    try:
        check("an unknown id: refused", raises(lambda: agent_trades.send_trade(user, "nope", "0x00"), ToolException,
                                                "No agent trade"))
        check("another account's trade: refused as unknown",
              raises(lambda: agent_trades.send_trade(user + 1, trade_id, "0x00"), ToolException, "No agent trade"))
        stranger = bytes(Account.create().unsafe_sign_hash(digest).signature)
        check("someone else's signature: refused, naming who made it",
              raises(lambda: agent_trades.send_trade(user, trade_id, stranger.hex()), ToolException, "not by the agent"))
        check("garbage: refused", raises(lambda: agent_trades.send_trade(user, trade_id, "0x1234"), ToolException,
                                         "can't be used"))
        check("...and the trade still waits for the right one", db.get_sign_request(request["id"])["status"] == OPEN)

        good = bytes(agent.unsafe_sign_hash(digest).signature)
        result = agent_trades.send_trade(user, trade_id, "0x" + good.hex())
        check("the agent's signature sends it", result["status"] == "sending" and len(sent) == 1, str(result))
        packed = sent[0][4]
        check("...signed as the wallet reads it: agent, epoch, deadline, then the signature",
              len(packed) == 95 and packed[:20] == bytes.fromhex(agent.address[2:]) and packed[30:] == good)
        check("...and its request is closed as sent", db.get_sign_request(request["id"])["status"] == SENT)
        check("a second send is refused", raises(lambda: agent_trades.send_trade(user, trade_id, good.hex()),
                                                  ToolException, "already sent"))
        check("...and nothing went out twice", len(sent) == 1)

        failing, digest2 = _trade_request(user, chain, agent.address)

        def refuse(*_args):
            raise ToolException("This trade would fail: AgentModule_AgentLimitExceeded")

        agent_trades._submit = refuse
        check("a trade that would fail now: refused with the reason",
              raises(lambda: agent_trades.send_trade(user, failing["quote_id"], agent.unsafe_sign_hash(digest2).signature.hex()),
                     ToolException, "AgentLimitExceeded"))
        closed = db.get_sign_request(failing["id"])
        check("...and closed as failed, with it", closed["status"] == FAILED and "AgentLimitExceeded" in closed["outcome"])

        expired, digest3 = _trade_request(user, chain, agent.address, expires_in=-1)
        check("after its time: refused, nothing sent",
              raises(lambda: agent_trades.send_trade(user, expired["quote_id"], agent.unsafe_sign_hash(digest3).signature.hex()),
                     ToolException, "expired"))
    finally:
        agent_trades._submit, agent_trades.landing.watch = saved


# ── 9. Reasons ──────────────────────────────────────────────────────────────────


def test_reasons():
    print("\n[9] Why a trade is refused, in words")
    limit = keccak(text="AgentModule_AgentLimitExceeded(uint256,uint256)")[:4] + abi_encode(["uint256", "uint256"],
                                                                                          [150 * USD, 100 * USD])
    text = contract_errors.describe_revert_data(limit)
    check("over the agent's own limit", "past its own limit" in text and "$150.00" in text and "$100.00" in text, text)
    token = "0x2222222222222222222222222222222222222222"
    uncounted = keccak(text="AgentModule_UncountedToken(address,bytes4)")[:4] + abi_encode(
        ["address", "bytes4"], [token, bytes.fromhex("a9059cbb")])
    text = contract_errors.describe_revert_data(uncounted)
    check("a token the limit doesn't count", token in text and "a9059cbb" in text and "doesn't count it" in text, text)
    native = keccak(text="AgentModule_NativeNotMetered(address)")[:4] + abi_encode(["address"], [WALLET])
    check("the native asset left out of the limit",
          contract_errors.describe_revert_data(native) == contract_errors._AGENT_NATIVE_TEXT)
    unknown = keccak(text="AgentModule_UnknownAgent(address,address)")[:4] + abi_encode(["address", "address"], [WALLET, token])
    check("any other of the module's errors is named",
          contract_errors.describe_revert_data(unknown) == "AgentModule_UnknownAgent(address,address)")


# ── 10. The database, and renaming ──────────────────────────────────────────────


def test_schema_and_rename():
    print("\n[10] Migration 13, and renaming an agent")
    conn = db.get_db()
    check("a fresh database runs every migration", db.schema_version(conn) == len(db.MIGRATIONS) == 13)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    check("agents keeps names and accounts",
          {"user_id", "chain_id", "wallet", "address", "name", "seen_epoch", "seen_window", "seen_spent", "relayed"}
          <= columns, str(columns))
    check("transactions says which agent", "agent" in {r[1] for r in conn.execute("PRAGMA table_info(transactions)")})

    api.limiter.enabled = False
    client = TestClient(api.app)
    _, headers, _ = sign_in(client)
    user = client.get("/api/me", headers=headers).json()["user_id"]
    wallet = to_checksum_address("0x00000000000000000000000000000000000000cd")
    db.save_wallet_address(user, 1, wallet)
    agent = Account.create().address
    db.add_agent_row(user, 1, wallet, agent, None, (1, 0, 0))

    def rename(name, who=agent, chain=1):
        return client.post("/api/wallet/agents/rename", headers=headers, json={"chain_id": chain, "agent": who, "name": name})

    r = rename("  Market   maker ")
    check("renamed, its words single-spaced", r.status_code == 200 and r.json()["name"] == "Market maker", r.text)
    check("...in the database", db.get_agent_row(1, wallet, agent)["name"] == "Market maker")
    check("an agent not on record: 404", rename("x", who=Account.create().address).status_code == 404)
    check("a chain with no wallet: 404", rename("x", chain=56).status_code == 404)
    check("control characters: 400", rename("a\nb").status_code == 400)
    check("a name of spaces: 400", rename("   ").status_code == 400)
    check("too long: 422", rename("x" * 41).status_code == 422)
    check("signed out: 401", client.post("/api/wallet/agents/rename", json={"chain_id": 1, "agent": agent, "name": "x"})
          .status_code == 401)


if __name__ == "__main__":
    test_message()
    test_signatures()
    test_shape()
    test_accounts()
    test_keeper_look()
    test_calls()
    test_isolation()
    test_send()
    test_reasons()
    test_schema_and_rename()
    finish("All trading-agent checks passed.")
