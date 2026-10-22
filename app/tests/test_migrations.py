"""
Checks the numbered database migrations (db.MIGRATIONS): a new database is built to the latest
version, one from before the numbering is brought up to it without losing what it holds, each
migration runs once, and a database newer than the code is refused. Throwaway files only.

Run: make migrations-test   (or: python app/tests/test_migrations.py)
"""
import os
import sqlite3
import tempfile

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

import db                           # noqa: E402

SEPOLIA = 11155111
_files: list[str] = []


def _use_new_file() -> str:
    """Points db at a new, empty database file, dropping this thread's open connection."""
    if hasattr(db._local, "db"):
        db._local.db.close()
        del db._local.db
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    _files.append(handle.name)
    db.DB_PATH = handle.name
    return handle.name


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_new_database():
    print("\n[1] a new database is built straight to the latest version")
    _use_new_file()
    db.init_db()
    conn = db.get_db()
    check("it reports the latest version", db.schema_version(conn) == len(db.MIGRATIONS),
          f"{db.schema_version(conn)} of {len(db.MIGRATIONS)}")
    check("every table is there",
          {"users", "transactions", "transaction_movements", "supported_tokens", "lp_tokens", "app_feedback",
           "passkeys", "passkey_challenges", "sign_requests"}
          <= _tables(conn), str(sorted(_tables(conn))))
    check("and no table of server-held session keys", not {"session_keys", "pending_session_keys"} & _tables(conn),
          str(sorted(_tables(conn))))
    check("with the newest columns", {"direction", "movements_read"} <= _columns(conn, "transactions")
          and {"telegram_chain_id", "webauthn_handle"} <= _columns(conn, "users"))


