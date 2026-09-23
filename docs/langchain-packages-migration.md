# Migrating the ERC20 + Uniswap V2 tools onto `langchain-erc20` / `langchain-uniswap-v2`

> **Historical record (2026-08-10).** `_submit_plan` no longer exists: write tools now
> return a quote through `_quote_plan` and only `confirm_transaction` sends (2026-09-23).
> See [app.md](app.md) `quotes.py` and THREAT_MODEL §4.2. The plan shapes below are unchanged.


**Status: IMPLEMENTED (2026-08-10).** All steps in §4 are done. Verified by running the real
`app/tools.py` tool bodies against an anvil Sepolia fork and executing every plan through the
live `SessionHandler` + `SpendingLimitModule` hook: **31/31 checks passed** (16 read tools,
12 write tools, 3 blocked-tool assertions). The `_submit_plan` batch shapes observed on-chain
were `[approve, swap]`, `[approve, swap, approve_reset]`, `[approve ×2, add, approve_reset ×2]`
and `[approve, remove]`, all accepted.

Kept as the record of *why* the wrapper layer exists — do not delete it when trimming docs;
§3 is the reason `app/toolkits.py` is not just `toolkit.get_tools()`.

**Verdict:** yes — the packages replace the *bodies* of the ERC20 and Uniswap V2 tools,
but **not** the agent-facing `@tool` surface. The project's tool signatures are unchanged;
their internals delegate.

### What the implementation changed relative to this plan

- **`factory_address` is not passed at all.** The plan proposed reusing `constants.py`'s
  per-chain factory switch. The toolkit reads `router.factory()` when the argument is omitted,
  which is strictly better: pair lookups cannot drift from the router the wallet actually uses,
  and Anvil works without a hardcoded entry. The four `*_V2_FACTORY` constants and
  `load_iuniswap_factory` were deleted rather than rewired.
- **`iweth` was deleted too** (a further 152 lines). Once `wrap_eth` delegates to
  `wrap_native`, nothing calls `deposit()`/`withdraw()` through `load_ierc20`, so its
  weth/wbnb ABI branch and the `uniswap_pair=` parameter went with it.
- **A bug was fixed in passing:** the old `remove_liquidity_eth` called `_submit_router_call`
  with **no `approvals=` argument**, so `removeLiquidityETH`'s `transferFrom` of the LP token
  had no allowance — and the module forbids pre-approving in a separate transaction, so the
  tool could not have worked. The package always includes the approve; it now runs as
  `[approve, remove]`.
- **Actual sizes:** `tools.py` 2288 → 1926 lines; `abi.py` 1923 → 977 (946 removed, more than
  the ~800 estimated); `app/toolkits.py` added at 117 lines.

---

## 1. What was tested

A throwaway venv with only the two built wheels installed:

```
langchain_erc20-0.1.0-py3-none-any.whl
langchain_uniswap_v2-0.3.0-py3-none-any.whl
```

