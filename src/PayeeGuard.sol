// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Conrad Japhet
pragma solidity 0.8.33;

import {IERC7579Execution, IERC7579ModuleConfig, Execution, MODULE_TYPE_VALIDATOR} from "@openzeppelin/contracts/interfaces/draft-IERC7579.sol";
import {ERC7579Utils, Mode, CallType} from "@openzeppelin/contracts/account/utils/draft-ERC7579Utils.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {IAppCheck} from "./interfaces/IAppCheck.sol";
import {SHRegistry} from "./SHRegistry.sol";
import {SHOracle} from "./SHOracle.sol";

/// @dev The one thing the guard asks of a wallet: who owns it.
interface IOwnedAccount {
    function owner() external view returns (address);
}

/// @dev The one thing the guard asks of its caller, the SpendingLimitModule.
interface ITrustedSpenders {
    function isTrustedSpender(address account, address spender) external view returns (bool);
}

/**
 * @title PayeeGuard
 * @author Conrad Japhet
 * @notice Holds a passkey's execution to the wallet's contacts: value may only go to
 *         the wallet itself or to an address the wallet's OWNER signed for, and trades and pool
 *         deposits may not stray more than {MAX_DEVIATION_BPS} from the oracle's prices.
 * @dev Why. Mitfah's server builds every op a passkey approves, and a passkey's prompt shows nothing
 *      about it, so a hacked Mitfah could otherwise have one Face ID tap send the wallet's money
 *      anywhere. The contact list lived only in Mitfah's database. Here it is enforced on chain, from
 *      signatures Mitfah can't make: the owner's own wallet signs each contact, for free, off chain.
 * @dev Who it binds. SpendingLimitModule calls {check} from its preCheck for every execution whose
 *      caller is the EntryPoint (a passkey op, or an owner-signed UserOp). Not checked: the owner's
 *      direct calls, installed executors (recurring payments, whose payee the owner set in a direct
 *      transaction), and trading agents' ops, which the wallet runs as a call to itself from
 *      `executeUserOp` (the owner's call, 2026-11-27: an agent may use any protocol and pay anyone,
 *      up to its own USD limit).
 * @dev Contact passes. The owner signs EIP-712 typed data, domain {name: "Mitfah", version: "2"} with
 *      no chainId and no verifying contract, so one signature works for every wallet the owner has on
 *      every chain: `Contact(string name,address payee,uint64 issuedAt)`. Mitfah appends the passes a
 *      call needs to the END of the `execute` calldata, after its ABI-encoded arguments, which
 *      Solidity's decoder ignores:
 *        execute(mode, executionCalldata) || pass_1 .. pass_n || uint8 n || {PASSES_MAGIC}
 *      Each pass is {PASS_LENGTH} bytes: payee (20) | issuedAt (8) | keccak256(name) (32) | r (32) |
 *      s (32) | v (1). They sit inside the bytes the passkey signs, but they don't need to:
 *      each proves itself, since only the owner's key makes a valid one. A pass is good while it was
 *      signed by the wallet's CURRENT owner, after the wallet's last {SpendingLimitModule-voidContactPasses},
 *      and no more than {MAX_ISSUE_AHEAD} in the future (so it can't be dated past a void yet to
 *      come). Passes are only checked when a payee needs one, so an unused pass costs calldata only.
 * @dev The rules, per call. Anything they don't allow is refused.
 *        - No calldata (a plain native send, or a bare call): the target must be a payee.
 *        - ERC-20 / ERC-721 / ERC-1155 token functions, on any contract, carrying no value:
 *          `transfer` to a payee; `transferFrom`, `safeTransferFrom` and `safeBatchTransferFrom` to a
 *          payee; `approve`, `increaseAllowance` and `increaseApproval` only to one of the wallet's
 *          trusted spenders (or to address(0), clearing an NFT's approval); `setApprovalForAll` only
 *          to revoke. They go by function name, like the limit module's own approval rules.
 *        - `removeAgent(wallet, agent)` on a validator module the wallet has installed: the passkey
 *          stop for a trading agent.
 *        - A call to an app with a check in {SHRegistry-appCheck}: the check reports the call's payees,
 *          trades and pool prices ({IAppCheck}), and this contract decides. Every payee must be the
 *          wallet or a contact; every trade must return at least (1 - {MAX_DEVIATION_BPS}) of what it
 *          puts in, at oracle prices; every pool must be priced within {MAX_DEVIATION_BPS} of the
 *          oracle. A trade or pool with a token the oracle doesn't price has no floor (the owner's
 *          call, 2026-11-26): a cap in the token's own units is what bounds it.
 *        - Delegatecall: refused (the wallet refuses it first for these callers).
 * @dev Stateless and not upgradeable. The rules above are fixed; the only thing that changes over time
 *      is which apps have checks, behind SHRegistry's two-day delay, and the oracle's feeds, behind
 *      SHOracle's.
 */
