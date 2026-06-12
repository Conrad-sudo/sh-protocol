import asyncio
import time
from decimal import Decimal


from db import (
    get_supported_tokens as _get_supported_tokens,
    save_contact as _save_contact,
    get_contact as _get_contact,
    get_all_contacts as _get_all_contacts,
    delete_contact as _delete_contact,
    get_all_sessions as _get_all_sessions,
    get_recurring_transfers as _get_recurring_transfers,
    save_recurring_transfer as _save_recurring_transfer,
    delete_recurring_transfer as _delete_recurring_transfer,
    delete_session,
    get_agent_id,
)
from network_config import load_network_config
from anvil import (
    send_user_op_as_session as _send_user_op_as_session,
    get_or_create_session_key,
)

from live_network import send_live_user_op_as_session as _send_live_user_op_as_session

from constants import ETH_SENTINEL, UNISWAP_V2_ROUTER, WEI_PER_ETH


def _to_base_units(amount: float, decimals: int) -> int:
    return int(Decimal(str(amount)) * Decimal(10) ** decimals)


def send_user_op_as_session(chat_id, key_ciphertext, target, value, data):
    """
    Central dispatch for all on-chain writes in the bot. Routes a UserOperation to
    either the local Anvil backend (for fork/test networks) or the live Alchemy bundler
    (for all other networks), based on the chain name stored in the user's network config.

    Every @tool function that submits an on-chain transaction calls this function.
    Callers pass a plain (target, value, data) triple; this function handles the full
    ERC-4337 flow — gas estimation, signing, submission, and receipt polling — via the
    appropriate backend. RuntimeError from either backend is converted to ToolException
    so LangChain's tool error handler can surface it to the agent cleanly.

    @param chat_id        The Telegram chat ID of the user making the request.
    @param key_ciphertext Vault Transit ciphertext for the session key ('vault:v1:...').
    @param target         The contract address the SessionHandler will call.
    @param value          ETH value in wei to forward with the inner call (0 for ERC20 ops).
    @param data           ABI-encoded calldata for the inner call on target.
    @return               A tuple of (tx_hash_bytes, receipt_dict) where receipt_dict
                          contains at least {"status": 1} on success.
    @raises ToolException If the UserOperation fails or the bundler rejects the submission.
    """
    _,_,chain_name = load_network_config(chat_id)

    if "fork" in chain_name.lower() or "anvil" in chain_name.lower():
        try:
            return _send_user_op_as_session(chat_id, key_ciphertext, target, value, data)
        except RuntimeError as e:
            raise ToolException(str(e))
    else:
        try:
            return _send_live_user_op_as_session(chat_id, key_ciphertext, target, value, data)
        except RuntimeError as e:
            raise ToolException(str(e))
        



from contracts import (
    load_session_handler,
    load_ierc20,
    load_calldata,
    load_iuniswap_router,
    load_iuniswap_pair,
    load_identity_registry,
    load_reputation_registry,
)
from network_config import load_network_config
from langchain.tools import tool
from langchain_core.tools import ToolException
from web3.exceptions import ContractLogicError

BPS_DENOMINATOR = 10_000
DEFAULT_SLIPPAGE_BPS = 50  # 0.5%
SWAP_DEADLINE_SECS = 600  # 10 minutes
SECONDS_PER_HOUR = 3_600

"""
 /*//////////////////////////////////////////////////////////////
                        DATABASE TOOLS
//////////////////////////////////////////////////////////////*/
"""


@tool
def get_supported_tokens(chat_id: int) -> list:
    """
    Retrieves a list of supported token tickers for the user's current network.

    Use this tool when you need to know which tokens the wallet is set up to handle,
    especially before any on-chain action or when the user asks about a specific token.
    The returned list reflects the network the user is connected to (anvil or mainnet).

    Args:
        chat_id: The Telegram chat ID of the user. Required to resolve the correct
                 network and token table.

    Returns:
        A list of supported token ticker symbols (e.g. ["usdc", "dai"]).
    """
    print("Running get_supported_tokens")
    return _get_supported_tokens(chat_id)


@tool
def get_all_sessions(chat_id: int) -> list[dict]:
    """
    Retrieves all active sessions for a given user.

    Use this tool when the user wants to see an overview of all their session keys,
    including which tokens they are scoped to, their spending limits, and when they expire.

    Args:
        chat_id: The Telegram chat ID of the user making the request.

    Returns:
        A list of dicts, each with 'target' (token ticker), 'spending_limit' (in whole units,
        e.g. 1000.0), and 'end_time' (ISO 8601 date string). Raises ValueError if no sessions
        exist for the user.
    """
    print("Running get_all_sessions")
    rows = _get_all_sessions(chat_id)
    session_handler = load_session_handler(chat_id)
    active = []
    for row in rows:
        session_key, _ = get_session_keys.func(chat_id, row["target"])
        if session_handler.functions.isSessionActive(session_key).call():
            active.append(row)
        else:
            delete_session(chat_id, row["target"])
    return active


@tool
def save_contact(chat_id: int, name: str, address: str):
    """
    Saves a new contact by associating a human-readable name with an Ethereum address.

    Use this tool when the user wants to add or update a contact so they can be
    referred to by name in future transactions instead of a raw address. If a contact
    with the same name already exists, their address will be updated. Name lookup is
    case-insensitive.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        name: A human-readable label for the contact (e.g. "Sandy"). Stored in lowercase.
        address: The Ethereum address to associate with the name (e.g. "0x70997970C51812dc3A010C7d01b50e0d17dc79C8").
    """
    print("Running save_contact")
    _save_contact(chat_id, name, address)


@tool
def get_contact(chat_id: int, name: str) -> str:
    """
    Looks up the Ethereum address of a saved contact by name.

    Use this tool when you need to resolve a contact's address before performing
    an operation that requires a raw address. If the contact is not found, ask the
    user to provide their Ethereum address and call save_contact before proceeding.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        name: The name of the contact to look up (e.g. "Sandy"). Case-insensitive.

    Returns:
        The Ethereum address associated with the name, or None if not found.
    """
    print("Running get_contact")
    return _get_contact(chat_id, name)


@tool
def get_all_contacts(chat_id: int) -> list:
    """
    Retrieves all saved contacts for a given user.

    Use this tool when the user wants to see their full contact list.

    Args:
        chat_id: The Telegram chat ID of the user making the request.

    Returns:
        A list of dicts with 'name' and 'address' keys, sorted alphabetically by name.
        Returns an empty list if no contacts are saved.
    """
    print("Running get_all_contacts")
    return _get_all_contacts(chat_id)


@tool
def delete_contact(chat_id: int, name: str):
    """
    Deletes a saved contact by name.

    Use this tool when the user wants to remove a contact from their list. If the
    contact does not exist, this function will do nothing.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        name: The name of the contact to delete (e.g. "Sandy"). Case-insensitive.
    """
    print("Running delete_contact")
    return _delete_contact(chat_id, name)


@tool
def get_recurring_transfers(chat_id: int) -> list[dict]:
    """
    Returns all scheduled recurring transfers for a user.

    Use this tool when the user asks to see their recurring transfers or wants to
    review what scheduled payments are active.

    Args:
        chat_id: The Telegram chat ID of the user making the request.

    Returns:
        A list of dicts with 'id', 'token', 'recipient', 'amount', and 'interval_hrs' keys.
        Returns an empty list if no recurring transfers are scheduled.
    """
    print("Running get_recurring_transfers")
    return _get_recurring_transfers(chat_id)


"""
 /*//////////////////////////////////////////////////////////////
                        BLOCKCHAIN TOOLS
//////////////////////////////////////////////////////////////*/
"""


@tool
def get_eth_balance(chat_id: int) -> float:
    """
    Retrieves the ETH balance of the smart wallet contract.

    Use this tool when the user asks how much ETH the wallet holds or wants
    to check whether there is enough ETH before wrapping or sending.

    Args:
        chat_id: The Telegram chat ID of the user making the request.

    Returns:
        The wallet's ETH balance in whole units (e.g. 1.5 for 1.5 ETH).
    """
    print("Running get_eth_balance")
    w3, _, _ = load_network_config(chat_id)
    address = load_session_handler(chat_id).address
    balance_wei = w3.eth.get_balance(address)
    return balance_wei / WEI_PER_ETH


@tool
def send_eth(
    chat_id: int, session_key_ciphertext: str, recipient: str, amount_eth: float
):
    """
    Sends native ETH to a named contact using the ETH session key.

    Use this tool when the user wants to send ETH to someone. The recipient must
    already be saved as a contact — if they are not, call save_contact first.
    Retrieve the session key by calling get_session_keys("eth").
    Specify the amount in whole ETH units (e.g. 1.5 for 1.5 ETH), not in wei.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: Vault ciphertext for the native ETH session key.
                                Obtain by calling get_session_keys("eth").
        recipient: The name of the contact to send ETH to (e.g. "Sandy"). Must be a saved contact.
        amount_eth: The amount of ETH to send, in whole units (e.g. 1.5 for 1.5 ETH). The tool converts this to wei internally before sending the transaction.

    Returns:
        A string summarizing the transaction result, including the transaction hash and status.
    """
    print("Running send_eth")
    recipient_addr = get_contact.func(chat_id, recipient)
    value = _to_base_units(amount_eth, 18)

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=recipient_addr,
        value=value,
        data=b"",
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}"


