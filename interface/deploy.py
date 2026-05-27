import os
import subprocess
import time
from eth_abi import encode as abi_encode
from web3 import Web3
from constants import (
    ETH_SENTINEL,
    UNISWAP_V2_ROUTER,
    HEARTBEAT_1H,
    HEARTBEAT_23H,
    HEARTBEAT_24H,

)

from network_config import load_network_config_by_name, load_network_config
from db import (
    get_json,

    save_wallet_address,
    save_anvil_token_address,
    save_entry_point_address,
    save_session,
    save_user_network,
    get_pricefeed_address,
    get_supported_tokens,
    get_entry_point_address,
    get_token_address,
    get_erc20_selectors,
    get_uniswapv2_selectors,
)
from anvil import get_or_create_session_key
from contracts import invalidate_cache

from contracts import load_session_handler, load_ierc20
from constants import WEI_PER_ETH

nonce: int 
# Chainlink mainnet heartbeats keyed by the ticker used in mainnet_tokens / mainnet_pricefeeds.
_MAINNET_HEARTBEATS = {
    "eth": HEARTBEAT_1H,
    "aave": HEARTBEAT_1H,
    "ape": HEARTBEAT_24H,
    "arb": HEARTBEAT_24H,
    "bnb": HEARTBEAT_24H,
    "comp": HEARTBEAT_1H,
    "crv": HEARTBEAT_24H,
    "dai": HEARTBEAT_1H,
    "ens": HEARTBEAT_24H,
    "link": HEARTBEAT_1H,
    "mkr": HEARTBEAT_1H,
    "oneinch": HEARTBEAT_24H,
    "sand": HEARTBEAT_24H,
    "sushi": HEARTBEAT_24H,
    "uni": HEARTBEAT_1H,
    "usdc": HEARTBEAT_23H,
    "usdt": HEARTBEAT_24H,
    "wbtc": HEARTBEAT_1H,
    "weth": HEARTBEAT_1H,
    "wtao": HEARTBEAT_24H,
    "yfi": HEARTBEAT_24H,
    "avax": HEARTBEAT_24H,
    "bat": HEARTBEAT_24H,
    "imx": HEARTBEAT_24H,
    "knc": HEARTBEAT_24H,
    "rdnt": HEARTBEAT_24H,
    "rpl": HEARTBEAT_24H,
    "sky": HEARTBEAT_24H,
    "snx": HEARTBEAT_24H,
    "stg": HEARTBEAT_24H,
    "sxt": HEARTBEAT_24H,
    "trump": HEARTBEAT_24H,
    "zec": HEARTBEAT_24H,
}


def _load(path):
    """
    Reads a Foundry compilation artifact and returns its ABI and bytecode.

    @param path  Path to the artifact JSON file (e.g. "./out/EntryPoint.sol/EntryPoint.json").
    @return      A tuple of (abi, bytecode_hex_string) ready to pass to w3.eth.contract().
    """
    artifact = get_json(path)
    return artifact["abi"], artifact["bytecode"]["object"]


def _deploy(deployer, w3: Web3, chain_id: int, nonce: int, abi, bytecode, *args):
    """
    Deploys a contract and returns a bound web3.py Contract instance.

    Signs and sends a deployment transaction, waits for the receipt, and returns
    a contract object bound to the deployed address. The caller is responsible for
    incrementing the nonce after each call.

    @param deployer   Signing account (web3.py LocalAccount).
    @param w3         Web3 connection to the target network.
    @param chain_id   EIP-155 chain ID used when building the transaction.
    @param nonce      Sender nonce for this transaction.
    @param abi        Contract ABI (list of dicts from the compilation artifact).
    @param bytecode   Contract bytecode hex string (from the compilation artifact).
    @param args       Constructor arguments forwarded to the contract constructor.
    @return           Bound web3.py Contract instance at the deployed address.
    """
    tx = (
        w3.eth.contract(abi=abi, bytecode=bytecode)
        .constructor(*args)
        .build_transaction(
            {"from": deployer.address, "nonce": nonce, "chainId": chain_id}
        )
    )
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(
            w3.eth.account.sign_transaction(tx, deployer.key).raw_transaction
        )
    )
    return w3.eth.contract(address=receipt["contractAddress"], abi=abi)


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


