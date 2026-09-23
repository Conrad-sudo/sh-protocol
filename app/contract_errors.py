"""
Names contract reverts, so a user or the agent sees `PriceOracle_SequencerDown()` rather than
`0x...` 4 bytes of hex.

Shared by the two places a revert surfaces: api.py, whose owner endpoints simulate a transaction
before asking the user to sign it, and the agent, whose tools read the chain and send UserOps.
"""
from eth_abi import decode
from eth_utils import keccak

from db import get_json

# Every contract whose errors can reach the app. An owner call fails in SessionHandler itself
# (SessionHandler_NotEnoughBalance) or in the module it forwards to (SpendingLimitModule_TokenNotPriced).
# SHOracle is here because every valuation runs through it and its reverts bubble up unchanged
# through the module's hook -- a stale feed or an L2 sequencer outage arrives as one of its errors,
# whether the owner or a session key triggered the valuation.
_ARTIFACTS = (
    "./out/SessionHandler.sol/SessionHandler.json",
    "./out/SpendingLimitModule.sol/SpendingLimitModule.json",
    "./out/SHOracle.sol/SHOracle.json",
)

# Error(string): what a plain `require(cond, "message")` reverts with -- Uniswap's router, for one.
_ERROR_STRING_SELECTOR = bytes.fromhex("08c379a0")

# The EntryPoint's own rejections of a UserOp, which wrap the reason in an "AAxx" code and, for a
# revert inside the account, the account's revert data -- e.g. FailedOpWithRevert(0, "AA23 reverted",
# EnforcedPause()) for a paused wallet. They surface when bundler.py estimates handleOps.
_FAILED_OP_SELECTOR = keccak(text="FailedOp(uint256,string)")[:4]
_FAILED_OP_WITH_REVERT_SELECTOR = keccak(text="FailedOpWithRevert(uint256,string,bytes)")[:4]


def _error_selectors() -> dict[str, str]:
    """
    Builds a 4-byte-selector -> error-signature map from the ABIs in _ARTIFACTS.

    The ABIs carry every error's name and argument types, and the selector is just
    keccak(signature)[:4], so the mapping can be rebuilt locally with no chain access.
    """
    selectors: dict[str, str] = {}
    for path in _ARTIFACTS:
        try:
            abi = get_json(path)["abi"]
        except (FileNotFoundError, KeyError):
            continue  # not built — fall back to the raw selector rather than failing
        for entry in abi:
            if entry.get("type") != "error":
                continue
            signature = f"{entry['name']}({','.join(i['type'] for i in entry['inputs'])})"
            selectors["0x" + keccak(text=signature)[:4].hex()] = signature
    return selectors


# Built once at import: the ABIs do not change while the process runs.
ERROR_SELECTORS = _error_selectors()


def name_revert(text: str) -> str | None:
    """
    The signature of the first known contract error whose selector appears in `text`, or None.

    `text` is anything that may carry a selector: a web3 exception's message (ContractCustomError
    puts the raw revert data there) or hex revert data.
    """
    for selector, signature in ERROR_SELECTORS.items():
        if selector in text:
            return signature
    return None


def describe_revert_data(data: bytes) -> str:
    """
    Turns raw revert data into something readable: a known custom error's signature, the message
    of an Error(string), or failing both the hex itself.

    @param data  The revert bytes, e.g. EntryPoint's UserOperationRevertReason.revertReason.
    """
    if data[:4] == _ERROR_STRING_SELECTOR:
        try:
            return f'Error("{decode(["string"], data[4:])[0]}")'
        except Exception:  # noqa: BLE001 -- malformed payload; the hex below still says something
            pass
    if data[:4] == _FAILED_OP_SELECTOR:
        try:
            return decode(["uint256", "string"], data[4:])[1]
        except Exception:  # noqa: BLE001
            pass
    if data[:4] == _FAILED_OP_WITH_REVERT_SELECTOR:
        try:
            _, reason, inner = decode(["uint256", "string", "bytes"], data[4:])
            return f"{reason}: {describe_revert_data(inner)}"
        except Exception:  # noqa: BLE001
            pass
    if not data:
        return "no revert data"
    # Matched on the leading 4 bytes only: an error's arguments could contain another selector.
    return ERROR_SELECTORS.get("0x" + data[:4].hex()) or "0x" + data.hex()
