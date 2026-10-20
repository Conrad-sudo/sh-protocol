-include .env

.PHONY: all test clean deploy install snapshot anvil bot db agent vault fund deploy-wallet setup-agent setup-test setup-bot identity-test auth-test custom-tokens-test history-test movements-test explorers-test explorers-live speed-test bundler-test telebot-test chat-limits-test redact-test migrations-test uniswap-v3-test positions-test position-tools-test py-test e2e-test agent-smoke load-test api api-prod



100_ETH:=0x56bc75e2d63100000

vault:
	@bash setup_vault.sh


all: clean install update build

# ── Foundry ───────────────────────────────────────────────────────────────────

clean:
	forge clean

install:
	forge install

update:
	forge update

build:
	forge build

test:
	forge test

snapshot:
	forge snapshot


# ── Testing ────────────────────────────────────────────────────────────────────


# Guards that no agent tool exposes the user's identity to the model, so a conversation can never
# talk the agent into acting as somebody else. Pure Python, no chain or RPC, so it is cheap to run
# on every change to app/tools.py.
identity-test:
	.venv/bin/python3 app/tests/test_identity.py

# Full auth flow against a throwaway DB: SIWE sign-in, refresh rotation and reuse detection,
# token forgery, the deployer check, signed (EIP-712) contacts and the Telegram link nonce. Also offline.
auth-test:
	.venv/bin/python3 app/tests/test_auth.py

# Tokens a user adds by address: the add rules, the routes, and how the tools price and name them.
# Against a fake chain, so also offline.
custom-tokens-test:
	.venv/bin/python3 app/tests/test_custom_tokens.py

# The History tab and the short chat memory: recording, describing and settling transactions,
# the history routes, and the conversation starting afresh after a send. Fake chain, so offline.
history-test:
	.venv/bin/python3 app/tests/test_history.py

# What each transaction moved, for the History tab's filters: owner calldata, and the assistant's
# receipts (token Transfers, the op's executions, wrapped-native events). Scripted data, so offline.
movements-test:
	.venv/bin/python3 app/tests/test_tx_movements.py

# Reading activity outside Mitfah from Etherscan, NodeReal and Alchemy: paging, block windows, rate
# limits, and API keys kept out of errors and logs. Scripted answers, so offline.
explorers-test:
	.venv/bin/python3 app/tests/test_explorers.py

# The same, against the real services: read-only, with the keys in .env. Not part of py-test.
explorers-live:
	.venv/bin/python3 app/tests/check_explorers_live.py

# What keeps the assistant quick: the chain id asked once, independent reads run together, the
# wallet checks every transaction tool runs itself, and a UserOp's hash worked out locally. Offline.
speed-test:
	.venv/bin/python3 app/tests/test_speed.py

# What a UserOp pays for posting its data to Ethereum on the live L2s (Base's L1 fee, Arbitrum's L1
# gas), as preVerificationGas, and that forks pay none; and tx_sender's send/confirm split (nonces run
# ahead, a stalled transaction is replaced at a higher fee). A fake node, so offline.
bundler-test:
	.venv/bin/python3 app/tests/test_bundler.py

# The Telegram bot: /chain offers only the user's networks the bot can reach (picked for them when
# there is one) and refuses a forged or group button press; replies name the network, and a "yes"
# after switching can't confirm the other network's quote; private chats only, one link; a flood
# past the chat limit makes no model call and gets one notice; replies keep their formatting (web
# app's rules) and are split to fit; the daily check reaches the right chats. Stand-ins for
# Telegram, the chain and the model, so offline.
telebot-test:
	.venv/bin/python3 app/tests/test_telebot.py

# The chat's rationing (app/chat_limits.py): the minute and day limits, one message running and one
# waiting per account, a refused message counting nothing, one notice a minute per kind of refusal,
# the slot freed after a turn that raises, and the web route on the same CHAT_RATE_LIMIT. A fake
# clock, so offline.
chat-limits-test:
	.venv/bin/python3 app/tests/test_chat_limits.py

# API keys kept out of HTTP errors, the model's tool results and the logs. Offline.
redact-test:
	.venv/bin/python3 app/tests/test_redact.py

# The numbered database migrations: a new database, one from before the numbering, each migration
# run once, a newer database refused. Throwaway files, so offline.
migrations-test:
	.venv/bin/python3 app/tests/test_migrations.py

# The in-repo Uniswap V3 toolkit: its tick and liquidity math against Solidity's own values, its
# addresses against script/Constants.s.sol, and every plan it builds (call order, approvals, the
# deadline multicall, burn at 100%) against a stand-in chain. Offline.
uniswap-v3-test:
	.venv/bin/python3 app/tests/test_uniswap_v3.py

