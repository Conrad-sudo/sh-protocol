import os
import time
from web3 import Web3
from constants import (
    ETH_SENTINEL,
    HEARTBEAT_24H,
    
)

from network_config import load_network_config_by_name, load_network_config
from db import (
    get_reputation_registry_selectors,
    save_wallet_address,
    save_session,
    save_user_network,
    get_token_address,
    get_erc20_selectors,
    get_uniswapv2_selectors,
)
from anvil import get_or_create_session_key
from contracts import invalidate_cache, load_factory, load_session_handler, load_ierc20

nonce: int


def _call(contract_fn, w3: Web3, deployer, chain_id: int, nonce: int):
    """
    Sends a non-deployment state-changing transaction and waits for the receipt.

    Builds, signs, and sends a transaction for a bound contract function call.
    Used for post-deployment setup steps such as minting tokens or calling execute().
    The caller is responsible for incrementing the nonce after each call.

    @param contract_fn  A bound ContractFunction ready to call build_transaction() on.
    @param w3           Web3 connection to the target network.
    @param deployer     Signing account (web3.py LocalAccount).
    @param chain_id     EIP-155 chain ID used when building the transaction.
    @param nonce        Sender nonce for this transaction.
    """
    tx = contract_fn.build_transaction(
        {"from": deployer.address, "nonce": nonce, "chainId": chain_id}
    )
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(
            w3.eth.account.sign_transaction(tx, deployer.key).raw_transaction
        )
    )


def sync_anvil_time(w3):
    """
    Sets Anvil's next block timestamp to the current system time,
    preventing Chainlink staleness errors on a mainnet fork.
    """
    import time

    w3.provider.make_request("evm_setNextBlockTimestamp", [int(time.time())])
    w3.provider.make_request("evm_mine", [])


def prefund(deployer, wallet_address: str, w3: Web3, network: str, chain_id: int):
    """
    Funds two accounts from the deployer after SessionHandler wallet deployment:
      1. SessionHandler wallet — 10 ETH to cover ERC-4337 prefund and any forwarded
         ETH used for WETH wraps or Uniswap swaps initiated by session keys.
      2. Bundler (MAINNET_BUNDLER on mainnet-fork, SEPOLIA_BUNDLER on sepolia-fork) —
         10 ETH to cover the gas cost of bundling ERC-4337 UserOperations on the fork.

    Args:
        deployer:       Signing account (web3.py LocalAccount) that pays for both transfers.
        wallet_address: Address of the deployed SessionHandler wallet.
        w3:             Web3 connection to the target network.
        network:        Network name (e.g. "anvil", "mainnet-fork", "sepolia-fork", "sepolia").
        chain_id:       EIP-155 chain ID used when building transactions.
    """
    nonce = w3.eth.get_transaction_count(deployer.address)
    # 1. Send 10 ETH to the SessionHandler wallet (covers ERC-4337 prefund + forwarded ETH for wraps/swaps)
    tx = {
        "from": deployer.address,
        "to": wallet_address,
        "value": w3.to_wei(10, "ether"),
        "nonce": nonce,
        "chainId": chain_id,
        "gas": 50_000,
        "gasPrice": w3.eth.gas_price,
    }
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(
            w3.eth.account.sign_transaction(tx, deployer.key).raw_transaction
        )
    )
    print(f"SessionHandler wallet funded with 10 ETH for {network} deployments.")

    if "fork" in network:
        # 2. Fund the Bundler with 10 ETH to cover the gas costs of bundling UserOperations on this fork.
        bundler_env = "MAINNET_BUNDLER" if network == "mainnet-fork" else "SEPOLIA_BUNDLER"
        bundler = w3.eth.account.from_key(os.getenv(bundler_env))
        nonce += 1
        tx = {
            "from": deployer.address,
            "to": bundler.address,
            "value": w3.to_wei(10, "ether"),
            "nonce": nonce,
            "chainId": chain_id,
            "gas": 50_000,
            "gasPrice": w3.eth.gas_price,
        }
        w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(
                w3.eth.account.sign_transaction(tx, deployer.key).raw_transaction
            )
        )
        print(f"Funded Bundler {bundler.address} with 10 ETH for {network} deployments.")


