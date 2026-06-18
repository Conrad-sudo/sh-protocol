# Setup Guides

Before running any setup, make sure you have completed the one-time steps:

1. **Clone and install** — see the root [README](../README.md#clone-and-install).
2. **Environment variables** — create `.env` as described in [Vault & Security](vault-security.md#environment-variables).
3. **Telegram `chat_id`** — send a message to [@userinfobot](https://t.me/userinfobot). It replies with your numeric `chat_id`. Set it in `.env`:

   ```env
   TELEGRAM_CHAT_ID=your_numeric_chat_id_here
   ```

   `TELEGRAM_CHAT_ID` is read by `deploy_wallet.py` (to know which user to deploy a wallet for) and by `telebot.py` / `smart_wallet_agent.py` (to know who's chatting). In CLI mode it doubles as the agent's `thread_id` and DB key — it doesn't need to be a real Telegram ID, any integer works, as long as it's the same one used at deployment time.

---

## The Setup Sequence

Every network (Anvil, mainnet fork, Sepolia fork, live Sepolia) follows the same six steps. The sections below give the exact commands for each network — this is the shape of what's happening and why the order matters:

1. **Start the network** — `make anvil`, `make mainnet-fork`, or `make sepolia-fork`. This must be running before anything else, since every later step talks to it over RPC. (Live Sepolia deployment skips this — there's no local node to start.)
2. **Deploy the shared protocol** — `make deploy ARGS="<network>"`. Runs `script/DeploySHProtocol.s.sol`, which deploys the infrastructure shared by all users: `EntryPoint`, mocks (on Anvil), `SHOracle`, `SHTreasury`, `SHRegistry`, `SHValueInterpreter`, and `SHFactory`. Only needs to be re-run when this infra doesn't exist yet on the target chain (e.g. after restarting Anvil, which wipes all chain state).
3. **Start and configure Vault** — start the Vault Docker container, then run `make vault`. Vault holds the Transit key that encrypts/decrypts session keys; it must be ready before any session key is created in step 5.
4. **Sync the database** — `make db`. Seeds reference data (token addresses, selectors, RPC URLs, price feeds) and, critically, reads the `SHFactory` address out of the Forge broadcast file written in step 2 (`broadcast/DeploySHProtocol.s.sol/<chain_id>/run-latest.json`) into the `factory` table. This step must run *after* step 2 — `deploy_wallet.py` resolves the factory address from the DB, not from the broadcast file directly. Safe to re-run any time; it's idempotent.
5. **Deploy your wallet** — `make deploy-wallet`. Calls `SHFactory.deployWallet()` to create a per-user `SessionHandler`, funds it with 10 ETH (and the bundler, on forks), and registers a default set of session keys.

   > **Before running this**, open [app/deploy_wallet.py](../app/deploy_wallet.py) and check the `network` argument passed to `deploy()` in the `__main__` block at the bottom of the file. It must exactly match the network you started in step 1 and deployed to in step 2 (`"anvil"`, `"mainnet-fork"`, `"sepolia-fork"`, or `"sepolia"`). If it doesn't match, the script will look up the wrong factory address (or none at all) and fail.

6. **Start talking to it** — `make bot` (Telegram) or `make agent` (interactive CLI).

---

## Local Setup (Anvil)

**Step 1 — Start Anvil:**

```bash
make anvil
```

**Step 2 — Deploy the shared protocol:**

```bash
make deploy
```

(No `ARGS` needed — the default `NETWORK_ARGS` in the Makefile already points at `http://127.0.0.1:8545` with the Anvil default account.) This deploys the full mock stack (EntryPoint, ERC20Mocks, MockV3Aggregators, MockIdentityRegistry, MockReputationRegistry, SHOracle, SHTreasury, SHRegistry, SHValueInterpreter, SHFactory).

**Step 3 — Start and configure Vault:**

```bash
docker run -d \
  -e VAULT_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e SKIP_SETCAP=true \
  -p 8200:8200 \
  --name vault-dev \
  hashicorp/vault:latest

make vault
```

**Step 4 — Sync the database:**

```bash
make db
```

**Step 5 — Deploy your wallet and register session keys:**

> Open [app/deploy_wallet.py](../app/deploy_wallet.py) and confirm the network in the `__main__` block is `"anvil"`, then run:

```bash
make deploy-wallet
```

Must be re-run (along with steps 1, 2, and 4) whenever Anvil is restarted — chain state, including the deployed protocol, is wiped on restart.

**Step 6 — Start the Telegram bot:**

```bash
make bot
```

**Step 7 — Chat:**

```
What is my wallet address?
What is my USDC balance?
Send 10 LINK to Sandy
Show me my recurring transfers
```

**Optional — interactive CLI (no Telegram required):**

```bash
make agent
```

---

## Local Setup (Mainnet Fork)

Requires `MAINNET_RPC_URL` in `.env`.

**Step 1 — Start a mainnet fork:**

```bash
make mainnet-fork
```

**Step 2 — Deploy the shared protocol:**

```bash
make deploy
```

**Step 3 — Start and configure Vault:**

```bash
docker run -d \
  -e VAULT_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e SKIP_SETCAP=true \
  -p 8200:8200 \
  --name vault-dev \
  hashicorp/vault:latest

make vault
```

**Step 4 — Sync the database:**

```bash
make db
```

**Step 5 — Deploy your wallet:**

> Open [app/deploy_wallet.py](../app/deploy_wallet.py) and set the network to `"mainnet-fork"`, then run:

```bash
make deploy-wallet
```

**Step 6 — Start:**

```bash
make bot      # Telegram bot
make agent    # Interactive CLI
```

> Re-run steps 1, 2, and 4 after any Anvil restart, and re-run `make vault` after any Vault container restart — fork state is wiped on restart.

---

## Local Setup (Sepolia Fork)

Requires `SEPOLIA_RPC_URL` in `.env`.

**Step 1 — Start a Sepolia fork:**

```bash
make sepolia-fork
```

**Step 2 — Deploy the shared protocol:**

```bash
make deploy ARGS="sepolia-fork"
```

`ARGS="sepolia-fork"` tells the Makefile to sign with `SEPOLIA_ACCOUNT` / `SEPOLIA_PRIVATE_KEY` while still broadcasting to the local fork at `http://127.0.0.1:8545`.

**Step 3 — Start and configure Vault:**

```bash
docker run -d \
  -e VAULT_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e SKIP_SETCAP=true \
  -p 8200:8200 \
  --name vault-dev \
  hashicorp/vault:latest

make vault
```

**Step 4 — Sync the database:**

```bash
make db
```

**Step 5 — Deploy your wallet:**

> Open [app/deploy_wallet.py](../app/deploy_wallet.py) and set the network to `"sepolia-fork"`, then run:

```bash
make deploy-wallet
```

**Step 6 — Start:**

```bash
make bot
make agent
```

> Uniswap V2 tools are unavailable on Sepolia fork — the router is not deployed on Sepolia. Only ETH, WETH, LINK, and Reputation Registry sessions are registered by default.

---

## Sepolia Deployment (Live)

Requires `SEPOLIA_RPC_URL` (Alchemy endpoint) and `SEPOLIA_PRIVATE_KEY` funded with Sepolia ETH.

There's no local node to start for a live deployment, so this flow skips step 1 above.

**Step 1 — Deploy the shared protocol to live Sepolia:**

```bash
make deploy ARGS="sepolia"
```

`ARGS="sepolia"` broadcasts to the real `SEPOLIA_RPC_URL` and (if `ETHERSCAN_API_KEY` is set) verifies contracts on Etherscan.

**Step 2 — Start and configure Vault:**

```bash
docker run -d \
  -e VAULT_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e SKIP_SETCAP=true \
  -p 8200:8200 \
  --name vault-dev \
  hashicorp/vault:latest

make vault
```

**Step 3 — Sync the database:**

```bash
make db
```

**Step 4 — Deploy your wallet:**

> Open [app/deploy_wallet.py](../app/deploy_wallet.py) and set the network to `"sepolia"`, then run:

```bash
make deploy-wallet
```

On Sepolia, `live_network.py` submits UserOps through the Alchemy bundler — no `SEPOLIA_BUNDLER` key is used. If `ETHERSCAN_API_KEY` is set, both `SHOracle` and `SessionHandler` are automatically verified on Etherscan after deployment.

> Uniswap V2 is not deployed on Sepolia. Swap, liquidity, and quote tools are unavailable — any attempt will fail because no `uniswapv2_router` session key is registered.

**Step 5 — Start:**

```bash
make bot
make agent
```

### Deployed Contracts (Sepolia)

| Contract | Address |
|---|---|
| `SessionHandler` | `0x202DCf56889F9b11F68f636e7567Ac14B9B1D249` |
| `SHOracle` | `0x6C1A1Da2518C456a097A082fc9f48d76f23F0aaC` |
| `IdentityRegistry` (canonical ERC-8004) | `0x8004A818BFB912233c491871b3d84c89A494BD9e` |
| `ReputationRegistry` (canonical ERC-8004) | `0x8004B663056A597Dffe9eCcC1965A193B7388713` |