# The dashboard's and Telegram's Uniswap V3 positions reader: spam and empty positions hidden, the
# native coin named as such, base token first, nothing raised when the chain read fails. Offline.
positions-test:
	.venv/bin/python3 app/tests/test_positions.py

# The assistant's Uniswap V3 liquidity tools: the range picked by name lands on the ticks that were
# shown, deposits count against the limit, and every refusal (unknown token, someone else's position,
# the wrong network's tools) holds; plus the per-network filter on which liquidity tools it sees.
position-tools-test:
	.venv/bin/python3 app/tests/test_position_tools.py

# Everything that runs without a chain.
py-test: identity-test auth-test custom-tokens-test history-test movements-test explorers-test speed-test bundler-test telebot-test chat-limits-test redact-test migrations-test uniswap-v3-test positions-test position-tools-test

# The real journey against a running fork: SIWE sign-in -> deploy -> every owner action -> the
# eth_call simulations, plus a faked sequencer outage where the chain has one (Arbitrum, Base). Sepolia
# unless ARGS names another fork, e.g. `make e2e-test ARGS=arbitrum-fork`. Needs `make vault`, that
# fork running and `make setup-test ARGS=<the fork>` first. Refuses to run if it is not on a local fork.
e2e-test:
	.venv/bin/python3 app/tests/test_e2e_fork.py $(ARGS)

# The prompt-regression gate: a REAL conversation against the fork, checking the agent still
# reaches the right tools in the right order now that the prompt no longer spells out an id
# argument. Costs DeepSeek credits. Needs the same stack as e2e-test.
agent-smoke:
	.venv/bin/python3 app/tests/test_agent_smoke.py

# How many chat turns one API process carries at once and the CPU each takes: the real API on a
# scratch database, a stand-in for DeepSeek, chats from many accounts at 8, 32, 64 and 128 at once
# (or ARGS="16 256"; it raises the API's CHAT_MAX_CONCURRENT to the highest). About 4 minutes. No
# chain, no credits.
load-test:
	.venv/bin/python3 app/tests/load_test.py $(ARGS)

unit-test:
	forge test --match-path test/unit/SHProtocolTest.t.sol -vvvv

mainnet-uniswap-test:
	forge test --match-path test/fork/SHMainnetUniswapV3Test.t.sol -vvvv

sepolia-uniswap-test:
	forge test --match-path test/fork/SHSepoliaUniswapV3Test.t.sol -vvvv
pancakeswap-test:
	forge test --match-path test/fork/SHPancakeswapV2Test.t.sol -vvvv

arbitrum-uniswap-test:
	forge test --match-path test/fork/SHArbitrumUniswapV3Test.t.sol -vvvv

base-uniswap-test:
	forge test --match-path test/fork/SHBaseUniswapV3Test.t.sol -vvvv

# SHSepoliaTest.t.sol was folded into the shared fork base (identity/reputation checks now run
# on every network), so sepolia-test is an alias of sepolia-uniswap-test.
sepolia-test: sepolia-uniswap-test


# ── Local node ─────────────────────────────────────────────────────────────────

# Plain local Anvil — no fork, no real chain state to inherit, so the pre-funded Anvil accounts
# are safe to use here (they are EIP-7702-delegated to drainers on real chains, which is why the
# fork targets below must not sign with them — see deploy_wallet.LIVE_PRIVATE_KEY_ENV).
anvil:
	anvil


# ── Forking ────────────────────────────────────────────────────────────────────

# Each fork listens on its own port, so several can run at once. Their chain IDs differ, so one API
# process serves them side by side. Keep these in step with the *-fork rows of RPCS in
# app/seed_data.py, which is where the app looks them up. Celo shares Sepolia's port: it has no
# deployment path yet, so it never runs beside another fork.
FORK_PORT_sepolia-fork  := 8545
FORK_PORT_bsc-fork      := 8546
FORK_PORT_mainnet-fork  := 8547
FORK_PORT_arbitrum-fork := 8548
FORK_PORT_base-fork     := 8549
FORK_PORT_celo-fork     := 8545
# The target's short name, so ARGS=arb-fork can never fall through to 8545 (the Sepolia fork's port).
FORK_PORT_arb-fork      := $(FORK_PORT_arbitrum-fork)