def deploy_wallet(chat_id: int, chain_name: str):
    """
    Deploys a new SessionHandler wallet for chat_id by calling SHFactory.deployWallet()
    on the given chain, then persists the resulting address to wallet.db.

    This assumes the shared protocol infrastructure (EntryPoint, SHOracle,
    SHTreasury/SHRegistry, SHFactory) has already been deployed on chain_name via
    `forge script script/DeploySHProtocol.s.sol` and synced into the DB via `make db` —
    this function only deploys the per-user SessionHandler wallet, then calls prefund()
    to fund the new wallet (and the bundler, on fork networks) with 10 ETH each.

    @param chat_id     The Telegram chat ID of the user who will own the new wallet.
    @param chain_name  The network to deploy on (e.g. "anvil", "mainnet-fork", "sepolia-fork", "sepolia").
    @return             The checksummed address of the newly deployed SessionHandler.
    """
    save_user_network(
        chat_id, chain_name
    )  # set network in DB before deployment so load_factory can resolve the right address
    w3, chain_id = load_network_config_by_name(chain_name)

    private_key_env = "ANVIL_PRIVATE_KEY" if chain_name != "sepolia" else "SEPOLIA_PRIVATE_KEY"
    deployer = w3.eth.account.from_key(os.getenv(private_key_env))

    factory = load_factory(chat_id)
    tx = factory.functions.deployWallet().build_transaction(
        {
            "from": deployer.address,
            "nonce": w3.eth.get_transaction_count(deployer.address),
            "chainId": chain_id,
        }
    )
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(
            w3.eth.account.sign_transaction(tx, deployer.key).raw_transaction
        )
    )

    logs = factory.events.WalletDeployed().process_receipt(receipt)
    if not logs:
        raise RuntimeError(
            "WalletDeployed event not found in receipt — deployWallet() may have reverted silently"
        )
    wallet_address = logs[0]["args"]["walletAddress"]

    prefund(deployer, wallet_address, w3, chain_name, chain_id)
    save_wallet_address(chat_id, wallet_address)
    invalidate_cache(chat_id)

    print(f"SessionHandler wallet deployed: {wallet_address}")
    print("Deployment complete — Database updated.")

    return wallet_address






