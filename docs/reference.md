# Reference

## Makefile

| Command | Description |
|---|---|
| `make build` | Compile contracts |
| `make test` | Run Forge test suite |
| `make unit-test` | Run unit tests (`test/unit/SHProtocolTest.t.sol`) |
| `make mainnet-uniswap-test` | Run Uniswap V3 fork tests against `MAINNET_RPC_URL` (`test/fork/SHMainnetUniswapV3Test.t.sol`) |
| `make sepolia-uniswap-test` | Run Uniswap V3 fork tests against `SEPOLIA_RPC_URL` (`test/fork/SHSepoliaUniswapV3Test.t.sol`) |
| `make pancakeswap-test` | Run PancakeSwap V2 fork tests against `BSC_RPC_URL` |
| `make arbitrum-uniswap-test` | Run Uniswap V3 fork tests against `ARB_RPC_URL` (`test/fork/SHArbitrumUniswapV3Test.t.sol`) |
| `make base-uniswap-test` | Run Uniswap V3 fork tests against `BASE_RPC_URL` (`test/fork/SHBaseUniswapV3Test.t.sol`) |
| `make sepolia-test` | Alias of `make sepolia-uniswap-test` — the standalone Sepolia suite was folded into the shared fork base (`SHForkTestBase.sol`) |
| `make identity-test` | Check that no agent tool lets the model choose whose wallet it acts on (`app/tests/test_identity.py`, offline) |
| `make auth-test` | API authentication checks against a throwaway database (`app/tests/test_auth.py`, offline) |
| `make custom-tokens-test` | Tokens a user adds by address: the add rules, the routes, how the tools price and name them, and that the tools refuse tokens nobody added and slippage over 12%; the dashboard's token list, LP tokens included, against a fake chain (`app/tests/test_custom_tokens.py`, offline) |
| `make history-test` | The History tab and the short chat memory: how transactions are recorded, described, settled and listed, the history and delete-chat routes, and the conversation starting afresh after a send, against a fake chain and a scripted model (`app/tests/test_history.py`, offline) |
| `make movements-test` | What each transaction moved, for the History tab's filters: owner calldata, and the assistant's receipts (token Transfers, the op's executions, wrapped-native events, CELO counted once) (`app/tests/test_tx_movements.py`, offline) |
| `make explorers-test` | Reading activity outside Mitfah from Etherscan, NodeReal and Alchemy: paging, block windows, rate limits and query timeouts retried, and API keys kept out of errors and logs (`app/tests/test_explorers.py`, offline) |
| `make explorers-live` | The same against the real services, read-only, with the keys in `.env` (`app/tests/check_explorers_live.py`; not part of `py-test`) |
| `make speed-test` | What keeps the assistant quick: the chain id asked once, independent reads run together, the wallet checks every transaction tool runs itself, and a UserOp's hash worked out locally (`app/tests/test_speed.py`, offline) |
| `make bundler-test` | What a UserOp pays for posting its data to Ethereum on the live L2s — Base's L1 fee and Arbitrum's L1 gas, as `preVerificationGas` — and that forks pay none; how a lane bundles ops (alone when idle, together under load, one per wallet, the op and gas caps, the lowest fee, one wait per bundle, a failing op refused on its own, a reverted bundle's ops sent again), and how the pool of bundler keys shares ops out (each to the least busy key, 120 at once 40/40/40, a wallet's ops to one key, a stuck key passed over) (`app/tests/test_bundler.py`, offline) |
| `make telebot-test` | The Telegram bot: `/chain` offers only the user's networks the bot can reach (picked for them when there is just one) and refuses a forged or group button press, replies name the network, a "yes" after switching can't confirm the other network's quote, private chats only, one link, a flood past the chat limit makes no model call and gets one notice, replies keep their formatting under the web app's rules and are split to fit, and the daily wallet check reaches the right chats (`app/tests/test_telebot.py`, offline) |
| `make chat-limits-test` | The chat's rationing (`app/chat_limits.py`): the minute and day limits, one message running and one waiting per account, a refused message counting nothing, one notice a minute per kind of refusal, the slot freed after a turn that raises, and the web route on the same `CHAT_RATE_LIMIT` (`app/tests/test_chat_limits.py`, offline, with a fake clock) |
| `make redact-test` | API keys kept out of HTTP errors, the model's tool results and the logs (`app/tests/test_redact.py`, offline) |
| `make migrations-test` | The numbered database migrations: a new database, one from before the numbering, each migration run once, a newer database refused (`app/tests/test_migrations.py`, offline) |
| `make uniswap-v3-test` | The in-repo Uniswap V3 toolkit (`app/langchain_uniswap_v3/`): its tick and liquidity math against Solidity's own values, its addresses against `script/Constants.s.sol`, and every plan it builds (call order, approvals, the deadline multicall, burn at 100%) against a stand-in chain (`app/tests/test_uniswap_v3.py`, offline) |
| `make positions-test` | The Uniswap V3 positions reader the dashboard, the assistant and Telegram share (`app/positions.py`): spam and empty positions hidden, the native coin named as such, base token first, nothing raised when the chain read fails (`app/tests/test_positions.py`, offline) |
| `make position-tools-test` | The assistant's six Uniswap V3 liquidity tools: a range picked by name lands on the ticks that were shown, deposits count against the limit, every refusal holds (unknown token, someone else's position, the wrong network), and each network shows the model only its own liquidity tools (`app/tests/test_position_tools.py`, offline) |
| `make py-test` | All fifteen offline Python suites: `identity-test` + `auth-test` + `custom-tokens-test` + `history-test` + `movements-test` + `explorers-test` + `speed-test` + `bundler-test` + `telebot-test` + `chat-limits-test` + `redact-test` + `migrations-test` + `uniswap-v3-test` + `positions-test` + `position-tools-test` |
| `make load-test` | How many chat turns one API process carries at once: the real API on a scratch database, a stand-in for DeepSeek answering each call in 1.5 s, and chats from many accounts at 8, 32, 64 and 128 at once (`ARGS="16 256"` for others; it raises the API's `CHAT_MAX_CONCURRENT` to the highest). Prints turns a second, turn times and the API's CPU per turn. Leaves out chain reads, which cost far more (50–200 ms a turn, measured on a mainnet fork) (`app/tests/load_test.py`; about 4 minutes, no credits, not part of `py-test`) |
| `make e2e-test` | The full user journey against a running fork (`app/tests/test_e2e_fork.py`) — Sepolia by default, or `ARGS=arbitrum-fork`, `ARGS=base-fork` etc. On the Uniswap V3 forks (Ethereum, Sepolia, Arbitrum, Base) it swaps by every route against ETH/USDC and takes a liquidity position from its range options to its burn; on `bsc-fork`, PancakeSwap V2's swaps and LP tokens. On Arbitrum and Base it also fakes a sequencer outage. Needs `make setup-test ARGS=<that fork>` first |
| `make agent-smoke` | A real agent conversation against the fork, checking it calls the right tools (`app/tests/test_agent_smoke.py`) — on a Uniswap V3 fork that includes adding liquidity: range options first, the user's pick, then one quote; costs DeepSeek credits |
| `make snapshot` | Generate gas snapshot |
| `make clean` | Remove build artifacts |
| `make install` | Install Forge dependencies |
| `make update` | Update Forge dependencies |
| `make anvil` | Start a local Anvil node |
| `make mainnet-fork` | Start an Ethereum mainnet fork at the latest block, on port 8547, and fund both bundlers (see `make fund`) |
| `make sepolia-fork` | Start a Sepolia fork at the latest block, on port 8545, and fund both bundlers |
| `make bsc-fork` | Start a BSC fork at the latest block, on port 8546, and fund both bundlers |
| `make celo-fork` | Start a Celo fork at the latest block, on port 8545, and fund both bundlers (no Solidity deployment path yet — see [docs/app.md](app.md)) |
| `make arb-fork` | Start an Arbitrum One fork at the latest block, on port 8548, and fund both bundlers (the app calls this network `arbitrum-fork`) |
| `make base-fork` | Start a Base fork at the latest block, on port 8549, and fund both bundlers |
| `make fund ARGS=<network>` | Set a 100 ETH balance on every bundler key's address — `API_BUNDLER_1` (= `SEPOLIA_ACCOUNT`), `API_BUNDLER_2`, … and `TELEGRAM_BUNDLER_1`, … — via `anvil_setBalance`. Runs for `*-fork` networks and bare `anvil` and no-ops for everything else, since only a local node implements that cheat RPC. Every fork target runs it as soon as the fork starts, and it is a prerequisite of both `deploy` and `deploy-wallet`, so it rarely needs running by hand — those addresses are the deployer/protocol owner and the two processes' bundlers on a fork, and start at the forked chain's real balance (zero on mainnet-fork/bsc-fork) |
| `make deploy [ARGS="sepolia-fork"]` | Deploy `DeploySHProtocol.s.sol` — `ARGS` selects the signer/broadcast target (see `docs/setup.md`) |
| `make vault` | Configure Vault and refresh `.env` credentials |
| `make db` | Bring the SQLite database up to date (numbered migrations, `db/schema.py`) and seed reference data |
| `make deploy-wallet [ARGS=<network>]` | Deploy a per-user `SessionHandler` (seeded with its USD spending cap) and register its single session key |
| `make agent` | Start the agent in interactive CLI mode (no Telegram needed) |
| `make api` | Start the FastAPI server on port 8000 — what the web app in `web/` talks to |
| `make api-prod` | The API for production, behind Caddy (`deploy/Caddyfile`): no reload, localhost only, trusting the proxy's `X-Forwarded-For` |
| `make bot` | Start the Telegram bot: one bot for every network (`MITFAH_API`), each user picking the network with `/chain`, with read commands (`/balance`, `/limit`, `/history`, `/positions`, `/contacts`, `/web`), `/send`, `/swap`, `/cancel`, `/feedback`, and Confirm/Cancel buttons under a quote — **optional**; the only target that needs that token and `python-telegram-bot`. Needs an up-to-date database (`make db`). Run exactly one copy |
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
│   │   ├── SHUniswapV3ForkTestBase.sol
│   │   ├── SHMainnetUniswapV3Test.t.sol
│   │   ├── SHSepoliaUniswapV3Test.t.sol
│   │   ├── SHArbitrumUniswapV3Test.t.sol
│   │   ├── SHBaseUniswapV3Test.t.sol
│   │   └── SHPancakeswapV2Test.t.sol
│   └── invariant/
│       ├── InvariantSH.t.sol
│       └── SHHandler.sol
├── app/
│   ├── constants.py
│   ├── db/                          ← SQLite data layer, one module per area; numbered migrations in schema.py
│   ├── seed_data.py
│   ├── network_config.py
│   ├── contracts.py
│   ├── toolkits.py                  ← per-user_id toolkits: langchain-erc20, the chain's exchange (V3 or V2), langchain-erc8004
│   ├── langchain_uniswap_v3/        ← in-repo Uniswap V3 toolkit: swaps through SwapRouter02, positions through the position manager
│   ├── positions.py                 ← the wallet's Uniswap V3 positions, read one way for the dashboard, the agent and Telegram
│   ├── userop.py                    ← UserOp calldata, nonce and session-key signing
│   ├── bundler.py                   ← the app's own ERC-4337 bundler, on every network
│   ├── tx_sender.py                 ← nonce-safe EOA broadcast
│   ├── vault_signer.py
│   ├── deploy_wallet.py
│   ├── tools/                       ← the agent's tools, one module per area
│   ├── tx_history.py                ← the History tab's record: every transaction on the wallet, Mitfah's and outside it
│   ├── explorers.py                 ← activity outside Mitfah: Etherscan's API (Ethereum, Sepolia, Arbitrum, Celo), NodeReal (BSC), Alchemy (Base)
│   ├── agent_context.py             ← (user_id, chain_id) injected into every tool
│   ├── smart_wallet_agent.py
│   ├── auth.py                      ← SIWE sign-in, JWTs, signed (EIP-712) contacts
│   ├── ens_names.py                 ← ENS name → address (mainnet), for adding a contact by name
│   ├── api.py                       ← FastAPI app for web/; includes the routers in routes/
│   ├── routes/                      ← the API's routers, one per area
│   ├── redact.py                    ← masks API keys in errors, tool results and logs
│   ├── chat_limits.py               ← the chat's rate limit (web and Telegram) and Telegram's turn gate
│   ├── telebot.py                   ← the Telegram bot: one for every chain, the network picked with /chain
│   ├── telegram_bots.py             ← the bot's token and username, from .env, and the chains it serves
│   ├── telegram_format.py           ← the agent's Markdown as Telegram HTML
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
│   ├── v2-periphery/
│   ├── v3-core/                      ← `0.8` branch
│   ├── v3-periphery/                 ← `0.8` branch
│   └── swap-router-contracts/        ← IV3SwapRouter (SwapRouter02)
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
| `Uniswap v2-core / v2-periphery` | `IUniswapV2Router01` and `IUniswapV2Factory`, for the fork suite's V2 venue hooks, which the PancakeSwap V2 suite on BSC runs (PancakeSwap exposes the same ABI). Solidity side only — the Python app gets its V2 ABIs from `langchain-uniswap-v2` |
| `Uniswap v3-core / v3-periphery / swap-router-contracts` | `IUniswapV3Factory`, `IUniswapV3Pool`, `INonfungiblePositionManager`, `IPeripheryImmutableState`, `IPeripheryPayments`, `IMulticall` and `IV3SwapRouter`, for the Uniswap V3 fork suites (`SHUniswapV3ForkTestBase`). v3-core and v3-periphery are on their `0.8` branches. SwapRouter02 is called through `IV3SwapRouter`, not v3-periphery's `ISwapRouter`: its swap params have no `deadline`, so the selectors differ. Solidity side only — the app has its own V3 ABIs (`app/langchain_uniswap_v3/abis.py`) |
| `forge-std` | Foundry testing and scripting utilities |
| ERC-8004 canonical registries (external) | `IIdentityRegistry`, `IReputationRegistry` — deployed on Sepolia and mainnet; `MockIdentityRegistry` / `MockReputationRegistry` used on Anvil |

> OpenZeppelin's ERC-7579 account/module contracts (`draft-AccountERC7579Hooked`, `draft-IERC7579`, `draft-ERC7579Utils`) are still in `draft-` status upstream — not yet a finalized, audited release. This is a conscious, accepted risk for this project rather than an oversight (see [THREAT_MODEL.md](../THREAT_MODEL.md)).

### Python

| Package | Purpose |
|---|---|
| `web3` | Ethereum JSON-RPC client |
| `eth-account` | Key management and EIP-191 message signing |
| `langchain-erc20` | ERC20 balances, allowances, transfers and native wrap/unwrap, as execution plans. Pinned exactly — pre-1.0 |
| `langchain-uniswap-v2` | V2 quotes, liquidity previews and swap/liquidity execution plans, including approval sequencing — pointed at PancakeSwap's router on BSC, the one chain still on V2. The app also reads its V2 ABIs for the dashboard's old V2 LP tokens and the History tab. Pinned exactly — pre-1.0. (Uniswap V3 on the other chains is the in-repo `app/langchain_uniswap_v3/`, not a dependency) |
| `hvac` | HashiCorp Vault Python client (Transit encrypt/decrypt) |
| `requests` | HTTP client for bundler JSON-RPC calls |
| `langchain` | Tool definitions and agent framework |
| `langchain-deepseek` | Default DeepSeek LLM integration — swappable for any [LangChain-supported provider](https://python.langchain.com/docs/integrations/chat/) |
| `langgraph` | Stateful agent execution with `AsyncSqliteSaver` checkpointer |
| `python-telegram-bot[job-queue]` | Telegram Bot API client (v22 async) with APScheduler — only used by `make bot`; not needed for the CLI agent |
| `markdown-it-py` | Parses the agent's Markdown so the Telegram bot can show it as Telegram formatting (`telegram_format.py`) |
| `python-dotenv` | `.env` file loading |

### Infrastructure

| Tool | Purpose |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Runs the HashiCorp Vault container locally |
| [HashiCorp Vault](https://developer.hashicorp.com/vault) | Transit encryption-as-a-service for session key custody |
| [Alchemy](https://www.alchemy.com/) | RPC endpoint for live Sepolia/mainnet reads and fork RPC access (the app bundles its own UserOps — no bundler service) |
| [Flashbots Protect](https://docs.flashbots.net/flashbots-protect/overview) | Private broadcast of the bundler's live-mainnet transactions, keeping them out of the public mempool |
| [BlockRazor](https://docs.blockrazor.io/transaction-submission/rpc/bsc/bsc-rpc-endpoint) | Private broadcast of the bundler's live-BSC transactions (full-privacy endpoint, no API key) |
| [Chainlink Price Feeds](https://data.chain.link/) | On-chain USD price data read directly by `SHOracle` — no off-chain fetch/push step required |
