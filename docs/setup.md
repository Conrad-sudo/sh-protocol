# Setup Guides

Before running any setup, make sure you have completed the one-time steps:

1. **Clone and install** — see the root [README](../README.md#clone-and-install).
2. **Environment variables** — create `.env` as described in [Vault & Security](vault-security.md#environment-variables).
3. **Which account to run as** — identity is an application account (`users.id`), not a Telegram chat id. For the local harnesses (`make deploy-wallet`, `make agent`) set:

   ```env
   APP_USER_ID=1
   ```

   `deploy_wallet.resolve_harness_user` reads it, and `smart_wallet_agent.main` uses the same resolution. If `APP_USER_ID` is unset it falls back to translating `TELEGRAM_CHAT_ID` through the `users` table — which is what keeps a pre-2026-09-10 setup working, since the migration created an account for your old chat id and recorded it as `users.telegram_chat_id`. A raw chat id is never used as a key any more, so an unlinked one is an error rather than a silent new account.

   > **Telegram is optional.** Only the Telegram bot (`make bot`) needs a `TELEGRAM_TOKEN` (from [@BotFather](https://t.me/BotFather)), `TELEGRAM_BOT_USERNAME`, and a real Telegram account. The bot serves **only chats that have been linked** to an account through the web app's deep link (`POST /api/integrations/telegram/link`); an unlinked chat is refused. For the interactive CLI (`make agent`), skip all of it and just set `APP_USER_ID`.

4. **Web API secrets** — `make api` needs `JWT_SECRET` (any long random string; `python -c "import secrets;print(secrets.token_hex(32))"`). `GOOGLE_CLIENT_ID` is needed only for Google sign-in, `TELEGRAM_BOT_USERNAME` only to mint Telegram deep links, and `CORS_ORIGINS` (comma-separated, default `http://localhost:3000`) must list your front end since the refresh cookie requires credentialed CORS. Set `COOKIE_SECURE=0` for local http development. `SIWE_DOMAIN` (comma-separated, default `localhost:3000`) is the site a wallet-binding message must name — set it to `mitfah.com` in production; a message written for any other site is refused, which is what stops a phishing page from binding a victim's address.
   >
   > **Google sign-in (optional).** Until `GOOGLE_CLIENT_ID` is set, the web app hides every Google button. To turn it on:
   > 1. In [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials), create an **OAuth client ID** of type **Web application**.
   > 2. Under **Authorized JavaScript origins**, add `http://localhost:3000` and `http://localhost` (dev), and later `https://mitfah.com`. No redirect URI is needed — the button uses a popup.
   > 3. Put the client ID in `.env` as `GOOGLE_CLIENT_ID=…`. The API uses it to check each token's audience, and `web/vite.config.ts` reads the same value for the button, so restart both `make api` and `npm run dev`.
   > 4. Publishing the OAuth consent screen needs a homepage, privacy policy and terms URLs on your domain (the web app's legal pages arrive in a later phase). While the app is in *Testing* mode, only the test users you list can sign in.
   >
   > **The LLM provider is optional too.** The agent defaults to Anthropic's Claude (`ANTHROPIC_API_KEY`); swap in any other [LangChain chat model](https://python.langchain.com/docs/integrations/chat/) with a small edit to `app/smart_wallet_agent.py` (see [docs/app.md](app.md#section-3--langchain-agent)) and that key is no longer required.

---

## The Setup Sequence

Every network (Anvil, Ethereum mainnet fork, Sepolia fork, live Sepolia, BSC fork, live BSC) follows the same six steps. The sections below give the exact commands for each network — this is the shape of what's happening and why the order matters:

1. **Start the network** — `make anvil`, `make mainnet-fork`, `make sepolia-fork`, or `make bsc-fork`. This must be running before anything else, since every later step talks to it over RPC. (Live deployments skip this — there's no local node to start.)
2. **Deploy the shared protocol** — `make deploy ARGS="<network>"`. Runs `script/DeploySHProtocol.s.sol`, which deploys the infrastructure shared by all users: `EntryPoint`, mocks (on Anvil), `SHOracle`, `SHTreasury`, `SHRegistry`, `SpendingLimitModule`, and `SHFactory` (with the module set via the factory constructor). Only needs to be re-run when this infra doesn't exist yet on the target chain (e.g. after restarting Anvil, which wipes all chain state).
3. **Start and configure Vault** — start the Vault Docker container, then run `make vault`. Vault holds the Transit key that encrypts/decrypts session keys; it must be ready before any session key is created in step 5.
4. **Sync the database** — `make db`. Seeds reference data (token addresses, selectors, RPC URLs, Chainlink feed addresses) and, critically, reads the `SHFactory` address out of the Forge broadcast file written in step 2 (`broadcast/DeploySHProtocol.s.sol/<chain_id>/run-latest.json`) into the `factory` table. This step must run *after* step 2 — `deploy_wallet.py` resolves the factory address from the DB, not from the broadcast file directly. Safe to re-run any time; it's idempotent.

   > **Watch for stale addresses.** Every re-run of `forge script script/DeploySHProtocol.s.sol --broadcast` on the same chain overwrites that chain's `run-latest.json` with a fresh set of addresses (new nonces → different addresses for every contract, including `SHFactory`). If you deploy again without re-running `make db`, the DB keeps pointing at the old (now-wrong) addresses — this can fail in a confusing way, since the old address might still have *some* contract's bytecode at it (e.g. a previous deployment's `SpendingLimitModule`), producing an empty-revert rather than an obvious "no code" error.

5. **Deploy your wallet** — `make deploy-wallet ARGS="<network>"`. Calls `SHFactory.deployWallet(dailyLimitUsd, windowDuration, watchedTokens, sessionKey, trustedSpenders)` to create a per-user `SessionHandler` in **one transaction**: `SpendingLimitModule` is installed as a spending-cap hook seeded with a $50k/24h cap over the network's default watched tokens, the wallet's single session key is authorized, and the chain's V2 router is granted as a trusted spender. It is funded with ETH by the same call (`deployWallet` is `payable`).

   > **One transaction, not three.** `addSession` and `addTrustedSpender` used to be separate owner-signed follow-ups. They are now seeded inside `initialize`, so `deploy_wallet.py`'s `add_default_session()` / `trust_router()` are recovery helpers only and are no longer called by `__main__`. Because the wallet is deployed with CREATE2, its address is known before the transaction is sent, which is what lets the session key (whose Vault entry is keyed by wallet address) be minted up front and passed in.

   > **Re-running gives you a NEW wallet, not the same one.** The CREATE2 salt is derived from `(deployer, factory.deployCount[deployer])` and the factory consumes that counter on every deploy, so each `make deploy-wallet` lands on a fresh address with no salt bookkeeping on the Python side. It does **not** migrate anything: `session_handlers` is overwritten for that `chat_id`, and the previous wallet keeps its prefund, tokens and LP positions with nothing pointing at it. The script warns when it is about to do this — withdraw first if the old wallet holds anything you want.

   > **`ARGS` here must exactly match** the network you started in step 1 and deployed to in step 2 (`"anvil"`, `"mainnet-fork"`, `"sepolia-fork"`, `"sepolia"`, `"bsc-fork"`, or `"bsc"`) — `deploy_wallet.py`'s `deploy()` dispatcher passes it straight through to `network`. If it doesn't match, the script will look up the wrong factory address (or none at all) and fail. Omitting `ARGS` defaults to `"anvil"`, matching `make deploy`'s own no-`ARGS` default.

6. **Start talking to it** — `make bot` (Telegram) or `make agent` (interactive CLI).

> **Shortcut — `make setup-agent ARGS="<network>"`.** Runs steps 2, 4, 5, and 6 (deploy → fund → db → deploy-wallet → agent) in one command, stopping if any step fails, then drops straight into the interactive CLI agent at the end. `fund` here is a balance top-up via `anvil_setBalance` (see the Makefile's "Funding Wallets" section) — it automatically skips itself on live networks (`sepolia`/`bsc`), where there's no local Anvil node to fund, so this chain is safe to use for all six networks without special-casing. Steps 1 (start the network) and 3 (Vault) are *not* part of this chain — start the network first (skip this for live deployments), and make sure Vault is already running and configured (it persists across redeploys, so you don't need to re-run `make vault` every time). The same `ARGS` value is passed through to `deploy`, `fund`, and `deploy-wallet` internally, so it must be one of the network names listed in step 5 above.
>
> **The full local setup in two commands** (once Vault is configured):
>
> ```bash
> make <network>-fork    # or `make anvil` for plain local Anvil — see step 1 above
> make setup-agent ARGS="<network>"
> ```
>
> e.g. `make sepolia-fork` followed by `make setup-agent ARGS="sepolia-fork"`. For a live deployment (no local node to start), just run `make setup-agent ARGS="sepolia"` (or `"bsc"`) on its own.
>
> **Two siblings run the identical chain and differ only in what they leave you in:** `make setup-bot ARGS="<network>"` ends in the Telegram bot instead of the CLI agent, and `make setup-test ARGS="<network>"` stops after `deploy-wallet` with no front end, for running tests against a freshly deployed stack.

> **Celo is not yet supported end-to-end.** The Python app layer has scaffolding for it (token list, chain ID, network routing), but `HelperConfig.s.sol` has no Celo chain ID branch, so step 2 (`make deploy ARGS="celo-fork"` or similar) cannot succeed on Celo until that's added on the Solidity side. Note also that `wrap_eth` is meaningless on Celo — CELO is natively an ERC-20 with no wrapping step. See [docs/app.md](app.md) for what's already wired up.

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

(No `ARGS` needed — the default `NETWORK_ARGS` in the Makefile already points at `http://127.0.0.1:8545` with the Anvil default account.) This deploys the full mock stack (EntryPoint, ERC20Mocks, MockV3Aggregators, MockIdentityRegistry, MockReputationRegistry, SHOracle, SHTreasury, SHRegistry, SpendingLimitModule, SHFactory).

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

```bash
make deploy-wallet
```

(No `ARGS` needed — `deploy_wallet.py` defaults to `"anvil"` when none is given, matching `make deploy`'s own default. Pass `ARGS="anvil"` explicitly if you'd rather not rely on the default.)

Must be re-run (along with steps 1, 2, and 4) whenever Anvil is restarted — chain state, including the deployed protocol, is wiped on restart.

> **Shortcut — full setup in two commands** (once Vault, step 3, is configured):
>
> ```bash
> make anvil
> make setup-agent
> ```
>
> Runs steps 2, 4, and 5 above plus a balance top-up (`fund`), finishing with the interactive CLI agent below (not the Telegram bot in step 6). No `ARGS` needed for either command — both default to plain Anvil. See [The Setup Sequence](#the-setup-sequence) above.

**Step 6 — Start the Telegram bot:**

```bash
make bot
```

**Step 7 — Chat:**

```
What is my wallet address?
What is my USDC balance?
Send 10 LINK to Sandy
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

```bash
make deploy-wallet ARGS="mainnet-fork"
```

**Step 6 — Start:**

```bash
make bot      # Telegram bot
make agent    # Interactive CLI
```

> Re-run steps 1, 2, and 4 after any Anvil restart, and re-run `make vault` after any Vault container restart — fork state is wiped on restart.

> **Shortcut — full setup in two commands** (once Vault, step 3, is configured):
>
> ```bash
> make mainnet-fork
> make setup-agent ARGS="mainnet-fork"
> ```
>
> Runs steps 2, 4, and 5 above plus a balance top-up (`fund`), finishing with `make agent` (not `make bot`) from step 6. See [The Setup Sequence](#the-setup-sequence) above.

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

`ARGS="sepolia-fork"` tells the Makefile to sign with `SEPOLIA_ACCOUNT` / `API_BUNDLER` while still broadcasting to the local fork at `http://127.0.0.1:8545`.

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

```bash
make deploy-wallet ARGS="sepolia-fork"
```

**Step 6 — Start:**

```bash
make bot
make agent
```

> Uniswap V2 is officially deployed on Sepolia, so swap/liquidity tools work here. The wallet is seeded with a $50k/24h USD cap over the default Sepolia watched tokens (WETH, USDC, LINK) and one session key that can drive any of them — see [docs/app.md](app.md#deploy_walletpy).

> **Shortcut — full setup in two commands** (once Vault, step 3, is configured):
>
> ```bash
> make sepolia-fork
> make setup-agent ARGS="sepolia-fork"
> ```
>
> Runs steps 2, 4, and 5 above plus a balance top-up (`fund`), finishing with `make agent` (not `make bot`) from step 6. See [The Setup Sequence](#the-setup-sequence) above.

---

## Local Setup (BSC Fork)

Requires `BSC_RPC_URL` in `.env`.

**Step 1 — Start a BSC fork:**

```bash
make bsc-fork
```

**Step 2 — Deploy the shared protocol:**

```bash
make deploy ARGS="bsc-fork"
```

`ARGS="bsc-fork"` signs with `MAINNET_DEPLOYER_PK` (the placeholder deployer key baked into `HelperConfig.s.sol`) and broadcasts with `--legacy --skip-simulation` — BSC's fee-history data and this fork's `baseFeePerGas: 0` blocks both confuse Forge's default gas estimation, so the Makefile routes around it (see the comment above the `bsc-fork` branch in `Makefile` for the full explanation). If the deployer needs a balance bump on the fork first, `make fund ARGS="bsc-fork"` sets it directly via `anvil_setBalance`.

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

```bash
make deploy-wallet ARGS="bsc-fork"
```

Seeds the wallet with a $50k/24h USD cap over the default BSC watched tokens (WBNB, USDC, USDT) and one session key — see [docs/app.md](app.md#deploy_walletpy).

**Step 6 — Start:**

```bash
make bot
make agent
```

> **Shortcut — full setup in two commands** (once Vault, step 3, is configured):
>
> ```bash
> make bsc-fork
> make setup-agent ARGS="bsc-fork"
> ```
>
> Runs steps 2, 4, and 5 above plus a balance top-up (`fund`), finishing with `make agent` (not `make bot`) from step 6. See [The Setup Sequence](#the-setup-sequence) above.

---

## Sepolia Deployment (Live)

Requires `SEPOLIA_RPC_URL` (Alchemy endpoint), and `API_BUNDLER` and `TELEGRAM_BUNDLER` funded with Sepolia ETH — the first deploys and bundles for the API, the second bundles for the Telegram bot.

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

```bash
make deploy-wallet ARGS="sepolia"
```

On Sepolia, as everywhere, the app is its own bundler: `bundler.py` submits UserOps inside `handleOps` from `API_BUNDLER` (the API) or `TELEGRAM_BUNDLER` (the bot), which the EntryPoint repays out of each wallet's prefund. Nothing tops those keys up on a live chain, so keep them funded. If `ETHERSCAN_API_KEY` is set, both `SHOracle` and `SessionHandler` are automatically verified on Etherscan after deployment.

> Uniswap V2 is officially deployed on Sepolia, so swap, liquidity, and quote tools are available here too. The wallet is seeded with a $50k/24h USD cap over WETH/USDC/LINK and one session key that can drive any external call within that cap.

**Step 5 — Start:**

```bash
make bot
make agent
```

> **Shortcut — full setup in one command** (once Vault, step 2, is configured):
>
> ```bash
> make setup-agent ARGS="sepolia"
> ```
>
> Since there's no local node to start for a live deployment, this alone covers steps 1, 3, and 4 above, finishing with `make agent` (not `make bot`) from step 5. The chain's `fund` step (a local-fork-only balance top-up) automatically no-ops on live networks, so it's safe to include here. **This broadcasts a real transaction to live Sepolia** — make sure that's what you intend before running it.

### Deployed Contracts (Sepolia)

| Contract | Address |
|---|---|
| `IdentityRegistry` (canonical ERC-8004) | `0x8004A818BFB912233c491871b3d84c89A494BD9e` |
| `ReputationRegistry` (canonical ERC-8004) | `0x8004B663056A597Dffe9eCcC1965A193B7388713` |

> `SessionHandler`, `SpendingLimitModule`, and `SHOracle` addresses are deployment-specific and intentionally omitted here — they change on every fresh `forge script --broadcast` run (different deployer nonces). After running `make deploy ARGS="sepolia"`, the fresh addresses are in the Forge broadcast file (`broadcast/DeploySHProtocol.s.sol/11155111/run-latest.json`) and get synced into `wallet.db` by `make db`.

---

## BSC Deployment (Live)

Requires `BSC_RPC_URL` (Alchemy endpoint) and `MAINNET_DEPLOYER_PK`/`MAINNET_DEPLOYER_ADDRESS` (see `Makefile`) funded with real BNB, plus `BSC_PRIVATE_KEY` for the Python wallet-deployment step.

There's no local node to start for a live deployment, so this flow skips step 1 above.

**Step 1 — Deploy the shared protocol to live BSC:**

```bash
make deploy ARGS="bsc"
```

> The `Makefile`'s `ARGS` matching for BSC only has an explicit branch for `bsc-fork` (which broadcasts to a local fork with `--legacy --skip-simulation`). A plain `ARGS="bsc"` falls through to the generic `NETWORK_ARGS` default (Anvil's burner key), which is **not** what you want for a real broadcast — replace `NETWORK_ARGS` with a BSC-specific branch (mirroring the `sepolia` branch: `--rpc-url $(BSC_RPC_URL) --private-key <funded-bsc-key> --broadcast`) before deploying live. This is a real gap in the current `Makefile`, not a documentation oversight — double-check the resolved command before broadcasting.

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

```bash
make deploy-wallet ARGS="bsc"
```

BSC is self-bundled like every other network: `bundler.py` signs `handleOps` with `API_BUNDLER` or `TELEGRAM_BUNDLER`, depending on which process is running (see `bundler.resolve_bundler`). On `bsc-fork` `make fund` tops both up; on live BSC they must hold real BNB. Live BSC has not been exercised yet.

**Step 5 — Start:**

```bash
make bot
make agent
```

> **Shortcut — full setup in one command** (once Vault, step 2, is configured):
>
> ```bash
> make setup-agent ARGS="bsc"
> ```
>
> Since there's no local node to start for a live deployment, this alone covers steps 1, 3, and 4 above, finishing with `make agent` (not `make bot`) from step 5. The chain's `fund` step (a local-fork-only balance top-up) automatically no-ops on live networks, so it's safe to include here. **This broadcasts a real transaction to live BSC** — make sure that's what you intend before running it. Also note the `make deploy` gap flagged in step 1 above: confirm `NETWORK_ARGS` actually resolves to a funded BSC broadcast before relying on this shortcut for a live BSC deploy.
