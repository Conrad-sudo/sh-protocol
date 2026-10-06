import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from web3 import Web3
from constants import (
    CHAIN_ID_ANVIL, CHAIN_ID_ARBITRUM, CHAIN_ID_BSC, CHAIN_ID_CELO, CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
)
from seed_data import CHAINS, SEEDS, SUPPORTED_TOKENS

CHAIN_IDs=[
    CHAIN_ID_ANVIL, CHAIN_ID_MAINNET, CHAIN_ID_SEPOLIA, CHAIN_ID_BSC, CHAIN_ID_CELO,
    CHAIN_ID_ARBITRUM,
]

DB_PATH = "./app/wallet.db"

_local = threading.local()

# The per-network token tables that supported_tokens replaced, and the chain each one held. Only
# _migrate_token_tables reads this, to carry an older wallet.db's rows across.
_LEGACY_TOKEN_TABLES: dict[str, int] = {
    "anvil_tokens": CHAIN_ID_ANVIL,
    "mainnet_tokens": CHAIN_ID_MAINNET,
    "sepolia_tokens": CHAIN_ID_SEPOLIA,
    "bsc_tokens": CHAIN_ID_BSC,
    "celo_tokens": CHAIN_ID_CELO,
    "arbitrum_tokens": CHAIN_ID_ARBITRUM,
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


def _migrate_token_tables(db: sqlite3.Connection):
    """
    Moves the rows of the old per-network token tables (mainnet_tokens, sepolia_tokens, ...) into
    supported_tokens under their chain ID, then drops the old tables.

    Anvil's rows matter most: they come from the deploy broadcast, not seed_data, so re-seeding
    would not bring them back without the broadcast. The seeded chains' rows are replaced by
    seed_reference_data anyway, but are carried too so init_db alone leaves nothing unlisted.

    MUST run after supported_tokens exists.
    """
    for table, chain_id in _LEGACY_TOKEN_TABLES.items():
        if not db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue  # fresh install, or already migrated
        db.execute(
            f"INSERT OR REPLACE INTO supported_tokens (chain_id, ticker, address) "
            f"SELECT ?, ticker, address FROM {table}",
            (chain_id,),
        )
        db.execute(f"DROP TABLE {table}")
        print(f"Migrated {table} into supported_tokens (chain {chain_id}).")
    db.commit()


def init_db():
    """
    Creates all tables if they do not already exist, migrating any that predate the per-chain
    wallet layout, the chat_id -> user_id identity switch or the single supported_tokens table.
    Safe to call on every startup.
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
        -- owner_addr is how the account signs in (SIWE, the only way in) and the EOA that owns its
        -- wallets on chain. Nullable only for accounts migrated from Telegram-only days, which
        -- can't sign in on the web; every account made since has one.
        CREATE TABLE IF NOT EXISTS users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
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

        -- Nonces for SIWE (EIP-4361) sign-in, issued before the user signs and burned on use so a
        -- captured signature cannot be replayed.
        CREATE TABLE IF NOT EXISTS siwe_nonces (
            nonce     TEXT PRIMARY KEY,
            issued_at INTEGER NOT NULL
        );

        -- Nonces for the EIP-712 signature that adds a contact (auth.contact_typed_data). Each is
        -- issued for one account, name and address, and burned on use.
        CREATE TABLE IF NOT EXISTS contact_nonces (
            nonce     TEXT PRIMARY KEY,
            user_id   INTEGER NOT NULL,
            name      TEXT NOT NULL,
            address   TEXT NOT NULL,
            issued_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS contacts (
            user_id INTEGER NOT NULL,
            name    TEXT NOT NULL,
            address TEXT NOT NULL,
            PRIMARY KEY (user_id, name)
        );

        -- Tokens a user added by address in the web app, MetaMask-style, per chain. Unlike the
        -- listed supported_tokens these have no price feed, so they can never count toward
        -- the spending cap; the list only decides what is shown and what the assistant can name.
        -- Keyed per chain, not per wallet, so it survives a redeploy. `ticker` is the token's own
        -- symbol(), lowercased and validated when it was added (custom_tokens.py) -- unique per
        -- user and chain so a name always resolves to one address.
        CREATE TABLE IF NOT EXISTS custom_tokens (
            user_id  INTEGER NOT NULL,
            chain_id INTEGER NOT NULL,
            address  TEXT NOT NULL,
            ticker   TEXT NOT NULL,
            name     TEXT,
            decimals INTEGER NOT NULL,
            added_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, chain_id, address),
            UNIQUE (user_id, chain_id, ticker)
        );

        -- The listed tokens each user shows on their dashboard, per chain. A token the wallet
        -- counts toward its limit is always here: the wallet read copies the counted list in
        -- (api.get_wallet_state), and removing one is refused while it still counts. Tokens added
        -- by address live in custom_tokens instead, and being there is what shows them. Keyed by
        -- ticker so the read is one join on supported_tokens, and a token Mitfah stops listing
        -- drops off every dashboard with it.
        CREATE TABLE IF NOT EXISTS dashboard_tokens (
            user_id  INTEGER NOT NULL,
            chain_id INTEGER NOT NULL,
            ticker   TEXT NOT NULL,
            added_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, chain_id, ticker)
        );

        -- The exchange pools (Uniswap V2, PancakeSwap on BSC) the assistant deposited into for each
        -- user, per chain: the dashboard shows the wallet's LP tokens in each one while it holds
        -- any, and nothing once it holds none. Saved when the user agrees to a deposit, before it is
        -- sent (tools.confirm_transaction), so a send that lands late still shows. Kept apart from
        -- custom_tokens on purpose: an LP token has no price, so the assistant must never be able to
        -- name one (tools._token_address) -- this list is the dashboard's alone. token0/token1 are
        -- the pool's tokens in the pool's own order (lower address first), so a pool is one row
        -- whichever way round it was named; the wrapped native token's ticker is the native
        -- asset's ("eth"), since that is what goes in and comes back. `pair` and the decimals are
        -- read off the chain the first time the dashboard shows the pool (api._lp_balances).
        CREATE TABLE IF NOT EXISTS lp_tokens (
            user_id   INTEGER NOT NULL,
            chain_id  INTEGER NOT NULL,
            token0    TEXT NOT NULL,
            token1    TEXT NOT NULL,
            ticker0   TEXT NOT NULL,
            ticker1   TEXT NOT NULL,
            pair      TEXT,
            decimals0 INTEGER,
            decimals1 INTEGER,
            added_at  INTEGER NOT NULL,
            PRIMARY KEY (user_id, chain_id, token0, token1)
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

        -- The tokens Mitfah lists on each chain: the ones the protocol's oracle prices, so a wallet
        -- can count them toward its limit. A fork shares its parent's rows because it shares the
        -- parent's chain ID, and with it the state and token addresses. A ticker names one address
        -- and an address carries one ticker, so each always resolves to the other. Addresses are
        -- checksummed. `make db` makes each chain's rows match seed_data exactly (anvil's: its
        -- deploy broadcast).
        CREATE TABLE IF NOT EXISTS supported_tokens (
            chain_id INTEGER NOT NULL,
            ticker   TEXT NOT NULL,
            address  TEXT NOT NULL,
            PRIMARY KEY (chain_id, ticker),
            UNIQUE (chain_id, address)
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

        -- Every transaction made through Mitfah, for the History tab. Kept apart from the chat,
        -- which is cleared after each transaction, so a hash never lives only in a conversation.
        -- `source` is 'assistant' (a UserOperation sent with the session key), 'owner' (signed
        -- by the user's own wallet in the browser) or 'outside' (anything else that touched the
        -- wallet, read from a block explorer). `action` is written by the server, never by the
        -- model, the browser or the explorer. An assistant row is written BEFORE its op is broadcast, keyed
        -- by user_op_hash, so a send that outlives the wait is still here as 'pending' --
        -- op_nonce and from_block are what tx_history.settle_pending needs to finish it later.
        -- `status` is 'pending', 'confirmed', 'failed' (mined, reverted) or 'dropped' (never
        -- mined, and now never will be). `mined_at` is the block's timestamp.
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            chain_id     INTEGER NOT NULL,
            wallet       TEXT NOT NULL,
            source       TEXT NOT NULL,
            action       TEXT NOT NULL,
            status       TEXT NOT NULL,
            tx_hash      TEXT,
            user_op_hash TEXT,
            op_nonce     TEXT,
            from_block   INTEGER,
            created_at   INTEGER NOT NULL,
            mined_at     INTEGER
        );
        CREATE INDEX IF NOT EXISTS transactions_by_user ON transactions (user_id, id);
        -- One row per op, and per owner transaction: the web confirms are polled, so the same
        -- hash arrives many times.
        CREATE UNIQUE INDEX IF NOT EXISTS transactions_by_op
            ON transactions (chain_id, user_op_hash) WHERE user_op_hash IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS transactions_by_owner_hash
            ON transactions (chain_id, tx_hash) WHERE source = 'owner';
        -- 'outside' rows: activity on the wallet that didn't go through Mitfah, read from a block
        -- explorer (tx_history.sync_outside). One per wallet and hash, so searching the same blocks
        -- twice adds nothing. Per wallet, not per chain: when one user's assistant pays another
        -- user, the payee still gets an outside row for the payer's transaction.
        CREATE UNIQUE INDEX IF NOT EXISTS transactions_by_outside_hash
            ON transactions (chain_id, wallet, tx_hash) WHERE source = 'outside';
        -- The History tab lists by time, not by id: outside rows are recorded long after they mined.
        CREATE INDEX IF NOT EXISTS transactions_by_user_time
            ON transactions (user_id, COALESCE(mined_at, created_at), id);

        -- How far the block-explorer search has got for each wallet: the block the next search
        -- starts from (NULL until a search has finished a batch), and when the last one ran (a
        -- wallet is searched at most once a minute).
        CREATE TABLE IF NOT EXISTS history_sync (
            chain_id   INTEGER NOT NULL,
            wallet     TEXT NOT NULL,
            next_block INTEGER,
            synced_at  INTEGER NOT NULL,
            PRIMARY KEY (chain_id, wallet)
        );



    """)
    db.commit()
    # After the schema, because it writes into supported_tokens.
    _migrate_token_tables(db)


