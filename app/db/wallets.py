"""
Each user's wallet on each chain, and the session keys the app holds for it.
"""
from db import get_db


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
