from web3.contract import Contract
from network_config import load_network_config
from db import (
    get_json,
    get_wallet_address,
    get_token_address,
    get_factory_address,
)
from abi import ientry_point
# The ERC-20 ABI comes from langchain-erc20 rather than a local copy: the package already
# exports the exact same nine-function ABI it builds all its own calldata against, so a local
# duplicate could only ever drift away from what the toolkits actually encode.
from langchain_erc20 import ERC20_ABI

# All keyed by (user_id, chain_id), never user_id alone: every address these hold is chain-specific,
# and a user runs a wallet on several chains at once. Keyed by user_id only, switching a user's
# network would hand back the previous chain's wallet, EntryPoint and module against the new
# chain's RPC -- reads would return garbage and writes would target a contract that isn't there.
_session_handler_cache: dict[tuple[int, int], Contract] = {}
_entry_point_cache: dict[tuple[int, int], Contract] = {}
_erc20_cache: dict[tuple[int, int, str], Contract] = {}
_sh_factory_cache: dict[tuple[int, int], Contract] = {}
_spending_limit_module_cache: dict[tuple[int, int], Contract] = {}
_registry_cache: dict[tuple[int, int], Contract] = {}

# ERC-7579 single-call, default-execution-type mode (CALLTYPE_SINGLE = 0x00 in the top byte).
ERC7579_SINGLE_CALL_MODE = b"\x00" * 32

# ERC-7579 batch, default-execution-type mode (CALLTYPE_BATCH = 0x01 in the top byte).
# Batches are how approvals stay legal under SpendingLimitModule's no-standing-approval rule:
# [approve, spend(, approve 0)] must all land in ONE transaction so nothing is left standing.
ERC7579_BATCH_CALL_MODE = b"\x01" + b"\x00" * 31


def invalidate_cache(user_id: int) -> None:
    """
    Drop all cached contract instances for user_id, on EVERY chain, after a redeploy or a network
    switch.

    Deliberately not chain-scoped: clearing one chain would be enough for a redeploy, but a network
    switch has to drop the chain the user is leaving, and the caller does not always know which that
    was. Rebuilding is a couple of cheap DB reads, so clear the lot.
    """
    # Imported here, not at module scope: toolkits.py imports load_session_handler from this
    # module, so a top-level import would be circular.
    from toolkits import invalidate_toolkits

    invalidate_toolkits(user_id)
    for cache in (
        _session_handler_cache,
        _entry_point_cache,
        _sh_factory_cache,
        _spending_limit_module_cache,
        _registry_cache,
        _erc20_cache,
    ):
        for key in [k for k in cache if k[0] == user_id]:
            del cache[key]


def load_session_handler(user_id: int) -> Contract:
    """
    Loads the SessionHandler contract ABI and binds it to the address stored in the DB
    for the given user.

    @param user_id  The application user ID.
    @return         A web3.py Contract instance pointing to the deployed SessionHandler.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _session_handler_cache:
        abi = get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
        address = get_wallet_address(user_id, chain_id)
        _session_handler_cache[key] = w3.eth.contract(address=address, abi=abi)
    return _session_handler_cache[key]


def read_spending_config(wallet: Contract) -> dict:
    """
    Reads a wallet's getConfig() struct as a dict keyed by its Solidity field names.

    Always use this rather than indexing the returned tuple. The struct's field order follows its
    storage packing, so it changes when the layout does -- it did on 2026-09-22, swapping
    spentInWindow and dailyLimitUsd -- and a positional read like cfg[3] would then silently
    return the wrong field. The names come from the ABI the contract was built with, so they
    always match the compiled layout.

    @param wallet  A SessionHandler Contract (its getConfig() takes no arguments).
    @return        {"installed", "windowStart", "windowDuration", "spentInWindow",
                    "dailyLimitUsd", "watchedTokens", "trustedSpenders"}.
    """
    fn = wallet.functions.getConfig
    names = [c["name"] for c in fn.abi["outputs"][0]["components"]]
    return dict(zip(names, fn().call()))


def load_spending_limit_module(user_id: int) -> Contract:
    """
    Loads the SpendingLimitModule contract ABI, bound to the address the wallet reports
    via its public SH_MODULE getter.

    The module is the ERC-7579 HOOK that enforces the wallet's global USD spending cap
    (net-value metering + no-standing-approvals). It is NOT a validator and holds no session-key
    state — session keys are an allowlist on the SessionHandler account itself
    (allowedSession/addSession), and SessionAdded/SessionRemoved are emitted there. This loader
    is used mainly to read the module's cap config/events.

    @param user_id  The application user ID.
    @return         A web3.py Contract instance pointing to the installed SpendingLimitModule.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _spending_limit_module_cache:
        abi = get_json("./out/SpendingLimitModule.sol/SpendingLimitModule.json")["abi"]
        address = load_session_handler(user_id).functions.SH_MODULE().call()
        _spending_limit_module_cache[key] = w3.eth.contract(address=address, abi=abi)
    return _spending_limit_module_cache[key]


