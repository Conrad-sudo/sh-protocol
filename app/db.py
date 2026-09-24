import json
import os
import sqlite3
import threading
import time
from web3 import Web3
from constants import (
    CHAIN_ID_ANVIL, CHAIN_ID_ARBITRUM, CHAIN_ID_BSC, CHAIN_ID_CELO, CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
)
from seed_data import SEEDS

CHAIN_IDs=[
    CHAIN_ID_ANVIL, CHAIN_ID_MAINNET, CHAIN_ID_SEPOLIA, CHAIN_ID_BSC, CHAIN_ID_CELO,
    CHAIN_ID_ARBITRUM,
]

DB_PATH = "./app/wallet.db"

_local = threading.local()

_NETWORK_DB_PREFIX: dict[str, str] = {
    "anvil": "anvil",
    "mainnet": "mainnet",
    "mainnet-fork": "mainnet",
    "sepolia": "sepolia",
    "sepolia-fork": "sepolia",
    "bsc": "bsc",
    "bsc-fork": "bsc",
    "celo": "celo",
    "celo-fork": "celo",
    "arbitrum": "arbitrum",
    "arbitrum-fork": "arbitrum",
}

# The token table for a chain, keyed by chain ID. A fork shares its parent chain's table because it
# shares its state and therefore its token addresses, which is why _NETWORK_DB_PREFIX above collapses
# each `-fork` name onto the same prefix — this map is that same fact expressed without the name.
_TOKEN_TABLE_BY_CHAIN_ID: dict[int, str] = {
    CHAIN_ID_ANVIL: "anvil_tokens",
    CHAIN_ID_MAINNET: "mainnet_tokens",
    CHAIN_ID_SEPOLIA: "sepolia_tokens",
    CHAIN_ID_BSC: "bsc_tokens",
    CHAIN_ID_CELO: "celo_tokens",
    CHAIN_ID_ARBITRUM: "arbitrum_tokens",
}


def get_db() -> sqlite3.Connection:
    """
    Returns a per-thread SQLite connection, opening it on first call in each thread.

    WAL and busy_timeout are set per connection (WAL is a persistent property of the FILE, but
    setting it is harmless and idempotent; busy_timeout is per connection and must be set every
    time). Both matter now that FastAPI serves requests from a threadpool while the LangGraph
    checkpointer writes the same file: the default journal mode lets one writer block every
    reader, and the default busy_timeout of 0 turns any overlap straight into
    "database is locked" instead of a short wait.
    """
    if not hasattr(_local, "db"):
        _local.db = sqlite3.connect(DB_PATH)
        _local.db.row_factory = sqlite3.Row
        _local.db.execute("PRAGMA journal_mode=WAL")
        _local.db.execute("PRAGMA busy_timeout=5000")
    return _local.db


def get_json(path: str):
    """
    Reads and parses a JSON file from the given path.

    @param path  The file path to the JSON file to load.
    @return      The parsed JSON content as a Python dict or list.
    """
    with open(path, "r") as file:
        return json.load(file)


def _migrate_add_chain_id(db: sqlite3.Connection):
    """
    Adds chain_id to session_handlers and session_keys on a database created before wallets were
    per-chain, preserving existing rows.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so without this an older wallet.db
    would keep its single-wallet-per-user shape and every write would fail on the missing column.

    The chain for an existing row is recovered from user_network, which is where that user's only
    wallet must have been (there was nowhere else to put one). A row whose user has no
    user_network entry cannot be placed on any chain and is dropped -- it was already unusable,
    since load_network_config raises for such a user before any wallet lookup happens.
    """
    for table, extra_cols, key_cols in (
        ("session_handlers", "address TEXT NOT NULL", "chat_id, chain_id"),
        (
            "session_keys",
            "target TEXT NOT NULL, key_address TEXT NOT NULL, key_ciphertext TEXT NOT NULL",
            "chat_id, chain_id, target",
        ),
    ):
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        if not cols or "chain_id" in cols:
            continue  # fresh install (init_db creates it correctly) or already migrated

        carried = [c for c in cols if c != "chain_id"]
        select_cols = ", ".join(f"old.{c}" for c in carried)
        db.executescript(f"""
            ALTER TABLE {table} RENAME TO {table}_old;
            CREATE TABLE {table} (
                chat_id INTEGER NOT NULL,
                chain_id INTEGER NOT NULL,
                {extra_cols},
                PRIMARY KEY ({key_cols})
            );
            INSERT OR REPLACE INTO {table} (chat_id, chain_id, {", ".join(carried)})
            SELECT old.chat_id, c.chain_id, {select_cols}
            FROM {table}_old old
            JOIN user_network un ON un.chat_id = old.chat_id
            JOIN chains c ON c.name = un.chain_name;
            DROP TABLE {table}_old;
        """)
        print(f"Migrated {table} to be per-chain (chat_id, chain_id).")
    db.commit()


