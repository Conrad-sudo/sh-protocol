# Reference

## Makefile

| Command | Description |
|---|---|
| `make build` | Compile contracts |
| `make test` | Run Forge test suite |
| `make unit-test` | Run unit tests (`test/unit/SHProtocolTest.t.sol`) |
| `make mainnet-uniswap-test` | Run Uniswap V2 fork tests against `MAINNET_RPC_URL` |
| `make sepolia-uniswap-test` | Run Uniswap V2 fork tests against `SEPOLIA_RPC_URL` (`test/fork/SHSepoliaUniswapV2Test.t.sol`) |
| `make pancakeswap-test` | Run PancakeSwap V2 fork tests against `BSC_RPC_URL` |
| `make arbitrum-uniswap-test` | Run Uniswap V2 fork tests against `ARB_RPC_URL` (`test/fork/SHArbitrumUniswapV2Test.t.sol`) |
| `make sepolia-test` | Alias of `make sepolia-uniswap-test` — the standalone Sepolia suite was folded into the shared fork base (`SHForkTestBase.sol`) |
| `make identity-test` | Check that no agent tool lets the model choose whose wallet it acts on (`app/tests/test_identity.py`, offline) |
| `make auth-test` | API authentication checks against a throwaway database (`app/tests/test_auth.py`, offline) |
| `make py-test` | Both offline Python suites: `identity-test` + `auth-test` |
| `make e2e-test` | The full user journey against a running fork (`app/tests/test_e2e_fork.py`) — Sepolia by default, or `ARGS=arbitrum-fork` etc.; on Arbitrum it also fakes a sequencer outage. Needs `make setup-test ARGS=<that fork>` first |
| `make agent-smoke` | A real agent conversation against the fork, checking it calls the right tools (`app/tests/test_agent_smoke.py`); costs Anthropic credits |
| `make snapshot` | Generate gas snapshot |
| `make clean` | Remove build artifacts |
| `make install` | Install Forge dependencies |
| `make update` | Update Forge dependencies |
| `make anvil` | Start a local Anvil node |
| `make mainnet-fork` | Start an Ethereum mainnet fork at the latest block |
| `make sepolia-fork` | Start a Sepolia fork at the latest block |
| `make bsc-fork` | Start a BSC fork at the latest block |
| `make celo-fork` | Start a Celo fork at the latest block (no Solidity deployment path yet — see [docs/app.md](app.md)) |
| `make arb-fork` | Start an Arbitrum One fork at the latest block (the app calls this network `arbitrum-fork`) |
| `make fund ARGS=<network>` | Set a 100 ETH balance on `SEPOLIA_ACCOUNT` via `anvil_setBalance`. Runs for `*-fork` networks and bare `anvil` and no-ops for everything else, since only a local node implements that cheat RPC. A prerequisite of both `deploy` and `deploy-wallet`, so it rarely needs running by hand — that address is deployer, protocol owner and bundler on a fork, and starts at the forked chain's real balance (zero on mainnet-fork/bsc-fork) |
| `make deploy [ARGS="sepolia-fork"]` | Deploy `DeploySHProtocol.s.sol` — `ARGS` selects the signer/broadcast target (see `docs/setup.md`) |
| `make vault` | Configure Vault and refresh `.env` credentials |
| `make db` | Initialise SQLite database and run migrations |
| `make deploy-wallet [ARGS=<network>]` | Deploy a per-user `SessionHandler` (seeded with its USD spending cap) and register its single session key |
| `make agent` | Start the agent in interactive CLI mode (no Telegram needed) |
| `make api` | Start the FastAPI server on port 8000 — what the web app in `web/` talks to |
| `make bot` | Start the Telegram bot — **optional**; the only target that needs `TELEGRAM_TOKEN` and `python-telegram-bot` |
| `make setup-agent ARGS=<network>` | Runs `deploy` → `fund` → `db` → `deploy-wallet` → `agent` in sequence for `<network>`, stopping on first failure. Assumes Vault is already running and configured (`make vault`) — not part of this chain since it persists across redeploys. Safe for all six networks (live `sepolia`/`bsc` included — `fund` no-ops on those). See `docs/setup.md`'s "Shortcut" callouts |
| `make setup-bot ARGS=<network>` | Same chain, ending in `bot` instead of `agent` — leaves you in the Telegram bot |
| `make setup-test ARGS=<network>` | Same chain with no front end: `deploy` → `fund` → `db` → `deploy-wallet`, for running tests against a freshly deployed stack |

> **There is no Celo fork-test target.** `make ubeswap-test` does not exist in the Makefile, and neither does the `test/fork/SHUbeswapV2Test.t.sol` it used to point at — Celo has no Solidity deployment path (see above), so both were removed rather than left failing. Adding Celo means adding the `HelperConfig`/`Constants` branch, the test file, and the target together.

---

## Project Structure