@tool
def get_session_keys(chat_id: int, token: str) -> tuple[str, str]:
    """
    Returns the session key address and Vault ciphertext for a given user and token.

    Use this tool before any on-chain write operation (transfer, approve, transferFrom)
    to retrieve the session credentials for the specified token. The returned ciphertext
    must be passed directly to the transaction tool — never expose it to the user in
    your response.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker symbol the session key is scoped to (e.g. "usdc").

    Returns:
        A tuple of (session_key_address, session_key_ciphertext). Pass the ciphertext
        to the relevant transaction tool. Do not include it in any message to the user.
    """
    print("Running get_session_keys")

    if token == "uniswapv2_router":
        target_address = UNISWAP_V2_ROUTER
    elif token == "eth":
        target_address = ETH_SENTINEL
    elif token == "reputation_registry":
        target_address = load_reputation_registry(chat_id).address
    else:
        target_address = load_ierc20(chat_id=chat_id, token=token).address

    return get_or_create_session_key(chat_id, target_address)


@tool
def check_session_validity(chat_id: int, token: str) -> bool:
    """
    Checks if a session key for a given token is still valid.

    Use this tool to verify whether the session key associated with the specified
    token is active and can be used for transactions. This is useful for ensuring
    that the user has a valid session before attempting to send tokens.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker symbol to check the session for (e.g. "usdc").

    Returns:
        True if the session key is valid and active, False otherwise.
    """
    print("Running check_session_validity")
    session_key, _ = get_session_keys.func(chat_id, token)
    session_handler = load_session_handler(chat_id)
    return session_handler.functions.isSessionActive(session_key).call()


@tool
def check_remaining_budget(chat_id: int, token: str) -> float:
    """
    Returns the remaining spending budget for a session key.

    Use this tool when the user wants to know how much budget is left on their
    session key for a given token.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker symbol to check the session for (e.g. "usdc").

    Returns:
        The remaining budget in whole USD units (e.g. 500.0 for $500 remaining).
    """
    print("Running check_remaining_budget")
    session_key, _ = get_session_keys.func(chat_id, token)
    session_handler = load_session_handler(chat_id)
    budget = session_handler.functions.getRemainingBudget(session_key).call()
    return budget / WEI_PER_ETH


@tool
def check_spending_within_budget(
    chat_id: int, token: str, amount: int, is_uniswap=False
) -> bool:
    """
    Checks if a proposed transaction amount is within the remaining budget of the session key.

    Use this tool before attempting a transfer to ensure that the amount being sent does not exceed the session's spending limit.
    Note: the comparison is done in USD — the token amount is converted to its USD value via a price
    oracle and checked against the remaining budget, which is also tracked in USD, not in token units.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker symbol used to price the amount (e.g. "usdc"). For Uniswap swaps,
               pass the token being sold (swapExactTokensForTokens) or the token being acquired
               (swapTokensForExactTokens) as a USD proxy.
        amount: The proposed transaction amount in whole token units (e.g. 100 for 100 USDC).
        is_uniswap: Set to True when checking budget for a Uniswap swap. Fetches the
                    "uniswapv2_router" session key instead of a token-scoped one. Defaults to False.

    Returns:
        True if the proposed amount is within the remaining budget, False otherwise.
    """
    print("Running check_spending_within_budget")
    session_handler = load_session_handler(chat_id)

    if is_uniswap:
        session_key, _ = get_session_keys.func(chat_id, "uniswapv2_router")
    else:
        session_key, _ = get_session_keys.func(chat_id, token)

    erc20 = load_ierc20(chat_id=chat_id, token=token)
    decimals = erc20.functions.decimals().call()
    return session_handler.functions.isSpendingWithinBudget(
        session_key, erc20.address, _to_base_units(amount, decimals)
    ).call()


@tool
def get_price(chat_id: int, token: str) -> float:
    """
    Retrieves the current USD price of a token by querying the registered PriceOracle.

    Use this tool when the user asks what a token is currently worth, or when you need
    to estimate the USD value of an amount before sending it.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker symbol to price (e.g. "usdc", "eth").

    Returns:
        The current USD price as a float (e.g. 2500.0 for ETH at $2500).
    """
    print("Running get_price")

    token_address = (
        ETH_SENTINEL
        if token.lower() == "eth"
        else load_ierc20(chat_id=chat_id, token=token).address
    )
    print(f"Getting price for token: {token}, address: {token_address}")
    session_handler = load_session_handler(chat_id)
    price, decimals = session_handler.functions.getPrice(token_address).call()
    return price / (10**decimals)


@tool
def get_usd_value(chat_id: int, token: str, amount: float) -> float:
    """
    Converts a token amount to its current USD value using the registered PriceOracle.

    Use this tool when confirming a transfer, approval, or transferFrom with the user or when the user asks for the USD value an amount of a token.
    Always call this before presenting the confirmation message so the user can see
    the USD equivalent of what they are about to send or approve.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker symbol (e.g. "usdc", "dai").
        amount: The token amount in whole units (e.g. 100 for 100 USDC).

    Returns:
        The USD value of the amount as a float (e.g. 99.5 for 100 USDC at $0.995).
    """
    print("Running get_usd_value")
    price = get_price.func(chat_id, token)
    return price * amount


@tool
def preflight_check(
    chat_id: int,
    token: str,
    amount: float,
    is_uniswap: bool = False,
) -> dict:
    """
    Runs all pre-transaction checks in a single call: session validity, budget check,
    and USD value conversion. Call this instead of check_session_validity,
    check_spending_within_budget, and get_usd_value separately before any on-chain action.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker to check (e.g. "usdc"). For Uniswap swaps, pass the token
               being sold (exact-in) or acquired (exact-out) as the USD proxy.
        amount: The proposed amount in whole token units (e.g. 100 for 100 USDC).
        is_uniswap: Set to True for Uniswap swaps so the router session key is used for
                    the budget check. Defaults to False.

    Returns:
        A dict with:
          - "session_active" (bool): True if the session key is valid and active.
          - "within_budget" (bool): True if the amount is within the remaining budget.
          - "usd_value" (float): The USD equivalent of `amount` at the current price.
        If "session_active" is False, abort and notify the user. If "within_budget" is
        False, abort and notify the user. Only proceed if both are True.
    """
    print("Running preflight_check")
    session_key_token = "uniswapv2_router" if is_uniswap else token
    session_key, _ = get_session_keys.func(chat_id, session_key_token)
    session_handler = load_session_handler(chat_id)

    session_active = session_handler.functions.isSessionActive(session_key).call()

    if token.lower() == "eth":

        decimals = 18
        within_budget = session_handler.functions.isSpendingWithinBudget(
            session_key, ETH_SENTINEL, _to_base_units(amount, decimals)
        ).call()

    else:
        erc20 = load_ierc20(chat_id=chat_id, token=token)
        decimals = erc20.functions.decimals().call()
        within_budget = session_handler.functions.isSpendingWithinBudget(
            session_key, erc20.address, _to_base_units(amount, decimals)
        ).call()

    usd_value = get_price.func(chat_id, token) * amount

    return {
        "session_active": session_active,
        "within_budget": within_budget,
        "usd_value": usd_value,
    }


@tool
def get_erc20_balance(chat_id: int, token: str) -> float:
    """
    Retrieves the ERC20 token balance of the smart wallet contract.

    Use this tool when the user asks about their own wallet's token balance
    (e.g. "my balance", "how much USDC do I have"). Do NOT use this to check
    a contact's balance — use get_contact_erc20_balance for that.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token: The token ticker symbol to check (e.g. "usdc").

    Returns:
        The smart wallet's token balance in whole units (e.g. 100.0 for 100 USDC).
    """
    print("Running get_erc20_balance")
    address = load_session_handler(chat_id).address
    erc20 = load_ierc20(chat_id=chat_id, token=token)
    balance = erc20.functions.balanceOf(address).call()
    decimals = erc20.functions.decimals().call()
    return balance / (10**decimals)


@tool
def get_contact_erc20_balance(chat_id: int, contact_name: str, token: str) -> float:
    """
    Retrieves the ERC20 token balance of a saved contact's address.

    Use this tool when the user asks about a contact's token balance
    (e.g. "how much USDC does Sandy have?", "what is Alice's LINK balance?").
    Do NOT use this to check the smart wallet's own balance — use get_erc20_balance for that.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        contact_name: The name of the saved contact (e.g. "Sandy"). Case-insensitive.
        token: The token ticker symbol to check (e.g. "usdc").

    Returns:
        The contact's token balance in whole units (e.g. 100.0 for 100 USDC).
    """
    print("Running get_contact_erc20_balance")
    address = _get_contact(chat_id, contact_name)
    erc20 = load_ierc20(chat_id=chat_id, token=token)
    balance = erc20.functions.balanceOf(address).call()
    decimals = erc20.functions.decimals().call()
    return balance / (10**decimals)


