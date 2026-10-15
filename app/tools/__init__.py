"""
The wallet agent's tools, one module per area:

  common        what every tool shares: amounts, token and contact resolution, the session key
  checks        the wallet checks every transaction tool runs itself, and preflight_check
  quoting       turning a plan into a quote the user approves
  sending       confirm_transaction (the only tool that sends) and cancel_transaction
  wallet_reads  status, balances, prices, the token list and the contact list
  transfers     payments to saved contacts, and wrapping the native asset
  uniswap       swaps on either exchange, and PancakeSwap V2's liquidity (BSC)
  positions     Uniswap V3's liquidity positions (Ethereum, Sepolia, Arbitrum, Base)
  erc8004       identity and reputation

get_tools() below is the agent's whole toolset. Only one exchange's liquidity tools apply on a
chain: LIQUIDITY_TOOLS names each set, smart_wallet_agent shows the model only its chain's, and
every one of them also refuses on the wrong chain.

db.save_contact and db.delete_contact are deliberately NOT imported here. The contact list is the
allowlist of destinations for value (see _resolve_contact), so the agent READS it and never writes
it in either direction; both writes belong to the authenticated web session (POST and DELETE
/api/contacts). Leaving the imports out is itself part of the guard -- no tool can call what the
module never bound -- and test_identity asserts that neither function is reachable under any alias.
"""
from langchain_core.tools import ToolException

from redact import redact
from tools.checks import (
    preflight_check,
)
from tools.erc8004 import (
    agent_exists,
    append_response,
    get_agent_feedback,
    get_agent_identity,
    get_agent_metadata,
    get_agent_reputation,
    get_feedback_clients,
    get_last_feedback_index,
    get_registry_info,
    get_response_count,
    give_feedback,
    list_all_feedback,
    post_reputation_feedback,
    revoke_feedback,
    verify_agent_endpoint,
)
from tools.positions import (
    add_to_position,
    collect_fees,
    get_positions,
    open_position,
    suggest_price_ranges,
    withdraw_from_position,
)
from tools.sending import (
    cancel_transaction,
    confirm_transaction,
)
from tools.transfers import (
    send_eth,
    transfer_erc20,
    transferFrom_erc20,
    wrap_eth,
)
from tools.uniswap import (
    add_liquidity,
    get_liquidity_token_balance,
    get_lp_amounts,
    get_pool_quote,
    get_quote_in,
    get_quote_out,
    remove_liquidity,
    swap,
)
from tools.wallet_reads import (
    get_all_contacts,
    get_contact,
    get_erc20_balance,
    get_eth_balance,
    get_price,
    get_supported_tokens,
    get_wallet_status,
)


# Each exchange's liquidity tools, by constants.get_exchange's name for it. The agent is built once
# with every tool, and smart_wallet_agent hides the set that doesn't apply on the chain a turn runs
# on (both, where there is no exchange). Two sets with their own names, because the tool node needs
# every name to be unique; a liquidity tool added to either module belongs here too.
LIQUIDITY_TOOLS = {
    "uniswap-v3": frozenset({
        "suggest_price_ranges", "open_position", "add_to_position", "get_positions",
        "withdraw_from_position", "collect_fees",
    }),
    "pancakeswap-v2": frozenset({
        "add_liquidity", "remove_liquidity", "get_pool_quote", "get_lp_amounts", "get_liquidity_token_balance",
    }),
}


def get_tools():
    tools_list = [
        # Database tools
        get_supported_tokens,
        get_wallet_status,
        # save_contact and delete_contact are absent by design — writing the contact list is an
        # owner action, reachable only from an authenticated web session (POST and DELETE
        # /api/contacts). See the note above get_contact, and _resolve_contact for why the list is
        # a security boundary. The agent reads this list; it never writes it.
        get_contact,
        get_all_contacts,
        # Blockchain tools
        get_eth_balance,
        get_erc20_balance,
        get_price,
        # For questions only: every write tool below runs the same checks itself before it quotes.
        preflight_check,
        # The only tool that sends anything, and its counterpart. Every other write tool stops at
        # a quote -- see quotes.py for why the sending step is separate and turn-gated.
        confirm_transaction,
        cancel_transaction,
        send_eth,
        transfer_erc20,
        transferFrom_erc20,
        wrap_eth,
        # The chain's exchange: previews for questions, then the writes. One swap tool picks the
        # router's function from which side is the native asset and which amount the user fixed,
        # on Uniswap V3 and PancakeSwap V2 alike, and the liquidity tools take the native asset as
        # their default second token.
        get_quote_in,
        get_quote_out,
        swap,
        # PancakeSwap V2's liquidity (BSC): LP tokens.
        get_pool_quote,
        get_lp_amounts,
        get_liquidity_token_balance,
        add_liquidity,
        remove_liquidity,
        # Uniswap V3's liquidity (Ethereum, Sepolia, Arbitrum, Base): positions with a price range.
        suggest_price_ranges,
        open_position,
        add_to_position,
        get_positions,
        withdraw_from_position,
        collect_fees,
        # ERC-8004 tools — every one delegates to langchain-erc8004 (see toolkits.py).
        #
        # Two groups are deliberately absent, on the same principle as _BLOCKED_TOOLS in
        # toolkits.py: a tool that cannot succeed on this wallet is worse than no tool, because
        # a model will still reach for it.
        #
        #   * The seven Validation Registry tools. That registry has no canonical deployment on
        #     any chain and this protocol deploys none, so the toolkit itself withholds them.
        #   * The eight agent-IDENTITY writes (register_agent, parse_registration_receipt,
        #     set_agent_uri, set_agent_metadata, transfer_agent, build_agent_wallet_typed_data,
        #     set_agent_wallet, unset_agent_wallet), and resolve_registration_file, which exists to
        #     check a file before registering one. This protocol registers ONE agent at deploy
        #     time, owned by the operator's key and shared by every wallet; a user's
        #     SessionHandler neither owns it nor can be given it (no onERC721Received fallback
        #     handler, so any mint or safeTransferFrom to the account reverts). Changing that
        #     identity is protocol governance, not a wallet action, so no wrapper exists for any
        #     of them (removed 2026-10-08; git history has them). A deployment whose wallets do own
        #     agents would wrap them like the reputation writes, through _erc8004 and
        #     _quote_registry_plan, and should refuse any write aimed at SHRegistry.agentId.
        #
        # get_agent_identity returns the owner, the agent wallet and the URI, so there is no
        # separate read for each; get_agent_reputation is the average to show, and
        # get_agent_feedback with one reviewer lists each of their entries.
        #
        # identity reads
        get_registry_info,
        get_agent_identity,
        agent_exists,
        get_agent_metadata,
        verify_agent_endpoint,
        # reputation reads
        get_feedback_clients,
        get_agent_feedback,
        list_all_feedback,
        get_last_feedback_index,
        get_response_count,
        get_agent_reputation,
        # reputation writes
        post_reputation_feedback,
        give_feedback,
        revoke_feedback,
        append_response,
    ]

    for t in tools_list:
        t.handle_tool_error = _tool_error_text
    return tools_list


def _tool_error_text(e: ToolException) -> str:
    """
    What the model reads when a tool refuses: the refusal's own text, as `handle_tool_error = True`
    would give it, with any key masked. A refusal can quote a node's error, and a node's error names
    its URL -- key and all -- while the model is a third-party service.
    """
    return redact(e.args[0] if e.args else "Tool execution error")
