"""
The contact list: the only destinations the assistant may pay (see tools.common._resolve_contact).
"""
from db import get_db


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
