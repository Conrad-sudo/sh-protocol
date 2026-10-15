"""
Reference data: what `make db` writes from seed_data.py and the deploy broadcasts.
"""
import os
import sqlite3

from web3 import Web3

from constants import (
    CHAIN_ID_ANVIL,
)
from db import get_db, get_json
from db.tokens import CHAIN_IDs
from seed_data import SEEDS, SUPPORTED_TOKENS


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
