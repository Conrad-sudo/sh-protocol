-include .env

.PHONY: all test clean deploy install snapshot anvil bot db agent vault fund deploy-wallet setup-agent setup-test setup-bot identity-test auth-test py-test e2e-test agent-smoke api



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

# Full auth flow against a throwaway DB: signup, login, refresh rotation and reuse detection,
# token forgery, SIWE binding, the deployer check and the Telegram link nonce. Also offline.
auth-test:
	.venv/bin/python3 app/tests/test_auth.py

# Everything that runs without a chain.
py-test: identity-test auth-test

# The real journey against a running fork: signup -> SIWE -> deploy -> every owner action -> the
# eth_call simulations, plus a faked sequencer outage where the chain has one (Arbitrum). Sepolia
# unless ARGS names another fork, e.g. `make e2e-test ARGS=arbitrum-fork`. Needs `make vault`, that
# fork running and `make setup-test ARGS=<the fork>` first. Refuses to run if it is not on a local fork.
e2e-test:
	.venv/bin/python3 app/tests/test_e2e_fork.py $(ARGS)

# The prompt-regression gate: a REAL conversation against the fork, checking the agent still
# reaches the right tools in the right order now that the prompt no longer spells out an id
# argument. Costs Anthropic credits. Needs the same stack as e2e-test.
agent-smoke:
	.venv/bin/python3 app/tests/test_agent_smoke.py

unit-test:
	forge test --match-path test/unit/SHProtocolTest.t.sol -vvvv

mainnet-uniswap-test:
	forge test --match-path test/fork/SHUniswapV2Test.t.sol --fork-url $(MAINNET_RPC_URL) -vvvv

sepolia-uniswap-test:
	forge test --match-path test/fork/SHSepoliaUniswapV2Test.t.sol --fork-url $(SEPOLIA_RPC_URL) -vvvv
pancakeswap-test:
	forge test --match-path test/fork/SHPancakeswapV2Test.t.sol --fork-url $(BSC_RPC_URL) -vvvv

arbitrum-uniswap-test:
	forge test --match-path test/fork/SHArbitrumUniswapV2Test.t.sol --fork-url $(ARB_RPC_URL) -vvvv

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


mainnet-fork:
	anvil --fork-url $(MAINNET_RPC_URL) --fork-block-number $$(cast block-number --rpc-url $(MAINNET_RPC_URL))

sepolia-fork:
	anvil --fork-url $(SEPOLIA_RPC_URL) --fork-block-number $$(cast block-number --rpc-url $(SEPOLIA_RPC_URL))

bsc-fork:
	anvil --fork-url $(BSC_RPC_URL) --fork-block-number $$(cast block-number --rpc-url $(BSC_RPC_URL))

arb-fork:
	anvil --fork-url $(ARB_RPC_URL) --fork-block-number $$(cast block-number --rpc-url $(ARB_RPC_URL))

# Alias so the target name matches the network name every later step takes as ARGS
# ("arbitrum-fork" in the chains/rpcs tables and deploy_wallet.LIVE_PRIVATE_KEY_ENV). Every other
# fork target already matches its network name; `arb-fork` predates the app-side wiring.
arbitrum-fork: arb-fork

celo-fork:
	anvil --fork-url $(CELO_RPC_URL) --fork-block-number $$(cast block-number --rpc-url $(CELO_RPC_URL))


# ── Funding Wallets────────────────────────────────────────────────────────────────────

# anvil_setBalance is a local-fork-only cheat RPC, so this always targets LOCAL_RPC_URL
# regardless of which network ARGS names. SEPOLIA_ACCOUNT is the deployer, protocol owner and
# bundler on every forked network (see deploy_wallet.LIVE_PRIVATE_KEY_ENV and anvil.py), so
# there is one address to fund whatever ARGS names.
FUND_ADDRESS := $(SEPOLIA_ACCOUNT)