_IDENTITY_TABLES = (
    # table, the columns that follow user_id, the primary key
    ("session_handlers", "chain_id INTEGER NOT NULL, address TEXT NOT NULL", "user_id, chain_id"),
    ("user_network", "chain_name TEXT NOT NULL", "user_id"),
    ("contacts", "name TEXT NOT NULL, address TEXT NOT NULL", "user_id, name"),
    (
        "session_keys",
        "chain_id INTEGER NOT NULL, target TEXT NOT NULL, key_address TEXT NOT NULL, "
        "key_ciphertext TEXT NOT NULL",
        "user_id, chain_id, target",
    ),
)


def _migrate_chat_id_to_user_id(db: sqlite3.Connection):
    """
    Replaces the `chat_id` key with `user_id` across every per-user table, minting a `users` row
    for each distinct legacy chat ID.

    Identity used to BE the Telegram chat ID. It is now an application account that a Telegram
    chat may be bound to, because the same person reaches this system from the web app too. So
    each legacy chat_id becomes a `users` row whose id is freshly allocated from 1, with the old
    value preserved in `telegram_chat_id` -- that is what keeps the existing Telegram user linked
    to their own wallet after the switch.

    Fresh ids (rather than carrying the chat IDs over as user IDs) are what let the id space stay
    clean: with Telegram IDs confined to their own column, a generated user id can never collide
    with one, so no high-offset autoincrement is needed.

    Also rewrites the LangGraph checkpoint tables, whose thread_id was str(chat_id). It becomes
    "{user_id}:{chain_id}" -- see chat() for why the chain belongs in the key. The chain is
    recovered from user_network, the same rule _migrate_add_chain_id already uses; a user with no
    user_network row has their checkpoints dropped, consistent with that migration, since such a
    user cannot reach the agent at all (load_network_config raises first).

    MUST run after _migrate_add_chain_id, which still expects the chat_id columns.
    """
    cols = [r[1] for r in db.execute("PRAGMA table_info(session_handlers)").fetchall()]
    if not cols or "user_id" in cols:
        return  # fresh install (init_db creates it correctly) or already migrated

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            email            TEXT UNIQUE,
            password_hash    TEXT,
            google_sub       TEXT UNIQUE,
            owner_addr       TEXT UNIQUE,
            telegram_chat_id INTEGER UNIQUE,
            created_at       INTEGER NOT NULL
        )
    """)

    # Every chat_id that appears anywhere, so a user known only from contacts still gets an account.
    legacy = [
        row[0]
        for row in db.execute("""
            SELECT chat_id FROM session_handlers
            UNION SELECT chat_id FROM session_keys
            UNION SELECT chat_id FROM user_network
            UNION SELECT chat_id FROM contacts
            ORDER BY chat_id
        """).fetchall()
    ]
    now = int(time.time())
    for chat_id in legacy:
        db.execute(
            "INSERT OR IGNORE INTO users (telegram_chat_id, created_at) VALUES (?, ?)",
            (chat_id, now),
        )

    for table, extra_cols, key_cols in _IDENTITY_TABLES:
        existing = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        carried = [c for c in existing if c != "chat_id"]
        db.executescript(f"""
            ALTER TABLE {table} RENAME TO {table}_old;
            CREATE TABLE {table} (
                user_id INTEGER NOT NULL,
                {extra_cols},
                PRIMARY KEY ({key_cols})
            );
            INSERT OR REPLACE INTO {table} (user_id, {", ".join(carried)})
            SELECT u.id, {", ".join(f"old.{c}" for c in carried)}
            FROM {table}_old old
            JOIN users u ON u.telegram_chat_id = old.chat_id;
            DROP TABLE {table}_old;
        """)

    # thread_id: str(chat_id) -> "{user_id}:{chain_id}". Only rewritten for users whose chain is
    # known; anything else is orphaned history and is deleted rather than left under a key nothing
    # will ever look up again.
    for table in ("checkpoints", "writes"):
        if not db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        db.execute(f"""
            UPDATE {table} SET thread_id = (
                SELECT u.id || ':' || c.chain_id
                FROM users u
                JOIN user_network un ON un.user_id = u.id
                JOIN chains c ON c.name = un.chain_name
                WHERE CAST(u.telegram_chat_id AS TEXT) = {table}.thread_id
            )
            WHERE EXISTS (
                SELECT 1 FROM users u
                JOIN user_network un ON un.user_id = u.id
                JOIN chains c ON c.name = un.chain_name
                WHERE CAST(u.telegram_chat_id AS TEXT) = {table}.thread_id
            )
        """)

    db.commit()
    print(f"Migrated identity from chat_id to user_id ({len(legacy)} user(s) created).")


def init_db():
    """
    Creates all tables if they do not already exist, migrating any that predate the per-chain
    wallet layout or the chat_id -> user_id identity switch. Safe to call on every startup.
    """
    db = get_db()
    # Order matters: _migrate_add_chain_id still reads and writes chat_id columns, so it has to
    # finish before the identity migration renames them away.
    _migrate_add_chain_id(db)
    _migrate_chat_id_to_user_id(db)
    db.executescript("""
        -- Vestigial tables from the per-target session-key design (dropped so `make db` cleans an
        -- existing wallet.db). The module now enforces ONE global USD spending cap: there are no
        -- per-target sessions and no on-chain (target, selector) allowlists to store.
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS erc20_selectors;
        DROP TABLE IF EXISTS uniswapv2_selectors;
        DROP TABLE IF EXISTS reputation_registry_selectors;

        -- The account. Identity is an application user, NOT a Telegram chat: the same person
        -- reaches this system from the web app and from Telegram, and the id below is what every
        -- other per-user table keys on. telegram_chat_id is one OPTIONAL way to reach that
        -- account, UNIQUE so two accounts can never claim the same Telegram user -- which is the
        -- whole reason a chat id is bound through the nonce flow instead of being typed in.
        -- email/password_hash/google_sub/owner_addr are all nullable: an account may be created
        -- by any one of the sign-in methods and gain the others later.
        CREATE TABLE IF NOT EXISTS users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            email            TEXT UNIQUE,
            password_hash    TEXT,
            google_sub       TEXT UNIQUE,
            owner_addr       TEXT UNIQUE,
            telegram_chat_id INTEGER UNIQUE,
            created_at       INTEGER NOT NULL
        );

        -- Single-use, short-lived nonces for the Telegram deep link. The bot reads one from
        -- /start's payload and binds the chat id FROM THE TELEGRAM UPDATE, so a chat id is never
        -- self-asserted (typing one in would let anyone attach their Telegram to another
        -- account's wallet and spend up to its cap).
        CREATE TABLE IF NOT EXISTS telegram_link_nonces (
            nonce      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );

        -- Only the SHA-256 of each refresh token is stored, so a database read cannot be replayed
        -- as a login. Rotation marks the old row revoked rather than deleting it, which is what
        -- makes reuse of a stolen token detectable.
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked    INTEGER NOT NULL DEFAULT 0
        );

        -- Nonces for SIWE (EIP-4361) wallet binding, issued before the user signs and burned on
        -- verify so a captured signature cannot be replayed.
        CREATE TABLE IF NOT EXISTS siwe_nonces (
            nonce     TEXT PRIMARY KEY,
            issued_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS contacts (
            user_id INTEGER NOT NULL,
            name    TEXT NOT NULL,
            address TEXT NOT NULL,
            PRIMARY KEY (user_id, name)
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
        CREATE TABLE IF NOT EXISTS bsc_tokens (
            ticker  TEXT PRIMARY KEY,
            address TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS celo_tokens (
            ticker  TEXT PRIMARY KEY,
            address TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arbitrum_tokens (
            ticker  TEXT PRIMARY KEY,
            address TEXT NOT NULL
        );

        -- One wallet PER CHAIN per user: the protocol is deployed on several chains and a user
        -- runs a SessionHandler on each, so chain_id is part of the key. Without it a deploy on
        -- one chain silently overwrote the user's record on every other.
        CREATE TABLE IF NOT EXISTS session_handlers (
            user_id INTEGER NOT NULL,
            chain_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            PRIMARY KEY (user_id, chain_id)
        );

        -- Which chain the user is currently pointed at. Stays one row per user: it is the user's
        -- CURRENT selection, not an inventory (session_handlers holds the inventory).
        CREATE TABLE IF NOT EXISTS user_network (
            user_id INTEGER PRIMARY KEY,
            chain_name TEXT  NOT NULL
        );

        -- Keyed by chain as well as target so each per-chain wallet has its OWN key. A user's
        -- wallet can share one address across chains (same factory address + same CREATE2 salt),
        -- so without chain_id those wallets would collide on one row and share a single key.
        CREATE TABLE IF NOT EXISTS session_keys (
            user_id         INTEGER NOT NULL,
            chain_id        INTEGER NOT NULL,
            target          TEXT NOT NULL,
            key_address     TEXT NOT NULL,
            key_ciphertext  TEXT NOT NULL,
            PRIMARY KEY (user_id, chain_id, target)
        );

        -- A key minted for a grant the owner has NOT signed yet. Same shape and key as session_keys,
        -- deliberately a separate table: until SessionHandler.addSession actually mines, the wallet
        -- still authorizes the OLD key, and writing the new one over it would leave the assistant
        -- signing with a key the chain rejects -- with the old ciphertext already gone. The row is
        -- promoted into session_keys only once the chain confirms it (see userop.reconcile_session_key).
        CREATE TABLE IF NOT EXISTS pending_session_keys (
            user_id         INTEGER NOT NULL,
            chain_id        INTEGER NOT NULL,
            target          TEXT NOT NULL,
            key_address     TEXT NOT NULL,
            key_ciphertext  TEXT NOT NULL,
            PRIMARY KEY (user_id, chain_id, target)
        );

                
       
                     
         CREATE TABLE IF NOT EXISTS factory (
            chain_id  INTEGER PRIMARY KEY,
            address  TEXT NOT NULL
        );
                     
        

    """)
    db.commit()


def seed_reference_data():
    """
    Seeds reference data from seed_data.SEEDS into the SQLite DB, then records
    the deployed SHFactory address per chain from the Forge broadcast files.
    Uses INSERT OR REPLACE, so re-running `make db` is idempotent: existing rows
    are updated and missing rows are inserted without needing to delete the DB first.
    """
    db = get_db()

    for table, key_col, value_col, data, checksum in SEEDS:
        for key, value in data.items():
            if checksum:
                value = Web3.to_checksum_address(value)
            db.execute(
                f"INSERT OR REPLACE INTO {table} ({key_col}, {value_col}) VALUES (?, ?)",
                (key, value),
            )

    for chain_id in CHAIN_IDs:

        if os.path.exists(f"./broadcast/DeploySHProtocol.s.sol/{chain_id}/run-latest.json"):
            broadcast_data = get_json(f"./broadcast/DeploySHProtocol.s.sol/{chain_id}/run-latest.json")
            address = None
            for item in broadcast_data["transactions"]:
                if item["contractName"]=="SHFactory":
                    address = item["contractAddress"]

            if address is not None:
                db.execute(
                    "INSERT OR REPLACE INTO factory (chain_id, address) VALUES (?, ?)",
                    (chain_id, Web3.to_checksum_address(address)),
                )

    # Anvil has no real token deployments to hardcode (unlike mainnet/sepolia/bsc/celo in
    # seed_data.py): HelperConfig.getOrCreateAnvilConfig() deploys fresh ERC20Mock/MockWeth mocks
    # inside the (broadcast) deploy, at addresses that change every run. Recover ticker->address
    # from the broadcast's decoded constructor arguments (symbol is arg index 1), e.g.
    # `new ERC20Mock("Circle USD","USDC",6)` -> ticker "usdc". This is the only writer of anvil_tokens.
    anvil_broadcast = f"./broadcast/DeploySHProtocol.s.sol/{CHAIN_ID_ANVIL}/run-latest.json"
    if os.path.exists(anvil_broadcast):
        for item in get_json(anvil_broadcast)["transactions"]:
            if item.get("contractName") in ("ERC20Mock", "MockWeth"):
                args = item.get("arguments") or []
                if len(args) >= 2 and item.get("contractAddress"):
                    ticker = str(args[1]).strip().strip('"').lower()
                    db.execute(
                        "INSERT OR REPLACE INTO anvil_tokens (ticker, address) VALUES (?, ?)",
                        (ticker, Web3.to_checksum_address(item["contractAddress"])),
                    )

    db.commit()
    print("Seeding complete.")


# ── Session handlers ──────────────────────────────────────────────────────────


def save_wallet_address(user_id: int, chain_id: int, address: str):
    """
    Saves the deployed SessionHandler address for a given user ON A GIVEN CHAIN.

    Keyed by (user_id, chain_id): the protocol is deployed on several chains and one user runs one
    wallet on each, so a Sepolia deploy must not overwrite that user's Arbitrum record. Replacing
    within a chain is still the behaviour — redeploying on the same chain repoints the user at the
    new wallet and leaves the old one funded but unreferenced (deploy_wallet warns before doing it).

    @param user_id   The application user ID.
    @param chain_id  The numeric chain ID the wallet is deployed on.
    @param address   The checksummed Ethereum address of the deployed SessionHandler.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO session_handlers (user_id, chain_id, address) VALUES (?, ?, ?)",
        (user_id, chain_id, address),
    )
    db.commit()


def get_wallet_address(user_id: int, chain_id: int) -> str:
    """
    Retrieves the SessionHandler address for a given user on a given chain.

    @param user_id   The application user ID.
    @param chain_id  The numeric chain ID to look the wallet up on.
    @return          The checksummed Ethereum address of the SessionHandler.
    @raises ValueError  If this user has no wallet on this chain.
    """
    row = (
        get_db()
        .execute(
            "SELECT address FROM session_handlers WHERE user_id = ? AND chain_id = ?",
            (user_id, chain_id),
        )
        .fetchone()
    )

    if row is None:
        raise ValueError(f"No SessionHandler address found for user {user_id} on chain {chain_id}")

    return row["address"]


def get_wallet_chains(user_id: int) -> list[int]:
    """
    Returns every chain ID this user already has a wallet on, ascending.

    @param user_id  The application user ID.
    @return         A list of chain IDs (empty if the user has no wallet anywhere).
    """
    rows = (
        get_db()
        .execute(
            "SELECT chain_id FROM session_handlers WHERE user_id = ? ORDER BY chain_id ASC",
            (user_id,),
        )
        .fetchall()
    )
    return [row["chain_id"] for row in rows]


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
    table = _TOKEN_TABLE_BY_CHAIN_ID.get(chain_id)
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


# ── SHFactory ─────────────────────────────────────────────────────────────────


def get_factory_address(chain_id: int) -> str:
    """
    Retrieves the deployed SHFactory contract address for a given chain.

    @param chain_id  The numeric chain ID to look up (e.g. 31337 for Anvil, 1 for mainnet).
    @return          The checksummed Ethereum address of the SHFactory.
    @raises ValueError  If no SHFactory address has been saved for the given chain ID.
    """
    row = (
        get_db()
        .execute("SELECT address FROM factory WHERE chain_id = ?", (chain_id,))
        .fetchone()
    )

    if row is None:
        raise ValueError(f"No SHFactory address found for chain_id {chain_id}")

    return Web3.to_checksum_address(row["address"])



# ── Session keys ─────────────────────────────────────────────────────────────


def get_session_key(user_id: int, chain_id: int, target: str) -> tuple[str, str] | None:
    """
    Retrieves the stored session key address and Vault ciphertext for a user, chain and target.

    @param user_id   The application user ID.
    @param chain_id  The chain the key is authorized on.
    @param target    The contract address the session key is scoped to (the wallet's address).
    @return          A tuple of (key_address, key_ciphertext), or None if not found.
    """
    row = (
        get_db()
        .execute(
            "SELECT key_address, key_ciphertext FROM session_keys "
            "WHERE user_id = ? AND chain_id = ? AND target = ?",
            (user_id, chain_id, target),
        )
        .fetchone()
    )
    return (row["key_address"], row["key_ciphertext"]) if row else None


def delete_session_key(user_id: int, chain_id: int, target: str) -> None:
    """
    Forgets the session key held for a user's wallet on a chain. No-op when there is none.

    Called once a revocation has MINED, so the app stops holding a key the wallet no longer accepts.
    Dropping the ciphertext is the point: a key revoked because it leaked must never come back, and
    while the row existed the next grant could hand the same address straight back.

    @param user_id   The application user ID.
    @param chain_id  The chain the key was authorized on.
    @param target    The wallet address the key was held for.
    """
    db = get_db()
    db.execute(
        "DELETE FROM session_keys WHERE user_id = ? AND chain_id = ? AND target = ?",
        (user_id, chain_id, target),
    )
    db.commit()


def get_pending_session_key(user_id: int, chain_id: int, target: str) -> tuple[str, str] | None:
    """
    Retrieves the session key minted for a grant that has not been confirmed on chain yet.

    @return  A tuple of (key_address, key_ciphertext), or None if no grant is outstanding.
    """
    row = (
        get_db()
        .execute(
            "SELECT key_address, key_ciphertext FROM pending_session_keys "
            "WHERE user_id = ? AND chain_id = ? AND target = ?",
            (user_id, chain_id, target),
        )
        .fetchone()
    )
    return (row["key_address"], row["key_ciphertext"]) if row else None


def save_pending_session_key(user_id: int, chain_id: int, target: str, key_address: str, key_ciphertext: str):
    """
    Stores a freshly minted key against a grant the owner has yet to sign.

    One outstanding grant per wallet: INSERT OR REPLACE, so preparing a second grant abandons the
    first rather than leaving two candidates for the same wallet.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO pending_session_keys (user_id, chain_id, target, key_address, key_ciphertext) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, chain_id, target, key_address, key_ciphertext),
    )
    db.commit()


def delete_pending_session_key(user_id: int, chain_id: int, target: str) -> None:
    """Drops an outstanding grant's key, once it is promoted or known to be dead."""
    db = get_db()
    db.execute(
        "DELETE FROM pending_session_keys WHERE user_id = ? AND chain_id = ? AND target = ?",
        (user_id, chain_id, target),
    )
    db.commit()


def save_session_key(user_id: int, chain_id: int, target: str, key_address: str, key_ciphertext: str):
    """
    Stores an encrypted session key for a user, chain and target.

    Keyed by chain as well as target so each of a user's per-chain wallets gets its OWN key. That
    matters beyond bookkeeping: deploying the protocol identically on several chains can land
    SHFactory at the same address on each, and the CREATE2 salt is the same too, so a user's wallet
    can share one address across chains. Without chain_id in the key those wallets would collide on
    a single row and share one session key, making a single key compromise reach every chain.

    @param user_id        The application user ID.
    @param chain_id        The chain the key is authorized on.
    @param target          The contract address the session key is scoped to.
    @param key_address     The Ethereum address derived from the session key.
    @param key_ciphertext  The Vault Transit ciphertext blob ('vault:v1:...').
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO session_keys (user_id, chain_id, target, key_address, key_ciphertext) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, chain_id, target, key_address, key_ciphertext),
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


# ── Tokens (supported list) ───────────────────────────────────────────────────


def get_supported_tokens(user_id: int) -> list[str]:
    """
    Returns all supported token tickers for the network the user is connected to,
    sorted alphabetically. Resolves the correct table (anvil_tokens or mainnet_tokens)
    by looking up the user's saved network via get_user_network().

    @param user_id  The application user ID. Used to determine which network
                    the user is on and therefore which token table to query.
    @return         A list of ticker strings (e.g. ["dai", "usdc"]).
    @raises ValueError  If the user's network is not set or is not "anvil" or "mainnet".
    """
    network = get_user_network(user_id)
    prefix = _NETWORK_DB_PREFIX.get(network)
    if prefix is None:
        raise ValueError(f"Unsupported network: '{network}'")
    table = f"{prefix}_tokens"
    rows = (
        get_db().execute(f"SELECT ticker FROM {table} ORDER BY ticker ASC").fetchall()
    )
    return [row["ticker"] for row in rows]


def get_supported_tokens_by_chain_id(chain_id: int) -> list[dict]:
    """
    Returns every supported token on `chain_id` as {"ticker", "address"} dicts, sorted by ticker.

    The by-chain_id twin of get_supported_tokens(), which resolves the table through the user's
    SAVED network. The API needs the list for a chain the user has not switched to yet — they pick
    watched tokens for the wallet they are about to deploy, before any network is saved for them —
    so the chain is passed explicitly here rather than read back out of user_network.

    Keyed by chain ID, not name, so it answers the same question get_token_address does off the same
    map. That also sidesteps the fork ambiguity: `sepolia` and `sepolia-fork` share chain 11155111
    AND the token table, so the caller does not need to have picked between them to list tokens.

    Returns the address alongside the ticker because the front end shows the ticker but the deploy
    endpoint validates what comes back; sending both lets it display one and echo the other.

    @param chain_id  The numeric chain ID to list tokens for.
    @return          A list of {"ticker": str, "address": str} dicts, empty if the table is unseeded.
    @raises ValueError If chain_id has no token table.
    """
    table = _TOKEN_TABLE_BY_CHAIN_ID.get(chain_id)
    if table is None:
        raise ValueError(f"Unsupported chain_id: {chain_id}")
    rows = (
        get_db()
        .execute(f"SELECT ticker, address FROM {table} ORDER BY ticker ASC")
        .fetchall()
    )
    return [{"ticker": row["ticker"], "address": row["address"]} for row in rows]


# ── Contacts ──────────────────────────────────────────────────────────────────


def save_contact(user_id: int, name: str, address: str):
    """
    Saves or updates a contact, associating a name with an Ethereum address.

    @param user_id   The application user ID.
    @param name      Human-readable label for the contact. Stored in lowercase.
    @param address   The Ethereum address to associate with the name.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO contacts (user_id, name, address) VALUES (?, ?, ?)",
        (user_id, name.lower(), address),
    )
    db.commit()
    print(f"Contact saved: {name}")


def get_contact(user_id: int, name: str) -> str | None:
    """
    Looks up the Ethereum address of a saved contact by name.

    @param user_id  The application user ID.
    @param name     The contact name to look up. Case-insensitive.
    @return         The Ethereum address, or None if not found.
    """
    row = (
        get_db()
        .execute(
            "SELECT address FROM contacts WHERE user_id = ? AND name = ?",
            (user_id, name.lower()),
        )
        .fetchone()
    )

    if row is None:
        print("Contact doesn't exist")
        return None

    return row["address"]


def get_all_contacts(user_id: int) -> list[dict]:
    """
    Returns all saved contacts for a user, sorted alphabetically by name.

    @param user_id  The application user ID.
    @return         A list of dicts with 'name' and 'address' keys.
    """
    rows = (
        get_db()
        .execute(
            "SELECT name, address FROM contacts WHERE user_id = ? ORDER BY name ASC",
            (user_id,),
        )
        .fetchall()
    )
    return [{"name": row["name"], "address": row["address"]} for row in rows]


def delete_contact(user_id: int, name: str) -> str:
    """
    Deletes a saved contact by name. Does nothing if the contact does not exist.

    @param user_id  The application user ID.
    @param name     The contact name to delete. Case-insensitive.
    @return         A confirmation string.
    """
    db = get_db()
    db.execute(
        "DELETE FROM contacts WHERE user_id = ? AND name = ?",
        (user_id, name.lower()),
    )
    db.commit()
    return f"Contact deleted: {name}"


def save_user_network(user_id: int, chain_name: str):

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO user_network (user_id, chain_name) VALUES (?, ?)",
        (user_id, chain_name),
    )
    db.commit()
    print(f"User network saved: {chain_name}")


def get_user_network(user_id: int):

    row = (
        get_db()
        .execute(
            "SELECT chain_name FROM user_network WHERE user_id = ?",
            (user_id,),
        )
        .fetchone()
    )

    return row["chain_name"] if row else None


# ── Users ─────────────────────────────────────────────────────────────────────


def create_user(
    email: str | None = None,
    password_hash: str | None = None,
    google_sub: str | None = None,
    owner_addr: str | None = None,
) -> int:
    """
    Creates an account and returns its new user ID.

    Every field is optional because an account can be born from any sign-in method and pick up the
    others later. The returned id is THE identity for this person everywhere else in the app.

    @param email          Login email, lowercased by the caller's validation. Must be unique.
    @param password_hash  An Argon2id hash from auth.hash_password. Never a plaintext password.
    @param google_sub     Google's stable subject claim. Keyed on instead of email, which can change.
    @param owner_addr     The EOA that owns this user's wallet on chain.
    @return               The new user_id.
    @raises ValueError    If any unique field is already taken.
    """
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, google_sub, owner_addr, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, password_hash, google_sub, owner_addr, int(time.time())),
        )
    except sqlite3.IntegrityError as e:
        raise ValueError(f"Account already exists: {e}")
    db.commit()
    return cur.lastrowid


