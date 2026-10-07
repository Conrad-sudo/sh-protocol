"""
Offline checks for tx_movements: what a transaction moved into or out of the wallet, which the
History tab's filters search. Owner calls are read from their calldata; the assistant's ops from a
scripted bundle transaction and receipt -- token Transfer logs, the op's own executions, and the
wrapped native token's events.

Pure functions, scripted data: no database, no node.

Run: make movements-test   (or: python app/tests/test_tx_movements.py)
"""
from checks import check, finish   # first: it puts app/ on sys.path for the imports below

from eth_abi import encode           # noqa: E402
from langchain_uniswap_v2.abis import router_abi  # noqa: E402
from web3 import Web3                # noqa: E402

import db                            # noqa: E402
import tx_movements                  # noqa: E402
from tx_movements import IN, OUT, Moved  # noqa: E402


def _addr(n: int) -> str:
    return Web3.to_checksum_address(f"0x{n:040x}")


WALLET = _addr(0xA11CE)
SAM = _addr(0x5A3)
STRANGER = _addr(0xBAD)
USDC = _addr(0x05DC)
WETH = _addr(0x0E7E)
LP = _addr(0x1B1B)
SPAM = _addr(0x5BA3)
CELO = _addr(0xCE10)
ROUTER = _addr(0x7007E)
PAIR = _addr(0x9A12)
ZERO = _addr(0)

KNOWN = {USDC: ("USDC", 6), WETH: ("WETH", 18), LP: ("ETH/USDC LP", 18), CELO: ("CELO", 18)}


def token_info(address: str):
    return KNOWN.get(Web3.to_checksum_address(address))


HANDLER_ABI = db.get_json("./out/SessionHandler.sol/SessionHandler.json")["abi"]
WALLET_CONTRACT = Web3().eth.contract(address=WALLET, abi=HANDLER_ABI)
ROUTER_CONTRACT = Web3().eth.contract(abi=router_abi)
TRANSFER = Web3.keccak(text="Transfer(address,address,uint256)")
DEPOSIT = Web3.keccak(text="Deposit(address,uint256)")
WITHDRAWAL = Web3.keccak(text="Withdrawal(address,uint256)")
SINGLE, BATCH = b"\x00" * 32, b"\x01" + b"\x00" * 31


# ── Scripted transactions ─────────────────────────────────────────────────────


