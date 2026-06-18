# App Layer — Agent, Bot & Blockchain Interface

The `app/` directory bridges the AI agent to the on-chain contracts. It is built on [web3.py](https://web3py.readthedocs.io/) and uses SQLite (`wallet.db`) for persistent off-chain state.

## Directory Layout

```
app/
├── constants.py           ← Chain IDs, addresses, Chainlink heartbeats, ERC-8004 registry addresses
├── db.py                  ← SQLite data layer (all reads/writes to wallet.db)
├── network_config.py      ← Web3 connection factory
├── contracts.py           ← Contract loading with per-chat_id caching
├── anvil.py               ← Session key management and UserOp execution (local/fork)
├── live_network.py        ← UserOp execution via Alchemy bundler (live networks)
├── vault_signer.py        ← HashiCorp Vault Transit encrypt/decrypt wrapper
├── deploy_wallet.py       ← Deployment and session registration scripts
├── tools.py               ← LangChain tool wrappers for the AI agent
├── smart_wallet_agent.py  ← LangChain agent and system prompt
├── telebot.py             ← Telegram bot front end
├── agent_card.json        ← ERC-8004/v1 agent card (hosted publicly, referenced by tokenURI)
├── artifacts/
│   ├── IEntryPoint.json           ← ABI for EntryPoint
│   ├── IReputationRegistry.json   ← ABI for ERC-8004 ReputationRegistry
│   ├── IERC20Extended.json        ← ABI for ERC20 tokens
│   ├── IWETH.json                 ← ABI for WETH
│   ├── IUniswapV2Router02.json    ← ABI for Uniswap V2 Router
│   ├── IUniswapV2Factory.json     ← ABI for Uniswap V2 Factory
│   ├── IUniswapV2Pair.json        ← ABI for Uniswap V2 Pair
│   ├── ERC20Mock.json             ← ABI for ERC20Mock (Anvil)
│   └── MockV3Aggregator.json      ← ABI for MockV3Aggregator (Anvil)
└── migrate/
    ├── Chains.json                ← Chain name → chain ID mapping
    ├── RPC.json                   ← Chain name → RPC URL mapping
    ├── ERC20_Selectors.json       ← ERC20 function name → selector
    ├── UniswapV2_Selectors.json   ← Uniswap V2 function name → selector
    ├── ReputationRegistry_Selectors.json ← ERC-8004 function name → selector
    ├── SHFactory.json             ← SHFactory ABI (for deployWallet())
    ├── Mainnet_Tokens.json        ← Token ticker → mainnet address
    ├── Mainnet_Pricefeeds.json    ← Token → Chainlink feed address (mainnet)
    ├── Sepolia_Tokens.json        ← Token ticker → Sepolia address
    └── Sepolia_Pricefeeds.json    ← Token → Chainlink feed address (Sepolia)
```

## Module Dependency Flow

```
telebot.py ──────────► smart_wallet_agent.py ──► tools.py ──► contracts.py ──► network_config.py ──► db.py
                                                  tools.py ──► anvil.py ─────► vault_signer.py
                                                                          ─────► network_config.py
                                                                          ─────► db.py
                                                  tools.py ──► live_network.py ► vault_signer.py
                                                                                ► network_config.py
                                                                                ► contracts.py
                                                  tools.py ──► db.py
deploy_wallet.py ───────────────────────────────────────────────────────────────────────────────────► db.py
```

The dependency graph is strictly one-directional — no circular imports.

---

## `constants.py`

Centralizes all shared constants imported by `db.py`, `anvil.py`, `live_network.py`, `tools.py`, and `deploy.py`:

```python
CHAIN_ID_ANVIL     = 31337
CHAIN_ID_MAINNET   = 1
CHAIN_ID_SEPOLIA   = 11155111
WEI_PER_ETH        = 10**18
ETH_SENTINEL       = "0x0000000000000000000000000000000000000000"
UNISWAP_V2_ROUTER  = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
ENTRYPOINT_V07     = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
HEARTBEAT_1H       = 3_600
HEARTBEAT_23H      = 82_800
HEARTBEAT_24H      = 86_400

IDENTITY_REGISTRY_MAINNET   = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
IDENTITY_REGISTRY_SEPOLIA   = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
REPUTATION_REGISTRY_MAINNET = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
REPUTATION_REGISTRY_SEPOLIA = "0x8004B663056A597Dffe9eCcC1965A193B7388713"
```

---

## `db.py`

The data persistence layer. All SQLite reads and writes go through this module — no other module accesses the database directly. It has no web3 or blockchain dependency, making it independently testable.

**Connection management:** Each thread gets its own SQLite connection via `threading.local()` to support the Telegram bot's async/multi-threaded environment.

**Network prefix mapping:**

```python
_NETWORK_DB_PREFIX = {
    "anvil": "anvil",
    "mainnet": "mainnet",
    "mainnet-fork": "mainnet",
    "sepolia": "sepolia",
    "sepolia-fork": "sepolia",
}
```

**Schema (`wallet.db`):**

```sql
-- Per-user session key metadata
CREATE TABLE sessions (
    chat_id INTEGER NOT NULL, target TEXT NOT NULL,
    spending_limit REAL NOT NULL, end_time DATE NOT NULL,
    PRIMARY KEY (chat_id, target)
);

-- Per-user encrypted session key storage
CREATE TABLE session_keys (
    chat_id INTEGER NOT NULL, target TEXT NOT NULL,
    key_address TEXT NOT NULL, key_ciphertext TEXT NOT NULL,  -- 'vault:v1:...'
    PRIMARY KEY (chat_id, target)
);

CREATE TABLE contacts (chat_id INTEGER NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL, PRIMARY KEY (chat_id, name));
CREATE TABLE session_handlers (chat_id INTEGER PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE entry_point (chain_id INTEGER PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE chains (name TEXT NOT NULL, chain_id INTEGER NOT NULL, PRIMARY KEY (name, chain_id));
CREATE TABLE rpcs (name TEXT PRIMARY KEY, rpc_url TEXT NOT NULL);
CREATE TABLE user_network (chat_id INTEGER PRIMARY KEY, chain_name TEXT NOT NULL);

CREATE TABLE anvil_tokens   (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE mainnet_tokens (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE sepolia_tokens (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE mainnet_pricefeeds (token TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE sepolia_pricefeeds (token TEXT PRIMARY KEY, address TEXT NOT NULL);

CREATE TABLE erc20_selectors             (name TEXT PRIMARY KEY, selector TEXT NOT NULL);
CREATE TABLE uniswapv2_selectors         (name TEXT PRIMARY KEY, selector TEXT NOT NULL);
CREATE TABLE reputation_registry_selectors (name TEXT PRIMARY KEY, selector TEXT NOT NULL);

CREATE TABLE agent_registries (chain_id INTEGER PRIMARY KEY, identity_registry TEXT NOT NULL, reputation_registry TEXT NOT NULL);
CREATE TABLE agent_ids        (chain_id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL);

CREATE TABLE recurring_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
    token TEXT NOT NULL, recipient TEXT NOT NULL, amount REAL NOT NULL, interval_hrs INTEGER NOT NULL
);
```

**Initialisation:** Run `make db` once to create all tables and seed from the `migrate/` JSON files. Re-running is safe — it uses `INSERT OR REPLACE`.

---

## `vault_signer.py`

Thin wrapper around the [hvac](https://hvac.readthedocs.io/) Vault client. Exposes two functions used by `anvil.py` and `live_network.py`:

```python
def encrypt_key(raw_key: bytes) -> str:
    # Base64-encodes raw_key, sends to Vault Transit, returns 'vault:v1:...' ciphertext

def decrypt_key(ciphertext: str) -> bytes:
    # Sends ciphertext to Vault, returns raw 32-byte key material
```

Authentication uses AppRole (`VAULT_ROLE_ID` + `VAULT_SECRET_ID`). A fresh authenticated client is created per call — tokens expire after 1 hour. The Transit key (`session-keys`) lives inside Vault and is never exported.

---

## `network_config.py`

Resolves Web3 connections from the database.

```python
def load_network_config(chat_id: int) -> tuple[Web3, int, str]
    # Looks up user's current network → returns (Web3, chain_id, chain_name)

def load_network_config_by_name(chain_name: str) -> tuple[Web3, int]
    # Bypasses user lookup — used in deployment scripts before user_network is set
```

---

## `contracts.py`

Contract loading with module-level caching. Every loader checks a per-`chat_id` dict before doing any work. On first call it hits the database and RPC; every subsequent call within the same process returns the cached instance immediately.

```python
def load_session_handler(chat_id: int) -> Contract
def load_entry_point(chat_id: int) -> Contract
def load_ierc20(chat_id: int, token: str) -> Contract       # uses IWETH ABI for "weth"
def load_iuniswap_router(chat_id: int) -> Contract
def load_iuniswap_factory(chat_id: int) -> Contract
def load_iuniswap_pair(chat_id: int, token_a: str, token_b: str) -> Contract
def load_factory(chat_id: int) -> Contract                   # SHFactory — used by deploy.py
def load_reputation_registry(chat_id: int) -> Contract
def load_calldata(instance: Contract, fn_name: str, args: list) -> bytes
def invalidate_cache(chat_id: int) -> None                   # call after a redeploy
```

> The cache is process-scoped. Restart the bot process to invalidate after a contract redeploy.

---

## `anvil.py`

The blockchain execution layer for **local and fork networks** (Anvil, mainnet-fork, sepolia-fork). Handles session key management and the full ERC-4337 UserOp lifecycle by calling `handleOps()` directly — no external bundler required.

### Session Key Management

```python
def get_or_create_session_key(chat_id: int, target_address: str) -> tuple[str, str]:
    # Returns (key_address, vault_ciphertext)
    # On first call: generates secrets.token_bytes(32), encrypts via Vault Transit,
    #                stores ciphertext in session_keys, wipes raw key
    # On subsequent calls: returns stored (address, ciphertext) from DB
```

**Security properties:**
- Database breach alone is useless — ciphertexts require Vault to decrypt
- Vault breach alone is useless — the attacker also needs the DB ciphertexts
- Per-user, per-target key isolation
- Raw key exists in process memory only for the milliseconds between `decrypt_key()` and the `finally` wipe

### ERC-4337 UserOp Flow

`send_user_op_as_session()` orchestrates the complete UserOp lifecycle:

1. ABI-encode `SessionHandler.execute(target, value, data)` as the UserOp `callData`.
2. Fetch nonce from `EntryPoint.getNonce()`.
3. Build a signed dummy op, estimate gas via `eth_estimateGas`, then construct the real op with a 20% buffer and live gas price.
4. Decrypt session key from Vault transiently, sign with EIP-191, wipe.
5. Submit via `EntryPoint.handleOps([userOp], bundler.address)`.
6. On revert: replay via `eth_call` to extract the revert reason.

**Gas constants:**

```python
DUMMY_INNER_GAS            = 500_000
DUMMY_PRE_VERIFICATION_GAS = 50_000
GAS_BUFFER_MULTIPLIER      = 1.2
PRE_VERIFICATION_GAS       = 50_000
```

---

## `live_network.py`

The blockchain execution layer for **live networks** (Sepolia, mainnet). Submits UserOps through an Alchemy bundler via JSON-RPC — no bundler EOA required from the caller.

### Bundler RPC Flow

`send_live_user_op_as_session()`:

1. ABI-encode `SessionHandler.execute(target, value, data)`.
2. Fetch nonce from `EntryPoint.getNonce()`.
3. Submit a signed dummy op to `eth_estimateUserOperationGas` to get per-component gas limits.
4. Construct the final op with a 20% buffer and live gas price.
5. Decrypt session key from Vault transiently, sign, wipe.
6. Submit via `eth_sendUserOperation` to the Alchemy bundler.
7. Poll `eth_getUserOperationReceipt` every 2 seconds (timeout: 600s).

**Gas constants:**

```python
DUMMY_VERIFICATION_GAS       = 150_000
DUMMY_CALL_GAS               = 500_000
DUMMY_PRE_VERIFICATION_GAS   = 50_000
GAS_BUFFER_MULTIPLIER        = 1.2
USER_OP_RECEIPT_TIMEOUT_SECS = 600
USER_OP_POLL_INTERVAL_SECS   = 2
```

> The Alchemy bundler signs and pays for the outer transaction. `SEPOLIA_BUNDLER` in `.env` is not used by `live_network.py`.

### Network Routing in `tools.py`

```python
def send_user_op_as_session(chat_id, key_ciphertext, target, value, data):
    _, _, chain_name = load_network_config(chat_id)
    if "fork" in chain_name.lower() or "anvil" in chain_name.lower():
        return _send_user_op_as_session(...)   # anvil.py — direct handleOps()
    else:
        return _send_live_user_op_as_session(...)  # live_network.py — bundler RPC
```

---

## `deploy_wallet.py`

Deployment and session registration scripts. Run once per fresh environment or network.

**`deploy_session_handler_anvil(chat_id)`** deploys the full stack on Anvil:

1. EntryPoint
2. 18 ERC20Mock tokens + 19 MockV3Aggregator price feeds
3. SHOracle
4. MockIdentityRegistry + MockReputationRegistry
5. SHTreasury (deploys SHRegistry) → SHValueInterpreter → SHFactory
6. `SHFactory.deployWallet()` → user's SessionHandler
7. Mint tokens + send 10 ETH to SessionHandler
8. Persist all addresses to `wallet.db`

**`deploy_session_handler(chat_id, network)`** deploys on a live or fork network (`"mainnet-fork"`, `"sepolia-fork"`, `"sepolia"`). Fork networks use `ANVIL_PRIVATE_KEY`; `"sepolia"` uses `SEPOLIA_PRIVATE_KEY`. Calls `prefund()` after deployment and `_verify()` for non-fork Sepolia (if `ETHERSCAN_API_KEY` is set).

**`add_default_session(chat_id)`** registers default session keys. Sessions vary by network:

**mainnet-fork** (5 sessions):

| Target | Selectors |
|---|---|
| `address(0)` (ETH) | None — value transfers only |
| WETH | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance`, `deposit`, `withdraw` |
| USDC | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance` |
| Uniswap V2 Router | all 6 swap functions + `addLiquidity`, `addLiquidityETH`, `removeLiquidity`, `removeLiquidityETH` |
| Reputation Registry | `giveFeedback` |

**sepolia / sepolia-fork** (4 sessions):

| Target | Selectors |
|---|---|
| `address(0)` (ETH) | None — value transfers only |
| WETH | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance`, `deposit`, `withdraw` |
| LINK | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance` |
| Reputation Registry | `giveFeedback` |

Each session gets a 50-day validity window and a $50,000 spending limit.

---

## `tools.py`

Wraps blockchain operations as LangChain `@tool`-decorated functions. Each tool has a structured docstring the LLM uses to decide when and how to call it.

**`get_tools(job_queue=None)`** is the tool factory. Passing a live `JobQueue` appends the recurring-transfer tools.

All write tools route through the central `send_user_op_as_session()` dispatcher.

### Database tools

| Tool | Description |
|---|---|
| `get_supported_tokens(chat_id)` | Returns supported token tickers for the user's network |
| `get_all_sessions(chat_id)` | Returns active session metadata, pruning expired ones |
| `save_contact(chat_id, name, address)` | Persists a new named contact |
| `get_contact(chat_id, name)` | Resolves a contact name to an Ethereum address |
| `get_all_contacts(chat_id)` | Returns the full contact list |
| `delete_contact(chat_id, name)` | Removes a contact |
| `get_recurring_transfers(chat_id)` | Returns all scheduled recurring transfers |

### Blockchain read tools

| Tool | Description |
|---|---|
| `get_eth_balance(chat_id)` | Wallet ETH balance in whole units |
| `get_erc20_balance(chat_id, token)` | Wallet token balance in whole units |
| `get_erc20_allowance(chat_id, token, spender)` | Approved allowance for a contact |
| `get_session_keys(chat_id, token)` | Returns `(key_address, vault_ciphertext)` — creates a new key if none exists |
| `get_price(chat_id, token)` | Current USD price from SHOracle |
| `get_usd_value(chat_id, token, amount)` | Converts a token amount to USD |
| `preflight_check(chat_id, token, amount, is_uniswap)` | Validates session, budget, and USD value in one call |
| `check_session_validity(chat_id, token)` | Checks if the session key is still active |
| `check_remaining_budget(chat_id, token)` | Remaining USD spending budget |
| `check_spending_within_budget(chat_id, token, amount)` | Validates amount against budget |
| `get_quote_in(chat_id, token_in, token_out, amount_out)` | Cost to acquire exact `amount_out` via `getAmountsIn` |
| `get_quote_out(chat_id, token_in, token_out, amount_in)` | Expected output for exact `amount_in` via `getAmountsOut` |
| `get_pool_quote(chat_id, token_a, token_b, amount_a)` | Proportional `token_b` for a given `amount_a` deposit, from live reserves |
| `get_lp_amounts(chat_id, token_a, token_b, lp_amount)` | Token amounts redeemable by burning `lp_amount` LP tokens |
| `get_liquidity_token_balance(chat_id, token_a, token_b)` | Wallet LP token balance for a pair |

### Balance sufficiency tools

| Tool | Description |
|---|---|
| `is_derived_input_sufficient(chat_id, token_in, token_out, amount_out, slippage_bps)` | Exact-output swaps: derives required input via `getAmountsIn` and checks balance |
| `is_exact_input_sufficient(chat_id, token_in, amount_in)` | Exact-input swaps: compares balance against spend amount |
| `is_liquidity_sufficient(chat_id, token_a, amount_a, token_b)` | `addLiquidity`: derives required `token_b` from reserves and checks both balances |
| `is_liquidity_removal_sufficient(chat_id, token_a, token_b, lp_amount)` | `removeLiquidity`: checks LP token balance |

### Blockchain write tools

| Tool | Description |
|---|---|
| `send_eth(chat_id, session_key_ciphertext, recipient, amount_eth)` | Sends native ETH |
| `transfer_erc20(chat_id, session_key_ciphertext, token, recipient, amount)` | Sends tokens |
| `approve_erc20(chat_id, session_key_ciphertext, token, spender, amount)` | Approves a spender |
| `transferFrom_erc20(chat_id, session_key_ciphertext, token, sender, recipient, amount)` | Transfers from approved sender |
| `wrap_eth(chat_id, session_key_ciphertext, amount_eth)` | Wraps ETH to WETH |
| `swap_ETH_for_exact_tokens(...)` | `swapETHForExactTokens` |
| `swap_exact_tokens_for_ETH(...)` | `swapExactTokensForETH` |
| `swap_tokens_for_exact_ETH(...)` | `swapTokensForExactETH` |
| `swap_exact_ETH_for_tokens(...)` | `swapExactETHForTokens` |
| `swap_exact_tokens_for_tokens(...)` | `swapExactTokensForTokens` |
| `swap_tokens_for_exact_tokens(...)` | `swapTokensForExactTokens` |
| `add_liquidity(...)` | Add liquidity to a Uniswap V2 pool |
| `add_liquidity_eth(...)` | Add liquidity to a token/ETH pool |
| `remove_liquidity(...)` | Remove liquidity; credits budget back |
| `remove_liquidity_eth(...)` | Remove liquidity from a token/ETH pool; credits budget back |

All write tools accept `session_key_ciphertext: str` — the opaque Vault ciphertext from `get_session_keys`. Never decrypted or logged at the tool layer.

### Recurring transfer tools *(requires `job_queue`)*

| Tool | Description |
|---|---|
| `schedule_recurring_transfer(chat_id, token, recipient, amount, interval_hrs)` | Saves to DB and registers a repeating PTB job |
| `cancel_recurring_transfer(chat_id, transfer_id)` | Removes the job and DB record |

### ERC-8004 tools

| Tool | Description |
|---|---|
| `get_agent_identity(chat_id)` | Returns the agent's ERC-8004 `token_id` and `card_uri` |
| `get_agent_reputation(chat_id)` | Returns `average_score` and `feedback_count` from the ReputationRegistry |
| `post_reputation_feedback(chat_id, session_key_ciphertext, score, tags)` | Posts `giveFeedback` via the `reputation_registry` session key |

---

## Section 3 — LangChain Agent

`app/smart_wallet_agent.py` wraps the blockchain tools in a LangChain ReAct agent powered by Anthropic's Claude.

**Default model:** `claude-sonnet-4-6`. Swappable for any [LangChain-supported provider](https://python.langchain.com/docs/integrations/chat/) by changing the `llm` initialisation.

### Architecture

```python
def init_agent(job_queue=None):
    tools = get_tools(job_queue=job_queue)
    agent = create_agent(
        model=llm, tools=tools, system_prompt=SYSTEM_PROMPT, checkpointer=memory
    )
```

Called once at startup — by `main()` for CLI mode (no recurring tools) and by the bot's `post_init` callback with a live `JobQueue`.

`AsyncSqliteSaver` persists the full message history to SQLite, keyed by `thread_id`. Each Telegram user gets an isolated, persistent conversation context that survives bot restarts.

### System Prompt

The `SYSTEM_PROMPT` instructs the agent on:

- **Hard rule:** Never estimate swap quantities from prices — always call `get_quote_in` or `get_quote_out`. Price-based estimates ignore pool depth and fees.
- Multi-step workflows for every operation type (transfers, swaps, liquidity, recurring transfers).
- Safety rules: never invent addresses; always resolve names via `get_contact` first; never expose `session_key_ciphertext` in responses.
- Token validation: always call `get_supported_tokens` before any on-chain action.
- How to extract `chat_id` from the `[chat_id: <number>]` message prefix.

### Per-User Memory

```python
config={"configurable": {"thread_id": str(chat_id)}}
```

### `chat()` Function

```python
def chat(chat_id: int, user_input: str) -> str:
    response = agent.invoke(
        {"messages": [HumanMessage(content=f"[chat_id: {chat_id}] {user_input}")]},
        config={"configurable": {"thread_id": str(chat_id)}},
    )
    return response["messages"][-1].content
```

The `chat_id` is embedded in the `HumanMessage` content because Anthropic's API does not allow multiple non-consecutive system messages.

---

## Section 4 — Telegram Bot

`app/telebot.py` exposes the AI agent as a Telegram bot using [python-telegram-bot v20](https://docs.python-telegram-bot.org/).

### Handlers

| Handler | Trigger | Action |
|---|---|---|
| `/start` | `/start` command | Sends welcome message and schedules daily session expiry check |
| `/help` | `/help` command | Sends help menu |
| `start_chat` | Any text message | Routes to AI agent via `asyncio.to_thread` and replies |

### `post_init` Callback

Runs once before polling starts:

1. Calls `init_agent(job_queue)` to wire the `JobQueue` into the agent.
2. Reads all `recurring_transfers` rows and re-registers each with `job_queue.run_repeating` — restoring scheduled jobs after a restart.

### Session Expiry Alerts

A daily `session_expiry_alert` job is registered per user on `/start`. Every 24 hours it checks all sessions and sends a warning if any expires within the next day.

### Recurring Transfers

After a transfer the agent asks if the user wants it to repeat. If yes, `schedule_recurring_transfer` persists the schedule and registers a `recurring_transfer_job`. If the session key expires, the job cancels itself and alerts the user.

### Async and Thread Safety

`invoke()` is synchronous and is offloaded via `asyncio.to_thread()` to avoid blocking the event loop. SQLite thread safety is handled in `db.py` via `threading.local()`.
