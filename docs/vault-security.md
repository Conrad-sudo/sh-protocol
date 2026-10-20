# Vault & Session Key Security

A session key is a cryptographic private key that authorizes an AI agent to sign ERC-4337 `UserOperation`s. Each wallet has exactly one session key (keyed to the wallet address in the `session_keys` table); it is a bare authorized signer, bounded on-chain by the wallet's USD spending cap and admin-surface guard rather than by any per-key scope. Storing it on disk in plaintext is unacceptable — a single database breach would expose the keys for all users — so this system uses **HashiCorp Vault Transit** (encryption-as-a-service) and raw key material never touches disk. The encryption mechanism below is unchanged from earlier designs; only *what the key is keyed to* changed (one key per wallet, not one per token/target).

## Environment Variables

Create a `.env` file in the project root:

```env
# Signing keys
ANVIL_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
API_BUNDLER_1=your_first_api_bundler_private_key_here  # the API's bundlers, numbered from 1; the first is also
API_BUNDLER_2=your_second_api_bundler_private_key_here #   the fork + live-Sepolia deployer
API_BUNDLER_3=your_third_api_bundler_private_key_here
TELEGRAM_BUNDLER_1=your_first_telegram_bundler_key_here # the Telegram bot's bundlers — none may be an API key
TELEGRAM_BUNDLER_2=your_second_telegram_bundler_key_here
TELEGRAM_BUNDLER_3=your_third_telegram_bundler_key_here
BSC_PRIVATE_KEY=your_bsc_deployer_private_key_here
CELO_PRIVATE_KEY=your_celo_deployer_private_key_here

# API_BUNDLER_1's address — the deployer account HelperConfig uses off plain Anvil
SEPOLIA_ACCOUNT=your_deployer_wallet_address_here

# Optional: where live-mainnet transactions are broadcast. Defaults to Flashbots Protect.
MAINNET_PRIVATE_RPC_URL=https://rpc.flashbots.net/fast
# Optional: where live-BSC transactions are broadcast. Defaults to BlockRazor's full-privacy endpoint.
BSC_PRIVATE_RPC_URL=https://bsc.blockrazor.xyz/fullprivacy

# RPC endpoints
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/your_alchemy_key
SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/your_alchemy_key
BSC_RPC_URL=https://bnb-mainnet.g.alchemy.com/v2/your_alchemy_key
ARB_RPC_URL=https://arb-mainnet.g.alchemy.com/v2/your_alchemy_key
BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/your_alchemy_key
CELO_RPC_URL=https://celo-mainnet.g.alchemy.com/v2/your_alchemy_key

# AI / bot credentials
DEEPSEEK_API_KEY=your_deepseek_api_key_here     # default LLM; not needed if you swap in another provider
APP_USER_ID=1                                   # which account the local harnesses run as (see setup.md)

# Optional — the Telegram bot (`make bot`): one bot for every network, from @BotFather. Its token,
# and its username without the @ (for the web app's link). Users pick the network with /chain.
MITFAH_API=your_bot_token
MITFAH_USERNAME=your_bot_username
TELEGRAM_MAX_CONCURRENT=64                      # optional; how many Telegram chat turns may run at once

# Web API (`make api`)
JWT_SECRET=                                     # REQUIRED for the API; long random string, fails closed if unset
CORS_ORIGINS=http://localhost:3000              # comma-separated; the refresh cookie needs credentialed CORS
COOKIE_SECURE=1                                 # set 0 for local http development
SIWE_DOMAIN=localhost:3000                      # the site SIWE messages must name; mitfah.com in production (comma-separated)
CHAT_MAX_CONCURRENT=180                         # optional; how many web chat turns may run at once
CONFIRM_MAX_CONCURRENT=64                       # optional; quote-card taps at once (web and bot each), apart from turns
LANDING_MAX_WAITS=256                           # optional; confirmed sends waited on at once, per process

# HashiCorp Vault — populated automatically by `make vault`
VAULT_ADDR=http://127.0.0.1:8200
VAULT_ROLE_ID=
VAULT_SECRET_ID=
VAULT_SECRET_ID_ACCESSOR=

# Optional — Etherscan contract verification after Sepolia deployment, and the History tab's
# activity outside Mitfah on Ethereum, Sepolia, Arbitrum and Celo
ETHERSCAN_API_KEY=your_etherscan_api_key_here
# Optional — the History tab's activity outside Mitfah on BSC (Etherscan's free tier excludes BSC)
NODEREAL_API_KEY=your_nodereal_api_key_here
# Optional — the same on Base, which Etherscan's free tier excludes too (the key in BASE_RPC_URL)
ALCHEMY_API_KEY=your_alchemy_key
```

