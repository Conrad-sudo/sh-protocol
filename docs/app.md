# App Layer — Agent, Bot & Blockchain Interface

The `app/` directory bridges the AI agent to the on-chain contracts. It is built on [web3.py](https://web3py.readthedocs.io/) and uses SQLite (`wallet.db`) for persistent off-chain state.

## Directory Layout

```
app/
├── constants.py           ← Chain IDs, ETH sentinel, native-wrapped ticker map
├── db.py                  ← SQLite data layer (all reads/writes to wallet.db)
├── seed_data.py           ← Static reference data (chains, RPCs, token addresses) seeded by make db
├── network_config.py      ← Web3 connection factory
├── contracts.py           ← Contract loading with per-user_id caching; ERC-7579 calldata/nonce helpers
├── toolkits.py            ← Per-user_id langchain-erc20 / langchain-uniswap-v2 toolkits (cached)
├── userop.py              ← Shared UserOp construction + signing, used by both backends
├── anvil.py               ← UserOp execution (self-bundled: local/fork/sepolia) — single + batch
├── live_network.py        ← UserOp execution via Alchemy bundler — single + batch
├── tx_sender.py           ← Nonce-safe EOA broadcast: locked nonce allocation + fee-bump/timeout
├── vault_signer.py        ← HashiCorp Vault Transit encrypt/decrypt wrapper
├── deploy_wallet.py       ← Per-user wallet deployment + single session-key registration
├── tools.py               ← LangChain tool wrappers for the AI agent
├── smart_wallet_agent.py  ← LangChain agent and system prompt
├── telebot.py             ← Telegram bot front end
├── agent_card.json        ← ERC-8004/v1 agent card (hosted publicly, referenced by tokenURI)
└── abi.py                 ← ABIs for EntryPoint, ERC20, the ERC-8004 registry, and mocks
```

> **ERC20 and Uniswap V2 calldata comes from two external packages.**
> [`langchain-erc20`](https://pypi.org/project/langchain-erc20/) and
> [`langchain-uniswap-v2`](https://pypi.org/project/langchain-uniswap-v2/) build every balance
> read, quote, slippage bound and approval sequence. They return an ordered *execution plan* of
> `(to, value, data)` calls and never sign or submit; `tools._submit_plan` turns a plan into one
> UserOperation. That is why `abi.py` no longer carries the router/factory/pair/WETH ABIs and
> `constants.py` no longer hardcodes factory addresses.

> **Celo support is partial.** `celo_tokens` has a seeded table and the network routing handles `"celo"`/`"celo-fork"`, but there is no Solidity-side deployment path yet, and Celo is intentionally excluded from `deploy_wallet.py`'s default watched-token map (see [docs/contracts.md](contracts.md#helperconfigssol)).

## Module Dependency Flow

```
telebot.py ──────────► smart_wallet_agent.py ──► tools.py ──► contracts.py ──► network_config.py ──► db.py
                                                  tools.py ──► toolkits.py ──► contracts.py
                                                  tools.py ──► anvil.py ─────► userop.py ──► vault_signer.py
                                                  tools.py ──► live_network.py ► userop.py ──► vault_signer.py
                                                  tools.py ──► db.py
deploy_wallet.py ───────────────────────────────────────────────────────────────────────────────────► db.py
```

The dependency graph is one-directional with a single exception: `contracts.invalidate_cache` imports `toolkits.invalidate_toolkits` inside the function body, because `toolkits.py` imports `load_session_handler` from `contracts.py` at module scope. Keeping invalidation behind one entry point is worth the function-local import.

---

## `constants.py`

Centralizes the small set of constants shared across the app layer:

```python
CHAIN_ID_ANVIL      = 31337
CHAIN_ID_MAINNET    = 1
CHAIN_ID_SEPOLIA    = 11155111
CHAIN_ID_BSC        = 56
CHAIN_ID_CELO       = 42220
WEI_PER_ETH         = 10**18
ETH_SENTINEL        = "0x0000000000000000000000000000000000000000"

# ROUTER maps each chain ID to its canonical V2 router; get_router(chain_id) resolves it.
# V2 factory addresses stay unhardcoded: langchain-uniswap-v2 reads router.factory()
# off that router. See toolkits.py.
```

`NATIVE_WRAPPED_TICKER` maps each chain ID to its wrapped-native ticker — `"weth"` on Ethereum/Sepolia/Anvil/Arbitrum (Arbitrum's gas asset is ETH too), `"wbnb"` on BSC, `"celo"` on Celo. `get_native_wrapped_ticker(chain_id)` and `get_native_asset_ticker(chain_id)` resolve these, raising `ValueError` for unconfigured chains.

> The old per-feed `HEARTBEAT_*` constants were removed — heartbeats are a Solidity-side (`Constants.s.sol` / `HelperConfig`) concern; the Python app no longer registers feeds.

---

## `db.py`

The data persistence layer. All SQLite reads and writes go through this module. It has no web3 dependency, making it independently testable. Each thread gets its own connection via `threading.local()`.

**Schema (`wallet.db`):**

**Identity is an application account, not a Telegram chat.** `users.id` is what every per-user table keys on. A Telegram chat is one *optional* way to reach an account, held in `users.telegram_chat_id` and bound through a single-use deep-link nonce — never typed in, because chat ids are enumerable and a form accepting one would let anyone attach their Telegram to someone else's wallet. `users.owner_addr` is the EOA that owns the `SessionHandler` on chain, proved via SIWE; it is the only address permitted to deploy for that account. `auth.verify_siwe` requires a full EIP-4361 message naming this site (`SIWE_DOMAIN`), the issued nonce in its `Nonce:` field, and a signer equal to the address it names. The domain check is the one that matters: the nonce endpoint is open, so without it a phishing page could have a victim sign a message and bind the victim's address to the attacker's account — permanently, since `owner_addr` is UNIQUE.

> Before 2026-09-10 the key was `chat_id` and *was* the Telegram chat id. `db._migrate_chat_id_to_user_id` mints a `users` row per legacy chat id, remaps every table, and rewrites the LangGraph `thread_id`s. It runs from `init_db`, after `_migrate_add_chain_id` — that order matters, since the older migration still reads `chat_id` columns.

**One wallet per chain, per user.** The protocol is deployed on several chains and a user runs a `SessionHandler` on each, reached by a Telegram bot per chain. So `session_handlers` and `session_keys` are both keyed by `(user_id, chain_id, …)`, and `user_network` holds which chain that user is currently pointed at. Deploying on one chain never disturbs another. Two consequences worth knowing:

- **Every contract cache in `contracts.py` is keyed `(user_id, chain_id)`.** Keyed by `user_id` alone, switching a user's network would hand back the previous chain's wallet, EntryPoint and module bound to the new chain's RPC.
- **Each chain gets its own session key**, even when a user's wallet has the *same address* on two chains — which is possible, since an identical protocol deploy can land `SHFactory` at the same address on each and the CREATE2 salt is the same too. Without `chain_id` in the key those wallets would share one row and one key, so a single key compromise would reach every chain.

```sql
-- The account. Every nullable sign-in field can be filled in later: an account may be born from a
-- password, from Google, or from a wallet signature, and gain the others afterwards.
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE, password_hash TEXT,      -- Argon2id
    google_sub TEXT UNIQUE,                     -- Google's stable subject claim, never the email
    owner_addr TEXT UNIQUE,                     -- the EOA that owns the wallet, proved via SIWE
    telegram_chat_id INTEGER UNIQUE,            -- NULL until linked; UNIQUE stops two accounts claiming one chat
    created_at INTEGER NOT NULL
);

-- Single-use, short-TTL nonces. Telegram links are redeemed by the bot's /start; SIWE nonces are
-- burned on verify so a captured signature cannot be replayed.
CREATE TABLE telegram_link_nonces (nonce TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE siwe_nonces (nonce TEXT PRIMARY KEY, issued_at INTEGER NOT NULL);

-- Only the SHA-256 of each refresh token is stored, so a DB read cannot be replayed as a login.
-- Rotation marks the old row revoked rather than deleting it, which is what makes reuse detectable.
CREATE TABLE refresh_tokens (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);

-- Encrypted session-key storage: one key per wallet PER CHAIN (`target` = the wallet address)
CREATE TABLE session_keys (
    user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, target TEXT NOT NULL,
    key_address TEXT NOT NULL, key_ciphertext TEXT NOT NULL,  -- 'vault:v1:...'
    PRIMARY KEY (user_id, chain_id, target)
);

CREATE TABLE contacts (user_id INTEGER NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL, PRIMARY KEY (user_id, name));
CREATE TABLE session_handlers (user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, address TEXT NOT NULL, PRIMARY KEY (user_id, chain_id));
CREATE TABLE factory (chain_id INTEGER PRIMARY KEY, address TEXT NOT NULL);  -- SHFactory address, from the Forge broadcast
CREATE TABLE chains (name TEXT NOT NULL, chain_id INTEGER NOT NULL, PRIMARY KEY (name, chain_id));
CREATE TABLE rpcs (name TEXT PRIMARY KEY, rpc_url TEXT NOT NULL);
CREATE TABLE user_network (user_id INTEGER PRIMARY KEY, chain_name TEXT NOT NULL);

CREATE TABLE anvil_tokens   (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE mainnet_tokens (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE sepolia_tokens (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE bsc_tokens     (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE celo_tokens    (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
CREATE TABLE arbitrum_tokens (ticker TEXT PRIMARY KEY, address TEXT NOT NULL);
```

> **Removed with the design overhaul:** the `sessions`, `erc20_selectors`, `uniswapv2_selectors`, and `reputation_registry_selectors` tables. `init_db()` issues `DROP TABLE IF EXISTS` on all four so `make db` migrates an existing `wallet.db`. Per-target session metadata and on-chain selector allowlists no longer exist — there's one global USD cap and one bare session key, both read on-chain.

**Token seeding.** Mainnet/Sepolia/BSC/Celo/Arbitrum token addresses are static (`seed_data.py`). The Arbitrum set is exactly the tokens `HelperConfig.getArbConfig` prices, and every address matches the corresponding `ARB_*` constant in `script/Constants.s.sol` — an unpriced token would only offer a watched-token choice that makes `deployWallet` revert with `TokenNotPriced`. **Anvil tokens are recovered from the Forge broadcast file** (`broadcast/DeploySHProtocol.s.sol/31337/run-latest.json`): the mocks are deployed at fresh addresses every run, so `seed_reference_data()` reads each `ERC20Mock`/`MockWeth` deployment's decoded constructor arguments (symbol = arg index 1) and maps ticker → address. This is the only writer of `anvil_tokens`.

**Initialisation:** run `make db` once to create tables and seed. Re-running is safe (`INSERT OR REPLACE`, plus the drops above). The `sepolia` RPC row comes from `SEPOLIA_RPC_URL` in `.env`, keeping API-keyed URLs out of source control.

---

## `vault_signer.py`

Thin wrapper around the [hvac](https://hvac.readthedocs.io/) Vault client (unchanged by the overhaul):

```python
def encrypt_key(raw_key: bytes) -> str:   # → 'vault:v1:...' ciphertext
def decrypt_key(ciphertext: str) -> bytes # → raw 32-byte key
```

Authentication uses AppRole (`VAULT_ROLE_ID` + `VAULT_SECRET_ID`); an authenticated client is cached and re-logged in only near token expiry. The Transit key (`session-keys`) lives inside Vault and is never exported. See [docs/vault-security.md](vault-security.md).

---

## `network_config.py`

Resolves Web3 connections from the database.

```python
def load_network_config(user_id: int) -> tuple[Web3, int, str]        # (Web3, chain_id, chain_name)
def load_network_config_by_name(chain_name: str) -> tuple[Web3, int]  # bypasses user lookup (deploy scripts)
```

---

## `contracts.py`

Contract loading with per-`user_id` caching, plus the ERC-7579 calldata/nonce helpers used by `userop.py`, `anvil.py`, and `live_network.py` so the packing logic lives in one place.

```python
def load_session_handler(user_id) -> Contract
def load_spending_limit_module(user_id) -> Contract   # the spending-cap HOOK, read off SessionHandler.SH_MODULE()
def load_entry_point(user_id) -> Contract
def load_ierc20(user_id, token) -> Contract      # ticker -> address + decimals, for the oracle-pricing tools
def load_factory(user_id) -> Contract
def load_calldata(instance, fn_name, args) -> bytes
def invalidate_cache(user_id) -> None

# ERC-7579 mode words + calldata/batch helpers
ERC7579_SINGLE_CALL_MODE = b"\x00" * 32           # CALLTYPE_SINGLE
ERC7579_BATCH_CALL_MODE  = b"\x01" + b"\x00"*31   # CALLTYPE_BATCH

def pack_execution_calldata(target, value, data) -> bytes            # abi.encodePacked(target, value, data)
def encode_batch_execution_calldata(executions) -> bytes            # abi.encode((address,uint256,bytes)[])
def session_key_nonce_key(module_address) -> int                    # uint192(uint160(module)) << 32
```

> **On the nonce key:** the module is a hook, never a validator, so no matter what address the nonce key encodes, the account falls through to its own `_rawSignatureValidation`. `session_key_nonce_key` is kept (any key value validates identically) for continuity with wallets that already submitted ops under it. The batch mode + `encode_batch_execution_calldata` are new — they're how the swap/liquidity tools grant and consume an approval in a single atomic transaction (the module reverts any approval left standing).

---

## `toolkits.py`

Builds and caches the two external toolkits per `user_id`, mirroring `contracts.py`'s caching. A toolkit instance is bound to one RPC, one router and one token map, so it cannot be shared across users.

```python
def get_erc20_tools(user_id)   -> dict[str, BaseTool]   # langchain-erc20
def get_uniswap_tools(user_id) -> dict[str, BaseTool]   # langchain-uniswap-v2
def invalidate_toolkits(user_id) -> None                # called by contracts.invalidate_cache
```

Both are constructed with `tx_mode="calls"`, which emits `plan["calls"]` and makes zero nonce/gas/fee RPC calls — the right mode for a smart account, whose nonce is `EntryPoint.getNonce(sender, key)` rather than an EOA transaction count.

Three configuration choices carry weight:

| Setting | Why |
|---|---|
| `router_address` read from `constants.get_router(chain_id)` | Never the package's own chain registry: it has no Anvil entry and points BSC elsewhere. The router is no longer protocol config (`SHRegistry.router` is gone), so this constant is also what `deploy_wallet` passes as `deployWallet`'s `trustedSpenders` (and what `trust_router` would pass to `addTrustedSpender` when re-granting) — one source of truth, so the router the toolkit builds calldata for is always the one the wallet trusts. |
| `factory_address=None` | The toolkit reads `router.factory()`, so pair lookups cannot drift from the router in use, and chains with no hardcoded factory work. |
| `reset_residual_approvals=True` | Appends `approve(spender, 0)` wherever the router may pull less than approved. Already the default in calls mode; explicit because the module depends on it. |

`_BLOCKED_TOOLS` withholds `approve`, `approve_token` and `revoke_approval` from the returned dicts. They build valid calldata but always revert here (see the approvals note under `tools.py`), so exposing them would only let the agent burn a UserOp on a guaranteed failure.

---

## `tx_sender.py`

Every transaction the app signs with one of *its own* EOAs goes out through here — the outer `handleOps` in `anvil.py`, and `deployWallet` / `addSession` in `deploy_wallet.py`.

Two problems it exists to solve, both invisible on a single-user Anvil run:

- **Nonce races.** `telebot.py` serves each user request on its own thread (`asyncio.to_thread`), but a handful of shared keys sign for everyone — one bundler EOA per chain, one deployer per chain. Reading the nonce per-thread hands the same value to two threads, and the second transaction is dropped or replaces the first. `send_tx()` allocates from a cached `(chain_name, address) → nonce` counter under a process-wide lock spanning allocate → sign → broadcast. The counter is seeded from `pending` (not `latest`, which does not count the mempool) and advanced locally; any failure clears it so the next caller re-seeds, which also self-heals a counter left stale by an out-of-band transaction.
- **Stuck transactions.** A fee cap the base fee has since overtaken will never be mined, so an unbounded `wait_for_transaction_receipt` hangs a user's request permanently. `send_and_confirm()` gives each attempt `ATTEMPT_TIMEOUT_SECS`, then replaces the transaction at its own nonce with both fee fields bumped past the node's price floor, up to `MAX_FEE_BUMPS` times, and raises `TimeoutError` rather than hanging. The replacement cap is floored against the *current* base fee, not just scaled from the stale one. All broadcast hashes are polled, since a replacement races the transaction it replaces and either may win.

Only the outer transaction is ever re-signed; an ERC-4337 UserOp in its calldata is untouched and its session-key signature stays valid, because the EntryPoint prices reimbursement purely from the UserOp's own gas fields.

```python
send_tx(w3, chain_name, account, tx)          # → tx_hash, nonce allocated under the lock
send_and_confirm(w3, chain_name, account, tx) # → receipt, with bounded wait + replace-by-fee
```

## `anvil.py` / `live_network.py`

The blockchain execution layer, split by how the UserOp reaches the EntryPoint. `anvil.py` **self-bundles** — it signs an outer `handleOps()` transaction with a bundler EOA of our own, fronting the gas and naming itself beneficiary so the EntryPoint reimburses it. `live_network.py` hands the op to an **Alchemy bundler** over JSON-RPC and pays no gas directly.

`tools.py` routes on the chain *name*: anything matching `fork`, `anvil` or `sepolia` self-bundles, everything else goes to the bundler RPC. Live **Sepolia is therefore self-bundled**, using `SEPOLIA_PRIVATE_KEY` as the bundler ([`anvil.py`](../app/anvil.py)) — the same key that deploys wallets there.

> **Note.** No chain other than Sepolia currently has a bundler-capable RPC configured (`mainnet` → cloudflare-eth, `bsc` → binance dataseed, `arbitrum` → arb1), so in practice `live_network.py` is unreachable: any chain routed to it fails on the first `eth_sendUserOperation`. Going live elsewhere means either adding a bundler URL for that chain or extending the self-bundling branch.

Both expose a single-call and a batch entry point, sharing one `_submit_user_op` tail:

```python
# anvil.py
send_user_op_as_session(user_id, key_ciphertext, target, value, data)
send_batch_user_op_as_session(user_id, key_ciphertext, executions)     # atomic multi-call

# live_network.py
send_live_user_op_as_session(user_id, key_ciphertext, target, value, data)
send_live_batch_user_op_as_session(user_id, key_ciphertext, executions)
```

**UserOp lifecycle (both backends):**
1. Build `SessionHandler.execute(mode, executionCalldata)` — single-call (`pack_execution_calldata`) or batch (`encode_batch_execution_calldata`).
2. Fetch a nonce keyed via `session_key_nonce_key()` (validated by the account's own `_rawSignatureValidation`).
3. Estimate gas (dummy op → `eth_estimateGas` / `eth_estimateUserOperationGas`), then build the real op with a 20% buffer.
4. **Decrypt the session key from Vault transiently, sign the EIP-191 digest, wipe.**
5. Submit — `EntryPoint.handleOps` (self-bundled: signed by `ANVIL_BUNDLER` on plain anvil, `SEPOLIA_PRIVATE_KEY` on every fork and live Sepolia) or `eth_sendUserOperation` (bundler RPC).
6. Surface inner-call reverts (`eth_call` replay / bundler reason).

### Network routing in `tools.py`

```python
def send_user_op_as_session(user_id, key_ciphertext, target, value, data): ...       # → anvil.py or live_network.py
def send_batch_user_op_as_session(user_id, key_ciphertext, executions): ...          # batch variant
```
`RuntimeError` from either backend is converted to `ToolException` so LangChain surfaces it to the agent cleanly.

---

## `deploy_wallet.py`

Per-user wallet deployment and single session-key registration. It does **not** deploy the shared infrastructure (the Forge script does, via `make deploy` + `make db`).

**Deployment config** (module names in the file):

```python
DEFAULT_DAILY_LIMIT_USD = 50_000 * 10**18   # per-window USD cap (18 decimals)
DEFAULT_WINDOW_SECS     = 86_400            # 24h window
DEFAULT_WATCHED_TICKERS = {                 # tokens metered against the cap, per network (each must be oracle-priced)
    "anvil": ["weth", "usdc", "dai"], "mainnet-fork": ["weth", "usdc", "dai"],
    "sepolia": ["weth", "usdc", "link"], "sepolia-fork": ["weth", "usdc", "link"],
    "bsc": ["wbnb", "usdc", "usdt"], "bsc-fork": ["wbnb", "usdc", "usdt"],
    "arbitrum": ["weth", "usdc", "dai"], "arbitrum-fork": ["weth", "usdc", "dai"],
    # Celo omitted — no Solidity NetworkConfig yet
}
```

**`deploy_wallet(user_id, chain_name)`** calls **`SHFactory.deployWallet(DEFAULT_DAILY_LIMIT_USD, DEFAULT_WINDOW_SECS, watched_tokens, session_key, trusted_spenders)`** — the first three seed the wallet's spending-cap config (`watched_tokens` is `DEFAULT_WATCHED_TICKERS` resolved to addresses); the next two make the wallet usable immediately, replacing what used to be two follow-up owner transactions. It decodes `WalletDeployed`, asserts the address matches the CREATE2 prediction, and persists it.

Order matters here, and CREATE2 is what makes it possible. `predictWalletAddress(deployer)` returns the address this deploy will land on — the factory salts with its own per-owner `deployCount`, so the value advances after each of our deploys and is untouched by anyone else's — then `get_or_create_session_key(user_id, chain_id, predicted)` mints the key **under the address the wallet is about to have** — the same `(user_id, chain_id, wallet_address)` key `tools.get_session_keys` resolves. Without a predictable address the key could not exist before the deploy that takes it as an argument. The mismatch check after the receipt is deliberate: a wrong address would silently orphan the key. On a stale prediction it re-files the session key under the address that actually deployed rather than raising — the on-chain grant is already correct for it, and raising would strand a funded wallet with no `session_handlers` row.

⚠️ **`deployCount` cannot answer "does this user have a wallet?" here.** It is keyed by `msg.sender`, and `_private_key_env` resolves ONE deployer EOA for every user on a chain, so it counts all of them. That check only works when the end user signs their own deploy (the web-app flow). In the bot flow, per-user ownership is the `session_handlers` table's job — `deploy_wallet` warns when it is about to replace an existing row **for this chain**, because that wallet keeps its funds and becomes unreachable from the app. Wallets on other chains are expected and are left alone. `trusted_spenders` is `[constants.get_router(chain_id)]`, or empty on bare Anvil, which has no Uniswap deployment. The wallet is seeded with ETH in the deployment call itself — `deployWallet` is `payable` and forwards its `msg.value` straight to the new clone (`WALLET_PREFUND_ETH_LIVE` on live chains, `WALLET_PREFUND_ETH_LOCAL` on anvil/forks), so no follow-up transfer is needed.

The deployer must already hold gas when this runs. On a fork it inherits the forked chain's real balance — **zero** on `mainnet-fork` and `bsc-fork` — so `make fund` (an `anvil_setBalance` cheat RPC) has to come first. The Makefile makes `fund` a prerequisite of both `deploy` and `deploy-wallet`, so this holds for every target, including a standalone `make deploy-wallet ARGS=<fork>`. Because the same address also bundles (see `anvil.resolve_bundler`), one top-up covers the deployment and every UserOp after it — there is no separate bundler-funding step.

**`add_default_session(user_id)`** registers the wallet's **single** session key. It derives one key via `get_or_create_session_key(user_id, wallet_address)` (Vault-encrypted, keyed to the wallet address), then calls **`SessionHandler.addSession(key)`** as the owner. **No longer part of the deploy path** — `deploy_wallet` seeds the key inside `deployWallet` — it is kept for re-granting a key on a wallet deployed without one, or after `removeSession`. There are no per-target sessions, selectors, expiries, or budgets to configure — the wallet-wide cap and the admin guard replace all of that. (The old `add_session(targets, functions, ...)` and the `approve()` router-pre-approval helper were removed: standing approvals now revert on-chain, so approvals only ever happen atomically inside the swap/liquidity tools.)

**`deploy(user_id, network)`** is the top-level dispatcher (validates the network, then calls `deploy_wallet`) — invoked by `make deploy-wallet` via the `__main__` block, which also seeds a demo contact. `__main__` no longer calls `add_default_session` or `trust_router`: `deployWallet` does both.

**Signing-key resolution** (`LIVE_PRIVATE_KEY_ENV`): live BSC and Celo use their own key (`BSC_PRIVATE_KEY`, `CELO_PRIVATE_KEY`); **every fork plus live Sepolia uses `SEPOLIA_PRIVATE_KEY`**; plain `anvil` falls back to `ANVIL_PRIVATE_KEY`. Forks of a real chain must avoid the well-known Anvil burner key: it is EIP-7702-delegated to drainers on real Sepolia/BSC/mainnet, and a fork inherits that code — which both breaks fork tests and disqualifies it as a `handleOps` beneficiary (the EntryPoint's bare ETH send reverts AA91 against an address with code).

---

## `tools.py`

Wraps blockchain operations as LangChain `@tool`-decorated functions; each docstring tells the LLM when/how to call it. `get_tools()` is the factory.

**The ERC20 and Uniswap tools are wrappers.** Their bodies do three things: resolve tickers and contact names to addresses, invoke the matching `langchain-erc20` / `langchain-uniswap-v2` tool to get an execution plan, and hand that plan to `_submit_plan`. `_submit_plan` sends a one-call plan as an ERC-7579 single execution and anything longer as an atomic batch, then raises `ToolException` on a non-success receipt.

The wrappers exist — rather than exposing the package tools directly — because the package tools take no `user_id` (a toolkit instance is bound to one user's chain), `langchain-uniswap-v2` accepts raw addresses only, and neither package submits anything. Their docstrings describe a *different* function signature (addresses, `from_address`, `nonce`, returns an unsubmitted plan), so they are not interchangeable with the docstrings here, which are the contract the LLM actually sees. See [langchain-packages-migration.md](langchain-packages-migration.md) §3.

**One key, one budget.** `get_session_keys(user_id, <anything>)` always returns the session key for the wallet on the user's CURRENT chain — the argument is kept for tool-API compatibility but selects nothing. Spending is bounded by one wallet-wide USD cap per window, read on-chain.

### Session / budget / pricing tools

| Tool | Description |
|---|---|
| `get_all_sessions(user_id)` | On-chain wallet status: `{session_active, daily_limit_usd, spent_usd, remaining_usd, window_hours, watched_tokens}` (reads `getConfig`/`getRemainingBudget`/`allowedSession`) |
| `get_session_keys(user_id, token)` | Returns `(key_address, vault_ciphertext)` for the wallet's session key |
| `check_session_validity(user_id, token)` | Whether the session key is on the `allowedSession` allowlist |
| `check_remaining_budget(user_id)` | Remaining USD budget this window (no token arg — the cap is global) |
| `check_spending_within_budget(user_id, token, amount)` | Prices `amount` via the oracle and compares to remaining budget |
| `preflight_check(user_id, token, amount)` | Session validity + budget check + USD value in one call (no `is_uniswap` arg) |
| `get_price(user_id, token)` / `get_usd_value(user_id, token, amount)` | Unit price / USD value via `SHOracle.getPrice` |

### Read / quote / sufficiency tools

`get_eth_balance`, `get_erc20_balance`, `get_contact_erc20_balance`, `get_erc20_allowance` (≈always 0 by design), `get_quote_in`, `get_quote_out`, `get_pool_quote`, `get_lp_amounts`, `get_liquidity_token_balance`, `is_derived_input_sufficient`, `is_exact_input_sufficient`, `is_liquidity_sufficient`, `is_liquidity_removal_sufficient`, plus contacts (`get_contact`/`get_all_contacts`) and `get_supported_tokens`, `get_native_asset`.

> **The agent reads the contact list and never writes it.** There is no `save_contact` and no `delete_contact` tool. The contact list is the allowlist of destinations for the wallet's funds — `_resolve_contact` takes a saved name and refuses a raw address — so changing it is an owner action and lives on the API instead: `POST /api/contacts`, `GET /api/contacts`, `DELETE /api/contacts/{name}`, all requiring a signed-in account. Whoever holds a chat surface can pay the people the owner saved and cannot add a new one; the case that motivates it is an unlocked stolen phone. Deleting moved too, even though it only ever shrinks the allowlist and steals nothing — one boundary ("reads, never writes") is easier to hold than a rule with an exception. See THREAT_MODEL §4.2.

> **The quote tools return whole units only.** `get_quote_in` / `get_quote_out` return `{amount_in, amount_out, path}`, and `get_pool_quote` / `get_lp_amounts` return only their whole-unit fields. The old `*_base`, `decimals_a/b`, `liquidity` and `token_*_address` keys are gone: they existed so the swap tools could do their own base-unit and slippage arithmetic, which now happens inside the packages.
>
> **`"eth"` is accepted by the Uniswap-side tools only** — the quote, sufficiency, swap and liquidity tools all route their token arguments through `tools._resolve`, which maps `"eth"` to the chain's wrapped-native address and passes a raw `0x…` through unchanged (that is how LP tokens are named). The six ERC20 tools (`get_erc20_balance`, `get_contact_erc20_balance`, `get_erc20_allowance`, `wrap_eth`, `transfer_erc20`, `transferFrom_erc20`) hand the ticker straight to `langchain-erc20`, which resolves it against the DB token map — and that map has no `"eth"` entry. Use `get_eth_balance` / `send_eth` for the native asset, as before.
>
> `is_derived_input_sufficient` and `is_liquidity_sufficient` keep their `{is_sufficient, derived_input}` / `{is_sufficient, amount_b}` shapes; the wrappers rename the packages' `required_input` / `required_b` / `required_native` fields so the agent-facing contract is unchanged.

### Write tools

| Tool | Description |
|---|---|
| `send_eth(...)` | Sends native ETH/BNB/CELO (metered — native value counts against the cap) |
| `transfer_erc20(...)` | Sends tokens (metered if watched) |
| `transferFrom_erc20(...)` | Transfers from an approved sender |
| `wrap_eth(...)` | Wraps native → WETH/WBNB |
| `swap_*` (all six variants) | Uniswap/PancakeSwap V2 swaps — **approve + swap sent as one atomic batch**. Optional `recipient` delivers the output straight to a saved contact (see below) |
| `add_liquidity(...)` / `add_liquidity_eth(...)` | Add liquidity — approvals batched and residuals zeroed atomically |
| `remove_liquidity(...)` / `remove_liquidity_eth(...)` | Remove liquidity — the pool's **LP token** is approved by address to the trusted router (granted in the `deployWallet` call itself) and consumed in one batch |

> **Swap-and-send in one transaction.** All six `swap_*` tools take an optional `recipient`, passed
> through to the router's own recipient argument, so "swap 1 ETH for USDC and send it to Sandy" is
> one atomic UserOp rather than a swap followed by a `transfer_erc20`. Beyond saving a set of fees,
> it removes a real correctness problem: a swap returns a *minimum* output, not an exact figure, so
> a follow-up transfer has no reliable amount to send.
>
> `recipient` accepts **a saved contact name or `"me"` — never an address.** `tools._resolve_recipient`
> raises `ToolException` on an unknown name before any UserOp is built, so an address injected into
> the conversation can never become a swap destination (THREAT_MODEL §4.2). Omitting it, or passing
> `"me"`, keeps the output in the wallet.

> **All contact resolution goes through `tools._resolve_contact`.** `send_eth`, `transfer_erc20`,
> `transferFrom_erc20` (both sender and recipient), `get_contact_erc20_balance`,
> `get_erc20_allowance` and the swap `recipient` all use it. It exists because `db.get_contact`
> returns `None` for an unknown name rather than raising — passing that `None` onward surfaced as
> an opaque `"must be a string, got NoneType"` from inside the calldata builder, which gives the
> agent nothing to act on. Now the error names the person, the role they were being used as
> (`sender`, `spender`, `swap output recipient`, …) and tells the agent to send the user to the
> web app to add the contact — the agent cannot add one itself, and must not ask for an address
> to use instead. `"me"` resolves to the wallet in every one of them, not just `transferFrom_erc20`.
>
> No new on-chain check was needed: routing the output away means the account's portfolio drops with
> nothing coming back, so the module charges the **full** outgoing value against the cap instead of a
> swap's usual near-zero net.

> **There is no `approve_erc20` tool.** Standing approvals revert on-chain (the module forbids leaving an allowance outstanding), so a standalone approval can never succeed — which is also why `toolkits._BLOCKED_TOOLS` withholds the packages' own `approve` / `approve_token` / `revoke_approval`. The swap and liquidity tools instead receive the approval as `plan["calls"][0]`, already sized to what the router will actually pull, and `_submit_plan` sends the whole plan as one atomic ERC-7579 batch. Exact-output swaps and `addLiquidity` — where the router may pull less than approved — carry a trailing `approve(router, 0)` in the same plan. Each call's `role` (`approve`, `approve_reset`, `swap`, …) records which is which.

### ERC-8004 tools

All 29 tools of [`langchain-erc8004`](https://pypi.org/project/langchain-erc8004/) are wrapped — the package owns the registry ABIs, address resolution, registration-file resolution, the sybil-aware read shapes and all calldata construction. `toolkits.get_erc8004_tools(user_id)` builds the toolkit per user, reading **both registry addresses off the wallet** (`SessionHandler.IDENTITY_REGISTRY()` / `.REPUTATION_REGISTRY()`) rather than from the package's chain table: that is the canonical `0x8004…` pair on Sepolia/BSC but the local mocks on Anvil, and chain 31337 is not in the package's `KNOWN_NETWORKS` at all.

**Who the agent is.** `DeploySHProtocol.s.sol` registers exactly **one** agent per deployment, stores its id in `SHRegistry.agentId`, and leaves the ERC-721 with the deploying operator's key. That agent is the *protocol's* on-chain identity, shared by every wallet on the chain — a user's SessionHandler is a smart account, not an agent, and has no registry entry of its own. This shapes the whole surface: `"protocol"` is the default for every `agent` argument (resolved by `_resolve_agent` via `get_agent_id`, which stays project-side), the user's wallet is always the *reviewer* rather than the subject, and the identity writes are withheld.

| Group | Tools | Exposed to the agent? |
|---|---|---|
| identity reads | `get_registry_info`, `get_agent_identity`, `agent_exists`, `get_agent_owner`, `get_agent_uri`, `get_agent_wallet`, `get_agent_metadata`, `resolve_registration_file`, `verify_agent_endpoint` | yes |
| reputation reads | `get_feedback_clients`, `get_agent_feedback`, `list_all_feedback`, `get_feedback_summary`, `read_feedback`, `get_last_feedback_index`, `get_response_count`, `get_agent_reputation` | yes |
| reputation writes | `post_reputation_feedback`, `give_feedback`, `revoke_feedback`, `append_response` | yes |
| identity writes | `register_agent`, `parse_registration_receipt`, `set_agent_uri`, `set_agent_metadata`, `transfer_agent` | **no** |
| agent wallet | `build_agent_wallet_typed_data`, `set_agent_wallet`, `unset_agent_wallet` | **no** |

21 registered, 60 agent tools in total (was 62 until `save_contact` and `delete_contact` moved to the API — see above). Every write returns a plan and goes out through the same `_submit_plan` as the ERC20/Uniswap tools; all write tools accept `session_key_ciphertext` — the opaque Vault ciphertext; never decrypted or logged at the tool layer. Every `agent` argument also accepts a bare id or a fully-qualified `eip155:chain:registry:id` reference, so third-party agents can be read and rated.

> **The eight identity writes are defined but withheld from `get_tools()`**, on the same principle as `toolkits._BLOCKED_TOOLS`: a tool that cannot succeed is worse than no tool. Changing the protocol agent is governance done with the operator's key, and a user's wallet cannot own an agent of its own either — `register_agent` mints the ERC-721 to the wallet, and `SessionHandler` installs no ERC-7579 fallback handler for `onERC721Received`, so any mint or `safeTransferFrom` to the account reverts with `ERC7579MissingFallbackHandler(0x150b7a02)`. Each wrapper additionally calls `_reject_protocol_agent_write`, which refuses the protocol's own agent before any calldata is built — so a deployment whose wallet *does* own an agent (or has been granted `setApprovalForAll`) can re-enable them by adding them back to the list, without exposing the protocol identity.

> **The Validation Registry's seven tools are absent too.** ERC-8004's validation registry has no canonical deployment on any chain and this protocol deploys none, so `get_erc8004_tools` passes `validation_registry=None` and the toolkit withholds them.

> **What the port fixed.** `get_agent_reputation` used to call `SessionHandler.getAgentReputation()`, which hardcodes `clients[0] = address(this)` and therefore only ever read back feedback *the wallet itself had given* — a real bug for "how is this service rated". It now aggregates over named reviewers, or over every discovered reviewer flagged as unfiltered. `post_reputation_feedback` passed the user's free-text label into **tag1**, the slot that names the *scale*, leaving every rating invisible to a reader filtering on `"starred"`; it now writes `tag1="starred"` with the label in `tag2`, and takes an optional `agent` so other agents can be rated. Note the `langchain-erc8004.md` spec's claim that the old tool was self-feedback does **not** apply here: `isAuthorizedOrOwner(userWallet, agentId)` is false, so a user rating the protocol's agent is a genuine attributed review, and that stayed the default.

---

## Section 3 — LangChain Agent

`app/smart_wallet_agent.py` wraps the tools in a LangChain agent powered by Claude (`claude-sonnet-4-6` by default).

> **The Anthropic LLM is optional — any LangChain chat model works.** The provider is set in one place: the `llm = ChatAnthropic(...)` call in `smart_wallet_agent.py`. To use a different provider, replace that line with the matching LangChain chat model (e.g. `ChatOpenAI`, `ChatGoogleGenerativeAI`, `ChatOllama`) and its API-key env var, and drop or swap the `AnthropicPromptCachingMiddleware` in `init_agent()` (it is Anthropic-specific). Nothing else in the app is tied to Anthropic — the tools, prompt, and checkpointer are provider-agnostic. `ANTHROPIC_API_KEY` is only needed while the default provider is in use.

```python
def init_agent():
    agent = create_agent(model=llm, tools=get_tools(), system_prompt=SYSTEM_PROMPT, checkpointer=_checkpointer, middleware=[...])
```

`AsyncSqliteSaver` persists message history keyed by `thread_id(user_id, chain_id)` (e.g. `"1:42161"`), so each user has an isolated, restart-surviving conversation per chain, shared by Telegram and the web app.

### System prompt

The `SYSTEM_PROMPT` teaches the agent the new model up front:

- **One session key, one global USD budget** per rolling window — no per-token limits, no expiry. `get_all_sessions` reports cap/spent/remaining/watched tokens.
- **Watched tokens and native value count against the cap;** only unwatched tokens move freely. A swap is charged its **net** value change, not the gross input.
- **Approvals are automatic** — there is no approve step or tool; swap/liquidity tools batch them atomically. A "please approve X" request should be declined with an explanation.
- **Removing liquidity is free** against the cap (it returns value — a net inflow).
- One **`preflight_check(token, amount)`** before any spend; **never** estimate swap amounts from prices (`get_quote_in`/`get_quote_out` only); resolve the wrapped-native ticker per chain; never invent addresses; always confirm before an on-chain write; never expose the ciphertext.

The user's message goes to the model verbatim; the `user_id` travels beside it as runtime context (`AgentContext`), so nothing typed can change whose wallet is acted on. `chat(user_id, chain_id, user_input)` is the synchronous entry point. It returns the reply as plain text (joining Anthropic content blocks). A failed turn returns a fixed apology, and the exception goes to the log, never to the user: its text can hold RPC URLs with API keys. `get_history(user_id, chain_id, limit)` returns only what was said, never tool traffic, for `GET /api/chat/history`.

---

## Section 4 — Telegram Bot

`app/telebot.py` exposes the agent as a Telegram bot ([python-telegram-bot v20](https://docs.python-telegram-bot.org/)).

> **The entire Telegram layer is optional.** `telebot.py` is just one front end over the same agent; `smart_wallet_agent.py`'s `main()` provides an equivalent interactive CLI (`make agent`) that needs neither `TELEGRAM_TOKEN` nor a Telegram account. Only `make bot` requires the token (read at `telebot.py` module load) and the `python-telegram-bot` dependency. Everything below — handlers, budget alerts — applies to `make bot` only.

| Handler | Trigger | Action |
|---|---|---|
| `/start` | `/start` | Welcome message; schedules the daily budget alert |
| `/help` | `/help` | Help menu |
| `start_chat` | Any text | Routes to the agent via `asyncio.to_thread` and replies |

### Budget alerts

A daily **`budget_alert`** job (registered per user on `/start`, replacing the old session-expiry alert since keys no longer expire) reads the wallet's on-chain status via `get_all_sessions` and warns the user when the session key is inactive, or when the remaining budget has dropped below **10%** (`BUDGET_ALERT_THRESHOLD`) of the window cap.

`post_init` opens the checkpointer and calls `init_agent()` once before polling. `invoke()` is synchronous and offloaded via `asyncio.to_thread()`; SQLite thread safety is handled in `db.py` via `threading.local()`.
