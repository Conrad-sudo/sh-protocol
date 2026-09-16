"""
Per-user toolkit construction for langchain-erc20, langchain-uniswap-v2 and langchain-erc8004.

These three packages own all ERC20, Uniswap V2 and ERC-8004 registry calldata construction:
balances, quotes, slippage bounds, approval sequencing, agent reads and registry writes. They
return an ordered *execution plan* -- a list of account-agnostic (to, value, data) calls -- and
never sign, submit, or hold a key. Turning a plan into a UserOperation is app/tools.py's job
(see _submit_plan there).

Two things force a wrapper layer rather than exposing the package tools to the agent directly:

  1. A toolkit instance is bound to ONE rpc/router/token set. The bot is multi-tenant by
     user_id, so toolkits are built lazily and cached per user_id, exactly like the Contract
     instances in contracts.py.
  2. langchain-uniswap-v2 takes raw addresses only. The agent speaks tickers and contact
     names, so app/tools.py resolves those before invoking a package tool.

All three toolkits are built in tx_mode="calls": they emit plan["calls"] and skip every
nonce/gas/fee RPC call. That is the correct mode for a smart account -- an ERC-4337 nonce is
EntryPoint.getNonce(sender, key), not an EOA transaction count, and eth_estimateGas with
`from` set to the account simulates it calling itself rather than the EntryPoint invoking it.
"""

from langchain_core.tools import BaseTool
from langchain_erc20 import ERC20Toolkit
from langchain_erc8004 import ERC8004Toolkit
from langchain_uniswap_v2 import UniswapV2Toolkit

from constants import ETH_SENTINEL, get_native_wrapped_ticker, get_router
from contracts import load_session_handler
from db import get_supported_tokens, get_token_address
from network_config import load_network_config

# Keyed (user_id, chain_id), matching contracts.py. Every toolkit below is bound to one chain's
# RPC, router and token addresses, so a user with wallets on several chains must not be served the
# toolkit built for the chain they just left -- it would build calldata for the wrong router and
# the wrong token addresses against the new chain's RPC.
_erc20_tools_cache: dict[tuple[int, int], dict[str, BaseTool]] = {}
_uniswap_tools_cache: dict[tuple[int, int], dict[str, BaseTool]] = {}
_erc8004_tools_cache: dict[tuple[int, int], dict[str, BaseTool]] = {}

# Package tools that build valid ERC20 calldata but ALWAYS revert on this wallet, because
# SpendingLimitModule rejects any transaction that leaves an allowance standing (and rejects
# type(uint256).max outright). Approvals are only ever legal inside a batch that consumes
# them in the same transaction -- which every write tool below already emits. Withheld so the
# agent cannot reach for one and burn a UserOp on a guaranteed revert.
_BLOCKED_TOOLS = frozenset({"approve", "approve_token", "revoke_approval"})


def _token_map(user_id: int) -> dict[str, str]:
    """Ticker -> checksummed address for every token listed on the user's current chain.

    Snapshotted into the toolkit at construction, so adding a token to the DB mid-session
    requires invalidate_toolkits(user_id) before it resolves.
    """
    _, chain_id, _ = load_network_config(user_id)
    return {
        ticker: get_token_address(chain_id, ticker)
        for ticker in get_supported_tokens(user_id)
    }


