"""
Chains, their RPCs and SHFactory addresses, and which network each user is acting on.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from web3 import Web3

from db import get_db
from seed_data import CHAINS


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
