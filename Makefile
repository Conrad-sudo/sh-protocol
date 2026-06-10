-include .env

.PHONY: all test clean deploy help install snapshot format anvil bot db agent vault

DEFAULT_ANVIL_KEY := 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
DEFAULT_ANVIL_KEY_2 := 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
DEFAULT_ANVIL_KEY_3 := 0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a
DEFAULT_ANVIL_ADDRESS := 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
DEFAULT_ANVIL_ADDRESS_2 := 0x70997970C51812dc3A010C7d01b50e0d17dc79C8

vault:
	@bash setup_vault.sh

help:
	@echo "Foundry"
	@echo "  make build              - Compile contracts"
	@echo "  make test               - Run Forge tests"
	@echo "  make snapshot           - Generate gas snapshot"
	@echo "  make format             - Format Solidity sources"
	@echo "  make clean              - Remove build artifacts"
	@echo "  make install            - Install Forge dependencies"
	@echo "  make update             - Update Forge dependencies"
	@echo "  make anvil              - Start a local Anvil node"
	@echo "  make deploy [ARGS=--network sepolia]  - Deploy SessionHandler"
	@echo ""
	@echo "Python"
	@echo "  make db                 - Initialise the SQLite database and run migrations"
	@echo "  make deploy-py          - Deploy contracts via web3.py and seed the database"
	@echo "  make agent              - Start the agent in interactive CLI mode"
	@echo "  make bot                - Start the Telegram bot"
	@echo ""
	@echo "Vault"
	@echo "  make vault              - Configure Vault and refresh .env credentials"

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

format:
	forge fmt

anvil:
	anvil -m 'test test test test test test test test test test test junk' --steps-tracing


mainnet-fork:
	anvil --fork-url $(MAINNET_RPC_URL) --fork-block-number $$(cast block-number --rpc-url $(MAINNET_RPC_URL))

sepolia-fork:
	anvil --fork-url $(SEPOLIA_RPC_URL) --fork-block-number $$(cast block-number --rpc-url $(SEPOLIA_RPC_URL))

NETWORK_ARGS := --rpc-url http://127.0.0.1:8545 --sender $(DEFAULT_ANVIL_ADDRESS) --private-key $(DEFAULT_ANVIL_KEY) --broadcast

ifeq ($(findstring --network sepolia,$(ARGS)),--network sepolia)
	ANVIL_NETWORK_ARGS := --rpc-url $(SEPOLIA_RPC_URL) --private-key $(PRIVATE_KEY) --broadcast --verify --etherscan-api-key $(ETHERSCAN_API_KEY) -vvvv
endif

deploy:
	@forge script script/DeploySessionHandler.s.sol $(NETWORK_ARGS)

fund:
	@forge script script/FundSessionHandler.s.sol $(NETWORK_ARGS)

# ── Python ────────────────────────────────────────────────────────────────────

db:
	.venv/bin/python3 interface/db.py

deploy-py:
	.venv/bin/python3 interface/deploy.py


bot:
	.venv/bin/python3 interface/telebot.py

agent:
	.venv/bin/python3 interface/smart_wallet_agent.py

register:
	.venv/bin/python3 interface/register_agent.py