def get_user_by_id(user_id: int) -> dict | None:
    """
    Returns the account row for a user ID, or None.

    @param user_id  The application user ID.
    @return         A dict of the users row, or None if no such account.
    """
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    """
    Returns the account row for an email address, or None. Matching is case-insensitive.

    @param email  The email to look up.
    @return       A dict of the users row, or None.
    """
    row = (
        get_db()
        .execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
        .fetchone()
    )
    return dict(row) if row else None


def get_user_by_google_sub(google_sub: str) -> dict | None:
    """
    Returns the account row for a Google subject claim, or None.

    Google sign-in keys on `sub` rather than email because a Google account's email can change
    while `sub` cannot, and because an unverified email must never select an account.

    @param google_sub  The `sub` claim from a verified Google ID token.
    @return            A dict of the users row, or None.
    """
    row = (
        get_db()
        .execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,))
        .fetchone()
    )
    return dict(row) if row else None


def get_user_by_owner_addr(owner_addr: str) -> dict | None:
    """
    Returns the account row that has bound this EOA, or None.

    @param owner_addr  A checksummed Ethereum address.
    @return            A dict of the users row, or None.
    """
    row = (
        get_db()
        .execute("SELECT * FROM users WHERE owner_addr = ?", (owner_addr,))
        .fetchone()
    )
    return dict(row) if row else None


