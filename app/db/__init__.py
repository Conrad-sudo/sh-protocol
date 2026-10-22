"""
The SQLite data layer: every read and write to wallet.db.

This package's own module holds what everything shares -- where the database lives, the per-thread
connection and the cached JSON reads -- and re-exports every function from the modules that hold
the tables, so `from db import get_wallet_address` works wherever it did before the split:

  schema        the tables and their numbered migrations (init_db)
  seed          reference data from seed_data.py and the deploy broadcasts (`make db`)
  chains        chains, RPCs, factory addresses, and the network each user acts on
  wallets       each user's wallet per chain
  passkeys      the account's passkeys, its WebAuthn user handle, registration challenges
  sign_requests quotes offered for a passkey's approval, frozen for whichever process signs them
  tokens        listed, added-by-address, dashboard and LP tokens
  contacts      the contact list, the only destinations the assistant may pay
  users         accounts and their linked Telegram chat
  signin        refresh tokens and the single-use sign-in nonces
  transactions  the History tab's records
  feedback      what users tell the team about the app (the bot's /feedback)
"""
import json
import os
import sqlite3
import threading

from dotenv import load_dotenv

# Where the database lives: APP_DB_PATH, so a server can keep it on a backed-up volume outside the
# checkout, or else app/wallet.db beside this package. Read from .env too: this module is imported
# before the entry points call load_dotenv, and the path is fixed at import.
load_dotenv()
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("APP_DB_PATH") or os.path.join(_APP_DIR, "wallet.db")

_local = threading.local()


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


# get_json's parses: path -> ((modification time, size), parsed value).
_json_cache: dict[str, tuple[tuple[int, int], object]] = {}


def get_json(path: str):
    """
    Reads and parses a JSON file, reusing the last parse until the file changes.

    The contract ABIs under ./out are 115-255 KB each and are read on every wallet request, so
    parsing them every time cost more than anything else in a request but the RPC. A `forge build`
    or a deploy rewrites them, which changes their modification time, and the next call parses the
    new file -- a rebuilt contract is picked up without a restart.

    The parsed value is shared by every caller: read it, never change it.

    @param path  The file path to the JSON file to load.
    @return      The parsed JSON content as a Python dict or list.
    """
    stat = os.stat(path)
    version = (stat.st_mtime_ns, stat.st_size)
    cached = _json_cache.get(path)
    if cached is not None and cached[0] == version:
        return cached[1]
    with open(path, "r") as file:
        value = json.load(file)
    _json_cache[path] = (version, value)
    return value


# The modules below import get_db from this one, so they come after it.
from db.chains import (  # noqa: E402 -- after get_db, which these modules import
    acting_network,
    get_chain_id_from_name,
    get_chain_name_from_id,
    get_factory_address,
    get_rpc_url,
    get_user_network,
    save_user_network,
)
from db.contacts import (  # noqa: E402 -- after get_db, which these modules import
    delete_contact,
    get_all_contacts,
    get_contact,
    save_contact,
)
from db.feedback import (  # noqa: E402 -- after get_db, which these modules import
    add_feedback,
    count_feedback_since,
)
from db.schema import (  # noqa: E402 -- after get_db, which these modules import
    MIGRATIONS,
    init_db,
    schema_version,
)
from db.seed import (  # noqa: E402 -- after get_db, which these modules import
    seed_reference_data,
)
from db.signin import (  # noqa: E402 -- after get_db, which these modules import
    consume_contact_nonce,
    consume_siwe_nonce,
    consume_telegram_link_nonce,
    get_refresh_token,
    purge_expired_refresh_tokens,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    save_contact_nonce,
    save_refresh_token,
    save_siwe_nonce,
    save_telegram_link_nonce,
)
from db.tokens import (  # noqa: E402 -- after get_db, which these modules import
    CHAIN_IDs,
    add_dashboard_tokens,
    delete_custom_token,
    get_custom_token,
    get_custom_tokens,
    get_dashboard_tokens,
    get_lp_tokens,
    get_supported_token_by_address,
    get_supported_tokens,
    get_supported_tokens_by_chain_id,
    get_token_address,
    remove_dashboard_token,
    resolve_token,
    save_custom_token,
    save_lp_token,
    set_lp_token_pair,
)
from db.transactions import (  # noqa: E402 -- after get_db, which these modules import
    TransactionFilter,
    add_transaction,
    delete_transaction,
    get_first_wallet_tx_hash,
    get_history_sync,
    get_movement_tickers,
    get_movements,
    get_owner_transaction,
    get_pending_transactions,
    get_transaction,
    get_transactions,
    get_unread_movement_rows,
    get_wallet_tx_hashes,
    save_history_sync,
    save_movements,
    settle_transaction,
    transaction_cursor,
)
from db.users import (  # noqa: E402 -- after get_db, which these modules import
    create_user,
    get_telegram_chain,
    get_telegram_chats_on_chain,
    get_user_by_id,
    get_user_by_owner_addr,
    get_user_id_by_telegram_chat_id,
    link_telegram,
    set_telegram_chain,
    unlink_telegram,
)
from db.wallets import (  # noqa: E402 -- after get_db, which these modules import
    get_wallet_address,
    get_wallet_chains,
    save_wallet_address,
)
from db.passkeys import (  # noqa: E402 -- after get_db, which these modules import
    consume_passkey_challenge,
    delete_passkey,
    get_or_create_webauthn_handle,
    get_passkey,
    get_passkey_by_credential,
    get_passkeys,
    rename_passkey,
    save_passkey,
    save_passkey_challenge,
    update_passkey_sign_count,
)
from db.sign_requests import (  # noqa: E402 -- after get_db, which these modules import
    cancel_sign_request,
    claim_sign_request,
    end_sign_request,
    get_closed_quote_ids,
    get_open_sign_requests,
    get_sign_request,
    get_sign_request_by_quote,
    get_sign_request_by_sign_id,
    get_sign_request_by_token,
    open_sign_request,
    save_sign_build,
    set_sign_request_message,
    set_sign_request_token,
)