@tool
def get_erc20_allowance(chat_id: int, token: str, spender: str) -> float:
    """
    Retrieves the smart wallet's ERC20 token allowance for a specified spender.

    Use this tool when the user wants to check how many tokens the wallet has approved
    for a particular spender before making a transferFrom or similar operation.

    Args:
        token: The token ticker symbol to check (e.g. "usdc").
        spender: The name of the contact who is the spender (e.g. "Sandy"). Must be a saved contact.

    Returns:
        The token allowance approved for the spender in whole units (e.g. 100.0 for 100 USDC).
    """
    print("Running get_erc20_allowance")
    erc20 = load_ierc20(chat_id=chat_id, token=token)
    address = load_session_handler(chat_id).address
    spender_addr = get_contact.func(chat_id, spender)
    allowance = erc20.functions.allowance(address, spender_addr).call()
    decimals = erc20.functions.decimals().call()
    return allowance / (10**decimals)


@tool
def wrap_eth(chat_id: int, session_key_ciphertext: str, amount_eth: float):
    """
    Wraps ETH into WETH by calling deposit() on the WETH contract.

    Use this tool when the user wants to convert ETH to WETH. This does not go through
    the Uniswap router — it is a direct 1:1 wrap on the WETH contract. Retrieve the
    session key by calling get_session_keys("weth").

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for the WETH contract. Obtain by calling get_session_keys("weth").
        amount_eth: The amount of ETH to wrap, in whole units (e.g. 1.5 for 1.5 ETH).
                    The tool converts this to wei internally before sending the transaction.

    Returns:
        A string summarizing the transaction result, including the transaction hash and status.
    """
    print("Running wrap_eth")
    iweth = load_ierc20(chat_id, "weth")
    value = _to_base_units(amount_eth, 18)

    data = load_calldata(
        instance=iweth,
        fn_name="deposit",
        args=[],
    )
    target = iweth.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=value,
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}"


@tool
def transfer_erc20(
    chat_id: int, session_key_ciphertext: str, token: str, recipient: str, amount: float
):
    """
    Transfers ERC20 tokens to a named contact using a session key.

    Use this tool when the user wants to send tokens to someone. The recipient must
    already be saved as a contact — if they are not, call save_contact first. The
    session_key_ciphertext must match the token being sent — retrieve it by calling
    get_session_keys with the token ticker. Specify the amount in whole token units
    (e.g. 100 for 100 USDC), not in raw base units.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for this token. Obtain by calling get_session_keys(token).
        token: The token ticker symbol to transfer (e.g. "usdc").
        recipient: The name of the contact to send tokens to (e.g. "Sandy").
                   Must be a saved contact.
        amount: The amount of tokens to send in whole units (e.g. 100 for 100 USDC).

    Returns: A string summarizing the transaction result, including the transaction hash and status.
    """
    print("Running transfer_erc20")
    erc20 = load_ierc20(chat_id=chat_id, token=token)
    recipient_addr = get_contact.func(chat_id, recipient)
    decimals = erc20.functions.decimals().call()
    value = _to_base_units(amount, decimals)

    data = load_calldata(
        instance=erc20, fn_name="transfer", args=[recipient_addr, value]
    )
    target = erc20.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}"


@tool
def approve_erc20(
    chat_id: int, session_key_ciphertext: str, token: str, spender: str, amount: float
):
    """
    Approves a spender to transfer ERC20 tokens on behalf of the smart wallet.

    Use this tool when the user wants to grant permission for a spender to transfer
    tokens from the wallet. The spender must already be saved as a contact — if they
    are not, call save_contact first. The session_key_ciphertext must match the token
    being approved — retrieve it by calling get_session_keys with the token ticker. Specify
    the amount in whole token units (e.g. 100 for 100 USDC), not in raw base units.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for this token. Obtain by calling get_session_keys(token).
        token: The token ticker symbol to approve (e.g. "usdc").
        spender: The name of the contact to approve as a spender (e.g. "Sandy").
                 Must be a saved contact.
        amount: The amount of tokens to approve in whole units (e.g. 100 for 100 USDC).

    Returns: A string summarizing the transaction result, including the transaction hash and status.
    """
    print("Running approve_erc20")
    erc20 = load_ierc20(chat_id=chat_id, token=token)
    spender_addr = get_contact.func(chat_id, spender)
    decimals = erc20.functions.decimals().call()
    value = _to_base_units(amount, decimals)

    data = load_calldata(instance=erc20, fn_name="approve", args=[spender_addr, value])
    target = erc20.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}"


@tool
def transferFrom_erc20(
    chat_id: int,
    session_key_ciphertext: str,
    token: str,
    sender: str,
    recipient: str,
    amount: float,
):
    """
    Transfers ERC20 tokens from a sender to a recipient.

    Use this tool when the user wants to transfer tokens from another address (sender)
    to a recipient. The sender and recipient must already be saved as contacts — if they
    are not, call save_contact first. The session_key_ciphertext must match the token being
    transferred — retrieve it by calling get_session_keys with the token ticker. Specify the
    amount in whole token units (e.g. 100 for 100 USDC), not in raw base units.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for this token. Obtain by calling get_session_keys(token).
        token: The token ticker symbol to transfer (e.g. "usdc").
        sender: The name of the contact who is the sender of the tokens (e.g. "Sandy"). Must be a saved contact.
        recipient: The name of the contact who is the recipient of the tokens (e.g. "Alex"). Must be a saved contact.
        amount: The amount of tokens to transfer in whole units (e.g. 100 for 100 USDC).

    Returns: A string summarizing the transaction result, including the transaction hash and status.
    """

    print("Running transferFrom_erc20")
    erc20 = load_ierc20(chat_id=chat_id, token=token)
    sender_addr = get_contact.func(chat_id, sender)

    if recipient.lower() == "me":
        recipient_addr = load_session_handler(chat_id).address
    else:
        recipient_addr = get_contact.func(chat_id, recipient)
    decimals = erc20.functions.decimals().call()
    value = _to_base_units(amount, decimals)

    data = load_calldata(
        instance=erc20,
        fn_name="transferFrom",
        args=[sender_addr, recipient_addr, value],
    )
    target = erc20.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}"


"""
 /*//////////////////////////////////////////////////////////////
                        UNISWAP_V2 TOOLS
//////////////////////////////////////////////////////////////*/
"""


@tool
def get_quote_in(
    chat_id: int, token_in: str, token_out: str, amount_out: float
) -> dict:
    """
    Returns how much of token_in is required to receive an exact amount of token_out,
    using the Uniswap V2 router's getAmountsIn. Routes through WETH when neither token is WETH.

    Use this tool when the user wants to know the cost of acquiring a specific amount of a token
    (e.g. "How much USDC do I need to buy exactly 100 DAI?"). Call this before a swap to give
    the user a price preview. The returned dict can also be passed directly to Uniswap swap tools:
    use amount_in for the swap's amount argument and amount_in_base for slippage calculations.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_in: The ticker of the token being spent (e.g. "usdc").
        token_out: The ticker of the token being received (e.g. "dai").
        amount_out: The exact amount of token_out to receive, in whole units (e.g. 100 for 100 DAI).

    Returns:
        A dict with:
          - path (list[str]): the token address path used for the quote (passable directly to swap tools)
          - amount_in (float): required token_in in whole units (e.g. 101.5 for 101.5 USDC)
          - amount_in_base (int): required token_in in base units (for slippage math in swap tools)
          - amount_out (float): the requested token_out amount in whole units
          - amount_out_base (int): the requested token_out amount in base units

        When presenting to the user, show only amount_in and amount_out.
        Never expose path, amount_in_base, or amount_out_base.
    """
    print("Running get_amount_in")
    token_in = "weth" if token_in.lower() == "eth" else token_in
    token_out = "weth" if token_out.lower() == "eth" else token_out
    router = load_iuniswap_router(chat_id=chat_id)
    erc20_out = load_ierc20(chat_id=chat_id, token=token_out)
    erc20_in = load_ierc20(chat_id=chat_id, token=token_in)
    decimals_out = erc20_out.functions.decimals().call()
    decimals_in = erc20_in.functions.decimals().call()
    amount_out_base = _to_base_units(amount_out, decimals_out)
    if token_in.lower() != "weth" and token_out.lower() != "weth":
        path = [
            erc20_in.address,
            load_ierc20(chat_id, "weth").address,
            erc20_out.address,
        ]
    else:
        path = [erc20_in.address, erc20_out.address]

    try:
        amounts = router.functions.getAmountsIn(amount_out_base, path).call()
    except ContractLogicError:
        raise ToolException(
            f"No Uniswap V2 liquidity path found for {token_in.upper()} → {token_out.upper()}. "
            f"The pool may not exist or have insufficient reserves."
        )

    return {
        "path": path,
        "amount_in": amounts[0] / 10**decimals_in,
        "amount_in_base": amounts[0],
        "amount_out": amount_out,
        "amount_out_base": amount_out_base,
    }


