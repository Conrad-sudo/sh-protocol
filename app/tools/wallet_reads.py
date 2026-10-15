"""
Read-only tools: the wallet's status and balances, prices, the token list and the contact list.
"""
import time

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import ToolException

from agent_context import AgentContext
from constants import (
    ETH_SENTINEL,
    WEI_PER_ETH,
    get_native_asset_ticker,
)
from contracts import (
    load_ierc20,
    load_session_handler,
    read_spending_config,
)
from db import (
    get_all_contacts as _get_all_contacts,
)
from db import (
    get_contact as _get_contact,
)
from db import (
    get_custom_tokens as _get_custom_tokens,
)
from db import (
    get_supported_tokens as _get_supported_tokens,
)
from network_config import load_network_config
from parallel import read_all
from toolkits import get_erc20_tools
from tools.common import (
    _addresses_to_tickers,
    _get_session_keys,
    _resolve_contact,
    _token_address,
    _unlisted_label,
)

"""
 /*//////////////////////////////////////////////////////////////
                        DATABASE TOOLS
//////////////////////////////////////////////////////////////*/
"""
@tool
def get_supported_tokens(runtime: ToolRuntime[AgentContext]) -> dict:
    """
    Lists the tokens this wallet can use on the user's current network.

    Use it when the user asks which tokens they can use, or to check a ticker. There is no need to
    call it before a transaction: every tool refuses a token the wallet doesn't know, and says so.

    Args:

    Returns:
        A dict with:
          - native (str): the network's native asset (e.g. "ETH", or "BNB" on BSC). Tool arguments
            call it "eth" on every network.
          - listed (list[str]): tickers Mitfah lists on this network (e.g. ["dai", "usdc", "weth"]).
            These have a price, and the ones on the wallet's watched list count toward the
            spending limit.
          - custom (list[str]): tickers the USER added themselves in the web app. These have NO
            price and NEVER count toward the spending limit. They can be sent, swapped and checked
            like any other token. Never state or guess a dollar value for one.
    """
    user_id = runtime.context.user_id
    print("Running get_supported_tokens")
    _, chain_id, _ = load_network_config(user_id)
    return {
        "native": get_native_asset_ticker(chain_id),
        "listed": _get_supported_tokens(user_id),
        "custom": [t["ticker"] for t in _get_custom_tokens(user_id, chain_id)],
    }


@tool
def get_wallet_status(runtime: ToolRuntime[AgentContext]) -> dict:
    """
    Reports the wallet's spending limit, the assistant's session key, and whether the wallet is
    paused.

    The wallet has ONE session key for every action and ONE USD spending limit per window, shared
    by every token (there are no per-token limits). The key runs out at a set time -- 30 days after
    it was granted by default, 90 at most -- and only the owner can renew it, in the web app
    (Controls → Renew). Use this when the user asks about their limit, what they have spent or
    have left, their session key, or whether the wallet is paused.

    Args:

    Returns:
        A dict with:
          - paused (bool): whether the owner has paused the wallet. A paused wallet rejects every
            transaction until the owner unpauses it in the web app.
          - session_active (bool): whether the session key can sign right now. False once it has
            run out or been revoked.
          - session_expires_at (int): when the key runs out, in Unix seconds.
          - session_expires_in_secs (int): how long that is from now; 0 once it has run out.
          - daily_limit_usd (float): the spending limit per window, in USD.
          - spent_usd (float): what has counted toward it in the current window, in USD.
          - remaining_usd (float): what is left of it, in USD. 0 when the owner has lowered the
            limit below what was already spent.
          - window_hours (float): the window's length, in hours.
          - watched_tokens (list): the ERC20 tickers that count toward the limit. The native asset
            (ETH/BNB) ALWAYS counts too and is not on this list; other tokens don't count.
          - allowlist_enabled (bool): whether the owner has limited the assistant to a list of
            contracts (web app, Controls → Advanced). While it is on, a transaction that calls a
            contract not on the list is refused; sending to a plain wallet address is not affected.
    """
    return _get_wallet_status(runtime.context.user_id)


