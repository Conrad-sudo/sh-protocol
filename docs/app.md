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
├── userop.py              ← UserOp calldata, nonce and session-key signing
├── bundler.py             ← The app's own ERC-4337 bundler, on every network — single + batch
├── parallel.py            ← Runs independent chain reads at the same time (two pools: reads, tasks)
├── tx_sender.py           ← Nonce-safe EOA broadcast: locked nonce allocation + fee-bump/timeout
├── contract_errors.py     ← Names contract reverts (custom errors incl. SHOracle's, Error(string)) for the API and the agent
├── vault_signer.py        ← HashiCorp Vault Transit encrypt/decrypt wrapper
├── deploy_wallet.py       ← Per-user wallet deployment + single session-key registration
├── quotes.py              ← Pending transactions: priced, unsigned, awaiting the user's confirmation
├── tx_history.py          ← The History tab's record: every transaction made through Mitfah, kept apart from the chat
├── custom_tokens.py       ← The checks run on a token a user adds by address (MetaMask-style)
├── tools.py               ← LangChain tool wrappers for the AI agent
├── agent_context.py       ← The runtime context (user_id, turn_id) injected into every tool
├── smart_wallet_agent.py  ← LangChain agent and system prompt
├── auth.py                ← SIWE sign-in, JWTs, the EIP-712 signature that adds a contact
├── api.py                 ← FastAPI HTTP API — what the web app in web/ talks to
├── telebot.py             ← Telegram bot front end
├── agent_card.json        ← ERC-8004/v1 agent card (hosted publicly, referenced by tokenURI)
├── abi.py                 ← ABIs for EntryPoint, ERC20, the ERC-8004 registry, and mocks
└── tests/
    ├── checks.py          ← Shared check()/finish() helpers; importing it puts app/ on sys.path
    ├── test_identity.py   ← No tool lets the model choose the account (make identity-test)
    ├── test_auth.py       ← API auth against a throwaway DB (make auth-test)
    ├── test_custom_tokens.py ← Tokens a user adds: rules, routes, tools; fake chain (make custom-tokens-test)
    ├── test_history.py    ← History tab + short chat memory; fake chain, scripted model (make history-test)
    ├── test_speed.py      ← Chain id asked once, parallel reads, the quote's wallet checks (balance and fee headroom too), local userOpHash (make speed-test)
    ├── test_e2e_fork.py   ← Full user journey on a fork, Sepolia unless ARGS names another (make e2e-test)
    └── test_agent_smoke.py ← Real agent conversation, checks tool calls (make agent-smoke)
```

> **ERC20 and Uniswap V2 calldata comes from two external packages.**
> [`langchain-erc20`](https://pypi.org/project/langchain-erc20/) and
> [`langchain-uniswap-v2`](https://pypi.org/project/langchain-uniswap-v2/) build every balance
> read, quote, slippage bound and approval sequence. They return an ordered *execution plan* of
> `(to, value, data)` calls and never sign or submit; `tools._quote_plan` prices a plan as one
> UserOperation and parks it for the user to approve. That is why `abi.py` no longer carries the router/factory/pair/WETH ABIs and
> `constants.py` no longer hardcodes factory addresses.

> **Celo support is partial.** Celo's tokens are seeded into `supported_tokens` and the network routing handles `"celo"`/`"celo-fork"`, but there is no Solidity-side deployment path yet, and Celo is intentionally excluded from `deploy_wallet.py`'s default watched-token map (see [docs/contracts.md](contracts.md#helperconfigssol)).

## Module Dependency Flow

```
telebot.py ──────────► smart_wallet_agent.py ──► tools.py ──► contracts.py ──► network_config.py ──► db.py
                                                  tools.py ──► toolkits.py ──► contracts.py
                                                  tools.py ──► quotes.py  ───► bundler.py
                                                  tools.py ──► bundler.py ───► userop.py ──► vault_signer.py
                                                               bundler.py ───► tx_sender.py
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
CHAIN_ID_ARBITRUM   = 42161
WEI_PER_ETH         = 10**18
ETH_SENTINEL        = "0x0000000000000000000000000000000000000000"

# get_router(chain_id) returns the chain's canonical V2 router from langchain-uniswap-v2's
# KNOWN_NETWORKS, checksummed (the package stores some, e.g. Arbitrum's, in lowercase).
# V2 factory addresses stay unhardcoded: langchain-uniswap-v2 reads router.factory()
# off that router. See toolkits.py.
```

`NATIVE_WRAPPED_TICKER` maps each chain ID to its wrapped-native ticker — `"weth"` on Ethereum/Sepolia/Anvil/Arbitrum (Arbitrum's gas asset is ETH too), `"wbnb"` on BSC, `"celo"` on Celo. `get_native_wrapped_ticker(chain_id)` and `get_native_asset_ticker(chain_id)` resolve these, raising `ValueError` for unconfigured chains.

**The wrapped native token always counts.** `get_always_counted_ticker(chain_id)` names it (WETH, WBNB; `None` on Celo, where `celo` *is* the native asset and watching it would count every movement twice). Mitfah treats it like the native asset it wraps, which the contract always meters: `GET /api/tokens` flags it `always_counted: true` (the web picker shows it ticked and locked), `POST /api/deploy` adds it to `watched_tokens` whatever the request picked, and `POST /api/wallet/watched-tokens/prepare` refuses to remove it (400). This is app policy, not a contract rule — the owner can still call `removeWatchedToken` on the wallet directly, and a wallet deployed before the rule may not watch it (Controls offers a one-click "Count it").

> The old per-feed `HEARTBEAT_*` constants were removed — heartbeats are a Solidity-side (`Constants.s.sol` / `HelperConfig`) concern; the Python app no longer registers feeds.

---

## `db.py`

The data persistence layer. All SQLite reads and writes go through this module. It has no web3 dependency, making it independently testable. Each thread gets its own connection via `threading.local()`.

**Schema (`wallet.db`):**

**Identity is an application account, not a Telegram chat.** `users.id` is what every per-user table keys on. A Telegram chat is one *optional* way to reach an account, held in `users.telegram_chat_id` and bound through a single-use deep-link nonce — never typed in, because chat ids are enumerable and a form accepting one would let anyone attach their Telegram to someone else's wallet. `users.owner_addr` is how the account signs in — Sign-In With Ethereum (`POST /api/auth/siwe/login`) is the only way in, and an address's first sign-in creates its account — and it is the EOA that owns the `SessionHandler` on chain, so it is the only address permitted to deploy for that account. `auth.verify_siwe` requires a full EIP-4361 message naming this site (`SIWE_DOMAIN`), the issued nonce in its `Nonce:` field, and a signer equal to the address it names. The domain check is the one that matters: the nonce endpoint is open, so without it a phishing page could have a victim sign a message for it and replay it here to sign in as the victim. Nothing can recover an account whose address is lost — just as nothing could ever recover control of its wallet.

> Email, password and Google sign-in were removed on 2026-10-01, with `wallet.db` recreated rather than migrated, so there is no migration from the old `email` / `password_hash` / `google_sub` columns. The code still runs on a `users` table that has them; it just never reads them. An account with no `owner_addr` (only possible from the older Telegram-only migration) cannot sign in on the web.

> Before 2026-09-10 the key was `chat_id` and *was* the Telegram chat id. `db._migrate_chat_id_to_user_id` mints a `users` row per legacy chat id, remaps every table, and rewrites the LangGraph `thread_id`s. It runs from `init_db`, after `_migrate_add_chain_id` — that order matters, since the older migration still reads `chat_id` columns.

**One wallet per chain, per user.** The protocol is deployed on several chains and a user runs a `SessionHandler` on each, reached by a Telegram bot per chain. So `session_handlers` and `session_keys` are both keyed by `(user_id, chain_id, …)`, and `user_network` holds the chain the user last deployed on — the chain the Telegram bot and the CLI harness act on, since neither names a network per message. A web chat turn does name one (the network picked on the page), and `db.acting_network` makes every tool of that turn act on it instead; before 2026-09-28 the web chat followed `user_network` too, so on the BNB Smart Chain page the agent answered from whichever wallet was deployed last. Deploying on one chain never disturbs another. Two consequences worth knowing:

- **Every contract cache in `contracts.py` is keyed `(user_id, chain_id)`.** Keyed by `user_id` alone, switching a user's network would hand back the previous chain's wallet, EntryPoint and module bound to the new chain's RPC.
- **Each chain gets its own session key**, even when a user's wallet has the *same address* on two chains — which is possible, since an identical protocol deploy can land `SHFactory` at the same address on each and the CREATE2 salt is the same too. Without `chain_id` in the key those wallets would share one row and one key, so a single key compromise would reach every chain.

```sql
-- The account. It signs in as owner_addr (SIWE, the only way in), which also owns its wallets.
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_addr TEXT UNIQUE,                     -- signs in, and owns the wallets; NULL only for Telegram-only legacy rows
    telegram_chat_id INTEGER UNIQUE,            -- NULL until linked; UNIQUE stops two accounts claiming one chat
    created_at INTEGER NOT NULL
);