@tool
def get_quote_out(
    chat_id: int, token_in: str, token_out: str, amount_in: float
) -> dict:
    """
    Returns how much of token_out will be received when spending an exact amount of token_in,
    using the Uniswap V2 router's getAmountsOut. Routes through WETH when neither token is WETH.

    Use this tool when the user wants to know how much they'll receive for a given spend
    (e.g. "How much DAI will I get for 100 USDC?"). Call this before a swap to give
    the user a price preview. The returned dict can also be passed directly to Uniswap swap tools:
    use amount_out for the swap's amount argument and amount_out_base for slippage calculations.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_in: The ticker of the token being spent (e.g. "usdc").
        token_out: The ticker of the token being received (e.g. "dai").
        amount_in: The exact amount of token_in to spend, in whole units (e.g. 100 for 100 USDC).

    Returns:
        A dict with:
          - path (list[str]): the token address path used for the quote (passable directly to swap tools)
          - amount_in (float): the token_in amount in whole units
          - amount_in_base (int): the token_in amount in base units
          - amount_out (float): expected token_out in whole units (e.g. 99.2 for 99.2 DAI)
          - amount_out_base (int): expected token_out in base units (for slippage math in swap tools)

        When presenting to the user, show only amount_in and amount_out.
        Never expose path, amount_in_base, or amount_out_base.
    """
    print("Running get_amount_out")
    token_in = "weth" if token_in.lower() == "eth" else token_in
    token_out = "weth" if token_out.lower() == "eth" else token_out
    router = load_iuniswap_router(chat_id=chat_id)
    erc20_out = load_ierc20(chat_id=chat_id, token=token_out)
    erc20_in = load_ierc20(chat_id=chat_id, token=token_in)
    decimals_out = erc20_out.functions.decimals().call()
    decimals_in = erc20_in.functions.decimals().call()
    amount_in_base = _to_base_units(amount_in, decimals_in)
    if token_in.lower() != "weth" and token_out.lower() != "weth":
        path = [
            erc20_in.address,
            load_ierc20(chat_id, "weth").address,
            erc20_out.address,
        ]
    else:
        path = [erc20_in.address, erc20_out.address]

    try:
        amounts = router.functions.getAmountsOut(amount_in_base, path).call()
    except ContractLogicError:
        raise ToolException(
            f"No Uniswap V2 liquidity path found for {token_in.upper()} → {token_out.upper()}. "
            f"The pool may not exist or have insufficient reserves."
        )

    return {
        "path": path,
        "amount_in": amount_in,
        "amount_in_base": amount_in_base,
        "amount_out": amounts[-1] / 10**decimals_out,
        "amount_out_base": amounts[-1],
    }


@tool
def get_liquidity_token_balance(
    chat_id: int, token_a: str, token_b: str = "weth"
) -> float:
    """
    Retrieves the smart wallet's balance of Uniswap V2 liquidity tokens for a given pair.

    Use this tool when the user wants to check how much liquidity they have provided to a Uniswap V2 pool.
    The tool identifies the correct pair based on the two token tickers and returns the wallet's balance
    of that pair's liquidity tokens in whole units (not base units).

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_a: The ticker symbol of the first token in the pair (e.g. "dai").
        token_b: The ticker symbol of the second token in the pair. Defaults to "weth".

    Returns:
        The wallet's balance of liquidity tokens for the specified pair, in whole units (e.g. 10.5).
    """
    print("Running get_liquidity_token_balance")
    pair = load_iuniswap_pair(chat_id, token_a, token_b)
    address = load_session_handler(chat_id).address
    pair_address = pair.address
    pair_erc20 = load_ierc20(chat_id, pair_address, uniswap_pair=True)
    balance = pair_erc20.functions.balanceOf(address).call()
    decimals = pair_erc20.functions.decimals().call()
    return balance / (10**decimals)


