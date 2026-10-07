"""
What a transaction moved into or out of the user's wallet: the History tab's filters (incoming or
outgoing, an amount, a token, the other side) search these, never the row's sentence.

Each kind of row is read from where its truth is:

  - 'outside': the explorer's movements (tx_history.outside_movements), as the sentence was.
  - 'owner': the call's own calldata. Only two owner calls move money -- a plain deposit, and
    withdraw(token, amount, to) -- and their arguments are exact.
  - 'assistant': the mined receipt. Token Transfer logs give every token that moved, exactly, with
    the other side. The native coin leaves no log, so what left is read from the op's own
    executions (their values), and what came back from unwrapping the wrapped native token, by the
    wallet or by the router on its behalf.

Fees are never movements: the gas prefund and the protocol fee are in neither the logs nor the
executions. Everything here is pure -- no database, no node -- so it is tested on its own.
"""
from dataclasses import dataclass
from typing import Callable

from eth_abi import decode
from langchain_uniswap_v2.abis import router_abi
from web3 import Web3

IN, OUT = "in", "out"

# (TICKER, decimals or None) for a token the user knows, or None for one they don't.
TokenInfo = Callable[[str], tuple[str, int | None] | None]

_ZERO = "0x" + "00" * 20
_TRANSFER = Web3.keccak(text="Transfer(address,address,uint256)")
_DEPOSIT = Web3.keccak(text="Deposit(address,uint256)")        # WETH9: wrap
_WITHDRAWAL = Web3.keccak(text="Withdrawal(address,uint256)")  # WETH9: unwrap
_HANDLE_OPS = Web3.keccak(
    text="handleOps((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[],address)"
)[:4]
_EXECUTE = Web3.keccak(text="execute(bytes32,bytes)")[:4]
_CALLTYPE_SINGLE, _CALLTYPE_BATCH = 0x00, 0x01

# Decoding calldata needs no node.
_router = Web3().eth.contract(abi=router_abi)


@dataclass(frozen=True)
class Moved:
    """One amount that entered or left the wallet."""

    direction: str            # IN or OUT
    token: str | None         # checksummed ERC-20 address; None for the native coin
    ticker: str
    amount: int               # base units
    decimals: int | None      # None when they couldn't be read: `amount` is then all there is
    counterparty: str | None  # checksummed; None for a mint or burn


def direction_of(movements: list[Moved]) -> str:
    """The History tab's group: 'out' if anything left the wallet, 'in' if it only received, else 'none'."""
    if any(m.direction == OUT for m in movements):
        return OUT
    if any(m.direction == IN for m in movements):
        return IN
    return "none"


def owner_movements(wallet_contract, tx, token_info: TokenInfo, native: str) -> list[Moved]:
    """
    What an owner transaction to the wallet moves, from its calldata: a plain deposit, or a
    withdrawal. Anything else, or a transaction to some other contract (the one that created the
    wallet), moves nothing.

    @param wallet_contract  The user's SessionHandler, with its ABI, to decode the call.
    @param tx               The transaction, as the node returns it.
    """
    wallet = wallet_contract.address
    if tx["to"] is None or Web3.to_checksum_address(tx["to"]) != wallet:
        return []
    data = _bytes(tx.get("input"))
    if not data:
        sender = Web3.to_checksum_address(tx["from"]) if tx.get("from") else None
        return [Moved(IN, None, native, tx["value"], 18, sender)] if tx["value"] else []
    try:
        fn, args = wallet_contract.decode_function_input(data)
    except ValueError:
        return []
    if fn.fn_name != "withdraw" or args["amount"] == 0:
        return []
    token = Web3.to_checksum_address(args["token"])
    if int(token, 16) == 0:
        token, ticker, decimals = None, native, 18
    else:
        ticker, decimals = token_info(token) or (_short(token), None)
    return [Moved(OUT, token, ticker, args["amount"], decimals, Web3.to_checksum_address(args["to"]))]