def get_user_id_by_telegram_chat_id(chat_id: int) -> int | None:
    """
    Translates a Telegram chat ID into the application user ID it is bound to.

    The bot's entry point. A chat that has not been linked returns None, and the caller must
    refuse to serve it -- an unlinked chat has no account and therefore no wallet.

    @param chat_id  The chat ID taken from the Telegram update (never from user input).
    @return         The user_id, or None if this chat is not linked to any account.
    """
    row = (
        get_db()
        .execute("SELECT id FROM users WHERE telegram_chat_id = ?", (chat_id,))
        .fetchone()
    )
    return row["id"] if row else None


def set_password_hash(user_id: int, password_hash: str):
    """
    Sets or replaces an account's password hash.

    @param user_id        The application user ID.
    @param password_hash  An Argon2id hash from auth.hash_password.
    """
    db = get_db()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    db.commit()


def link_google(user_id: int, google_sub: str):
    """
    Binds a Google account to an existing user.

    @param user_id     The application user ID.
    @param google_sub  The `sub` claim from a verified Google ID token.
    @raises ValueError If that Google account is already bound elsewhere.
    """
    db = get_db()
    try:
        db.execute("UPDATE users SET google_sub = ? WHERE id = ?", (google_sub, user_id))
    except sqlite3.IntegrityError:
        raise ValueError("That Google account is already linked to another user.")
    db.commit()


