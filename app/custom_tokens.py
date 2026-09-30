"""
Checks a token a user wants to add by address, MetaMask-style, before it is saved to their list.

What adding a token does and does not mean: the wallet can already hold and move ANY ERC-20, and
one the oracle cannot price sits outside the spending cap whether or not it is on a list. So the
list grants no permission on chain. It decides what the dashboard shows and what the assistant can
name -- which is why the checks below care about the token's SYMBOL as much as its code: the symbol
reaches the assistant (langchain-erc20 reads symbol() on chain for every quote's action line) and
the user. A token calling itself "USDC", or carrying instructions in its symbol, is a trick aimed
at one of them, so it is refused rather than relabelled.

A token Mitfah already lists can be added the same way, by address: it goes on the user's dashboard
(db.dashboard_tokens) under the listed ticker, and none of the symbol rules apply -- Mitfah chose
it, and the oracle prices it, so it can also count toward the limit.

Everything is read from the chain here; nothing the browser says about the token is trusted.
"""

import re

from hexbytes import HexBytes
from web3 import Web3

from constants import get_chain_display_name, get_native_asset_ticker, get_native_wrapped_ticker
from contracts import load_ierc20
from db import (
    get_custom_token,
    get_custom_tokens,
    get_dashboard_tokens,
    get_supported_token_by_address,
    get_supported_tokens_by_chain_id,
)
from network_config import load_network_config

# Each added token costs the dashboard read one balanceOf, so the list is capped per network.
MAX_CUSTOM_TOKENS_PER_CHAIN = 25

# 1-12 characters of letters, digits and . _ - $ -- room for every ordinary symbol (stETH, USD.e,
# 1INCH, $PEPE) and nothing that can carry a sentence, markup or a look-alike Unicode letter.
TICKER_PATTERN = re.compile(r"^[A-Za-z0-9._$-]{1,12}$")

# ERC-20 allows any uint8, but nothing real uses more than 36 and larger values overflow the
# float amounts shown to the assistant. MetaMask draws the same line.
MAX_DECIMALS = 36

# A name is only ever shown in the web app (React escapes it) and never handed to the assistant, so
# it is kept as a courtesy: control characters stripped, length capped.
MAX_NAME_LENGTH = 64

# For the bytes32 fallback only; everything else goes through the ERC-20 ABI.
_SELECTOR_NAME = "0x06fdde03"
_SELECTOR_SYMBOL = "0x95d89b41"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class CustomTokenError(ValueError):
    """A token that cannot be added, with a message written for the user."""


def _read(fn):
    """Calls a bound contract function, or None if it reverts, returns nothing, or won't decode."""
    try:
        return fn.call()
    except Exception:  # noqa: BLE001 -- any failure means "this does not answer like an ERC-20"
        return None


def _read_bytes32_text(w3: Web3, address: str, selector: str) -> str | None:
    """
    symbol() or name() from a pre-2018 token (MKR, SAI) that returns bytes32 instead of a string --
    the one legitimate ERC-20 shape the standard ABI can't decode.
    """
    try:
        raw = bytes(w3.eth.call({"to": address, "data": selector}))
    except Exception:  # noqa: BLE001
        return None
    if len(raw) != 32:
        return None
    try:
        return raw.rstrip(b"\x00").decode("utf-8") or None
    except UnicodeDecodeError:
        return None


def _check_address_again(network: str, what: str) -> CustomTokenError:
    """The refusal for an address that isn't an ERC-20 token, asking the user to check it."""
    return CustomTokenError(
        f"{what}, so this doesn't look like an ERC-20 token on {network}. Check the token address "
        f"again, and that it's the token's address on {network}."
    )


def reserved_tickers(chain_id: int) -> set[str]:
    """
    Names an added token may not take on `chain_id`: every listed ticker, plus the native asset's
    names -- "eth" means the chain's native asset to every tool in this app, whatever the chain.
    """
    reserved = {t["ticker"].lower() for t in get_supported_tokens_by_chain_id(chain_id)}
    reserved.add("eth")
    for lookup in (get_native_asset_ticker, get_native_wrapped_ticker):
        try:
            reserved.add(lookup(chain_id).lower())
        except ValueError:
            pass
    return reserved


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    name = _CONTROL_CHARS.sub("", name).strip()
    return name[:MAX_NAME_LENGTH] or None


