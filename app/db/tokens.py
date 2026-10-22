"""
Tokens: the ones Mitfah lists, the ones a user adds by address, the dashboard's list and the LP tokens
of the pools the assistant deposited into.
"""
import sqlite3
import time

from web3 import Web3

from constants import (
    CHAIN_ID_ANVIL,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BASE,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
)
from db import get_db
from db.chains import get_chain_id_from_name, get_user_network

CHAIN_IDs=[
    CHAIN_ID_ANVIL, CHAIN_ID_MAINNET, CHAIN_ID_SEPOLIA, CHAIN_ID_BSC, CHAIN_ID_CELO,
    CHAIN_ID_ARBITRUM, CHAIN_ID_BASE,
]


def _require_token_chain(chain_id: int):
    """Raises ValueError for a chain Mitfah keeps no token list for."""
    if chain_id not in CHAIN_IDs:
        raise ValueError(f"Unsupported chain_id: {chain_id}")


def get_token_address(chain_id: int, token: str) -> str:
    """
    Retrieves the token address for a given ticker symbol on the specified chain.

    @param chain_id  The numeric chain ID (e.g. 31337 for Anvil, 1 for mainnet).
    @param token     The token ticker symbol (e.g. "usdc").
    @return          The checksummed Ethereum address of the token contract.
    @raises ValueError  If the chain_id is unsupported or the ticker is not found.
    """
    _require_token_chain(chain_id)
    row = (
        get_db()
        .execute(
            "SELECT address FROM supported_tokens WHERE chain_id = ? AND ticker = ?",
            (chain_id, token.lower()),
        )
        .fetchone()
    )
    if row is None:
        raise ValueError(
            f"No token address found for ticker '{token}' on chain {chain_id}"
        )
    return row["address"]


def get_supported_tokens(user_id: int) -> list[str]:
    """
    Returns all supported token tickers for the network the user is connected to, sorted
    alphabetically. The chain comes from the user's network (get_user_network), by name.

    @param user_id  The application user ID. Used to determine which network
                    the user is on and therefore which chain's tokens to list.
    @return         A list of ticker strings (e.g. ["dai", "usdc"]).
    @raises ValueError  If the user's network is not set or has no token list.
    """
    network = get_user_network(user_id)
    chain_id = get_chain_id_from_name(network) if network else None
    if chain_id not in CHAIN_IDs:
        raise ValueError(f"Unsupported network: '{network}'")
    return [t["ticker"] for t in get_supported_tokens_by_chain_id(chain_id)]


def get_supported_tokens_by_chain_id(chain_id: int) -> list[dict]:
    """
    Returns every supported token on `chain_id` as {"ticker", "address"} dicts, sorted by ticker.

    The by-chain_id twin of get_supported_tokens(), which resolves the chain through the user's
    SAVED network. The API needs the list for a chain the user has not switched to yet — they pick
    watched tokens for the wallet they are about to deploy, before any network is saved for them —
    so the chain is passed explicitly here rather than read back out of user_network.

    Keyed by chain ID, not name, which also sidesteps the fork ambiguity: `sepolia` and
    `sepolia-fork` share chain 11155111 and therefore its tokens, so the caller does not need to
    have picked between them to list tokens.

    Returns the address alongside the ticker because the front end shows the ticker but the deploy
    endpoint validates what comes back; sending both lets it display one and echo the other.

    @param chain_id  The numeric chain ID to list tokens for.
    @return          A list of {"ticker": str, "address": str} dicts, empty if the chain is unseeded.
    @raises ValueError If Mitfah keeps no token list for chain_id.
    """
    _require_token_chain(chain_id)
    rows = (
        get_db()
        .execute(
            "SELECT ticker, address FROM supported_tokens WHERE chain_id = ? ORDER BY ticker ASC",
            (chain_id,),
        )
        .fetchall()
    )
    return [{"ticker": row["ticker"], "address": row["address"]} for row in rows]