def link_owner_addr(user_id: int, owner_addr: str):
    """
    Binds the on-chain owner EOA to an account.

    Worth surfacing in the UI at bind time: this address owns the SessionHandler on chain and can
    only be changed by transferOwnership signed by the current owner. If the user loses it,
    account recovery cannot help -- the app has no authority over their wallet.

    @param user_id     The application user ID.
    @param owner_addr  A checksummed Ethereum address.
    @raises ValueError If that address is already bound to another account.
    """
    db = get_db()
    try:
        db.execute("UPDATE users SET owner_addr = ? WHERE id = ?", (owner_addr, user_id))
    except sqlite3.IntegrityError:
        raise ValueError("That address is already linked to another user.")
    db.commit()


def link_telegram(user_id: int, chat_id: int):
    """
    Binds a Telegram chat to an account.

    @param user_id  The application user ID.
    @param chat_id  The chat ID from the Telegram update.
    @raises ValueError If that chat is already linked to another account.
    """
    db = get_db()
    try:
        db.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat_id, user_id))
    except sqlite3.IntegrityError:
        raise ValueError("That Telegram account is already linked to another user.")
    db.commit()


def unlink_telegram(user_id: int):
    """
    Detaches whatever Telegram chat is bound to an account.

    @param user_id  The application user ID.
    """
    db = get_db()
    db.execute("UPDATE users SET telegram_chat_id = NULL WHERE id = ?", (user_id,))
    db.commit()


