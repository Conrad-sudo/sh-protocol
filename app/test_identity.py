"""
Guards the one rule that makes the agent multi-user safe: the model must never be able to choose
whose wallet a tool acts on.

Identity used to be a `chat_id` argument on every @tool, filled in by the model from a marker
prepended to the user's message. Anything that puts it back into a tool's argument schema -- a new
tool written to the old pattern, a refactor that "restores" the parameter -- reopens an
account-takeover path that no other test in this repo would notice. Hence this file.

Run: make identity-test   (or: python app/test_identity.py)
"""
import sys
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from agent_context import AgentContext
from tools import get_tools

# Names that must never appear in a tool's model-visible argument schema.
FORBIDDEN_ARGS = ("user_id", "chat_id", "runtime")

failures: list[str] = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")
        failures.append(label)


class StubModel(GenericFakeChatModel):
    """A model that emits scripted tool calls. bind_tools is a no-op so the agent will accept it."""

    def bind_tools(self, tools, **kwargs):
        return self


def test_no_tool_exposes_identity():
    """
    Every exported tool's schema must be free of the identity.

    This is the regression guard proper: it fails the moment somebody adds a tool that takes the
    id as an argument, which is what the whole refactor removed.
    """
    print("\n[1] no exported tool exposes identity in its model-visible schema")
    tools = get_tools()
    check("get_tools() returned tools", len(tools) > 0, f"got {len(tools)}")

    leaked = []
    for t in tools:
        params = convert_to_openai_tool(t)["function"]["parameters"]
        for name in params.get("properties", {}):
            if name in FORBIDDEN_ARGS:
                leaked.append(f"{t.name}.{name}")
    check(
        f"none of the {len(tools)} tools exposes {'/'.join(FORBIDDEN_ARGS)}",
        not leaked,
        f"leaked: {leaked}",
    )


def test_context_beats_model_supplied_id():
    """
    A model that supplies the id anyway must be ignored.

    Covers a tool WITH default arguments, because those are the ones where `runtime` had to be the
    first parameter (Python forbids a non-default parameter after a defaulted one) -- the position
    most likely to be "tidied" later.
    """
    print("\n[2] runtime context wins over a model-supplied id")
    seen = []

    @tool
    def spend(runtime: ToolRuntime[AgentContext], token: str, slippage_bps: int = 50) -> str:
        """Spend a token."""
        seen.append((runtime.context.user_id, token, slippage_bps))
        return "done"

    schema = convert_to_openai_tool(spend)["function"]["parameters"]["properties"]
    check("the injected parameter is absent from the schema", "runtime" not in schema)
    check("the identity is absent from the schema", "user_id" not in schema)
    check("real arguments survive", set(schema) == {"token", "slippage_bps"}, str(set(schema)))

    # The model tries to act as user 999; the context says 42.
    messages = iter([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "spend",
                "args": {"token": "usdc", "user_id": 999},
                "id": "call_1",
            }],
        ),
        AIMessage(content="ok"),
    ])
    agent = create_agent(
        model=StubModel(messages=messages), tools=[spend], context_schema=AgentContext
    )
    agent.invoke(
        {"messages": [HumanMessage(content="spend some usdc")]},
        context=AgentContext(user_id=42),
    )

    check("the tool ran", len(seen) == 1, f"ran {len(seen)} times")
    if seen:
        observed_user, token, slippage = seen[0]
        check("the tool saw the CONTEXT id, not the model's", observed_user == 42, f"saw {observed_user}")
        check("the model's arguments still arrived", (token, slippage) == ("usdc", 50))


def test_no_tool_writes_the_contact_list():
    """
    The agent must not be able to add a payee.

    The contact list is the allowlist of destinations for the wallet's funds: _resolve_contact
    accepts a saved name and refuses a raw address, so a tool that could WRITE that list would
    hand whoever holds the chat surface the ability to name themselves as the recipient -- an
    unlocked stolen phone being the concrete case. Changing the list therefore belongs to the
    authenticated web session (POST / DELETE /api/contacts), and this fails if either write
    comes back.

    Deletion is guarded too, though it is not a theft vector on its own -- it only shrinks the
    allowlist. The rule being protected is the simple one: the agent reads this list and never
    writes it. A guard that permitted "destructive writes only" would be the kind of subtlety
    that erodes.

    Both halves of the boundary are checked, because either alone is a hole: a write tool is
    harmless only while names are the sole accepted destination, and the name-only rule is
    worthless the moment a write tool exists.
    """
    print("\n[3] the agent cannot add a payee")
    import db
    import tools

    names = {t.name for t in get_tools()}
    check("no contact-writing tool is exported",
          not (names & {"save_contact", "add_contact", "set_contact", "update_contact",
                        "delete_contact", "remove_contact"}),
          str(sorted(names)))

    # Stronger than a name check: neither db writer may be bound in the tools module AT ALL, under
    # any alias, so nothing there can reach one even indirectly.
    for label, fn in (("save_contact", db.save_contact), ("delete_contact", db.delete_contact)):
        aliases = [n for n, v in vars(tools).items() if v is fn]
        check(f"db.{label} is not reachable from tools.py", not aliases, f"bound as {aliases}")

    # The complementary half. If _resolve_contact ever starts accepting addresses, removing the
    # write tool buys nothing -- the model could just name the address directly.
    from langchain_core.tools import ToolException

    original = tools._get_contact
    tools._get_contact = lambda user_id, name: None      # no DB access; every name is unsaved
    try:
        tools._resolve_contact(1, "0x70997970C51812dc3A010C7d01b50e0d17dc79C8")
        check("a raw address is refused as a recipient", False, "it was accepted")
    except ToolException:
        check("a raw address is refused as a recipient", True)
    finally:
        tools._get_contact = original


def test_thread_id_is_per_chain():
    """
    Conversation history is keyed per user PER CHAIN.

    A Telegram user id is the same number for every bot, so a user-only key would merge every
    per-chain bot's conversation into one history.
    """
    print("\n[4] thread ids are scoped per chain")
    from smart_wallet_agent import thread_id

    check("same user, different chains -> different threads",
          thread_id(1, 31337) != thread_id(1, 42161))
    check("same user and chain -> stable", thread_id(1, 31337) == thread_id(1, 31337))
    check("different users -> different threads", thread_id(1, 31337) != thread_id(2, 31337))


if __name__ == "__main__":
    test_no_tool_exposes_identity()
    test_context_beats_model_supplied_id()
    test_no_tool_writes_the_contact_list()
    test_thread_id_is_per_chain()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print("All identity guards passed.")