def assistant_movements(
    wallet: str,
    tx,
    receipt,
    token_info: TokenInfo,
    *,
    native: str,
    wrapped: str | None,
    router: str | None,
    native_contract: str | None = None,
) -> list[Moved]:
    """
    What an assistant UserOperation moved, from the transaction that executed it and its receipt.

    @param wallet           The user's wallet, checksummed: the op's sender.
    @param tx               The bundle transaction (EntryPoint.handleOps), as the node returns it.
    @param token_info       Tokens the user knows; a Transfer of any other token is left out.
    @param wrapped          The chain's wrapped native token (WETH, WBNB), or None.
    @param router           The exchange router, or None: it unwraps a swap's or a withdrawn
                            pool's native coin and sends it on.
    @param native_contract  Where the native coin is also an ERC-20 (CELO), its address. Its
                            Transfer logs are then the native coin's movements, and the
                            executions' values aren't counted a second time.
    """
    wallet = Web3.to_checksum_address(wallet)
    executions = executions_of(wallet, _bytes(tx.get("input")))
    logs = list(receipt["logs"])
    moved: list[Moved] = []

    # Every token that moved, with the other side.
    for log in logs:
        parties = _transfer(log)
        if parties is None:
            continue
        src, dst, value = parties
        token = Web3.to_checksum_address(log["address"])
        if token == native_contract:
            token, ticker, decimals = None, native, 18
        else:
            info = token_info(token)
            if info is None:
                continue
            ticker, decimals = info
        if dst == wallet and src != wallet:
            moved.append(Moved(IN, token, ticker, value, decimals, _party(src)))
        elif src == wallet and dst != wallet:
            moved.append(Moved(OUT, token, ticker, value, decimals, _party(dst)))

    if native_contract is not None:
        return moved

    # The native coin the op sent, call by call.
    for target, value, _ in executions:
        if value:
            moved.append(Moved(OUT, None, native, value, 18, target))

    if wrapped is None:
        return moved
    wrapped = Web3.to_checksum_address(wrapped)
    own = [log for log in logs if Web3.to_checksum_address(log["address"]) == wrapped]
    wrapped_ticker = (token_info(wrapped) or (_short(wrapped), 18))[0]
    wrap_events = [
        (_topic(log, 0), _address(log["topics"][1]), _uint(log["data"]))
        for log in own
        if len(log["topics"]) == 2 and _topic(log, 0) in (_DEPOSIT, _WITHDRAWAL)
    ]
    transfers = [t for t in map(_transfer, own) if t is not None]

    # WETH9 and its copies emit Deposit and Withdrawal and no Transfer when wrapping; tokens that
    # mint and burn instead (Arbitrum's) were already counted above, from their Transfer logs.
    if not any(_ZERO in (src, dst) for src, dst, _ in transfers):
        for kind, who, wad in wrap_events:
            if who == wallet and wad:
                moved.append(Moved(IN if kind == _DEPOSIT else OUT, wrapped, wrapped_ticker, wad, 18, None))

    # The native coin that came back: unwrapped by the wallet itself, or by the router for a swap
    # or a pool withdrawal that pays the wallet. Read from Withdrawal events, or from burns where
    # the token emits those instead.
    unwraps = [(who, wad) for kind, who, wad in wrap_events if kind == _WITHDRAWAL]
    if not unwraps:
        unwraps = [(src, value) for src, dst, value in transfers if dst == _ZERO]
    router = Web3.to_checksum_address(router) if router else None
    router_pays_wallet = router is not None and any(
        target == router and _router_recipient(data) == wallet for target, _, data in executions
    )
    for who, wad in unwraps:
        if not wad:
            continue
        if who == wallet:
            moved.append(Moved(IN, None, native, wad, 18, wrapped))
        elif who == router and router_pays_wallet:
            moved.append(Moved(IN, None, native, wad, 18, router))
    return moved


def executions_of(wallet: str, data: bytes) -> list[tuple[str, int, bytes]]:
    """
    The calls the wallet's op made, as [(target, value, calldata), ...], read from the bundle
    transaction's handleOps calldata. Empty when it isn't one, or holds no op from `wallet`.
    """
    if data[:4] != _HANDLE_OPS:
        return []
    try:
        ops, _ = decode(
            ["(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[]", "address"], data[4:]
        )
    except Exception:  # noqa: BLE001 -- malformed calldata means no executions, not a failure
        return []
    call = next((op[3] for op in ops if op[0].lower() == wallet.lower()), None)
    if call is None or call[:4] != _EXECUTE:
        return []
    try:
        mode, execution = decode(["bytes32", "bytes"], call[4:])
        if mode[0] == _CALLTYPE_SINGLE:
            # abi.encodePacked(target, value, data): see contracts.pack_execution_calldata.
            target = Web3.to_checksum_address("0x" + execution[:20].hex())
            return [(target, int.from_bytes(execution[20:52], "big"), bytes(execution[52:]))]
        if mode[0] == _CALLTYPE_BATCH:
            (batch,) = decode(["(address,uint256,bytes)[]"], execution)
            return [(Web3.to_checksum_address(t), v, bytes(d)) for t, v, d in batch]
    except Exception:  # noqa: BLE001 -- as above
        return []
    return []


def _router_recipient(data: bytes) -> str | None:
    """The `to` of a router call -- who gets the swap's output or the pool's tokens -- or None."""
    try:
        _, args = _router.decode_function_input(data)
    except ValueError:
        return None
    to = args.get("to")
    return Web3.to_checksum_address(to) if to else None


def _transfer(log) -> tuple[str, str, int] | None:
    """(from, to, value) of an ERC-20 Transfer log, or None. ERC-721's has a fourth topic."""
    if len(log["topics"]) != 3 or _topic(log, 0) != _TRANSFER:
        return None
    return _address(log["topics"][1]), _address(log["topics"][2]), _uint(log["data"])


def _topic(log, i: int) -> bytes:
    return bytes(log["topics"][i])


def _address(topic) -> str:
    return Web3.to_checksum_address("0x" + bytes(topic)[-20:].hex())


def _uint(data) -> int:
    data = _bytes(data)
    return int.from_bytes(data[:32], "big") if data else 0


def _bytes(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, str):
        return bytes.fromhex(value.removeprefix("0x"))
    return bytes(value)


def _party(address: str) -> str | None:
    return None if address == Web3.to_checksum_address(_ZERO) else address


def _short(address: str) -> str:
    """`0x1234…abcd`, as tx_history and the web app shorten addresses."""
    return f"{address[:6]}…{address[-4:]}"
