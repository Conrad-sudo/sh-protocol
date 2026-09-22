import os
import sys
from web3.logs import DISCARD
from constants import get_router
from network_config import load_network_config_by_name, load_network_config
from db import (
    save_wallet_address,
    save_user_network,
    save_contact,
    save_session_key,
    get_wallet_address,
    get_wallet_chains,
    get_user_id_by_telegram_chat_id,
)
from userop import get_or_create_session_key
from tx_sender import send_and_confirm
from contracts import (
    invalidate_cache,
    load_factory,
    load_session_handler,
    load_ierc20,
)

nonce: int

# Wallet-wide USD spending cap (18 decimals) configured at deployment: the max NET value that
# may leave the wallet per spending window, across every token and venue combined. Mirrors the
# $50k per-session limits the old per-target design used.
DEFAULT_DAILY_LIMIT_USD = 50_000 * 10**18
# Spending-window length in seconds (24h — the cap refills each window).
DEFAULT_WINDOW_SECS = 86_400

# ETH transferred into a freshly deployed wallet as deployWallet's msg.value so it can pay its own ERC-4337
# prefund (maxFeePerGas * total gas limit, drawn from balance since the wallet holds no EntryPoint
# deposit) and forward ETH into WETH wraps / Uniswap swaps. Live testnets get a modest amount
# (Sepolia/BSC ETH is faucet-scarce) sized to cover a UserOp prefund even when the base fee is
# elevated — at ~40 gwei base, a ~400k-gas op prefunds ~0.03 ETH, so 0.15 leaves room for the op
# plus the transfer. Local anvil + fork chains run on a richly-funded deployer, so they keep a
# larger cushion for multi-swap e2e runs. Strings (not floats) so to_wei converts them exactly.
WALLET_PREFUND_ETH_LIVE = "1"
WALLET_PREFUND_ETH_LOCAL = "1"

# Tokens the wallet's spending-limit module meters (net-value tracking), per network. Each MUST
# already be priced by the deployed SHOracle, or deployWallet reverts with TokenNotPriced.
# Native ETH/BNB is ALWAYS metered (it is not on this watched list); only unwatched ERC20s sit
# outside the meter.
DEFAULT_WATCHED_TICKERS = {
    "anvil": ["weth", "usdc", "dai"],
    "mainnet-fork": ["weth", "usdc", "dai"],
    "sepolia": ["weth", "usdc", "link"],
    "sepolia-fork": ["weth", "usdc", "link"],
    "bsc": ["wbnb", "usdc", "usdt"],
    "bsc-fork": ["wbnb", "usdc", "usdt"],
    # Arbitrum's native gas asset is ETH, so this mirrors the mainnet list. All three are priced by
    # SHOracle there (ARB_ETH_USD/ARB_USDC_USD/ARB_DAI_USD in Constants.s.sol); "arb" is priced too
    # and is a reasonable fourth if you want the chain's own token metered.
    "arbitrum": ["weth", "usdc", "dai"],
    "arbitrum-fork": ["weth", "usdc", "dai"],
    # Celo intentionally omitted: HelperConfig.s.sol has no Celo NetworkConfig, so a Celo deploy
    # falls back to mainnet config anyway. Add a Celo watched list here only once Celo is wired up.
}

# Maps a chain_name to the env var holding its deployer private key. Every fork of a real chain
# (mainnet-fork, sepolia-fork, bsc-fork, celo-fork) shares SEPOLIA_PRIVATE_KEY rather than the
# Anvil default burner key: forking inherits that chain's real on-chain state, and the well-known
# Anvil/Hardhat accounts have been EIP-7702-delegated to drainer contracts on real
# Sepolia/BSC/mainnet (see HelperConfig.s.sol's ANVIL_BURNER_WALLET comment for the same issue on
# the Foundry side, which now resolves every non-Anvil network to this same SEPOLIA_ACCOUNT).
# Delegated means the address has CODE, which breaks it as a handleOps
# beneficiary — the EntryPoint's _compensate() plain ETH send reverts AA91. Fork balances come
# from `make fund`'s anvil_setBalance, so this key never needs real funds off Sepolia.
#
# Plain "anvil" (no fork) has no real-world state to inherit, so the burner key is fine there —
# it's the only chain name allowed to fall through to the ANVIL_PRIVATE_KEY default below.
LIVE_PRIVATE_KEY_ENV = {
    "sepolia": "SEPOLIA_PRIVATE_KEY",
    "bsc": "BSC_PRIVATE_KEY",
    "celo": "CELO_PRIVATE_KEY",
    "arbitrum": "ARBITRUM_PRIVATE_KEY",
    "mainnet-fork": "SEPOLIA_PRIVATE_KEY",
    "sepolia-fork": "SEPOLIA_PRIVATE_KEY",
    "bsc-fork": "SEPOLIA_PRIVATE_KEY",
    "celo-fork": "SEPOLIA_PRIVATE_KEY",
    "arbitrum-fork": "SEPOLIA_PRIVATE_KEY",
}