# anvil_setBalance only exists on a local node, so this runs for fork networks and bare "anvil"
# and skips everything else. Stated as an ALLOWLIST on purpose: the previous form skipped a
# hardcoded '^(sepolia|bsc)$' and so fired setBalance at LOCAL_RPC_URL for any other live name
# (mainnet, celo, arbitrum). An allowlist makes a new or misspelt network skip harmlessly rather
# than aim a cheat RPC at a real deployment.
#
# Plain "anvil" funds FUND_ADDRESS even though its own deploys use the pre-funded ANVIL_* keys —
# harmless, and it keeps `make fund ARGS=anvil` from looking like a failure.
fund:
	@if echo "$(ARGS)" | grep -qE 'fork|^anvil$$'; then \
		cast rpc anvil_setBalance $(FUND_ADDRESS) $(100_ETH) --rpc-url $(LOCAL_RPC_URL); \
	else \
		echo "Skipping fund — '$(ARGS)' has no local Anvil node to send anvil_setBalance to."; \
	fi



# ── Deployment Arguments ────────────────────────────────────────────────────────────


# Assembled from the three pieces that actually vary, rather than spelled out per network —
# every branch below used to repeat the same --rpc-url/--broadcast boilerplate to change one of:
#
#   DEPLOY_RPC     the local Anvil node for anvil and every fork; the real endpoint live
#   DEPLOY_SIGNER  Anvil's burner on plain anvil, SEPOLIA_ACCOUNT everywhere else — see
#                  deploy_wallet.LIVE_PRIVATE_KEY_ENV; one address deploys, owns and bundles
#   DEPLOY_EXTRA   per-network workarounds, each documented where it is set

DEPLOY_RPC    := $(LOCAL_RPC_URL)
DEPLOY_SIGNER := --sender $(ANVIL_ACCOUNT) --private-key $(ANVIL_PRIVATE_KEY)
DEPLOY_EXTRA  :=

# Forks inherit real chain state, so they must not sign with the Anvil burner: it is
# EIP-7702-delegated on real mainnet/Sepolia/BSC and a fork inherits that code.
ifneq ($(findstring fork,$(ARGS)),)
	DEPLOY_SIGNER := --sender $(SEPOLIA_ACCOUNT) --private-key $(SEPOLIA_PRIVATE_KEY)
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
	DEPLOY_EXTRA := --legacy --skip-simulation
endif

# Live Sepolia is the only target that leaves the local node: real endpoint, Etherscan
# verification, and no --sender (Forge derives the sender from --private-key).
ifeq ($(ARGS),sepolia)
	DEPLOY_RPC    := $(SEPOLIA_RPC_URL)
	DEPLOY_SIGNER := --private-key $(SEPOLIA_PRIVATE_KEY)
	DEPLOY_EXTRA  := --verify --etherscan-api-key $(ETHERSCAN_API_KEY) -vvvv
endif

# strip: DEPLOY_EXTRA is empty for most networks, which would otherwise leave a trailing space.
NETWORK_ARGS := $(strip --rpc-url $(DEPLOY_RPC) $(DEPLOY_SIGNER) --broadcast $(DEPLOY_EXTRA))


# Depends on fund: both broadcast as SEPOLIA_ACCOUNT, which on a fork inherits the forked chain's
# real balance — zero on mainnet-fork/bsc-fork. Without the top-up first, the very first broadcast
# fails for lack of gas. `fund` self-skips on live networks, and Make runs it once per invocation,
# so the setup chains below are unaffected.
deploy: fund
	forge script script/DeploySHProtocol.s.sol $(NETWORK_ARGS)



# ── Python ────────────────────────────────────────────────────────────────────

db:
	.venv/bin/python3 app/db.py

# Depends on fund for the same reason as deploy — deploy_wallet.py signs with that same account,
# and this makes a standalone `make deploy-wallet ARGS=<fork>` work without remembering to fund.
deploy-wallet: fund
	.venv/bin/python3 app/deploy_wallet.py $(ARGS)


bot:
	.venv/bin/python3 app/telebot.py

agent:
	.venv/bin/python3 app/smart_wallet_agent.py

# --workers 1 is not a default worth changing: tx_sender hands out bundler nonces from a cache
# guarded by a process-wide lock, so a second worker process means a second nonce counter for the
# same EOA and transactions that silently replace each other. --reload is for development.
# --app-dir rather than `cd app`: db.DB_PATH is "./app/wallet.db", relative to the repo root, so
# the working directory has to stay here while app/ goes on sys.path.
api:
	.venv/bin/uvicorn --app-dir app api:app --reload --workers 1 --port 8000


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

