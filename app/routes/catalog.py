"""
What this server offers, public and RPC-free: the chains it serves and the tokens each can count.
"""
from fastapi import APIRouter, HTTPException, status

import telegram_bots
from constants import (
    get_always_counted_ticker,
    get_default_spenders,
    get_exchange,
    get_native_asset_ticker,
)
from db import (
    get_factory_address,
    get_rpc_url,
    get_supported_tokens_by_chain_id,
)
from network_config import (
    CHAIN_NAME_BY_ID,
    FORK_MODE,
    FORKABLE_CHAIN_IDS,
)

router = APIRouter()


@router.get("/api/chains")
def list_chains():
    """
    Lists the chains a user can deploy a wallet on with this server.

    A chain qualifies when this deployment serves it (CHAIN_NAME_BY_ID) AND the protocol is deployed
    there (a `factory` row). The front end builds its network picker from this rather than from a
    list of its own that would drift from the server's.

    Public and RPC-free, like /api/tokens: it reads local tables and says nothing that is not
    already public on chain, apart from a fork's local node address.

    @return  {"chains": [{"chain_id", "name", "native_ticker", "fork", "rpc_url", "exchange",
             "default_spenders", "telegram_bot"}, ...]}, by chain ID.
             `fork` is true when this server points that chain at a local fork (APP_FORK_MODE).
             `rpc_url` is that fork's local node, for the user to set in their browser wallet — each
             fork runs on its own port. None on a live chain: its RPC may carry an API key.
             `exchange` is what the assistant trades on there: "uniswap-v3", "pancakeswap-v2", or
             None where it can't trade (constants.get_exchange).
             `default_spenders` are the exchange contracts every new wallet on that chain is
             deployed trusting (see /api/deploy), [] where there are none. The Controls page keeps
             them off the removable list, and offers to trust any a wallet is missing: one deployed
             before its chain moved to Uniswap V3 trusts only the V2 router.
             `telegram_bot` is the username (no @) of that chain's Telegram bot, or None where it
             has none. Public anyway: anyone can find a bot in Telegram.
    """
    chains = []
    for chain_id, name in sorted(CHAIN_NAME_BY_ID.items()):
        try:
            get_factory_address(chain_id)
        except ValueError:
            continue
        try:
            native_ticker = get_native_asset_ticker(chain_id)
        except ValueError:
            native_ticker = None
        fork = FORK_MODE and chain_id in FORKABLE_CHAIN_IDS
        chains.append({
            "chain_id": chain_id,
            "name": name,
            "native_ticker": native_ticker,
            "fork": fork,
            "rpc_url": get_rpc_url(f"{name}-fork") if fork else None,
            "exchange": get_exchange(chain_id),
            "default_spenders": get_default_spenders(chain_id),
            "telegram_bot": telegram_bots.bot_username(chain_id),
        })
    return {"chains": chains}


@router.get("/api/tokens")
def list_tokens(chain_id: int):
    """
    Lists the tokens a wallet on `chain_id` may meter, for the deploy screen's picker.

    The user's selection comes back as DeployRequest.watched_tokens. Every ticker here is from the
    same table the deploy endpoint validates against, so anything pickable is deployable.

    Deliberately does NOT go through _resolve_chain: listing tokens needs no RPC, and a fork shares
    its parent's token table, so the fork/live distinction cannot change the answer.

    `always_counted` marks the wrapped native token (WETH, WBNB): every new wallet counts it, like
    the native asset it wraps, so the picker shows it ticked and locked (see /api/deploy).

    @return  {"chain_id", "tokens": [{"ticker", "address", "always_counted"}, ...]}.
    """
    try:
        tokens = get_supported_tokens_by_chain_id(chain_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    always = get_always_counted_ticker(chain_id)
    return {"chain_id": chain_id, "tokens": [{**t, "always_counted": t["ticker"] == always} for t in tokens]}