def _get_wallet_status(user_id: int) -> dict:
    """
    Returns the wallet's spending-limit, session-key and pause status.

    The plain-function half of get_wallet_status. Also used by telebot's wallet checks, which run
    on a timer with no agent and therefore no ToolRuntime.

    @param user_id  The application user ID.
    @return         The status dict documented on get_wallet_status.
    """
    print("Running get_wallet_status")
    session_handler = load_session_handler(user_id)
    session_key, _ = _get_session_keys(user_id)
    wallet = session_handler.functions
    reads = read_all({
        # By field name, never position: see contracts.read_spending_config.
        "config": lambda: read_spending_config(session_handler),
        "remaining": wallet.getRemainingBudget().call,
        "paused": wallet.paused().call,
        "active": wallet.isSessionActive(session_key).call,
        "expires_at": wallet.currentSessionValidUntil().call,
        "allowlist": wallet.sessionAllowlistEnabled().call,
    })
    cfg = reads["config"]

    return {
        "paused": reads["paused"],
        "session_active": reads["active"],
        "session_expires_at": reads["expires_at"],
        "session_expires_in_secs": max(reads["expires_at"] - int(time.time()), 0),
        "daily_limit_usd": cfg["dailyLimitUsd"] / WEI_PER_ETH,
        "spent_usd": cfg["spentInWindow"] / WEI_PER_ETH,
        # Below zero when the owner lowered the limit under what was already spent: nothing is left.
        "remaining_usd": max(reads["remaining"], 0) / WEI_PER_ETH,
        "window_hours": cfg["windowDuration"] / 3600,
        "watched_tokens": _addresses_to_tickers(user_id, cfg["watchedTokens"]),
        "allowlist_enabled": reads["allowlist"],
    }


# There is NO save_contact or delete_contact tool, and adding either back would reopen a hole rather
# than add a convenience. The contact list is the allowlist of destinations for value --
# _resolve_contact accepts a saved name and refuses a raw address -- so the only thing standing
# between whoever holds the chat surface and an arbitrary payee is the inability to write that list.
# A stolen, unlocked phone is the concrete case: the thief owns the Telegram session, and with a save
# tool would simply add their own address and drain up to the daily cap. Both writes therefore
# require the authenticated web session: POST and DELETE /api/contacts.
#
# Deleting is included even though it is NOT a theft vector on its own -- it only ever shrinks the
# allowlist, so the worst it achieves is nuisance. It moved because one boundary is easier to hold
# than two: "the agent reads the contact list and never writes it" is a rule a reviewer can check at
# a glance, where "the agent may write it, but only destructively" invites someone to re-derive the
# distinction later and get it wrong. Reads stay here; they create no new destination.
@tool
def get_contact(runtime: ToolRuntime[AgentContext], name: str) -> str:
    """
    Looks up the Ethereum address of a saved contact by name.

    Use it to answer questions about a contact ("what's Sandy's address?", "is Bob saved?"). The
    transaction tools take a contact's NAME and look it up themselves, so don't call this before
    them. Contacts can only be added from the web app — if the contact is not found, tell the user
    to add it there. Do NOT ask for an address to use instead; an address given in conversation
    cannot be used as a recipient.

    Args:
        name: The name of the contact to look up (e.g. "Sandy"). Case-insensitive.

    Returns:
        The Ethereum address associated with the name, or None if not found.
    """
    user_id = runtime.context.user_id
    print("Running get_contact")
    return _get_contact(user_id, name)


@tool
def get_all_contacts(runtime: ToolRuntime[AgentContext]) -> list:
    """
    Retrieves the user's saved contacts.

    Use this tool when the user wants to see their full contact list.

    Args:

    Returns:
        A list of dicts with 'name' and 'address' keys, sorted alphabetically by name.
        Returns an empty list if no contacts are saved.
    """
    user_id = runtime.context.user_id
    print("Running get_all_contacts")
    return _get_all_contacts(user_id)


"""
 /*//////////////////////////////////////////////////////////////
                        BLOCKCHAIN TOOLS
//////////////////////////////////////////////////////////////*/
"""
def _get_native_balance(user_id: int) -> float:
    """The wallet's native-asset balance, in whole units: get_eth_balance's read, as a plain float."""
    w3, _, _ = load_network_config(user_id)
    address = load_session_handler(user_id).address
    balance_wei = w3.eth.get_balance(address)
    return balance_wei / WEI_PER_ETH


