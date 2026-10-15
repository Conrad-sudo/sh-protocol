"""
Tokens a user adds to the dashboard, by contract address or from Mitfah's list.
"""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from web3 import Web3

from auth import get_current_user
from constants import (
    get_always_counted_ticker,
    get_native_asset_ticker,
)
from contracts import read_spending_config
from custom_tokens import CustomTokenError, inspect_custom_token
from db import (
    acting_network,
    add_dashboard_tokens,
    delete_custom_token,
    get_dashboard_tokens,
    get_supported_token_by_address,
    get_wallet_address,
    remove_dashboard_token,
    save_custom_token,
)
from routes.deps import _load_wallet_for_chain, _resolve_chain, limiter

router = APIRouter()


#
# Tokens a user adds by contract address, MetaMask-style. They are a list Mitfah keeps, not a
# setting on the wallet: the wallet can already hold and move any ERC-20, and one without a price
# feed sits outside the spending cap whether it is listed here or not. So adding or removing one
# needs no signature and changes nothing on chain -- it decides what the dashboard shows and which
# names the assistant resolves. Every check (a real ERC-20, a symbol safe to show the assistant, no
# copy of a listed ticker) is in custom_tokens.py and runs against the chain, never the request.
#
# A token Mitfah lists can be added the same way: it goes on the dashboard (dashboard_tokens), and
# the web app offers to count it -- a separate owner transaction (watched-tokens/prepare). Either
# kind can be removed, except a token the wallet still counts: the dashboard must show everything
# the limit covers, so that one has to stop counting first.
class CustomTokenRequest(BaseModel):
    """Body of POST /api/tokens/custom/lookup and POST /api/tokens/custom."""

    chain_id: int
    address: str = Field(max_length=64)


def _inspect_for_user(req: CustomTokenRequest, user_id: int) -> dict:
    """
    Runs the add-a-token checks for this user's wallet on `req.chain_id`, as 400s the UI can show.

    The checks read the token through contracts.load_ierc20, which finds the chain through the
    user's network -- so the request's chain is made the acting network for the duration, exactly
    as a chat turn does (db.acting_network). The saved network, which the Telegram bot follows, is
    left alone.
    """
    w3, chain_name = _resolve_chain(req.chain_id)
    wallet = _load_wallet_for_chain(w3, user_id, req.chain_id)
    try:
        with acting_network(user_id, chain_name, req.chain_id):
            return inspect_custom_token(user_id, req.chain_id, req.address, wallet.address)
    except CustomTokenError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/api/tokens/custom/lookup")
@limiter.limit("30/minute")
def lookup_custom_token(request: Request, req: CustomTokenRequest, user_id: int = Depends(get_current_user)):
    """
    Reads a token off the chain so the user can see what they are about to add. Saves nothing.

    @return  {"chain_id", "address", "ticker", "symbol", "name", "decimals", "balance_raw"}: the
             symbol and decimals are what make it an ERC-20 -- an address that can't answer both is
             refused with a message asking the user to check it again.
    @raises HTTPException 400 with the reason the token can't be added; 404 with no wallet here.
    """
    return {"chain_id": req.chain_id, **_inspect_for_user(req, user_id)}


@router.post("/api/tokens/custom", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
def add_custom_token(request: Request, req: CustomTokenRequest, user_id: int = Depends(get_current_user)):
    """
    Adds a token to the signed-in account's list for `chain_id`, after checking it again on chain.
    A listed token goes on the dashboard; counting it is the owner's separate transaction.

    The lookup's answer is not trusted: the checks run again here, so a request that skipped the
    preview (or raced another tab) gets the same answer.

    @return  The saved token: {"chain_id", "address", "ticker", "symbol", "name", "decimals",
             "balance_raw", "listed"}.
    @raises HTTPException 400 if the token can't be added, 409 if another request just added it.
    """
    token = _inspect_for_user(req, user_id)
    if token["listed"]:
        add_dashboard_tokens(user_id, req.chain_id, [token["ticker"]])
        return {"chain_id": req.chain_id, **token}
    try:
        save_custom_token(
            user_id, req.chain_id, token["address"], token["ticker"], token["name"], token["decimals"]
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "That token was just added. Reload to see it.")
    # No cache to drop, here or in the bot's process: the tools resolve names from the table on
    # every call (db.resolve_token), and nothing caches by ticker.
    return {"chain_id": req.chain_id, **token}


def _remove_listed_token(user_id: int, chain_id: int, token: dict):
    """Takes a listed token off the dashboard, refusing while the wallet still counts it."""
    name = token["ticker"].upper()
    # Both refusals that need no chain come first, so they cost no RPC round trip.
    if token["ticker"] == get_always_counted_ticker(chain_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{name} always counts toward your limit, like {get_native_asset_ticker(chain_id)}, "
            "so it stays on your dashboard.",
        )
    if not any(t["ticker"] == token["ticker"] for t in get_dashboard_tokens(user_id, chain_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} is not on your dashboard")
    try:
        get_wallet_address(user_id, chain_id)
        has_wallet = True
    except ValueError:
        has_wallet = False  # nothing can count it: nothing to check
    if has_wallet:
        w3, _ = _resolve_chain(chain_id)
        watched = read_spending_config(_load_wallet_for_chain(w3, user_id, chain_id))["watchedTokens"]
        if any(Web3.to_checksum_address(a) == token["address"] for a in watched):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{name} counts toward your limit. Stop counting it first, then remove it.",
            )
    remove_dashboard_token(user_id, chain_id, token["ticker"])


@router.delete("/api/tokens/custom/{chain_id}/{address}")
def remove_custom_token(chain_id: int, address: str, user_id: int = Depends(get_current_user)):
    """
    Removes a token from the signed-in account's list. The tokens themselves stay in the wallet --
    the owner can still withdraw them, and adding the token again brings them back into view.

    A listed token comes off the dashboard, but not while the wallet counts it toward the limit
    (read from the chain, not trusted to the page), and never the wrapped native token, which
    always counts. An added-by-address token has no price, so it never counts.

    @raises HTTPException 400 for the wrapped native token, 404 if the token is not on this
            account's list for `chain_id`, 409 while a listed token still counts.
    """
    try:
        address = Web3.to_checksum_address(address)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{address}' is not on your token list")
    listed = get_supported_token_by_address(chain_id, address)
    if listed is not None:
        _remove_listed_token(user_id, chain_id, listed)
        return {"status": "deleted", "chain_id": chain_id, "address": address}
    if not delete_custom_token(user_id, chain_id, address):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{address} is not on your token list")
    return {"status": "deleted", "chain_id": chain_id, "address": address}
