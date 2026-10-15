"""
Payments to saved contacts, and wrapping the native asset. Each quotes; confirm_transaction sends.
"""
from langchain.tools import ToolRuntime, tool

from agent_context import AgentContext
from constants import (
    get_native_asset_ticker,
    get_native_wrapped_ticker,
)
from contracts import (
    load_session_handler,
)
from network_config import load_network_config
from toolkits import get_erc20_tools
from tools.common import (
    RECEIVED,
    SENT,
    _limit_note,
    _resolve_contact,
    _to_base_units,
    _token_address,
)
from tools.quoting import _quote_executions, _quote_plan


@tool
def send_eth(
    runtime: ToolRuntime[AgentContext], recipient: str, amount_eth: float
):
    """
    Sends the chain's native gas asset (ETH on Ethereum, Sepolia, Arbitrum and Base, BNB on BSC) to a
    named contact. This works identically on every supported network — "eth" in the
    tool/parameter names is a generic internal label, not a claim that the network is Ethereum.
    Never refuse this request just because the network isn't Ethereum: the quote's `action` names
    the asset.

    Use this tool when the user wants to send their native asset (ETH, BNB) to someone.
    The recipient must already be saved as a contact; contacts are added in the web app, not here.
    Specify the amount in whole native-asset units (e.g. 1.5), not in wei. The fees are paid in the
    native asset too, so the wallet can't send its whole balance: if it can't cover the amount and
    the fees, the tool refuses and says so.

    Args:
        recipient: The name of the contact to send to (e.g. "Sandy"). Must be a saved contact.
        amount_eth: The amount of the native asset to send, in whole units (e.g. 1.5).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running send_eth")
    _, chain_id, _ = load_network_config(user_id)
    recipient_addr = _resolve_contact(user_id, recipient)
    value = _to_base_units(amount_eth, 18)

    # No package plan behind a bare native send, so the action line is written here -- from the
    # RESOLVED address, not the name, so what the user approves names where the value goes.
    return _quote_executions(
        runtime,
        [(recipient_addr, value, b"")],
        f"Send {amount_eth} {get_native_asset_ticker(chain_id)} to {recipient} ({recipient_addr})",
        legs=[("eth", amount_eth, SENT)],
    )


@tool
def wrap_eth(runtime: ToolRuntime[AgentContext], amount_eth: float):
    """
    Wraps native ETH/BNB into the chain's wrapped-native token (WETH on Ethereum, WBNB on
    BSC) by calling deposit() on that contract.

    Use this tool when the user wants to convert native ETH/BNB to its wrapped form. This
    does not go through the router — it is a direct 1:1 wrap.

    Args:
        amount_eth: The amount of native ETH/BNB to wrap, in whole units (e.g. 1.5).
                    The tool converts this to wei internally before sending the transaction.

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running wrap_eth")
    _, chain_id, _ = load_network_config(user_id)
    plan = get_erc20_tools(user_id)["wrap_native"].invoke(
        {
            "from_address": load_session_handler(user_id).address,
            # str, not float: the package parses amounts as Decimal, and a float cannot
            # represent 18 decimal places exactly.
            "amount": str(amount_eth),
        }
    )
    return _quote_plan(
        runtime,
        plan,
        legs=[("eth", amount_eth, SENT), (get_native_wrapped_ticker(chain_id), amount_eth, RECEIVED)],
    )


@tool
def transfer_erc20(
    runtime: ToolRuntime[AgentContext], token: str, recipient: str, amount: float
):
    """
    Transfers ERC20 tokens to a named contact.

    Use this tool when the user wants to send tokens to someone. The recipient must
    already be saved as a contact; contacts are added in the web app, not here. Specify the
    amount in whole token units (e.g. 100 for 100 USDC), not in raw base units.

    Args:
        token: The token ticker symbol to transfer (e.g. "usdc").
        recipient: The name of the contact to send tokens to (e.g. "Sandy").
                   Must be a saved contact.
        amount: The amount of tokens to send in whole units (e.g. 100 for 100 USDC).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id
    print("Running transfer_erc20")
    recipient_addr = _resolve_contact(user_id, recipient)
    plan = get_erc20_tools(user_id)["transfer"].invoke(
        {
            "token": _token_address(user_id, token),
            "to": recipient_addr,
            "from_address": load_session_handler(user_id).address,
            "amount": str(amount),
        }
    )
    return _quote_plan(runtime, plan, details=_limit_note(user_id, token), legs=[(token, amount, SENT)])


@tool
def transferFrom_erc20(
    runtime: ToolRuntime[AgentContext],
    token: str,
    sender: str,
    recipient: str,
    amount: float,
):
    """
    Transfers ERC20 tokens from a sender to a recipient, on the sender's approval.

    Use this tool when the user wants to move tokens out of a contact's account (the sender) to a
    recipient. It works only if the sender has already approved this wallet to spend at least
    `amount` of the token, which they do from their own wallet; the tool checks that approval and
    the sender's balance, and refuses with the reason if either is short. The sender and recipient
    must already be saved as contacts; contacts are added in the web app, not here. Specify the
    amount in whole token units (e.g. 100 for 100 USDC), not in raw base units.

    Args:
        token: The token ticker symbol to transfer (e.g. "usdc").
        sender: The name of the contact who is the sender of the tokens (e.g. "Sandy"). Must be a saved contact.
        recipient: The name of the contact who is the recipient of the tokens (e.g. "Alex"). Must be a
                   saved contact. Exception: if the user names themselves or the wallet as the
                   recipient (e.g. "to me", "to my wallet"), pass the literal string "me".
        amount: The amount of tokens to transfer in whole units (e.g. 100 for 100 USDC).

    Returns:
        A QUOTE — NOTHING IS SENT. Show it to the user as its `next_step` says, then wait for
        their reply.
    """
    user_id = runtime.context.user_id

    print("Running transferFrom_erc20")
    wallet = load_session_handler(user_id).address
    sender_addr = _resolve_contact(user_id, sender, role="sender")
    # _resolve_contact maps "me" to the wallet, which is this tool's documented convention
    # for the user naming themselves as the recipient.
    recipient_addr = _resolve_contact(user_id, recipient)

    plan = get_erc20_tools(user_id)["transfer_from"].invoke(
        {
            "token": _token_address(user_id, token),
            "owner": sender_addr,
            "to": recipient_addr,
            "from_address": wallet,
            "amount": str(amount),
        }
    )
    # The limit counts only what leaves the wallet. Moving a contact's tokens on their approval
    # leaves the wallet's balance alone (or adds to it, when the recipient is "me"), so then there
    # is only the pause and the key to check.
    legs = [(token, amount, SENT)] if sender_addr.lower() == wallet.lower() else None
    return _quote_plan(runtime, plan, legs=legs)