contract PayeeGuard {
    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @notice The calldata is not a call to `execute`, the only thing a passkey may run.
    error PayeeGuard_NotAnExecution();
    /// @notice The passes footer is malformed: too many passes, or longer than the calldata.
    error PayeeGuard_BadPasses();
    /// @notice Value would go to `payee`, which is neither the wallet nor a contact with a valid pass.
    error PayeeGuard_NotAContact(address payee);
    /// @notice An allowance on `token` would go to `spender`, which is not one of the wallet's trusted spenders.
    error PayeeGuard_UntrustedSpender(address token, address spender);
    /// @notice `setApprovalForAll(_, true)` on `token`: an operator for every NFT is never granted.
    error PayeeGuard_ApprovalForAll(address token);
    /// @notice A token function on `target` carried native value.
    error PayeeGuard_ValueOnTokenCall(address target);
    /// @notice Nothing allows this call: `target` has no app check and `selector` is no token function.
    error PayeeGuard_CallNotAllowed(address target, bytes4 selector);
    /// @notice The execution mode is a delegatecall or a call type the wallet doesn't run.
    error PayeeGuard_UnsupportedCallType();
    /// @notice A trade returns less than the floor allows, at oracle prices (18-decimal USD).
    error PayeeGuard_TradeBelowFloor(address tokenIn, address tokenOut, int256 inUsd, int256 outUsd);
    /// @notice A pool's price is more than {MAX_DEVIATION_BPS} off the oracle's (18-decimal USD).
    error PayeeGuard_PoolPriceOff(address token0, address token1, int256 usd0, int256 usd1);
    /// @notice A pool reported with a zero amount: an empty pool has no price to check.
    error PayeeGuard_EmptyPool(address token0, address token1);

    /*//////////////////////////////////////////////////////////////
                               CONSTANTS
    //////////////////////////////////////////////////////////////*/

    /// @notice The last four bytes of calldata that carries passes.
    bytes4 public constant PASSES_MAGIC = 0x4d465031; // "MFP1"
    /// @notice Bytes per pass: payee 20, issuedAt 8, name hash 32, r 32, s 32, v 1.
    uint256 public constant PASS_LENGTH = 125;
    /// @notice Most passes one execution may carry.
    uint256 public constant MAX_PASSES = 8;
    /// @notice How far past the current block a pass's `issuedAt` may lie.
    uint256 public constant MAX_ISSUE_AHEAD = 1 days;
    /// @notice How far a trade or a pool may stray from the oracle's prices, in basis points. 12%, the
    ///         same as the Mitfah app's own slippage ceiling, which a hacked server would skip.
    uint256 public constant MAX_DEVIATION_BPS = 1200;
    /// @dev The same in signed form, for the USD comparisons: 100% and 100% - MAX_DEVIATION_BPS.
    int256 private constant BPS = 10_000;
    int256 private constant FLOOR_BPS = 8_800;

    /// @notice EIP-712 type of a contact pass.
    bytes32 public constant CONTACT_TYPEHASH = keccak256("Contact(string name,address payee,uint64 issuedAt)");

    bytes4 private constant TRANSFER = 0xa9059cbb; // transfer(address,uint256)
    bytes4 private constant TRANSFER_FROM = 0x23b872dd; // transferFrom(address,address,uint256), ERC-20 and ERC-721
    bytes4 private constant SAFE_TRANSFER_FROM = 0x42842e0e; // safeTransferFrom(address,address,uint256)
    bytes4 private constant SAFE_TRANSFER_FROM_DATA = 0xb88d4fde; // safeTransferFrom(address,address,uint256,bytes)
    bytes4 private constant SAFE_TRANSFER_FROM_1155 = 0xf242432a; // safeTransferFrom(address,address,uint256,uint256,bytes)
    bytes4 private constant SAFE_BATCH_TRANSFER_FROM = 0x2eb2c2d6; // safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)
    bytes4 private constant APPROVE = 0x095ea7b3; // approve(address,uint256), ERC-20 and ERC-721
    bytes4 private constant INCREASE_ALLOWANCE = 0x39509351; // increaseAllowance(address,uint256)
    bytes4 private constant INCREASE_APPROVAL = 0xd73dd623; // increaseApproval(address,uint256)
    bytes4 private constant SET_APPROVAL_FOR_ALL = 0xa22cb465; // setApprovalForAll(address,bool)
    bytes4 private constant REMOVE_AGENT = 0xd7d282ad; // removeAgent(address,address)

    /*//////////////////////////////////////////////////////////////
                               IMMUTABLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Where the app checks and the price oracle are looked up.
    SHRegistry public immutable REGISTRY;

    /// @notice EIP-712 domain separator of every contact pass: {name: "Mitfah", version: "2"}.
    bytes32 public immutable DOMAIN_SEPARATOR;

    /*//////////////////////////////////////////////////////////////
                                 TYPES
    //////////////////////////////////////////////////////////////*/

    /// @dev What one {check} carries between its calls.
    struct Context {
        address account; // the wallet
        address module; // the caller, asked about trusted spenders
        uint64 voidedAt; // passes issued at or before this are void
        address owner; // the wallet's owner, read on the first pass that needs it
        uint256 verified; // bit i set once pass i is known good
        SHOracle oracle; // read on the first trade or pool that needs it
    }

    /// @param registry The protocol's SHRegistry.
    constructor(address registry) {
        REGISTRY = SHRegistry(registry);
        DOMAIN_SEPARATOR = keccak256(
            abi.encode(keccak256("EIP712Domain(string name,string version)"), keccak256("Mitfah"), keccak256("2"))
        );
    }

    /*//////////////////////////////////////////////////////////////
                                  CHECK
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Reverts unless every call in the execution `msgData` encodes is allowed for `account`.
     * @dev Called by SpendingLimitModule's preCheck; `msg.sender` is the module, asked whether a spender
     *      is trusted. A view: it changes nothing, so anyone may call it to try an execution out.
     * @param account  The wallet executing.
     * @param voidedAt The wallet's last {SpendingLimitModule-voidContactPasses}, or 0.
     * @param msgData  The wallet's full calldata: `execute(mode, executionCalldata)`, then any passes.
     */
    function check(address account, uint64 voidedAt, bytes calldata msgData) external view {
        if (msgData.length < 68 || bytes4(msgData[:4]) != IERC7579Execution.execute.selector) {
            revert PayeeGuard_NotAnExecution();
        }
        bytes calldata args = msgData[4:];
        Mode mode = Mode.wrap(bytes32(args[0:32]));
        uint256 dataOffset = uint256(bytes32(args[32:64]));
        uint256 dataLength = uint256(bytes32(args[dataOffset:dataOffset + 32]));
        bytes calldata executionCalldata = args[dataOffset + 32:dataOffset + 32 + dataLength];
        bytes calldata passes = _passes(msgData);

        Context memory ctx = Context({
            account: account, module: msg.sender, voidedAt: voidedAt, owner: address(0), verified: 0,
            oracle: SHOracle(address(0))
        });

        (CallType callType,,,) = ERC7579Utils.decodeMode(mode);
        if (callType == ERC7579Utils.CALLTYPE_SINGLE) {
            (address target, uint256 value, bytes calldata callData) = ERC7579Utils.decodeSingle(executionCalldata);
            _checkCall(ctx, passes, target, value, callData);
        } else if (callType == ERC7579Utils.CALLTYPE_BATCH) {
            Execution[] calldata batch = ERC7579Utils.decodeBatch(executionCalldata);
            for (uint256 i = 0; i < batch.length; i++) {
                _checkCall(ctx, passes, batch[i].target, batch[i].value, batch[i].callData);
            }
        } else {
            revert PayeeGuard_UnsupportedCallType();
        }
    }

    /**
     * @notice The EIP-712 digest the owner signs for a pass naming `payee`.
     * @param payee    The contact's address.
     * @param issuedAt When the pass was issued (unix seconds).
     * @param nameHash keccak256 of the contact's name, as signed.
     */
    function contactDigest(address payee, uint64 issuedAt, bytes32 nameHash) public view returns (bytes32) {
        return MessageHashUtils.toTypedDataHash(
            DOMAIN_SEPARATOR, keccak256(abi.encode(CONTACT_TYPEHASH, nameHash, payee, issuedAt))
        );
    }

    /*//////////////////////////////////////////////////////////////
                                 RULES
    //////////////////////////////////////////////////////////////*/

    /// @dev Applies the rules (see the contract NatSpec) to one call.
    function _checkCall(Context memory ctx, bytes calldata passes, address target, uint256 value, bytes calldata data)
        internal
        view
    {
        if (data.length == 0) {
            _requirePayee(ctx, passes, target);
            return;
        }
        bytes4 selector = data.length >= 4 ? bytes4(data[:4]) : bytes4(0);
        if (_isTokenFunction(selector)) {
            if (value != 0) revert PayeeGuard_ValueOnTokenCall(target);
            _checkTokenCall(ctx, passes, target, selector, data);
            return;
        }
        if (selector == REMOVE_AGENT && _isAgentStop(ctx.account, target, value, data)) return;

        address appCheck = REGISTRY.appCheck(target);
        if (appCheck == address(0)) revert PayeeGuard_CallNotAllowed(target, selector);
        (address[] memory payees, IAppCheck.Trade[] memory trades, IAppCheck.PoolPrice[] memory prices) =
            IAppCheck(appCheck).inspect(ctx.account, value, data);
        for (uint256 i = 0; i < payees.length; i++) {
            _requirePayee(ctx, passes, payees[i]);
        }
        for (uint256 i = 0; i < trades.length; i++) {
            _checkTrade(ctx, trades[i]);
        }
        for (uint256 i = 0; i < prices.length; i++) {
            _checkPoolPrice(ctx, prices[i]);
        }
    }

    /// @dev Whether `selector` is one of the token functions the guard reads itself.
    function _isTokenFunction(bytes4 selector) internal pure returns (bool) {
        return selector == TRANSFER || selector == TRANSFER_FROM || selector == SAFE_TRANSFER_FROM
            || selector == SAFE_TRANSFER_FROM_DATA || selector == SAFE_TRANSFER_FROM_1155
            || selector == SAFE_BATCH_TRANSFER_FROM || selector == APPROVE || selector == INCREASE_ALLOWANCE
            || selector == INCREASE_APPROVAL || selector == SET_APPROVAL_FOR_ALL;
    }

    /// @dev The token rules. Every function here names its recipient, spender or operator in a static
    ///      word right after the selector (the first, or for the transfers-from the second), so only
    ///      the first two words are decoded, which also checks they are clean addresses.
    function _checkTokenCall(
        Context memory ctx,
        bytes calldata passes,
        address token,
        bytes4 selector,
        bytes calldata data
    ) internal view {
        if (selector == TRANSFER) {
            (address to,) = abi.decode(data[4:68], (address, uint256));
            _requirePayee(ctx, passes, to);
        } else if (selector == SET_APPROVAL_FOR_ALL) {
            (, bool approved) = abi.decode(data[4:68], (address, bool));
            if (approved) revert PayeeGuard_ApprovalForAll(token);
        } else if (selector == APPROVE || selector == INCREASE_ALLOWANCE || selector == INCREASE_APPROVAL) {
            (address spender,) = abi.decode(data[4:68], (address, uint256));
            if (spender != address(0) && !ITrustedSpenders(ctx.module).isTrustedSpender(ctx.account, spender)) {
                revert PayeeGuard_UntrustedSpender(token, spender);
            }
        } else {
            // transferFrom and the safe transfers: (from, to, ...)
            (, address to) = abi.decode(data[4:68], (address, address));
            _requirePayee(ctx, passes, to);
        }
    }

    /// @dev Whether a `removeAgent` call is the passkey stop: no value, for this wallet, on a validator
    ///      this wallet has installed. Removing an agent only takes authority away.
    function _isAgentStop(address account, address target, uint256 value, bytes calldata data)
        internal
        view
        returns (bool)
    {
        if (value != 0 || data.length < 68) return false;
        (address forAccount,) = abi.decode(data[4:68], (address, address));
        return forAccount == account
            && IERC7579ModuleConfig(account).isModuleInstalled(MODULE_TYPE_VALIDATOR, target, "");
    }

    /// @dev A trade must return at least (1 - MAX_DEVIATION_BPS) of what it puts in, at oracle prices.
    ///      No floor when the oracle can't price either side.
    function _checkTrade(Context memory ctx, IAppCheck.Trade memory trade) internal view {
        SHOracle oracle = _oracle(ctx);
        if (!oracle.isPriced(trade.tokenIn) || !oracle.isPriced(trade.tokenOut)) return;
        int256 inUsd = oracle.getPrice(trade.tokenIn, trade.maxIn);
        int256 outUsd = oracle.getPrice(trade.tokenOut, trade.minOut);
        if (outUsd * BPS < inUsd * FLOOR_BPS) {
            revert PayeeGuard_TradeBelowFloor(trade.tokenIn, trade.tokenOut, inUsd, outUsd);
        }
    }

    /// @dev A pool's two amounts must be worth the same to within MAX_DEVIATION_BPS, at oracle prices.
    ///      No check when the oracle can't price either side.
    function _checkPoolPrice(Context memory ctx, IAppCheck.PoolPrice memory price) internal view {
        SHOracle oracle = _oracle(ctx);
        if (!oracle.isPriced(price.token0) || !oracle.isPriced(price.token1)) return;
        if (price.amount0 == 0 || price.amount1 == 0) revert PayeeGuard_EmptyPool(price.token0, price.token1);
        int256 usd0 = oracle.getPrice(price.token0, price.amount0);
        int256 usd1 = oracle.getPrice(price.token1, price.amount1);
        if (usd0 * BPS < usd1 * FLOOR_BPS || usd1 * BPS < usd0 * FLOOR_BPS) {
            revert PayeeGuard_PoolPriceOff(price.token0, price.token1, usd0, usd1);
        }
    }

    /// @dev The protocol's current oracle, read once per {check}.
    function _oracle(Context memory ctx) internal view returns (SHOracle) {
        if (address(ctx.oracle) == address(0)) ctx.oracle = SHOracle(REGISTRY.priceOracle());
        return ctx.oracle;
    }

    /*//////////////////////////////////////////////////////////////
                                 PASSES
    //////////////////////////////////////////////////////////////*/

    /// @dev Reverts unless `payee` is the wallet or a contact with a valid pass.
    function _requirePayee(Context memory ctx, bytes calldata passes, address payee) internal view {
        if (payee == ctx.account) return;
        uint256 count = passes.length / PASS_LENGTH;
        for (uint256 i = 0; i < count; i++) {
            bytes calldata pass = passes[i * PASS_LENGTH:(i + 1) * PASS_LENGTH];
            if (address(bytes20(pass[0:20])) != payee) continue;
            // forge-lint: disable-next-line(incorrect-shift)
            if (ctx.verified & (1 << i) != 0) return;
            if (_isValidPass(ctx, payee, pass)) {
                // forge-lint: disable-next-line(incorrect-shift)
                ctx.verified |= 1 << i;
                return;
            }
        }
        revert PayeeGuard_NotAContact(payee);
    }

    /// @dev Whether `pass`, which names `payee`, was signed by the wallet's owner after its last void
    ///      and isn't dated more than MAX_ISSUE_AHEAD ahead.
    function _isValidPass(Context memory ctx, address payee, bytes calldata pass) internal view returns (bool) {
        uint64 issuedAt = uint64(bytes8(pass[20:28]));
        if (issuedAt <= ctx.voidedAt || issuedAt > block.timestamp + MAX_ISSUE_AHEAD) return false;
        bytes32 digest = contactDigest(payee, issuedAt, bytes32(pass[28:60]));
        (address signer, ECDSA.RecoverError err,) =
            ECDSA.tryRecover(digest, uint8(pass[124]), bytes32(pass[60:92]), bytes32(pass[92:124]));
        if (err != ECDSA.RecoverError.NoError) return false;
        if (ctx.owner == address(0)) ctx.owner = IOwnedAccount(ctx.account).owner();
        return signer == ctx.owner;
    }

    /// @dev The passes `msgData` carries, as one run of PASS_LENGTH-byte records: empty unless the
    ///      calldata ends with PASSES_MAGIC. Reverts on a footer that claims more than it can hold.
    function _passes(bytes calldata msgData) internal pure returns (bytes calldata) {
        uint256 length = msgData.length;
        if (length < 5 || bytes4(msgData[length - 4:]) != PASSES_MAGIC) return msgData[0:0];
        uint256 count = uint8(msgData[length - 5]);
        if (count > MAX_PASSES) revert PayeeGuard_BadPasses();
        uint256 size = count * PASS_LENGTH;
        // The execute selector and its two head words must come first.
        if (length < 5 + size + 68) revert PayeeGuard_BadPasses();
        return msgData[length - 5 - size:length - 5];
    }
}
