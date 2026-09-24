# SessionHandler Protocol 🤖⛓️

**Agentic wallet infrastructure.**

SessionHandler Protocol gives every user a programmable smart account and lets an AI agent run it for them across DeFi — swapping, paying, providing liquidity and building on-chain reputation — from plain-language instructions. The user keeps the root key. The agent works through a session key. And the protocol, not the agent, decides what is allowed to happen.

It is built from the standards the ecosystem is converging on:

- **ERC-4337** account abstraction — every agent action is a UserOperation through the canonical EntryPoint
- **ERC-7579** modular accounts — behaviour is added by installing modules, not by rewriting the account
- **ERC-8004** agent identity and reputation — the protocol's agent is a registered on-chain identity that users can review
- **Chainlink** price feeds — every limit and fee is expressed in US dollars, on every chain

The reference application is a **web app** in `web/`: sign in, deploy a wallet with one signature, set its rules, and talk to the assistant on the web or in Telegram.

---

## Demo

[![AI agent powered smart wallet — demo video](https://img.youtube.com/vi/OIOLdvbGoNQ/maxresdefault.jpg)](https://www.youtube.com/watch?v=OIOLdvbGoNQ)

---

## How the protocol is built

The protocol is built in layers: a small on-chain core that enforces the rules, shared contracts that configure it, integrations with DeFi, token and identity protocols, a runtime for the agent, and the applications people actually use.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  APPLICATIONS       web app  ·  Telegram bot  ·  CLI agent                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  SERVICE LAYER      FastAPI  ·  accounts, sign-in (email / Google / SIWE)   │
│                     contacts allowlist  ·  owner actions signed in-browser  │
├─────────────────────────────────────────────────────────────────────────────┤
│  AGENT RUNTIME      LangChain agent (Claude by default, any chat model)     │
│                     the account is injected — the model can't choose it     │
├─────────────────────────────────────────────────────────────────────────────┤
│  INTEGRATIONS       ERC-20 · Uniswap V2 / PancakeSwap V2 · ERC-8004         │
│                     Chainlink pricing · UserOp builder and bundler          │
├─────────────────────────────────────────────────────────────────────────────┤
│  KEY CUSTODY        HashiCorp Vault Transit — session keys encrypted        │
├─────────────────────────────────────────────────────────────────────────────┤
│  CORE (on-chain)    SessionHandler account  +  SpendingLimitModule hook     │
│                     every agent transaction is checked here                 │
├─────────────────────────────────────────────────────────────────────────────┤
│  SHARED CONTRACTS   SHTreasury · SHRegistry · SHOracle · SHFactory          │
├─────────────────────────────────────────────────────────────────────────────┤
│  CHAINS             Ethereum · Arbitrum · BNB Chain · Sepolia · Anvil       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Core — the smart account and its spending-limit hook

**`SessionHandler`** is each user's ERC-7579 smart account, one per user per chain. It checks its own UserOperations and accepts a signature from either the owner or the account's one approved session key. A session key can move value but can never touch the account's own settings, so an agent can't loosen its own rules — and it **expires**, so a key nobody renews stops working on its own rather than living forever.

Every transaction the account executes is wrapped by **`SpendingLimitModule`**, an ERC-7579 hook. It measures how much US-dollar value left the wallet (native coin plus the watched tokens, before vs. after) and refuses anything that would go past the owner's limit for the current window. Because it measures the result rather than reading calldata, it works the same for a transfer, a swap on any venue, or a liquidity move: a fair swap costs almost nothing against the limit, a bad-rate or draining swap costs what was actually lost. It also forbids standing token approvals — an approval must be used up within the same transaction, unless it goes to a spender the owner trusts (like the DEX router).

This hook is what turns "an AI holds a key to my money" into "an AI can spend at most $X per day from my money". Everything above it in the stack can fail — a confused model, a bad prompt, a compromised server — and the most that can leave the wallet is still bounded here. It is a standalone module, so any ERC-7579 account can install it.

Owners also keep full control of the wallet: pause the wallet, switch the agent off, change the limit and window, choose which tokens count, trust spenders, cap network fees, and withdraw.

### Shared contracts — one set per chain

| Contract | Role |
|---|---|
| **`SHTreasury`** | The admin root. Owns the registry, oracle and factory, and collects protocol fees. |
| **`SHRegistry`** | Central settings every wallet reads live: EntryPoint, ERC-8004 registries, oracle, fee, treasury, agent id, and the module new wallets install. A change here reaches every wallet with no redeploy. Swapping the oracle takes a **2-day timelock**. |
| **`SHOracle`** | Turns token amounts into US dollars with Chainlink feeds. Refuses stale prices, and on L2s refuses to price while the sequencer is down. |
| **`SHFactory`** | Deploys wallets with CREATE2, so the address is known before the transaction. One signature creates the wallet, installs the hook with its limit, authorizes the session key, trusts the router and funds it. |

```
SHTreasury  (operator — admin root, fee sink)
   ├── SHRegistry ── live settings for every wallet
   │      └── SHOracle ── Chainlink USD prices (2-day timelocked swap)
   └── SHFactory ── deployWallet(...) in one transaction
            └── SessionHandler  (per user, per chain)
                   ├── validates owner + session-key signatures
                   ├── guards its own admin surface from session keys
                   ├── pays a small flat protocol fee per agent action
                   └── hook ──▶ SpendingLimitModule ──▶ SHOracle
```

**Protocol economics.** Each agent-driven execution pays a flat fee in the chain's native coin: about $0.015 at launch, and always within bounds set at deploy to about $0.005–$0.10. Owner actions are free. See [docs/contracts.md](docs/contracts.md#protocol-fee).

### How one instruction flows through the system

```
"Swap $200 of ETH for USDC"
   │
   ▼  app (web / Telegram / CLI) → API → agent runtime
   │     agent checks remaining budget, gets a quote, runs a preflight
   ▼  integrations build [approve, swap, approve 0] as one batch
   ▼  Vault decrypts the session key just long enough to sign the UserOp
   ▼  ERC-4337 EntryPoint → SessionHandler.validateUserOp
   ▼  SpendingLimitModule.preCheck  → snapshot the wallet's USD value
   ▼  the swap runs on Uniswap / PancakeSwap
   ▼  SpendingLimitModule.postCheck → value lost ≤ remaining limit? else revert
   ▼  receipt back to the user in plain language
```

### What the agent can do

The agent has 60+ tools across four areas:

- **Payments** — native and ERC-20 transfers to contacts the owner has saved on the web (the contact list is the destination allowlist; the agent can read it, never edit it)
- **DeFi trading and liquidity** — quotes, all six V2 swap types, add/remove liquidity, wrapping, with sufficiency and preflight checks before any write
- **Budget awareness** — remaining limit, whether a planned spend fits, live USD prices
- **Identity and reputation (ERC-8004)** — look up agents, read and give feedback, resolve registration files; the protocol's own agent is registered on-chain and user wallets act as its reviewers

The LLM is Anthropic's Claude by default; any [LangChain chat model](https://python.langchain.com/docs/integrations/chat/) can be swapped in with a small edit to `app/smart_wallet_agent.py`.

### Security model in one paragraph

The owner key never leaves the user's browser wallet — deploying and every owner action are signed there, so the service is non-custodial. Session keys live encrypted in Vault. The agent is handed the user's account by the runtime and cannot pick another. And every agent transaction, no matter which layer produced it, passes through the spending-limit hook on-chain. Full analysis: [THREAT_MODEL.md](THREAT_MODEL.md).

---

## Networks

| Network | Chain ID | DEX | Status |
|---|---|---|---|
| Ethereum mainnet | 1 | Uniswap V2 | fork-tested |
| Arbitrum One | 42161 | Uniswap V2 | fork-tested, with L2 sequencer check |
| BNB Chain | 56 | PancakeSwap V2 | fork-tested, live deploy path |
| Sepolia | 11155111 | Uniswap V2 | live testnet + fork |
| Anvil | 31337 | mocks | local |

Celo has partial scaffolding in the Python layer but no contract deployment path yet. New chains are added with the `add-network` skill in `.claude/skills/`.

---

## Prerequisites

- [Foundry](https://book.getfoundry.sh/getting-started/installation)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — for HashiCorp Vault
- Python 3.12+
- Node 22.22+ — only for the web app in `web/`
- An LLM API key — `ANTHROPIC_API_KEY` by default, or any other LangChain chat model
- An Alchemy API key — for live Sepolia and the mainnet / BSC / Arbitrum forks
- *(Optional)* A Telegram bot token from [@BotFather](https://t.me/BotFather) — only for `make bot`

## Clone and install

```bash
git clone https://github.com/Conrad-sudo/sh-protocol.git
cd sh-protocol

# Contracts
forge install

# Python back end and agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Web app (optional)
cd web && npm install
```

## Quick start (local Sepolia fork)

```bash
make sepolia-fork                             # terminal 1: local fork with real Chainlink feeds
make vault                                    # terminal 2: configure Vault (container must be running first)
make setup-agent ARGS="sepolia-fork"          # deploy protocol → db → wallet → CLI agent
```

Starting the Vault container is covered in [docs/vault-security.md](docs/vault-security.md#step-1--start-the-vault-container).

To run the full app instead: `COOKIE_SECURE=0 make api` and `cd web && npm run dev`, then open http://localhost:3000. Every network and step is explained in [docs/setup.md](docs/setup.md).

## Tests

```bash
forge test                  # unit, invariant and fork suites
make py-test                # identity and auth tests
make e2e-test               # full user journey against a running fork (Sepolia; ARGS=arbitrum-fork for Arbitrum)
make agent-smoke            # a real agent conversation against the fork
cd web && npm test && npm run e2e
```

---

## Documentation

| Document | Contents |
|---|---|
| [docs/contracts.md](docs/contracts.md) | Every contract, the spending-limit design, the fee, the test suite |
| [docs/app.md](docs/app.md) | Agent runtime, integrations, bundler, API and Telegram bot |
| [web/README.md](web/README.md) | The web app |
| [docs/vault-security.md](docs/vault-security.md) | Environment variables, Vault setup, session-key security |
| [docs/setup.md](docs/setup.md) | Local, fork and live deployments |
| [docs/langchain-packages-migration.md](docs/langchain-packages-migration.md) | Why ERC-20 / Uniswap calldata comes from external packages |
| [docs/reference.md](docs/reference.md) | Makefile reference, project structure, dependencies |
| [THREAT_MODEL.md](THREAT_MODEL.md) | Assets, trust boundaries, on-chain and off-chain threats |

## License

See [LICENSE](LICENSE).