def _replace_supported_tokens(db: sqlite3.Connection, chain_id: int, tokens: dict[str, str]):
    """
    Makes `chain_id`'s rows in supported_tokens exactly `tokens` (ticker -> address): a token no
    longer in the source is deleted rather than left behind. Runs inside seed_reference_data's
    transaction, so a reader never sees the chain with no tokens.
    """
    db.execute("DELETE FROM supported_tokens WHERE chain_id = ?", (chain_id,))
    db.executemany(
        "INSERT INTO supported_tokens (chain_id, ticker, address) VALUES (?, ?, ?)",
        [(chain_id, ticker.lower(), Web3.to_checksum_address(address)) for ticker, address in tokens.items()],
    )


def seed_reference_data():
    """
    Seeds reference data from seed_data into the SQLite DB, then records the deployed SHFactory
    address per chain from the Forge broadcast files. Re-running `make db` is idempotent without
    deleting the DB first: SEEDS rows are INSERT OR REPLACEd, and each chain's supported tokens
    are replaced outright, so removing a token from seed_data removes it here too.
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

    for chain_id, tokens in SUPPORTED_TOKENS.items():
        _replace_supported_tokens(db, chain_id, tokens)

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

    # Anvil has no real token deployments to hardcode (unlike the chains in seed_data.py):
    # HelperConfig.getOrCreateAnvilConfig() deploys fresh ERC20Mock/MockWeth mocks inside the
    # (broadcast) deploy, at addresses that change every run. Recover ticker->address from the
    # broadcast's decoded constructor arguments (symbol is arg index 1), e.g.
    # `new ERC20Mock("Circle USD","USDC",6)` -> ticker "usdc". This is the only writer of anvil's
    # rows. With no broadcast they are left as they are, like the factory row above.
    anvil_broadcast = f"./broadcast/DeploySHProtocol.s.sol/{CHAIN_ID_ANVIL}/run-latest.json"
    if os.path.exists(anvil_broadcast):
        anvil_tokens = {}
        for item in get_json(anvil_broadcast)["transactions"]:
            if item.get("contractName") in ("ERC20Mock", "MockWeth"):
                args = item.get("arguments") or []
                if len(args) >= 2 and item.get("contractAddress"):
                    anvil_tokens[str(args[1]).strip().strip('"').lower()] = item["contractAddress"]
        _replace_supported_tokens(db, CHAIN_ID_ANVIL, anvil_tokens)

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


# ── Custom tokens ─────────────────────────────────────────────────────────────


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
    db.execute(
        "INSERT INTO custom_tokens (user_id, chain_id, address, ticker, name, decimals, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, chain_id, address, ticker.lower(), name, decimals, int(time.time())),
    )
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


# ── Dashboard tokens ──────────────────────────────────────────────────────────


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


# ── LP tokens ─────────────────────────────────────────────────────────────────


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


# The network one agent turn acts on, as (user_id, chain_name), while acting_network() holds it.
#
# A user has a wallet on several chains but user_network holds ONE name -- whichever chain they last
# deployed on. Every tool finds its chain through get_user_network, so before this the web chat
# answered on that chain whatever network the page was showing: asked "how much BNB do I have?" on
# the BNB Smart Chain page, the agent read the Arbitrum wallet and said the network's coin was ETH,
# and a payment asked for there would have been quoted and sent on Arbitrum.
#
# A ContextVar rather than a user_network write: writing would repoint the Telegram bot too, and two
# tabs chatting on different chains would flip each other's network mid-turn. The value is set by
# the server around one turn and never by the model. LangGraph runs tools in threads that copy the
# caller's context, so every tool of the turn sees it (test_auth checks this through a real graph).
_acting_network: ContextVar[tuple[int, str] | None] = ContextVar("acting_network", default=None)


@contextmanager
def acting_network(user_id: int, chain_name: str, chain_id: int):
    """
    Makes `chain_name` the network get_user_network answers for `user_id` inside the block.

    @param chain_id  The chain the caller believes `chain_name` is, checked against the static
                     chains list so a mismatched pair -- history on one chain, tools on another,
                     the very bug this exists to stop -- fails loudly instead.
    @raises ValueError  If `chain_name` is not a known network for `chain_id`.
    """
    if CHAINS.get(chain_name) != chain_id:
        raise ValueError(f"Network '{chain_name}' is not chain {chain_id}")
    token = _acting_network.set((user_id, chain_name))
    try:
        yield
    finally:
        _acting_network.reset(token)


def get_user_network(user_id: int):
    """
    The network the user's requests act on: the one the current agent turn was started for (see
    acting_network), else the one saved at their last deploy -- which is what the Telegram bot and
    the CLI harness, neither of which names a network per message, keep using.
    """
    acting = _acting_network.get()
    if acting is not None and acting[0] == user_id:
        return acting[1]

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


def create_user(owner_addr: str | None = None) -> int:
    """
    Creates an account and returns its new user ID.

    The web app creates one on an address's first sign-in, so `owner_addr` is always set there;
    only tests make an account without one. The returned id is THE identity for this person
    everywhere else in the app.

    @param owner_addr  The checksummed EOA that signs in to the account and owns its wallets.
    @return            The new user_id.
    @raises ValueError If that address already has an account.
    """
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (owner_addr, created_at) VALUES (?, ?)",
            (owner_addr, int(time.time())),
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


def get_user_by_owner_addr(owner_addr: str) -> dict | None:
    """
    Returns the account that signs in as this EOA, or None.

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