```
sh-protocol/
├── src/
│   ├── SHFactory.sol
│   ├── SHTreasury.sol
│   ├── SHRegistry.sol
│   ├── SHOracle.sol
│   ├── SessionHandler.sol
│   ├── SpendingLimitModule.sol
│   ├── interfaces/
│   │   ├── IWETH.sol
│   │   ├── IERC20Extended.sol
│   │   ├── AggregatorV3Interface.sol
│   │   ├── IIdentityRegistry.sol
│   │   └── IReputationRegistry.sol
│   └── mocks/
│       ├── MockIdentityRegistry.sol
│       ├── MockReputationRegistry.sol
│       ├── ERC20Mock.sol
│       ├── MockV3Aggregator.sol         ← Chainlink AggregatorV3Interface mock, seeded on Anvil
│       └── MockWeth.sol
├── script/
│   ├── DeploySHProtocol.s.sol
│   ├── Constants.s.sol
│   ├── HelperConfig.s.sol
│   └── SendPackedUserOp.s.sol
├── test/
│   ├── unit/
│   │   ├── SHProtocolTest.t.sol
│   │   ├── SessionGuardTest.t.sol
│   │   └── SpendingLimitModuleHarness.sol
│   ├── fork/
│   │   ├── SHForkTestBase.sol
│   │   ├── SHUniswapV2Test.t.sol
│   │   ├── SHSepoliaUniswapV2Test.t.sol
│   │   └── SHPancakeswapV2Test.t.sol
│   └── invariant/
│       ├── InvariantSH.t.sol
│       └── SHHandler.sol
├── app/
│   ├── constants.py
│   ├── db.py
│   ├── seed_data.py
│   ├── network_config.py
│   ├── contracts.py
│   ├── toolkits.py                  ← per-user_id langchain-erc20 / langchain-uniswap-v2 toolkits
│   ├── userop.py                    ← shared UserOp construction/signing (both backends)
│   ├── anvil.py
│   ├── live_network.py
│   ├── tx_sender.py                 ← nonce-safe EOA broadcast
│   ├── vault_signer.py
│   ├── deploy_wallet.py
│   ├── tools.py
│   ├── agent_context.py             ← (user_id, chain_id) injected into every tool
│   ├── smart_wallet_agent.py
│   ├── auth.py                      ← passwords, JWTs, Google, SIWE
│   ├── api.py                       ← FastAPI HTTP API for web/
│   ├── telebot.py
│   ├── agent_card.json
│   ├── abi.py                       ← IEntryPoint, IERC20Extended, IReputationRegistry, mocks
│   ├── tests/                       ← Python test scripts, run through make (see above)
│   └── wallet.db                    ← not committed
├── docs/
│   ├── contracts.md
│   ├── app.md
│   ├── langchain-packages-migration.md
│   ├── vault-security.md
│   ├── setup.md
│   └── reference.md
├── lib/
│   ├── account-abstraction/
│   ├── openzeppelin-contracts/
│   ├── chainlink-brownie-contracts/  ← AggregatorV3Interface, used by SHOracle
│   ├── forge-std/
│   ├── v2-core/
│   └── v2-periphery/
├── setup_vault.sh
├── Makefile
├── foundry.toml
└── .env                             ← not committed
```

---

## Dependencies

### Solidity

| Library | Purpose |
|---|---|
| `eth-infinitism/account-abstraction` | ERC-4337 `IAccount`, `EntryPoint`, `PackedUserOperation` |
| `OpenZeppelin Contracts` | `AccountERC7579Hooked`, ERC-7579 module interfaces/utils (`draft-`), `ECDSA`, `Ownable`, `ReentrancyGuard`, `Pausable`, `SafeERC20`, `IERC20Metadata`, `ERC721`, `ERC721URIStorage`, `EIP712` |
| `chainlink-brownie-contracts` | `AggregatorV3Interface` for `SHOracle`'s Chainlink price feeds |
| `Uniswap v2-core / v2-periphery` | `IUniswapV2Router01/02`, `IUniswapV2Factory`, `IUniswapV2Pair` interfaces (shared by both Uniswap V2 on mainnet and PancakeSwap V2 on BSC, which expose the same ABI). Solidity side only — the Python app gets its router/pair ABIs from `langchain-uniswap-v2` |
| `forge-std` | Foundry testing and scripting utilities |
| ERC-8004 canonical registries (external) | `IIdentityRegistry`, `IReputationRegistry` — deployed on Sepolia and mainnet; `MockIdentityRegistry` / `MockReputationRegistry` used on Anvil |

> OpenZeppelin's ERC-7579 account/module contracts (`draft-AccountERC7579Hooked`, `draft-IERC7579`, `draft-ERC7579Utils`) are still in `draft-` status upstream — not yet a finalized, audited release. This is a conscious, accepted risk for this project rather than an oversight (see [THREAT_MODEL.md](../THREAT_MODEL.md)).

### Python

| Package | Purpose |
|---|---|
| `web3` | Ethereum JSON-RPC client |
| `eth-account` | Key management and EIP-191 message signing |
| `langchain-erc20` | ERC20 balances, allowances, transfers and native wrap/unwrap, as execution plans. Pinned exactly — pre-1.0 |
| `langchain-uniswap-v2` | Uniswap V2 quotes, liquidity previews and swap/liquidity execution plans, including approval sequencing. Pinned exactly — pre-1.0 |
| `hvac` | HashiCorp Vault Python client (Transit encrypt/decrypt) |
| `requests` | HTTP client for bundler JSON-RPC calls |
| `langchain` | Tool definitions and agent framework |
| `langchain-anthropic` | Default Claude LLM integration — swappable for any [LangChain-supported provider](https://python.langchain.com/docs/integrations/chat/) |
| `langgraph` | Stateful agent execution with `AsyncSqliteSaver` checkpointer |
| `python-telegram-bot[job-queue]` | Telegram Bot API client (v20 async) with APScheduler — only used by `make bot`; not needed for the CLI agent |
| `python-dotenv` | `.env` file loading |

### Infrastructure

| Tool | Purpose |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Runs the HashiCorp Vault container locally |
| [HashiCorp Vault](https://developer.hashicorp.com/vault) | Transit encryption-as-a-service for session key custody |
| [Alchemy](https://www.alchemy.com/) | Bundler-compatible RPC endpoint for live Sepolia/mainnet/BSC UserOp submission and fork RPC access |
| [Chainlink Price Feeds](https://data.chain.link/) | On-chain USD price data read directly by `SHOracle` — no off-chain fetch/push step required |
