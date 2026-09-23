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
    # Which turn of the conversation this is: a counter the caller of chat() bumps once per user
    # message, never something the model can set or see. It exists so confirm_transaction can tell
    # "the user replied to the quote" from "the same turn that raised the quote is now confirming
    # it" -- the turn only advances when a real message arrives, so text the model merely READ
    # (a tool result, an on-chain string, a registration file) cannot quote and send in one go.
    # See app/quotes.py.
    turn_id: int = 0