def _topic(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def transfer(token: str, src: str, dst: str, value: int) -> dict:
    return {"address": token, "topics": [TRANSFER, _topic(src), _topic(dst)], "data": value.to_bytes(32, "big")}


def deposit(who: str, wad: int, token: str = WETH) -> dict:
    return {"address": token, "topics": [DEPOSIT, _topic(who)], "data": wad.to_bytes(32, "big")}


def withdrawal(who: str, wad: int, token: str = WETH) -> dict:
    return {"address": token, "topics": [WITHDRAWAL, _topic(who)], "data": wad.to_bytes(32, "big")}


def bundle(executions: list[tuple[str, int, bytes]], sender: str = WALLET) -> dict:
    """The handleOps transaction that runs one op from `sender` carrying `executions`."""
    if len(executions) == 1:
        target, value, data = executions[0]
        mode, execution = SINGLE, bytes.fromhex(target[2:]) + value.to_bytes(32, "big") + data
    else:
        mode, execution = BATCH, encode(["(address,uint256,bytes)[]"], [executions])
    call_data = Web3.keccak(text="execute(bytes32,bytes)")[:4] + encode(["bytes32", "bytes"], [mode, execution])
    op = (sender, 1, b"", call_data, bytes(32), 0, bytes(32), b"", b"")
    selector = Web3.keccak(
        text="handleOps((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[],address)"
    )[:4]
    return {
        "to": _addr(0xE7),
        "value": 0,
        "input": selector + encode(
            ["(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[]", "address"], [[op], STRANGER]
        ),
    }


def moved(executions, logs, *, native_contract=None) -> list[Moved]:
    return tx_movements.assistant_movements(
        WALLET, bundle(executions), {"logs": logs}, token_info,
        native="ETH", wrapped=WETH, router=ROUTER, native_contract=native_contract,
    )


def _erc20(fn: str, args: list) -> bytes:
    selectors = {"transfer": "transfer(address,uint256)", "approve": "approve(address,uint256)"}
    return Web3.keccak(text=selectors[fn])[:4] + encode(["address", "uint256"], args)


def router_call(fn: str, args: list) -> bytes:
    return bytes.fromhex(ROUTER_CONTRACT.encode_abi(fn, args=args)[2:])


# ── The assistant's ops ───────────────────────────────────────────────────────


def test_token_and_native_sends():
    print("\n[1] sends: a token from its Transfer log, the native coin from the op's execution")
    got = moved([(USDC, 0, _erc20("transfer", [SAM, 5_000_000]))], [transfer(USDC, WALLET, SAM, 5_000_000)])
    check("a USDC transfer: out, exact, to Sam", got == [Moved(OUT, USDC, "USDC", 5_000_000, 6, SAM)], str(got))

    got = moved([(SAM, 10**17, b"")], [])
    check("a native send: out, from the execution's value, to Sam",
          got == [Moved(OUT, None, "ETH", 10**17, 18, SAM)], str(got))

    got = moved([(USDC, 0, b"")], [transfer(USDC, SAM, WALLET, 7), transfer(SPAM, STRANGER, WALLET, 10**24)])
    check("tokens arriving count; a token nobody knows doesn't",
          got == [Moved(IN, USDC, "USDC", 7, 6, SAM)], str(got))

    nft = {"address": USDC, "topics": [TRANSFER, _topic(WALLET), _topic(SAM), (1).to_bytes(32, "big")], "data": b""}
    check("an ERC-721 Transfer (a fourth topic) is not a token amount", moved([(USDC, 0, b"")], [nft]) == [])

    got = moved([(USDC, 0, b"")], [transfer(USDC, WALLET, WALLET, 9)])
    check("a transfer to itself moved nothing", got == [], str(got))


def test_swaps():
    print("\n[2] swaps: what left, and what came back -- unwrapped by the router only when it pays the wallet")
    deadline = 2**32
    sell = [
        (USDC, 0, _erc20("approve", [ROUTER, 100_000_000])),
        (ROUTER, 0, router_call("swapExactTokensForETH", [100_000_000, 1, [USDC, WETH], WALLET, deadline])),
    ]
    logs = [
        transfer(USDC, WALLET, PAIR, 100_000_000),
        transfer(WETH, PAIR, ROUTER, 3 * 10**16),
        withdrawal(ROUTER, 3 * 10**16),
    ]
    got = moved(sell, logs)
    check("USDC -> ETH: out USDC to the pool, in ETH from the router",
          got == [Moved(OUT, USDC, "USDC", 100_000_000, 6, PAIR), Moved(IN, None, "ETH", 3 * 10**16, 18, ROUTER)], str(got))

    to_sam = [sell[0], (ROUTER, 0, router_call("swapExactTokensForETH", [100_000_000, 1, [USDC, WETH], SAM, deadline]))]
    got = moved(to_sam, logs)
    check("the same swap paying Sam: only the USDC that left", got == [Moved(OUT, USDC, "USDC", 100_000_000, 6, PAIR)], str(got))

    buy = [(ROUTER, 10**17, router_call("swapExactETHForTokens", [1, [WETH, USDC], WALLET, deadline]))]
    logs = [deposit(ROUTER, 10**17), transfer(WETH, ROUTER, PAIR, 10**17), transfer(USDC, PAIR, WALLET, 300_000_000)]
    got = moved(buy, logs)
    check("ETH -> USDC: out ETH to the router, in USDC from the pool, no WETH of the wallet's",
          sorted(got, key=str) == sorted([Moved(OUT, None, "ETH", 10**17, 18, ROUTER),
                                          Moved(IN, USDC, "USDC", 300_000_000, 6, PAIR)], key=str), str(got))


def test_wrapping_and_liquidity():
    print("\n[3] wrapping and liquidity: WETH9 events, mint-and-burn wrappers, LP tokens")
    got = moved([(WETH, 10**18, bytes.fromhex("d0e30db0"))], [deposit(WALLET, 10**18)])
    check("wrap (WETH9): out ETH to WETH, in WETH",
          got == [Moved(OUT, None, "ETH", 10**18, 18, WETH), Moved(IN, WETH, "WETH", 10**18, 18, None)], str(got))

    unwrap = [(WETH, 0, bytes.fromhex("2e1a7d4d") + (10**18).to_bytes(32, "big"))]
    got = moved(unwrap, [withdrawal(WALLET, 10**18)])
    check("unwrap (WETH9): out WETH, in ETH from WETH",
          got == [Moved(OUT, WETH, "WETH", 10**18, 18, None), Moved(IN, None, "ETH", 10**18, 18, WETH)], str(got))

    got = moved(unwrap, [transfer(WETH, WALLET, ZERO, 10**18)])
    check("unwrap (a wrapper that burns, like Arbitrum's): counted once each way",
          got == [Moved(OUT, WETH, "WETH", 10**18, 18, None), Moved(IN, None, "ETH", 10**18, 18, WETH)], str(got))

    add = [
        (USDC, 0, _erc20("approve", [ROUTER, 100_000_000])),
        (ROUTER, 4 * 10**16, router_call("addLiquidityETH", [USDC, 100_000_000, 1, 1, WALLET, 2**32])),
    ]
    logs = [transfer(USDC, WALLET, PAIR, 100_000_000), deposit(ROUTER, 4 * 10**16), transfer(LP, ZERO, WALLET, 5 * 10**17)]
    got = moved(add, logs)
    check("add liquidity: out USDC and ETH, in LP tokens (minted: no other side)",
          sorted(got, key=str) == sorted([Moved(OUT, USDC, "USDC", 100_000_000, 6, PAIR),
                                          Moved(IN, LP, "ETH/USDC LP", 5 * 10**17, 18, None),
                                          Moved(OUT, None, "ETH", 4 * 10**16, 18, ROUTER)], key=str), str(got))

    remove = [(ROUTER, 0, router_call("removeLiquidityETH", [USDC, 5 * 10**17, 1, 1, WALLET, 2**32]))]
    logs = [transfer(LP, WALLET, PAIR, 5 * 10**17), transfer(USDC, PAIR, WALLET, 99_000_000), withdrawal(ROUTER, 39 * 10**15)]
    got = moved(remove, logs)
    check("remove liquidity: out LP, in USDC, in ETH from the router",
          got == [Moved(OUT, LP, "ETH/USDC LP", 5 * 10**17, 18, PAIR), Moved(IN, USDC, "USDC", 99_000_000, 6, PAIR),
                  Moved(IN, None, "ETH", 39 * 10**15, 18, ROUTER)], str(got))


def test_celo_and_odd_calldata():
    print("\n[4] the native coin as an ERC-20 (CELO) is counted once; calldata that isn't an op gives nothing")
    got = moved([(SAM, 10**18, b"")], [transfer(CELO, WALLET, SAM, 10**18)], native_contract=CELO)
    check("CELO: one movement, from the log, as the native coin",
          got == [Moved(OUT, None, "ETH", 10**18, 18, SAM)], str(got))

    check("not a handleOps call: no executions", tx_movements.executions_of(WALLET, b"\x12\x34\x56\x78") == [])
    check("an op from another wallet: no executions",
          tx_movements.executions_of(WALLET, bundle([(SAM, 1, b"")], sender=STRANGER)["input"]) == [])
    check("a batch is read call by call",
          tx_movements.executions_of(WALLET, bundle([(SAM, 1, b"\x01"), (USDC, 0, b"\x02")])["input"])
          == [(SAM, 1, b"\x01"), (USDC, 0, b"\x02")])


# ── The owner's calls ─────────────────────────────────────────────────────────


def owner_tx(fn: str | None, args: list | None = None, value: int = 0, to: str = WALLET) -> dict:
    data = bytes.fromhex(WALLET_CONTRACT.encode_abi(fn, args=args or [])[2:]) if fn else b""
    return {"to": to, "from": SAM, "value": value, "input": data}


def test_owner_calls():
    print("\n[5] owner calls: a deposit and a withdrawal move money, from their own arguments; nothing else does")

    def owner(tx):
        return tx_movements.owner_movements(WALLET_CONTRACT, tx, token_info, "ETH")

    check("a plain deposit: in, from whoever sent it",
          owner(owner_tx(None, value=2 * 10**17)) == [Moved(IN, None, "ETH", 2 * 10**17, 18, SAM)])
    check("withdraw ETH: out, to the address it names",
          owner(owner_tx("withdraw", [ZERO, 10**18, STRANGER])) == [Moved(OUT, None, "ETH", 10**18, 18, STRANGER)])
    check("withdraw USDC: out, with the token",
          owner(owner_tx("withdraw", [USDC, 5_000_000, SAM])) == [Moved(OUT, USDC, "USDC", 5_000_000, 6, SAM)])
    check("withdraw a token nobody listed: still out, named by its address",
          owner(owner_tx("withdraw", [SPAM, 9, SAM])) == [Moved(OUT, SPAM, f"{SPAM[:6]}…{SPAM[-4:]}", 9, None, SAM)])
    check("pausing moves nothing", owner(owner_tx("pause")) == [])
    check("a transaction to another contract (the one that made the wallet) moves nothing",
          owner(owner_tx(None, value=1, to=STRANGER)) == [])


def test_direction_of():
    print("\n[6] the group: out if anything left, in if it only received, none if nothing moved")
    out, came = Moved(OUT, None, "ETH", 1, 18, SAM), Moved(IN, USDC, "USDC", 1, 6, SAM)
    check("a swap is out", tx_movements.direction_of([out, came]) == "out")
    check("a deposit is in", tx_movements.direction_of([came]) == "in")
    check("a setting is none", tx_movements.direction_of([]) == "none")


if __name__ == "__main__":
    test_token_and_native_sends()
    test_swaps()
    test_wrapping_and_liquidity()
    test_celo_and_odd_calldata()
    test_owner_calls()
    test_direction_of()
    finish("All movement checks passed.")