def get_supported_token_by_address(chain_id: int, address: str) -> dict | None:
    """
    The supported token at `address` on `chain_id`, or None if Mitfah doesn't list it there.

    @param address  Any case; compared checksummed, which is how supported_tokens stores it.
    @return         {"ticker", "address"}.
    """
    row = (
        get_db()
        .execute(
            "SELECT ticker, address FROM supported_tokens WHERE chain_id = ? AND address = ?",
            (chain_id, Web3.to_checksum_address(address)),
        )
        .fetchone()
    )
    return {"ticker": row["ticker"], "address": row["address"]} if row else None


def save_custom_token(user_id: int, chain_id: int, address: str, ticker: str, name: str | None, decimals: int):
    """
    Adds a token to the user's own list for `chain_id`.

    Stores what it is given: every check (a real ERC-20, a safe symbol, no clash with a listed
    ticker, the per-chain cap) lives in custom_tokens.inspect_custom_token, which the API runs first.

    @param address   The token contract, checksummed.
    @param ticker    The token's symbol, lowercased.
    @raises sqlite3.IntegrityError  If this user already has that address or ticker on the chain.
    """
    db = get_db()
    try:
        db.execute(
            "INSERT INTO custom_tokens (user_id, chain_id, address, ticker, name, decimals, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, chain_id, address, ticker.lower(), name, decimals, int(time.time())),
        )
    except sqlite3.IntegrityError:
        # The failed INSERT leaves this thread's write transaction open, holding the database's one
        # write lock: every other thread's write would wait and fail "database is locked".
        db.rollback()
        raise
    db.commit()


def delete_custom_token(user_id: int, chain_id: int, address: str) -> bool:
    """
    Removes a token from the user's list. The tokens themselves stay in the wallet.

    @param address  The token contract, checksummed.
    @return         True if a row was removed, False if there was none.
    """
    db = get_db()
    cur = db.execute(
        "DELETE FROM custom_tokens WHERE user_id = ? AND chain_id = ? AND address = ?",
        (user_id, chain_id, address),
    )
    db.commit()
    return cur.rowcount > 0