# ── Telegram link nonces ──────────────────────────────────────────────────────


def save_telegram_link_nonce(nonce: str, user_id: int, expires_at: int):
    """
    Records a pending Telegram link.

    @param nonce       A single-use random token, carried in the t.me deep link.
    @param user_id     The account the nonce will bind the chat to.
    @param expires_at  Unix seconds after which the nonce is refused.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO telegram_link_nonces (nonce, user_id, expires_at) VALUES (?, ?, ?)",
        (nonce, user_id, expires_at),
    )
    db.commit()


def consume_telegram_link_nonce(nonce: str) -> int | None:
    """
    Redeems a Telegram link nonce, returning the user it was minted for.

    Burns the nonce whether or not it had expired, so a leaked deep link cannot be retried, and
    deletes before checking expiry for the same reason.

    @param nonce  The token from /start's payload.
    @return       The user_id, or None if the nonce is unknown or expired.
    """
    db = get_db()
    row = (
        db.execute(
            "SELECT user_id, expires_at FROM telegram_link_nonces WHERE nonce = ?", (nonce,)
        ).fetchone()
    )
    db.execute("DELETE FROM telegram_link_nonces WHERE nonce = ?", (nonce,))
    db.commit()
    if row is None or row["expires_at"] < int(time.time()):
        return None
    return row["user_id"]


# ── Refresh tokens ────────────────────────────────────────────────────────────


def save_refresh_token(token_hash: str, user_id: int, expires_at: int):
    """
    Stores the SHA-256 of a refresh token.

    @param token_hash  Hex SHA-256 of the opaque token. The token itself is never stored, so a
                       database read cannot be replayed as a login.
    @param user_id     The account the token authenticates.
    @param expires_at  Unix seconds after which the token is refused.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO refresh_tokens (token_hash, user_id, expires_at, revoked) "
        "VALUES (?, ?, ?, 0)",
        (token_hash, user_id, expires_at),
    )
    db.commit()


