"""
The database's schema and its numbered migrations: init_db brings any wallet.db up to date.
"""
import sqlite3
import time

from constants import (
    CHAIN_ID_ANVIL,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BSC,
    CHAIN_ID_CELO,
    CHAIN_ID_MAINNET,
    CHAIN_ID_SEPOLIA,
)
from db import DB_PATH, get_db

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


def _migrate_transaction_direction(db: sqlite3.Connection):
    """
    Adds `direction` and `movements_read` to a transactions table that predates the History tab's
    filters. Mitfah's own rows get their movements read later, a few per read of the tab
    (tx_history.read_movements). Outside rows are deleted, and every wallet's explorer search
    starts over: they are copies of explorer data, and the search writes them again with their
    movements.

    MUST run after the transactions and history_sync tables exist.
    """
    cols = [r[1] for r in db.execute("PRAGMA table_info(transactions)").fetchall()]
    if "direction" in cols:
        return
    db.execute("ALTER TABLE transactions ADD COLUMN direction TEXT")
    db.execute("ALTER TABLE transactions ADD COLUMN movements_read INTEGER NOT NULL DEFAULT 0")
    outside = db.execute("DELETE FROM transactions WHERE source = 'outside'").rowcount
    db.execute("DELETE FROM history_sync")
    db.commit()
    print(f"Added transaction directions; {outside} outside row(s) will be searched for again.")


def _migrate_telegram_chain(db: sqlite3.Connection):
    """
    Adds `telegram_chain_id` to users: the network the account last chose with /chain in the one
    Telegram bot. NULL until it chooses, and whenever no Telegram chat is linked.

    Kept apart from user_network, which is the chain the account last deployed on: choosing a chain
    in Telegram must not move what the CLI and the web app read from there.
    """
    cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if "telegram_chain_id" in cols:
        return
    db.execute("ALTER TABLE users ADD COLUMN telegram_chain_id INTEGER")
    db.commit()


def _migrate_app_feedback(db: sqlite3.Connection):
    """
    Adds app_feedback: what users tell the team about the app with the Telegram bot's /feedback.
    Indexed by account and time, which is how the bot counts each account's feedback against its
    hourly and daily allowance.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS app_feedback (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            chain_id   INTEGER,
            source     TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_app_feedback_user_time ON app_feedback (user_id, created_at);
    """)
    db.commit()


def _migrate_passkeys(db: sqlite3.Connection):
    """
    Session keys become passkeys (2026-10-20): the app no longer holds any.

    A session key used to be an Ethereum key this server minted, kept encrypted in Vault (its
    ciphertext in session_keys, or pending_session_keys until the owner's grant mined) and signed
    with. It is now a passkey on the user's own device, which signs only after Face ID, a
    fingerprint or a PIN. So both tables are dropped, ciphertexts and all, and the server keeps only
    what is public:

      - passkeys: one row per device the user made a passkey on. key_id is what the wallet files
        the key under (keccak256(abi.encode(qx, qy))), credential_id is how the browser names it,
        public_key the COSE key the approval is verified with, and sign_count the authenticator's
        counter (0 for synced passkeys, which don't keep one). Which wallet a passkey is approved
        on, and until when, lives on chain only (SessionHandler.getSessions).
      - users.webauthn_handle: the account's random 16-byte WebAuthn user handle, made on first use.
        Random rather than the account id, as WebAuthn asks, so a passkey reveals nothing about
        who it belongs to.
      - passkey_challenges: registration challenges, single use and short-lived, kept here rather
        than in memory so a `--reload` between the options and the response loses nothing.
      - sign_requests: a quote offered for a passkey's approval, frozen at offer time. The Telegram
        bot and the API are separate processes: the bot raises the quote, and the API, where the
        approval page posts, signs and sends it. token_hash is the SHA-256 of the Telegram link's
        token (NULL for the web chat, which names the quote under its JWT); `op` and `challenge`
        are the UserOperation built by the latest /api/sign/start and the hash the passkey must
        sign, and sign_id names that build, so an older one can't be finished. `status` is
        'open', 'sending' (claimed by a finish), 'sent', 'failed' or 'cancelled'; one past
        expires_at -- the quote's own expiry -- counts as expired whatever it says.
    """
    columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
    if "webauthn_handle" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN webauthn_handle BLOB")
    db.executescript("""
        DROP TABLE IF EXISTS session_keys;
        DROP TABLE IF EXISTS pending_session_keys;

        CREATE TABLE IF NOT EXISTS passkeys (
            key_id        TEXT PRIMARY KEY,
            user_id       INTEGER NOT NULL,
            credential_id TEXT NOT NULL UNIQUE,
            public_key    BLOB NOT NULL,
            qx            TEXT NOT NULL,
            qy            TEXT NOT NULL,
            sign_count    INTEGER NOT NULL DEFAULT 0,
            device_name   TEXT NOT NULL,
            created_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS passkeys_by_user ON passkeys (user_id, created_at);

        CREATE TABLE IF NOT EXISTS passkey_challenges (
            challenge  TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sign_requests (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id            TEXT NOT NULL,
            user_id             INTEGER NOT NULL,
            chain_id            INTEGER NOT NULL,
            origin              TEXT NOT NULL,
            token_hash          TEXT UNIQUE,
            telegram_chat_id    INTEGER,
            telegram_message_id INTEGER,
            quote               TEXT NOT NULL,
            created_at          INTEGER NOT NULL,
            expires_at          INTEGER NOT NULL,
            status              TEXT NOT NULL,
            sign_id             TEXT UNIQUE,
            op                  TEXT,
            challenge           TEXT,
            built_at            INTEGER,
            transaction_id      INTEGER,
            outcome             TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS sign_requests_by_quote ON sign_requests (user_id, chain_id, quote_id);
        CREATE INDEX IF NOT EXISTS sign_requests_by_status ON sign_requests (user_id, chain_id, status);
    """)
    db.commit()


