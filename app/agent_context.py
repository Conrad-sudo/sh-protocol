from dataclasses import dataclass


@dataclass
class AgentContext:
    """
    Per-invocation context the agent runtime hands to tools, carrying who the request is for.

    This exists so the user's identity reaches the tools WITHOUT passing through the model. It used
    to travel as a `chat_id` argument on every @tool, filled in by the model from a marker prepended
    to the user's message -- which made identity something the conversation could talk the agent
    into changing. Fields here are supplied by the caller of `chat()` and injected by LangChain into
    any tool that declares a `ToolRuntime` parameter, so they never appear in the schema the model
    fills in and a model-supplied `user_id` is ignored.

    Lives in its own module rather than in smart_wallet_agent.py because tools.py needs it and
    smart_wallet_agent.py already imports tools.py -- defining it there would be a circular import.
    """

    user_id: int