# fund and deploy talk to the node the fork in ARGS runs on, not to .env's LOCAL_RPC_URL (which
# stays the address for plain anvil). A LOCAL_RPC_URL given on the command line still wins.
ifneq ($(FORK_PORT_$(ARGS)),)
LOCAL_RPC_URL := http://127.0.0.1:$(FORK_PORT_$(ARGS))
endif

# $(call start_fork,<upstream RPC>,<network name>): forks the upstream at its latest block on that
# network's port, then gives every bundler key (API_BUNDLER_n, TELEGRAM_BUNDLER_n) 100 of the native
# coin each (`fund`) as soon as the node answers, so a fresh fork is ready to deploy to.
#
# Anvil runs in the background only so the funding can happen after it starts; the recipe then
# waits on it, so it stays in the foreground as before. The trap is what makes Ctrl+C stop it: a
# background job in a non-interactive shell ignores SIGINT itself. If funding fails the fork keeps
# running, and `make fund ARGS=<network>` can be re-run by hand.
#
# It refuses to start when a node already answers on the port: Anvil would fail to bind, but the
# readiness loop would see the OTHER node and fund that instead.
#
# `make -n` on these targets really starts the fork: the line calls $(MAKE), which -n still runs.
define start_fork
@if cast chain-id --rpc-url http://127.0.0.1:$(FORK_PORT_$(2)) >/dev/null 2>&1; then \
	echo "Port $(FORK_PORT_$(2)) is already in use. Is $(2) already running?"; \
	exit 1; \
fi; \
anvil --fork-url $(1) --fork-block-number $$(cast block-number --rpc-url $(1)) --port $(FORK_PORT_$(2)) & \
pid=$$!; \
trap 'kill $$pid 2>/dev/null' INT TERM EXIT; \
until cast chain-id --rpc-url http://127.0.0.1:$(FORK_PORT_$(2)) >/dev/null 2>&1; do \
	kill -0 $$pid 2>/dev/null || exit 1; \
	sleep 1; \
done; \
kill -0 $$pid 2>/dev/null || exit 1; \
if $(MAKE) --no-print-directory fund ARGS=$(2) >/dev/null; then \
	echo "Funded every bundler key with 100 native coin each on $(2)."; \
else \
	echo "Funding the bundlers on $(2) failed; the fork is still running. Retry: make fund ARGS=$(2)"; \
fi; \
wait $$pid
endef

mainnet-fork:
	$(call start_fork,$(MAINNET_RPC_URL),mainnet-fork)

sepolia-fork:
	$(call start_fork,$(SEPOLIA_RPC_URL),sepolia-fork)

bsc-fork:
	$(call start_fork,$(BSC_RPC_URL),bsc-fork)

arb-fork:
	$(call start_fork,$(ARB_RPC_URL),arbitrum-fork)

# Alias so the target name matches the network name every later step takes as ARGS
# ("arbitrum-fork" in the chains/rpcs tables and deploy_wallet.LIVE_PRIVATE_KEY_ENV). Every other
# fork target already matches its network name; `arb-fork` predates the app-side wiring.
arbitrum-fork: arb-fork

base-fork:
	$(call start_fork,$(BASE_RPC_URL),base-fork)

celo-fork:
	$(call start_fork,$(CELO_RPC_URL),celo-fork)


# ── Funding Wallets────────────────────────────────────────────────────────────────────

# anvil_setBalance is a local-fork-only cheat RPC, so this always targets LOCAL_RPC_URL — the port
# of the fork ARGS names (see FORK_PORT_* above). Every bundler key .env sets needs gas on a local
# node -- each process's pool, numbered from 1 (bundler.bundler_keys):
#   API_BUNDLER_1, _2, ...       the API's. The first is SEPOLIA_ACCOUNT: the deployer and protocol
#                                owner on every fork too (deploy_wallet.LIVE_PRIVATE_KEY_ENV)
#   TELEGRAM_BUNDLER_1, _2, ...  the Telegram bot's -- separate keys, so the two processes never
#                                hand out the same nonce (bundler.use_bundler_key)
# The addresses are derived from the keys rather than kept in .env twice. Recursively expanded (=),
# so cast only runs when `fund` does, and inside an @-silenced recipe, so no key is ever echoed.
BUNDLER_ADDRESSES = $(foreach key,$(filter API_BUNDLER_% TELEGRAM_BUNDLER_%,$(.VARIABLES)),$(shell cast wallet address --private-key $($(key))))