Both installed clean (web3 7.16.0, langchain-core 1.5.3, no conflicts with
`requirements.txt`'s web3 7.14.1 — the packages floor at `web3>=7.0`).

Tests ran against an **anvil fork of Sepolia** at the project's real deployed wallet
`0x3AC9cf0214fe05511e533b78F104739e35035a4b`, funded with WETH/LINK/USDC/DAI via storage
override, using the live Sepolia Uniswap V2 router `0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3`.

### Parity against the project's own encoders — 27/28

Every write plan was compared to the exact calldata `app/tools.py` would submit.

| Flow | Result |
|---|---|
| `get_balance` / `get_allowance` / `get_native_balance` | identical values |
| `transfer` | calldata **byte-identical** to `transfer_erc20` |
| `wrap_native` | `deposit()` + value, identical to `wrap_eth` |
| `get_quote_out` | matches `router.getAmountsOut` exactly |
| `swap_exact_tokens_for_tokens` | `[approve, swap]`, approve identical, path/recipient/deadline match |
| `swap_tokens_for_exact_tokens` | `[approve, swap, approve_reset]`, amountInMax matches |
| `add_liquidity` | `[approve, approve, add_liquidity, approve_reset, approve_reset]` |
| `remove_liquidity` | `[approve(LP), remove_liquidity]` |

The one non-match was a **precision bug in this project, not the package** (see §5.1).

### Execution through the real hook — all flows accepted

`execute()` is `onlyEntryPointOrSelfOrOwner`, so plans were driven through the real
`SessionHandler` + `SpendingLimitModule` by impersonating the owner on the fork:

| Plan | Roles | Result |
|---|---|---|
| erc20 `transfer` | `[action]` | accepted, gas 152 845, budget −$5.00 |
| erc20 `wrap_native` | `[action]` | accepted, gas 128 767 |
| `swap_exact_tokens_for_tokens` | `[approve, swap]` | accepted, gas 229 172 |
| `swap_tokens_for_exact_tokens` | `[approve, swap, approve_reset]` | accepted, gas 236 731 |
| `add_liquidity` | `[approve ×2, add, approve_reset ×2]` | accepted, gas 286 131 |
| `remove_liquidity` | `[approve(LP), remove]` | accepted, gas 243 576 |

**Negative controls behaved correctly** — both reverted on-chain:

- `approve(unlimited=True)` → `SpendingLimitModule_UnlimitedApprovalRejected`
- bare `approve` with nothing spending it → `SpendingLimitModule_StandingApprovalNotAllowed`

This is the important result: `reset_residual_approvals` (which defaults to `True` in
`tx_mode="calls"`) produces exactly the approval sequencing this wallet's no-standing-approval
rule requires, on every flow, without any project-side patching.

---

## 2. Coverage matrix

### Fully replaceable (delegate the body)

| `app/tools.py` | Package tool |
|---|---|
| `get_erc20_balance`, `get_contact_erc20_balance` | `erc20.get_balance` |
| `get_erc20_allowance` | `erc20.get_allowance` |
| `get_eth_balance` | `erc20.get_native_balance` |
| `wrap_eth` | `erc20.wrap_native` |
| `transfer_erc20` | `erc20.transfer` |
| `transferFrom_erc20` | `erc20.transfer_from` |
| `get_quote_in` / `get_quote_out` | same names |
| `get_pool_quote`, `get_lp_amounts`, `get_liquidity_token_balance` | same names |
| `is_exact_input_sufficient` | `uni.is_token_balance_sufficient` |
| `is_derived_input_sufficient` | `uni.is_derived_token_input_sufficient` |
| `is_liquidity_sufficient` | same name (+ `_eth` variant) |
| `is_liquidity_removal_sufficient` | same name |
| all 6 `swap_*` tools | same names (`ETH` → `eth`) |
| `add_liquidity`, `add_liquidity_eth` | same names |
| `remove_liquidity`, `remove_liquidity_eth` | same names |
| `_submit_router_call` (88 lines) | replaced by a ~15-line `_submit_plan` |

### Stays project-side — no package equivalent

- `send_eth` — neither package sends raw native value. `wrap_native` is not a substitute.
- `get_price`, `get_usd_value` — SHOracle-backed, project-specific.
- `preflight_check`, `check_session_validity`, `check_remaining_budget`,
  `check_spending_within_budget`, `get_session_keys`, `get_all_sessions` — session-key and
  USD-cap logic.
- contacts / `get_supported_tokens` / `get_native_asset` — DB layer.
- `get_agent_id` — reads `SHRegistry.agentId()`, project-specific. (The ERC-8004 *tools* have
  since moved to `langchain-erc8004` on the same pattern — see the ERC-8004 section of
  `docs/app.md`.)

### Must **not** be exposed to the agent

These build valid ERC-20 calldata but always revert on this wallet:

| Tool | Why |
|---|---|
| `erc20.approve` | leaves a standing approval → `StandingApprovalNotAllowed` |
| `uni.approve_token` | same |
| `erc20.revoke_approval` | bare `approve(0)`, nothing to reset |
| `erc20.approve(unlimited=True)` | `UnlimitedApprovalRejected` |

Approvals on this wallet are only ever legal *inside* a batch that consumes them — which the
write tools already emit. Filter these three names out of `get_tools()`.

---

## 3. Why the tool surface must stay

Three structural mismatches make a straight swap impossible:

1. **Multi-tenancy.** Every project tool takes `user_id`. Package tools have no such
   parameter — a toolkit instance is bound to one RPC, one router, one token map. Requires a
   per-`user_id` toolkit cache.
2. **Tickers and contacts.** The project speaks `"usdc"` and `"Sandy"`.
   `langchain-erc20` accepts a `tokens=` map (case-insensitive — verified), but
   **`langchain-uniswap-v2` requires raw addresses** and rejects tickers outright. Contact-name
   resolution exists in neither.
3. **The packages never submit.** They return plans. The session-key ciphertext, Vault signing,
   bundler routing and receipt polling all stay here. Handing raw plans to the agent and adding
   a generic `execute_plan` tool would move calldata through the model's context — don't.

`ToolException` from `langchain_core` is what both packages raise, so
`t.handle_tool_error = True` in `get_tools()` keeps working unchanged.

---

## 4. Implementation steps

### Step 1 — dependencies

```diff
  # Blockchain interface
  web3==7.14.1
  eth-account==0.13.7
+ langchain-erc20==0.1.0
+ langchain-uniswap-v2==0.3.0
```

Both are alpha (`0.1.0` / `0.3.0`) with an explicitly unstable public API — pin exactly.

### Step 2 — new file `app/toolkits.py`

Toolkits hit the RPC at construction (`ConnectionError` if unreachable), so build lazily and
cache per `user_id`, mirroring `contracts.py`:

```python
from langchain_erc20 import ERC20Toolkit
from langchain_uniswap_v2 import UniswapV2Toolkit

from constants import SEPOLIA_UNISWAP_V2_FACTORY, ...  # existing factory constants
from contracts import load_session_handler
from db import get_supported_tokens, get_token_address
from network_config import load_network_config

_erc20_toolkit_cache: dict[int, dict] = {}
_uniswap_toolkit_cache: dict[int, dict] = {}

# Tools that always revert under SpendingLimitModule's no-standing-approval rule.
_BLOCKED = {"approve", "approve_token", "revoke_approval"}


def _token_map(user_id: int) -> dict[str, str]:
    _, chain_id, _ = load_network_config(user_id)
    return {t: get_token_address(chain_id, t) for t in get_supported_tokens(user_id)}


def get_erc20_tools(user_id: int) -> dict:
    if user_id not in _erc20_toolkit_cache:
        w3, chain_id, _ = load_network_config(user_id)
        tokens = _token_map(user_id)
        tk = ERC20Toolkit(
            rpc_url=w3.provider.endpoint_uri,
            tx_mode="calls",                       # zero nonce/gas RPC; plan["calls"] only
            tokens=tokens,
            native_wrapped_address=tokens[get_native_wrapped_ticker(chain_id)],
        )
        _erc20_toolkit_cache[user_id] = {
            t.name: t for t in tk.get_tools() if t.name not in _BLOCKED
        }
    return _erc20_toolkit_cache[user_id]


def get_uniswap_tools(user_id: int) -> dict:
    if user_id not in _uniswap_toolkit_cache:
        w3, chain_id, _ = load_network_config(user_id)
        tokens = _token_map(user_id)
        tk = UniswapV2Toolkit(
            rpc_url=w3.provider.endpoint_uri,
            # Read the router from the wallet, exactly as load_iuniswap_router does —
            # never from the package's chain registry, which knows nothing about Anvil
            # or Ubeswap on Celo.
            router_address=load_session_handler(user_id).functions.getRouter().call(),
            factory_address=_factory_for(chain_id),   # existing constants.py switch
            native_wrapped_address=tokens[get_native_wrapped_ticker(chain_id)],
            tx_mode="calls",
            # Default is already True in calls mode; make it explicit — the module
            # depends on it.
            reset_residual_approvals=True,
        )
        _uniswap_toolkit_cache[user_id] = {
            t.name: t for t in tk.get_tools() if t.name not in _BLOCKED
        }
    return _uniswap_toolkit_cache[user_id]


def invalidate_toolkits(user_id: int) -> None:
    _erc20_toolkit_cache.pop(user_id, None)
    _uniswap_toolkit_cache.pop(user_id, None)
```

Call `invalidate_toolkits(user_id)` from `contracts.invalidate_cache(user_id)` so a redeploy or
a network switch drops both. A token added to the DB mid-session also needs an invalidation —
the token map is snapshotted at construction.

Use the **explicit constructors, never `for_chain()`**: the registry has no Anvil entry, and on
Celo `langchain-erc20.for_chain(42220)` deliberately raises.

### Step 3 — replace `_submit_router_call` with `_submit_plan`

```python
def _submit_plan(user_id, key_ciphertext, plan):
    """Submit a package execution plan as one UserOp: single call direct, multi-call batched.

    Batching is mandatory whenever the plan contains an approval — SpendingLimitModule
    reverts anything that leaves an allowance standing, so [approve, spend, reset] must
    land atomically.
    """
    execs = [
        (Web3.to_checksum_address(c["to"]), c["value"], bytes.fromhex(c["data"][2:]))
        for c in plan["calls"]
    ]
    if len(execs) == 1:
        target, value, data = execs[0]
        tx_hash, receipt = send_user_op_as_session(
            user_id=user_id, key_ciphertext=key_ciphertext,
            target=target, value=value, data=data,
        )
    else:
        tx_hash, receipt = send_batch_user_op_as_session(
            user_id=user_id, key_ciphertext=key_ciphertext, executions=execs,
        )
    if receipt["status"] != 1:
        raise ToolException(f"UserOp failed! tx: {tx_hash.hex()}")
    return tx_hash, receipt
```

Verified: `plan["calls"]` converts to `(address, uint256, bytes)` triples that
`encode_batch_execution_calldata` accepts verbatim.

### Step 4 — rewrite tool bodies, keep signatures and docstrings

Every tool keeps its `user_id` / `session_key_ciphertext` / ticker / contact-name signature and
its prompt-tuned docstring. Only the body changes:

```python
@tool
def swap_exact_tokens_for_tokens(
    user_id: int, session_key_ciphertext: str, token_in: str, token_out: str,
    amount_in: float, slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
):
    """<unchanged docstring>"""
    _, chain_id, _ = load_network_config(user_id)
    plan = get_uniswap_tools(user_id)["swap_exact_tokens_for_tokens"].invoke({
        "token_in": get_token_address(chain_id, token_in),
        "token_out": get_token_address(chain_id, token_out),
        "amount_in": amount_in,
        "from_address": load_session_handler(user_id).address,
        "slippage_bps": slippage_bps,
    })
    tx_hash, receipt = _submit_plan(user_id, session_key_ciphertext, plan)
    s = plan["summary"]
    return (
        f"Tx hash: `{tx_hash.hex()}`, Status: {receipt['status']}, "
        f"{token_in.upper()} spent: {s['amount_in']}, "
        f"{token_out.upper()} min received: {s['amount_out_min']:.6f}"
    )
```

Two small behaviour notes when rewriting the return strings:

- Swap summaries expose `amount_out_min`, not the expected `amount_out` the current strings
  report. Either reword to "min received" (above) or call `get_quote_out` for the estimate.
- `get_pool_quote` / `get_lp_amounts` return whole units only (`amount_a`,
  `amount_b_desired`, `expected_a/b`). The current `*_base` and `decimals_a/b` keys are gone —
  which is fine once the write flows no longer re-derive base units themselves.

Leave the packages' own `preflight=True` on. It duplicates a little of
`is_*_sufficient`, but it raises a `ToolException` naming the exact shortfall before any
UserOp is built. The project's `preflight_check` (session validity + USD cap) remains separate
and still runs first — the packages know nothing about either.

### Step 5 — delete what's now dead

| Target | Lines |
|---|---|
| `tools.py` ERC20 section (687–919) | 233 |
| `tools.py` Uniswap section (921–2133) | 1 213 |
| `tools.py` `_submit_router_call` (113–200) | 88 |
| **subtotal removed** | **~1 534 of 2 288 (67%)** |
| replaced by wrappers + `toolkits.py` | ~600 |

Also removable once nothing references them:

- `contracts.py`: `load_iuniswap_router`, `load_iuniswap_factory`, `load_iuniswap_pair`,
  and their caches (`_router_cache`, `_factory_cache`, `_pair_cache`).
  Keep `load_ierc20` — `deploy_wallet.py:175` still uses it to resolve tickers at deploy time.
- `abi.py`: `iuniswap_v2_factory` (95), `iuniswap_v2_pair` (342), `iuniswap_v2_router02` (363),
  `iweth` (154) — **800 lines**, once `wrap_eth` goes through `erc20.wrap_native`.
  `ierc20_extended` stays (used by `load_ierc20`).

### Step 6 — tests and docs

- Re-point `test/fork/` Python-side checks at the new wrappers.
- Update `docs/app.md` and `docs/reference.md` tool tables (the ticker→address boundary moves).
- `THREAT_MODEL.md` gains a dependency: calldata construction for every swap and transfer now
  comes from two external alpha packages. Add them to the supply-chain section and pin hashes.

---

## 5. Findings worth acting on regardless

### 5.1 The project's slippage math was lossy (fixed by the migration)

`app/tools.py` computes bounds in float:

```python
amount_out_min = int(quote["amount_out_base"] * (BPS_DENOMINATOR - slippage_bps) / BPS_DENOMINATOR)
```

At 18 decimals a Python float has ~15–16 significant digits, so this drifts. Measured on the
fork for a 0.01 WETH → LINK swap:

```
exact amount_out_base : 2092369741078638309
integer math (package): 2081907892373245117
float math   (project): 2081907892373245184   <- 67 wei high
```

For `amountOutMin` drifting high is merely over-strict. For **`amountInMax` in exact-output
swaps and `amountADesired`/`amountBDesired` in `addLiquidity`, drifting high means approving
and spending more than intended** — small, but on the wrong side, and it scales with token
value. The packages use `base * (BPS ± bps) // BPS` throughout.

Resolved: every one of those float expressions was deleted with the tool bodies that held
them. All bounds are now derived inside the packages in exact integer arithmetic. Only
`DEFAULT_SLIPPAGE_BPS` survives in `tools.py`, as the default for the agent-facing
`slippage_bps` argument; `BPS_DENOMINATOR` and `SWAP_DEADLINE_SECS` are gone.

### 5.2 Things the packages give you that the project lacks

- `get_token_metadata`, `is_balance_sufficient`, `is_allowance_sufficient`,
  `transfer_all`, `batch_transfer`, `unwrap_native`, `supports_permit`.
- `bytes32` `name()`/`symbol()` fallback (MKR-style tokens).
- `decimals()` absent → raises instead of assuming 18. The project calls
  `erc20.functions.decimals().call()` unguarded in ~8 places.
- Non-standard-return tokens (USDT/BNB/OMG) handled by never decoding write returndata.

### 5.3 Risks

- **Alpha API.** `langchain-erc20` 0.1.0 states the public API may change before 1.0;
  `langchain-uniswap-v2` already broke write-tool return shape between 0.2.0 and 0.3.0. The
  wrapper layer in Step 4 is what contains that blast radius — it is not optional overhead.
- **EIP-2612 permit is unimplemented** in `langchain-erc20`, and is EOA-only anyway. No impact
  here; this wallet batches `[approve, action]` instead.
- **Token registry is yours.** Neither package ships addresses; `_token_map` remains the
  single source of truth, as today.
- **Celo.** `langchain-erc20` refuses `for_chain(42220)` by design (CELO is natively ERC-20, so
  wrapping is meaningless). The explicit constructor still works for everything except
  `wrap_native`/`unwrap_native` — matching the existing note in `constants.py` that `wrap_eth`
  has no meaning there.

---

## 6. Recommendation

Migrate. The packages produce byte-identical calldata to what this project already builds, and
their approval sequencing satisfies `SpendingLimitModule` on every flow — proven by execution
against the real hook, not by inspection. It removes ~1 500 lines of tool code and ~800 lines of
ABI, and fixes a genuine precision bug on the way.

Do it in the order above, one section at a time (ERC20 first — it is 233 lines and has no
approval sequencing to get wrong), with the fork test suite green between steps.