def get_erc20_tools(user_id: int) -> dict[str, BaseTool]:
    """Returns {tool_name: tool} for langchain-erc20, bound to this user's chain.

    @param user_id  The application user ID.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _erc20_tools_cache:
        tokens = _token_map(user_id)
        toolkit = ERC20Toolkit(
            rpc_url=w3.provider.endpoint_uri,
            tx_mode="calls",
            tokens=tokens,
            # deposit()/withdraw() target for wrap_native/unwrap_native. On Celo this
            # resolves to the CELO ERC20, which has no deposit() -- wrapping has never been
            # meaningful there (see NATIVE_WRAPPED_TICKER in constants.py).
            native_wrapped_address=tokens[get_native_wrapped_ticker(chain_id)],
            # Matches the address(0) sentinel the wallet and SHOracle already use for native.
            native_sentinel=ETH_SENTINEL,
        )
        _erc20_tools_cache[key] = {
            t.name: t for t in toolkit.get_tools() if t.name not in _BLOCKED_TOOLS
        }
    return _erc20_tools_cache[key]


def get_uniswap_tools(user_id: int) -> dict[str, BaseTool]:
    """Returns {tool_name: tool} for langchain-uniswap-v2, bound to this user's router.

    @param user_id  The application user ID.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _uniswap_tools_cache:
        tokens = _token_map(user_id)
        toolkit = UniswapV2Toolkit(
            rpc_url=w3.provider.endpoint_uri,
            # Read the router from app constants, never from the package's chain registry
            # (which has no Anvil entry and points BSC elsewhere). This is the same address
            # deploy_wallet.py passes to addTrustedSpender, so the router the toolkit builds
            # calldata for is always the one the wallet actually trusts.
            router_address=get_router(chain_id),
            # Omitted on purpose -- the toolkit reads router.factory(), so the pair lookups
            # can never drift from the router the wallet actually uses, and Anvil works
            # without a hardcoded address.
            factory_address=None,
            native_wrapped_address=tokens[get_native_wrapped_ticker(chain_id)],
            tx_mode="calls",
            # Already the default in calls mode; explicit because SpendingLimitModule depends
            # on it. Appends approve(router, 0) to any plan where the router may pull less
            # than it was approved for (exact-output swaps, addLiquidity), so no residual
            # allowance survives the transaction.
            reset_residual_approvals=True,
        )
        _uniswap_tools_cache[key] = {
            t.name: t for t in toolkit.get_tools() if t.name not in _BLOCKED_TOOLS
        }
    return _uniswap_tools_cache[key]


def get_erc8004_tools(user_id: int) -> dict[str, BaseTool]:
    """Returns {tool_name: tool} for langchain-erc8004, bound to this user's registries.

    @param user_id  The application user ID.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _erc8004_tools_cache:
        wallet = load_session_handler(user_id)
        toolkit = ERC8004Toolkit(
            rpc_url=w3.provider.endpoint_uri,
            # Both addresses come off the WALLET, never from the package's chain table. The
            # canonical 0x8004... pair is right on Sepolia/BSC, but on Anvil the protocol
            # deploys MockIdentityRegistry/MockReputationRegistry -- and chain 31337 is not in
            # KNOWN_NETWORKS at all. Reading them from the account the UserOp executes from is
            # the only way the toolkit's calldata and the wallet's view cannot disagree.
            identity_registry=wallet.functions.IDENTITY_REGISTRY().call(),
            reputation_registry=wallet.functions.REPUTATION_REGISTRY().call(),
            # Omitted deliberately: ERC-8004's Validation Registry has no canonical deployment
            # on any chain, and this protocol deploys none. Leaving it unset makes the toolkit
            # withhold its seven tools rather than offer ones that can only fail.
            validation_registry=None,
            tx_mode="calls",
            # The sender every plan is built FOR: registry writes go out as UserOps executed by
            # the account, so msg.sender at the registry is the wallet, not the session key.
            # It is also what the package preflights against -- the self-feedback guard and the
            # owner/operator checks on the identity writes all test this address.
            from_address=wallet.address,
            # In calls mode this is a single eth_call preflight rather than a gas estimate, so
            # a write that would revert (self-feedback, not the owner, nonexistent agent) is
            # caught here instead of burning a UserOp.
            estimate_gas=True,
            # No default reviewer allowlist: filtered reputation reads must name their
            # reviewers explicitly. Sybil inflation is the expected attack on this registry
            # (anyone can register an agent and review it from addresses they control), and
            # there is no set of reviewers this project has an independent reason to trust.
            client_allowlist=None,
        )
        _erc8004_tools_cache[key] = {t.name: t for t in toolkit.get_tools()}
    return _erc8004_tools_cache[key]


def invalidate_toolkits(user_id: int) -> None:
    """Drop every cached toolkit for user_id, on EVERY chain, after a redeploy, network switch or
    token add.

    Called by contracts.invalidate_cache, which is the single invalidation entry point, and
    chain-agnostic for the same reason it is: a network switch has to drop the chain being left
    and the caller does not always know which that was.
    """
    for cache in (_erc20_tools_cache, _uniswap_tools_cache, _erc8004_tools_cache):
        for cached_key in [k for k in cache if k[0] == user_id]:
            del cache[cached_key]
