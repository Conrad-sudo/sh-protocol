"""
Offline checks for what a UserOp is charged for posting its data to Ethereum: Base's L1 fee (an OP
Stack fee in wei, priced by the GasPriceOracle predeploy) and Arbitrum's (gas units, from
NodeInterface), each turned into preVerificationGas on the live chain only, never on a fork.

No network: the node is a fake that answers the two oracles with fixed figures. Against real Base
handleOps receipts, the pricing repaid 1.20-1.22x of the L1 fee actually charged, the cushion
L1_GAS_BUFFER is there to give.

Run: make bundler-test   (or: python app/tests/test_bundler.py)
"""
from types import SimpleNamespace

import rlp
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import keccak

from checks import check, finish   # first: it puts app/ on sys.path for the imports below

import bundler                     # noqa: E402

ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
# Stands in for a handleOps transaction's calldata: zero and non-zero bytes, as ABI encoding has.
HANDLE_OPS = keccak(text="handleOps")[:4] + bytes(100) + b"\x01" * 200
BASE_PVG = bundler._intrinsic_gas(HANDLE_OPS) + bundler.ENTRY_POINT_OVERHEAD_GAS
GWEI = 10**9


class FakeNode:
    """Answers eth_call for GasPriceOracle.getL1Fee and NodeInterface.gasEstimateL1Component, and keeps every call."""

    def __init__(self, l1_fee_wei: int = 0, arbitrum_l1_gas: int = 0):
        self.l1_fee_wei, self.arbitrum_l1_gas = l1_fee_wei, arbitrum_l1_gas
        self.calls: list[dict] = []
        self.eth = SimpleNamespace(call=self._call)

    def _call(self, tx: dict) -> bytes:
        self.calls.append(tx)
        if tx["to"] == bundler.OP_GAS_PRICE_ORACLE:
            return abi_encode(["uint256"], [self.l1_fee_wei])
        if tx["to"] == bundler.ARB_NODE_INTERFACE:
            return abi_encode(["uint64", "uint256", "uint256"], [self.arbitrum_l1_gas, 0, 0])
        raise AssertionError(f"unexpected call to {tx['to']}")


ENTRY_POINT_CONTRACT = SimpleNamespace(
    address=ENTRY_POINT,
    encode_abi=lambda abi_element_identifier, args: "0x" + HANDLE_OPS.hex(),
)


def _pvg(chain_name: str, node: FakeNode, gas_price: int) -> int:
    return bundler._pre_verification_gas(node, chain_name, ENTRY_POINT_CONTRACT, (), "0x" + "be" * 20, gas_price)


def test_base_l1_fee():
    print("\n[1] Base: the L1 fee, in wei, becomes preVerificationGas at the price the EntryPoint repays")
    fee, price = 7 * GWEI, 6_000_000  # what a 1-2 KB handleOps costs on Base, at ~0.006 gwei
    node = FakeNode(l1_fee_wei=fee)
    pvg = _pvg("base", node, price)
    l1_gas = -(-fee // price)
    check("the fee as gas at that price, rounded up, then buffered",
          pvg == BASE_PVG + int(l1_gas * bundler.L1_GAS_BUFFER), f"{pvg - BASE_PVG} != {int(l1_gas * bundler.L1_GAS_BUFFER)}")
    repaid = (pvg - BASE_PVG) * price
    check("repaid at that price, it covers the fee with the buffer's cushion, to within one gas",
          fee * bundler.L1_GAS_BUFFER - price <= repaid <= fee * bundler.L1_GAS_BUFFER + 2 * price, f"{repaid / fee:.4f}x")

    check("one call, to the GasPriceOracle predeploy's getL1Fee",
          len(node.calls) == 1 and node.calls[0]["to"] == "0x420000000000000000000000000000000000000F"
          and node.calls[0]["data"].startswith("0x" + keccak(text="getL1Fee(bytes)")[:4].hex()), str(node.calls))
    (priced,) = abi_decode(["bytes"], bytes.fromhex(node.calls[0]["data"][10:]))
    fields = rlp.decode(priced[1:])
    check("what it prices is an unsigned EIP-1559 transaction to the EntryPoint carrying the handleOps calldata",
          priced[:1] == b"\x02" and len(fields) == 9 and fields[5] == bytes.fromhex(ENTRY_POINT[2:])
          and fields[7] == HANDLE_OPS and fields[8] == [], str(fields)[:200])

    node = FakeNode(l1_fee_wei=price + 1)
    check("a fee just over one gas's worth is two gas, never rounded down",
          _pvg("base", node, price) == BASE_PVG + int(2 * bundler.L1_GAS_BUFFER))
    node = FakeNode(l1_fee_wei=fee)
    check("a zero gas price is no division by zero", _pvg("base", node, 0) > BASE_PVG)


def test_forks_and_other_chains():
    print("\n[2] only the live L2s pay an L1 charge; forks and L1s don't")
    for name in ("base-fork", "arbitrum-fork", "mainnet", "sepolia-fork", "bsc"):
        node = FakeNode(l1_fee_wei=7 * GWEI, arbitrum_l1_gas=5_000)
        check(f"{name}: calldata and fixed costs only, and no oracle asked", _pvg(name, node, 6_000_000) == BASE_PVG
              and node.calls == [], f"{_pvg(name, FakeNode(), 6_000_000) - BASE_PVG} extra")

    node = FakeNode(arbitrum_l1_gas=5_000)
    pvg = _pvg("arbitrum", node, 10**7)
    check("Arbitrum: NodeInterface's gas units, buffered, whatever the gas price",
          pvg == BASE_PVG + int(5_000 * bundler.L1_GAS_BUFFER) and node.calls[0]["to"] == bundler.ARB_NODE_INTERFACE,
          str(pvg - BASE_PVG))


if __name__ == "__main__":
    test_base_l1_fee()
    test_forks_and_other_chains()
    finish("All bundler L1-charge checks passed.")