# anvil_setBalance only exists on a local node, so this runs for fork networks and bare "anvil"
# and skips everything else. Stated as an ALLOWLIST on purpose: the previous form skipped a
# hardcoded '^(sepolia|bsc)$' and so fired setBalance at LOCAL_RPC_URL for any other live name
# (mainnet, celo, arbitrum). An allowlist makes a new or misspelt network skip harmlessly rather
# than aim a cheat RPC at a real deployment.
#
# Plain "anvil" deploys with the pre-funded ANVIL_* keys, but both processes still bundle with
# their own keys there, so those are funded on it too.
fund:
	@if echo "$(ARGS)" | grep -qE 'fork|^anvil$$'; then \
		for address in $(BUNDLER_ADDRESSES); do \
			cast rpc anvil_setBalance $$address $(100_ETH) --rpc-url $(LOCAL_RPC_URL) || exit 1; \
		done; \
	else \
		echo "Skipping fund — '$(ARGS)' has no local Anvil node to send anvil_setBalance to."; \
	fi



# ── Deployment Arguments ────────────────────────────────────────────────────────────


# Assembled from the three pieces that actually vary, rather than spelled out per network —
# every branch below used to repeat the same --rpc-url/--broadcast boilerplate to change one of:
#
#   DEPLOY_RPC     the local Anvil node for anvil and every fork (each fork on its own port, see
#                  FORK_PORT_*); the real endpoint live
#   DEPLOY_SIGNER  Anvil's burner on plain anvil, SEPOLIA_ACCOUNT (API_BUNDLER_1's address)
#                  everywhere else — see deploy_wallet.LIVE_PRIVATE_KEY_ENV; one address deploys,
#                  owns, and bundles for the API
#   DEPLOY_EXTRA   per-network workarounds, each documented where it is set

DEPLOY_RPC    := $(LOCAL_RPC_URL)
DEPLOY_SIGNER := --sender $(ANVIL_ACCOUNT) --private-key $(ANVIL_PRIVATE_KEY)
DEPLOY_EXTRA  :=

# Forks inherit real chain state, so they must not sign with the Anvil burner: it is
# EIP-7702-delegated on real mainnet/Sepolia/BSC and a fork inherits that code.
# --offline: a fork reports a real chain ID, so after simulating, Forge looks up every outside
# contract on Etherscan/Sourcify and every unknown function selector online, one by one, only to
# label its traces. That took a fork deploy from ~1s to many minutes, and stalls it outright while
# Sourcify is erroring. A local deploy needs nothing from the network but the fork itself.
ifneq ($(findstring fork,$(ARGS)),)
	DEPLOY_SIGNER := --sender $(SEPOLIA_ACCOUNT) --private-key $(API_BUNDLER_1)
	DEPLOY_EXTRA  := --offline
endif

# --legacy: BSC's EIP-1559 fee-history data confuses Forge's fee estimator into deriving a bogus
# maxFeePerGas, which fails broadcast with a misleading "lack of funds" error even when the
# deployer is fully funded. Legacy (single gasPrice) transactions avoid it.
# --skip-simulation: these forks' blocks report baseFeePerGas=0, which trips Forge's separate
# pre-broadcast validation pass ("Setting up 1 EVM") into computing a bogus required balance (it
# misreports needing ~2 ether — exactly SHOracle's constructor value — with balance "0", even
# though the deployer is genuinely funded). Skipping that local check and broadcasting directly
# works fine; the deployer's real balance is enough. Applied to celo-fork as a precaution, since
# its fork snapshots can report baseFeePerGas=0 the same way.
ifneq ($(or $(findstring bsc-fork,$(ARGS)),$(findstring celo-fork,$(ARGS))),)
	DEPLOY_EXTRA += --legacy --skip-simulation
endif

# Live Sepolia is the only target that leaves the local node: real endpoint, Etherscan
# verification, and no --sender (Forge derives the sender from --private-key).
# --gas-estimate-multiplier 1000: Sepolia runs Glamsterdam's new gas prices, where storing state
# costs far more (deploying code ~1,540 gas/byte instead of 200). Forge 1.5.1 simulates with the
# old prices and sets every gas limit ~4-8x too low, so each tx ran out of gas (2026-10-15).
# 10x the simulated gas covers the worst case (SHFactory, which also deploys SessionHandler).
# You pay only for gas used, so the higher limit costs nothing. Drop it once Forge prices
# Glamsterdam itself.
ifeq ($(ARGS),sepolia)
	DEPLOY_RPC    := $(SEPOLIA_RPC_URL)
	DEPLOY_SIGNER := --private-key $(API_BUNDLER_1)
	DEPLOY_EXTRA  := --verify --etherscan-api-key $(ETHERSCAN_API_KEY) -vvvv --gas-estimate-multiplier 1000
