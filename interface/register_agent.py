"""
Registers the agent in the ERC-8004 Identity Registry.

This is a one-time operator script, not a user-facing tool. Run it once after
deployment to mint the agent's on-chain identity NFT. The Sepolia private key
signs the transaction directly — the SessionHandler wallet is not involved.

Usage:
    python interface/register_agent.py <card_uri> [chain_name]
    python interface/register_agent.py --save-id <agent_id> [chain_name]

The --save-id flag skips registration and just writes an existing agentId to the
DB. Use this when the signer is already registered on-chain but the local DB is
empty (e.g. after cloning the repo or switching machines).

Example:
    python interface/register_agent.py https://raw.githubusercontent.com/.../agent_card.json
    python interface/register_agent.py --save-id 42 sepolia-fork
"""

import os
from dotenv import load_dotenv
from db import get_json, save_agent_id, try_get_agent_id
from network_config import load_network_config_by_name
from constants import (
    CHAIN_ID_SEPOLIA, CHAIN_ID_MAINNET, CHAIN_ID_ANVIL,
    IDENTITY_REGISTRY_MAINNET, IDENTITY_REGISTRY_SEPOLIA,
)

load_dotenv()

_IDENTITY_REGISTRY = {
    CHAIN_ID_SEPOLIA: IDENTITY_REGISTRY_SEPOLIA,
    CHAIN_ID_MAINNET: IDENTITY_REGISTRY_MAINNET,
}




def _load_abi(chain_id: int) -> list:
    if chain_id == CHAIN_ID_ANVIL:
        return get_json("./out/AgentIdentityRegistry.sol/AgentIdentityRegistry.json")["abi"]
    return get_json("./interface/artifacts/IdentityRegistry.json")




def register_agent(card_uri: str, chain_name: str = "sepolia-fork") -> dict:
    """
    Registers the deployer's address in the ERC-8004 Identity Registry.

    Signs with SEPOLIA_PRIVATE_KEY (or ANVIL_PRIVATE_KEY for local networks).
    Each address can only register once — returns early if already registered.
    Saves the returned agentId to the DB so tools can look it up without
    needing identityOf(address), which the canonical contract does not expose.

    If the signer is already registered but the DB is empty (e.g. on a fork of
    Sepolia where the registration already exists), use --save-id instead of
    scanning event logs, which requires an archive/paid RPC node.

    @param card_uri    Public URL or IPFS CID pointing to the agent card JSON.
    @param chain_name  Network to register on (default: "sepolia-fork").
    @return            Dict with agent_id, tx_hash, and status on success.
    """
    w3, chain_id = load_network_config_by_name(chain_name)

    key_env = (
        "SEPOLIA_PRIVATE_KEY"
        if "fork"  in chain_name.lower()
        else "ANVIL_PRIVATE_KEY"
    )
    private_key = os.getenv(key_env)
    if not private_key:
        raise EnvironmentError(f"{key_env} is not set in the environment.")

    signer = w3.eth.account.from_key(private_key)

    if "sepolia" in chain_name:
        registry_address = _IDENTITY_REGISTRY[CHAIN_ID_SEPOLIA]
    elif "mainnet" in chain_name:
        registry_address = _IDENTITY_REGISTRY[CHAIN_ID_MAINNET]
    
    abi = _load_abi(chain_id)
    registry = w3.eth.contract(address=registry_address, abi=abi)

    # If already registered on-chain, check DB for the saved agentId.
    if registry.functions.balanceOf(signer.address).call() > 0:
        existing = try_get_agent_id(chain_id)
        if existing is not None:
            print(f"Already registered. agent_id={existing} (from DB)")
            return {"already_registered": True, "agent_id": existing}

        # DB is empty but on-chain balance > 0 — can't scan logs on free RPC.
        raise RuntimeError(
            f"Signer {signer.address} is already registered on chain_id={chain_id} "
            f"but no agentId found in the local DB.\n"
            f"Look up your agentId on Etherscan (filter Transfer events from 0x0 to "
            f"your address on {registry_address}), then run:\n"
            f"  python interface/register_agent.py --save-id <agent_id> {chain_name}"
        )

    # Not yet registered — mint the identity NFT.
    nonce = w3.eth.get_transaction_count(signer.address)
    tx = registry.functions.register(card_uri).build_transaction({
        "from": signer.address,
        "nonce": nonce,
        "chainId": chain_id,
        "gasPrice": w3.eth.gas_price,
    })
    tx["gas"] = w3.eth.estimate_gas(tx)

    signed = w3.eth.account.sign_transaction(tx, signer.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    registered_events = registry.events.Registered().process_receipt(receipt)
    agent_id = int(registered_events[0]["args"]["agentId"])
    save_agent_id(chain_id, agent_id)

    print(f"Registered. agent_id={agent_id}  tx={tx_hash.hex()}  status={receipt['status']}")
    return {
        "registered": True,
        "agent_id": agent_id,
        "tx_hash": tx_hash.hex(),
        "status": receipt["status"],
        "signer": signer.address,
        "registry": registry_address,
    }


if __name__ == "__main__":
    
    chain = "sepolia-fork"
    uri = "https://raw.githubusercontent.com/0xAgentFoundry/agent-cards/main/cards/agent_card.json"
    result = register_agent(uri, chain)

    print(result)