@tool
def get_eth_balance(runtime: ToolRuntime[AgentContext]) -> dict:
    """
    Retrieves the smart wallet's native gas asset balance, together with what that asset is
    actually called on the current network. "eth" in the tool name is a generic internal
    label, not a claim that the network is Ethereum — this works identically on every
    supported network. Always report the `asset` value from the result, never assume "ETH".

    Use this tool when the user asks how much of their native asset (ETH, BNB) the wallet holds.
    There is no need to check before sending or wrapping: those tools check the balance
    themselves. If the ticker the user asked about (e.g. "ETH") does not match the returned
    `asset` (e.g. "BNB"), do not report the balance under the ticker they asked about and do not
    invent a balance for it either — tell them their wallet is on a network whose native asset is
    `asset`, and there is no separate balance for the ticker they named on this network.

    Args:

    Returns:
        A dict with `balance` (float, whole units, e.g. 1.5) and `asset` (str, e.g. "ETH" or
        "BNB" — the actual name of the native asset on the wallet's current network).
    """
    user_id = runtime.context.user_id
    print("Running get_eth_balance")
    _, chain_id, _ = load_network_config(user_id)
    return {
        "balance": _get_native_balance(user_id),
        "asset": get_native_asset_ticker(chain_id),
    }


@tool
def get_price(runtime: ToolRuntime[AgentContext], token: str, amount: float | None = None) -> float:
    """
    Retrieves a token's current USD price from the registered SHOracle -- or, given `amount`,
    what that amount is worth.

    Use this tool when the user asks what a token, or an amount of one, is worth. A transaction's
    quote already carries the USD value of what it moves, so don't call this before one. NEVER use
    this to estimate swap output quantities — use get_quote_in or get_quote_out, which query live
    pool reserves.

    Args:
        token: The token ticker symbol to price (e.g. "usdc"), or "eth" for the native asset.
        amount: Optional. An amount in whole units (e.g. 100 for 100 USDC) to value instead of
                one unit.

    Returns:
        The USD price of one unit as a float (e.g. 2500.0 for ETH at $2500), or the USD value of
        `amount` when it is given.
    """
    price = _get_price(runtime.context.user_id, token)
    return price if amount is None else price * amount


def _get_price(user_id: int, token: str) -> float:
    """
    Returns a token's USD price from the registered SHOracle.

    The plain-function half of get_price, callable from other tools' bodies (which have no
    ToolRuntime to pass to the @tool wrapper).

    @param user_id  The application user ID.
    @param token    The token ticker to price (e.g. "usdc", "eth").
    @return         The USD price as a float.
    """
    print("Running get_price")

    if token.lower() in ("eth", "bnb"):
        token_address = ETH_SENTINEL
        decimals = 18
    else:
        erc20 = load_ierc20(user_id=user_id, token=token)
        token_address = erc20.address
        label = _unlisted_label(user_id, token_address)
        if label is not None:
            raise ToolException(
                f"Mitfah has no price for {label}: it isn't a token Mitfah lists (the user added it "
                f"themselves), so there is no price feed for it. Tell the user its dollar value "
                f"isn't available here, and never estimate one."
            )
        decimals = erc20.functions.decimals().call()
    print(f"Getting price for token: {token}, address: {token_address}")
    session_handler = load_session_handler(user_id)
    # getUsdValue(token, one whole token) returns the unit price with 18 decimals.
    usd_value = session_handler.functions.getUsdValue(token_address, 10**decimals).call()
    return usd_value / WEI_PER_ETH


@tool
def get_erc20_balance(runtime: ToolRuntime[AgentContext], token: str, contact: str | None = None) -> float:
    """
    Retrieves an ERC20 token balance: the user's own wallet's, or a saved contact's.

    Use this tool when the user asks how much of a token they hold ("how much USDC do I have?"),
    or how much a contact holds ("what is Sandy's LINK balance?"). For the native asset (ETH, BNB),
    use get_eth_balance. There is no need to check a balance before a transaction: every
    transaction tool checks it itself.

    Args:
        token: The token ticker symbol to check (e.g. "usdc").
        contact: A saved contact's name, to read their balance instead of the wallet's. Leave it
                 unset (or pass "me") for the user's own wallet.

    Returns:
        The balance in whole units (e.g. 100.0 for 100 USDC).
    """
    user_id = runtime.context.user_id
    print("Running get_erc20_balance")
    if contact is None:
        owner = load_session_handler(user_id).address
    else:
        # "me" is the wallet itself; any other name must be a saved contact.
        owner = _resolve_contact(user_id, contact, role="account to check")
    return get_erc20_tools(user_id)["get_balance"].invoke(
        {"token": _token_address(user_id, token), "owner": owner}
    )["amount"]