def _create_schema(db: sqlite3.Connection):
    """
    Every table and index, as they stood when the migrations were numbered (2026-10-08). Frozen
    like any other migration: a later schema change is a new migration, never an edit here, because
    a database that has already run this one never runs it again.
    """
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
        -- mined, and now never will be). `mined_at` is the block's timestamp. `direction` is the
        -- History tab's group: 'in' (the wallet only received), 'out' (something left it) or
        -- 'none' (nothing moved), NULL while unknown. `movements_read` is 1 once its
        -- transaction_movements have been read, even if it moved nothing.
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
            mined_at     INTEGER,
            direction    TEXT,
            movements_read INTEGER NOT NULL DEFAULT 0
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

        -- What each transaction moved into or out of the wallet, for the History tab's filters
        -- (tx_movements.py). `direction` is 'in' or 'out'; `token` is NULL for the native coin;
        -- `amount` is exact, in base units, and `amount_value` the same in whole units, for range
        -- filters; `counterparty` is the other side, NULL for a mint or burn. Deleting a
        -- transaction deletes its movements in code: foreign keys are off on these connections.
        CREATE TABLE IF NOT EXISTS transaction_movements (
            tx_id        INTEGER NOT NULL,
            direction    TEXT NOT NULL,
            token        TEXT,
            ticker       TEXT NOT NULL,
            amount       TEXT NOT NULL,
            amount_value REAL NOT NULL,
            counterparty TEXT
        );
        CREATE INDEX IF NOT EXISTS transaction_movements_by_tx ON transaction_movements (tx_id);

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


# The migrations, in order. Each brings the database up one version, and SQLite's user_version
# records how many have run, so each runs once.
#
# The first five predate the numbering. A database made before it reports version 0 whatever shape
# it is really in, so these five check the shape themselves and do nothing where there is nothing
# to do -- which is also why their order matters only as written here: _migrate_add_chain_id still
# reads the chat_id columns that _migrate_chat_id_to_user_id renames away, and the last two write
# into tables _create_schema makes. A migration added after them can rely on the version instead.
#
# Never edit, remove or reorder a migration once it has run anywhere: add a new one at the end. One
# that fails part-way runs again from its start next time, so make each a single transaction, or
# safe to run twice.
MIGRATIONS = (
    _migrate_add_chain_id,           # 1: one wallet per chain
    _migrate_chat_id_to_user_id,     # 2: accounts instead of Telegram chat ids
    _create_schema,                  # 3: every table and index
    _migrate_token_tables,           # 4: one supported_tokens table for every chain
    _migrate_transaction_direction,  # 5: the History tab's filters
    _migrate_telegram_chain,         # 6: the network chosen in the one Telegram bot
    _migrate_app_feedback,           # 7: the bot's /feedback
    _migrate_passkeys,               # 8: passkeys instead of server-held session keys
)


def schema_version(db: sqlite3.Connection) -> int:
    """How many MIGRATIONS this database has run."""
    return db.execute("PRAGMA user_version").fetchone()[0]


def init_db():
    """
    Brings the database up to date: runs, in order, every migration it hasn't run yet. Safe to call
    on every startup; a database already at the latest version is left alone.

    @raises RuntimeError  If the database has run more migrations than this code knows: newer code
            migrated it, and older code writing to it could corrupt what that code added.
    """
    db = get_db()
    current = schema_version(db)
    if current > len(MIGRATIONS):
        raise RuntimeError(
            f"{DB_PATH} is at schema version {current}, but this code only knows {len(MIGRATIONS)}. "
            "It was migrated by a newer version of Mitfah: run that version, or restore a backup."
        )
    for version, migrate in enumerate(MIGRATIONS[current:], start=current + 1):
        migrate(db)
        # PRAGMA takes no bound parameters; `version` is an int from enumerate.
        db.execute(f"PRAGMA user_version = {version}")
        db.commit()
