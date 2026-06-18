# Reference

## Makefile

| Command | Description |
|---|---|
| `make build` | Compile contracts |
| `make test` | Run Forge test suite |
| `make unit-test` | Run unit tests (`test/unit/SHProtocolTest.t.sol`) |
| `make uniswap-test` | Run Uniswap V2 fork tests against `MAINNET_RPC_URL` |
| `make sepolia-test` | Run Sepolia fork tests against `SEPOLIA_RPC_URL` |
| `make snapshot` | Generate gas snapshot |
| `make format` | Format Solidity sources |
| `make clean` | Remove build artifacts |
| `make install` | Install Forge dependencies |
| `make update` | Update Forge dependencies |
| `make anvil` | Start a local Anvil node |
| `make mainnet-fork` | Start a mainnet fork at the latest block |
| `make sepolia-fork` | Start a Sepolia fork at the latest block |
| `make deploy [ARGS="--network sepolia"]` | Deploy `DeploySHProtocol.s.sol` |
| `make vault` | Configure Vault and refresh `.env` credentials |
| `make db` | Initialise SQLite database and run migrations |
| `make deploy-wallet` | Deploy contracts and register session keys (set network in `deploy_wallet.py` `__main__` first) |
| `make agent` | Start the agent in interactive CLI mode |
| `make bot` | Start the Telegram bot |

---

## Project Structure

```
session-key-infra/
├── src/
│   ├── SHFactory.sol
│   ├── SHTreasury.sol
│   ├── SHRegistry.sol
│   ├── SHOracle.sol
│   ├── SHValueInterpreter.sol
│   ├── SessionHandler.sol
│   ├── interfaces/
│   │   ├── IWETH.sol
│   │   ├── IERC20Extended.sol
│   │   ├── IIdentityRegistry.sol
│   │   └── IReputationRegistry.sol
│   └── mocks/
│       ├── MockIdentityRegistry.sol
│       ├── MockReputationRegistry.sol
│       ├── ERC20Mock.sol
│       ├── MockV3Aggregator.sol
│       └── MockWeth.sol
├── script/
│   ├── DeploySHProtocol.s.sol
│   ├── Constants.s.sol
│   ├── HelperConfig.s.sol
│   └── SendPackedUserOp.s.sol
├── test/
│   ├── unit/
│   │   ├── SHProtocolTest.t.sol
│   │   └── SessionHandlerHarness.sol
│   ├── fork/
│   │   ├── SHUniswapV2Test.t.sol
│   │   └── SHSepoliaTest.t.sol
│   └── invariant/
│       ├── InvariantSH.t.sol
│       └── SHHandler.sol
├── app/
│   ├── constants.py
│   ├── db.py
│   ├── network_config.py
│   ├── contracts.py
│   ├── anvil.py
│   ├── live_network.py
│   ├── vault_signer.py
│   ├── deploy_wallet.py
│   ├── tools.py
│   ├── smart_wallet_agent.py
│   ├── telebot.py
│   ├── agent_card.json
│   ├── wallet.db                    ← not committed
│   ├── artifacts/
│   │   ├── IEntryPoint.json
│   │   ├── IReputationRegistry.json
│   │   ├── IERC20Extended.json
│   │   ├── IWETH.json
│   │   ├── IUniswapV2Router02.json
│   │   ├── IUniswapV2Factory.json
│   │   ├── IUniswapV2Pair.json
│   │   ├── ERC20Mock.json
│   │   └── MockV3Aggregator.json
│   └── migrate/
│       ├── Chains.json
│       ├── RPC.json
│       ├── ERC20_Selectors.json
│       ├── UniswapV2_Selectors.json
│       ├── ReputationRegistry_Selectors.json
│       ├── SHFactory.json
│       ├── Mainnet_Tokens.json
│       ├── Mainnet_Pricefeeds.json
│       ├── Sepolia_Tokens.json
│       └── Sepolia_Pricefeeds.json
├── docs/
│   ├── contracts.md
│   ├── app.md
│   ├── vault-security.md
│   ├── setup.md
│   └── reference.md
├── lib/
│   ├── account-abstraction/
│   ├── openzeppelin-contracts/
│   ├── chainlink-brownie-contracts/
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
| `OpenZeppelin Contracts` | `ECDSA`, `Ownable`, `ReentrancyGuard`, `Pausable`, `SafeERC20`, `IERC20Metadata`, `ERC721`, `ERC721URIStorage`, `EIP712` |
| `chainlink-brownie-contracts` | `AggregatorV3Interface` for Chainlink price feeds |
| `Uniswap v2-core / v2-periphery` | `IUniswapV2Router01/02`, `IUniswapV2Factory`, `IUniswapV2Pair` interfaces |
| `forge-std` | Foundry testing and scripting utilities |
| ERC-8004 canonical registries (external) | `IIdentityRegistry`, `IReputationRegistry` — deployed on Sepolia and mainnet; `MockIdentityRegistry` / `MockReputationRegistry` used on Anvil |

### Python

| Package | Purpose |
|---|---|
| `web3` | Ethereum JSON-RPC client |
| `eth-account` | Key management and EIP-191 message signing |
| `hvac` | HashiCorp Vault Python client (Transit encrypt/decrypt) |
| `requests` | HTTP client for bundler JSON-RPC calls |
| `langchain` | Tool definitions and agent framework |
| `langchain-anthropic` | Default Claude LLM integration — swappable for any [LangChain-supported provider](https://python.langchain.com/docs/integrations/chat/) |
| `langgraph` | Stateful agent execution with `AsyncSqliteSaver` checkpointer |
| `python-telegram-bot[job-queue]` | Telegram Bot API client (v20 async) with APScheduler |
| `python-dotenv` | `.env` file loading |

### Infrastructure

| Tool | Purpose |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Runs the HashiCorp Vault container locally |
| [HashiCorp Vault](https://developer.hashicorp.com/vault) | Transit encryption-as-a-service for session key custody |
| [Alchemy](https://www.alchemy.com/) | Bundler-compatible RPC endpoint for live Sepolia/mainnet UserOp submission |
