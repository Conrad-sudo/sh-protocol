"""
The contact list: the only destinations the assistant may pay (see tools.common._resolve_contact).

Each contact keeps the owner's signature over it (auth.contact_typed_data): the wallet's PayeeGuard
accepts a payment only to an address the owner signed for, and Mitfah attaches these signatures to the
ops it builds (userop.contact_passes). A contact saved before 2026-11-26 has none and can't be paid
until the owner signs it again.
"""
from db import get_db


def save_contact(user_id: int, name: str, address: str, issued_at: int, signature: str):
    """
    Saves or updates a contact, associating a name with an Ethereum address, with the owner's
    signature over it.

    @param user_id    The application user ID.
    @param name       Human-readable label for the contact. Stored in lowercase, as it was signed.
    @param address    The Ethereum address to associate with the name.
    @param issued_at  The issuedAt the owner signed.
    @param signature  The owner's 65-byte EIP-712 signature, 0x hex.
    """
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO contacts (user_id, name, address, issued_at, signature) VALUES (?, ?, ?, ?, ?)",
        (user_id, name.lower(), address, issued_at, signature),
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
    @return         A list of dicts with 'name', 'address', 'signed' (whether the owner's signature is
                    kept, so the wallet will pay it) and 'issued_at' (None when unsigned).
    """
    rows = (
        get_db()
        .execute(
            "SELECT name, address, issued_at, signature FROM contacts WHERE user_id = ? ORDER BY name ASC",
            (user_id,),
        )
        .fetchall()
    )
    return [
        {
            "name": row["name"],
            "address": row["address"],
            "signed": row["signature"] is not None,
            "issued_at": row["issued_at"],
        }
        for row in rows
    ]


def is_contact_signed(user_id: int, name: str) -> bool:
    """Whether the contact `name` carries the owner's signature, so the wallet will pay it."""
    row = (
        get_db()
        .execute(
            "SELECT signature FROM contacts WHERE user_id = ? AND name = ?",
            (user_id, name.lower()),
        )
        .fetchone()
    )
    return row is not None and row["signature"] is not None


def get_contact_passes(user_id: int) -> list[dict]:
    """
    Every signed contact's pass material: what userop.contact_passes needs to attach a pass.

    @return  A list of dicts with 'name', 'address', 'issued_at' and 'signature', for signed contacts only.
    """
    rows = (
        get_db()
        .execute(
            "SELECT name, address, issued_at, signature FROM contacts "
            "WHERE user_id = ? AND signature IS NOT NULL ORDER BY name ASC",
            (user_id,),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


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