def test_database_from_before_the_numbering():
    print("\n[2] a database from before the numbering is brought up to date, its rows kept")
    path = _use_new_file()
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL,
                wallet TEXT NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
                tx_hash TEXT, user_op_hash TEXT, op_nonce TEXT, from_block INTEGER,
                created_at INTEGER NOT NULL, mined_at INTEGER
            );
            INSERT INTO transactions (user_id, chain_id, wallet, source, action, status, created_at)
                VALUES (1, 11155111, '0xW', 'assistant', 'Sent 1 ETH', 'confirmed', 1),
                       (1, 11155111, '0xW', 'outside', 'Received', 'confirmed', 2);
            CREATE TABLE history_sync (
                chain_id INTEGER NOT NULL, wallet TEXT NOT NULL, next_block INTEGER, synced_at INTEGER NOT NULL,
                PRIMARY KEY (chain_id, wallet)
            );
            INSERT INTO history_sync VALUES (11155111, '0xW', 100, 1);
            CREATE TABLE sepolia_tokens (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
            INSERT INTO sepolia_tokens VALUES ('usdc', '0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238');
        """)
    check("it starts at version 0", sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 0)

    db.init_db()
    conn = db.get_db()
    check("it ends at the latest version", db.schema_version(conn) == len(db.MIGRATIONS), str(db.schema_version(conn)))
    check("the History tab's columns were added", {"direction", "movements_read"} <= _columns(conn, "transactions"))
    rows = [r["source"] for r in conn.execute("SELECT source FROM transactions")]
    check("Mitfah's own rows are kept; outside rows go, to be searched for again", rows == ["assistant"], str(rows))
    check("...and so does the explorer search's progress", conn.execute("SELECT COUNT(*) FROM history_sync").fetchone()[0] == 0)
    tokens = conn.execute("SELECT ticker FROM supported_tokens WHERE chain_id = ?", (SEPOLIA,)).fetchall()
    check("the old per-network token table was carried across",
          [t["ticker"] for t in tokens] == ["usdc"] and "sepolia_tokens" not in _tables(conn), str(list(map(tuple, tokens))))


def test_each_migration_runs_once():
    print("\n[3] each migration runs once")
    _use_new_file()
    db.init_db()
    runs = []
    original = db.schema.MIGRATIONS
    db.schema.MIGRATIONS = original + (lambda conn: runs.append(db.schema_version(conn)),)
    try:
        db.init_db()
        db.init_db()
        check("a new migration runs once, on top of the latest version",
              runs == [len(original)] and db.schema_version(db.get_db()) == len(original) + 1, str(runs))
    finally:
        db.schema.MIGRATIONS = original


def test_newer_database_is_refused():
    print("\n[4] a database migrated by newer code is refused, not written to")
    _use_new_file()
    db.get_db().execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    try:
        db.init_db()
        error = None
    except RuntimeError as e:
        error = str(e)
    check("init_db stops with a reason", error is not None and "newer version" in error, str(error))


def test_telegram_chain_column():
    print("\n[5] migration 6 gives accounts a Telegram network, keeping their rows")
    _use_new_file()
    original = db.schema.MIGRATIONS
    db.schema.MIGRATIONS = original[:5]
    try:
        db.init_db()
    finally:
        db.schema.MIGRATIONS = original
    conn = db.get_db()
    user_id = db.create_user("0x00000000000000000000000000000000000000A1")
    db.link_telegram(user_id, 4242)
    check("it starts at version 5 without the column",
          db.schema_version(conn) == 5 and "telegram_chain_id" not in _columns(conn, "users"))

    db.init_db()
    check("it ends at the latest version with the column",
          db.schema_version(conn) == len(db.MIGRATIONS) and "telegram_chain_id" in _columns(conn, "users"))
    check("the account and its Telegram link are kept", db.get_user_id_by_telegram_chat_id(4242) == user_id)
    check("no network is chosen yet", db.get_telegram_chain(user_id) is None)
    db.schema._migrate_telegram_chain(conn)
    check("running it again changes nothing", "telegram_chain_id" in _columns(conn, "users"))

    db.set_telegram_chain(user_id, 8453)
    check("a chosen network is read back", db.get_telegram_chain(user_id) == 8453)
    db.unlink_telegram(user_id)
    check("unlinking Telegram forgets the network too",
          db.get_telegram_chain(user_id) is None and db.get_user_id_by_telegram_chat_id(4242) is None)
    check("an account that doesn't exist has no network", db.get_telegram_chain(user_id + 999) is None)


def test_feedback_table():
    print("\n[6] migration 7 adds the table the bot's /feedback writes to")
    _use_new_file()
    original = db.schema.MIGRATIONS
    db.schema.MIGRATIONS = original[:6]
    try:
        db.init_db()
    finally:
        db.schema.MIGRATIONS = original
    conn = db.get_db()
    user_id = db.create_user("0x00000000000000000000000000000000000000B2")
    check("it starts at version 6 without the table",
          db.schema_version(conn) == 6 and "app_feedback" not in _tables(conn))

    db.init_db()
    check("it ends at the latest version with the table",
          db.schema_version(conn) == len(db.MIGRATIONS) and "app_feedback" in _tables(conn)
          and {"user_id", "chain_id", "source", "text", "created_at"} <= _columns(conn, "app_feedback"))
    check("indexed by account and time", "idx_app_feedback_user_time" in
          {r[1] for r in conn.execute("PRAGMA index_list(app_feedback)")})
    db.add_feedback(user_id, 8453, "telegram", "first")
    db.schema._migrate_app_feedback(conn)
    check("running it again changes nothing, and keeps what's there",
          db.count_feedback_since(user_id, 0) == 1)
    check("only an account's own feedback is counted, and only since when asked",
          db.count_feedback_since(user_id + 1, 0) == 0 and db.count_feedback_since(user_id, 2**40) == 0)


def test_passkeys():
    print("\n[7] migration 8 drops the server-held session keys and adds passkeys")
    _use_new_file()
    original = db.schema.MIGRATIONS
    db.schema.MIGRATIONS = original[:7]
    try:
        db.init_db()
    finally:
        db.schema.MIGRATIONS = original
    conn = db.get_db()
    user_id = db.create_user("0x00000000000000000000000000000000000000C3")
    conn.execute(
        "INSERT INTO session_keys (user_id, chain_id, target, key_address, key_ciphertext) VALUES (?, ?, ?, ?, ?)",
        (user_id, SEPOLIA, "0x" + "11" * 20, "0x" + "22" * 20, "vault:v1:ciphertext"),
    )
    conn.commit()
    check("it starts at version 7 with a Vault ciphertext and no passkey tables",
          db.schema_version(conn) == 7 and "session_keys" in _tables(conn) and "passkeys" not in _tables(conn))

    db.init_db()
    check("it ends at the latest version with the passkey tables",
          db.schema_version(conn) == len(db.MIGRATIONS)
          and {"passkeys", "passkey_challenges", "sign_requests"} <= _tables(conn)
          and "webauthn_handle" in _columns(conn, "users"))
    check("the session-key tables are gone, ciphertexts and all",
          not {"session_keys", "pending_session_keys"} & _tables(conn))
    check("the account is kept", (db.get_user_by_id(user_id) or {}).get("owner_addr", "").lower() == "0x00000000000000000000000000000000000000c3")
    db.schema._migrate_passkeys(conn)
    check("running it again changes nothing", "passkeys" in _tables(conn) and "webauthn_handle" in _columns(conn, "users"))


if __name__ == "__main__":
    try:
        test_new_database()
        test_database_from_before_the_numbering()
        test_each_migration_runs_once()
        test_newer_database_is_refused()
        test_telegram_chain_column()
        test_feedback_table()
        test_passkeys()
    finally:
        if hasattr(db._local, "db"):
            db._local.db.close()
        for name in _files:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(name + suffix):
                    os.unlink(name + suffix)

    finish("All migration checks passed.")