def add_session(
    chat_id: int,
    targets: list[str],
    functions: list[list[str]],
    session_ends: list[int],
    limits: list[int],
):
    """
    Registers one session key per target on the SessionHandler contract by calling
    addSessionKey() as the owner. Each target gets its own session with independent
    function whitelists, duration, and spending limit.

    All four list parameters must have the same length — one entry per target.


    @param chat_id      The Telegram chat ID of the user for whom to create session keys.
                        Used to look up the deployed SessionHandler address from the database.
                         Passed to load_network_config() to obtain the Web3 instance, chain ID and Chain Name.
    @param targets      Token ticker symbols identifying the target contracts
                        (e.g. ["usdc", "dai"]). Each is resolved to a contract address
                        via get_anvil_token_address().
    @param functions    Per-target lists of function names to whitelist
                        (e.g. [["transfer", "approve"], ["transfer"]]).
                        Each name is looked up in erc20_selectors and keccak-hashed
                        to produce the 4-byte selector passed to addSessionKey().
    @param session_ends Per-target session durations in days from the current time.
                        (e.g. [1, 7] — 1 day for usdc, 7 days for dai).
    @param limits       Per-target maximum cumulative spending limits in whole USD units
                        (e.g. [1000, 500]). Converted to wei (18-decimal) internally.
    """
    w3, chain_id, chain_name = load_network_config(chat_id)
    if not (len(targets) == len(functions) == len(session_ends) == len(limits)):
        raise ValueError(
            "targets, functions, session_ends, and limits must all have the same length"
        )

    private_key_env = "ANVIL_PRIVATE_KEY" if chain_name != "sepolia" else "SEPOLIA_PRIVATE_KEY"
    owner = w3.eth.account.from_key(os.getenv(private_key_env))
    session_handler = load_session_handler(chat_id=chat_id)
    selector_map = {row["name"]: row["selector"] for row in get_erc20_selectors()}
    selector_map.update(
        {row["name"]: row["selector"] for row in get_uniswapv2_selectors()}
    )
    selector_map.update(
        {row["name"]: row["selector"] for row in get_reputation_registry_selectors()}
    )
    for target, funcs, session_end, limit in zip(
        targets, functions, session_ends, limits
    ):

        if target == "eth":
            target_address = ETH_SENTINEL

        elif target == "uniswapv2_router":
            target_address = session_handler.functions.getUniswapRouter().call()

        elif chain_name == "anvil":
            target_address = load_ierc20(chat_id=chat_id, token=target).address

        elif target == "reputation_registry":
            target_address = session_handler.functions.REPUTATION_REGISTRY().call()
            

        else:
            target_address = get_token_address(chain_id, target)
        session_key, _ = get_or_create_session_key(chat_id, target_address)

        selectors = []
        valid_from = 0
        valid_until = int(time.time()) + HEARTBEAT_24H * session_end
        spending_limit = w3.to_wei(limit, "ether")  # scale USD to 18-decimal precision

        for func in funcs:
            if func not in selector_map:
                raise ValueError(
                    f"Function '{func}' has no selector in erc20_selectors"
                )
            selectors.append(bytes.fromhex(selector_map[func].removeprefix("0x")))

        tx = session_handler.functions.addSessionKey(
            session_key,
            target_address,
            selectors,
            valid_from,
            valid_until,
            spending_limit,
        ).build_transaction(
            {
                "from": owner.address,
                "nonce": w3.eth.get_transaction_count(owner.address),
                "chainId": chain_id,
            }
        )

        signed_tx = w3.eth.account.sign_transaction(tx, owner.key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        save_session(chat_id, target, spending_limit, valid_until)

        logs = session_handler.events.SessionAdded().process_receipt(receipt)
        print(f"Session added! tx: {tx_hash.hex()}, status: {receipt['status']}")
        if logs:
            log = logs[0]
            print("Session Key:", log["args"]["sessionKey"])
            print("Target:     ", log["args"]["target"])
            print("Valid Until:", log["args"]["validUntil"])
        else:
            print(
                "Warning: SessionAdded event could not be decoded (stale ABI — run forge build)"
            )
    if "mainnet" in chain_name:
        for target in targets:
            if target not in ("uniswapv2_router", "eth"):
                approve(
                    chat_id, target
                )  # approve each token for the Uniswap router so it can be swapped within the session limits


def approve(chat_id: int, token: str):
    """
    Approves the Uniswap V2 router to spend type(uint256).max of a given token
    from the SessionHandler by calling execute() as the owner (deployer).

    This must be called before any swap that spends token_in, since the router
    uses transferFrom to pull tokens from the SessionHandler.

    @param chat_id  The Telegram chat ID of the user.
    @param token    The ticker symbol of the ERC20 token to approve (e.g. "usdc").
    """
    w3, chain_id, chain_name = load_network_config(chat_id)
    private_key_env = "ANVIL_PRIVATE_KEY" if chain_name != "sepolia" else "SEPOLIA_PRIVATE_KEY"
    deployer = w3.eth.account.from_key(os.getenv(private_key_env))
    session_handler = load_session_handler(chat_id)
    router_address=session_handler.functions.getUniswapRouter().call()
    erc20 = load_ierc20(chat_id=chat_id, token=token)

    approve_data = erc20.encode_abi(
        abi_element_identifier="approve",
        args=[router_address, 2**256 - 1],
    )

    _call(
        session_handler.functions.execute(erc20.address, 0, approve_data),
        w3,
        deployer,
        chain_id,
        w3.eth.get_transaction_count(deployer.address),
    )
    print(f"Approved Uniswap V2 router to spend {token.upper()} from SessionHandler.")


def add_default_session(chat_id: int):
    """
    Registers a default set of session keys for a new user.

    Called automatically after deploy_wallet() to give the user
    immediate access to the four most common tokens with sensible defaults.
    Each token gets its own session key scoped to the standard ERC20 functions,
    a 1-day validity window, and a per-token spending limit.

    @param chat_id  The Telegram chat ID of the user.
    """
    _,_,chain_name = load_network_config(chat_id)
    erc20_functions = ["transfer", "balanceOf", "approve", "transferFrom", "allowance"]
    weth_functions = erc20_functions + ["deposit", "withdraw"]
    uniswapV2_functions = [
        "swapETHForExactTokens",
        "swapExactTokensForTokens",
        "swapTokensForExactTokens",
        "swapExactTokensForETH",
        "swapExactETHForTokens",
        "swapTokensForExactETH",
        "addLiquidity",
        "addLiquidityETH",
        "removeLiquidity",
        "removeLiquidityETH",
    ]
    reputation_registry_functions = ["giveFeedback"]

    if chain_name == "mainnet-fork":
        add_session(
            chat_id=chat_id,
            targets=["eth", "weth", "usdc", "uniswapv2_router","reputation_registry"],
            functions=[
                [],  # empty selector array for native ETH sessions (address(0) target) since there are no function calls, just value transfers
                weth_functions,
                erc20_functions,
                uniswapV2_functions,
                reputation_registry_functions
            ],
            session_ends=[50, 50, 50, 50, 50],
            limits=[50000, 50000, 50000, 50000, 0],
        )
    if "sepolia" in chain_name: 
        add_session(
            chat_id=chat_id,
            targets=["eth", "weth", "link","reputation_registry"],
            functions=[
                [],  # empty selector array for native ETH sessions (address(0) target) since there are no function calls, just value transfers
                weth_functions,
                erc20_functions,
                reputation_registry_functions
                
            ],
            session_ends=[50, 50, 50, 50],
            limits=[50000, 50000, 50000, 0],
        )


def deploy(chat_id: int, network: str):
    """
    Top-level deployment dispatcher. Deploys a SessionHandler wallet for chat_id via
    SHFactory.deployWallet() on the given network.

    Supported networks: "anvil", "mainnet-fork", "sepolia-fork", "sepolia". Each one
    must already have the shared protocol infrastructure deployed (see deploy_wallet()).

    To target a different network, change the `network` argument in the __main__ block
    at the bottom of this file before running `make deploy`.

    @param chat_id  Telegram chat ID — used to key all database records for this user.
    @param network  Target network name (see supported values above).
    @raises ValueError  If network is not one of the supported values.
    """
    if network in ("anvil", "mainnet-fork", "sepolia-fork", "sepolia"):
        deploy_wallet(chat_id, network)
    else:
        raise ValueError(f"Unsupported network '{network}'")


if __name__ == "__main__":
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
    deploy(chat_id=chat_id, network="sepolia-fork")
    add_default_session(chat_id=chat_id)