def save_siwe_nonce(nonce: str, ttl_secs: int):
    """
    Records a SIWE nonce as issued, and forgets the ones nobody redeemed in time.

    The nonce endpoint is open to anyone, so without the clean-up every unredeemed request would
    leave a row behind for good.

    @param nonce     The random nonce embedded in the message the user will sign.
    @param ttl_secs  How long a nonce stays redeemable.
    """
    db = get_db()
    now = int(time.time())
    db.execute("DELETE FROM siwe_nonces WHERE issued_at < ?", (now - ttl_secs,))
    db.execute("INSERT OR REPLACE INTO siwe_nonces (nonce, issued_at) VALUES (?, ?)", (nonce, now))
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


def save_contact_nonce(nonce: str, user_id: int, name: str, address: str, ttl_secs: int):
    """
    Records the nonce of one contact's typed data, for that account, name and address only, and
    forgets the ones nobody redeemed in time.

    @param ttl_secs  How long a nonce stays redeemable.
    """
    db = get_db()
    now = int(time.time())
    db.execute("DELETE FROM contact_nonces WHERE issued_at < ?", (now - ttl_secs,))
    db.execute(
        "INSERT INTO contact_nonces (nonce, user_id, name, address, issued_at) VALUES (?, ?, ?, ?, ?)",
        (nonce, user_id, name, address, now),
    )
    db.commit()


