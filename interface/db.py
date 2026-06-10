import json
import os
import sqlite3
import threading
from web3 import Web3
from datetime import datetime, timezone
from constants import (
    CHAIN_ID_ANVIL, CHAIN_ID_MAINNET, WEI_PER_ETH, CHAIN_ID_SEPOLIA, ENTRYPOINT_V07,

)

DB_PATH = "./interface/wallet.db"

_local = threading.local()

_NETWORK_DB_PREFIX: dict[str, str] = {
    "anvil": "anvil",
    "mainnet": "mainnet",
    "mainnet-fork": "mainnet",
    "sepolia": "sepolia",
    "sepolia-fork": "sepolia",
}


def get_db() -> sqlite3.Connection:
    """Returns a per-thread SQLite connection, opening it on first call in each thread."""
    if not hasattr(_local, "db"):
        _local.db = sqlite3.connect(DB_PATH)
        _local.db.row_factory = sqlite3.Row
    return _local.db


def get_json(path: str):
    """
    Reads and parses a JSON file from the given path.

    @param path  The file path to the JSON file to load.
    @return      The parsed JSON content as a Python dict or list.
    """
    with open(path, "r") as file:
        return json.load(file)


def init_db():
    """
    Creates all tables if they do not already exist. Safe to call on every startup.
    """
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id        INTEGER NOT NULL,
            target         TEXT NOT NULL,
            spending_limit REAL NOT NULL,
            end_time       DATE NOT NULL,
            PRIMARY KEY (chat_id, target)
        );

        CREATE TABLE IF NOT EXISTS contacts (
            chat_id INTEGER NOT NULL,
            name    TEXT NOT NULL,
            address TEXT NOT NULL,
            PRIMARY KEY (chat_id, name)
        );

        CREATE TABLE IF NOT EXISTS erc20_selectors (
            name      TEXT PRIMARY KEY,
            selector TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS uniswapv2_selectors (
            name      TEXT PRIMARY KEY,
            selector TEXT NOT NULL
        );
         CREATE TABLE IF NOT EXISTS reputation_registry_selectors (
            name      TEXT PRIMARY KEY,
            selector TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chains (
            name      TEXT NOT NULL,
            chain_id  INTEGER NOT NULL,
            PRIMARY KEY (name, chain_id)
        );

        CREATE TABLE IF NOT EXISTS rpcs (
            name    TEXT PRIMARY KEY,
            rpc_url TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS anvil_tokens (
            ticker  TEXT PRIMARY KEY,
            address TEXT NOT NULL
        );
         CREATE TABLE IF NOT EXISTS mainnet_tokens (
            ticker  TEXT PRIMARY KEY,
            address TEXT NOT NULL
        ); 
        CREATE TABLE IF NOT EXISTS sepolia_tokens (
            ticker  TEXT PRIMARY KEY,
            address TEXT NOT NULL
        );             

        CREATE TABLE IF NOT EXISTS session_handlers (
            chat_id INTEGER PRIMARY KEY,
            address TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entry_point (
            chain_id INTEGER PRIMARY KEY,
            address  TEXT NOT NULL
        );
                     
        CREATE TABLE IF NOT EXISTS user_network (
            chat_id INTEGER PRIMARY KEY,
            chain_name TEXT  NOT NULL
        );             

        CREATE TABLE IF NOT EXISTS recurring_transfers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            token        TEXT NOT NULL,
            recipient    TEXT NOT NULL,
            amount       REAL NOT NULL,
            interval_hrs INTEGER NOT NULL
        );
                     
          CREATE TABLE IF NOT EXISTS mainnet_pricefeeds (
            token TEXT PRIMARY KEY,
            address TEXT  NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sepolia_pricefeeds (
            token TEXT PRIMARY KEY,
            address TEXT  NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_keys (
            chat_id         INTEGER NOT NULL,
            target          TEXT NOT NULL,
            key_address     TEXT NOT NULL,
            key_ciphertext  TEXT NOT NULL,
            PRIMARY KEY (chat_id, target)
        );

        CREATE TABLE IF NOT EXISTS agent_registries (
            chain_id            INTEGER PRIMARY KEY,
            identity_registry   TEXT NOT NULL,
            reputation_registry TEXT NOT NULL
        );
                     
       

        CREATE TABLE IF NOT EXISTS agent_ids (
            chain_id  INTEGER PRIMARY KEY,
            agent_id  INTEGER NOT NULL
        );
                     
        

    """)
    db.commit()


def migrate_json_to_db():
    """
    Seeds reference data from JSON files into the SQLite DB.
    Uses INSERT OR REPLACE, so re-running `make db` is idempotent: existing rows
    are updated and missing rows are inserted without needing to delete the DB first.
    """
    db = get_db()

    if os.path.exists("./interface/migrate/ERC20_Selectors.json"):
        for name, selector in get_json(
            "./interface/migrate/ERC20_Selectors.json"
        ).items():
            db.execute(
                "INSERT OR REPLACE INTO erc20_selectors (name, selector) VALUES (?, ?)",
                (name, selector),
            )

    if os.path.exists("./interface/migrate/UniswapV2_Selectors.json"):
        for name, selector in get_json(
            "./interface/migrate/UniswapV2_Selectors.json"
        ).items():
            db.execute(
                "INSERT OR REPLACE INTO uniswapv2_selectors (name, selector) VALUES (?, ?)",
                (name, selector),
            )

    if os.path.exists("./interface/migrate/ReputationRegistry_Selectors.json"):
        for name, selector in get_json(
            "./interface/migrate/ReputationRegistry_Selectors.json"
        ).items():
            db.execute(
                "INSERT OR REPLACE INTO reputation_registry_selectors (name, selector) VALUES (?, ?)",
                (name, selector),
            )

    if os.path.exists("./interface/migrate/Chains.json"):
        for name, chain_id in get_json("./interface/migrate/Chains.json").items():
            db.execute(
                "INSERT OR REPLACE INTO chains (name, chain_id) VALUES (?, ?)",
                (name, chain_id),
            )

    if os.path.exists("./interface/migrate/Mainnet_Tokens.json"):
        for name, address in get_json(
            "./interface/migrate/Mainnet_Tokens.json"
        ).items():
            db.execute(
                "INSERT OR REPLACE INTO mainnet_tokens (ticker, address) VALUES (?, ?)",
                (name, Web3.to_checksum_address(address)),
            )
    if os.path.exists("./interface/migrate/Sepolia_Tokens.json"):
        for name, address in get_json(
            "./interface/migrate/Sepolia_Tokens.json"
        ).items():
            db.execute(
                "INSERT OR REPLACE INTO sepolia_tokens (ticker, address) VALUES (?, ?)",
                (name, Web3.to_checksum_address(address)),
            )

    if os.path.exists("./interface/migrate/Mainnet_Pricefeeds.json"):
        for name, address in get_json(
            "./interface/migrate/Mainnet_Pricefeeds.json"
        ).items():
            db.execute(
                "INSERT OR REPLACE INTO mainnet_pricefeeds (token, address) VALUES (?, ?)",
                (name, Web3.to_checksum_address(address)),
            )
    if os.path.exists("./interface/migrate/Sepolia_Pricefeeds.json"):
        for name, address in get_json(
            "./interface/migrate/Sepolia_Pricefeeds.json"
        ).items():
            db.execute(
                "INSERT OR REPLACE INTO sepolia_pricefeeds (token, address) VALUES (?, ?)",
                (name, Web3.to_checksum_address(address)),
            )

    if os.path.exists("./interface/migrate/RPC.json"):
        for name, rpc_url in get_json("./interface/migrate/RPC.json").items():
            db.execute(
                "INSERT OR REPLACE INTO rpcs (name, rpc_url) VALUES (?, ?)",
                (name, rpc_url),
            )


        db.execute(
            "INSERT OR REPLACE INTO entry_point (chain_id, address) VALUES (?, ?)",
            (CHAIN_ID_MAINNET, Web3.to_checksum_address(ENTRYPOINT_V07)),
        )
        db.execute(
            "INSERT OR REPLACE INTO entry_point (chain_id, address) VALUES (?, ?)",
            (CHAIN_ID_SEPOLIA, Web3.to_checksum_address(ENTRYPOINT_V07)),
        )

    db.commit()
    print("Migration complete.")


# ── Session handlers ──────────────────────────────────────────────────────────


def save_wallet_address(chat_id: int, address: str):
    """
    Saves the deployed SessionHandler address for a given chat ID.

    @param chat_id   The Telegram chat ID of the user.
    @param address   The checksummed Ethereum address of the deployed SessionHandler.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO session_handlers (chat_id, address) VALUES (?, ?)",
        (chat_id, address),
    )
    db.commit()


def get_wallet_address(chat_id: int) -> str:
    """
    Retrieves the SessionHandler address for a given chat ID.

    @param chat_id  The Telegram chat ID of the user.
    @return         The checksummed Ethereum address of the SessionHandler.
    """
    row = (
        get_db()
        .execute("SELECT address FROM session_handlers WHERE chat_id = ?", (chat_id,))
        .fetchone()
    )

    if row is None:
        raise ValueError(f"No SessionHandler address found for chat ID {chat_id}")

    return row["address"]


# ── Tokens ────────────────────────────────────────────────────────────────────


def save_anvil_token_address(ticker: str, address: str):
    """
    Saves the deployed ERC20 token address for a given ticker symbol.

    @param ticker   The token ticker symbol (e.g. "usdc").
    @param address  The checksummed Ethereum address of the deployed ERC20 token.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO anvil_tokens (ticker, address) VALUES (?, ?)",
        (ticker.lower(), address),
    )
    db.commit()
    print(f"Token address saved for {ticker}: {address}")


def get_token_address(chain_id: int, token: str) -> str:
    """
    Retrieves the token address for a given ticker symbol on the specified chain.

    @param chain_id  The numeric chain ID (e.g. 31337 for Anvil, 1 for mainnet).
    @param token     The token ticker symbol (e.g. "usdc").
    @return          The checksummed Ethereum address of the token contract.
    @raises ValueError  If the chain_id is unsupported or the ticker is not found.
    """
    table = {
        CHAIN_ID_ANVIL: "anvil_tokens",
        CHAIN_ID_MAINNET: "mainnet_tokens",
        CHAIN_ID_SEPOLIA: "sepolia_tokens",
    }.get(chain_id)
    if table is None:
        raise ValueError(f"Unsupported chain_id: {chain_id}")
    row = (
        get_db()
        .execute(f"SELECT address FROM {table} WHERE ticker = ?", (token.lower(),))
        .fetchone()
    )
    if row is None:
        raise ValueError(
            f"No token address found for ticker '{token}' on chain {chain_id}"
        )
    return row["address"]


# ── Entry point ───────────────────────────────────────────────────────────────


def save_entry_point_address(chain_id: int, address: str):
    """
    Saves the deployed EntryPoint contract address for a given chain.

    @param chain_id  The numeric chain ID to associate this EntryPoint with
                     (e.g. 31337 for Anvil, 1 for mainnet).
    @param address   The checksummed Ethereum address of the deployed EntryPoint.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO entry_point (chain_id, address) VALUES (?, ?)",
        (chain_id, address),
    )
    db.commit()


def get_entry_point_address(chain_id: int) -> str:
    """
    Retrieves the deployed EntryPoint contract address for a given chain.

    @param chain_id  The numeric chain ID to look up (e.g. 31337 for Anvil, 1 for mainnet).
    @return          The checksummed Ethereum address of the EntryPoint.
    @raises ValueError  If no EntryPoint address has been saved for the given chain ID.
    """
    row = (
        get_db()
        .execute("SELECT address FROM entry_point WHERE chain_id = ?", (chain_id,))
        .fetchone()
    )

    if row is None:
        raise ValueError("No EntryPoint address found in database")

    return Web3.to_checksum_address(row["address"])


# ── ERC-8004 Agent ID ────────────────────────────────────────────────────



def save_agent_id(chain_id: int, agent_id: int):
    """Persists the agent's ERC-8004 tokenId (agentId) for a given chain."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO agent_ids (chain_id, agent_id) VALUES (?, ?)",
        (chain_id, agent_id),
    )
    db.commit()


def get_agent_id(chain_id: int) -> int:
    """
    Returns the agent's ERC-8004 tokenId for the given chain.

    @raises ValueError  If register_agent.py has not been run for this chain yet.
    """
    row = (
        get_db()
        .execute("SELECT agent_id FROM agent_ids WHERE chain_id = ?", (chain_id,))
        .fetchone()
    )
    if row is None:
        raise ValueError(f"No agent_id found for chain_id {chain_id}. Run register_agent.py first.")
    return int(row["agent_id"])


def try_get_agent_id(chain_id: int) -> int | None:
    """Returns the agent's ERC-8004 tokenId, or None if not yet saved."""
    row = (
        get_db()
        .execute("SELECT agent_id FROM agent_ids WHERE chain_id = ?", (chain_id,))
        .fetchone()
    )
    return int(row["agent_id"]) if row else None


# ── ERC-8004 Agent Registries ─────────────────────────────────────────────────


def save_agent_registry_addresses(chain_id: int, identity: str, reputation: str):
    """Persists the Identity and Reputation registry addresses for a chain."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO agent_registries (chain_id, identity_registry, reputation_registry) VALUES (?, ?, ?)",
        (chain_id, identity, reputation),
    )
    db.commit()


def get_identity_registry_address(chain_id: int) -> str:
    """
    Returns the ERC-8004 Identity Registry address for the given chain.
   
    """
    
    row = (
        get_db()
        .execute("SELECT identity_registry FROM agent_registries WHERE chain_id = ?", (chain_id,))
        .fetchone()
    )
    if row is None:
        raise ValueError(f"No Identity Registry address found for chain_id {chain_id}. Run deployment first.")
    return Web3.to_checksum_address(row["identity_registry"])


def get_reputation_registry_address(chain_id: int) -> str:
    """
    Returns the ERC-8004 Reputation Registry address for the given chain.
   
    """
  
    row = (
        get_db()
        .execute("SELECT reputation_registry FROM agent_registries WHERE chain_id = ?", (chain_id,))
        .fetchone()
    )
    if row is None:
        raise ValueError(f"No Reputation Registry address found for chain_id {chain_id}. Run deployment first.")
    return Web3.to_checksum_address(row["reputation_registry"])


# ── Sessions ──────────────────────────────────────────────────────────────────


def save_session(chat_id: int, target: str, spending_limit: int, end_time: int):
    """
    Saves a session entry to the sessions table.

    @param chat_id        The Telegram chat ID of the user.
    @param target         The token ticker symbol the session is scoped to (e.g. "usdc").
    @param spending_limit The spending limit in wei; converted to whole units before saving.
    @param end_time       The session expiry as a Unix timestamp; converted to ISO 8601 date.
    """
    end_date = datetime.fromtimestamp(end_time, tz=timezone.utc).date().isoformat()
    limit = spending_limit / WEI_PER_ETH
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO sessions (chat_id, target, spending_limit, end_time) VALUES (?, ?, ?, ?)",
        (chat_id, target, limit, end_date),
    )
    db.commit()
    print(f"Session saved\nTarget: {target}\nSpending Limit: {limit}")


def get_session(chat_id: int, target: str) -> tuple[float, str]:
    """
    Retrieves the stored session metadata for a given user and token.

    @param chat_id  The Telegram chat ID of the user.
    @param target   The token ticker symbol the session is scoped to (e.g. "usdc").
    @return         A tuple of (spending_limit, end_time) where spending_limit is in
                    whole USD units (e.g. 1000.0) and end_time is an ISO 8601 date string.
    """
    row = (
        get_db()
        .execute(
            "SELECT spending_limit, end_time FROM sessions WHERE chat_id = ? AND target = ?",
            (chat_id, target),
        )
        .fetchone()
    )

    if row is None:
        raise ValueError(
            f"No session found for chat_id '{chat_id}' and target '{target}'"
        )

    return row["spending_limit"], row["end_time"]


def get_all_sessions(chat_id: int) -> list[dict]:
    """
    Retrieves the stored metadata for all sessions for a given user.

    @param chat_id  The Telegram chat ID of the user.
    @return         A list of dicts with 'target', 'spending_limit', and 'end_time' keys,
                    where spending_limit is in whole units (e.g. 1000.0) and end_time is
                    an ISO 8601 date string.
    """
    rows = (
        get_db()
        .execute(
            "SELECT target, spending_limit, end_time FROM sessions WHERE chat_id = ?",
            (chat_id,),
        )
        .fetchall()
    )

    return [
        {
            "target": r["target"],
            "spending_limit": r["spending_limit"],
            "end_time": r["end_time"],
        }
        for r in rows
    ]


def delete_session(chat_id: int, target: str):
    """
    Deletes a session entry from the sessions table.

    @param chat_id  The Telegram chat ID of the user.
    @param target   The token ticker symbol the session is scoped to (e.g. "usdc").
    @return         A confirmation string.
    """
    db = get_db()
    db.execute(
        "DELETE FROM sessions WHERE chat_id = ? AND target = ?",
        (chat_id, target),
    )
    db.commit()


# ── Session keys ─────────────────────────────────────────────────────────────


def get_session_key(chat_id: int, target: str) -> tuple[str, str] | None:
    """
    Retrieves the stored session key address and Vault ciphertext for a given user and target.

    @param chat_id  The Telegram chat ID of the user.
    @param target   The contract address the session key is scoped to.
    @return         A tuple of (key_address, key_ciphertext), or None if not found.
    """
    row = (
        get_db()
        .execute(
            "SELECT key_address, key_ciphertext FROM session_keys WHERE chat_id = ? AND target = ?",
            (chat_id, target),
        )
        .fetchone()
    )
    return (row["key_address"], row["key_ciphertext"]) if row else None


def save_session_key(chat_id: int, target: str, key_address: str, key_ciphertext: str):
    """
    Stores an encrypted session key for a given user and target.

    @param chat_id         The Telegram chat ID of the user.
    @param target          The contract address the session key is scoped to.
    @param key_address     The Ethereum address derived from the session key.
    @param key_ciphertext  The Vault Transit ciphertext blob ('vault:v1:...').
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO session_keys (chat_id, target, key_address, key_ciphertext) VALUES (?, ?, ?, ?)",
        (chat_id, target, key_address, key_ciphertext),
    )
    db.commit()


# ── Chains & RPCs ─────────────────────────────────────────────────────────────


def get_rpc_url(chain_name: str) -> str | None:
    """
    Retrieves the RPC URL for a given chain name.

    @param chain_name  The network name (e.g. "anvil", "mainnet").
    @return            The RPC URL string, or None if not found.
    """
    row = (
        get_db()
        .execute("SELECT rpc_url FROM rpcs WHERE name = ?", (chain_name.lower(),))
        .fetchone()
    )
    return row["rpc_url"] if row else None


def get_chain_id_from_name(chain_name: str) -> int | None:
    """
    Retrieves the chain ID for a given chain name.

    @param chain_name  The network name (e.g. "anvil", "mainnet").
    @return            The chain ID as an integer, or None if not found.
    """
    row = (
        get_db()
        .execute("SELECT chain_id FROM chains WHERE name = ?", (chain_name.lower(),))
        .fetchone()
    )
    return row["chain_id"] if row else None


def get_chain_name_from_id(chain_id: int) -> str | None:
    """
    Retrieves the chain name for a given chain ID.

    @param chain_id  The numeric chain ID (e.g. 31337 for Anvil).
    @return          The chain name string, or None if not found.
    """
    row = (
        get_db()
        .execute("SELECT name FROM chains WHERE chain_id = ?", (chain_id,))
        .fetchone()
    )
    return row["name"] if row else None


# ──  Selectors ───────────────────────────────────────────────────────────


def get_erc20_selectors() -> list[dict]:
    """
    Returns all rows from the erc20_selectors table.

    @return  A list of dicts with 'name' and 'selector' keys
             (e.g. [{"name": "transfer", "selector": "0xa9059cbb"}, ...]).
    """
    rows = (
        get_db()
        .execute("SELECT name, selector FROM erc20_selectors ORDER BY name ASC")
        .fetchall()
    )
    return [{"name": row["name"], "selector": row["selector"]} for row in rows]


def get_uniswapv2_selectors() -> list[dict]:
    """
    Returns all rows from the uniswapv2_selectors table.

    @return  A list of dicts with 'name' and 'selector' keys
             (e.g. [{"name": "transfer", "selector": "0xa9059cbb"}, ...]).
    """
    rows = (
        get_db()
        .execute("SELECT name, selector FROM uniswapv2_selectors ORDER BY name ASC")
        .fetchall()
    )
    return [{"name": row["name"], "selector": row["selector"]} for row in rows]



def get_reputation_registry_selectors() -> list[dict]:
    """
    Returns all rows from the reputation_registry_selectors table.

    @return  A list of dicts with 'name' and 'selector' keys
             (e.g. [{"name": "updateReputation", "selector": "0x12345678"}, ...]).
    """
    rows = (
        get_db()
        .execute("SELECT name, selector FROM reputation_registry_selectors ORDER BY name ASC")
        .fetchall()
    )
    return [{"name": row["name"], "selector": row["selector"]} for row in rows]


# ── Tokens (supported list) ───────────────────────────────────────────────────


def get_supported_tokens(chat_id: int) -> list[str]:
    """
    Returns all supported token tickers for the network the user is connected to,
    sorted alphabetically. Resolves the correct table (anvil_tokens or mainnet_tokens)
    by looking up the user's saved network via get_user_network().

    @param chat_id  The Telegram chat ID of the user. Used to determine which network
                    the user is on and therefore which token table to query.
    @return         A list of ticker strings (e.g. ["dai", "usdc"]).
    @raises ValueError  If the user's network is not set or is not "anvil" or "mainnet".
    """
    network = get_user_network(chat_id)
    prefix = _NETWORK_DB_PREFIX.get(network)
    if prefix is None:
        raise ValueError(f"Unsupported network: '{network}'")
    table = f"{prefix}_tokens"
    rows = (
        get_db().execute(f"SELECT ticker FROM {table} ORDER BY ticker ASC").fetchall()
    )
    return [row["ticker"] for row in rows]



def get_pricefeed_address(chat_id: int, token: str) -> str:
    """
    Retrieves the price feed address for a specific token on the user's network.

    @return  A single Chainlink feed address (e.g. "0xD10a...").
    @raises ValueError  If the network is unsupported or the token has no feed.
    """
    network = get_user_network(chat_id)
    prefix = _NETWORK_DB_PREFIX.get(network)
    if prefix is None:
        raise ValueError(f"Unsupported network: '{network}'")
    table = f"{prefix}_pricefeeds"
    row = get_db().execute(
        f"SELECT address FROM {table} WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No price feed for token '{token}' on network '{network}'")
    return row["address"]


# ── Contacts ──────────────────────────────────────────────────────────────────


def save_contact(chat_id: int, name: str, address: str):
    """
    Saves or updates a contact, associating a name with an Ethereum address.

    @param chat_id   The Telegram chat ID of the user.
    @param name      Human-readable label for the contact. Stored in lowercase.
    @param address   The Ethereum address to associate with the name.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO contacts (chat_id, name, address) VALUES (?, ?, ?)",
        (chat_id, name.lower(), address),
    )
    db.commit()
    print(f"Contact saved: {name}")


def get_contact(chat_id: int, name: str) -> str | None:
    """
    Looks up the Ethereum address of a saved contact by name.

    @param chat_id  The Telegram chat ID of the user.
    @param name     The contact name to look up. Case-insensitive.
    @return         The Ethereum address, or None if not found.
    """
    row = (
        get_db()
        .execute(
            "SELECT address FROM contacts WHERE chat_id = ? AND name = ?",
            (chat_id, name.lower()),
        )
        .fetchone()
    )

    if row is None:
        print("Contact doesn't exist")
        return None

    return row["address"]


def get_all_contacts(chat_id: int) -> list[dict]:
    """
    Returns all saved contacts for a user, sorted alphabetically by name.

    @param chat_id  The Telegram chat ID of the user.
    @return         A list of dicts with 'name' and 'address' keys.
    """
    rows = (
        get_db()
        .execute(
            "SELECT name, address FROM contacts WHERE chat_id = ? ORDER BY name ASC",
            (chat_id,),
        )
        .fetchall()
    )
    return [{"name": row["name"], "address": row["address"]} for row in rows]


def delete_contact(chat_id: int, name: str) -> str:
    """
    Deletes a saved contact by name. Does nothing if the contact does not exist.

    @param chat_id  The Telegram chat ID of the user.
    @param name     The contact name to delete. Case-insensitive.
    @return         A confirmation string.
    """
    db = get_db()
    db.execute(
        "DELETE FROM contacts WHERE chat_id = ? AND name = ?",
        (chat_id, name.lower()),
    )
    db.commit()
    return f"Contact deleted: {name}"


# ── Recurring transfers ───────────────────────────────────────────────────────


def save_recurring_transfer(
    chat_id: int, token: str, recipient: str, amount: float, interval_hrs: int
) -> int:
    """
    Saves a new recurring transfer to the database.

    @param chat_id       The Telegram chat ID of the user.
    @param token         The token ticker symbol (e.g. "usdc").
    @param recipient     The contact name to send tokens to.
    @param amount        The amount in whole token units (e.g. 100.0 for 100 USDC).
    @param interval_hrs  How often to repeat, in hours (e.g. 24 for daily).
    @return              The auto-assigned transfer ID.
    """
    db = get_db()
    cursor = db.execute(
        "INSERT INTO recurring_transfers (chat_id, token, recipient, amount, interval_hrs) VALUES (?, ?, ?, ?, ?)",
        (chat_id, token.lower(), recipient.lower(), amount, interval_hrs),
    )
    db.commit()
    return cursor.lastrowid


def get_recurring_transfers(chat_id: int) -> list[dict]:
    """
    Returns all recurring transfers for a given user.

    @param chat_id  The Telegram chat ID of the user.
    @return         A list of dicts with 'id', 'token', 'recipient', 'amount', and 'interval_hrs' keys.
    """
    rows = (
        get_db()
        .execute(
            "SELECT id, token, recipient, amount, interval_hrs FROM recurring_transfers WHERE chat_id = ?",
            (chat_id,),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def get_all_recurring_transfers() -> list[dict]:
    """
    Returns all recurring transfers across all users. Used on bot startup to restore jobs.

    @return  A list of dicts with 'id', 'chat_id', 'token', 'recipient', 'amount', and 'interval_hrs' keys.
    """
    rows = (
        get_db()
        .execute(
            "SELECT id, chat_id, token, recipient, amount, interval_hrs FROM recurring_transfers"
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def delete_recurring_transfer(transfer_id: int, chat_id: int):
    """
    Deletes a recurring transfer by ID, scoped to the owning chat_id.

    @param transfer_id  The ID of the recurring transfer to delete.
    @param chat_id      The Telegram chat ID of the transfer owner.
    """
    db = get_db()
    db.execute(
        "DELETE FROM recurring_transfers WHERE id = ? AND chat_id = ?",
        (transfer_id, chat_id),
    )
    db.commit()


def save_user_network(chat_id: int, chain_name: str):

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO user_network (chat_id, chain_name) VALUES (?, ?)",
        (chat_id, chain_name),
    )
    db.commit()
    print(f"User network saved: {chain_name}")


def get_user_network(chat_id: int):

    row = (
        get_db()
        .execute(
            "SELECT chain_name FROM user_network WHERE chat_id = ?",
            (chat_id,),
        )
        .fetchone()
    )

    return row["chain_name"] if row else None


if __name__ == "__main__":
    init_db()
    migrate_json_to_db()