endif

# strip: DEPLOY_EXTRA is empty for most networks, which would otherwise leave a trailing space.
NETWORK_ARGS := $(strip --rpc-url $(DEPLOY_RPC) $(DEPLOY_SIGNER) --broadcast $(DEPLOY_EXTRA))


# Depends on fund: both broadcast as SEPOLIA_ACCOUNT, which on a fork inherits the forked chain's
# real balance — zero on mainnet-fork/bsc-fork. Without the top-up first, the very first broadcast
# fails for lack of gas. `fund` self-skips on live networks, and Make runs it once per invocation,
# so the setup chains below are unaffected.
deploy: 
	forge script script/DeploySHProtocol.s.sol $(NETWORK_ARGS)



# ── Python ────────────────────────────────────────────────────────────────────

# db is a package now (app/db/), run as a module with app/ on the path; the working directory stays
# the repo root, where the deploy broadcasts it reads are.
db:
	PYTHONPATH=app .venv/bin/python3 -m db

# Depends on fund for the same reason as deploy — deploy_wallet.py signs with that same account,
# and this makes a standalone `make deploy-wallet ARGS=<fork>` work without remembering to fund.
deploy-wallet: fund
	.venv/bin/python3 app/deploy_wallet.py $(ARGS)


# The Telegram bot: one bot for every chain (MITFAH_API, MITFAH_USERNAME), each user picking the
# network with /chain. A network that doesn't answer as its chain at startup isn't offered, so on
# forks only the forks that are running are. Needs an up-to-date database (`make db`). Run exactly
# ONE copy: Telegram lets one process poll a bot (a second gets "Conflict" errors), and every turn
# bundles with the TELEGRAM_BUNDLER_n keys, whose nonces are counted per process.
bot:
	.venv/bin/python3 app/telebot.py

agent:
	.venv/bin/python3 app/smart_wallet_agent.py

# --workers 1 is not a default worth changing: tx_sender hands out bundler nonces from a cache
# guarded by in-process locks, so a second worker process means a second nonce counter for the
# same EOAs (API_BUNDLER_n) and transactions that silently replace each other. The Telegram bot is
# safe beside it only because it bundles with different keys (TELEGRAM_BUNDLER_n). The CLI agent
# (`make agent`) and `make agent-smoke` bundle with the API's keys, so don't run them beside the API.
# --reload is for development.
# --app-dir rather than `cd app`: the contract ABIs are read from ./out and the deploy records from
# ./broadcast, relative to the repo root, so the working directory has to stay here while app/ goes
# on sys.path. (The database follows APP_DB_PATH, or app/wallet.db.)
api:
	.venv/bin/uvicorn --app-dir app api:app --reload --workers 1 --port 8000

# Production, behind Caddy (deploy/Caddyfile): no --reload, listening on this machine only, and
# trusting the proxy's X-Forwarded-For so the per-address rate limits see each visitor rather than
# 127.0.0.1. One worker, for the same reason as above.
api-prod:
	.venv/bin/uvicorn --app-dir app api:app --workers 1 --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1


# ── Combined workflows ──────────────────────────────────────────────────────────

# Three variants of the same chain — deploy -> fund -> db -> deploy-wallet — differing only in what
# they leave you in at the end (stops if any step fails):
#   setup-agent  ... + agent   interactive CLI agent
#   setup-bot    ... + bot     the Telegram bot
#   setup-test   (nothing)     just the deployed stack, for running tests against
# Assumes Vault is already running and configured (`make vault`) — not part of this
# chain since, once started, Vault persists across redeploys and doesn't need to be
# re-run every time. Pass the same ARGS you'd give `deploy`/`deploy-wallet` individually,
# e.g. `make setup-agent ARGS="sepolia-fork"` — every step reads the same $(ARGS).
setup-agent: deploy fund db deploy-wallet agent
setup-test: deploy fund db deploy-wallet 
setup-bot: deploy fund db deploy-wallet bot
wipe-db:
	rm ./app/wallet.db
	rm ./app/wallet.db-shm
	rm ./app/wallet.db-wal
clear-broadcast:
	rm -rf ./broadcast/DeploySHProtocol.s.sol/31337/*
	rm -rf ./broadcast/DeploySHProtocol.s.sol/42161/*
	rm -rf ./broadcast/DeploySHProtocol.s.sol/8453/*
	rm -rf ./broadcast/DeploySHProtocol.s.sol/11155111/*
	rm -rf ./broadcast/DeploySHProtocol.s.sol/1/*
