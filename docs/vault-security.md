# Vault & Session Key Security

Session keys are cryptographic private keys that authorize an AI agent to sign ERC-4337 `UserOperation`s. Storing them on disk in plaintext is unacceptable — a single database breach would expose all keys for all users. This system uses **HashiCorp Vault Transit** (encryption-as-a-service) so that raw key material never touches disk.

## Environment Variables

Create a `.env` file in the project root:

```env
# Signing keys
ANVIL_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
SEPOLIA_PRIVATE_KEY=your_sepolia_deployer_private_key_here

# Deployer wallet address (used by HelperConfig on live networks)
SEPOLIA_ACCOUNT=your_deployer_wallet_address_here

# Bundler keys (sign the outer handleOps transaction on local/fork networks)
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

# Optional — Etherscan contract verification after Sepolia deployment
ETHERSCAN_API_KEY=your_etherscan_api_key_here
```

> `SEPOLIA_ACCOUNT` is the public Ethereum address corresponding to `SEPOLIA_PRIVATE_KEY`. It is used by `HelperConfig.s.sol` as the deployer account on live networks. Load it via `vm.envAddress("SEPOLIA_ACCOUNT")` — do not hardcode it.
>
> `ANVIL_PRIVATE_KEY` and `ANVIL_BUNDLER` are Anvil's default account 0 and account 2 keys. They are public and safe to use locally only.
>
> `SEPOLIA_PRIVATE_KEY` and `SEPOLIA_BUNDLER` must be funded with Sepolia ETH before deployment. They can be the same key.
>
> `MAINNET_BUNDLER` and `SEPOLIA_BUNDLER` are only required for their respective network. On fork networks, the bundler is funded programmatically by `prefund()`.
>
> On live Sepolia, the Alchemy bundler handles gas — `SEPOLIA_BUNDLER` is not used by `live_network.py`.
>
> `ETHERSCAN_API_KEY` is optional. If not set, `deploy.py` skips contract verification and prints a notice.

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