def get_refresh_token(token_hash: str) -> dict | None:
    """
    Looks up a stored refresh token by hash.

    Returns revoked and expired rows too: the caller has to tell "unknown" from "revoked" apart,
    because presenting an already-rotated token means the token was stolen and every session for
    that user should be dropped.

    @param token_hash  Hex SHA-256 of the presented token.
    @return            A dict with user_id, expires_at and revoked, or None if unknown.
    """
    row = (
        get_db()
        .execute("SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,))
        .fetchone()
    )
    return dict(row) if row else None


def revoke_refresh_token(token_hash: str):
    """
    Marks one refresh token revoked, leaving the row in place so its later reuse is detectable.

    @param token_hash  Hex SHA-256 of the token to revoke.
    """
    db = get_db()
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
    db.commit()


def revoke_all_refresh_tokens(user_id: int):
    """
    Revokes every refresh token for an account -- logout-everywhere, and the response to a
    detected token reuse.

    @param user_id  The application user ID.
    """
    db = get_db()
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
    db.commit()


def purge_expired_refresh_tokens():
    """Deletes refresh tokens that expired more than 30 days ago, so the table cannot grow forever."""
    db = get_db()
    db.execute(
        "DELETE FROM refresh_tokens WHERE expires_at < ?", (int(time.time()) - 30 * 86_400,)
    )
    db.commit()


# ── SIWE nonces ───────────────────────────────────────────────────────────────


def save_siwe_nonce(nonce: str):
    """
    Records a SIWE nonce as issued.

    @param nonce  The random nonce embedded in the message the user will sign.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO siwe_nonces (nonce, issued_at) VALUES (?, ?)",
        (nonce, int(time.time())),
    )
    db.commit()


def consume_siwe_nonce(nonce: str, ttl_secs: int) -> bool:
    """
    Redeems a SIWE nonce, burning it so a captured signature cannot be replayed.

    @param nonce     The nonce parsed out of the signed message.
    @param ttl_secs  How long after issue a nonce stays valid.
    @return          True if the nonce was outstanding and still fresh.
    """
    db = get_db()
    row = db.execute("SELECT issued_at FROM siwe_nonces WHERE nonce = ?", (nonce,)).fetchone()
    db.execute("DELETE FROM siwe_nonces WHERE nonce = ?", (nonce,))
    db.commit()
    return row is not None and row["issued_at"] >= int(time.time()) - ttl_secs


if __name__ == "__main__":
    init_db()
    seed_reference_data()
