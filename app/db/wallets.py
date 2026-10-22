"""
Each user's wallet on each chain.

The session keys that used to live beside them are gone: they are passkeys now, on the user's own
devices (db.passkeys names them; the wallet itself says which are approved, on chain).
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