def _verify(address: str, contract_src: str, arg_types: list, arg_values: list, chain_id: int):
    """Submit a deployed contract for Etherscan verification via forge verify-contract."""
    api_key = os.getenv("ETHERSCAN_API_KEY")
    if not api_key:
        print(f"  Skipping verification for {address}: ETHERSCAN_API_KEY not set.")
        return

    cmd = [
        "forge", "verify-contract",
        "--chain-id", str(chain_id),
        "--etherscan-api-key", api_key,
        "--optimizer-runs", "200",
        "--via-ir",
        "--watch",
        address,
        contract_src,
    ]

    if arg_types:
        cmd += ["--constructor-args", abi_encode(arg_types, arg_values).hex()]

    print(f"  Verifying {contract_src} at {address}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  Verified: {address}")
    else:
        print(f"  Verification failed for {address}:\n{result.stdout}{result.stderr}")


def sync_anvil_time(w3):
    """
    Sets Anvil's next block timestamp to the current system time,
    preventing Chainlink staleness errors on a mainnet fork.
    """
    import time

    w3.provider.make_request("evm_setNextBlockTimestamp", [int(time.time())])
    w3.provider.make_request("evm_mine", [])


def prefund(deployer, session_handler, w3, network, chain_id):
    """
    Funds two accounts from the deployer after SessionHandler deployment:
      1. SessionHandler — 10 ETH to cover ERC-4337 prefund and any forwarded
         ETH used for WETH wraps or Uniswap swaps initiated by session keys.
      2. Bundler (MAINNET_BUNDLER env var) — 10 ETH to cover the gas cost of
         bundling ERC-4337 UserOperations on the mainnet fork.

    Reads and increments the module-level `nonce` global. The caller is
    responsible for initialising `nonce` before the first call and not reusing
    it after this function returns.

    Args:
        deployer:        Signing account (web3.py LocalAccount) that pays for both transfers.
        session_handler: Deployed SessionHandler contract instance.
        w3:              Web3 connection to the target network.
        chain_id:        EIP-155 chain ID used when building transactions.
    """
    global nonce
     # 3. Send 10 ETH to SessionHandler (covers ERC-4337 prefund + forwarded ETH for wraps/swaps)
    tx = {
        "from": deployer.address,
        "to": session_handler.address,
        "value": w3.to_wei(1, "ether"),
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
    print(f"SessionHandler funded with 1 ETH for {network} deployments.")

    if "fork" in network:
    # Fund the Bundler with 10 ETH to cover the deployment gas costs of users who connect to this SessionHandler on the mainnet fork.
        nonce += 1
        bundler = w3.eth.account.from_key(os.getenv("MAINNET_BUNDLER"))
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

  

def deploy_session_handler_anvil(chat_id: int):
    """
    Deploys the full SessionHandler stack on Anvil from scratch using web3.py,
    mirroring what HelperConfig.getOrCreateAnvilConfig() + DeploySessionHandler.run() does.

    Deployment order:
      1. EntryPoint
      2. ERC20Mock tokens  (USDC 6 decimals, all others 18)
      3. MockV3Aggregator price feeds  (8 decimals each, prices match HelperConfig constants)
      4. PriceOracle  (parallel token / feed address arrays)
      5. SessionHandler  (entryPoint, oracle)
      6. Mint tokens into SessionHandler
      7. Send 10 ETH to SessionHandler
      8. Persist all deployed addresses to wallet.db via save_wallet_address(),
         save_entry_point_address(), and save_token_address()

    Args:
        chat_id: Telegram chat ID of the user — used to associate the deployed
                 SessionHandler address with the correct user in the database.
    """
    w3, chain_id = load_network_config_by_name("anvil")

    deployer = w3.eth.account.from_key(os.getenv("ANVIL_PRIVATE_KEY"))
    nonce = w3.eth.get_transaction_count(deployer.address)

    save_user_network(
        chat_id, "anvil"
    )  # set network to anvil in DB before deployment so helper functions work during deployment
    # Load ABIs + bytecodes
    ep_abi, ep_bc = _load("./out/EntryPoint.sol/EntryPoint.json")
    erc20_abi, erc20_bc = _load("./out/ERC20Mock.sol/ERC20Mock.json")
    agg_abi, agg_bc = _load("./out/MockV3Aggregator.sol/MockV3Aggregator.json")
    oracle_abi, oracle_bc = _load("./out/PriceOracle.sol/PriceOracle.json")
    sh_abi, sh_bc = _load("./out/SessionHandler.sol/SessionHandler.json")

    # 1. EntryPoint
    entry_point = _deploy(deployer, w3, chain_id, nonce, ep_abi, ep_bc)
    nonce += 1

    # 2. ERC20Mock tokens  — (name, symbol, decimals)
    token_specs = [
        ("Circle USD", "USDC", 6),
        ("DAI Stablecoin", "DAI", 18),
        ("Aave Token", "AAVE", 18),
        ("Chainlink Token", "LINK", 18),
        ("1inch Token", "1INCH", 18),
        ("ApeCoin", "APE", 18),
        ("Arbitrum", "ARB", 18),
        ("BNB", "BNB", 18),
        ("Wrapped Bitcoin", "WBTC", 18),
        ("Compound", "COMP", 18),
        ("Curve DAO Token", "CRV", 18),
        ("Ethereum Name Service", "ENS", 18),
        ("Maker", "MKR", 18),
        ("The Sandbox", "SAND", 18),
        ("SushiSwap", "SUSHI", 18),
        ("Wrapped TAO", "wTAO", 18),
        ("Uniswap", "UNI", 18),
        ("yearn.finance", "YFI", 18),
    ]
    tokens = {}
    for name, sym, dec in token_specs:
        tokens[sym.lower()] = _deploy(
            deployer, w3, chain_id, nonce, erc20_abi, erc20_bc, name, sym, dec
        )
        nonce += 1

    # 3. MockV3Aggregator price feeds  — (decimals=8, initialAnswer)
    #    Prices match HelperConfig constants, expressed as integer with 8 decimal places.
    FEED_DECIMALS = 8
    feed_specs = [
        ("eth", 210_000_000_000),  # $2 100.00
        ("usdc", 99_800_000),  # $0.998
        ("dai", 120_000_000),  # $1.20
        ("aave", 11_900_000_000),  # $119.00
        ("link", 921_000_000),  # $9.21
        ("1inch", 10_000_000),  # $0.10
        ("ape", 10_000_000),  # $0.10
        ("arb", 10_000_000),  # $0.10
        ("bnb", 67_403_000_000),  # $674.03
        ("wbtc", 7_149_824_000_000),  # $71 498.24
        ("comp", 1_860_000_000),  # $18.60
        ("crv", 23_000_000),  # $0.23
        ("ens", 611_000_000),  # $6.11
        ("mkr", 189_621_000_000),  # $1 896.21
        ("sand", 8_000_000),  # $0.08
        ("sushi", 22_000_000),  # $0.22
        ("wtao", 27_161_000_000),  # $271.61
        ("uni", 377_000_000),  # $3.77
        ("yfi", 256_107_000_000),  # $2 561.07
    ]
    feeds = {}
    for ticker, price in feed_specs:
        feeds[ticker] = _deploy(
            deployer, w3, chain_id, nonce, agg_abi, agg_bc, FEED_DECIMALS, price
        )
        nonce += 1

    # 4. PriceOracle  — parallel (token_addresses, feed_addresses) arrays.
    #    address(0) registers native ETH (matches SessionHandler ETH_TOKEN_ADDRESS sentinel).
    ZERO = "0x" + "0" * 40
    ordered = [
        "usdc",
        "dai",
        "aave",
        "link",
        "1inch",
        "ape",
        "arb",
        "bnb",
        "wbtc",
        "comp",
        "crv",
        "ens",
        "mkr",
        "sand",
        "sushi",
        "wtao",
        "uni",
        "yfi",
    ]
    token_addrs = [ZERO] + [tokens[t].address for t in ordered]
    feed_addrs = [feeds["eth"].address] + [feeds[t].address for t in ordered]
    heartbeats = [HEARTBEAT_1H] * len(token_addrs)
    oracle = _deploy(
        deployer,
        w3,
        chain_id,
        nonce,
        oracle_abi,
        oracle_bc,
        token_addrs,
        feed_addrs,
        heartbeats,
    )
    nonce += 1

    # 5. SessionHandler

    session_handler = _deploy(
        deployer,
        w3,
        chain_id,
        nonce,
        sh_abi,
        sh_bc,
        entry_point.address,
        oracle.address,
        UNISWAP_V2_ROUTER,
    )
    nonce += 1

    # 6. Mint tokens into SessionHandler
    mint_amounts = {"usdc": 20_000 * 10**6}  # USDC has 6 decimals
    for t in ordered:
        if t != "usdc":
            mint_amounts[t] = 2_000 * WEI_PER_ETH
    for ticker, amount in mint_amounts.items():
        _call(
            tokens[ticker].functions.mint(session_handler.address, amount),
            w3,
            deployer,
            chain_id,
            nonce,
        )
        nonce += 1

    # 7. Send 10 ETH to SessionHandler
    tx = {
        "from": deployer.address,
        "to": session_handler.address,
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

    # 8. Persist addresses to DB

    save_wallet_address(chat_id, session_handler.address)
    save_entry_point_address(chain_id, entry_point.address)
    for sym_lower, contract in tokens.items():
        save_anvil_token_address(sym_lower, contract.address)
    invalidate_cache(chat_id)

    print(f"EntryPoint:     {entry_point.address}")
    print(f"PriceOracle:    {oracle.address}")
    print(f"SessionHandler: {session_handler.address}")
    print("Deployment complete — Database updated!.")

    return session_handler, oracle, entry_point


def deploy_session_handler(chat_id: int, network: str):
    """
    Deploys PriceOracle and SessionHandler on the given network.

    Supported networks: "mainnet-fork", "sepolia-fork", "sepolia".
    Fork networks use ANVIL_PRIVATE_KEY; "sepolia" uses SEPOLIA_PRIVATE_KEY.
    Mainnet-fork enables Uniswap V2 and uses per-token Chainlink heartbeats;
    Sepolia variants set the router to address(0) and use uniform 1-hour heartbeats.

    Deployment order:
      1. PriceOracle  (token + feed addresses from DB)
      2. SessionHandler  (EntryPoint, PriceOracle, router)
      3. Prefund SessionHandler
      4. Persist SessionHandler address and network to wallet.db
    """
    w3, chain_id = load_network_config_by_name(network)

    is_mainnet_fork = network == "mainnet-fork"
    private_key_env = "ANVIL_PRIVATE_KEY" if network.endswith("-fork") else "SEPOLIA_PRIVATE_KEY"
    deployer = w3.eth.account.from_key(os.getenv(private_key_env))
    global nonce
    nonce = w3.eth.get_transaction_count(deployer.address)

    save_user_network(chat_id, network)

    oracle_abi, oracle_bc = _load("./out/PriceOracle.sol/PriceOracle.json")
    sh_abi, sh_bc = _load("./out/SessionHandler.sol/SessionHandler.json")

    ordered = get_supported_tokens(chat_id)
    token_addrs = [ETH_SENTINEL] + [get_token_address(chain_id, t) for t in ordered]
    eth_usd_feed = get_pricefeed_address(chat_id, "weth")
    feed_addrs = [eth_usd_feed] + [get_pricefeed_address(chat_id, t) for t in ordered]
    if is_mainnet_fork:
        heartbeats = [HEARTBEAT_1H] + [_MAINNET_HEARTBEATS[t] for t in ordered]
    else:
        heartbeats = [HEARTBEAT_24H] * len(token_addrs)

    # 1. PriceOracle
    oracle = _deploy(
        deployer,
        w3,
        chain_id,
        nonce,
        oracle_abi,
        oracle_bc,
        token_addrs,
        feed_addrs,
        heartbeats,
    )
    nonce += 1

    if not network.endswith("-fork"):
        _verify(
            oracle.address,
            "src/PriceOracle.sol:PriceOracle",
            ["address[]", "address[]", "uint256[]"],
            [token_addrs, feed_addrs, heartbeats],
            chain_id,
        )

    # 2. SessionHandler
    entry_point = get_entry_point_address(chain_id)
    router = UNISWAP_V2_ROUTER if is_mainnet_fork else ETH_SENTINEL
    session_handler = _deploy(
        deployer,
        w3,
        chain_id,
        nonce,
        sh_abi,
        sh_bc,
        entry_point,
        oracle.address,
        router,
    )
    nonce += 1

    if not network.endswith("-fork"):
        _verify(
            session_handler.address,
            "src/SessionHandler.sol:SessionHandler",
            ["address", "address", "address"],
            [entry_point, oracle.address, router],
            chain_id,
        )

    prefund(deployer, session_handler, w3, network, chain_id)
    save_wallet_address(chat_id, session_handler.address)
    invalidate_cache(chat_id)

    print(f"PriceOracle:    {oracle.address}")
    print(f"SessionHandler: {session_handler.address}")
    print("Deployment complete — Database updated.")

    return session_handler, oracle






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
    for target, funcs, session_end, limit in zip(
        targets, functions, session_ends, limits
    ):

        if target == "eth":
            target_address = ETH_SENTINEL

        elif target == "uniswapv2_router":
            target_address = UNISWAP_V2_ROUTER

        elif chain_name == "anvil":
            target_address = load_ierc20(chat_id=chat_id, token=target).address

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
    erc20 = load_ierc20(chat_id=chat_id, token=token)

    approve_data = erc20.encode_abi(
        abi_element_identifier="approve",
        args=[UNISWAP_V2_ROUTER, 2**256 - 1],
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

    Called automatically after deploy_session_handler() to give the user
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

    if chain_name == "mainnet-fork":
        add_session(
            chat_id=chat_id,
            targets=["eth", "weth", "link", "uniswapv2_router"],
            functions=[
                [],  # empty selector array for native ETH sessions (address(0) target) since there are no function calls, just value transfers
                weth_functions,
                erc20_functions,
                uniswapV2_functions,
            ],
            session_ends=[50, 50, 50, 50],
            limits=[50000, 50000, 50000, 50000],
        )
    if "sepolia" in chain_name: 
        add_session(
            chat_id=chat_id,
            targets=["eth", "weth", "link"],
            functions=[
                [],  # empty selector array for native ETH sessions (address(0) target) since there are no function calls, just value transfers
                weth_functions,
                erc20_functions,
                
            ],
            session_ends=[50, 50, 50],
            limits=[50000, 50000, 50000],
        )


def deploy(chat_id: int, network: str):
    """
    Top-level deployment dispatcher. Routes to the correct deployment function
    based on the target network.

    Supported networks:
      - "anvil"         → deploy_session_handler_anvil(): deploys a full mock stack
                          (EntryPoint, ERC20Mocks, MockV3Aggregators, PriceOracle, SessionHandler).
      - "mainnet-fork"  → deploy_session_handler(): deploys PriceOracle + SessionHandler
                          against live mainnet token and feed addresses on a local fork.
      - "sepolia-fork"  → deploy_session_handler(): same as mainnet-fork but against
                          Sepolia addresses on a local fork.
      - "sepolia"       → deploy_session_handler(): deploys to live Sepolia testnet via
                          SEPOLIA_PRIVATE_KEY and submits contracts for Etherscan verification.

    To target a different network, change the `network` argument in the __main__ block
    at the bottom of this file before running `make deploy`.

    @param chat_id  Telegram chat ID — used to key all database records for this user.
    @param network  Target network name (see supported values above).
    @raises ValueError  If network is not one of the supported values.
    """
    if network == "anvil":
        deploy_session_handler_anvil(chat_id)
    elif network in ("mainnet-fork", "sepolia-fork", "sepolia"):
        deploy_session_handler(chat_id, network)
    else:
        raise ValueError(f"Unsupported network '{network}'")


if __name__ == "__main__":
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
    deploy(chat_id=chat_id, network="sepolia")
    add_default_session(chat_id=chat_id)