-- Single-use, short-TTL nonces. Telegram links are redeemed by the bot's /start; SIWE nonces are
-- burned on sign-in so a captured signature cannot be replayed (and pruned once expired, since the
-- endpoint that issues them is open). A contact nonce is issued for one account, name and address,
-- and burned when the owner's EIP-712 signature over that contact is checked.
CREATE TABLE telegram_link_nonces (nonce TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE siwe_nonces (nonce TEXT PRIMARY KEY, issued_at INTEGER NOT NULL);
CREATE TABLE contact_nonces (nonce TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL, issued_at INTEGER NOT NULL);

-- Only the SHA-256 of each refresh token is stored, so a DB read cannot be replayed as a login.
-- Rotation marks the old row revoked rather than deleting it, which is what makes reuse detectable.
CREATE TABLE refresh_tokens (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);

-- Encrypted session-key storage: one key per wallet PER CHAIN (`target` = the wallet address)
CREATE TABLE session_keys (
    user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, target TEXT NOT NULL,
    key_address TEXT NOT NULL, key_ciphertext TEXT NOT NULL,  -- 'vault:v1:...'
    PRIMARY KEY (user_id, chain_id, target)
);
-- A freshly minted key whose grant has not mined yet. Promoted into session_keys only once the
-- wallet's currentSession is seen to equal it (userop.reconcile_session_key).
CREATE TABLE pending_session_keys (
    user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, target TEXT NOT NULL,
    key_address TEXT NOT NULL, key_ciphertext TEXT NOT NULL,
    PRIMARY KEY (user_id, chain_id, target)
);

CREATE TABLE contacts (user_id INTEGER NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL, PRIMARY KEY (user_id, name));
CREATE TABLE custom_tokens (user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, address TEXT NOT NULL,
    ticker TEXT NOT NULL, name TEXT, decimals INTEGER NOT NULL, added_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, chain_id, address), UNIQUE (user_id, chain_id, ticker));  -- tokens a user added by address
CREATE TABLE dashboard_tokens (user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, ticker TEXT NOT NULL,
    added_at INTEGER NOT NULL, PRIMARY KEY (user_id, chain_id, ticker));  -- the listed tokens a user shows on the dashboard
CREATE TABLE lp_tokens (user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, token0 TEXT NOT NULL, token1 TEXT NOT NULL,
    ticker0 TEXT NOT NULL, ticker1 TEXT NOT NULL, pair TEXT, decimals0 INTEGER, decimals1 INTEGER,
    added_at INTEGER NOT NULL, PRIMARY KEY (user_id, chain_id, token0, token1));  -- pools the assistant deposited into
CREATE TABLE session_handlers (user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL, address TEXT NOT NULL, PRIMARY KEY (user_id, chain_id));
CREATE TABLE factory (chain_id INTEGER PRIMARY KEY, address TEXT NOT NULL);  -- SHFactory address, from the Forge broadcast
CREATE TABLE chains (name TEXT NOT NULL, chain_id INTEGER NOT NULL, PRIMARY KEY (name, chain_id));
CREATE TABLE rpcs (name TEXT PRIMARY KEY, rpc_url TEXT NOT NULL);
CREATE TABLE user_network (user_id INTEGER PRIMARY KEY, chain_name TEXT NOT NULL);

CREATE TABLE supported_tokens (chain_id INTEGER NOT NULL, ticker TEXT NOT NULL, address TEXT NOT NULL,
    PRIMARY KEY (chain_id, ticker), UNIQUE (chain_id, address));  -- the tokens Mitfah lists (and the oracle prices), every chain

-- The History tab: every transaction made through Mitfah. See tx_history.py.
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    source TEXT NOT NULL,                       -- 'assistant' (session-key UserOp) or 'owner' (signed in the browser)
    action TEXT NOT NULL,                       -- written by the server, never by the model or the browser
    status TEXT NOT NULL,                       -- 'pending' | 'confirmed' | 'failed' | 'dropped'
    tx_hash TEXT,                               -- NULL only while an assistant op hasn't been seen on chain
    user_op_hash TEXT, op_nonce TEXT, from_block INTEGER,  -- assistant ops: how a late one is found and settled
    created_at INTEGER NOT NULL, mined_at INTEGER          -- mined_at: the block's timestamp
);  -- unique per (chain_id, user_op_hash) and per owner (chain_id, tx_hash): the web confirms are polled
```

> **Removed with the design overhaul:** the `sessions`, `erc20_selectors`, `uniswapv2_selectors`, and `reputation_registry_selectors` tables. `init_db()` issues `DROP TABLE IF EXISTS` on all four so `make db` migrates an existing `wallet.db`. Per-target session metadata and on-chain selector allowlists no longer exist — there's one global USD cap and one bare session key, both read on-chain.

> **Replaced 2026-09-30:** the six per-network tables (`anvil_tokens`, `mainnet_tokens`, `sepolia_tokens`, `bsc_tokens`, `celo_tokens`, `arbitrum_tokens`) became the one `supported_tokens` table, keyed by chain ID. `init_db()` moves their rows across and drops them. A fork shares its parent's rows because it shares its chain ID.

**Token seeding.** Mainnet/Sepolia/BSC/Celo/Arbitrum token addresses are static: one dict per chain in `seed_data.SUPPORTED_TOKENS`, keyed by chain ID. `make db` makes each chain's rows match its dict **exactly** — a token deleted from `seed_data.py` is deleted from `wallet.db` too. The Arbitrum set is exactly the tokens `HelperConfig.getArbConfig` prices, and every address matches the corresponding `ARB_*` constant in `script/Constants.s.sol` — an unpriced token would only offer a watched-token choice that makes `deployWallet` revert with `TokenNotPriced`. **Anvil tokens are recovered from the Forge broadcast file** (`broadcast/DeploySHProtocol.s.sol/31337/run-latest.json`): the mocks are deployed at fresh addresses every run, so `seed_reference_data()` reads each `ERC20Mock`/`MockWeth` deployment's decoded constructor arguments (symbol = arg index 1) and maps ticker → address. This is the only writer of anvil's rows, and it replaces them outright too (left alone when there is no broadcast).

**Tokens a user adds (`custom_tokens`).** Besides the listed tokens above, each user can add tokens by contract address from the web app, per chain (MetaMask-style). They have no price feed, so they can never be watched and never count toward the cap; the list only decides what the dashboard shows and which names the agent resolves. `db.resolve_token(user_id, chain_id, ref)` is the one lookup every tool, the withdraw endpoint and the balance read share: a listed ticker first, then the user's own, and a raw `0x` address passes through (the owner's withdraw endpoint relies on that). The agent's tools add one rule on top, in `tools._token_address`: an address is accepted only for a listed or added token, so the chat can never trade or send a token the owner didn't choose (THREAT_MODEL §4.2). It reads the table on every call — no snapshot — so a token added on the web works in the Telegram bot's process at once. See `custom_tokens.py` below and THREAT_MODEL §4.9.

**The dashboard's tokens (`dashboard_tokens`).** The dashboard shows only the tokens the user chose: the native token, the listed tokens in `dashboard_tokens`, and their `custom_tokens`. Every wallet read copies the wallet's counted (watched) tokens into `dashboard_tokens`, plus the wrapped native token (`api._show_counted_tokens`) — so the tokens picked at deploy, tokens counted later in Controls, and wallets made before the list existed all show without a separate write. A listed token can also be added by address (it keeps its listed ticker and skips the symbol rules and the 25-token cap). Any token can be removed except the native token and WETH/WBNB; a listed token is refused (409) while the wallet still counts it, so the dashboard always shows everything the limit covers. The list is dashboard-only: the agent still knows every listed token. Read as a join on `supported_tokens`, so a token Mitfah stops listing drops off every dashboard.

**LP tokens on the dashboard (`lp_tokens`, 2026-10-05).** When the user agrees to an `add_liquidity` quote, `confirm_transaction` saves the deposit's pool (`db.save_lp_token`) *before* the deposit is sent, like the History row, so one that lands after the wait still shows; a failure to save never fails the send. The quote carries the pool (`PendingTransaction.lp_pool`, from `tools._lp_pool`); nothing else saves one. A pool is one row whichever way round it was named — its two tokens in the pool's own order — and the wrapped native token is named as the native asset (`eth`), since that is what goes in and comes back. Every wallet read lists, after the added tokens, one row per pool the wallet still holds LP tokens in (`api._lp_balances`): `lp: true`, named like `eth/usdc lp` (native side first), with `underlying` = the wallet's share of each of the pool's tokens right now (balance × reserve ÷ totalSupply). A pool the wallet holds nothing in isn't shown, and less than a trillionth of a pool counts as nothing (`_LP_DUST`): "remove all" passes through a float and can leave a few wei. The first read of a pool finds its address (`factory.getPair`, the factory read once per chain off the router) and its tokens' decimals and saves them; after that it is three reads per pool, all at once. The list is the dashboard's only — it is deliberately not `custom_tokens`, so the agent still can't name or send an LP token (THREAT_MODEL §4.2). Only deposits made through the assistant are tracked; liquidity added elsewhere isn't found.

**Initialisation:** run `make db` once to create tables and seed. Re-running is safe (`INSERT OR REPLACE`, each chain's tokens replaced as a whole, plus the drops above). The `sepolia` RPC row comes from `SEPOLIA_RPC_URL` in `.env`, keeping API-keyed URLs out of source control.

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

**The chain id is asked once.** web3's validation middleware reads `w3.eth.chain_id` before every
`eth_call` and `eth_estimateGas` — twice per call in practice — so until 2026-10-02 most requests the
app sent were that one question (22 of the 37 in quoting a transfer). Every connection now uses
`_ChainIdOnceProvider`, which keeps the node's first answer and reuses it for every thread. It is
the node's real answer, not the database's, so the middleware's check still means something; an
error is not kept. (web3's own request cache keys entries by thread, and LangGraph runs each turn's
tools on fresh threads, so it would still have asked about once per tool call.)

---

## `contracts.py`

Contract loading with per-`user_id` caching, plus the ERC-7579 calldata/nonce helpers used by `userop.py` and `bundler.py` so the packing logic lives in one place.

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

**They share the app's provider.** Each package builds its own web3 connection from the bare RPC URL, with web3's stock provider — the one that asks the chain id before every call. `_share_provider` points the ERC-20 and Uniswap toolkits' connections at the app's (`toolkit.w3.provider = w3.provider`), so they ask once too and share one HTTP session per chain; their contracts look the provider up per call, so everything already bound follows. The ERC-8004 toolkit keeps its own: it sets a 20 s timeout of its own and is off the send/swap path.

`_BLOCKED_TOOLS` withholds `approve`, `approve_token` and `revoke_approval` from the returned dicts. They build valid calldata but always revert here (see the approvals note under `tools.py`), so exposing them would only let the agent burn a UserOp on a guaranteed failure.

---

## `tx_sender.py`

Every transaction the app signs with one of *its own* EOAs goes out through here — the outer `handleOps` in `bundler.py`, and `deployWallet` / `addSession` in `deploy_wallet.py`.

Two problems it exists to solve, both invisible on a single-user Anvil run:

- **Nonce races.** `telebot.py` serves each user request on its own thread (`asyncio.to_thread`), but a handful of shared keys sign for everyone — one bundler EOA per process, one deployer per chain. Reading the nonce per-thread hands the same value to two threads, and the second transaction is dropped or replaces the first. `send_tx()` allocates from a cached `(chain_name, address) → nonce` counter under a process-wide lock spanning allocate → sign → broadcast. The counter is seeded from `pending` (not `latest`, which does not count the mempool) and advanced locally; any failure clears it so the next caller re-seeds, which also self-heals a counter left stale by an out-of-band transaction. The lock covers **one process**: that is why the API and the Telegram bot bundle with different keys (`API_BUNDLER`, `TELEGRAM_BUNDLER`) — sharing one, each process would keep its own counter and hand out the same nonces.
- **Stuck transactions.** A fee cap the base fee has since overtaken will never be mined, so an unbounded `wait_for_transaction_receipt` hangs a user's request permanently. `send_and_confirm()` gives each attempt `ATTEMPT_TIMEOUT_SECS`, then replaces the transaction at its own nonce with both fee fields bumped past the node's price floor, up to `MAX_FEE_BUMPS` times, and raises `TimeoutError` rather than hanging. The replacement cap is floored against the *current* base fee, not just scaled from the stale one. All broadcast hashes are polled, every `RECEIPT_POLL_INTERVAL_SECS` (0.5 s — the user is waiting on it), since a replacement races the transaction it replaces and either may win.

Only the outer transaction is ever re-signed; an ERC-4337 UserOp in its calldata is untouched and its session-key signature stays valid, because the EntryPoint prices reimbursement purely from the UserOp's own gas fields.

```python
send_tx(w3, chain_name, account, tx, send_w3=None)          # → tx_hash, nonce allocated under the lock
send_and_confirm(w3, chain_name, account, tx, send_w3=None) # → receipt, with bounded wait + replace-by-fee
```

`send_w3` broadcasts through a different endpoint than the one nonces and receipts are read from — the bundler passes a private RPC on live mainnet. A privately sent transaction is invisible to the normal node until mined, so after a restart with one still pending the counter re-seeds below it.

## `bundler.py`

The blockchain execution layer. The app is its **own ERC-4337 bundler on every network** — plain Anvil, every fork, every live chain. It signs an outer `handleOps()` transaction with a bundler EOA of its own, fronting the gas and naming itself beneficiary so the EntryPoint repays it out of the wallet's prefund. No third-party bundler ever holds a signed op. (An Alchemy-bundler path, `live_network.py`, existed until 2026-09-22; it was unreachable in practice and was deleted.)

```python
send_user_op_as_session(user_id, key_ciphertext, target, value, data)
send_batch_user_op_as_session(user_id, key_ciphertext, executions)     # atomic multi-call, unattended

