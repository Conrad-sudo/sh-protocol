# Agentic Wallet Infrastructure 🤖⛓️ 



A full-stack smart wallet system built on ERC-4337 account abstraction. The wallet is controlled by delegated session keys — scoped, time-limited, and spending-capped authorizations that allow an AI agent to sign transactions on behalf of the owner through natural language command without ever exposing the owner's private key.

The system is composed of five layers: Solidity smart contracts, a Python blockchain interface, HashiCorp Vault (key custody), a LangChain AI agent, and a Telegram bot front end. It also integrates the **ERC-8004** canonical on-chain agent identity and reputation registries — the agent holds an ERC-721 identity NFT minted on the `AgentIdentityRegistry`, and users can post on-chain feedback through the `ReputationRegistry` via a dedicated session key.

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

**Network support:** Anvil (local), mainnet fork, Sepolia fork, and live Sepolia testnet. On fork/local networks, UserOps are submitted directly via `handleOps()`. On live networks (Sepolia), they are submitted through an Alchemy bundler using `eth_sendUserOperation`.

---

## Prerequisites

- [Foundry](https://book.getfoundry.sh/getting-started/installation) — contract compilation and testing
- [Anvil](https://book.getfoundry.sh/anvil/) — local EVM node (ships with Foundry)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — **required** to run HashiCorp Vault locally
- Python 3.12+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An API key for your chosen LLM provider (default: Anthropic — see [supported providers](https://python.langchain.com/docs/integrations/chat/))
- An Alchemy API key (required for live Sepolia deployment and mainnet fork)

---

## Clone and Install

```bash
git clone https://github.com/Conrad-sudo/agentic-wallet-infra.git
cd session-key-infra
```

**Foundry dependencies:**

```bash
forge install 
```

**Python dependencies:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> `python-telegram-bot[job-queue]` pulls in APScheduler alongside the bot library, which is required for session expiry alerts and recurring transfers.

---

## Environment Variables

Create a `.env` file in the project root:

```env
# Signing keys
ANVIL_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
SEPOLIA_PRIVATE_KEY=your_sepolia_deployer_private_key_here

# Deployer wallet address (used by HelperConfig on live networks)
SEPOLIA_ACCOUNT=your_deployer_wallet_address_here

# Bundler keys (sign the outer handleOps transaction)
ANVIL_BUNDLER=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a
MAINNET_BUNDLER=your_mainnet_bundler_private_key_here
SEPOLIA_BUNDLER=your_sepolia_bundler_private_key_here

# RPC endpoints
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/your_alchemy_key
SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/your_alchemy_key

# AI / bot credentials
ANTHROPIC_API_KEY=your_anthropic_api_key_here
TELEGRAM_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# HashiCorp Vault — populated automatically by `make vault`
VAULT_ADDR=http://127.0.0.1:8200
VAULT_ROLE_ID=
VAULT_SECRET_ID=
VAULT_SECRET_ID_ACCESSOR=

# Optional — for Etherscan contract verification after Sepolia deployment
ETHERSCAN_API_KEY=your_etherscan_api_key_here
```

> `SEPOLIA_ACCOUNT` is the public Ethereum address corresponding to `SEPOLIA_PRIVATE_KEY`. It is used by `HelperConfig.s.sol` as the deployer account on live networks. Load it via `vm.envAddress("SEPOLIA_ACCOUNT")` — do not hardcode it.
>
> `ANVIL_PRIVATE_KEY` and `ANVIL_BUNDLER` are Anvil's default account 0 and account 2 keys. They are public and safe to use locally only.
>
> `SEPOLIA_PRIVATE_KEY` and `SEPOLIA_BUNDLER` must be funded with Sepolia ETH before deployment. They can be the same key.
>
> `MAINNET_BUNDLER` and `SEPOLIA_BUNDLER` are only required for the respective network. On fork networks (`mainnet-fork`, `sepolia-fork`), the bundler is funded programmatically by `prefund()`.
>
> `ETHERSCAN_API_KEY` is optional. If not set, `deploy.py` skips contract verification and prints a notice.

---

## Vault & Docker: Session Key Security Model

Session keys are cryptographic private keys that authorize an AI agent to sign ERC-4337 `UserOperation`s. Storing them on disk in plaintext is unacceptable — a single database breach would expose all keys for all users. This system uses **HashiCorp Vault Transit** (encryption-as-a-service) so that raw key material never touches disk.

### Threat model

| Attacker | Obtains | Impact |
|---|---|---|
| Breaches the SQLite database | `key_ciphertext` blobs | Zero — ciphertexts require Vault's Transit key to decrypt |
| Breaches Vault | AES-256 Transit key | Zero — the attacker still needs the specific ciphertexts from the DB |
| Breaches both simultaneously | Both | Full key compromise — both must be protected |

This is a **2-of-2 security model**: the database holds ciphertexts, Vault holds the decryption key. Neither is sufficient alone.

### How Docker runs Vault

Vault is started as a Docker container exposing port 8200. Dev mode is used for local development — all state is in-memory and the root token is `dev-root-token`.

> **`dev-root-token` is the well-known Vault dev mode default. It is hardcoded in `setup_vault.sh` and is intentionally insecure — do not use it outside of a local development environment. In production, revoke the root token entirely after initial setup and authenticate exclusively via AppRole or another auth method backed by a persistent storage backend.**

A custom AppRole (`wallet-agent`) is provisioned with narrowly scoped policy:

```hcl
path "transit/encrypt/session-keys" { capabilities = ["update"] }
path "transit/decrypt/session-keys" { capabilities = ["update"] }
```

The AppRole issues short-lived tokens (1-hour TTL, 4-hour max). `vault_signer.py` authenticates with `VAULT_ROLE_ID` + `VAULT_SECRET_ID` and creates a fresh authenticated client on every call — there is no long-lived token cached in the process.

### Step 1 — Start the Vault container

```bash
docker run -d \
  -e VAULT_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e SKIP_SETCAP=true \
  -p 8200:8200 \
  --name vault-dev \
  hashicorp/vault:latest
```

### Step 2 — Configure Vault and inject credentials into `.env`

```bash
make vault
```

`setup_vault.sh` runs the full configuration sequence inside the container — enabling the Transit secrets engine, creating the `session-keys` AES-256-GCM96 key, enabling AppRole auth, writing the `wallet-agent` policy, and generating a fresh `role_id` / `secret_id` pair. It then writes all four Vault variables (`VAULT_ADDR`, `VAULT_ROLE_ID`, `VAULT_SECRET_ID`, `VAULT_SECRET_ID_ACCESSOR`) directly into `.env`. No manual shell commands required.

> Save the `VAULT_SECRET_ID_ACCESSOR` separately — you need it to revoke or rotate the secret without the secret itself.

### HVAC client: `vault_signer.py`

The Python interface to Vault is a thin wrapper around the [hvac](https://hvac.readthedocs.io/) library. It exposes exactly two functions consumed by `anvil.py` and `live_network.py`:

```python
def encrypt_key(raw_key: bytes) -> str:
    # Base64-encodes raw_key → Vault Transit encrypt → returns 'vault:v1:...' ciphertext

def decrypt_key(ciphertext: str) -> bytes:
    # Vault Transit decrypt → returns raw 32-byte key material
```

`_client()` performs AppRole login on every call, returning a fresh authenticated `hvac.Client`. The Transit key named `session-keys` lives inside Vault and is never exported or readable — it can only be referenced by the encrypt/decrypt API.

### Key lifecycle in `anvil.py`

```
generate: secrets.token_bytes(32)
    │
    ▼
derive address: w3.eth.account.from_key(raw_key)
    │
    ▼
encrypt: vault_signer.encrypt_key(raw_key)  →  ciphertext stored in session_keys table
    │
    ▼
wipe: raw_key = b"\x00" * 32
```

At signing time the raw key is decrypted transiently inside `create_signed_user_op` and wiped in a `finally` block immediately after the EIP-191 signature is produced. The key exists in process memory only for the milliseconds between `decrypt_key()` and the `finally` wipe:

```python
raw_key = decrypt_key(key_ciphertext)
try:
    signed = w3.eth.account.sign_message(encode_defunct(user_op_hash), private_key=raw_key)
    return user_op[:-1] + (signed.signature,)
finally:
    raw_key = b"\x00" * len(raw_key)
    del raw_key
```

The opaque ciphertext (`vault:v1:...`) travels through the tool chain as a string argument. The LLM passes it between tool calls but never sees or logs the raw key material.

### Important operational notes

> **Dev mode is ephemeral.** All Vault state (keys, AppRole config, policies) is in-memory. A container restart wipes everything. Re-run `make vault` after any restart. Sleep/suspend preserves state; a full restart does not.

> **Production readiness:** Before going to production, replace dev mode with a real Vault server backed by a persistent storage backend, enable auto-unseal via a cloud KMS (AWS KMS, GCP Cloud KMS, or Azure Key Vault), revoke the root token, and enforce TLS. See the [Vault production hardening guide](https://developer.hashicorp.com/vault/tutorials/operations/production-hardening).

---

## Section 1 — Smart Contract Architecture

The smart contract layer implements ERC-4337 account abstraction with delegated, scoped session keys. It is composed of two core contracts, a shared constants file, deployment scripts, a configuration helper, and a comprehensive test suite.

### Contract Overview

```
src/
├── SessionHandler.sol           ← ERC-4337 smart account with session key logic
├── PriceOracle.sol              ← Chainlink-based USD value converter
├── Constants.sol                ← Shared mainnet/Sepolia contract addresses
├── AgentIdentityRegistry.sol    ← Local ERC-8004 identity registry scaffold (Anvil)
├── ReputationRegistry.sol       ← Local ERC-8004 reputation registry scaffold (Anvil)
├── interfaces/
│   ├── IWETH.sol                ← WETH interface (extends IERC20Extended)
│   ├── IERC20Extended.sol       ← IERC20 + IERC20Metadata combined interface
│   ├── IIdentityRegistry.sol    ← ERC-8004 IIdentityRegistry interface
│   └── IReputationRegistry.sol  ← ERC-8004 IReputationRegistry interface
└── mocks/
    ├── ERC20Mock.sol         ← Mintable ERC20 for local testing
    ├── MockV3Aggregator.sol  ← Chainlink price feed mock for Anvil
    └── MockWeth.sol          ← WETH mock with deposit/withdraw for Anvil

script/
├── DeploySessionHandler.s.sol  ← Deployment entry point
├── DeployAgentRegistry.s.sol   ← Deploys local AgentIdentityRegistry (Anvil)
├── HelperConfig.s.sol          ← Chain-specific configuration resolver
├── SendPackedUserOp.s.sol      ← UserOp construction and signing helper
└── FundSessionHandler.s.sol    ← One-off ETH funding script

test/
├── unit/
│   ├── TestSessionHandler.t.sol    ← Full SessionHandler unit test suite
│   └── SessionHandlerHarness.sol   ← Test harness exposing internal functions
├── fork/
│   ├── UniswapV2Test.t.sol              ← Uniswap V2 integration tests (mainnet fork)
│   └── SessionHandlerSepoliaTest.t.sol  ← SessionHandler integration tests (Sepolia fork)
└── invariant/
    ├── InvariantSessionHandler.t.sol  ← Stateful invariant tests
    └── SessionHandlerHandler.sol      ← Action handler for fuzzing
```

---

### `Constants.sol`

Shared Solidity constants for all well-known contract addresses used across the project, importable by any contract or script. Constants are grouped into four sections: Uniswap infrastructure, ERC-4337 EntryPoint, Sepolia tokens and price feeds (prefixed `SEPOLIA_`), and mainnet tokens and price feeds (prefixed `MNT_`).

```solidity
// ERC-4337
address constant ENTRYPOINT_V07      = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;

// Uniswap
address constant UNISWAP_V2_ROUTER_02 = 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D;
address constant UNISWAP_V2_FACTORY   = 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f;

// ERC-8004 canonical registries
address constant SEPOLIA_IDENTITY_REGISTRY   = 0x8004A818BFB912233c491871b3d84c89A494BD9e;
address constant SEPOLIA_REPUTATION_REGISTRY = 0x8004B663056A597Dffe9eCcC1965A193B7388713;
address constant MNT_IDENTITY_REGISTRY       = 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432;
address constant MNT_REPUTATION_REGISTRY     = 0x8004BAa17C55a88189AE136b182e5fdA19dE9b63;

// Sepolia tokens (representative sample)
address constant SEPOLIA_USDC = 0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238;
address constant SEPOLIA_WETH = 0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14;
address constant SEPOLIA_ETH_USD_PRICE_FEED = 0x694AA1769357215DE4FAC081bf1f309aDC325306;
// ... plus SEPOLIA_DAI, SEPOLIA_USDT, SEPOLIA_AAVE, SEPOLIA_LINK, SEPOLIA_WBTC, SEPOLIA_UNI
// ... and SEPOLIA_USDC/DAI/LINK/BTC_USD_PRICE_FEED

// Mainnet tokens (representative sample)
address constant MNT_USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
address constant MNT_WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
address constant MNT_ETH_USD_PRICE_FEED = 0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419;
// ... plus 23 more MNT_ token constants and 25 MNT_*_USD_PRICE_FEED constants
```

---

### `SessionHandler.sol`

The `SessionHandler` is an ERC-4337-compliant smart account (implements `IAccount`) that supports both owner-signed and session-key-signed `UserOperation`s. It inherits `Ownable`, `ReentrancyGuard`, and `Pausable` from OpenZeppelin.

**Key features:**

| Feature | Detail |
|---|---|
| ERC-4337 v0.7 compatible | Implements `validateUserOp` with packed gas fields |
| Session time windows | 48-bit `validFrom` / `validUntil` timestamps |
| Spending limits | USD-denominated per-session cumulative cap via `PriceOracle` |
| Selector whitelisting | O(1) `mapping(address => mapping(bytes4 => bool))` lookup |
| Uniswap V2 support | Assembly-based calldata parsing for 4 token-input swap functions plus `addLiquidity`, `addLiquidityETH`, `removeLiquidity`, `removeLiquidityETH`; ETH-input swaps (`swapETHForExactTokens`, `swapExactETHForTokens`) are budget-accounted via the `value` field |
| Native ETH sends | `address(0)` session target sentinel — allows ETH transfers to arbitrary recipients |
| Owner revocation | `revokeSessionKey` cleans up mappings and resets storage |
| Owner withdrawal | `withdraw(token, amount)` allows the owner to pull ERC20 tokens or ETH from the wallet |

**Session struct:**

```solidity
struct Session {
    bool active;            // session control switch (auto-activates when validFrom passes)
    address target;         // whitelisted target contract; address(0) = native ETH send
    uint48 validFrom;       // activation timestamp
    uint48 validUntil;      // expiry timestamp
    uint256 spendingLimit;  // max cumulative USD spend (18 decimals)
    uint256 spentAmount;    // running total of USD spent
    bytes4[] selectors;     // whitelisted function selectors
}
```

**EIP-1153 transient storage bridge:**

`validateUserOp` and `execute` run as two separate calls within the same `handleOps` transaction. Two EIP-1153 transient slots bridge the two steps — storing just enough context for `execute` to identify which session is active and which selector was authorized:

```solidity
address transient t_pendingSessionKey;
bytes4  transient t_pendingSelector;
```

USD computation is deferred entirely to `execute()` via `_computeUSDValue()`, because oracle reads (external storage) are forbidden during validation. The transient slots are zeroed automatically at transaction end — no manual cleanup required.

**Signature validation flow (`validateUserOp`):**

1. Recover the signer from the EIP-191 wrapped `userOpHash` using ECDSA.
2. If signer is the **owner** — set `t_pendingSessionKey = address(0)`, return `SIG_VALIDATION_SUCCESS` immediately.
3. If signer is in `sessionExists` — call `_validateSession`:
   - If `data.length == 0 && value > 0` — this is a native ETH send; assert the session target is `address(0)` and `_isSessionUsable` (budget check without oracle). Write `t_pendingSessionKey` and `bytes4(0)` to transient storage. Return packed validation data with time bounds.
   - Otherwise, decode `callData[4:]` to extract `dest`, `value`, and inner `data`.
   - Assert `dest` matches the session's whitelisted `target`.
   - Extract the 4-byte selector via inline assembly.
   - Assert `_isSessionUsable(signer)` (own-storage only — no oracle, no `block.timestamp`) and `isSelectorAllowed[signer][selector]`.
   - Write `t_pendingSessionKey` and `t_pendingSelector` to transient storage via `_setPendingSession`. No USD amounts are computed here.
   - Return packed validation data with time bounds.
4. Otherwise — return `SIG_VALIDATION_FAILED`.

**USD computation and budget enforcement (`execute`):**

When `execute` is called by the EntryPoint and `t_pendingSessionKey != address(0)`, it calls `_computeUSDValue(dest, value, data, selector)` — which performs all oracle reads and assembly calldata parsing — to produce `(debitValueInUSD, creditValueInUSD)`. The spending limit is then checked and `spentAmount` updated before the inner call dispatches. This deferral to `execute` is necessary because `SLOAD` on external contracts (oracle) is forbidden during validation.

**`removeLiquidity` budget accounting:**

For `removeLiquidity` and `removeLiquidityETH`, `_computeUSDValue` returns a non-zero `creditValueInUSD` (computed from `amountAMin`/`amountBMin`/`amountETHMin`) and a zero `debitValueInUSD`. In `execute`, rather than charging the session, the system credits back up to the current `spentAmount`. This prevents LP removal from being double-counted as a spend, since the user is recovering value, not spending it.

**Key functions:**

```solidity
// Add a scoped session key (owner only)
function addSessionKey(
    address sessionKey,
    address target,
    bytes4[] calldata selectors,
    uint48 validFrom,
    uint48 validUntil,
    uint256 spendingLimit
) external onlyOwner;

// Revoke an active session key (owner only)
function revokeSessionKey(address sessionKey) public onlyOwner;

// Execute an arbitrary call (EntryPoint or owner only)
function execute(address dest, uint256 value, bytes calldata data)
    external onlyEntryPointOrOwner whenNotPaused;

// Withdraw ERC20 tokens or ETH to the owner (owner only)
function withdraw(address token, uint256 amount) external onlyOwner;

// View helpers
function getSession(address sessionKey) public view returns (Session memory);
function isSessionActive(address sessionKey) public view returns (bool);
function getRemainingBudget(address sessionKey) public view returns (uint256);
function isSpendingWithinBudget(address sessionKey, address token, uint256 amount) public view returns (bool);
function getPrice(address token) public view returns (uint256 price, uint8 decimals);
```

---

### `PriceOracle.sol`

The `PriceOracle` converts ETH and ERC20 token amounts into real-time USD values using Chainlink price feeds. It is used by `SessionHandler._computeUSDValue` (called from `execute()`) to enforce USD-denominated spending limits rather than raw token amounts.

This design accounts for stablecoin depeg events (e.g., USDC at $0.87 during the March 2023 SVB crisis) by querying actual market prices rather than assuming a 1:1 peg.

**Supported tokens (21 registered by Forge deploy script; 26 in mainnet `HelperConfig`):** ETH (via `address(0)`), USDC, DAI, WETH, USDT, AAVE, LINK, 1INCH, APE, ARB, BNB, WBTC, COMP, CRV, ENS, MKR, SAND, SUSHI, wTAO, UNI, YFI. The mainnet `HelperConfig` additionally provides WAVAX, BAT, IMX, KNC, and RDNT (5 extra feeds), giving 26 total. The Forge `DeploySessionHandler.s.sol` script registers only the first 21; the Python `deploy.py` path registers all 26 on mainnet.

Token-to-feed mappings are stored in `mapping(address => address) private s_priceFeed`, and per-feed staleness thresholds in `mapping(address => uint256) private s_heartbeat`. Both are keyed by feed address and set at construction time via parallel `tokens[]`, `priceFeeds[]`, and `heartbeats[]` arrays. Token decimals are read dynamically via `IERC20Metadata.decimals()`. Entries with `address(0)` feeds are silently skipped, allowing safe deployment on chains with partial feed availability.

**Per-feed staleness protection:** Reverts with `PriceOracle_StalePrice` if a feed has not updated within its registered heartbeat. Heartbeats reflect real Chainlink update schedules — 1 hour for ETH, BTC, AAVE, LINK, DAI, COMP, MKR, UNI, and WETH; 23 hours for USDC; 24 hours for all other feeds.

```solidity
constructor(
    address[] memory tokens,
    address[] memory priceFeeds,
    uint256[] memory heartbeats
);

function getUSDValue(address token, uint256 amount) external view returns (uint256);
function getPrice(address token) external view returns (uint256 price, uint8 decimals);
```

---

### ERC-8004 Contracts

The project includes on-chain agent identity and reputation infrastructure following the **ERC-8004** standard.

**`src/AgentIdentityRegistry.sol`** — local scaffolding for a deployable ERC-8004 identity registry on Anvil. Inherits `ERC721` and `ERC721URIStorage`. On live networks (Sepolia, mainnet) the canonical UUPS-upgradeable registries already deployed by the ERC-8004 working group are used instead. The local version is deployed during `deploy_session_handler_anvil` so that Anvil-based flows can exercise the full registration path without requiring a network connection.

**`src/ReputationRegistry.sol`** — local scaffolding for the ERC-8004 Reputation Registry on Anvil. Deployed alongside `AgentIdentityRegistry` on local networks.

**`src/interfaces/IIdentityRegistry.sol`** — the canonical ERC-8004 identity interface. Exposes `register()` (three overloads — no URI, URI-only, and URI + metadata), `setAgentURI`, `setMetadata`, `getMetadata`, `setAgentWallet`, `getAgentWallet`, `unsetAgentWallet`, `tokenURI`, `ownerOf`, `balanceOf`, and `isAuthorizedOrOwner`.

**`src/interfaces/IReputationRegistry.sol`** — the canonical ERC-8004 reputation interface. Exposes `giveFeedback` (stores a scored, tagged, hash-anchored feedback entry) and `getSummary` / `readAllFeedback` for querying aggregated or per-client scores.

**`script/DeployAgentRegistry.s.sol`** — Foundry broadcast script that deploys `AgentIdentityRegistry` to Anvil for local testing.

**Canonical network addresses:**

| Contract | Sepolia | Mainnet |
|---|---|---|
| `IdentityRegistry` | `0x8004A818BFB912233c491871b3d84c89A494BD9e` | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| `ReputationRegistry` | `0x8004B663056A597Dffe9eCcC1965A193B7388713` | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` |

**`interface/register_agent.py`** — one-time operator script that mints the agent's ERC-721 identity NFT. Signs with `SEPOLIA_PRIVATE_KEY` (or `ANVIL_PRIVATE_KEY` for local networks), calls `register(card_uri)` on the `AgentIdentityRegistry`, and saves the returned `agentId` to `wallet.db`. The `agentId` is used by `get_agent_identity` and `get_agent_reputation` tools at runtime.

```bash
# Register on Sepolia (one-time):
python interface/register_agent.py

# If already registered on-chain but DB is empty (e.g. after cloning):
python interface/register_agent.py --save-id <agent_id> sepolia-fork
```

**`interface/agent_card.json`** — the agent card following the `erc-8004/v1` schema. Describes the agent's `name`, `description`, `version`, `walletAddress`, `capabilities`, `trustModel`, and `network`. Hosted publicly and referenced by the on-chain `tokenURI`.

---

### `HelperConfig.s.sol`

`HelperConfig` resolves chain-specific deployment parameters at runtime, keeping deployment and test scripts chain-agnostic.

| Network | Chain ID | EntryPoint |
|---|---|---|
| Ethereum Mainnet | 1 | `ENTRYPOINT_V07` (canonical) |
| Ethereum Sepolia | 11155111 | `ENTRYPOINT_V07` (canonical) |
| Anvil (local) | 31337 | Freshly deployed, cached per session |

For local Anvil runs, `HelperConfig` deploys a fresh `EntryPoint`, 25 token mocks (24 ERC20Mock + 1 MockWeth, including USDT, WAVAX, BAT, IMX, KNC, and RDNT), and 25 MockV3Aggregator price feeds, then caches the result so repeated calls do not redeploy. Mock prices are set to approximate real-world values (e.g. ETH at $1000, USDC at $0.998). All mock feeds use a 1-hour heartbeat on Anvil.

For Sepolia, `HelperConfig` returns live testnet token and Chainlink feed addresses. Only a subset of tokens have Sepolia deployments (WETH, LINK, USDC, DAI, USDT, WBTC, AAVE, UNI) — all others are set to `address(0)` and silently skipped by `PriceOracle`. Note: USDT, AAVE, and UNI have Sepolia token addresses but their Chainlink price feeds are `address(0)` on Sepolia, so they are registered as tokens but skipped during oracle construction. Uniswap V2 is not officially deployed on Sepolia, so `uniswapRouter` is `address(0)`.

`getMainnetConfig()` sets `account` to `ANVIL_BURNER_WALLET` (Anvil's default account 0: `0xf39F...2266`) because this config is primarily used for mainnet-fork testing against a local Anvil node, where that key is pre-funded. **Before deploying to live mainnet, replace `ANVIL_BURNER_WALLET` with a real funded EOA address** — the Anvil default key has no funds on mainnet and its private key is public.

The `NetworkConfig` struct carries per-token heartbeat values for all supported feeds alongside their token and price feed addresses.

---

### `DeploySessionHandler.s.sol`

The Foundry deployment script orchestrates the full contract deployment sequence:

1. Instantiates `HelperConfig` to resolve chain-specific addresses.
2. Deploys `PriceOracle` with parallel token, feed address, and heartbeat arrays.
3. Deploys `SessionHandler` with the `EntryPoint`, `PriceOracle`, and Uniswap V2 Router addresses.
4. On Anvil only: mints 1,000 WETH, 10,000 USDC (6 decimals), 20,000 USDT (18 decimals), 10,000 DAI, and 2,000 of each other supported token into the `SessionHandler`, and sends 10 ETH via `vm.deal`.

```solidity
function run() external returns (SessionHandler, HelperConfig.NetworkConfig memory, PriceOracle);
```

---

### `SendPackedUserOp.s.sol`

A reusable script helper for constructing signed `PackedUserOperation`s, used by both the test suite and deployment scripts.

**Two signing modes:**

- **Owner mode** — pass `sessionSigner = address(0)` and `sessionSignerKey = 0`. Uses the `HelperConfig` account (or the Anvil default key on chain 31337).
- **Session key mode** — pass a valid `sessionSigner` address and its `sessionSignerKey`. Signs the UserOp as the session key.

**Signing flow:**

1. Fetch nonce from `EntryPoint.getNonce(sender, 0)`.
2. Build an unsigned `PackedUserOperation` with hardcoded gas parameters.
3. Get `userOpHash` from `EntryPoint.getUserOpHash(userOp)`.
4. Wrap in EIP-191 envelope via `toEthSignedMessageHash`.
5. Sign the digest with `vm.sign(privateKey, digest)`.
6. Attach `(r, s, v)` signature to the UserOp.

---

### Test Suite

**`test/unit/TestSessionHandler.t.sol`** — comprehensive coverage of the full `SessionHandler` lifecycle.

| Category | Coverage |
|---|---|
| Access Control | Owner/non-owner permissions for `execute`, `pause`, `addSessionKey`, `revokeSessionKey` |
| Session Validation | Time bounds, target matching, selector whitelisting, spending limits, stale prices |
| Signature Recovery | ECDSA recovery for owner and session key UserOps |
| ERC-4337 Flow | End-to-end `EntryPoint.handleOps()` for both owner and session key |
| Session Lifecycle | Activation, expiry, auto-revocation, budget exhaustion |
| View Functions | `isSessionActive`, `getRemainingBudget`, `getSession`, `isSpendingWithinBudget` |
| Events | `SessionAdded` and `SessionRevoked` emissions |

Tests that use `vm.warp` call `_refreshMockFeeds()` after warping to reset the `updatedAt` timestamp on all `MockV3Aggregator` instances. This prevents false `PriceOracle_StalePrice` reverts caused by the per-feed heartbeat check — which fires even for sessions that have already expired, since the oracle is called before `isSessionActive` in the validation flow.

**`test/fork/UniswapV2Test.t.sol`** — integration tests for all six Uniswap V2 swap functions against a live mainnet fork. Uses `vm.mockCall` on `latestRoundData` to keep all Chainlink feeds appearing fresh after `vm.warp`. Real fork feeds cannot have `updateAnswer` called directly, so mock-call patching is used instead.

| Test | Swap Function |
|---|---|
| `testSwapExactTokensForTokensWithSession` | `swapExactTokensForTokens` |
| `testSwapTokensForExactTokensWithSession` | `swapTokensForExactTokens` |
| `testSwapEthForExactTokensWithSession` | `swapETHForExactTokens` |
| `testSwapExactTokensForETHWithSession` | `swapExactTokensForETH` |
| `testSwapTokensForExactETHWithSession` | `swapTokensForExactETH` |
| `testSwapExactETHForTokensWithSession` | `swapExactETHForTokens` |

**`test/fork/SessionHandlerSepoliaTest.t.sol`** — integration tests against a live Sepolia fork. Tests the ETH transfer and ERC20 transfer (LINK) flows using real Sepolia token addresses and Chainlink feeds.

| Test | Description |
|---|---|
| `testSendingEthWithSession` | Sends 1 ETH via an ETH-session key; verifies budget deduction and recipient balance |
| `testTransferERC20WithSession` | Transfers 20 LINK via a LINK-session key; verifies recipient balance |

**`test/unit/SessionHandlerHarness.sol`** — test harness that inherits `SessionHandler` and re-exports internal functions (e.g. `_packValidationData`) as external for unit testing.

**`test/invariant/`** — stateful invariant tests using Foundry's fuzzer. `SessionHandlerHandler` defines valid actions; `InvariantSessionHandler` asserts invariants (e.g. `spentAmount` never exceeds `spendingLimit`) hold across arbitrary action sequences.

---

### Foundry Commands

**Build contracts:**

```bash
forge build
```

**Run all tests:**

```bash
forge test
```

**Run tests with verbose output:**

```bash
forge test -vvvv
```

**Run a specific test:**

```bash
forge test --match-test testFunctionName -vvvv
```

**Unit tests:**

```bash
forge test --match-path test/unit/TestSessionHandler.t.sol
```

**Invariant tests:**

```bash
forge test --match-path test/invariant/InvariantSessionHandler.t.sol
```

**Fork test — Uniswap V2 integration (mainnet fork):**

```bash
forge test --match-path test/fork/UniswapV2Test.t.sol --fork-url $MAINNET_RPC_URL
```

**Fork test — Sepolia integration:**

```bash
forge test --match-path test/fork/SessionHandlerSepoliaTest.t.sol --fork-url $SEPOLIA_RPC_URL
```

**Start a local Anvil node:**

```bash
make anvil
```

**Start a mainnet fork:**

```bash
make mainnet-fork
```

**Start a Sepolia fork:**

```bash
make sepolia-fork
```

**Deploy to Sepolia:**

```bash
forge script script/DeploySessionHandler.s.sol \
  --rpc-url $SEPOLIA_RPC_URL \
  --account <keystore-account> \
  --broadcast \
  --verify
```

---

## Section 2 — Blockchain Interface

The `interface/` directory contains the Python layer that bridges the AI agent to the on-chain contracts. It is built on [web3.py](https://web3py.readthedocs.io/) and uses SQLite (`wallet.db`) for persistent off-chain state.

```
interface/
├── constants.py           ← Chain IDs, addresses, Chainlink heartbeats, ERC-8004 registry addresses
├── db.py                  ← SQLite data layer (all reads/writes to wallet.db)
├── network_config.py      ← Web3 connection factory
├── contracts.py           ← Contract loading with per-chat_id caching
├── anvil.py               ← Session key management and UserOp execution (local/fork)
├── live_network.py        ← UserOp execution via Alchemy bundler (live networks)
├── vault_signer.py        ← HashiCorp Vault Transit encrypt/decrypt wrapper
├── deploy.py              ← Deployment and session registration scripts
├── register_agent.py      ← One-time script to mint the agent's ERC-8004 identity NFT
├── tools.py               ← LangChain tool wrappers for the AI agent
├── smart_wallet_agent.py  ← LangChain agent and system prompt
├── telebot.py             ← Telegram bot front end
├── agent_card.json        ← ERC-8004/v1 agent card (hosted publicly, referenced by tokenURI)
├── artifacts/
│   ├── SessionHandler.json        ← ABI for SessionHandler
│   ├── EntryPoint.json            ← ABI for EntryPoint
│   ├── IERC20Extended.json        ← ABI for ERC20 tokens
│   ├── IWETH.json                 ← ABI for WETH
│   ├── IUniswapV2Router02.json    ← ABI for Uniswap V2 Router
│   ├── IUniswapV2Factory.json     ← ABI for Uniswap V2 Factory
│   ├── IUniswapV2Pair.json        ← ABI for Uniswap V2 Pair
│   ├── ERC20Mock.json             ← ABI for ERC20Mock (Anvil deployment)
│   ├── MockV3Aggregator.json      ← ABI for MockV3Aggregator (Anvil deployment)
│   ├── IdentityRegistry.json      ← ABI for canonical ERC-8004 IdentityRegistry (Sepolia/mainnet)
│   └── ReputationRegistry.json    ← ABI for canonical ERC-8004 ReputationRegistry (Sepolia/mainnet)
└── migrate/
    ├── Chains.json                ← Chain name → chain ID mapping
    ├── RPC.json                   ← Chain name → RPC URL mapping
    ├── ERC20_Selectors.json       ← ERC20 function name → selector
    ├── UniswapV2_Selectors.json   ← Uniswap V2 function name → selector
    ├── Registry_Selectors.json    ← ERC-8004 ReputationRegistry function name → selector
    ├── Mainnet_Tokens.json        ← Token ticker → mainnet address
    ├── Mainnet_Pricefeeds.json    ← Token → Chainlink feed address (mainnet)
    ├── Sepolia_Tokens.json        ← Token ticker → Sepolia address (WETH, LINK)
    └── Sepolia_Pricefeeds.json    ← Token → Chainlink feed address (Sepolia)
```

---

### Module Dependency Flow

```
telebot.py ──────────► smart_wallet_agent.py ──► tools.py ──► contracts.py ──► network_config.py ──► db.py
                                                  tools.py ──► anvil.py ─────► vault_signer.py
                                                                          ─────► network_config.py
                                                                          ─────► db.py
                                                  tools.py ──► live_network.py ► vault_signer.py
                                                                                ► network_config.py
                                                                                ► contracts.py
                                                  tools.py ──► db.py
deploy.py ─────────────────────────────────────────────────────────────────────────────────────────► db.py
```

Each layer has a single responsibility. The dependency graph is strictly one-directional — no circular imports.

---

### `constants.py`

Centralizes all shared constants so they are defined once and imported by `db.py`, `anvil.py`, `live_network.py`, `tools.py`, and `deploy.py`:

```python
CHAIN_ID_ANVIL     = 31337
CHAIN_ID_MAINNET   = 1
CHAIN_ID_SEPOLIA   = 11155111
WEI_PER_ETH        = 10**18
ETH_SENTINEL       = "0x0000000000000000000000000000000000000000"
UNISWAP_V2_ROUTER  = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
ENTRYPOINT_V09     = "0x433709009B8330FDa32311DF1C2AFA402eD8D009"
ENTRYPOINT_V07     = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
HEARTBEAT_1H       = 3_600    # 1 hour  — ETH, BTC, AAVE, LINK, DAI, COMP, MKR, UNI, WETH
HEARTBEAT_23H      = 82_800   # 23 hours — USDC
HEARTBEAT_24H      = 86_400   # 24 hours — all other feeds

# ERC-8004 canonical registry addresses
IDENTITY_REGISTRY_MAINNET   = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
IDENTITY_REGISTRY_SEPOLIA   = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
REPUTATION_REGISTRY_MAINNET = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
REPUTATION_REGISTRY_SEPOLIA = "0x8004B663056A597Dffe9eCcC1965A193B7388713"
```

---

### `db.py`

The data persistence layer. All SQLite reads and writes go through this module. It has no web3 or blockchain dependency, making it independently testable.

**Connection management:** Each thread gets its own SQLite connection via `threading.local()` to support the Telegram bot's async/multi-threaded environment.

**Network prefix mapping:** `_NETWORK_DB_PREFIX` maps logical network names to the correct token/pricefeed table prefix:

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
    chat_id        INTEGER NOT NULL,
    target         TEXT NOT NULL,       -- token ticker or "uniswapv2_router" or "eth"
    spending_limit REAL NOT NULL,       -- USD limit in whole units
    end_time       DATE NOT NULL,       -- ISO 8601 expiry date
    PRIMARY KEY (chat_id, target)
);

-- Per-user encrypted session key storage
CREATE TABLE session_keys (
    chat_id         INTEGER NOT NULL,
    target          TEXT NOT NULL,      -- contract address the key is scoped to
    key_address     TEXT NOT NULL,      -- Ethereum address derived from the key
    key_ciphertext  TEXT NOT NULL,      -- Vault Transit ciphertext ('vault:v1:...')
    PRIMARY KEY (chat_id, target)
);

-- Per-user named address book
CREATE TABLE contacts (
    chat_id INTEGER NOT NULL,
    name    TEXT NOT NULL,              -- stored in lowercase
    address TEXT NOT NULL,
    PRIMARY KEY (chat_id, name)
);

-- Deployed contract addresses
CREATE TABLE session_handlers (
    chat_id INTEGER PRIMARY KEY,
    address TEXT NOT NULL
);

CREATE TABLE entry_point (
    chain_id INTEGER PRIMARY KEY,
    address  TEXT NOT NULL
);

-- Network configuration
CREATE TABLE chains (
    name     TEXT NOT NULL,
    chain_id INTEGER NOT NULL,
    PRIMARY KEY (name, chain_id)
);

CREATE TABLE rpcs (
    name    TEXT PRIMARY KEY,
    rpc_url TEXT NOT NULL
);

CREATE TABLE user_network (
    chat_id    INTEGER PRIMARY KEY,
    chain_name TEXT NOT NULL
);

-- Supported ERC20 tokens (per network)
CREATE TABLE anvil_tokens (
    ticker  TEXT PRIMARY KEY,
    address TEXT NOT NULL
);

CREATE TABLE mainnet_tokens (
    ticker  TEXT PRIMARY KEY,
    address TEXT NOT NULL
);

CREATE TABLE sepolia_tokens (
    ticker  TEXT PRIMARY KEY,
    address TEXT NOT NULL
);

CREATE TABLE mainnet_pricefeeds (
    token   TEXT PRIMARY KEY,
    address TEXT NOT NULL
);

CREATE TABLE sepolia_pricefeeds (
    token   TEXT PRIMARY KEY,
    address TEXT NOT NULL
);

-- ABI function selectors
CREATE TABLE erc20_selectors (
    name      TEXT PRIMARY KEY,
    selector  TEXT NOT NULL
);

CREATE TABLE uniswapv2_selectors (
    name      TEXT PRIMARY KEY,
    selector  TEXT NOT NULL
);

CREATE TABLE reputation_registry_selectors (
    name      TEXT PRIMARY KEY,
    selector  TEXT NOT NULL
);

-- ERC-8004 identity and reputation registry addresses (per chain)
CREATE TABLE agent_registries (
    chain_id            INTEGER PRIMARY KEY,
    identity_registry   TEXT NOT NULL,
    reputation_registry TEXT NOT NULL
);

-- ERC-8004 agent token IDs (one per chain after registration)
CREATE TABLE agent_ids (
    chain_id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL
);

-- Scheduled recurring transfers
CREATE TABLE recurring_transfers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    token        TEXT NOT NULL,
    recipient    TEXT NOT NULL,        -- saved contact name
    amount       REAL NOT NULL,        -- whole token units
    interval_hrs INTEGER NOT NULL
);
```

**Initialisation:** Run `make db` once to create all tables and seed the database from the `migrate/` JSON files (including Sepolia token and pricefeed tables). Re-running `make db` is safe — it uses `INSERT OR REPLACE` so existing rows are updated without requiring a database reset.

---

### `vault_signer.py`

Thin wrapper around the [hvac](https://hvac.readthedocs.io/) Vault client. Exposes two functions used by both `anvil.py` and `live_network.py`:

```python
def encrypt_key(raw_key: bytes) -> str:
    # Base64-encodes raw_key, sends to Vault Transit, returns 'vault:v1:...' ciphertext

def decrypt_key(ciphertext: str) -> bytes:
    # Sends ciphertext to Vault, returns raw 32-byte key material
```

Authentication uses AppRole (`VAULT_ROLE_ID` + `VAULT_SECRET_ID`). A fresh authenticated client is created per call — tokens expire after 1 hour per the role's `token_ttl`. The Transit key (`session-keys`) lives inside Vault and is never exported.

---

### `network_config.py`

Resolves Web3 connections from the database.

```python
def load_network_config(chat_id: int) -> tuple[Web3, int, str]
    # Looks up user's current network → returns (Web3, chain_id, chain_name)

def load_network_config_by_name(chain_name: str) -> tuple[Web3, int]
    # Bypasses user lookup — used in deployment scripts before user_network is set
```

---

### `contracts.py`

Contract loading with module-level caching. Every loader checks a per-`chat_id` (or per-`(chat_id, token)`) dict before doing any work. On first call it hits the database and RPC; every subsequent call within the same process returns the cached instance immediately.

```python
def load_session_handler(chat_id: int) -> Contract
def load_entry_point(chat_id: int) -> Contract
def load_ierc20(chat_id: int, token: str) -> Contract       # keyed by (chat_id, token); uses IWETH ABI for "weth"
def load_iuniswap_router(chat_id: int) -> Contract
def load_iuniswap_factory(chat_id: int) -> Contract          # Uniswap V2 Factory at 0x5C69bEe...
def load_iuniswap_pair(chat_id: int, token_a: str, token_b: str) -> Contract  # resolves pair via factory.getPair()
def load_identity_registry(chat_id: int) -> Contract         # ERC-8004 IdentityRegistry; local ABI on Anvil, canonical ABI on live networks
def load_reputation_registry(chat_id: int) -> Contract       # ERC-8004 ReputationRegistry; same ABI fallback logic
def load_calldata(instance: Contract, fn_name: str, args: list) -> bytes
```

> **Note:** The cache is process-scoped. If a contract is redeployed (e.g. after an Anvil restart) the bot process must be restarted to invalidate the cache.

---

### `anvil.py`

The blockchain execution layer for **local and fork networks** (Anvil, mainnet-fork, sepolia-fork). Handles session key management and the full ERC-4337 UserOp lifecycle by calling `handleOps()` directly — no external bundler required.

#### Session Key Management

Session keys are generated once per `(chat_id, target_address)` pair, encrypted at rest, and never stored in plaintext:

```python
def get_or_create_session_key(chat_id: int, target_address: str) -> tuple[str, str]:
    # Returns (key_address, vault_ciphertext)
    # On first call: generates secrets.token_bytes(32), encrypts via Vault Transit,
    #                stores ciphertext in session_keys table, wipes raw key
    # On subsequent calls: returns stored (address, ciphertext) from DB
```

**Security properties:**
- Database breach alone is useless — ciphertexts require Vault to decrypt
- Vault breach alone is useless — the attacker also needs the DB ciphertexts
- Per-user, per-target key isolation — compromise of one key does not affect others
- Vault audit logs record every decrypt call
- Raw key exists in process memory only for the milliseconds between `decrypt_key()` and the `finally` wipe

#### ERC-4337 UserOp Flow

`send_user_op_as_session()` orchestrates the complete UserOp lifecycle for local/fork networks:

1. ABI-encode `SessionHandler.execute(target, value, data)` as the UserOp `callData`.
2. Fetch the current nonce from `EntryPoint.getNonce()`.
3. Build a signed dummy op with placeholder gas limits to estimate gas via `eth_estimateGas`, then construct the real op with a 20% gas buffer (`GAS_BUFFER_MULTIPLIER = 1.2`) and the live gas price.
4. Decrypt the session key from Vault transiently, sign with EIP-191 (`encode_defunct`) — matching `toEthSignedMessageHash` in `SessionHandler._validateSignature` — then wipe.
5. Submit via `EntryPoint.handleOps([userOp], bundler.address)` signed by the bundler key (`ANVIL_BUNDLER`, `MAINNET_BUNDLER`, or `SEPOLIA_BUNDLER` depending on `chain_id`).
6. On outer tx revert: replay via `eth_call` at the pre-execution block to extract the revert reason.
7. On inner call failure (status=1 but `UserOperationEvent.success=false`): raise `RuntimeError` with nonce and gas cost.

**Gas estimation constants:**

```python
DUMMY_INNER_GAS            = 500_000   # placeholder for both verificationGas and callGas
DUMMY_PRE_VERIFICATION_GAS = 50_000
_DUMMY_GAS_PRICE_WEI       = 256       # minimal non-zero price for prefund check
GAS_BUFFER_MULTIPLIER      = 1.2       # 20% headroom on estimated gas
PRE_VERIFICATION_GAS       = 50_000
```

---

### `live_network.py`

The blockchain execution layer for **live networks** (Sepolia, mainnet). Submits UserOps through an Alchemy bundler via JSON-RPC instead of calling `handleOps()` directly — no bundler EOA or gas payment is required from the caller.

#### Bundler RPC Flow

`send_live_user_op_as_session()` orchestrates the full live-network UserOp lifecycle:

1. ABI-encode `SessionHandler.execute(target, value, data)` as the UserOp `callData`.
2. Fetch the current nonce from `EntryPoint.getNonce()`.
3. Build a signed dummy op and submit it to `eth_estimateUserOperationGas` to get per-component gas limits (`callGasLimit`, `verificationGasLimit`, `preVerificationGas`).
4. Construct the final unsigned op with a 20% buffer on `callGasLimit` and `preVerificationGas`, and the live gas price.
5. Decrypt the session key from Vault transiently, sign with EIP-191, wipe.
6. Submit via `eth_sendUserOperation` to the Alchemy bundler RPC endpoint.
7. Poll `eth_getUserOperationReceipt` every 2 seconds until the op is included (timeout: 600s).
8. On `success=false`: raise `RuntimeError` with the revert reason from the receipt.

**Key internal functions:**

```python
def _packed_user_op_to_rpc_json(user_op: tuple) -> dict:
    # Unpacks a PackedUserOperation tuple into the JSON format expected by bundler RPC methods.
    # Splits the packed accountGasLimits and gasFees bytes32 fields into separate hex strings.

def _bundler_rpc(rpc_url: str, method: str, params: list) -> dict:
    # Sends a JSON-RPC POST request to the bundler. Raises RuntimeError on HTTP errors,
    # empty responses, or JSON-RPC error fields.

def create_unsigned_user_op(...) -> tuple:
    # Calls eth_estimateUserOperationGas with a signed dummy op, then constructs the
    # final unsigned op with buffered limits and the latest gas price.

def create_signed_user_op(...) -> tuple:
    # Decrypts the session key from Vault, signs the userOpHash, and wipes the raw key.
```

**Gas constants:**

```python
DUMMY_VERIFICATION_GAS        = 150_000   # separate from call gas — ECDSA + storage reads
DUMMY_CALL_GAS                = 500_000
DUMMY_PRE_VERIFICATION_GAS    = 50_000
GAS_BUFFER_MULTIPLIER         = 1.2
USER_OP_RECEIPT_TIMEOUT_SECS  = 600
USER_OP_POLL_INTERVAL_SECS    = 2
```

> On live networks the Alchemy bundler handles gas payment and MEV protection. The `SEPOLIA_BUNDLER` key in `.env` is not used by `live_network.py` — the Alchemy bundler signs and pays for the outer transaction itself.

#### Routing in `tools.py`

The central dispatch function in `tools.py` routes all on-chain write operations to the correct backend based on the user's current network:

```python
def send_user_op_as_session(chat_id, key_ciphertext, target, value, data):
    _, _, chain_name = load_network_config(chat_id)
    if "fork" in chain_name.lower() or "anvil" in chain_name.lower():
        return _send_user_op_as_session(...)   # anvil.py — direct handleOps()
    else:
        return _send_live_user_op_as_session(...)  # live_network.py — bundler RPC
```

---

### `deploy.py`

Deployment and session registration scripts. Run once per fresh environment or network.

**`deploy_session_handler_anvil(chat_id)`** deploys the full contract stack on Anvil using web3.py (no Foundry required at runtime):

1. EntryPoint
2. 18 ERC20Mock tokens (USDC at 6 decimals, all others at 18)
3. 19 MockV3Aggregator price feeds (ETH + one per token, `FEED_DECIMALS=8`)
4. PriceOracle with parallel token, feed address, and heartbeat arrays (all `HEARTBEAT_1H` on Anvil)
5. SessionHandler
6. Mint tokens into SessionHandler (20,000 USDC at 6 decimals; 2,000 of each other token at 18 decimals)
7. Send 10 ETH to SessionHandler
8. Deploy local `AgentIdentityRegistry` and `ReputationRegistry` scaffolds; persist addresses to `wallet.db`
9. Persist all remaining addresses to `wallet.db`

**`deploy_session_handler(chat_id, network)`** deploys PriceOracle and SessionHandler on a live or fork network. Supported `network` values: `"mainnet-fork"`, `"sepolia-fork"`, `"sepolia"`.

- Fork networks (`*-fork`) use `ANVIL_PRIVATE_KEY`; `"sepolia"` uses `SEPOLIA_PRIVATE_KEY`.
- Mainnet fork enables Uniswap V2 (router set to the live address); Sepolia variants set the router to `address(0)` since Uniswap V2 is not deployed on Sepolia.
- Mainnet fork uses per-token Chainlink heartbeats from `_MAINNET_HEARTBEATS`; Sepolia variants use `HEARTBEAT_24H` uniformly.
- After deployment, calls `prefund()` to send 1 ETH to the SessionHandler and (for fork networks) 10 ETH to the bundler.
- For non-fork networks (`"sepolia"`), calls `_verify()` to submit both `PriceOracle` and `SessionHandler` for Etherscan verification via `forge verify-contract`. Verification is skipped if `ETHERSCAN_API_KEY` is not set.
- Persists SessionHandler address and selected network to `wallet.db`.

**`add_session(chat_id, targets, functions, session_ends, limits)`** registers one session key per target on the SessionHandler. All four list parameters must have the same length — one entry per target. Automatically calls `approve()` for each ERC20 target on mainnet-fork networks.

**`deploy(chat_id, network)`** is the top-level dispatcher called by `make deploy-py`. It routes to the correct deployment function based on the network string. To target a different network, change the `network` argument in the `__main__` block at the bottom of `deploy.py` before running `make deploy-py`. Supported values: `"anvil"`, `"mainnet-fork"`, `"sepolia-fork"`, `"sepolia"`.

**`add_default_session(chat_id)`** registers a default set of session keys. The set varies by network:

**mainnet-fork** — Uniswap V2 is live on the fork, so five sessions are registered:

| Target | Selectors |
|---|---|
| `address(0)` (native ETH) | None — value transfers only |
| WETH | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance`, `deposit`, `withdraw` |
| USDC | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance` |
| Uniswap V2 Router | all 6 swap functions + `addLiquidity`, `addLiquidityETH`, `removeLiquidity`, `removeLiquidityETH` |
| Reputation Registry | `giveFeedback` |

**sepolia / sepolia-fork** — No Uniswap V2 on Sepolia, so four sessions are registered:

| Target | Selectors |
|---|---|
| `address(0)` (native ETH) | None — value transfers only |
| WETH | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance`, `deposit`, `withdraw` |
| LINK | `transfer`, `balanceOf`, `approve`, `transferFrom`, `allowance` |
| Reputation Registry | `giveFeedback` |

Each session gets a 50-day validity window and a $50,000 per-session spending limit. For mainnet-fork, `approve()` is also called for WETH and LINK to grant the Uniswap router an unlimited allowance from the SessionHandler.

**`approve(chat_id, token)`** approves the Uniswap V2 router to spend `type(uint256).max` of a given token from the SessionHandler by calling `execute()` as the owner (deployer). Called automatically by `add_session()` for non-ETH targets on mainnet-fork networks.

---

### `tools.py`

Wraps blockchain operations as LangChain `@tool`-decorated functions. Each tool has a structured docstring the LLM uses to decide when and how to call it.

**`get_tools(job_queue=None)`** is the tool factory. When called without a `job_queue` (CLI mode) it returns the base tool list. When called with a live PTB `JobQueue`, it appends the recurring-transfer tools via closure factories.

All write tools route through the central `send_user_op_as_session()` dispatcher, which selects the Anvil or live-network backend based on the user's current chain.

**Database tools:**

| Tool | Description |
|---|---|
| `get_supported_tokens(chat_id)` | Returns the list of supported token tickers for the user's network |
| `get_all_sessions(chat_id)` | Returns active session metadata, pruning expired ones from DB via `isSessionActive` |
| `save_contact(chat_id, name, address)` | Persists a new named contact |
| `get_contact(chat_id, name)` | Resolves a contact name to an Ethereum address |
| `get_all_contacts(chat_id)` | Returns the full contact list |
| `delete_contact(chat_id, name)` | Removes a contact |
| `get_recurring_transfers(chat_id)` | Returns all scheduled recurring transfers |

**Blockchain read tools:**

| Tool | Description |
|---|---|
| `get_eth_balance(chat_id)` | Returns the wallet's ETH balance in whole units |
| `get_erc20_balance(chat_id, token)` | Returns the wallet's token balance in whole units |
| `get_erc20_allowance(chat_id, token, spender)` | Returns approved allowance for a contact |
| `get_session_keys(chat_id, token)` | Returns `(key_address, vault_ciphertext)` for a token — creates and encrypts a new key if none exists; resolves `"eth"` to `address(0)` and `"uniswapv2_router"` to the router address |
| `get_price(chat_id, token)` | Returns the current USD price from the PriceOracle |
| `get_usd_value(chat_id, token, amount)` | Converts a token amount to USD |
| `preflight_check(chat_id, token, amount, is_uniswap)` | Checks session validity, budget, and USD value in a single call |
| `check_session_validity(chat_id, token)` | Checks if the session key is still active |
| `check_remaining_budget(chat_id, token)` | Returns the remaining USD spending budget |
| `check_spending_within_budget(chat_id, token, amount)` | Validates amount against budget |
| `get_quote_in(chat_id, token_in, token_out, amount_out)` | Returns cost to acquire an exact `amount_out` via Uniswap V2 `getAmountsIn`; routes through WETH for non-WETH pairs |
| `get_quote_out(chat_id, token_in, token_out, amount_in)` | Returns expected output for an exact `amount_in` via Uniswap V2 `getAmountsOut`; routes through WETH for non-WETH pairs |
| `get_pool_quote(chat_id, token_a, token_b, amount_a)` | Returns the proportional `token_b` amount required for a given `amount_a` deposit, from live reserves via `router.quote()` |
| `get_lp_amounts(chat_id, token_a, token_b, lp_amount)` | Returns expected token amounts redeemable by burning `lp_amount` LP tokens, computed from live reserves |
| `get_liquidity_token_balance(chat_id, token_a, token_b)` | Returns the wallet's LP token balance for a pair |

**Balance sufficiency tools:**

| Tool | Description |
|---|---|
| `is_derived_input_sufficient(chat_id, token_in, token_out, amount_out, slippage_bps)` | For exact-output swaps: derives the required input (including slippage) via `getAmountsIn` and checks balance |
| `is_exact_input_sufficient(chat_id, token_in, amount_in)` | For exact-input swaps: compares live balance against the fixed spend amount |
| `is_liquidity_sufficient(chat_id, token_a, amount_a, token_b)` | For `addLiquidity`: derives the required `token_b` from pool reserves and checks both balances |
| `is_liquidity_removal_sufficient(chat_id, token_a, token_b, lp_amount)` | For `removeLiquidity`: checks LP token balance is sufficient |

**Blockchain write tools:**

| Tool | Description |
|---|---|
| `send_eth(chat_id, session_key_ciphertext, recipient, amount_eth)` | Sends native ETH to a contact via the `address(0)` session key |
| `transfer_erc20(chat_id, session_key_ciphertext, token, recipient, amount)` | Sends tokens to a contact |
| `approve_erc20(chat_id, session_key_ciphertext, token, spender, amount)` | Approves a spender |
| `transferFrom_erc20(chat_id, session_key_ciphertext, token, sender, recipient, amount)` | Transfers from an approved sender; pass `"me"` as recipient to target the SessionHandler itself |
| `wrap_eth(chat_id, session_key_ciphertext, amount_eth)` | Wraps ETH to WETH via `deposit()` |
| `swap_ETH_for_exact_tokens(...)` | Buy exact tokens with ETH via `swapETHForExactTokens` |
| `swap_exact_tokens_for_ETH(...)` | Sell exact tokens for ETH via `swapExactTokensForETH` |
| `swap_tokens_for_exact_ETH(...)` | Buy exact ETH with tokens via `swapTokensForExactETH` |
| `swap_exact_ETH_for_tokens(...)` | Sell exact ETH for tokens via `swapExactETHForTokens` |
| `swap_exact_tokens_for_tokens(...)` | Sell exact tokens for tokens via `swapExactTokensForTokens` |
| `swap_tokens_for_exact_tokens(...)` | Buy exact tokens with tokens via `swapTokensForExactTokens` |
| `add_liquidity(...)` | Add liquidity to a Uniswap V2 pool; derives `amount_b` from live reserves |
| `add_liquidity_eth(...)` | Add liquidity to a token/ETH pool; forwards ETH as `msg.value` — no prior WETH wrap needed |
| `remove_liquidity(...)` | Remove liquidity and receive both tokens; credits budget back |
| `remove_liquidity_eth(...)` | Remove liquidity from a token/ETH pool and receive raw ETH; credits budget back |

All write tools accept `session_key_ciphertext: str` — the opaque Vault ciphertext returned by `get_session_keys`. The LLM passes this value between tool calls; it is never decrypted or exposed at the tool layer.

**Recurring transfer tools** (only available when `job_queue` is provided):

| Tool | Description |
|---|---|
| `schedule_recurring_transfer(chat_id, token, recipient, amount, interval_hrs)` | Saves to DB and registers a repeating PTB job |
| `cancel_recurring_transfer(chat_id, transfer_id)` | Removes the job and deletes the DB record |

`recurring_transfer_job` is the async PTB callback that executes each scheduled transfer. If the session key has expired it sends the user a warning, removes the job, and deletes the DB record.

**ERC-8004 tools:**

| Tool | Description |
|---|---|
| `get_agent_identity(chat_id)` | Looks up the agent's ERC-8004 on-chain identity — returns `token_id` and `card_uri` if registered |
| `get_agent_reputation(chat_id)` | Returns the agent's `average_score` (0–100) and `feedback_count` from the `ReputationRegistry` |
| `post_reputation_feedback(chat_id, session_key_ciphertext, score, tags)` | Posts a `giveFeedback` call to the `ReputationRegistry` via the `reputation_registry` session key |

`post_reputation_feedback` routes through the standard `send_user_op_as_session` dispatcher and requires a dedicated `reputation_registry` session key (registered by `add_default_session` alongside the ETH, WETH, and LINK keys). The `reputation_registry` target is resolved to the canonical Sepolia or mainnet registry address based on the user's current network.

---

## Section 3 — LangChain Agent Integration

The `interface/smart_wallet_agent.py` script wraps the blockchain tools in a LangChain agent powered by Anthropic's Claude. The agent reasons over user instructions and decides which tools to call, in what order, and with what arguments.

**Default model:** `claude-sonnet-4-6` (configured in `smart_wallet_agent.py`).

The agent can be reconfigured to use any LLM supported by LangChain — including other Anthropic models, OpenAI, Gemini, Ollama, and more — by swapping the `llm` initialisation. See the [LangChain chat model integrations](https://python.langchain.com/docs/integrations/chat/) for the full list of supported providers and setup instructions.

### Agent Architecture

The LLM, memory checkpointer, and agent are initialised by `init_agent(job_queue=None)`:

```python
def init_agent(job_queue=None):
    tools = get_tools(job_queue=job_queue)
    agent = create_agent(
        model=llm, tools=tools, system_prompt=SYSTEM_PROMPT, checkpointer=memory
    )
```

This is called once at startup — by `main()` for CLI use (no recurring tools) and by the bot's `post_init` callback with the live `JobQueue` so the scheduling tools are included.

**`create_agent`** creates a ReAct-style agent: it loops between calling tools and reasoning about their results until it produces a final natural language answer.

**`InMemorySaver`** persists the full message history in memory, keyed by `thread_id`. This gives each Telegram user their own isolated conversation context.

### System Prompt

The `SYSTEM_PROMPT` instructs the agent on:

- **Hard rules:** Never estimate swap quantities using prices — always call `get_quote_in` or `get_quote_out` to query actual pool reserves. Price-based estimates ignore pool depth, liquidity, and fees.
- What each tool does and when to call it.
- The correct multi-step workflow for each operation. Most write operations begin with `preflight_check` (a single call that validates session, budget, and USD value simultaneously) followed by `is_exact_input_sufficient` or `is_derived_input_sufficient` before proceeding to confirmation and execution.
- Uniswap swap workflows: each of the six swap functions has a dedicated workflow covering session check, slippage confirmation, and balance sufficiency.
- Liquidity workflows: add and remove liquidity with pool reserve quotes and proportional amount derivation.
- Safety rules: never invent or guess addresses, always resolve names via `get_contact` first, never expose `session_key_ciphertext` in responses.
- Token validation: always call `get_supported_tokens` and verify the token before any on-chain action.
- Recurring transfer caveat: warn users that scheduled transfers depend on the session key remaining valid.
- How to extract the `chat_id` from the `[chat_id: <number>]` message prefix.

### Per-User Memory

Each user gets an isolated conversation thread by using their Telegram `chat_id` as the LangGraph `thread_id`:

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

The `chat_id` is embedded in the `HumanMessage` content rather than injected as a `SystemMessage` because Anthropic's API does not allow multiple non-consecutive system messages.

---

## Section 4 — Telegram Bot

The `interface/telebot.py` script exposes the AI agent as a Telegram bot using the [python-telegram-bot v20](https://docs.python-telegram-bot.org/) async API.

### Handlers

| Handler | Trigger | Action |
|---|---|---|
| `/start` | `/start` command | Sends welcome message and schedules the daily session expiry check |
| `/help` | `/help` command | Sends help menu |
| `start_chat` | Any text message | Routes message to the AI agent via `asyncio.to_thread` and replies |

### `post_init` Callback

`post_init(application)` runs once after the bot is initialised but before polling starts. It:

1. Calls `init_agent(job_queue)` — wires the live `JobQueue` into the agent so the scheduling tools are active.
2. Reads all rows from `recurring_transfers` via `get_all_recurring_transfers()` and re-registers each one with `job_queue.run_repeating`. This restores all scheduled jobs after a bot restart.

### Session Expiry Alerts

When a user sends `/start`, a daily `session_expiry_alert` job is registered for their `chat_id`. Every 24 hours it checks all their sessions. If any session expires within the next day it sends a Telegram warning prompting the user to renew.

### Recurring Transfers

After a transfer the agent asks the user whether they want it to repeat. If yes, `schedule_recurring_transfer` is called, which persists the schedule to `wallet.db` and registers a `recurring_transfer_job` with the `JobQueue`. Each time the job fires it executes the transfer and notifies the user. If the session key has expired the job cancels itself and alerts the user.

### Async and Thread Safety

`python-telegram-bot` v20 uses Python's `asyncio` event loop. Because the LangChain agent's `invoke()` is synchronous, it is offloaded to a thread pool via `asyncio.to_thread()` to prevent blocking the event loop. The same pattern is used inside `recurring_transfer_job` for the blocking web3 calls.

SQLite thread safety is handled in `db.py` via `threading.local()` — each thread gets its own connection.

---

## Local Setup (Anvil)

**Step 1 — Find your Telegram `chat_id`:**

Send a message to [@userinfobot](https://t.me/userinfobot). It will reply with your numeric `chat_id`.

**Step 2 — Populate `.env`:**

Set `TELEGRAM_CHAT_ID` in your `.env`:

```env
TELEGRAM_CHAT_ID=your_numeric_chat_id_here
```

**Step 3 — Initialise the database (one-time):**

```bash
make db
```

This creates `wallet.db` and seeds it from the `migrate/` JSON files (chains, RPCs, token addresses, pricefeed addresses, and function selectors for all supported networks). Only needs to be run once — the database persists between restarts. Re-running is safe.

**Step 4 — Start Vault:**

```bash
docker run -d \
  -e VAULT_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e SKIP_SETCAP=true \
  -p 8200:8200 \
  --name vault-dev \
  hashicorp/vault:latest
```

**Step 5 — Configure Vault and inject credentials (one command):**

```bash
make vault
```

This runs `setup_vault.sh` inside the running container and writes `VAULT_ROLE_ID`, `VAULT_SECRET_ID`, `VAULT_SECRET_ID_ACCESSOR`, and `VAULT_ADDR` into your `.env` automatically. Re-run this step after any Vault container restart.

**Step 6 — Start Anvil:**

```bash
make anvil
```

**Step 7 — Deploy the contracts and register session keys:**

> Before running this step, open [interface/deploy.py](interface/deploy.py) and confirm the network name in the `__main__` block at the bottom of the file matches your target. The default is `"sepolia"`. Change it to `"anvil"`, `"mainnet-fork"`, or `"sepolia-fork"` as needed.

```bash
make deploy-py
```

This deploys the full contract stack on the configured network and registers default session keys for the configured `chat_id`. On Anvil it deploys a full mock stack (EntryPoint, ERC20Mocks, MockV3Aggregators, PriceOracle, SessionHandler). Must be re-run whenever Anvil is restarted (chain state is wiped on restart).

**Step 8 — Start the Telegram bot:**

```bash
make bot
```

**Step 9 — Open Telegram, find your bot, and start chatting:**

```
What is my wallet address?
What is my USDC balance?
Send 10 LINK to Sandy
What is my WETH balance?
Show me my recurring transfers
```

**Optional — run the agent interactively from the CLI:**

```bash
make agent
```

Starts the agent in a read-eval-print loop without launching the Telegram bot. Useful for testing tool behaviour directly. Recurring-transfer tools are not available in this mode (no `JobQueue`).

> When running in CLI mode, `TELEGRAM_CHAT_ID` is used as the `thread_id` for the agent's memory and as the key for all database lookups (session keys, deployed contracts, network config). It does not need to be a real Telegram chat ID — any integer will work, as long as it matches the `chat_id` used when the contracts were deployed.

---

## Local Setup (Mainnet Fork)

Run the full stack against a local mainnet fork. Requires `MAINNET_RPC_URL` to be set in `.env`.

**Step 1 — Start a mainnet fork:**

```bash
make mainnet-fork
```

**Step 2 — Initialise the database (one-time):**

```bash
make db
```

**Step 3 — Deploy the contracts and register session keys:**

> Open [interface/deploy.py](interface/deploy.py) and set the network in the `__main__` block to `"mainnet-fork"`, then run:

```bash
make deploy-py
```

**Step 4 — Start the bot or agent:**

```bash
make bot      # Telegram bot
make agent    # Interactive CLI
```

> Re-run `make vault` and `make deploy-py` after any Vault container restart or Anvil restart — fork state is wiped on restart.

---

## Local Setup (Sepolia Fork)

Run the full stack against a local Sepolia fork. Requires `SEPOLIA_RPC_URL` to be set in `.env`.

**Step 1 — Start a Sepolia fork:**

```bash
make sepolia-fork
```

**Step 2 — Initialise the database (one-time):**

```bash
make db
```

**Step 3 — Deploy the contracts and register session keys:**

> Open [interface/deploy.py](interface/deploy.py) and set the network in the `__main__` block to `"sepolia-fork"`, then run:

```bash
make deploy-py
```

**Step 4 — Start the bot or agent:**

```bash
make bot      # Telegram bot
make agent    # Interactive CLI
```

> Uniswap V2 tools are unavailable on the Sepolia fork — the router is not deployed on Sepolia. Only ETH, WETH, and LINK sessions are registered by default.

---

## Sepolia Deployment

`deploy.py` supports deploying to live Sepolia via `deploy_session_handler(chat_id, "sepolia")`. The `__main__` block runs this automatically when called from `make deploy` after updating `TELEGRAM_CHAT_ID` and `SEPOLIA_PRIVATE_KEY` in `.env`.

On Sepolia, the Alchemy bundler handles UserOp submission. `SEPOLIA_RPC_URL` must point to an Alchemy Sepolia endpoint (the bundler-compatible endpoint is the same URL as the standard JSON-RPC endpoint for Alchemy).

After a successful Sepolia deployment, `deploy.py` prints the deployed `PriceOracle` and `SessionHandler` addresses. If `ETHERSCAN_API_KEY` is set, both contracts are automatically submitted for Etherscan verification via `forge verify-contract`.

The default Sepolia session registers ETH, WETH, LINK, and Reputation Registry keys (Uniswap V2 operations are not supported on Sepolia since the router is not deployed there).

> **Uniswap V2 tools are unavailable on Sepolia and Sepolia fork.** Uniswap V2 has no official deployment on Sepolia, so `deploy_session_handler()` sets the router address to `address(0)` and `add_default_session()` does not register a `uniswapv2_router` session key. Any agent tool that performs a swap, adds liquidity, or removes liquidity (`swap_*`, `add_liquidity*`, `remove_liquidity*`, `get_quote_in`, `get_quote_out`, `get_pool_quote`, `get_lp_amounts`, `get_liquidity_token_balance`) will fail on these networks — either because the session key does not exist or because there is no router to call.

### Deployed Contracts (Sepolia)

| Contract | Address |
|---|---|
| `SessionHandler` | `0x202DCf56889F9b11F68f636e7567Ac14B9B1D249` |
| `PriceOracle` | `0x6C1A1Da2518C456a097A082fc9f48d76f23F0aaC` |
| `IdentityRegistry` (canonical ERC-8004) | `0x8004A818BFB912233c491871b3d84c89A494BD9e` |
| `ReputationRegistry` (canonical ERC-8004) | `0x8004B663056A597Dffe9eCcC1965A193B7388713` |

---

## Makefile Reference

| Command | Description |
|---|---|
| `make build` | Compile contracts |
| `make test` | Run Forge test suite |
| `make snapshot` | Generate gas snapshot |
| `make format` | Format Solidity sources |
| `make clean` | Remove build artifacts |
| `make install` | Install Forge dependencies |
| `make update` | Update Forge dependencies |
| `make anvil` | Start a local Anvil node |
| `make mainnet-fork` | Start a mainnet fork at the latest block |
| `make sepolia-fork` | Start a Sepolia fork at the latest block |
| `make vault` | Configure Vault and refresh `.env` credentials |
| `make db` | Initialise SQLite database and run migrations |
| `make deploy-py` | Deploy contracts and register session keys (set network in `deploy.py` `__main__` first) |
| `make agent` | Start the agent in interactive CLI mode |
| `make bot` | Start the Telegram bot |

---

## Project Structure

```
session-key-infra/
├── src/
│   ├── SessionHandler.sol
│   ├── PriceOracle.sol
│   ├── Constants.sol
│   ├── AgentIdentityRegistry.sol    ← ERC-8004 identity registry scaffold (Anvil)
│   ├── ReputationRegistry.sol       ← ERC-8004 reputation registry scaffold (Anvil)
│   ├── interfaces/
│   │   ├── IWETH.sol
│   │   ├── IERC20Extended.sol
│   │   ├── IIdentityRegistry.sol    ← ERC-8004 IIdentityRegistry interface
│   │   └── IReputationRegistry.sol  ← ERC-8004 IReputationRegistry interface
│   └── mocks/
│       ├── ERC20Mock.sol
│       ├── MockV3Aggregator.sol
│       └── MockWeth.sol
├── script/
│   ├── DeploySessionHandler.s.sol
│   ├── DeployAgentRegistry.s.sol    ← Deploys local AgentIdentityRegistry (Anvil)
│   ├── HelperConfig.s.sol
│   ├── SendPackedUserOp.s.sol
│   └── FundSessionHandler.s.sol
├── test/
│   ├── unit/
│   │   ├── TestSessionHandler.t.sol
│   │   └── SessionHandlerHarness.sol
│   ├── fork/
│   │   ├── UniswapV2Test.t.sol
│   │   └── SessionHandlerSepoliaTest.t.sol
│   └── invariant/
│       ├── InvariantSessionHandler.t.sol
│       └── SessionHandlerHandler.sol
├── interface/
│   ├── constants.py
│   ├── db.py
│   ├── network_config.py
│   ├── contracts.py
│   ├── anvil.py
│   ├── live_network.py
│   ├── vault_signer.py
│   ├── deploy.py
│   ├── register_agent.py            ← One-time ERC-8004 identity registration script
│   ├── tools.py
│   ├── smart_wallet_agent.py
│   ├── telebot.py
│   ├── agent_card.json              ← ERC-8004/v1 agent card
│   ├── wallet.db                    ← SQLite database (not committed)
│   ├── artifacts/
│   │   ├── SessionHandler.json
│   │   ├── EntryPoint.json
│   │   ├── IERC20Extended.json
│   │   ├── IWETH.json
│   │   ├── IUniswapV2Router02.json
│   │   ├── IUniswapV2Factory.json
│   │   ├── IUniswapV2Pair.json
│   │   ├── ERC20Mock.json
│   │   ├── MockV3Aggregator.json
│   │   ├── IdentityRegistry.json    ← Canonical ERC-8004 IdentityRegistry ABI
│   │   └── ReputationRegistry.json  ← Canonical ERC-8004 ReputationRegistry ABI
│   └── migrate/
│       ├── Chains.json
│       ├── RPC.json
│       ├── ERC20_Selectors.json
│       ├── UniswapV2_Selectors.json
│       ├── Registry_Selectors.json  ← ERC-8004 ReputationRegistry selectors
│       ├── Mainnet_Tokens.json
│       ├── Mainnet_Pricefeeds.json
│       ├── Sepolia_Tokens.json
│       └── Sepolia_Pricefeeds.json
├── lib/                             ← Foundry dependencies (git submodules)
│   ├── account-abstraction/
│   ├── openzeppelin-contracts/
│   ├── chainlink-brownie-contracts/
│   ├── forge-std/
│   ├── v2-core/
│   └── v2-periphery/
├── setup_vault.sh                   ← Vault configuration automation script
├── Makefile
├── foundry.toml
└── .env                             ← Not committed — create locally
```

---

## Dependencies

**Solidity**

| Library | Purpose |
|---|---|
| `eth-infinitism/account-abstraction` | ERC-4337 `IAccount`, `EntryPoint`, `PackedUserOperation` |
| `OpenZeppelin Contracts` | `ECDSA`, `Ownable`, `ReentrancyGuard`, `Pausable`, `IERC20Metadata`, `ERC721`, `ERC721URIStorage` |
| `chainlink-brownie-contracts` | `AggregatorV3Interface` for Chainlink price feeds |
| `Uniswap v2-core / v2-periphery` | `IUniswapV2Router01/02`, `IUniswapV2Factory`, `IUniswapV2Pair` interfaces |
| `forge-std` | Foundry testing and scripting utilities |
| ERC-8004 canonical registries (external) | `IIdentityRegistry`, `IReputationRegistry` — canonical UUPS-upgradeable deployments on Sepolia and mainnet; local scaffolds (`AgentIdentityRegistry`, `ReputationRegistry`) used on Anvil |

**Python**

| Package | Purpose |
|---|---|
| `web3` | Ethereum JSON-RPC client |
| `eth-account` | Key management and EIP-191 message signing |
| `hvac` | HashiCorp Vault Python client (Transit encrypt/decrypt) |
| `requests` | HTTP client for bundler JSON-RPC calls (`live_network.py`) |
| `langchain` | Tool definitions and agent framework |
| `langchain-anthropic` | Default Claude LLM integration — swappable for any other [LangChain-supported provider](https://python.langchain.com/docs/integrations/chat/) (OpenAI, Gemini, Ollama, etc.) |
| `langgraph` | Stateful agent execution with `InMemorySaver` checkpointer |
| `python-telegram-bot[job-queue]` | Telegram Bot API client (v20 async) with APScheduler for timed jobs |
| `python-dotenv` | `.env` file loading |

**Infrastructure**

| Tool | Purpose |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Runs the HashiCorp Vault container locally |
| [HashiCorp Vault](https://developer.hashicorp.com/vault) | Transit encryption-as-a-service for session key custody |
| [Alchemy](https://www.alchemy.com/) | Bundler-compatible RPC endpoint for live Sepolia/mainnet UserOp submission |