def consume_contact_nonce(nonce: str, user_id: int, name: str, address: str, ttl_secs: int) -> bool:
    """
    Redeems a contact nonce, burning it whatever the outcome.

    @return  True if it was outstanding, still fresh, and issued for exactly this account, name and
             address.
    """
    db = get_db()
    row = db.execute(
        "SELECT user_id, name, address, issued_at FROM contact_nonces WHERE nonce = ?", (nonce,)
    ).fetchone()
    db.execute("DELETE FROM contact_nonces WHERE nonce = ?", (nonce,))
    db.commit()
    return (
        row is not None
        and row["user_id"] == user_id
        and row["name"] == name
        and row["address"] == address
        and row["issued_at"] >= int(time.time()) - ttl_secs
    )


# ── Transaction history ───────────────────────────────────────────────────────
# Plain storage. What gets recorded, and when, is tx_history.py's business.

_TRANSACTION_COLUMNS = (
    "id, user_id, chain_id, wallet, source, action, status, tx_hash, user_op_hash, op_nonce, "
    "from_block, created_at, mined_at"
)


def add_transaction(
    user_id: int,
    chain_id: int,
    wallet: str,
    source: str,
    action: str,
    status: str,
    *,
    tx_hash: str | None = None,
    user_op_hash: str | None = None,
    op_nonce: int | None = None,
    from_block: int | None = None,
    mined_at: int | None = None,
) -> int | None:
    """
    Records a transaction for the History tab.

    @param source  'assistant', 'owner' or 'outside'.
    @param status  'pending', 'confirmed', 'failed' or 'dropped'.
    @return        The new row's id, or None if this op or transaction is already recorded.
    """
    db = get_db()
    cur = db.execute(
        "INSERT OR IGNORE INTO transactions (user_id, chain_id, wallet, source, action, status, "
        "tx_hash, user_op_hash, op_nonce, from_block, created_at, mined_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, chain_id, wallet, source, action, status, tx_hash, user_op_hash,
            None if op_nonce is None else str(op_nonce), from_block, int(time.time()), mined_at,
        ),
    )
    row_id = cur.lastrowid if cur.rowcount else None
    if row_id is not None and source != "outside" and tx_hash is not None:
        _drop_outside_twin(db, row_id)
    db.commit()
    return row_id