quote_user_op(user_id, session_handler, entry_point, calldata, nonce, bundler) -> UserOpQuote  # nonce=None: read with the rest
check_bundler_funds(user_id, quote, bundler, outer_gas=None, balance=None)  # can the service afford to send it
prepare_user_op(user_id, key_ciphertext, session_handler, entry_point, quote, bundler) -> PreparedUserOp
broadcast_user_op(user_id, prepared, bundler) -> (tx_hash, receipt)
use_bundler_key(env_name)                                              # which key this process signs with
```

**Quote, then send.** `quote_user_op` does everything except sign; `prepare_user_op` signs a quote
and `broadcast_user_op` sends it. The split is what lets the user be shown a price before an
executable transaction exists anywhere — see `quotes.py`. `send_user_op_as_session` runs all three
back to back for the unattended path (tests, and anything with nobody to ask).

**In rounds, not one read after another (2026-10-02).** A quote used to make about a dozen chain
reads in sequence; now `quote_user_op` makes three rounds (`parallel.read_all`): everything that
needs only the calldata at once — the execution estimate, the latest block (one read, where it used
to be two), the tip, `maxOpGasCost`, the override check and, when the caller passes `nonce=None`,
the nonce — then the validation estimate (it signs over the nonce), then the whole-bundle
simulation. `prepare_user_op` makes two: the nonce, the block number and the bundler's balance at
once, then the signed estimate. The op's hash is no longer asked of the EntryPoint at all:
`userop.hash_user_op` works it out with ERC-4337 v0.7's formula, checks the first op on each
(chain, EntryPoint) against `getUserOpHash`, and asks the EntryPoint every time after if they ever
disagree. (Signing used to ask for the same hash twice.)

**Which key signs.** Chosen by the *process*, not the chain: the API bundles with `API_BUNDLER` (the default), and `telebot.py` switches to `TELEGRAM_BUNDLER` at startup. `tx_sender`'s nonce lock only coordinates threads within one process, so two processes sharing a key would hand out the same nonces. Anything else that runs the agent in its own process (`make agent`, `make agent-smoke`) uses `API_BUNDLER` and must not run beside the API. Both keys must be plain EOAs with no code on every chain they bundle for — the EntryPoint's bare ETH send to the beneficiary reverts AA91 against code — which is why the well-known Anvil keys are never used. `make fund` tops both up on a local node; on a live chain the operator funds them.

**UserOp lifecycle:**
1. Build `SessionHandler.execute(mode, executionCalldata)` — single-call (`pack_execution_calldata`) or batch (`encode_batch_execution_calldata`) — and fetch a nonce keyed via `session_key_nonce_key()`.
2. **Estimate without the session key.** Execution gas: `execute()` estimated `from` the EntryPoint's address, exactly as `handleOps` will call it — no op, no signature. It walks the session-key path (admin guard, protocol fee, the spending-limit hook with every price read), so an op that would fail is refused here, *by name*, before anything is signed or paid. Validation gas: `validateUserOp()` estimated the same way, signed by a **throwaway key** the wallet never authorized — it fails the signature check without reverting, at the same cost as the real key. All estimates run against the `pending` block, whose timestamp is the next block's, so a price feed that goes stale before the op lands fails the estimate too.
3. `preVerificationGas` = 21,000 + the handleOps calldata (4 gas per zero byte, 16 per non-zero) + a fixed 20,000 for handleOps' own bookkeeping, plus the Ethereum data fee on live Arbitrum (from NodeInterface). The fee cap is clamped under the wallet's `maxOpGasCost`.
4. **Simulate the whole bundle, still without the session key.** The op is signed by the same throwaway key — over the op's *real* `userOpHash`, or validation fails on the signature — and `handleOps` is estimated with a **state override** writing the throwaway key *and a far-future deadline* into the wallet's packed `currentSession` slot (`CURRENT_SESSION_SLOT`). Both halves matter: the wallet returns the deadline as the op's ERC-4337 validity window, so a key written with a zero deadline would simulate as expired and the whole bundle would fail `AA22` instead of being priced. It is a plain slot, not a mapping base — the wallet authorizes one key at a time, so there is no key to hash in. The override lives only inside that one call, so the op being simulated is executable by nobody. It catches everything a real submission would hit and measures what the transaction burns. The slot is verified against the live contract first (one `eth_call` of `isSessionActive` under the override, which checks *both* halves); if the node refuses overrides or the layout has moved, the outer gas falls back to the sum of the parts and the quote loses only precision.

   This is where a quote stops. Two figures come out of it, and they are different on purpose:
   `expected_gas` (`preVerificationGas` + the *unbuffered* validation and execution estimates + the
   EntryPoint's 10%-above-40k fine for execution gas reserved and unused) is what the wallet will
   actually be charged; `op_gas × maxFeePerGas` is the ceiling it *could* be charged. The bundle
   simulation is deliberately **not** used for the price — `eth_estimateGas` must return enough to
   forward both gas limits whether or not they are used, so it tracks the limits and over-states the
   cost by the whole buffer (measured: 17% high). It sizes the outer transaction and nothing else.
   On a mainnet fork the parts-based figure came in 7.8% above the real charge.
5. **Decrypt the session key from Vault transiently, sign the EIP-191 digest, wipe.** Nothing before
   this point produced a transaction anyone could execute.
6. Re-read the nonce (only that: the gas limits and fee cap come from the quote unchanged, so what is sent is what was quoted) and estimate the whole `handleOps` with the real op — catches anything that moved between the quote and the user agreeing, and sizes the outer transaction.
7. Broadcast via `tx_sender.send_and_confirm` — through Flashbots Protect on live mainnet (`MAINNET_PRIVATE_RPC_URL` overrides it), the chain's normal RPC everywhere else.
8. If our transaction reverted or stalled, look the op up on chain by its hash: the signature does not cover the beneficiary, so someone else may have landed it first. The user's action then *has* happened, and that transaction is returned instead of a failure the user might retry.
9. Surface an inner-call revert from `UserOperationRevertReason`, named via `contract_errors`.

Two gaps remain. **Celo** is now an OP-stack L2, and any L1 data fee it charges the outer transaction is *not* priced into `preVerificationGas` — the bundler would absorb it, so watch its balance there before relying on it. **Forks** charge no L1 fee at all, so Arbitrum's L1 pricing is only exercised on the live chain.

---

## `quotes.py`

The pending-transaction store: what stands between the agent describing a transaction and the chain
executing one. Every write tool stops at a quote — the exact executions, priced — parked here under
a random id. `confirm_transaction(quote_id)` is the only thing in the app that sends.

```python
QUOTE_TTL_SECONDS = 300
put(user_id, chain_id, turn_id, action, calls, quote, cost) -> PendingTransaction   # holds no key
take(user_id, chain_id, quote_id, turn_id) -> PendingTransaction   # claims it; single-use
drop(user_id, quote_id) -> bool                                    # the user said no
```

Three properties do the work:

- **The transaction is fixed before the user sees it.** A quote holds the calldata and gas the
  confirm will use, so there is no re-describing step in between for an instruction to sit in, and
  `action` is composed from the calls themselves (the packages' own `description` fields, or
  `tools.py` for a bare native send) rather than by the model. The most a confirmed quote can do is
  what its quote said.
- **Confirming takes a real turn boundary.** `take` refuses a quote whose `turn_id` is not strictly
  less than the current one. `turn_id` comes from `smart_wallet_agent._next_turn_id()`, bumped once
  per user message, so text arriving *within* a turn — a tool result, a token name, a registration
  file, anything the model read rather than the user typed — cannot quote and confirm on its own.
- **Quotes go stale and are single-use.** Five minutes, one claim. A broadcast that fails may still
  have landed (§4.4a), so a quote must never be replayable.

What it does *not* do: a user who approves without reading is still approving whatever the quote
says. This makes the agent's claims checkable against text the agent did not write, and bounds one
confirmation to one transaction. See THREAT_MODEL §4.2.

The store is in memory and per process, deliberately: a pending transaction is a live intent and
should die with the process rather than outlive a restart in a database. The practical consequence
is that a quote raised in the web app cannot be confirmed from Telegram — the second process reports
an unknown quote and the user asks again.

---

## `custom_tokens.py`

The checks run on a token a user wants to add, all read from the chain (nothing the browser says about the token is trusted):

```python
def inspect_custom_token(user_id, chain_id, address, wallet_address) -> dict  # or raises CustomTokenError
```

The token is read through `contracts.load_ierc20` — the same ERC-20 binding the agent's tools use — so the API runs the checks inside `db.acting_network` for the request's chain. It counts as an ERC-20 only if **both `symbol()` and `decimals()` answer**; an address with no code, or a contract that can't answer both, is refused with a message asking the user to **check the token address again**. A token is accepted only if the address is valid, not zero, not the wallet itself and has code; `decimals()` answers 0–36 and `balanceOf` works; `symbol()` is readable (string, or old `bytes32` via a raw-call fallback) and is 1–12 characters of `A–Z a–z 0–9 . _ - $`; its symbol is not a listed ticker, `eth` or the native/wrapped ticker, and the user has not already added it (or another token under the same symbol). At most `MAX_CUSTOM_TOKENS_PER_CHAIN` (25) per user per chain. A **listed** token's address is accepted too, unless it is already on the user's dashboard: it comes back `listed: true` under its listed ticker, and the symbol rules and the cap don't apply. The symbol rule is strict because the symbol reaches the agent: langchain-erc20 reads `symbol()` on chain for every quote's `action` line, so a token calling itself `USDC`, or carrying a sentence, is refused rather than relabelled. The name is kept for the web app only (control characters stripped, 64 characters) and never handed to the model.

Routes (all need a signed-in account and a wallet on the chain; none needs a signature — nothing changes on chain):

| Route | |
|---|---|
| `POST /api/tokens/custom/lookup` `{chain_id, address}` | Preview: `{address, ticker, symbol, name, decimals, balance_raw, listed}` — the symbol and decimals are what show it's an ERC-20 — or 400 with the reason it can't be added. Saves nothing |
| `POST /api/tokens/custom` `{chain_id, address}` | Runs the checks again, then saves: a listed token to `dashboard_tokens`, any other to `custom_tokens`. 201; 409 if a parallel request just added it. Counting a listed token is the owner's separate `watched-tokens/prepare` transaction |
| `DELETE /api/tokens/custom/{chain_id}/{address}` | Takes it off the dashboard (404 if it isn't on it). 409 for a listed token the wallet still counts ("Stop counting it first"), 400 for WETH/WBNB. The tokens stay in the wallet |

`GET /api/wallet/{chain_id}` lists only the dashboard's tokens in `balances`: the native token, then the listed ones in `dashboard_tokens` (`always_counted: true` on WETH/WBNB), then added ones with `custom: true` and their `name` (one `balanceOf` each; decimals are stored), then the LP tokens of the pools the assistant deposited into, with `lp: true` and `underlying` (see `lp_tokens` above). `POST /api/wallet/withdraw/prepare` accepts a token **address** as well as a ticker, so the owner can recover any token — added or not, LP tokens included.

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

**`deploy_wallet(user_id, chain_name)`** calls **`SHFactory.deployWallet(DEFAULT_DAILY_LIMIT_USD, DEFAULT_WINDOW_SECS, watched_tokens, session_key, session_key_valid_until, trusted_spenders)`** — the first three seed the wallet's spending-cap config (`watched_tokens` is `DEFAULT_WATCHED_TICKERS` resolved to addresses); the rest make the wallet usable immediately, replacing what used to be two follow-up owner transactions. The key's deadline is `DEFAULT_SESSION_TTL_SECS` (30 days) from now; the contract refuses anything past `MAX_SESSION_TTL` (90). It decodes `WalletDeployed`, asserts the address matches the CREATE2 prediction, and persists it.

Order matters here, and CREATE2 is what makes it possible. `predictWalletAddress(deployer)` returns the address this deploy will land on — the factory salts with its own per-owner `deployCount`, so the value advances after each of our deploys and is untouched by anyone else's — then `create_pending_session_key(user_id, chain_id, predicted)` mints a fresh key **under the address the wallet is about to have** — the same `(user_id, chain_id, wallet_address)` key `tools._get_session_keys` resolves — and holds it in `pending_session_keys` until the deploy has mined. Without a predictable address the key could not exist before the deploy that takes it as an argument. The mismatch check after the receipt is deliberate: a wrong address would silently orphan the key. On a stale prediction it re-files the session key under the address that actually deployed rather than raising — the on-chain grant is already correct for it, and raising would strand a funded wallet with no `session_handlers` row.

⚠️ **`deployCount` cannot answer "does this user have a wallet?" here.** It is keyed by `msg.sender`, and `_private_key_env` resolves ONE deployer EOA for every user on a chain, so it counts all of them. That check only works when the end user signs their own deploy (the web-app flow). In the bot flow, per-user ownership is the `session_handlers` table's job — `deploy_wallet` warns when it is about to replace an existing row **for this chain**, because that wallet keeps its funds and becomes unreachable from the app. Wallets on other chains are expected and are left alone. `trusted_spenders` is `[constants.get_router(chain_id)]`, or empty on bare Anvil, which has no Uniswap deployment. The wallet is seeded with ETH in the deployment call itself — `deployWallet` is `payable` and forwards its `msg.value` straight to the new clone (`WALLET_PREFUND_ETH_LIVE` on live chains, `WALLET_PREFUND_ETH_LOCAL` on anvil/forks), so no follow-up transfer is needed.

The deployer must already hold gas when this runs. On a fork it inherits the forked chain's real balance — **zero** on `mainnet-fork` and `bsc-fork` — so `make fund` (an `anvil_setBalance` cheat RPC) has to come first. The Makefile makes `fund` a prerequisite of both `deploy` and `deploy-wallet`, so this holds for every target, including a standalone `make deploy-wallet ARGS=<fork>`. On a fork the deployer is also the API's bundler (`API_BUNDLER`, see `bundler.resolve_bundler`), and `make fund` tops up the Telegram bot's bundler (`TELEGRAM_BUNDLER`) at the same time, so there is no separate bundler-funding step.

**`add_default_session(user_id)`** registers the wallet's **single** session key. It mints a **fresh** key via `create_pending_session_key` (Vault-encrypted, keyed to the wallet address), calls **`SessionHandler.addSession(key, validUntil)`** as the owner, and promotes the key out of pending once that mines. Granting evicts whatever key the wallet held. **No longer part of the deploy path** — `deploy_wallet` seeds the key inside `deployWallet` — it is kept for re-granting a key on a wallet deployed without one, or after `removeSession`. There are no per-target sessions, selectors or budgets to configure — the wallet-wide cap and the admin guard replace all of that; the one thing a key carries is its deadline. (The old `add_session(targets, functions, ...)` and the `approve()` router-pre-approval helper were removed: standing approvals now revert on-chain, so approvals only ever happen atomically inside the swap/liquidity tools.)

**`deploy(user_id, network)`** is the top-level dispatcher (validates the network, then calls `deploy_wallet`) — invoked by `make deploy-wallet` via the `__main__` block, which also seeds a demo contact. `__main__` no longer calls `add_default_session` or `trust_router`: `deployWallet` does both.

**Signing-key resolution** (`LIVE_PRIVATE_KEY_ENV`): live BSC and Celo use their own key (`BSC_PRIVATE_KEY`, `CELO_PRIVATE_KEY`); **every fork plus live Sepolia uses `API_BUNDLER`** (formerly `SEPOLIA_PRIVATE_KEY`); plain `anvil` falls back to `ANVIL_PRIVATE_KEY`. Forks of a real chain must avoid the well-known Anvil burner key: it is EIP-7702-delegated to drainers on real Sepolia/BSC/mainnet, and a fork inherits that code — which both breaks fork tests and disqualifies it as a `handleOps` beneficiary (the EntryPoint's bare ETH send reverts AA91 against an address with code).

---

## `tools.py`

Wraps blockchain operations as LangChain `@tool`-decorated functions; each docstring tells the LLM when/how to call it. `get_tools()` is the factory.

**The ERC20 and Uniswap tools are wrappers.** Their bodies do three things: resolve tickers and contact names to addresses, invoke the matching `langchain-erc20` / `langchain-uniswap-v2` tool to get an execution plan, and hand that plan to `_quote_plan`. `_quote_plan` prices a one-call plan as an ERC-7579 single execution and anything longer as an atomic batch, and parks it in `quotes.py`.

**No write tool sends anything.** They all stop at a quote — what the transaction does, what it costs in USD, and a `quote_id`. `confirm_transaction(quote_id)` is the only tool in the app that signs and broadcasts, and `cancel_transaction(quote_id)` discards one. See `quotes.py` above for why the send is a separate, turn-gated step.

The wrappers exist — rather than exposing the package tools directly — because the package tools take no `user_id` (a toolkit instance is bound to one user's chain), `langchain-uniswap-v2` accepts raw addresses only, and neither package submits anything. Their docstrings describe a *different* function signature (addresses, `from_address`, `nonce`, returns an unsubmitted plan), so they are not interchangeable with the docstrings here, which are the contract the LLM actually sees. See [langchain-packages-migration.md](langchain-packages-migration.md) §3.

**One key, one budget — and the model never handles the key.** The wallet on the user's CURRENT chain has exactly one session key, so there is nothing to choose: no tool takes it as an argument. `_quote_executions` reads it only to refuse early when there is none, and `confirm_transaction` reads it when it signs, so a key renewed between quote and confirm is the one used. (Until 2026-10-02 a `get_session_keys` tool handed the model the Vault ciphertext to pass back into every write — one model call to fetch it and ~90 characters of random text to repeat, per transaction.) Spending is bounded by one wallet-wide USD cap per window, read on-chain.

### Session / budget / pricing tools

| Tool | Description |
|---|---|
| `get_wallet_status()` | On-chain wallet status: `{paused, session_active, session_expires_at, session_expires_in_secs, daily_limit_usd, spent_usd, remaining_usd, window_hours, watched_tokens}` (reads `paused`/`getConfig`/`getRemainingBudget`/`isSessionActive`/`currentSessionValidUntil` in one round of parallel calls). `remaining_usd` is floored at 0, as in a quote. Called `get_all_sessions` until 2026-10-02 — a name left over from per-token sessions; its plain half `_get_wallet_status` also serves the Telegram bot's `budget_alert` |
| `preflight_check(token, amount, token_received?, amount_received?)` | **For questions only since 2026-10-02** ("could I send $500 of ETH?") — every write tool runs the same checks itself (`_wallet_checks`, shared). Pause + session validity + balance + budget check + USD value in one call. Charges what the module will: the metered value leaving minus the metered value coming back (native + watched tokens only), so a wrap into a watched WETH is `charged_usd: 0`. Returns `is_paused, session_active, session_expires_in_secs, expiring_imminently, enough_balance, within_budget, usd_value, charged_usd, remaining_usd`, plus `balance_short` when the wallet doesn't hold the amount (`usd_value` is `null` for a token the user added: it has no price). `session_active` is reported false once under `SESSION_EXPIRY_MARGIN_SECS` (60s) remain, so a transaction cannot be quoted, confirmed and then refused with `AA22` while in flight. Its chain reads run in two rounds of parallel calls (the wallet's state with each token's watched flag, decimals and — for what is sent — balance, then the prices) — on a live RPC that is ~1 s instead of 3–4.5 s one after another. A price that is only shown (a token the limit doesn't count) may fail without failing the checks: `usd_value` is then `null` and `usd_value_unavailable` says why. The balance is the amount alone: the fees come on top, in the native asset, and only a quote knows them |
| `get_price(token, amount?)` | Unit price via the oracle (`getUsdValue` of one whole token), or the USD value of `amount`. Refuses a token the user added by name ("no price") rather than letting the oracle revert |

Removed on 2026-10-02, each covered by a tool above or by the checks every quote makes: `check_session_validity` (it took a `token` it ignored, from the per-token sessions), `check_remaining_budget`, `check_spending_within_budget` (it also counted a listed token the limit doesn't watch at full value, and took a whole-number amount) and `get_usd_value` (now `get_price`'s `amount`).

### Read and quote tools

`get_eth_balance`, `get_erc20_balance(token, contact?)` (the wallet's own balance, or a saved contact's with `contact`), `get_quote_in`, `get_quote_out`, `get_pool_quote`, `get_lp_amounts`, `get_liquidity_token_balance` (`token_b` defaults to `"eth"`, the native asset's pool), plus contacts (`get_contact`/`get_all_contacts`) and `get_supported_tokens` (returns `{"native": "ETH", "listed": [...], "custom": [...]}` — `custom` is the tokens the user added). The quote and preview tools are for questions: the write tools quote the router themselves.

Removed on 2026-10-02: `get_contact_erc20_balance` (now `get_erc20_balance`'s `contact`), `get_native_asset` (`get_supported_tokens` returns it as `native`, `get_eth_balance` as `asset`, and every quote's `action` names it), `get_erc20_allowance` (the wallet's allowance to a contact is always 0 by design, and the one that matters — a contact's to the wallet, for `transferFrom_erc20` — that tool checks itself), and the four `is_*_sufficient` tools (every write tool refuses a short balance itself).

Every token argument accepts a listed ticker, a ticker the user added, or a `0x` address; `_token_address` resolves it through `db.resolve_token` and the packages are always handed an address. A quote that moves a token Mitfah doesn't list carries a `details` sentence from `_limit_note` saying how the cap treats it: sending or selling one costs nothing; buying one with the native asset or a watched token counts the **full** amount paid, because what comes back has no price.

> **The agent reads the contact list and never writes it.** There is no `save_contact` and no `delete_contact` tool. The contact list is the allowlist of destinations for the wallet's funds — `_resolve_contact` takes a saved name and refuses a raw address — so changing it is an owner action and lives on the API instead: `POST /api/contacts/prepare`, `POST /api/contacts`, `GET /api/contacts`, `DELETE /api/contacts/{name}`, all requiring a signed-in account. Adding (or changing) one also takes the owner wallet's EIP-712 signature over the exact name and address: `prepare` checks the contact and returns the typed data (`AddContact`, domain `Mitfah`/`1`, a single-use nonce), and the save verifies the signature against `owner_addr` — so even a stolen web session cannot add a payee. Whoever holds a chat surface can pay the people the owner saved and cannot add a new one; the case that motivates it is an unlocked stolen phone. Deleting moved too, even though it only ever shrinks the allowlist and steals nothing — one boundary ("reads, never writes") is easier to hold than a rule with an exception. See THREAT_MODEL §4.2.

> **The quote tools return whole units only.** `get_quote_in` / `get_quote_out` return `{amount_in, amount_out, path}`, and `get_pool_quote` / `get_lp_amounts` return only their whole-unit fields. The old `*_base`, `decimals_a/b`, `liquidity` and `token_*_address` keys are gone: they existed so the swap tools could do their own base-unit and slippage arithmetic, which now happens inside the packages.
>
> **`"eth"` (or `"bnb"`) means the native asset in the Uniswap-side tools only.** `swap`, `add_liquidity` and `remove_liquidity` take a native side through the router's native functions (`swapExactETHForTokens`, `addLiquidityETH`, …), so it is sent or received as the native asset itself; `"weth"`/`"wbnb"` names the wrapped token. The quote and preview tools route their token arguments through `tools._resolve`, which maps the native asset to the wrapped-native address — the pool the native asset trades in — and passes a raw `0x…` through. The four ERC20 tools (`get_erc20_balance`, `wrap_eth`, `transfer_erc20`, `transferFrom_erc20`) hand the ticker to `langchain-erc20`, whose token map has no `"eth"`: use `get_eth_balance` / `send_eth` for the native asset.

### Write tools

**Every write tool checks before it quotes (2026-10-02).** `_quote_executions(runtime, executions, action, legs)` runs `_wallet_checks` — the pause, the session key (and the 60 s expiry margin), and, from `legs` (`[(token, amount, SENT | RECEIVED)]`), whether the wallet holds what it sends and what the spending limit would count — on a task thread *alongside* the quote's own reads, and refuses with the reason (`_enforce_wallet_checks`) before anything is parked: "the owner has paused this wallet", "the session key is no longer active … Controls → Renew", "Not enough ETH: the wallet holds X, and this needs Y", "this would count $X toward the spending limit, but only $Y is left". A failed check wins over a failed simulation, being the clearer reason. A passing quote carries `usd_value`, `charged_usd`, `remaining_usd` and `session_expires_in_secs`. Writes that move nothing countable (registry writes, removing liquidity, a `transferFrom` from a contact) pass `legs=None` and check only the pause and the key. Adding liquidity counts **both** deposits (the LP token back has no price). The packages' own preflight (`preflight=True`) refuses a short token balance too, while building the plan; `send_eth` has no package plan, so its balance is checked only here.

**The native asset has to cover the fees as well (`_check_native_headroom`).** One balance pays, in this order, the gas the EntryPoint asks for up front (whatever the wallet's deposit there doesn't cover), Mitfah's fee, then the value the calls carry. So when the calls carry native value, the quote also reads the wallet's balance and its EntryPoint deposit, and refuses unless value + fee + (`max_gas_wei` − deposit) fits — saying the most it can use now. Before this, "send all my ETH" was refused as a bare `FailedCall()`, and an amount a little smaller passed the quote and then failed on chain after the user agreed, charging them the gas. When the quote itself failed (the value plus the fee already don't fit), the gas is unknown and only the fee is counted.

The swaps' quotes carry the router's figures, so no `get_quote_*` first: the package reports only the slippage-bounded amount, and `_quote_exact_input_swap` / `_quote_exact_output_swap` recover the expected one from it (`min × 10000 / (10000 − bps)`, `max × 10000 / (10000 + bps)` — exact to within a unit in the last place) for `details` ("received: about X, at least Y (slippage tolerance 0.5%)") and for the limit's estimate. The cost comes from one native price read (`getUsdValue(ETH, 1e18)`, scaled — the oracle is linear, so this is the three old reads to the wei), started alongside everything else. `confirm_transaction` reads the remaining budget while the History row is written.

| Tool | Description |
|---|---|
| `send_eth(...)` | Sends native ETH/BNB (metered — native value counts against the cap) |
| `transfer_erc20(...)` | Sends tokens (metered if watched) |
| `transferFrom_erc20(...)` | Transfers from a sender who has approved the wallet (the package checks that allowance and their balance) |
| `wrap_eth(...)` | Wraps native → WETH/WBNB |
| `swap(token_in, token_out, amount_in? \| amount_out?, slippage_bps, recipient?)` | Uniswap/PancakeSwap V2 swaps — **approve + swap sent as one atomic batch**. Exactly one amount; picks the router function (below). Optional `recipient` delivers the output straight to a saved contact (see below) |
| `add_liquidity(token_a, amount_a, token_b="eth", slippage_bps)` | Add liquidity — approvals batched and residuals zeroed atomically. The native asset by default (`addLiquidityETH`); `token_a`'s amount is the one fixed, and a native `token_a` is refused ("Name how much X to deposit first"). Its quote carries the pool, which confirming puts on the dashboard (`lp_tokens`) |
| `remove_liquidity(token_a, lp_amount, token_b="eth", slippage_bps)` | Remove liquidity — the pool's **LP token** is approved by address to the trusted router (granted in the `deployWallet` call itself) and consumed in one batch. The native asset back by default (`removeLiquidityETH`); a pool named native-first is turned round |
| `confirm_transaction(quote_id)` | **The only tool that sends.** Signs the quoted op with the wallet's current session key and broadcasts it. Its result ends with what is left of the spending limit (`_budget_left`; omitted if the read fails — never an error after a send), so the agent needs no `get_wallet_status` call |
| `cancel_transaction(quote_id)` | Discards a quote the user declined |

> **Every row above except the last two returns a QUOTE, not a receipt.** The tool builds the exact
> calldata, prices the whole UserOperation against the chain, and returns `action` (what it does,
> composed from the calls rather than by the model; `_action_of` leaves out the approvals around the
> step, by their `approve`/`approve_reset` roles — until 2026-10-02 it kept only an `action` role,
> which the Uniswap package doesn't use, so swap and liquidity quotes listed every approval), `network`, `total_usd`, `max_total_usd`,
> `protocol_fee_usd`, the wallet-check figures, a `quote_id` and a `next_step` saying what to show.
> Nothing is signed and nothing is sent until `confirm_transaction`, which must come in a **later
> conversation turn** — so the user has actually replied to the price. The swap and liquidity tools
> put the expected amounts and their slippage bounds in the quote's `details`. Every write tool's
> docstring ends the same way: a quote, nothing sent, show it as its `next_step` says — one line,
> where it used to be a six-line paragraph in each of them that had fallen behind `next_step`.

> **One swap tool, one add, one remove (2026-10-02).** They used to be ten tools — six swaps named
> after the router functions and an `_eth` twin of each liquidity tool — and the model had to pick
> the function by name. Now it fills in what the user fixed, and the tool picks:
>
> | `swap` gets | native side | router function |
> |---|---|---|
> | `amount_in` | none | `swapExactTokensForTokens` |
> | `amount_out` | none | `swapTokensForExactTokens` |
> | `amount_in` | `token_in` | `swapExactETHForTokens` |
> | `amount_out` | `token_in` | `swapETHForExactTokens` |
> | `amount_in` | `token_out` | `swapExactTokensForETH` |
> | `amount_out` | `token_out` | `swapTokensForExactETH` |
>
> Both amounts or neither, the native asset on both sides, or the same token on both (the native
> asset and its wrapped form count as one: `wrap_eth` wraps, and unwrapping isn't offered) are
> refused before anything is built. "eth" now always means the native asset here: before, passing
> it to a token-to-token swap or as `add_liquidity`'s `token_b` quietly used WETH while the quote
> said ETH. The quote helpers (`_quote_exact_input_swap` / `_quote_exact_output_swap`) are
> unchanged, and `test_custom_tokens` checks that each shape reaches its own package function.

> **Swap-and-send in one transaction.** `swap` takes an optional `recipient`, passed
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
> `transferFrom_erc20` (both sender and recipient), `get_erc20_balance`'s `contact` and the swap
> `recipient` all use it. It exists because `db.get_contact`
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

> **There is no `approve_erc20` tool.** Standing approvals revert on-chain (the module forbids leaving an allowance outstanding), so a standalone approval can never succeed — which is also why `toolkits._BLOCKED_TOOLS` withholds the packages' own `approve` / `approve_token` / `revoke_approval`. The swap and liquidity tools instead receive the approval as `plan["calls"][0]`, already sized to what the router will actually pull, and the plan is sent as one atomic ERC-7579 batch. Exact-output swaps and `addLiquidity` — where the router may pull less than approved — carry a trailing `approve(router, 0)` in the same plan. Each call's `role` (`approve`, `approve_reset`, `swap`, …) records which is which.

### ERC-8004 tools

[`langchain-erc8004`](https://pypi.org/project/langchain-erc8004/) owns the registry ABIs, address resolution, registration-file resolution, the sybil-aware read shapes and all calldata construction. `toolkits.get_erc8004_tools(user_id)` builds the toolkit per user, reading **both registry addresses off the wallet** (`SessionHandler.IDENTITY_REGISTRY()` / `.REPUTATION_REGISTRY()`) rather than from the package's chain table: that is the canonical `0x8004…` pair on Sepolia/BSC but the local mocks on Anvil, and chain 31337 is not in the package's `KNOWN_NETWORKS` at all.

**Who the agent is.** `DeploySHProtocol.s.sol` registers exactly **one** agent per deployment, stores its id in `SHRegistry.agentId`, and leaves the ERC-721 with the deploying operator's key. That agent is the *protocol's* on-chain identity, shared by every wallet on the chain — a user's SessionHandler is a smart account, not an agent, and has no registry entry of its own. This shapes the whole surface: `"protocol"` is the default for every `agent` argument (resolved by `_resolve_agent` via `get_agent_id`, which stays project-side), the user's wallet is always the *reviewer* rather than the subject, and the identity writes are withheld.

| Group | Tools | Exposed to the agent? |
|---|---|---|
| identity reads | `get_registry_info`, `get_agent_identity`, `agent_exists`, `get_agent_metadata`, `verify_agent_endpoint` | yes |
| reputation reads | `get_feedback_clients`, `get_agent_feedback`, `list_all_feedback`, `get_last_feedback_index`, `get_response_count`, `get_agent_reputation` | yes |
| reputation writes | `post_reputation_feedback`, `give_feedback`, `revoke_feedback`, `append_response` | yes |
| identity writes | `register_agent`, `parse_registration_receipt`, `set_agent_uri`, `set_agent_metadata`, `transfer_agent`, and `resolve_registration_file` (it exists to check a file before registering one) | **no** |
| agent wallet | `build_agent_wallet_typed_data`, `set_agent_wallet`, `unset_agent_wallet` | **no** |

15 exposed, 37 agent tools in total (2026-10-02: down from 61). Five of the package's reads are not wrapped at all, because another tool returns what they do: `get_agent_owner`, `get_agent_uri` and `get_agent_wallet` (all three are in `get_agent_identity`, which reports an unreachable registration file as data rather than failing), `get_feedback_summary` (the registry's truncated average — `get_agent_reputation` is the one to show) and `read_feedback` (`get_agent_feedback` with one reviewer lists each entry with its index). Wherever a read takes a reviewer (`client`, `clients`, `responders`), `"me"` is the user's own wallet (`_resolve_reviewer`): the model is never told the wallet's address, and `revoke_feedback` needs the user's own entries to find the index. The feedback writes ask for no yes of their own: the quote is the confirmation, and their docstrings say to state that the write is public and permanent when showing it. Every write returns a plan and is quoted through the same `_quote_plan` as the ERC20/Uniswap tools (via `_quote_registry_plan`, which carries the package's `summary` into the quote), so a registry write is confirmed exactly like a transfer; like every write, none takes the session key — it is read when the user confirms. Every `agent` argument also accepts a bare id or a fully-qualified `eip155:chain:registry:id` reference, so third-party agents can be read and rated.

> **The eight identity writes are defined but withheld from `get_tools()`**, on the same principle as `toolkits._BLOCKED_TOOLS`: a tool that cannot succeed is worse than no tool. Changing the protocol agent is governance done with the operator's key, and a user's wallet cannot own an agent of its own either — `register_agent` mints the ERC-721 to the wallet, and `SessionHandler` installs no ERC-7579 fallback handler for `onERC721Received`, so any mint or `safeTransferFrom` to the account reverts with `ERC7579MissingFallbackHandler(0x150b7a02)`. Each wrapper additionally calls `_reject_protocol_agent_write`, which refuses the protocol's own agent before any calldata is built — so a deployment whose wallet *does* own an agent (or has been granted `setApprovalForAll`) can re-enable them by adding them back to the list, without exposing the protocol identity.

> **The Validation Registry's seven tools are absent too.** ERC-8004's validation registry has no canonical deployment on any chain and this protocol deploys none, so `get_erc8004_tools` passes `validation_registry=None` and the toolkit withholds them.

> **What the port fixed.** `get_agent_reputation` used to call `SessionHandler.getAgentReputation()`, which hardcodes `clients[0] = address(this)` and therefore only ever read back feedback *the wallet itself had given* — a real bug for "how is this service rated". It now aggregates over named reviewers, or over every discovered reviewer flagged as unfiltered. `post_reputation_feedback` passed the user's free-text label into **tag1**, the slot that names the *scale*, leaving every rating invisible to a reader filtering on `"starred"`; it now writes `tag1="starred"` with the label in `tag2`, and takes an optional `agent` so other agents can be rated. Note the `langchain-erc8004.md` spec's claim that the old tool was self-feedback does **not** apply here: `isAuthorizedOrOwner(userWallet, agentId)` is false, so a user rating the protocol's agent is a genuine attributed review, and that stayed the default.

---

## `tx_history.py`

The History tab's record: every transaction made through Mitfah, with its hash, what it did, the
network and when. It lives apart from the chat because the chat is cleared after every transaction
(see Section 3), so a conversation can't be where a hash is kept.

```python
start_assistant_tx(user_id, chain_id, wallet, action, prepared) -> row id   # pending, BEFORE broadcast
finish_assistant_tx(row_id, w3, receipt, succeeded)                      # the executing tx's hash + block time
discard_assistant_tx(row_id)                                             # never executed: nothing to list
record_owner_tx(w3, user_id, chain_id, wallet, tx_hash, receipt=None)    # owner actions and deposits; idempotent
record_deploy(w3, user_id, chain_id, wallet_address, tx_hash, receipt)
describe_wallet_tx(w3, user_id, chain_id, wallet, tx) -> str             # "Withdraw 0.5 ETH to sam", from calldata
settle_pending(user_id, web3_for)                                        # finishes rows left pending
```

- **The assistant's sends are recorded before they go.** `tools.confirm_transaction` writes a
  pending row keyed by the op's `userOpHash` between `prepare_user_op` and `broadcast_user_op`, then
  fills in the hash of the transaction that executed it — a rival's, if one landed the op first.
  An op whose call reverted is on chain and paid for, so it is listed as `failed` with its hash
  (`bundler.UserOpReverted` carries it). One that never executed is removed. One that outlives the
  wait (a `TimeoutError`) stays `pending`. The description is the quote's own `action`, written by
  code from the calldata.
- **Owner transactions are recorded when the app is handed their hash**: by every poll of the three
  web confirms (`/api/deploy/confirm`, `/api/wallet/tx/confirm`, `/api/wallet/session/confirm`) and
  by `POST /api/transactions/deposit` for the Fund drawer. The first poll that can see the
  transaction records it, the one with the receipt settles it, and the unique index stops
  duplicates. Only a transaction sent to the user's own wallet is recorded, and it is described from
  its calldata against the SessionHandler ABI, never from anything the browser says.
- **Settling.** Reading the History tab's first page runs `settle_pending`. An assistant op is
  searched for by its `userOpHash` from `from_block`. If it isn't there and its nonce has since been
  used by another op, it can never land, so it becomes `dropped`. The nonce is read before the log
  search so this op's own late execution can't pass for another's. An owner transaction is settled
  from its receipt, and is `dropped` once the node hasn't known it for a day (a "speed up" in the
  browser wallet replaces it under a new hash).
- **Best effort, always.** Every recording function logs and swallows its own errors. By the time
  one runs, the transaction is already on its way, and an exception would turn a payment that went
  through into one that looks failed — the outcome that leads a user to send it twice.

Not covered: transfers into the wallet from outside Mitfah and anything done directly on chain.
Those would need a block-explorer API or an indexer.

---

## Section 3 — LangChain Agent

`app/smart_wallet_agent.py` wraps the tools in a LangChain agent powered by Claude (`claude-sonnet-4-6` by default).

> **The Anthropic LLM is optional — any LangChain chat model works.** The provider is set in one place: the `llm = ChatAnthropic(...)` call in `smart_wallet_agent.py`. To use a different provider, replace that line with the matching LangChain chat model (e.g. `ChatOpenAI`, `ChatGoogleGenerativeAI`, `ChatOllama`) and its API-key env var, and drop or swap the `AnthropicPromptCachingMiddleware` in `init_agent()` (it is Anthropic-specific). Nothing else in the app is tied to Anthropic — the tools, prompt, and checkpointer are provider-agnostic. `ANTHROPIC_API_KEY` is only needed while the default provider is in use.

```python
def init_agent():
    agent = create_agent(model=llm, tools=get_tools(), system_prompt=SystemMessage(<SYSTEM_PROMPT, cached 1h>), checkpointer=_checkpointer, middleware=[...])
