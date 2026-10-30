"""
Checks the agent's conversation store (chat_memory.ChatMemory): a turn is saved once, as it ends,
and one that fails is saved as it stood; many turns at once on their own threads keep every
conversation whole; and two processes on the one file -- the API and the Telegram bot -- see each
other's writes. The agent's graph is the real one, with a stand-in for the model. Offline.

Run: make chat-memory-test   (or: python app/tests/test_chat_memory.py)
"""
import os
import tempfile
import threading
import uuid

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

# A scratch wallet.db, set before db is imported: the conversations' file is named after it.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["APP_DB_PATH"] = _tmp_db.name
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")

import db                                                     # noqa: E402
db.init_db()

from langchain.agents import create_agent                     # noqa: E402
from langchain_core.language_models import BaseChatModel      # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool                         # noqa: E402

import smart_wallet_agent                                     # noqa: E402
from chat_memory import ChatMemory                            # noqa: E402

THREADS = 16
TURNS = 5
FAIL = "fail on this one"


@tool
def look_up(question: str) -> str:
    """Looks the question up (a stand-in tool)."""
    return f"found: {question}"


class TwoStepModel(BaseChatModel):
    """
    A turn as the agent's usually go: a tool call, then a reply. Stateless, with a fresh message
    every call, so any number of threads can share it. Raises on FAIL, as a model outage would.
    """

    @property
    def _llm_type(self) -> str:
        return "two-step"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if isinstance(last, HumanMessage):
            if last.content == FAIL:
                raise RuntimeError("the model is down")
            message = AIMessage(content="", tool_calls=[{"name": "look_up", "args": {"question": last.content},
                                                         "id": f"call-{uuid.uuid4().hex}", "type": "tool_call"}])
        else:
            message = AIMessage(content=f"Done, {last.content}")
        return ChatResult(generations=[ChatGeneration(message=message)])


memory = ChatMemory(db.CHAT_DB_PATH)
agent = create_agent(TwoStepModel(), tools=[look_up], checkpointer=memory)


def _config(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


def _turn(thread: str, text: str, durability: str = smart_wallet_agent.DURABILITY):
    return agent.invoke({"messages": [HumanMessage(content=text)]}, config=_config(thread), durability=durability)


def _messages(thread: str, store=None) -> list:
    graph = agent if store is None else create_agent(TwoStepModel(), tools=[look_up], checkpointer=store)
    return graph.get_state(_config(thread)).values.get("messages", [])


def _saved(thread: str) -> int:
    """How many checkpoints the thread has in the file."""
    with memory.cursor(transaction=False) as cur:
        return cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread,)).fetchone()[0]


def test_the_file():
    print("\n[1] the conversations have a file of their own, beside wallet.db")
    check("named after it", db.CHAT_DB_PATH == os.path.splitext(db.DB_PATH)[0] + "-chat.db", db.CHAT_DB_PATH)
    tables = {r[0] for r in db.get_db().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    check("and wallet.db holds none of them", not {"checkpoints", "writes"} & tables, str(sorted(tables)))


def test_saved_once():
    print("\n[2] a turn is saved once, as it ends")
    _turn("once", "what is the cap?")
    kinds = [type(m).__name__ for m in _messages("once")]
    check("the whole turn is there", kinds == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"], str(kinds))
    check("in one checkpoint", _saved("once") == 1, str(_saved("once")))
    _turn("once", "and the fee?")
    check("one more for the next turn", _saved("once") == 2 and len(_messages("once")) == 8, str(_saved("once")))
    _turn("every-step", "what is the cap?", durability="async")
    check(f"where saving after every step took {_saved('every-step')}", _saved("every-step") > 1)


def test_failed_turn():
    print("\n[3] a turn that fails is saved as it stood")
    for thread, durability in (("failed", smart_wallet_agent.DURABILITY), ("failed-every-step", "async")):
        _turn(thread, "what is the cap?", durability)
        try:
            _turn(thread, FAIL, durability)
            raised = False
        except RuntimeError:
            raised = True
        check(f"the model's error reaches the caller ({durability})", raised)
    saved = [m.content for m in _messages("failed")]
    check("the message that failed is kept, after the turn before it",
          len(saved) == 5 and saved[-1] == FAIL, str(saved))
    check("the same as saving after every step keeps",
          saved == [m.content for m in _messages("failed-every-step")])


def test_turns_at_once():
    print(f"\n[4] {THREADS} conversations at once, each on its own thread")
    errors, connections = [], {}

    def converse(n: int):
        try:
            connections[n] = memory.conn  # held, so no id is reused before the count below
            for turn in range(TURNS):
                _turn(f"busy-{n}", f"question {turn} from {n}")
        except Exception as e:  # noqa: BLE001 -- reported below
            errors.append(f"{n}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=converse, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("every turn finished", not errors, "; ".join(errors[:3]))
    check("no two threads shared a connection", len({id(c) for c in connections.values()}) == THREADS)
    whole = all(
        [m.content for m in _messages(f"busy-{n}") if isinstance(m, HumanMessage)]
        == [f"question {turn} from {n}" for turn in range(TURNS)]
        and len(_messages(f"busy-{n}")) == 4 * TURNS
        and isinstance(_messages(f"busy-{n}")[-2], ToolMessage)
        for n in range(THREADS)
    )
    check("each conversation is whole, in order, and nobody else's", whole)
    check("one checkpoint a turn", all(_saved(f"busy-{n}") == TURNS for n in range(THREADS)))


def test_two_processes():
    print("\n[5] two stores on the one file, as the API and the bot are, see each other's writes")
    other = ChatMemory(db.CHAT_DB_PATH)
    _turn("shared", "hello from the web")
    check("the other reads what this one wrote", len(_messages("shared", other)) == 4)
    other.delete_thread("shared")
    check("and a delete there is seen here", _messages("shared") == [])


if __name__ == "__main__":
    try:
        test_the_file()
        test_saved_once()
        test_failed_turn()
        test_turns_at_once()
        test_two_processes()
    finally:
        for name in (_tmp_db.name, db.CHAT_DB_PATH):
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(name + suffix):
                    os.unlink(name + suffix)

    finish("All chat memory checks passed.")