def get_custom_tokens(user_id: int, chain_id: int) -> list[dict]:
    """
    The user's own tokens on `chain_id`, sorted by ticker.

    @return  [{"ticker", "address", "name", "decimals"}, ...], empty if none were added.
    """
    rows = (
        get_db()
        .execute(
            "SELECT ticker, address, name, decimals FROM custom_tokens "
            "WHERE user_id = ? AND chain_id = ? ORDER BY ticker ASC",
            (user_id, chain_id),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def get_custom_token(user_id: int, chain_id: int, ref: str) -> dict | None:
    """
    One of the user's own tokens on `chain_id`, looked up by ticker or by address.

    @param ref  A ticker (case-insensitive) or a 0x address (any case).
    @return     {"ticker", "address", "name", "decimals"}, or None if the user never added it.
    """
    if _looks_like_address(ref):
        column, value = "address", Web3.to_checksum_address(ref)
    else:
        column, value = "ticker", ref.lower()
    row = (
        get_db()
        .execute(
            f"SELECT ticker, address, name, decimals FROM custom_tokens "
            f"WHERE user_id = ? AND chain_id = ? AND {column} = ?",
            (user_id, chain_id, value),
        )
        .fetchone()
    )
    return dict(row) if row else None


def _looks_like_address(ref: str) -> bool:
    return ref.startswith(("0x", "0X")) and len(ref) == 42


def resolve_token(user_id: int, chain_id: int, ref: str) -> str:
    """
    A token reference -> its checksummed address, for this user on `chain_id`.

    The one lookup every agent tool, the withdraw endpoint and the balance read share, so a token
    the user added in the web app works everywhere a listed one does. Read from the database on
    every call rather than from a snapshot: the Telegram bot is a separate process, and a cached
    map there would miss a token added (or removed and re-added under the same name) on the web.

    Listed tokens win: a custom token can never take a listed ticker (custom_tokens.py refuses it),
    so the order only matters for a database edited by hand.

    @param ref  A listed ticker, a ticker the user added, or a raw 0x address (passed through
                checksummed -- LP/pair tokens and the like).
    @raises ValueError  If the chain is unsupported, or the ticker is neither listed nor added.
    """
    if _looks_like_address(ref):
        return Web3.to_checksum_address(ref)
    try:
        return get_token_address(chain_id, ref)
    except ValueError:
        custom = get_custom_token(user_id, chain_id, ref)
        if custom is None:
            raise ValueError(
                f"No token '{ref}' on chain {chain_id}: it is neither a token Mitfah lists nor one "
                "this user added."
            )
        return custom["address"]


def add_dashboard_tokens(user_id: int, chain_id: int, tickers: list[str]):
    """
    Puts listed tokens on the user's dashboard for `chain_id`. Already there is fine, not an error.

    @param tickers  Tickers from supported_tokens, any case.
    """
    now = int(time.time())
    db = get_db()
    db.executemany(
        "INSERT OR IGNORE INTO dashboard_tokens (user_id, chain_id, ticker, added_at) VALUES (?, ?, ?, ?)",
        [(user_id, chain_id, ticker.lower(), now) for ticker in tickers],
    )
    db.commit()


def remove_dashboard_token(user_id: int, chain_id: int, ticker: str) -> bool:
    """
    Takes a listed token off the user's dashboard. The tokens themselves stay in the wallet.

    Whether it may go (not while it counts toward the limit) is the caller's check: it needs the chain.

    @return  True if it was on the dashboard.
    """
    db = get_db()
    cur = db.execute(
        "DELETE FROM dashboard_tokens WHERE user_id = ? AND chain_id = ? AND ticker = ?",
        (user_id, chain_id, ticker.lower()),
    )
    db.commit()
    return cur.rowcount > 0


def get_dashboard_tokens(user_id: int, chain_id: int) -> list[dict]:
    """
    The listed tokens on the user's dashboard for `chain_id`, as {"ticker", "address"}, by ticker.

    Joined on supported_tokens, so a token Mitfah no longer lists is left out.
    """
    rows = (
        get_db()
        .execute(
            "SELECT s.ticker, s.address FROM dashboard_tokens d "
            "JOIN supported_tokens s ON s.chain_id = d.chain_id AND s.ticker = d.ticker "
            "WHERE d.user_id = ? AND d.chain_id = ? ORDER BY s.ticker ASC",
            (user_id, chain_id),
        )
        .fetchall()
    )
    return [{"ticker": row["ticker"], "address": row["address"]} for row in rows]


def save_lp_token(user_id: int, chain_id: int, token_a: str, ticker_a: str, token_b: str, ticker_b: str):
    """
    Remembers a pool the assistant is depositing into, so the dashboard shows the LP tokens that
    come back. A pool already remembered is left as it is, with what was read about it.

    @param token_a, token_b    The pool's two tokens, checksummed, in either order.
    @param ticker_a, ticker_b  How to name each on the dashboard, any case.
    """
    (token0, ticker0), (token1, ticker1) = sorted(
        ((token_a, ticker_a), (token_b, ticker_b)), key=lambda token: int(token[0], 16)
    )
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO lp_tokens (user_id, chain_id, token0, token1, ticker0, ticker1, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, chain_id, token0, token1, ticker0.lower(), ticker1.lower(), int(time.time())),
    )
    db.commit()


def set_lp_token_pair(user_id: int, chain_id: int, token0: str, token1: str, pair: str, decimals0: int, decimals1: int):
    """Saves what was read off the chain about a remembered pool: its address and its tokens' decimals."""
    db = get_db()
    db.execute(
        "UPDATE lp_tokens SET pair = ?, decimals0 = ?, decimals1 = ? "
        "WHERE user_id = ? AND chain_id = ? AND token0 = ? AND token1 = ?",
        (pair, decimals0, decimals1, user_id, chain_id, token0, token1),
    )
    db.commit()


def get_lp_tokens(user_id: int, chain_id: int) -> list[dict]:
    """
    The pools remembered for the user on `chain_id`, oldest first.

    @return  [{"token0", "token1", "ticker0", "ticker1", "pair", "decimals0", "decimals1"}, ...];
             `pair` and the decimals are None until the dashboard has read them.
    """
    rows = (
        get_db()
        .execute(
            "SELECT token0, token1, ticker0, ticker1, pair, decimals0, decimals1 FROM lp_tokens "
            "WHERE user_id = ? AND chain_id = ? ORDER BY added_at ASC, token0 ASC",
            (user_id, chain_id),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]
