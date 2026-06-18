# SessionHandler Protocol 🤖⛓️

A full DeFi protocol built on ERC-4337 account abstraction. Users deploy smart wallets via `SHFactory` and delegate control to scoped, time-limited, spending-capped session keys — allowing an AI agent to sign transactions on their behalf through natural language without ever exposing the owner's private key.

The protocol is composed of five layers: Solidity smart contracts, a Python blockchain interface, HashiCorp Vault (key custody), a LangChain AI agent, and a Telegram bot front end. It also integrates the **ERC-8004** canonical on-chain agent identity and reputation registries.

```
┌─────────────────────────────────┐
│         User Interface          │  ← User sends natural language messages
├─────────────────────────────────┤
│       LangChain AI Agent        │  ← Claude LLM reasons and selects tools
├─────────────────────────────────┤
│     Blockchain Interface        │  ← web3.py builds and submits UserOps
├─────────────────────────────────┤
│    ERC-4337 Smart Contracts     │  ← SessionHandler validates and executes
├─────────────────────────────────┤
│  HashiCorp Vault (Docker)       │  ← Transit encrypt/decrypt for session keys
└─────────────────────────────────┘
```

**Network support:** Anvil (local), mainnet fork, Sepolia fork, and live Sepolia testnet.

---

## Protocol Architecture

```
   User (Telegram / CLI)
         │  natural language
         ▼
   LangChain AI Agent
         │  signs UserOps with scoped session keys (via HashiCorp Vault)
         ▼
   ERC-4337 EntryPoint
         │ validateUserOp / execute
         ▼
   SessionHandler  (per-user smart account)
         ├─ reads config at runtime ──▶  SHRegistry
         │                               (fee, treasury, oracle, agentId, router, interpreter)
         ├─ USD computation ──────────▶  SHValueInterpreter → SHOracle (Chainlink)
         ├─ protocol fee ─────────────▶  SHTreasury (owns SHRegistry)
         └─ identity / reputation ────▶  ERC-8004 Registries

   SHFactory  (deploys new SessionHandlers on demand)
```

### Contract Hierarchy

```
SHTreasury  (protocol operator — owns SHRegistry)
    └── SHRegistry  (fee, treasury, oracle, agentId, router, interpreter)
              ├── SHOracle           (Chainlink USD price feeds)
              └── SHValueInterpreter (calldata → USD debit/credit)

SHFactory   (user-facing factory)
    └── SessionHandler  (per-user ERC-4337 smart account)
              reads ──▶ SHRegistry  (at execution time)
              pays  ──▶ SHTreasury  (protocol fee per session-key execution)
```

---

## Prerequisites

- [Foundry](https://book.getfoundry.sh/getting-started/installation)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — required for HashiCorp Vault
- Python 3.12+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An Anthropic API key (or any [LangChain-supported LLM provider](https://python.langchain.com/docs/integrations/chat/))
- An Alchemy API key (required for live Sepolia and mainnet fork)

## Clone and Install

```bash
git clone https://github.com/Conrad-sudo/session-key-infra.git
cd session-key-infra

# Foundry dependencies
forge install

# Python dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Documentation

| Document | Contents |
|---|---|
| [docs/contracts.md](docs/contracts.md) | Smart contract architecture — all contracts, test suite, Foundry commands |
| [docs/app.md](docs/app.md) | Python app layer — LangChain agent, blockchain interface, Telegram bot |
| [docs/vault-security.md](docs/vault-security.md) | Environment variables, Vault/Docker setup, session key security model |
| [docs/setup.md](docs/setup.md) | Local setup (Anvil, mainnet fork, Sepolia fork) and live Sepolia deployment |
| [docs/reference.md](docs/reference.md) | Makefile reference, full project structure, dependencies |