```

**Prompt caching has two points.** The tools and the prompt (~20k tokens since the 2026-10-02 tool review, ~30k before; the same for every user) end in a cache marker of their own with a one-hour lifetime, so every conversation — a new user's, or one started afresh after a transaction — reads that block from the cache. `AnthropicPromptCachingMiddleware` marks the last message (five minutes), which caches each conversation's growing history. Before 2026-10-02 only the second existed, and every new conversation's first call wrote the whole ~33k-token block again (measured: 0 read, 32,828 written; now ~30,300 read, ~400 written). The longer-lived marker has to come first, and it does.

`AsyncSqliteSaver` persists message history keyed by `thread_id(user_id, chain_id)` (e.g. `"1:42161"`), so each user has an isolated, restart-surviving conversation per chain, shared by Telegram and the web app.

### System prompt

The `SYSTEM_PROMPT` teaches the agent the new model up front:

- **One session key, one global USD budget** per rolling window — no per-token limits. The key **expires** (30 days by default, 90 at most) and only one is authorized at a time; `get_wallet_status` reports cap/spent/remaining/watched tokens and when the key runs out.
- **Watched tokens and native value count against the cap;** only unwatched tokens move freely. A swap is charged its **net** value change, not the gross input.
- **Approvals are automatic** — there is no approve step or tool; swap/liquidity tools batch them atomically. A "please approve X" request should be declined with an explanation.
- **Removing liquidity is free** against the cap (it returns value — a net inflow).
- **A paused wallet rejects every transaction** until the owner unpauses it in the web app; the agent can't unpause, and says so.
- **One tool call, one message, one yes.** The agent calls the transaction tool straight away — it resolves the contact, checks the wallet and quotes — and shows the quote with its figures in one message; the user's reply to that is the only yes (swaps state the default 0.5% slippage there instead of asking first). No `preflight_check`, `get_contact`, `get_quote_*` or balance read first: the tool refuses with the reason when a check fails. **Never** estimate swap amounts from prices (`get_quote_in`/`get_quote_out` for questions; a swap's quote carries the router's figures); `"eth"` is the native asset in every token argument, and the wrapped token is named only when meant; never invent addresses.
- **Measured (`make agent-smoke`, Sepolia fork):** a send went from 3 user messages, 8 model calls and 22.6 s to 2 messages, 4 calls and 8.6 s; a swap from 3 messages, 9 calls and 27.7 s to 2, 4 and 9.9 s. With 0.2 s added to every RPC request (to stand in for a live provider), quoting a send went from 37 requests and 7.9 s to 16 and 0.7 s, confirming it from 20 and 4.3 s to 8 and 1.1 s, and quoting a swap from 45 and 9.6 s to 22 and 1.35 s.

The user's message goes to the model verbatim; the `user_id` travels beside it as runtime context (`AgentContext`), so nothing typed can change whose wallet is acted on. `chat(user_id, chain_id, user_input, network)` is the synchronous entry point. It returns the reply as plain text (joining Anthropic content blocks). A failed turn returns a fixed apology, and the exception goes to the log, never to the user: its text can hold RPC URLs with API keys. `get_history(user_id, chain_id, limit)` returns only what was said, never tool traffic, for `GET /api/chat/history`.

### Short memory: the chat starts afresh after each transaction

A wallet assistant needs no long memory, and an unbounded one breaks: every model call re-sends the
whole thread on top of ~20k tokens of prompt and tool descriptions, so a thread kept forever grows
dearer and slower until it overflows the model's window and every turn fails. So at the start of
each turn `chat()` runs `_start_fresh_if_due`, which deletes the thread and starts a new one carrying
only the **text** of the last exchange (the user's last message and the reply; no tool calls,
results or ciphertext, marked `carried_over`). That way a "yes" to the assistant's last question
still makes sense. It does this when:

- the conversation holds a `confirm_transaction` that succeeded (a failed one is kept, so the user
  can ask why), or it has grown past `HISTORY_TOKEN_LIMIT` (40k approximate tokens) — a user who
  only asks questions never sends a transaction;
- **and** the user has no quote waiting for an answer on that chain (`quotes.has_pending`): its
  description and id live only in the conversation, so clearing it would leave the next "yes" with
  nothing to confirm.

It runs at the start of the next turn rather than at the end of the one that sent, so the new
thread is written by the graph itself and the "Sent" reply stays on screen until the user writes
again. Deleting the thread also deletes its old checkpoints. The lasting record is the History tab
(`tx_history.py`), and the prompt tells the agent to point there when asked about older
transactions.

**Deleting on request.** `DELETE /api/chat/history?chain_id=` (or with no chain, every chain) runs
`clear_history`. It deletes the threads and drops the quotes raised in them. The request is refused
with 409 while a turn is running on one of them in this process, because a turn that finishes after
its thread was deleted writes the whole history back. A turn running in the Telegram bot (another
process) can't be seen, so that rare overlap can still undo a delete. The transaction history is
never touched.

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

A daily **`budget_alert`** job (registered per user on `/start`) reads the wallet's on-chain status via `_get_wallet_status` and warns the user on three counts: the session key is inactive (revoked, replaced or already expired), the key expires within **3 days** (`SESSION_EXPIRY_WARN_SECS`), or the remaining budget has dropped below **10%** (`BUDGET_ALERT_THRESHOLD`) of the window cap.

The expiry warning is why this job matters to a Telegram-only user: renewing a key is an owner-signed transaction they can only make in the web app, so learning about it after the key lapsed is learning too late.

`post_init` opens the checkpointer and calls `init_agent()` once before polling. `invoke()` is synchronous and offloaded via `asyncio.to_thread()`; SQLite thread safety is handled in `db.py` via `threading.local()`.

---

## Section 5 — Web Front End

`web/` is a React app served at mitfah.com and the main way people use any of this; Telegram is the
other. It has no privileges of its own: everything goes through `api.py`, with the same `user_id`
rules as the bot, so nothing below this layer has to trust the browser.

What it does that the bot cannot:

- **Signing in is the wallet.** The user connects a browser wallet and signs a SIWE message for this
  site (`GET /api/auth/siwe/nonce`, then `POST /api/auth/siwe/login`); an address's first sign-in
  creates its account. There is no email, password or Google sign-in, and `/signup` redirects to
  `/login`. On a phone that means WalletConnect or the wallet app's own browser.
- **Onboarding is non-custodial.** The user's own browser wallet signs `deployWallet`, so the wallet
  is owned by a key the server has never seen. The API only prepares the transaction and records it
  once the network has it (`POST /api/deploy`, then `POST /api/deploy/confirm`).
- **Owner-only actions.** Pausing, limits, watched tokens, trusted spenders and withdrawals are all
  signed by that same owner key in the browser, each one prepared by its own
  `/api/wallet/<action>/prepare` endpoint and finished with `POST /api/wallet/tx/confirm`. The agent
  has no way to reach them: the module's guard blocks execute-routed admin calls even for the owner
  ([THREAT_MODEL.md](../THREAT_MODEL.md) §3.5).
- **The assistant's key.** Controls turns the assistant off (`removeSession`), and turns it on or
  renews it — the same transaction, since every grant mints a new key lasting 30 days. These finish
  with `POST /api/wallet/session/confirm` instead, which files the new key or forgets the revoked
  one once the wallet is seen to have changed. `GET /api/wallet/{chain_id}` runs the same
  reconciliation, so a grant whose tab was closed before it confirmed is still picked up the next
  time anyone looks at the wallet. The dashboard, the chat page and Controls all warn once fewer
  than three days are left (`session.needs_renewal`), and say so once the key has run out.
- **Every confirm says whether the network has the transaction.** The four confirms
  (`/api/deploy/confirm`, `/api/wallet/tx/confirm`, `/api/wallet/session/confirm`, and
  `POST /api/transactions/deposit` for the Fund drawer) answer 202 while it is pending, with
  `seen: false` while the API's node has never seen the hash. The page then says it may never arrive
  (the wallet failed to send it, sent it through another RPC, or replaced it) instead of waiting out
  its deadline. A deploy the user's wallet sped up never mines under the hash the page holds, so when
  the owner's wallet is already at the predicted address `/api/deploy/confirm` finishes it from the
  chain. It lists it under the hash that created it, found by halving the last million blocks to the
  one the wallet's code appears in and reading that block's `WalletDeployed` log (providers cap log
  searches: Alchemy's free tier at 10 blocks). Only the caller's own wallet qualifies: the owner
  check and the pending key that only `/api/deploy` for that account mints.

- **The History tab.** Every transaction made through Mitfah, newest first, from
  `GET /api/transactions` (paged by `before`, filterable by `chain_id`): what it did, the date and
  time in the reader's time zone, the network, who sent it, its status, and its hash as a link to the
  network's live block explorer. That includes forks, which link where the live network would. The
  Fund drawer follows its deposits through `POST /api/transactions/deposit`, which lists them. The
  Assistant page's "Clear chat" and Settings' "Delete all" call `DELETE /api/chat/history`.

Contacts are **web-only**: the list is the destination allowlist, so the agent reads it and can
never write to it, and adding one takes the owner wallet's EIP-712 signature (the dialog's last step). The chat page is a front end over `chat(user_id, chain_id, …, network)` — the same agent,
the same history, shared with Telegram through the checkpointer's `thread_id`. The turn acts on
the page's network, and every quote names the network it would run on (`network`).

Setup, scripts, the production settings and the front end's own layout are in
[web/README.md](../web/README.md).