> `SEPOLIA_ACCOUNT` is the public Ethereum address corresponding to `API_BUNDLER_1` (which was `API_BUNDLER` until 2026-10-20, and `SEPOLIA_PRIVATE_KEY` until 2026-09-22). It is used by `HelperConfig.s.sol` as the deployer account on live Sepolia. Load it via `vm.envAddress("SEPOLIA_ACCOUNT")` — do not hardcode it. Mainnet/BSC use a separate placeholder key (`MAINNET_DEPLOYER_PK`) hardcoded in `HelperConfig.s.sol` — **replace it with a real funded key before broadcasting a live mainnet or BSC deployment.**
>
> `ANVIL_PRIVATE_KEY` is Anvil's default account 0 key — public, and safe to use locally only. `ANVIL_BUNDLER` is no longer read: plain Anvil now bundles with the `API_BUNDLER_n` / `TELEGRAM_BUNDLER_n` keys like every other network, and can be removed from `.env`.
>
> **The bundler keys.** The app is its own bundler on every network (`bundler.py`), and each *process* signs `handleOps` with a pool of keys of its own, numbered from 1: the API with `API_BUNDLER_1`, `API_BUNDLER_2`, …, the Telegram bot with `TELEGRAM_BUNDLER_1`, `_2`, … (read up to the first number not set; three each today). No key may be in both pools — `tx_sender`'s nonce lock covers one process, so two processes sharing a key would hand out the same nonces and replace each other's transactions. Within a process each op goes to the least busy key (`bundler.pick_bundler`), so a stuck transaction holds up only its own key. Every key must be a plain EOA with no code, and on a live chain every key must hold enough of the native asset to keep fronting gas (the EntryPoint repays them per op; nothing tops them up automatically). A quote checks `API_BUNDLER_1`'s balance, so keep the keys funded evenly.
>
> `API_BUNDLER_1` and `BSC_PRIVATE_KEY` must be funded with real Sepolia ETH / BSC BNB before deployment. `API_BUNDLER_1` carries three roles at once: the live-Sepolia deployer/owner, the deployer on every fork, and one of the API's bundlers. Collapsing the deployer and bundler into one key is acceptable on a testnet; on mainnet they must be separate, since the deployer is the protocol's admin root (see [THREAT_MODEL.md](../THREAT_MODEL.md)) and a bundler is an always-online hot key.
>
> `FORK_DEPLOYER_PK` / `FORK_DEPLOYER_ADDRESS` are **no longer used** and can be removed from `.env`. Every fork network (mainnet-fork, sepolia-fork, bsc-fork, celo-fork, arbitrum-fork, base-fork) now uses `SEPOLIA_ACCOUNT` / `API_BUNDLER_1` for all three roles at once: the `--sender`/`--private-key` broadcasting `DeploySHProtocol.s.sol` (see the Makefile), the deployer/owner key `deploy_wallet.py` signs with, and one of the API's bundler keys for the outer `handleOps` transaction. `HelperConfig.s.sol` resolves `config.account` to the same address, so it also owns `SHTreasury`.
>
> That address inherits the forked chain's **real** balance, which is zero on mainnet-fork and bsc-fork, so `make fund` sets it to 100 ETH via `anvil_setBalance` — and does the same for every other `API_BUNDLER_n` and `TELEGRAM_BUNDLER_n` address, derived from the keys. It is a prerequisite of both `deploy` and `deploy-wallet`, and self-skips on any network without a local node, so there is no separate bundler-funding step.
>
> `CELO_PRIVATE_KEY` / `CELO_RPC_URL` are read by the Python app's network routing (`deploy_wallet.py`), but Celo has no Solidity deployment path yet (`HelperConfig.s.sol` has no Celo branch) — see [docs/app.md](app.md).
>
> On live mainnet and live BSC the bundler broadcasts through a private RPC (`MAINNET_PRIVATE_RPC_URL`, default Flashbots Protect; `BSC_PRIVATE_RPC_URL`, default BlockRazor's full-privacy endpoint, which needs no API key) so its transactions never sit in the public mempool, where a bot could lift a UserOp out and land it first — which, now that one transaction carries many users' ops, would revert the whole bundle. Reads and estimates still go to `MAINNET_RPC_URL` / `BSC_RPC_URL`.
>
> `ETHERSCAN_API_KEY` is optional. If not set, deployment skips contract verification and prints a notice, and the History tab lists no activity from outside Mitfah on Ethereum, Sepolia, Arbitrum or Celo. `NODEREAL_API_KEY` does the same for BSC, and `ALCHEMY_API_KEY` for Base. Without them, the tab still lists everything Mitfah sent; the API logs a warning for the search it couldn't run.
>
> **The bot's token is optional — the whole Telegram layer is.** `MITFAH_API` is read only by `telebot.py` (`make bot`); `MITFAH_USERNAME` is read by the API, to mint the link that opens the bot and to show it in the web app. The interactive CLI (`make agent`) reads neither, so you can run the full agent without a bot or a Telegram account; it runs as `APP_USER_ID` (see [setup.md](setup.md)). On a live chain, every `TELEGRAM_BUNDLER_n` key must hold the native coin of every live network the bot serves. `TELEGRAM_MAX_CONCURRENT` (default 64) caps how many Telegram chat turns run at once, as `CHAT_MAX_CONCURRENT` (default 180 since 2026-10-20) does for the web chat (see [app.md](app.md#section-4--telegram-bot)); DeepSeek sets no rate limit, so the ceiling is the one process's CPU (a turn reading the chain uses 50–200 ms of it) and the chain RPC's request limit. Quote-card taps have their own `CONFIRM_MAX_CONCURRENT` (default 64).
>
> **The ten per-chain bot variables are gone (2026-10-16).** Until then each network had a bot of its own, set as `MITFAH_<STEM>_API` / `MITFAH_<STEM>_USERNAME` with the stems `ETH`, `SEPOLIA`, `BSC`, `ARB` and `BASE`. Nothing reads them any more: delete them. If `MITFAH_API` is missing and any of them is still set, `make bot` names them. The move itself is in [setup.md](setup.md#moving-from-five-telegram-bots-to-one).
>
> **`DEEPSEEK_API_KEY` is only the default.** The agent uses DeepSeek out of the box, but the LLM is not tied to DeepSeek — swap in any [LangChain chat model](https://python.langchain.com/docs/integrations/chat/) with a small edit to `app/smart_wallet_agent.py` (see [docs/app.md](app.md#section-3--langchain-agent)), and this key is no longer needed. Some LLM provider is always required; DeepSeek specifically is not.

---

## Threat Model

| Attacker | Obtains | Impact |
|---|---|---|
| Breaches the SQLite database | `key_ciphertext` blobs | Zero — ciphertexts require Vault's Transit key to decrypt |
| Breaches Vault | AES-256 Transit key | Zero — the attacker still needs the specific ciphertexts from the DB |
| Breaches both simultaneously | Both | Full key compromise — both must be protected |

This is a **2-of-2 security model**: the database holds ciphertexts, Vault holds the decryption key. Neither is sufficient alone.

---

## How Docker Runs Vault

Vault is started as a Docker container exposing port 8200. Dev mode is used for local development — all state is in-memory and the root token is `dev-root-token`.

> **`dev-root-token` is intentionally insecure.** Do not use it outside of a local development environment. In production, revoke the root token entirely after initial setup and authenticate exclusively via AppRole or another auth method backed by a persistent storage backend.

A custom AppRole (`wallet-agent`) is provisioned with narrowly scoped policy:

```hcl
path "transit/encrypt/session-keys" { capabilities = ["update"] }
path "transit/decrypt/session-keys" { capabilities = ["update"] }
```

The AppRole issues short-lived tokens (1-hour TTL, 4-hour max). `vault_signer.py` authenticates with `VAULT_ROLE_ID` + `VAULT_SECRET_ID` and creates a fresh authenticated client on every call — no long-lived token is cached in the process.

---

## Step 1 — Start the Vault Container

```bash
docker run -d \
  -e VAULT_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e SKIP_SETCAP=true \
  -p 8200:8200 \
  --name vault-dev \
  hashicorp/vault:latest
```

## Step 2 — Configure Vault (one command)

```bash
make vault
```

`setup_vault.sh` runs the full configuration sequence inside the container — enabling the Transit secrets engine, creating the `session-keys` AES-256-GCM96 key, enabling AppRole auth, writing the `wallet-agent` policy, and generating a fresh `role_id` / `secret_id` pair. It then writes all four Vault variables directly into `.env`:

```
VAULT_ADDR, VAULT_ROLE_ID, VAULT_SECRET_ID, VAULT_SECRET_ID_ACCESSOR
```

> Save `VAULT_SECRET_ID_ACCESSOR` separately — you need it to revoke or rotate the secret without the secret itself.
>
> Re-run `make vault` after any Vault container restart — dev mode is entirely in-memory.

---

## `vault_signer.py`

The Python interface to Vault. Exposes two functions:

```python
def encrypt_key(raw_key: bytes) -> str:
    # Base64-encodes raw_key → Vault Transit encrypt → returns 'vault:v1:...' ciphertext

def decrypt_key(ciphertext: str) -> bytes:
    # Vault Transit decrypt → returns raw 32-byte key material
```

The Transit key named `session-keys` lives inside Vault and is never exported or readable — it can only be referenced by the encrypt/decrypt API.

---

## Key Lifecycle

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

At signing time the raw key is decrypted transiently and wiped immediately after the signature is produced:

```python
raw_key = decrypt_key(key_ciphertext)
try:
    signed = w3.eth.account.sign_message(encode_defunct(user_op_hash), private_key=raw_key)
    return user_op[:-1] + (signed.signature,)
finally:
    raw_key = b"\x00" * len(raw_key)
    del raw_key
```

The key exists in process memory only for the milliseconds between `decrypt_key()` and the `finally` wipe. The opaque ciphertext (`vault:v1:...`) travels through the tool chain as a string — the LLM passes it between tool calls but never sees or logs raw key material.

---

## Operational Notes

> **Dev mode is ephemeral.** All Vault state (keys, AppRole config, policies) is in-memory. A container restart wipes everything. Sleep/suspend preserves state; a full restart does not.

> **Production readiness:** Before going to production, replace dev mode with a real Vault server backed by a persistent storage backend, enable auto-unseal via a cloud KMS (AWS KMS, GCP Cloud KMS, or Azure Key Vault), revoke the root token, and enforce TLS. See the [Vault production hardening guide](https://developer.hashicorp.com/vault/tutorials/operations/production-hardening).