def settle_transaction(
    tx_id: int, status: str, tx_hash: str | None = None, mined_at: int | None = None
) -> bool:
    """
    Moves a pending transaction to its outcome. A row that is no longer pending is left alone.

    @return  True if the row was pending and is now settled.
    """
    db = get_db()
    cur = db.execute(
        "UPDATE transactions SET status = ?, tx_hash = COALESCE(?, tx_hash), "
        "mined_at = COALESCE(?, mined_at) WHERE id = ? AND status = 'pending'",
        (status, tx_hash, mined_at, tx_id),
    )
    if cur.rowcount and tx_hash is not None:
        _drop_outside_twin(db, tx_id)
    db.commit()
    return cur.rowcount > 0


def _drop_outside_twin(db: sqlite3.Connection, tx_id: int):
    """
    Deletes the outside row for the same wallet and hash as Mitfah row `tx_id`, if there is one.

    An explorer search can run while an assistant send is still pending, before its row knows its
    hash. It then records the send as outside activity. Once the Mitfah row learns the hash, the
    outside copy goes. Leaves the commit to the caller.
    """
    db.execute(
        "DELETE FROM transactions WHERE source = 'outside' AND id != ? AND (chain_id, lower(wallet), tx_hash) = "
        "(SELECT chain_id, lower(wallet), tx_hash FROM transactions WHERE id = ?)",
        (tx_id, tx_id),
    )


def delete_transaction(tx_id: int):
    """Removes a row recorded for something that never reached the chain."""
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    db.commit()