@tool
def is_derived_input_sufficient(
    chat_id: int,
    token_in: str,
    token_out: str,
    amount_out: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> dict[bool, float]:
    """
    Checks if the user has sufficient funds to execute a swap based on a quote and slippage tolerance.

    Use this function before attempting a swap to ensure that the user has enough of the input token
    to cover the required amount plus slippage. This is a helper function that can be called after
    get_quote_in or get_quote_out to validate that the swap can proceed.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_in: The ticker of the token being spent (e.g. "usdc").
        token_out: The ticker of the token being received (e.g. "dai").
        amount_out: The amount of token_out to receive, in whole units (e.g. 100 for 100 DAI).
        slippage_bps: The acceptable slippage in basis points (e.g. 50 for 0.5% slippage).
    Returns:
        A dict with:
          - is_sufficient (bool): True if the user has sufficient funds to cover the swap including slippage, False otherwise.
          - derived_input (float): The amount of the input token required to cover the swap including slippage.
    """

    if token_in.lower() == "eth":
        balance = get_eth_balance.func(chat_id)
    else:
        balance = get_erc20_balance.func(chat_id, token_in)

    if token_in.lower() == "eth":
        token_in = "weth"
    if token_out.lower() == "eth":
        token_out = "weth"

    quote = get_quote_in.func(chat_id, token_in, token_out, amount_out)

    required_with_slippage = (
        quote["amount_in"] * (BPS_DENOMINATOR + slippage_bps) / BPS_DENOMINATOR
    )

    if balance < required_with_slippage:
        return {"is_sufficient": False, "derived_input": required_with_slippage}
    else:
        return {"is_sufficient": True, "derived_input": required_with_slippage}


@tool
def is_exact_input_sufficient(chat_id: int, token_in: str, amount_in: float) -> bool:
    """
    Checks if the user has sufficient funds to execute a swap based on an exact input quote.

    Use this function before attempting a swap to ensure that the user has enough of the input token
    to cover the required amount without considering slippage.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_in: The ticker of the token being spent (e.g. "usdc").
        amount_in: The amount of token_in to spend, in whole units (e.g. 100 for 100 USDC).

    Returns:
        True if the user has sufficient funds to cover the swap without slippage, False otherwise.
    """
    if token_in.lower() == "eth":
        balance = get_eth_balance.func(chat_id)
    else:
        balance = get_erc20_balance.func(chat_id, token_in)

    if balance < amount_in:
        return False
    else:
        return True


@tool
def is_liquidity_sufficient(
    chat_id: int, token_a: str, amount_a: float, token_b: str
) -> dict[bool, float]:
    """
    Checks whether the wallet holds enough of both tokens to add liquidity to a Uniswap V2 pool.

    Derives the required token_b amount from live pool reserves via get_pool_quote internally —
    no need to pre-compute it. Pass "eth" as token_b when the pool pairs an ERC20 with native ETH
    (i.e. for add_liquidity_eth); the function maps "eth" to "weth" for the reserve lookup and
    checks the ETH balance accordingly.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_a: The ticker of the first token (e.g. "dai").
        amount_a: The desired token_a deposit amount in whole units.
        token_b: The ticker of the second token (e.g. "weth"), or "eth" for native ETH.

    Returns:
        A dict with:
          - is_sufficient (bool): True if the wallet holds enough of both tokens, False otherwise.
          - amount_b (float): The proportional token_b amount required, in whole units.
    """
    quote_token_b = "weth" if token_b.lower() == "eth" else token_b
    quote = get_pool_quote.func(chat_id, token_a, quote_token_b, amount_a)
    amount_b = quote["amount_b_desired"]

    balance_a = get_erc20_balance.func(chat_id, token_a)
    if amount_a > balance_a:
        return {"is_sufficient": False, "amount_b": amount_b}

    if token_b.lower() == "eth":
        balance_b = get_eth_balance.func(chat_id)
    else:
        balance_b = get_erc20_balance.func(chat_id, token_b)
    if amount_b > balance_b:
        return {"is_sufficient": False, "amount_b": amount_b}

    return {"is_sufficient": True, "amount_b": amount_b}


@tool
def is_liquidity_removal_sufficient(
    chat_id: int, token_a: str, token_b: str, lp_amount: float
) -> bool:
    """
    Checks whether the wallet holds enough LP tokens to remove liquidity from a Uniswap V2 pool.


    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_a: The ticker of the first token in the pair (e.g. "dai").
        token_b: The ticker of the second token in the pair (e.g. "weth").
        lp_amount: The amount of LP tokens to burn, in whole units (e.g. 0.5).

    Returns:
        True if the wallet holds enough LP tokens to burn, False otherwise.
    """
    lp_token_balance = get_liquidity_token_balance.func(chat_id, token_a, token_b)
    return lp_amount <= lp_token_balance


@tool
def get_pool_quote(chat_id: int, token_a: str, token_b: str, amount_a: float) -> dict:
    """
    Returns the proportional token_b amount required to match a given token_a deposit in a
    Uniswap V2 pool, using live pool reserves and router.quote(). Also returns token addresses
    and base-unit amounts needed by the add_liquidity and add_liquidity_eth tools.

    Use this tool when the user wants to preview how much of the second token they need to
    provide before adding liquidity (e.g. "How much ETH do I need to pair with 2500 DAI?").
    For ETH pools, pass token_b="weth". When presenting the result to the user, only show
    amount_a and amount_b_desired — never expose token addresses or base-unit fields.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_a: The ticker of the first token (e.g. "dai").
        token_b: The ticker of the second token (e.g. "weth").
        amount_a: The amount of token_a to deposit, in whole units (e.g. 2500 for 2500 DAI).

    Returns:
        A dict with:
          - token_a_address (str): checksummed address of token_a
          - token_b_address (str): checksummed address of token_b
          - decimals_a (int): decimal precision of token_a
          - decimals_b (int): decimal precision of token_b
          - amount_a (float): token_a deposit in whole units
          - amount_a_base (int): token_a deposit in base units
          - amount_b_desired (float): required token_b in whole units
          - amount_b_desired_base (int): required token_b in base units
    """
    router = load_iuniswap_router(chat_id)
    erc20_a = load_ierc20(chat_id=chat_id, token=token_a)
    erc20_b = load_ierc20(chat_id=chat_id, token=token_b)
    try:
        pair = load_iuniswap_pair(chat_id, token_a, token_b)
    except ValueError as e:
        raise ToolException(str(e))

    decimals_a = erc20_a.functions.decimals().call()
    decimals_b = erc20_b.functions.decimals().call()
    amount_a_base = _to_base_units(amount_a, decimals_a)

    reserve0, reserve1, _ = pair.functions.getReserves().call()
    token0 = pair.functions.token0().call()
    if token0.lower() == erc20_a.address.lower():
        reserve_a, reserve_b = reserve0, reserve1
    else:
        reserve_a, reserve_b = reserve1, reserve0

    amount_b_desired_base = router.functions.quote(
        amount_a_base, reserve_a, reserve_b
    ).call()

    return {
        "token_a_address": erc20_a.address,
        "token_b_address": erc20_b.address,
        "decimals_a": decimals_a,
        "decimals_b": decimals_b,
        "amount_a": amount_a,
        "amount_a_base": amount_a_base,
        "amount_b_desired": amount_b_desired_base / 10**decimals_b,
        "amount_b_desired_base": amount_b_desired_base,
    }


@tool
def get_lp_amounts(chat_id: int, token_a: str, token_b: str, lp_amount: float) -> dict:
    """
    Returns the expected token amounts redeemable by burning a given amount of Uniswap V2 LP
    tokens, derived from live reserves using the proportional share formula
    (liquidity × reserve / totalSupply). Also returns base-unit amounts needed by the
    remove_liquidity and remove_liquidity_eth tools.

    Use this tool when the user wants to preview how much they'll receive before removing
    liquidity (e.g. "How much DAI and ETH will I get back for 0.5 LP tokens?"). For ETH
    pools, pass token_b="weth". When presenting the result to the user, only show
    expected_a and expected_b — never expose token addresses, base-unit fields, or liquidity.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        token_a: The ticker of the first token in the pair (e.g. "dai").
        token_b: The ticker of the second token in the pair (e.g. "weth").
        lp_amount: The amount of LP tokens to burn, in whole units (e.g. 0.5).

    Returns:
        A dict with:
          - token_a_address (str): checksummed address of token_a
          - token_b_address (str): checksummed address of token_b
          - decimals_a (int): decimal precision of token_a
          - decimals_b (int): decimal precision of token_b
          - liquidity (int): LP amount in base units
          - expected_a (float): expected token_a return in whole units
          - expected_a_base (int): expected token_a return in base units
          - expected_b (float): expected token_b return in whole units
          - expected_b_base (int): expected token_b return in base units
    """
    erc20_a = load_ierc20(chat_id=chat_id, token=token_a)
    erc20_b = load_ierc20(chat_id=chat_id, token=token_b)
    pair = load_iuniswap_pair(chat_id, token_a, token_b)

    pair_erc20 = load_ierc20(chat_id, pair.address, uniswap_pair=True)
    lp_decimals = pair_erc20.functions.decimals().call()
    decimals_a = erc20_a.functions.decimals().call()
    decimals_b = erc20_b.functions.decimals().call()
    liquidity = _to_base_units(lp_amount, lp_decimals)

    reserve0, reserve1, _ = pair.functions.getReserves().call()
    total_supply = pair_erc20.functions.totalSupply().call()
    token0 = pair.functions.token0().call()

    raw0 = (liquidity * reserve0) // total_supply
    raw1 = (liquidity * reserve1) // total_supply

    if token0.lower() == erc20_a.address.lower():
        expected_a_base, expected_b_base = raw0, raw1
    else:
        expected_a_base, expected_b_base = raw1, raw0

    return {
        "token_a_address": erc20_a.address,
        "token_b_address": erc20_b.address,
        "decimals_a": decimals_a,
        "decimals_b": decimals_b,
        "liquidity": liquidity,
        "expected_a": expected_a_base / 10**decimals_a,
        "expected_a_base": expected_a_base,
        "expected_b": expected_b_base / 10**decimals_b,
        "expected_b_base": expected_b_base,
    }


@tool
def swap_ETH_for_exact_tokens(
    chat_id: int,
    session_key_ciphertext: str,
    token_out: str,
    amount_out: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Swaps ETH for an exact amount of an ERC20 token via the Uniswap V2 router using
    swapETHForExactTokens. The user specifies how many tokens to receive; the router
    charges however much ETH is needed (plus a slippage buffer) and refunds any excess.

    Use this tool when the user wants to acquire a specific amount of an ERC20 token by
    spending ETH. The session key must be authorized for the Uniswap router. Always
    retrieve it by calling get_session_keys("uniswapv2_router") — the session is scoped
    to the router, not to the output token.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for the Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token_out: The ticker symbol of the ERC20 token to acquire (e.g. "usdc").
        amount_out: The exact amount of token_out to receive, in whole units (e.g. 100 for 100 USDC).
                    The tool converts this to base units internally.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). Applied as an
                      upward buffer on the ETH value sent so the swap succeeds even if the price
                      moves slightly. Defaults to 50 bps. Use a higher value for volatile tokens
                      or low-liquidity pools.
    Returns: A string summarizing the transaction result, including the transaction hash, status,
             ETH spent, and amount of token_out received.
    """
    print("Running swap_ETH_for_exact_tokens")
    router = load_iuniswap_router(chat_id)
    quote = get_quote_in.func(chat_id, "weth", token_out, amount_out)
    derived_check = is_derived_input_sufficient.func(
        chat_id, "eth", token_out, amount_out, slippage_bps
    )
    if not derived_check["is_sufficient"]:
        raise ToolException(f"Insufficient ETH balance for this swap.")

    value = int(
        quote["amount_in_base"] * (BPS_DENOMINATOR + slippage_bps) / BPS_DENOMINATOR
    )

    deadline = int(time.time()) + SWAP_DEADLINE_SECS
    to = load_session_handler(chat_id).address

    data = load_calldata(
        instance=router,
        fn_name="swapETHForExactTokens",
        args=[
            quote["amount_out_base"],
            quote["path"],
            to,
            deadline,
        ],
    )
    target = router.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=value,
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"ETH spent: {quote['amount_in']:.6f} ETH, "
        f"{token_out.upper()} received: {amount_out}"
    )


@tool
def swap_exact_tokens_for_tokens(
    chat_id: int,
    session_key_ciphertext: str,
    token_in: str,
    token_out: str,
    amount_in: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Swaps an exact amount of one ERC20 token (including WETH) for another using the Uniswap router.

    Use this tool when the user wants to swap a specific amount of one token for another.
    The session key must be authorized for the Uniswap router. Retrieve it by calling
    get_session_keys("uniswapv2_router") before calling this tool.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for the Uniswap router. Obtain by calling get_session_keys("uniswapv2_router").
        token_in: The ticker symbol of the ERC20 token to swap from (e.g. "usdc").
        token_out: The ticker symbol of the ERC20 token to acquire (e.g. "dai").
        amount_in: The amount of token_in to swap, in whole units (e.g. 100 for 100 USDC).
                   The tool converts this to base units internally before sending the transaction.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). The tool
                      queries getAmountsOut to find the expected output and sets amountOutMin
                      accordingly. Defaults to 50 bps. Use a higher value (e.g. 100–300) for
                      volatile tokens or low-liquidity pools.
    Returns: A string summarizing the transaction result, including the transaction hash, status,
             amount of token_in spent, and amount of token_out received.
    """
    print("Running swap_exact_tokens_for_tokens")

    if not is_exact_input_sufficient.func(chat_id, token_in, amount_in):
        raise ToolException(f"Insufficient {token_in.upper()} balance for this swap.")

    router = load_iuniswap_router(chat_id)
    quote = get_quote_out.func(chat_id, token_in, token_out, amount_in)

    amount_out_min = int(
        quote["amount_out_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS

    data = load_calldata(
        instance=router,
        fn_name="swapExactTokensForTokens",
        args=[
            quote["amount_in_base"],
            amount_out_min,
            quote["path"],
            load_session_handler(chat_id).address,
            deadline,
        ],
    )
    target = router.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"{token_in.upper()} spent: {amount_in}, "
        f"{token_out.upper()} received: {quote['amount_out']:.6f}"
    )


@tool
def swap_tokens_for_exact_tokens(
    chat_id: int,
    session_key_ciphertext: str,
    token_in: str,
    token_out: str,
    amount_out: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Swaps an amount of one ERC20 token (including WETH) for an exact amount of another using the Uniswap router.

    Use this tool when the user wants to acquire a specific amount of one token by swapping another.
    The session key must be authorized for the Uniswap router. Retrieve it by calling
    get_session_keys("uniswapv2_router") before calling this tool.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for the Uniswap router. Obtain by calling get_session_keys("uniswapv2_router").
        token_in: The ticker symbol of the ERC20 token to swap from (e.g. "usdc").
        token_out: The ticker symbol of the ERC20 token to acquire (e.g. "dai").
        amount_out: The exact amount of token_out to acquire, in whole units (e.g. 100 for 100 DAI).
                    The tool converts this to base units internally before sending the transaction.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). The tool
                      queries getAmountsIn to find the expected input cost and sets amountInMax
                      accordingly. Defaults to 50 bps. Use a higher value (e.g. 100–300) for
                      volatile tokens or low-liquidity pools.
    Returns: A string summarizing the transaction result, including the transaction hash, status,
             amount of token_in spent, and amount of token_out received.
    """
    print("Running swap_tokens_for_exact_tokens")
    router = load_iuniswap_router(chat_id)
    quote = get_quote_in.func(chat_id, token_in, token_out, amount_out)
    derived_check = is_derived_input_sufficient.func(
        chat_id, token_in, token_out, amount_out, slippage_bps
    )
    if not derived_check["is_sufficient"]:
        raise ToolException(f"Insufficient {token_in.upper()} balance for this swap.")

    amount_in_max = int(
        quote["amount_in_base"] * (BPS_DENOMINATOR + slippage_bps) / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS

    data = load_calldata(
        instance=router,
        fn_name="swapTokensForExactTokens",
        args=[
            quote["amount_out_base"],
            amount_in_max,
            quote["path"],
            load_session_handler(chat_id).address,
            deadline,
        ],
    )
    target = router.address
    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=int(0),
        data=data,
    )
    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"{token_in.upper()} spent: {quote['amount_in']:.6f}, "
        f"{token_out.upper()} received: {amount_out}"
    )


@tool
def swap_exact_tokens_for_ETH(
    chat_id: int,
    session_key_ciphertext: str,
    token_in: str,
    amount_in: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Swaps an exact amount of an ERC20 token for ETH via the Uniswap V2 router
    using swapExactTokensForETH. The user specifies how much of token_in to sell;
    they receive however much ETH the pool gives back (minus slippage).

    Use this tool when the user wants to sell a specific amount of an ERC20 token
    and receive ETH in return. The session key must be authorized for the Uniswap
    router. Always retrieve it by calling get_session_keys("uniswapv2_router")
    before calling this tool.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for the Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token_in: The ticker symbol of the ERC20 token to sell (e.g. "usdc", "dai").
        amount_in: The exact amount of token_in to sell, in whole units (e.g. 100 for 100 USDC).
                   The tool converts this to base units internally.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). The tool
                      queries getAmountsOut to find the expected ETH output and sets amountOutMin
                      accordingly. Defaults to 50 bps. Use a higher value for volatile tokens
                      or low-liquidity pools.
    Returns: A string summarizing the transaction result, including the transaction hash, status,
             amount of token_in spent, and ETH received.
    """
    print("Running swap_exact_tokens_for_ETH")

    if not is_exact_input_sufficient.func(chat_id, token_in, amount_in):
        raise ToolException(f"Insufficient {token_in.upper()} balance for this swap.")

    router = load_iuniswap_router(chat_id)
    quote = get_quote_out.func(chat_id, token_in, "weth", amount_in)

    amount_out_min = int(
        quote["amount_out_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS
    to = load_session_handler(chat_id).address

    data = load_calldata(
        instance=router,
        fn_name="swapExactTokensForETH",
        args=[
            quote["amount_in_base"],
            amount_out_min,
            quote["path"],
            to,
            deadline,
        ],
    )

    target = router.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"{token_in.upper()} spent: {amount_in}, "
        f"ETH received: {quote['amount_out']:.6f}"
    )


@tool
def swap_tokens_for_exact_ETH(
    chat_id: int,
    session_key_ciphertext: str,
    token_in: str,
    amount_out_eth: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Swaps however much of an ERC20 token is needed to receive an exact amount of ETH via
    the Uniswap V2 router using swapTokensForExactETH. The user specifies how much ETH
    they want to receive; the router spends as much token_in as required (up to amountInMax).

    Use this tool when the user wants to receive a specific amount of ETH by selling an
    ERC20 token. The session key must be authorized for the Uniswap router. Always retrieve
    it by calling get_session_keys("uniswapv2_router") before calling this tool.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for the Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token_in: The ticker symbol of the ERC20 token to sell (e.g. "usdc", "dai").
        amount_out_eth: The exact amount of ETH to receive, in whole units (e.g. 1.5 for 1.5 ETH).
                        The tool converts this to wei internally.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). Applied as an
                      upward buffer on amountInMax so the swap succeeds even if the price moves
                      slightly. Defaults to 50 bps. Use a higher value for volatile tokens or
                      low-liquidity pools.
    Returns: A string summarizing the transaction result, including the transaction hash, status,
             amount of token_in spent, and ETH received.
    """
    print("Running swap_tokens_for_exact_ETH")
    router = load_iuniswap_router(chat_id)
    quote = get_quote_in.func(chat_id, token_in, "weth", amount_out_eth)
    derived_check = is_derived_input_sufficient.func(
        chat_id, token_in, "eth", amount_out_eth, slippage_bps
    )
    if not derived_check["is_sufficient"]:
        raise ToolException(f"Insufficient {token_in.upper()} balance for this swap.")

    amount_in_max = int(
        quote["amount_in_base"] * (BPS_DENOMINATOR + slippage_bps) / BPS_DENOMINATOR
    )

    deadline = int(time.time()) + SWAP_DEADLINE_SECS
    to = load_session_handler(chat_id).address

    data = load_calldata(
        instance=router,
        fn_name="swapTokensForExactETH",
        args=[
            quote["amount_out_base"],
            amount_in_max,
            quote["path"],
            to,
            deadline,
        ],
    )

    target = router.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"{token_in.upper()} spent: {quote['amount_in']:.6f}, "
        f"ETH received: {amount_out_eth}"
    )


@tool
def swap_exact_ETH_for_tokens(
    chat_id: int,
    session_key_ciphertext: str,
    token_out: str,
    eth_amount_in: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Swaps an exact amount of ETH for an ERC20 token via the Uniswap V2 router using
    swapExactETHForTokens. The user specifies how much ETH to spend; they receive
    however many tokens the pool gives back (minus slippage).

    Use this tool when the user wants to spend a specific amount of ETH and receive as
    many tokens as possible in return. The session key must be authorized for the Uniswap
    router. Always retrieve it by calling get_session_keys("uniswapv2_router") — the
    session is scoped to the router, not to the output token.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized
                                for the Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token_out: The ticker symbol of the ERC20 token to receive (e.g. "usdc", "dai").
        eth_amount_in: The exact amount of ETH to spend, in whole units (e.g. 1.5 for 1.5 ETH).
                       The tool converts this to wei internally and forwards it as msg.value.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). The tool
                      queries getAmountsOut to find the expected token output and sets amountOutMin
                      accordingly. Defaults to 50 bps. Use a higher value for volatile tokens
                      or low-liquidity pools.
    Returns: A string summarizing the transaction result, including the transaction hash, status,
             ETH spent, and amount of token_out received.
    """
    print("Running swap_exact_ETH_for_tokens")

    if not is_exact_input_sufficient.func(chat_id, "eth", eth_amount_in):
        raise ToolException(f"Insufficient ETH balance for this swap.")

    router = load_iuniswap_router(chat_id)
    quote = get_quote_out.func(chat_id, "weth", token_out, eth_amount_in)

    amount_out_min = int(
        quote["amount_out_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS
    to = load_session_handler(chat_id).address

    data = load_calldata(
        instance=router,
        fn_name="swapExactETHForTokens",
        args=[
            amount_out_min,
            quote["path"],
            to,
            deadline,
        ],
    )

    target = router.address

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=target,
        value=quote["amount_in_base"],
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"ETH spent: {eth_amount_in}, "
        f"{token_out.upper()} received: {quote['amount_out']:.6f}"
    )


@tool
def add_liquidity(
    chat_id: int,
    session_key_ciphertext: str,
    token_a: str,
    amount_a: float,
    token_b: str = "weth",
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Adds liquidity to a Uniswap V2 pool via addLiquidity. The user specifies token_a and an
    amount; the proportional token_b amount is derived from live pool reserves via router.quote()
    so the deposit always matches the current pool ratio.

    Use this tool when the user wants to provide liquidity to a Uniswap V2 pool. The session
    key must be authorized for the Uniswap router. Retrieve it by calling
    get_session_keys("uniswapv2_router") before calling this tool. Both tokens must already
    have their ERC20 allowance set for the router so it can pull both amounts.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized for the
                                Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token_a: The ticker symbol of the first token to deposit (e.g. "dai").
        amount_a: The desired amount of token_a to deposit, in whole units (e.g. 2500 for 2500 DAI).
                  The proportional token_b amount is computed from pool reserves automatically.
        token_b: The ticker symbol of the second token to deposit. Defaults to "weth", which is
                 the standard pairing on Uniswap V2. Only override if depositing into a non-WETH pair.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). Applied to
                      both amountAMin and amountBMin. Defaults to 50 bps.

    Returns:
        A string summarizing the transaction result, including the transaction hash and status.
    """
    print("Running add_liquidity")
    router = load_iuniswap_router(chat_id)
    quote = get_pool_quote.func(chat_id, token_a, token_b, amount_a)

    liquidity_check = is_liquidity_sufficient.func(chat_id, token_a, amount_a, token_b)
    if not liquidity_check["is_sufficient"]:
        raise ToolException(
            f"Insufficient token balance. Ensure the wallet holds enough {token_a.upper()} and {token_b.upper()} to cover the deposit."
        )

    amount_a_min = int(
        quote["amount_a_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    amount_b_min = int(
        quote["amount_b_desired_base"]
        * (BPS_DENOMINATOR - slippage_bps)
        / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS

    data = load_calldata(
        instance=router,
        fn_name="addLiquidity",
        args=[
            quote["token_a_address"],
            quote["token_b_address"],
            quote["amount_a_base"],
            quote["amount_b_desired_base"],
            amount_a_min,
            amount_b_min,
            load_session_handler(chat_id).address,
            deadline,
        ],
    )

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=router.address,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"{token_a.upper()} min deposited: {amount_a_min / 10**quote['decimals_a']:.6f}, "
        f"{token_b.upper()} min deposited: {amount_b_min / 10**quote['decimals_b']:.6f}"
    )


@tool
def add_liquidity_eth(
    chat_id: int,
    session_key_ciphertext: str,
    token: str,
    amount_token: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Adds liquidity to a Uniswap V2 token/ETH pool via addLiquidityETH. The user specifies
    the ERC20 token and an amount; the proportional ETH amount is derived from live pool
    reserves via router.quote() so the deposit always matches the current pool ratio. ETH
    is forwarded directly as msg.value — no prior WETH wrapping is required.

    Use this tool when the user wants to add liquidity to a Uniswap V2 pool using raw ETH
    (as opposed to WETH). The session key must be authorized for the Uniswap router. Retrieve
    it by calling get_session_keys("uniswapv2_router") before calling this tool. The token
    must already have its ERC20 allowance set for the router so it can pull the token amount.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized for the
                                Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token: The ticker symbol of the ERC20 token to deposit alongside ETH (e.g. "dai").
        amount_token: The desired amount of the ERC20 token to deposit, in whole units
                      (e.g. 2500 for 2500 DAI). The proportional ETH amount is computed
                      from pool reserves automatically.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). Applied
                      to both amountTokenMin and amountETHMin. Defaults to 50 bps.

    Returns:
        A string summarizing the transaction result, including the transaction hash, status,
        token min deposited, and ETH min deposited.
    """
    print("Running add_liquidity_eth")
    router = load_iuniswap_router(chat_id)
    quote = get_pool_quote.func(chat_id, token, "weth", amount_token)

    liquidity_check = is_liquidity_sufficient.func(chat_id, token, amount_token, "eth")
    if not liquidity_check["is_sufficient"]:
        raise ToolException(
            f"Insufficient token balance. Ensure the wallet holds enough {token.upper()} and ETH to cover the deposit."
        )

    amount_token_min = int(
        quote["amount_a_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    amount_eth_min = int(
        quote["amount_b_desired_base"]
        * (BPS_DENOMINATOR - slippage_bps)
        / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS

    data = load_calldata(
        instance=router,
        fn_name="addLiquidityETH",
        args=[
            quote["token_a_address"],
            quote["amount_a_base"],
            amount_token_min,
            amount_eth_min,
            load_session_handler(chat_id).address,
            deadline,
        ],
    )

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=router.address,
        value=quote["amount_b_desired_base"],
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")

    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"{token.upper()} min deposited: {amount_token_min / 10**quote['decimals_a']:.6f}, "
        f"ETH min deposited: {amount_eth_min / WEI_PER_ETH:.6f}"
    )


@tool
def remove_liquidity(
    chat_id: int,
    session_key_ciphertext: str,
    token_a: str,
    lp_amount: float,
    token_b: str = "weth",
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Removes liquidity from a Uniswap V2 pool via removeLiquidity. The user specifies the
    LP token amount to burn; the expected return amounts for both tokens are derived from
    live pool reserves using the proportional share formula (liquidity * reserve / totalSupply).
    Slippage is applied downward to compute amountAMin and amountBMin.

    Use this tool when the user wants to withdraw liquidity from a Uniswap V2 pool and
    receive both tokens back. The session key must be authorized for the Uniswap router.
    Retrieve it by calling get_session_keys("uniswapv2_router") before calling this tool.
    The LP token allowance for the router must already be set.

    Note: removeLiquidity credits the session budget back rather than charging it, so no
    preflight budget check is required — only session validity needs to be confirmed.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized for the
                                Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token_a: The ticker symbol of the first token in the pair (e.g. "dai").
        lp_amount: The amount of LP tokens to burn, in whole units (e.g. 0.5 for 0.5 LP tokens).
                   The tool converts this to base units using the pair's decimals internally.
        token_b: The ticker symbol of the second token in the pair. Defaults to "weth".
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). Applied
                      as a downward buffer on amountAMin and amountBMin. Defaults to 50 bps.

    Returns:
        A string summarizing the transaction result, including the transaction hash and status.
    """
    print("Running remove_liquidity")
    if not is_liquidity_removal_sufficient.func(chat_id, token_a, token_b, lp_amount):
        raise ToolException(
            "Insufficient LP tokens. Use get_liquidity_token_balance to check your balance."
        )

    router = load_iuniswap_router(chat_id)
    lp = get_lp_amounts.func(chat_id, token_a, token_b, lp_amount)

    amount_a_min = int(
        lp["expected_a_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    amount_b_min = int(
        lp["expected_b_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS

    data = load_calldata(
        instance=router,
        fn_name="removeLiquidity",
        args=[
            lp["token_a_address"],
            lp["token_b_address"],
            lp["liquidity"],
            amount_a_min,
            amount_b_min,
            load_session_handler(chat_id).address,
            deadline,
        ],
    )

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=router.address,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"Min {token_a.upper()} returned: {amount_a_min / 10**lp['decimals_a']:.6f}, "
        f"Min {token_b.upper()} returned: {amount_b_min / 10**lp['decimals_b']:.6f}"
    )


@tool
def remove_liquidity_eth(
    chat_id: int,
    session_key_ciphertext: str,
    token: str,
    lp_amount: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """
    Removes liquidity from a Uniswap V2 token/ETH pool via removeLiquidityETH. The user
    specifies the ERC20 token and the LP amount to burn; expected return amounts for the
    token and ETH are derived from live reserves using the proportional share formula
    (liquidity * reserve / totalSupply). Slippage is applied downward to compute
    amountTokenMin and amountETHMin. The router unwraps the WETH share to raw ETH before
    sending it back to the wallet.

    Use this tool when the user wants to remove liquidity from a token/ETH pool and receive
    the ERC20 token and raw ETH back. The session key must be authorized for the Uniswap
    router. Retrieve it by calling get_session_keys("uniswapv2_router") before calling this
    tool. The LP token allowance for the router must already be set.

    Note: removeLiquidityETH credits the session budget back rather than charging it, so no
    preflight budget check is required — only session validity needs to be confirmed.

    Args:
        chat_id: The Telegram chat ID of the user making the request.
        session_key_ciphertext: The Vault ciphertext for the session key authorized for the
                                Uniswap router. Obtain via get_session_keys("uniswapv2_router").
        token: The ticker symbol of the ERC20 token in the pair (e.g. "dai"). The other
               token is always ETH.
        lp_amount: The amount of LP tokens to burn, in whole units (e.g. 0.5 for 0.5 LP
                   tokens). The tool converts this to base units internally.
        slippage_bps: Maximum acceptable slippage in basis points (e.g. 50 = 0.5%). Applied
                      as a downward buffer on amountTokenMin and amountETHMin. Defaults to 50 bps.

    Returns:
        A string summarizing the transaction result, including the transaction hash and status.
    """
    print("Running remove_liquidity_eth")
    if not is_liquidity_removal_sufficient.func(chat_id, token, "weth", lp_amount):
        raise ToolException(
            "Insufficient LP tokens. Use get_liquidity_token_balance to check your balance."
        )

    router = load_iuniswap_router(chat_id)
    lp = get_lp_amounts.func(chat_id, token, "weth", lp_amount)

    amount_token_min = int(
        lp["expected_a_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    amount_eth_min = int(
        lp["expected_b_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR
    )
    deadline = int(time.time()) + SWAP_DEADLINE_SECS

    data = load_calldata(
        instance=router,
        fn_name="removeLiquidityETH",
        args=[
            lp["token_a_address"],
            lp["liquidity"],
            amount_token_min,
            amount_eth_min,
            load_session_handler(chat_id).address,
            deadline,
        ],
    )

    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=router.address,
        value=int(0),
        data=data,
    )

    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"Min {token.upper()} returned: {amount_token_min / 10**lp['decimals_a']:.6f}, "
        f"Min ETH returned: {amount_eth_min / WEI_PER_ETH:.6f}"
    )


"""
 /*//////////////////////////////////////////////////////////////
                        JOB AND SCHEDULING TOOLS
//////////////////////////////////////////////////////////////*/
"""


async def recurring_transfer_job(context) -> None:
    """
    PTB job callback that executes a single iteration of a scheduled ERC20 transfer.

    Reads transfer details from context.job.data, checks session validity, and sends
    the tokens. If the session has expired the job removes itself and notifies the user.
    """
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    transfer_id = job_data["transfer_id"]
    token = job_data["token"]
    recipient = job_data["recipient"]
    amount = job_data["amount"]

    def _execute():
        session_key, session_key_ciphertext = get_session_keys.func(chat_id, token)
        session_handler = load_session_handler(chat_id)
        if not session_handler.functions.isSessionActive(session_key).call():
            return None, None, "expired"
        erc20 = load_ierc20(chat_id=chat_id, token=token)
        recipient_addr = _get_contact(chat_id, recipient)
        decimals = erc20.functions.decimals().call()
        value = _to_base_units(amount, decimals)
        calldata = load_calldata(
            instance=erc20, fn_name="transfer", args=[recipient_addr, value]
        )
        tx_hash, receipt = send_user_op_as_session(
            chat_id=chat_id,
            key_ciphertext=session_key_ciphertext,
            target=erc20.address,
            value=int(0),
            data=calldata,
        )
        return tx_hash, receipt, "ok"

    try:
        tx_hash, receipt, status = await asyncio.to_thread(_execute)
    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Recurring transfer error: {e}",
        )
        return

    if status == "expired":
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Recurring transfer of {amount} {token.upper()} to {recipient} "
                f"was skipped — the {token.upper()} session key has expired. "
                f"Please renew your session to resume scheduled transfers."
            ),
        )
        context.job.schedule_removal()
        _delete_recurring_transfer(transfer_id)
        return

    if receipt["status"] == 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Recurring transfer of {amount} {token.upper()} to {recipient} sent.\n"
                f"Tx: `{tx_hash.hex()}`"
            ),
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Recurring transfer of {amount} {token.upper()} to {recipient} failed.\n"
                f"Tx: `{tx_hash.hex()}`"
            ),
        )


def _make_schedule_tool(job_queue):
    @tool
    def schedule_recurring_transfer(
        chat_id: int, token: str, recipient: str, amount: float, interval_hrs: int
    ) -> str:
        """
        Schedules a recurring ERC20 transfer that repeats at a fixed interval.

        Use this tool when the user confirms they want a transfer to repeat automatically.
        The recipient must already be a saved contact. The job will fire every `interval_hrs`
        hours, starting after the first interval has elapsed.

        Args:
            chat_id: The Telegram chat ID of the user making the request.
            token: The token ticker symbol to transfer (e.g. "usdc").
            recipient: The name of the saved contact to send tokens to (e.g. "Sandy").
            amount: The amount of tokens to send each time, in whole units (e.g. 100 for 100 USDC).
            interval_hrs: How often to repeat the transfer, in hours (e.g. 24 for daily).

        Returns:
            A confirmation string including the assigned transfer ID.
        """
        print("Running schedule_recurring_transfer")
        transfer_id = _save_recurring_transfer(
            chat_id, token, recipient, amount, interval_hrs
        )
        interval_secs = interval_hrs * SECONDS_PER_HOUR
        job_queue.run_repeating(
            recurring_transfer_job,
            interval=interval_secs,
            first=interval_secs,
            chat_id=chat_id,
            name=f"recurring_{transfer_id}",
            data={
                "chat_id": chat_id,
                "transfer_id": transfer_id,
                "token": token,
                "recipient": recipient,
                "amount": amount,
            },
        )
        return (
            f"Recurring transfer #{transfer_id} scheduled: "
            f"{amount} {token.upper()} to {recipient} every {interval_hrs}h."
        )

    return schedule_recurring_transfer


def _make_cancel_tool(job_queue):
    @tool
    def cancel_recurring_transfer(chat_id: int, transfer_id: int) -> str:
        """
        Cancels a scheduled recurring transfer by its ID.

        Use this tool when the user wants to stop a recurring transfer. Removes the
        scheduled job and deletes the record from the database.

        Args:
            chat_id: The Telegram chat ID of the user making the request.
            transfer_id: The numeric ID of the recurring transfer to cancel (visible in
                         get_recurring_transfers output).

        Returns:
            A confirmation string.
        """
        print("Running cancel_recurring_transfer")
        for job in job_queue.get_jobs_by_name(f"recurring_{transfer_id}"):
            job.schedule_removal()
        _delete_recurring_transfer(transfer_id, chat_id)
        return f"Recurring transfer #{transfer_id} cancelled."

    return cancel_recurring_transfer


"""
 /*//////////////////////////////////////////////////////////////
                       ERC-8004 TOOLS
//////////////////////////////////////////////////////////////*/
"""



@tool
def get_agent_identity(chat_id: int) -> dict:
    """
    Looks up this agent's ERC-8004 on-chain identity.

    Returns the token ID and agent card URI if registered, or indicates that the
    agent is not registered. Call this when the user asks about the agent's identity
    or wants to verify on-chain registration.

    Args:
        chat_id: The Telegram chat ID of the user.

    Returns:
        A dict with 'registered' (bool), and if True: 'token_id' (int) and 'card_uri' (str).
    """
    print("Running get_agent_identity")
    _, chain_id, _ = load_network_config(chat_id)
    agent_id = get_agent_id(chain_id)
    identity_registry = load_identity_registry(chat_id)
    try:
        identity_registry.functions.ownerOf(agent_id).call()
        card_uri = identity_registry.functions.tokenURI(agent_id).call()
        return {"registered": True, "token_id": agent_id, "card_uri": card_uri}
    except Exception:
        return {"registered": False}


@tool
def get_agent_reputation(chat_id: int) -> dict:
    """
    Returns the on-chain reputation score and feedback count for this agent.

    Call this when the user wants to check how the agent is rated.

    Args:
        chat_id: The Telegram chat ID of the user.

    Returns:
        A dict with 'token_id' (int), 'average_score' (float, 0–100), and 'feedback_count' (int).
    """
    print("Running get_agent_reputation")
    _, chain_id, _ = load_network_config(chat_id)
    agent_id = get_agent_id(chain_id)
    client=load_session_handler(chat_id).address
    reputation_registry = load_reputation_registry(chat_id)

    count, summary_value, summary_value_decimals = reputation_registry.functions.getSummary(
        agent_id, [client], "", ""
    ).call()
    average_score = round(summary_value / (10 ** summary_value_decimals) / count, 2) if count > 0 else 0
    return {
        "token_id": agent_id,
        "average_score": average_score,
        "feedback_count": count,
    }


@tool
def post_reputation_feedback(
    chat_id: int, session_key_ciphertext: str, score: int, tags: str
) -> dict:
    """
    Posts on-chain feedback for this agent in the ERC-8004 Reputation Registry.

    Call this when the user wants to rate the agent after an interaction. The score must
    be between 0 and 100. Tags are comma-separated descriptors (e.g. "reliable,fast").
    Always retrieve the session key by calling get_session_keys("reputation_registry")
    before calling this tool.

    Args:
        chat_id: The Telegram chat ID of the user.
        session_key_ciphertext: Vault ciphertext for the reputation_registry session key.
                                Obtain by calling get_session_keys("reputation_registry").
        score: Rating from 0 (worst) to 100 (best).
        tags: Comma-separated tags describing the interaction (e.g. "reliable,fast").

    Returns:
        A dict with 'tx_hash' (str) and 'status' (int, 1 = success).
    """
    print("Running post_reputation_feedback")

    if score < 0 or score > 100:
        raise ToolException("Score must be between 0 and 100.")

    _, chain_id, _ = load_network_config(chat_id)
    agent_id = get_agent_id(chain_id)
    reputation_registry = load_reputation_registry(chat_id)

    # score 0-100 → int128 value with valueDecimals=0; tag goes into tag1, tag2 empty
    data = load_calldata(
        instance=reputation_registry,
        fn_name="giveFeedback",
        args=[agent_id, score, 0, tags, "", "", "", b"\x00" * 32],
    )
    tx_hash, receipt = send_user_op_as_session(
        chat_id=chat_id,
        key_ciphertext=session_key_ciphertext,
        target=reputation_registry.address,
        value=int(0),
        data=data,
    )
    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return {"tx_hash": tx_hash.hex(), "status": receipt["status"]}


def get_tools(job_queue=None):
    tools_list = [
        # Database tools
        get_supported_tokens,
        get_all_sessions,
        save_contact,
        get_contact,
        get_all_contacts,
        delete_contact,
        get_recurring_transfers,
        # Blockchain tools
        get_eth_balance,
        send_eth,
        get_session_keys,
        check_session_validity,
        check_remaining_budget,
        check_spending_within_budget,
        preflight_check,
        swap_exact_tokens_for_tokens,
        swap_tokens_for_exact_tokens,
        get_price,
        get_usd_value,
        get_erc20_balance,
        get_contact_erc20_balance,
        get_erc20_allowance,
        wrap_eth,
        is_derived_input_sufficient,
        is_exact_input_sufficient,
        is_liquidity_sufficient,
        is_liquidity_removal_sufficient,
        get_quote_in,
        get_quote_out,
        get_pool_quote,
        get_lp_amounts,
        swap_ETH_for_exact_tokens,
        swap_exact_tokens_for_ETH,
        swap_tokens_for_exact_ETH,
        swap_exact_ETH_for_tokens,
        add_liquidity,
        add_liquidity_eth,
        remove_liquidity,
        remove_liquidity_eth,
        get_liquidity_token_balance,
        transfer_erc20,
        approve_erc20,
        transferFrom_erc20,
        # ERC-8004 tools
        get_agent_identity,
        get_agent_reputation,
        post_reputation_feedback,
    ]




    
    if job_queue is not None:
        tools_list.extend(
            [
                _make_schedule_tool(job_queue),
                _make_cancel_tool(job_queue),
            ]
        )
    for t in tools_list:
        t.handle_tool_error = True
    return tools_list