def _private_key_env(chain_name: str) -> str:
    """Returns the env var name holding the deployer key for chain_name."""
    return LIVE_PRIVATE_KEY_ENV.get(chain_name, "ANVIL_PRIVATE_KEY")


def _default_watched_tokens(user_id: int, chain_name: str) -> list[str]:
    """
    Resolves the default watched-token ticker list for chain_name into checksummed addresses.
    Requires the DB token table to be seeded (`make db`) so tickers resolve on this network.

    @param user_id     The application user ID (used to resolve the network's token addresses).
    @param chain_name  The network being deployed to.
    @return            List of token addresses to pass to deployWallet as watchedTokens.
    """
    tickers = DEFAULT_WATCHED_TICKERS.get(chain_name, [])
    return [load_ierc20(user_id=user_id, token=ticker).address for ticker in tickers]


def deploy_wallet(user_id: int, chain_name: str):
    """
    Deploys a new SessionHandler wallet for user_id by calling SHFactory.deployWallet(
    dailyLimitUsd, windowDuration, watchedTokens, sessionKey, trustedSpenders) on
    the given chain, then persists the resulting address to wallet.db. The first three
    arguments seed the wallet's spending-cap config: SpendingLimitModule is installed as a
    hook with a DEFAULT_DAILY_LIMIT_USD cap per DEFAULT_WINDOW_SECS window, metering the
    DEFAULT_WATCHED_TICKERS for this network.

    The last two make the wallet usable after this ONE transaction. The session key and the
    trusted router used to require a follow-up owner-signed call each (add_default_session and
    trust_router below); they are now applied inside initialize(). The wallet is deployed with
    CREATE2 at a salt the FACTORY derives from (deployer, its per-owner deployCount), so its
    address is known BEFORE the transaction is sent -- which is what lets the session key, whose
    Vault entry is keyed by wallet address, be minted up front.

    This assumes the shared protocol infrastructure (EntryPoint, SHOracle,
    SHTreasury/SHRegistry, SHFactory, SpendingLimitModule) has already been deployed on
    chain_name via `forge script script/DeploySHProtocol.s.sol` and synced into the DB via
    `make db` — including SHFactory.setSpendingLimitModule(...), without which
    deployWallet() reverts. This function only deploys the per-user SessionHandler
    wallet, seeding it with ETH via deployWallet's own payable msg.value.

    The deployer must already hold gas. On a fork it starts from the forked chain's real balance —
    zero, on mainnet-fork and bsc-fork — so `make fund` (anvil_setBalance) has to run first; the
    Makefile makes it a prerequisite of both `deploy` and `deploy-wallet`, so this holds for every
    target. The same address also bundles here (see anvil.resolve_bundler), so one top-up covers
    deployment and every UserOp that follows.

    @param user_id     The application user ID who will own the new wallet.
    @param chain_name  The network to deploy on (e.g. "anvil", "mainnet-fork", "sepolia-fork", "sepolia").
    @return             The checksummed address of the newly deployed SessionHandler.
    """
    save_user_network(
        user_id, chain_name
    )  # set network in DB before deployment so load_factory can resolve the right address
    w3, chain_id = load_network_config_by_name(chain_name)

    private_key_env = _private_key_env(chain_name)
    deployer = w3.eth.account.from_key(os.getenv(private_key_env))

    factory = load_factory(user_id)
    watched_tokens = _default_watched_tokens(user_id, chain_name)

    # CREATE2 means the address exists as a prediction before the tx is sent, so the session key --
    # whose Vault ciphertext is stored under (user_id, wallet_address), the same key tools.py's
    # get_session_keys resolves -- can be minted now and passed into the deploy itself.
    #
    # predictWalletAddress answers for the deployer's NEXT deploy: the factory salts with its own
    # per-owner deployCount, so this advances after each of OUR deploys and is untouched by anyone
    # else's. That is what keeps `make deploy-wallet` re-runnable (each run gets a fresh wallet) with
    # no salt bookkeeping here. A caller wanting one-wallet-per-user reads factory.deployCount(owner)
    # and refuses when it is non-zero, rather than relying on a revert.
    # A user is EXPECTED to hold one wallet per chain, so having a wallet elsewhere is normal and
    # says nothing here. Only a wallet on THIS chain is a problem: session_handlers is keyed
    # (user_id, chain_id), so redeploying replaces that row and the previous wallet keeps its
    # prefund, tokens and LP positions with nothing pointing at it. Warn on that case alone.
    try:
        existing = get_wallet_address(user_id, chain_id)
        print(
            f"WARNING: user {user_id} already has wallet {existing} on {chain_name} "
            f"(chain {chain_id}). Deploying a second one on this chain — the old wallet's funds "
            f"stay there and become unreachable from the app. Withdraw first if that is not what "
            f"you want. (Wallets on other chains are unaffected.)"
        )
    except ValueError:
        others = [c for c in get_wallet_chains(user_id) if c != chain_id]
        if others:
            print(f"user {user_id} has wallets on chains {others}; adding {chain_name} ({chain_id}).")

    predicted = factory.functions.predictWalletAddress(deployer.address).call()
    session_key, session_key_ct = get_or_create_session_key(user_id, chain_id, predicted)

    # The router is a wallet-level choice, not protocol config, so it is granted by the caller here
    # rather than auto-trusted in initialize(). Sourced from constants.get_router so the router the
    # wallet trusts is always the one toolkits.py builds calldata for. Bare Anvil has no Uniswap
    # deployment, so it gets an empty list.
    try:
        trusted_spenders = [get_router(chain_id)]
    except ValueError:
        print(f"No router configured for chain {chain_id}; deploying with no trusted spenders")
        trusted_spenders = []

    tx = factory.functions.deployWallet(
        DEFAULT_DAILY_LIMIT_USD,
        DEFAULT_WINDOW_SECS,
        watched_tokens,
        session_key,
        trusted_spenders,
    ).build_transaction(
        {   "value": w3.to_wei(WALLET_PREFUND_ETH_LIVE if "fork" not in chain_name and chain_name != "anvil"
                           else WALLET_PREFUND_ETH_LOCAL, "ether"),
            "from": deployer.address,
            # Placeholder: send_and_confirm assigns the real nonce under its lock. One deployer
            # EOA serves every user, and telebot.py runs their requests on separate threads.
            "nonce": 0,
            "chainId": chain_id,
        }
    )
    receipt = send_and_confirm(w3, chain_name, deployer, tx)

    logs = factory.events.WalletDeployed().process_receipt(receipt,errors=DISCARD)
    if not logs:
        raise RuntimeError(
            "WalletDeployed event not found in receipt — deployWallet() may have reverted silently"
        )
    wallet_address = logs[0]["args"]["walletAddress"]

    # The prediction can go stale: deployCount is keyed by msg.sender, and ONE deployer EOA serves
    # every user here (see _private_key_env), so any other deployWallet from that key landing in
    # between moves the address. It needs no concurrency — send_and_confirm can raise TimeoutError
    # on a transaction that is still live in the mempool and later mines, and the retry then
    # predicts an address the first one has taken.
    #
    # Recover rather than raise. By this point the wallet EXISTS on chain, holds the whole prefund,
    # and already has `session_key` authorized — that key was an ARGUMENT to deployWallet, so it is
    # correct for whatever address deployed. Only the DB row is filed under the wrong address, so
    # re-file it. Raising here instead would strand a funded wallet with no session_handlers row,
    # reachable only by hand from the deployer key.
    if wallet_address.lower() != predicted.lower():
        print(
            f"WARNING: CREATE2 prediction was stale (predicted {predicted}, deployed "
            f"{wallet_address}) — another deploy from this key landed first. Re-filing the session "
            f"key under the deployed address; the on-chain grant is already correct."
        )
        save_session_key(user_id, chain_id, wallet_address, session_key, session_key_ct)

    save_wallet_address(user_id, chain_id, wallet_address)
    invalidate_cache(user_id)

    print(f"SessionHandler wallet deployed: {wallet_address}")
    print(f"  session key authorized at deploy: {session_key}")
    print(f"  trusted spenders seeded: {trusted_spenders or '(none)'}")
    print("Deployment complete — Database updated.")

    return wallet_address






