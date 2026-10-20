"""
ENS names -> addresses, so the owner can add a contact by its name (sam.eth) instead of its 0x address.

Used only when the OWNER adds a contact in the web app. The address found here goes back through
the usual prepare -> sign -> save (routes/contacts.py), so what the owner signs and what is stored is
the address itself, and nothing reads the name again. A name that later changes hands, expires or is
pointed somewhere else therefore never redirects a payment to a saved contact: the owner approved an
address, not a name.

The agent never resolves names. To it, "sam.eth" is only a contact name, and one that isn't saved is
refused like any other (tools.common._resolve_contact) -- otherwise anyone able to type into the chat
could register a name and have it paid.
"""
from ens.exceptions import InvalidName
from ens.utils import normalize_name


from network_config import load_network_config_by_name

# ENS lives on Ethereum mainnet whatever chain the wallet is on (Basenames and other L2 names are
# answered from mainnet too, through their offchain gateways). Read from the live chain even in
# fork mode: a fork's ENS records are as old as the fork, and a contact saved there should point
# where the name really points.
ENS_NETWORK = "mainnet"


def normalize(name: str) -> str:
    """
    `name` as ENS normalises it (ENSIP-15: lowercased, with invisible and right-to-left characters
    refused), the form the owner sees, the lookup uses and the contact is saved under.

    @raises InvalidName  If it isn't a valid ENS name, or has no "." (a bare label names nothing).
    """
    normalized = normalize_name(name.strip())
    if "." not in normalized:
        raise InvalidName(f"'{name}' has no ending such as .eth")
    return normalized


def resolve(name: str) -> str | None:
    """
    The Ethereum address a normalised ENS name points to now, or None if it points nowhere.

    @param name  A name already passed through normalize().
    @return      The checksummed address, or None for an unregistered name or one with no address.
    """
    w3, _ = load_network_config_by_name(ENS_NETWORK)
    return w3.ens.address(name)