def inspect_custom_token(user_id: int, chain_id: int, address: str, wallet_address: str) -> dict:
    """
    Reads a token off the chain and decides whether this user may add it on `chain_id`.

    The token is read through contracts.load_ierc20 -- the same ERC-20 contract binding the
    assistant's tools use -- and it counts as an ERC-20 only if its symbol() and decimals() both
    answer. load_ierc20 finds the chain through the user's network, so the caller must have made
    `chain_id` the acting network first (db.acting_network); anything else is refused rather than
    read from the wrong chain.

    A listed token is accepted unless it is on the user's dashboard already; it keeps the listed
    ticker and skips the symbol rules and the per-chain cap, which are about unpriced tokens.

    @param address         What the user pasted.
    @param wallet_address  The user's wallet on this chain, for the balance preview.
    @return  {"address", "ticker", "symbol", "name", "decimals", "balance_raw", "listed"} -- ticker
             is the symbol lowercased (a listed token's: its listed ticker), balance_raw a string (a
             uint256 does not survive JSON's float64), listed True for a token Mitfah prices.
    @raises CustomTokenError  With a message for the user, if the token can't be added.
    """
    network = get_chain_display_name(chain_id)
    try:
        address = Web3.to_checksum_address(address.strip())
    except (ValueError, TypeError, AttributeError):
        raise CustomTokenError("That isn't a valid token address. It should start with 0x and have 40 more characters.")
    if int(address, 16) == 0:
        raise CustomTokenError("That's the zero address, not a token.")
    if address == Web3.to_checksum_address(wallet_address):
        raise CustomTokenError("That's your Mitfah wallet's own address, not a token.")

    listed = get_supported_token_by_address(chain_id, address)
    if listed is not None:
        if any(t["ticker"] == listed["ticker"] for t in get_dashboard_tokens(user_id, chain_id)):
            raise CustomTokenError(f"{listed['ticker'].upper()} is already on your dashboard.")
    else:
        existing = get_custom_token(user_id, chain_id, address)
        if existing is not None:
            raise CustomTokenError(f"You've already added this token ({existing['ticker'].upper()}).")
        if len(get_custom_tokens(user_id, chain_id)) >= MAX_CUSTOM_TOKENS_PER_CHAIN:
            raise CustomTokenError(
                f"You can add up to {MAX_CUSTOM_TOKENS_PER_CHAIN} tokens on each network. Remove one first."
            )

    w3, acting_chain_id, _ = load_network_config(user_id)
    if acting_chain_id != chain_id:
        raise RuntimeError(f"inspect_custom_token needs chain {chain_id} as the acting network, not {acting_chain_id}")
    if w3.eth.get_code(address) in (b"", HexBytes("0x")):
        raise _check_address_again(network, "There's no contract at this address")

    erc20 = load_ierc20(user_id, address)
    symbol = _read(erc20.functions.symbol()) or _read_bytes32_text(w3, address, _SELECTOR_SYMBOL)
    decimals = _read(erc20.functions.decimals())
    if symbol is None or decimals is None:
        missing = " and ".join(n for n, v in (("a symbol", symbol), ("decimals", decimals)) if v is None)
        raise _check_address_again(network, f"Mitfah couldn't read {missing} from this contract")
    if decimals > MAX_DECIMALS:
        raise _check_address_again(network, f"This contract reports {decimals} decimals")
    balance = _read(erc20.functions.balanceOf(Web3.to_checksum_address(wallet_address)))
    if balance is None:
        raise _check_address_again(network, "This contract can't report a balance")
    name = _clean_name(_read(erc20.functions.name()) or _read_bytes32_text(w3, address, _SELECTOR_NAME))
    symbol = symbol.strip()
    token = {"address": address, "name": name, "decimals": decimals, "balance_raw": str(balance)}

    if listed is not None:
        return {**token, "ticker": listed["ticker"], "symbol": symbol, "listed": True}

    if not TICKER_PATTERN.fullmatch(symbol):
        shown = symbol[:24] + ("…" if len(symbol) > 24 else "")
        raise CustomTokenError(
            f'This token\'s symbol ("{shown}") isn\'t one Mitfah accepts: it has to be 1–12 letters, '
            "digits or . _ - $. The assistant reads token symbols, so unusual ones are refused."
        )
    ticker = symbol.lower()
    if ticker in reserved_tickers(chain_id):
        raise CustomTokenError(
            f"This token calls itself {symbol.upper()}, the same as a token Mitfah already lists on "
            f"{network}. Copying a well-known symbol is a common way to fake a token, so it can't be added."
        )
    clash = get_custom_token(user_id, chain_id, ticker)
    if clash is not None:
        raise CustomTokenError(
            f"You already added a different token called {symbol.upper()} ({clash['address']}). "
            "Remove that one first to add this one."
        )
    return {**token, "ticker": ticker, "symbol": symbol, "listed": False}