def load_registry(user_id: int) -> Contract:
    """
    Loads SHRegistry, bound to the address the wallet reports via its public REGISTRY getter.

    The registry is where the protocol fee and the treasury live. The wallet reads them per call
    rather than storing them, so this is the only way to learn what one execution will cost before
    running it -- see SessionHandler._extractFee.

    @param user_id  The application user ID.
    @return         A web3.py Contract instance pointing to the SHRegistry the wallet uses.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _registry_cache:
        abi = get_json("./out/SHRegistry.sol/SHRegistry.json")["abi"]
        address = load_session_handler(user_id).functions.REGISTRY().call()
        _registry_cache[key] = w3.eth.contract(address=address, abi=abi)
    return _registry_cache[key]


def load_entry_point(user_id: int) -> Contract:
    """
    Loads the EntryPoint contract ABI and binds it to the address stored in the DB.

    @return  A web3.py Contract instance pointing to the deployed EntryPoint.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _entry_point_cache:
        abi = ientry_point
        # entryPoint() (lowercase, a function) — SessionHandler inherits this from OZ's
        # Account.sol.
        address = load_session_handler(user_id).functions.ENTRY_POINT().call()
        _entry_point_cache[key] = w3.eth.contract(address=address, abi=abi)
    return _entry_point_cache[key]


def pack_execution_calldata(target: str, value: int, data: bytes) -> bytes:
    """
    Packs (target, value, data) into the ERC-7579 single-execution calldata expected by
    SessionHandler.execute(bytes32 mode, bytes executionCalldata).

    Mirrors Solidity's abi.encodePacked(target, value, data): a raw 20-byte address,
    followed by a raw 32-byte big-endian value, followed by the inner calldata — no ABI
    offset/length words, unlike a normal abi.encode.

    @param target  Checksummed hex address string (e.g. "0xAbC...").
    @param value   Native ETH value in wei to forward with the inner call.
    @param data    ABI-encoded calldata for the inner call.
    @return        The packed executionCalldata bytes.
    """
    return bytes.fromhex(target[2:]) + value.to_bytes(32, "big") + data


def session_key_nonce_key(module_address: str) -> int:
    """
    Builds the ERC-4337 nonce key used for session-key UserOps.

    SessionHandler (via OZ's AccountERC7579) reads the top 20 bytes of the nonce's 192-bit key
    as a validator-module address. SpendingLimitModule is now a HOOK only — it is never
    installed as a validator — so whatever address the key encodes, the account falls through to
    its own _rawSignatureValidation (owner OR allowedSession signer). Keeping the module-derived
    key preserves nonce continuity for wallets that already submitted ops under it; any key
    value (including 0) validates identically.

    @param module_address  Checksummed hex address of the installed SpendingLimitModule.
    @return                The nonce key to pass as EntryPoint.getNonce's second argument.
    """
    return int(module_address, 16) << 32


def encode_batch_execution_calldata(executions: list[tuple[str, int, bytes]]) -> bytes:
    """
    ABI-encodes an ERC-7579 batch: Execution[] where Execution = (address target, uint256 value,
    bytes callData). This is a normal abi.encode of the struct array (matching OZ's
    ERC7579Utils.encodeBatch/decodeBatch), unlike the single-call packed form.

    @param executions  List of (target_address, value_wei, calldata_bytes) triples.
    @return            The ABI-encoded executionCalldata for ERC7579_BATCH_CALL_MODE.
    """
    from eth_abi import encode

    return encode(["(address,uint256,bytes)[]"], [executions])


def load_ierc20(user_id: int, token: str) -> Contract:
    """
    Loads an IERC20 Contract instance for the given ticker symbol.

    Only used now for address lookups and decimals() by the oracle-pricing tools; all ERC20
    reads and calldata construction moved to langchain-erc20 (see app/toolkits.py). The
    wrapped-native token needs no special ABI here either -- deposit()/withdraw() are the
    package's wrap_native/unwrap_native.

    @param token  The token ticker symbol to look up (e.g. "usdc", "dai").
    @return       A web3.py Contract instance for the matching token.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id, token)
    if key not in _erc20_cache:
        address = get_token_address(chain_id, token)
        _erc20_cache[key] = w3.eth.contract(address=address, abi=ERC20_ABI)
    return _erc20_cache[key]


def load_factory(user_id: int) -> Contract:
    """
    Loads the SHFactory contract ABI and binds it to the address stored in the DB
    for the chain the user is connected to.

    @param user_id  The application user ID.
    @return         A web3.py Contract instance pointing to the deployed SHFactory.
    """
    w3, chain_id, _ = load_network_config(user_id)
    key = (user_id, chain_id)
    if key not in _sh_factory_cache:
        abi = get_json("./out/SHFactory.sol/SHFactory.json")["abi"]
        address = get_factory_address(chain_id)
        _sh_factory_cache[key] = w3.eth.contract(address=address, abi=abi)
    return _sh_factory_cache[key]


def load_calldata(instance: Contract, fn_name: str, args: list) -> bytes:
    """
    ABI-encodes a call to a function and returns the raw calldata bytes.

    @param instance       A bound web3.py  Contract instance.
    @param fn_name         The function name to encode (e.g. "transfer").
    @param args            The positional arguments for the function.
    @return                The ABI-encoded calldata as bytes (without 0x prefix).
    """
    return bytes.fromhex(
        instance.encode_abi(abi_element_identifier=fn_name, args=args)[2:]
    )


# load_reputation_registry lived here until langchain-erc8004 took over the ERC-8004 registries.
# The package owns both the ABIs and the address resolution now; app/toolkits.py builds the
# toolkit from the addresses the WALLET reports, so there is nothing left for a local Contract
# instance to do.