def add_default_session(user_id: int):
    """
    Registers the user's single session key on the SessionHandler wallet.

    NOT part of the deploy path any more: deploy_wallet() passes the key into
    SHFactory.deployWallet, which authorizes it inside initialize(). Kept as a recovery helper
    for re-granting a key on a wallet deployed without one (sessionKey == address(0)) or after
    removeSession. Costs one owner-signed transaction; deploying seeds the key for free.

    The wallet authorizes ONE bare session key for the whole account (an allowedSession
    allowlist entry) rather than per-target scoped keys: the key may sign UserOps for any
    external call, bounded on-chain by two guardrails that replace the old per-target
    scoping entirely:
      1. the wallet-wide USD spending cap (net-value metering per window) configured at
         deployWallet time, and
      2. the account's execution guard, which blocks session-key calls to the wallet itself
         or its SpendingLimitModule (so a key can never raise its own cap), and its
         no-standing-approval rule (approvals must be consumed in the same transaction).

    Called automatically after deploy_wallet(). The key is generated (or fetched) via
    get_or_create_session_key keyed to the wallet address, encrypted in Vault, and
    registered on-chain with SessionHandler.addSession() as the owner.

    @param user_id  The application user ID.
    """
    w3, chain_id, chain_name = load_network_config(user_id)
    private_key_env = _private_key_env(chain_name)
    owner = w3.eth.account.from_key(os.getenv(private_key_env))
    session_handler = load_session_handler(user_id=user_id)

    # One key per wallet per chain: keyed to (user_id, chain_id, wallet address), which is exactly
    # how tools.get_session_keys resolves it, so every tool signs with this key on this chain.
    session_key, _ = get_or_create_session_key(user_id, chain_id, session_handler.address)

    tx = session_handler.functions.addSession(session_key).build_transaction(
        {
            "from": owner.address,
            "nonce": w3.eth.get_transaction_count(owner.address),
            "chainId": chain_id,
        }
    )
    signed_tx = w3.eth.account.sign_transaction(tx, owner.key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise RuntimeError(f"addSession reverted (tx: {tx_hash.hex()})")

    logs = session_handler.events.SessionAdded().process_receipt(receipt, errors=DISCARD)
    print(f"Session added! tx: {tx_hash.hex()}, status: {receipt['status']}")
    if logs:
        print("Session Key:", logs[0]["args"]["sessionKey"])
    else:
        print("Warning: SessionAdded event could not be decoded (stale ABI — run forge build)")


def trust_router(user_id: int):
    """
    Trusts the chain's canonical V2 router on the user's wallet, as the owner.

    NOT part of the deploy path any more: deploy_wallet() passes the router in
    SHFactory.deployWallet's trustedSpenders, which grants it inside initialize(). Kept as a
    recovery helper for wallets deployed with an empty list, or to re-grant after
    removeTrustedSpender. Idempotent — the module ignores a spender it already trusts.

    A wallet deploys with an EMPTY trusted-spender list unless one is seeded. SpendingLimitModule
    refuses any
    approval on a token the oracle cannot price unless its spender is trusted, so without
    this the LP-token approval inside remove_liquidity would revert (LP tokens have no
    Chainlink feed). Priced-token approvals never needed the exemption and are unaffected.

    The router used to be protocol configuration (SHRegistry.router) and was auto-trusted
    inside initialize(). It is now a wallet-level choice, granted here from the same
    constants.ROUTER entry toolkits.py binds the Uniswap toolkit to, so the trusted router
    and the router the bot builds calldata for cannot drift apart.

    Trusting a spender is a real grant: it may pull an unpriced token within a single
    transaction. The no-standing-approval rule still forces every such approval to exactly
    zero before the transaction ends.

    @param user_id  The application user ID.
    """
    w3, chain_id, chain_name = load_network_config(user_id)
    try:
        router = get_router(chain_id)
    except ValueError:
        # Bare Anvil has no Uniswap deployment; nothing to trust and nothing to do.
        print(f"No router configured for chain {chain_id}; skipping trust_router")
        return

    private_key_env = _private_key_env(chain_name)
    owner = w3.eth.account.from_key(os.getenv(private_key_env))
    session_handler = load_session_handler(user_id=user_id)

    tx = session_handler.functions.addTrustedSpender(router).build_transaction(
        {
            "from": owner.address,
            "nonce": w3.eth.get_transaction_count(owner.address),
            "chainId": chain_id,
        }
    )
    signed_tx = w3.eth.account.sign_transaction(tx, owner.key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise RuntimeError(f"addTrustedSpender reverted (tx: {tx_hash.hex()})")
    print(f"Router trusted: {router} (tx: {tx_hash.hex()})")


def deploy(user_id: int, network: str):
    """
    Top-level deployment dispatcher. Deploys a SessionHandler wallet for user_id via
    SHFactory.deployWallet() on the given network.

    Supported networks: "anvil", "mainnet-fork", "sepolia-fork", "bsc-fork", "celo-fork",
    "arbitrum-fork", "sepolia", "bsc", "celo". Each one must already have the shared protocol
    infrastructure deployed (see
    deploy_wallet()). "sepolia" and "bsc" are live networks — SEPOLIA_PRIVATE_KEY /
    BSC_PRIVATE_KEY must be set and funded with real ETH/BNB before deploying (see
    LIVE_PRIVATE_KEY_ENV).

    To target a different network, pass it as the first CLI argument (e.g.
    `python3 app/deploy_wallet.py bsc-fork`, or `make deploy-wallet ARGS=bsc-fork`).
    Defaults to "anvil" when no argument is given, matching `make deploy`'s own
    no-ARGS default (plain local Anvil) — see the Makefile's `NETWORK_ARGS`.

    @param user_id  The application user ID — keys all database records for this user.
    @param network  Target network name (see supported values above).
    @raises ValueError  If network is not one of the supported values.
    """
    if network in (
        "anvil", "mainnet-fork", "sepolia-fork", "bsc-fork", "celo-fork", "arbitrum-fork",
        "sepolia", "bsc", "celo",
    ):
        deploy_wallet(user_id, network)
    else:
        raise ValueError(f"Unsupported network '{network}'")


def resolve_harness_user() -> int:
    """
    Resolves which account this test harness should deploy for.

    Identity is no longer the Telegram chat id, so TELEGRAM_CHAT_ID is not a user id any more --
    using it directly would write rows keyed to an account that does not exist. Prefer an explicit
    APP_USER_ID; otherwise translate TELEGRAM_CHAT_ID through the users table, which is what keeps
    an existing local setup working untouched.

    @return  The application user ID.
    @raises SystemExit with an explanation if neither resolves.
    """
    explicit = os.getenv("APP_USER_ID")
    if explicit:
        return int(explicit)

    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if chat_id:
        user_id = get_user_id_by_telegram_chat_id(int(chat_id))
        if user_id is not None:
            return user_id
        raise SystemExit(
            f"TELEGRAM_CHAT_ID={chat_id} is not linked to any account. Link it from the web app "
            f"(POST /api/integrations/telegram/link), or set APP_USER_ID to the account to use."
        )

    raise SystemExit("Set APP_USER_ID (or a linked TELEGRAM_CHAT_ID) to say which account to deploy for.")


if __name__ == "__main__":
    user_id = resolve_harness_user()
    save_contact(user_id=user_id,name="tim",address="0x9f4d8D3f66C47c75b95325f01861d1643825Bffc")
    network = sys.argv[1] if len(sys.argv) > 1 else "anvil"
    # One transaction. add_default_session() and trust_router() are no longer part of the happy
    # path — deployWallet seeds both — and are kept above only as recovery/re-grant helpers.
    deploy(user_id=user_id, network=network)
