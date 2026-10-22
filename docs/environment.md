# Environment Variables

Everything the contracts, the API, the Telegram bot and the CLI agent read from `.env`. Create it in the project root:

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

# Passkeys — the session keys, which live on users' devices; the server keeps only their public keys
WEB_APP_URL=http://localhost:3000               # the web app's address; Telegram's Confirm links (/c/<token>) and /web open it
WEBAUTHN_RP_ID=                                 # optional; default SIWE_DOMAIN's host: localhost in development, mitfah.com in production
WEBAUTHN_ORIGINS=                               # optional; comma list of origins an approval may come from; default WEB_APP_URL + CORS_ORIGINS

# Optional — Etherscan contract verification after Sepolia deployment, and the History tab's
# activity outside Mitfah on Ethereum, Sepolia, Arbitrum and Celo
ETHERSCAN_API_KEY=your_etherscan_api_key_here
# Optional — the History tab's activity outside Mitfah on BSC (Etherscan's free tier excludes BSC)
NODEREAL_API_KEY=your_nodereal_api_key_here
# Optional — the same on Base, which Etherscan's free tier excludes too (the key in BASE_RPC_URL)
ALCHEMY_API_KEY=your_alchemy_key
```

> **Session keys are passkeys, and there is no key store to run.** Each device's session key is a passkey (WebAuthn, P-256) made in the user's browser and kept by the device or its password manager. The wallet holds its public key; so does `wallet.db`, to name the device. Nothing the server stores can sign anything, which is why there is no Vault any more (see [THREAT_MODEL.md](../THREAT_MODEL.md) §4.1).
>
> **`WEBAUTHN_RP_ID` is the domain every passkey is made for, and a passkey is bound to it for life.** Changing it later strands every passkey made under the old one: each user would have to make a new one and approve it on each wallet again. It must be the domain the web app is served from (`mitfah.com` in production); left unset it is `SIWE_DOMAIN`'s host, which already is. Browsers allow passkeys only over https, with `localhost` the one exception, so development works on `http://localhost:3000` and nothing else plain-http.
>
> **`WEB_APP_URL` is where Telegram's Confirm button sends the user** (`/c/<token>`, the page that asks for the passkey), so in production it must be the public https address. Left unset, the bot uses the first of `CORS_ORIGINS`. `WEBAUTHN_ORIGINS` names the pages an approval may come from (scheme, host and port, no path); every approval is checked against it, since the relying-party id alone would admit any page under the domain.
>
> **The Vault variables are gone.** `VAULT_ADDR`, `VAULT_ROLE_ID`, `VAULT_SECRET_ID` and `VAULT_SECRET_ID_ACCESSOR` are not read any more: delete them. The Vault container can be stopped once the API and the bot run this version (their first `make db` drops the tables that held its ciphertexts).
>
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