def get_owner_transaction(user_id: int, chain_id: int, tx_hash: str) -> dict | None:
    """The user's row for an owner transaction, by hash, or None."""
    row = (
        get_db()
        .execute(
            f"SELECT {_TRANSACTION_COLUMNS} FROM transactions "
            "WHERE user_id = ? AND chain_id = ? AND tx_hash = ? AND source = 'owner'",
            (user_id, chain_id, tx_hash),
        )
        .fetchone()
    )
    return dict(row) if row else None


def get_transactions(
    user_id: int, chain_id: int | None = None, before: tuple[int, int] | None = None, limit: int = 50
) -> list[dict]:
    """
    The user's transactions, newest first: by when they mined, or by when Mitfah recorded them
    while they haven't. Not by id, since outside rows are recorded long after they mined.

    @param chain_id  Only this chain's, or every chain's when None.
    @param before    Only rows older than this (time, id): the cursor for the next page. See
                     transaction_cursor.
    """
    query = f"SELECT {_TRANSACTION_COLUMNS} FROM transactions WHERE user_id = ?"
    params: list = [user_id]
    if chain_id is not None:
        query += " AND chain_id = ?"
        params.append(chain_id)
    if before is not None:
        query += " AND (COALESCE(mined_at, created_at), id) < (?, ?)"
        params.extend(before)
    query += " ORDER BY COALESCE(mined_at, created_at) DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in get_db().execute(query, params).fetchall()]


def transaction_cursor(row: dict) -> tuple[int, int]:
    """Where a row sits in get_transactions' order: the `before` that starts the page after it."""
    return (row["mined_at"] if row["mined_at"] is not None else row["created_at"], row["id"])


def get_wallet_tx_hashes(chain_id: int, wallet: str) -> set[str]:
    """Every transaction hash already recorded for this wallet, whatever its source."""
    rows = (
        get_db()
        .execute(
            "SELECT tx_hash FROM transactions WHERE chain_id = ? AND lower(wallet) = lower(?) AND tx_hash IS NOT NULL",
            (chain_id, wallet),
        )
        .fetchall()
    )
    return {row["tx_hash"].lower() for row in rows}


def get_first_wallet_tx_hash(chain_id: int, wallet: str) -> str | None:
    """
    The hash of the earliest Mitfah transaction recorded for this wallet: normally the one that
    created it. Where a block-by-block search of its history can start.
    """
    row = (
        get_db()
        .execute(
            "SELECT tx_hash FROM transactions WHERE chain_id = ? AND lower(wallet) = lower(?) "
            "AND source != 'outside' AND tx_hash IS NOT NULL ORDER BY id ASC LIMIT 1",
            (chain_id, wallet),
        )
        .fetchone()
    )
    return row["tx_hash"] if row else None


def get_history_sync(chain_id: int, wallet: str) -> dict | None:
    """
    How far the explorer search has got for a wallet: {"next_block", "synced_at"}, or None if it
    has never run. `next_block` is None until a search has finished a batch.
    """
    row = (
        get_db()
        .execute(
            "SELECT next_block, synced_at FROM history_sync WHERE chain_id = ? AND wallet = ?",
            (chain_id, Web3.to_checksum_address(wallet)),
        )
        .fetchone()
    )
    return dict(row) if row else None


def save_history_sync(chain_id: int, wallet: str, next_block: int | None = None):
    """
    Saves where the next explorer search starts, and that one ran just now. With no `next_block`
    it only marks the run (a search that failed) and keeps the saved start.
    """
    db = get_db()
    db.execute(
        "INSERT INTO history_sync (chain_id, wallet, next_block, synced_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (chain_id, wallet) DO UPDATE SET "
        "next_block = COALESCE(?, next_block), synced_at = excluded.synced_at",
        (chain_id, Web3.to_checksum_address(wallet), next_block, int(time.time()), next_block),
    )
    db.commit()


def get_pending_transactions(user_id: int, limit: int) -> list[dict]:
    """The user's oldest still-pending transactions, up to `limit`."""
    rows = (
        get_db()
        .execute(
            f"SELECT {_TRANSACTION_COLUMNS} FROM transactions "
            "WHERE user_id = ? AND status = 'pending' ORDER BY id ASC LIMIT ?",
            (user_id, limit),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


if __name__ == "__main__":
    init_db()
    seed_reference_data()
