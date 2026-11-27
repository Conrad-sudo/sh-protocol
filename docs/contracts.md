# Smart Contract Architecture

The smart contract layer implements the SessionHandler Protocol as an **ERC-7579 modular account system**: shared infrastructure contracts deployed once per chain, and per-user `SessionHandler` smart wallets deployed on demand through `SHFactory`.

The account (`SessionHandler`) validates its own UserOperations and manages its session keys, which are **passkeys** (P-256 keys on the user's own devices, up to five per wallet). The USD spending cap lives in a single installable **hook** (`SpendingLimitModule`) that meters net portfolio value around every execution, so any ERC-7579 account could reuse it. Every wallet also gets one **executor** (`RecurringPaymentModule`), which sends recurring payments the owner approved once; each payment goes back through the wallet's guard, fee and cap. And every wallet gets one **validator**, **`AgentModule`**, empty until the owner adds an agent: a trading agent with its own key and its own USD limit, whose trades are ordinary UserOps the wallet pays gas for, like a passkey's, run through the same guard, fee and cap.

## Contract Overview

```
src/
├── SHFactory.sol              ← User-facing factory — deploys one SessionHandler per user
├── SHTreasury.sol             ← Protocol admin root — owns SHRegistry, SHOracle, and SHFactory; fee sink
├── SHRegistry.sol             ← Central config store — every address a wallet needs (EntryPoint, both ERC-8004 registries, all three modules, oracle, treasury, both fees, agentId)
├── SHOracle.sol               ← Chainlink-based USD value converter
├── SessionHandler.sol         ← ERC-7579 smart account — self-validation, passkey session keys, the validator route (executeUserOp), cap config, admin guard
├── SpendingLimitModule.sol   ← ERC-7579 Hook (type 4 only) — global USD spending cap (net-value metering)
├── RecurringPaymentModule.sol ← ERC-7579 Executor (type 2) — owner-approved recurring payments, run by a keeper
├── AgentModule.sol            ← ERC-7579 Validator (type 1) — trading agents with their own key and USD limit; on every wallet, empty until the owner adds one
├── PayeeGuard.sol             ← Stateless rules the spending hook applies to passkeys' executions (not agents'): pay only owner-signed contacts, call only apps with a check, trade within 12% of the oracle
├── appchecks/                 ← IAppCheck contracts, one per app, registered in SHRegistry behind a 2-day delay
│   ├── AppCheckBase.sol          ← Shared report building, and "parking" (funds paid to the app itself must leave again in the same call)
│   ├── SwapRouter02Check.sol     ← Uniswap SwapRouter02: the four V3 swaps, unwrapWETH9/sweepToken/refundETH, multicall
│   ├── V3PositionManagerCheck.sol← Uniswap V3 positions: mint/increase/decrease/collect/burn, unwrap/sweep/refundETH, multicall
│   ├── V2RouterCheck.sol         ← A Uniswap-V2-style router (PancakeSwap on BSC): swaps and liquidity, no permits
│   ├── WrappedNativeCheck.sol    ← WETH9 / WBNB deposit and withdraw
│   └── ReputationCheck.sol       ← ERC-8004 Reputation Registry reviews
├── utils/
│   └── WebAuthnNative.sol        ← OpenZeppelin's WebAuthn verifier, calling the P-256 precompile only (no Solidity fallback)
├── interfaces/
│   ├── IWETH.sol                 ← WETH interface (extends IERC20Extended)
│   ├── IERC20Extended.sol        ← IERC20 + IERC20Metadata, plus non-standard allowance functions (for selector matching only)
│   ├── AggregatorV3Interface.sol ← Vendored Chainlink price-feed interface (used by SHOracle)
│   ├── IUserOpHooks.sol          ← preExecute / postExecute: what the wallet calls on a validator around each op it approved
│   ├── IAppCheck.sol             ← What an app check reports about one call: payees, trades, pool prices
│   ├── IIdentityRegistry.sol     ← ERC-8004 IIdentityRegistry interface
│   └── IReputationRegistry.sol   ← ERC-8004 IReputationRegistry interface
└── mocks/
    ├── MockIdentityRegistry.sol   ← Full ERC-8004 Identity Registry mock (local testing)
    ├── MockReputationRegistry.sol ← ERC-8004 Reputation Registry mock (local testing)
    ├── ERC20Mock.sol              ← Mintable ERC20 for local testing
    ├── MockV3Aggregator.sol       ← Chainlink AggregatorV3Interface mock, seeded per-token on Anvil
    └── MockWeth.sol               ← WETH mock with deposit/withdraw for Anvil

script/
├── DeploySHProtocol.s.sol     ← Deployment entry point (SHTreasury → SHOracle → agent → SHRegistry → setRegistry → module → setSpendingLimitModule → recurring module → setRecurringPaymentModule → AgentModule → setAgentModule → SHFactory → setFactory)
├── Constants.s.sol            ← Chain IDs, canonical addresses, per-network token/Chainlink-feed addresses, Anvil mock prices
├── HelperConfig.s.sol         ← Chain-specific configuration resolver (Mainnet, Sepolia, BSC, Arbitrum, Base, Anvil)
└── SendPackedUserOp.s.sol     ← ERC-7579 UserOp construction and signing helper (single + batch)

test/
├── unit/
│   ├── SHProtocolTest.t.sol            ← Core unit suite: deploy, module lifecycle, metering, approvals, trusted spenders, passkey management (131 tests)
│   ├── TokenCapTest.t.sol              ← Limits in a token's own units: setters, charging, allowed calls, broken tokens
│   ├── NativeMeteringTest.t.sol        ← ETH/BNB counted or not: the deploy choice, both directions, the switch and WETH with it (22 tests)
│   ├── SessionGuardTest.t.sol          ← End-to-end guard + session-key auth proof via the real EntryPoint (85 tests)
│   ├── PasskeyValidationTest.t.sol     ← How a passkey-signed UserOp is validated: what passes, every way it can fail, the op deadline, gas (54 tests)
│   ├── SHAdminTest.t.sol               ← Admin surface: the SHTreasury ownership graph, the oracle timelock, feed admin (70 tests)
│   ├── RecurringPaymentTest.t.sol      ← RecurringPaymentModule on a real wallet: who may create/cancel, when run pays, guard/fee/cap on each payment, gas (26 tests)
│   ├── RegistryFeeTest.t.sol           ← The two fees in gwei beside the treasury, setKeeperFee, setRecurringPaymentModule (13 tests)
│   ├── AgentModuleTest.t.sol           ← AgentModule on a real wallet: who adds/removes agents, signatures, the three rules, both limits, fees, the window, not held to contacts (85 tests)
│   ├── PayeeGuardTest.t.sol            ← The contact rule and trade floors through the wallet: passes, void, token rules, app checks and their delay, who is held (47 tests)
│   ├── AppChecksTest.t.sol             ← Each app check's report and refusals, against mocks of the app (29 tests)
│   └── SpendingLimitModuleHarness.sol ← Test harness exposing the module's internal calldata/approval helpers
├── utils/
│   ├── PasskeyHelper.sol               ← A soft passkey: builds WebAuthn assertions the way a browser + authenticator do
│   ├── ConfiguredTokens.sol            ← Every ERC-20 a NetworkConfig names, to check the deploy script prices them all
│   ├── ContactPassHelper.sol           ← Signs contact passes and appends them to execute calldata, as the app does
│   └── TestAppChecks.sol               ← OpenAppCheck (allows any call) and ScriptedAppCheck (a set report), for tests
├── fork/
│   ├── SHForkTestBase.sol              ← Shared abstract fork suite (32 tests) parameterized per network; V2 venue hooks: swap calldata, _trustedSpenders(), liquidity round-trip, _depositBatch()
│   ├── SHUniswapV3ForkTestBase.sol     ← Same suite on Uniswap V3: swaps through SwapRouter02, trusts it and the position manager, and a position round-trip (mint → half out → rest out + burn)
│   ├── SHMainnetUniswapV3Test.t.sol    ← Mainnet-fork instance (WETH→DAI, V3 0.3%)
│   ├── SHSepoliaUniswapV3Test.t.sol    ← Sepolia-fork instance (WETH→USDC, V3 0.05%)
│   ├── SHPancakeswapV2Test.t.sol       ← BSC-fork instance (USDT→WBNB→LINK, PancakeSwap V2)
│   ├── SHArbitrumUniswapV3Test.t.sol   ← Arbitrum-fork instance (WETH→USDC, V3 0.05%) + L2 sequencer-gate tests
│   └── SHBaseUniswapV3Test.t.sol       ← Base-fork instance (WETH→USDC, V3 0.05%) + L2 sequencer-gate tests
└── invariant/
    ├── InvariantSH.t.sol  ← Stateful invariant tests (11 invariants)
    └── SHHandler.sol      ← Action handler for fuzzing, with a ghost metering model
```

> **Celo is not yet wired into the Solidity deployment path.** The Python app layer has partial scaffolding for Celo, but `HelperConfig.s.sol` / `Constants.s.sol` have no Celo branch, so `DeploySHProtocol.s.sol` cannot deploy to Celo until that's added.

---

## `SHRegistry.sol`

The `SHRegistry` is the central configuration store. Deployed `SessionHandler` wallets read runtime parameters — protocol fee, keeper fee, treasury address, price oracle, and agent identity — from this single contract, so any parameter can be updated by the operator without redeploying user wallets. It is owned by `SHTreasury`, and all admin flows through `SHTreasury`'s pass-through setters. The `owner` is supplied to the constructor rather than taken from `msg.sender`, so the registry is deployed already owned by the treasury with no follow-up `transferOwnership`.

**Stored parameters:**

| Parameter | Type | Purpose |
|---|---|---|
| `_protocolFeeGwei` | `uint48` (private) | Flat fee charged on every session-key execution, stored **in gwei**; `protocolFee()` and `getFee()` return it in wei, with no oracle call. Bounded to `[MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE]`. See [Protocol fee](#protocol-fee) below. |
| `MIN_PROTOCOL_FEE` / `MAX_PROTOCOL_FEE` | `uint256` (immutable) | The fee's bounds in wei (whole gwei), set by the constructor. The deploy script converts **$0.005 and $0.10** through the chain's native price, so each chain gets the same dollar range in its own coin. |
| `_keeperFeeGwei` | `uint48` (private) | Flat fee added on top of the protocol fee **only** when an executor module runs a payment, stored in gwei; `keeperFee()` returns wei. It pays back the gas the backend spends sending recurring payments. See [Keeper fee](#keeper-fee). |
| `MIN_KEEPER_FEE` / `MAX_KEEPER_FEE` | `uint256` (immutable) | The keeper fee's bounds in wei (whole gwei). Dollar figures are **per chain** in `HelperConfig`. The floor may be zero. |
| `treasury` | `address` | `SHTreasury` — fee sink and admin root (owns this registry, the oracle, and the factory) |
| `priceOracle` | `address` | Canonical `SHOracle` address for USD accounting. **Also governs spending-cap enforcement** — `SpendingLimitModule` resolves it from here on every valuation, so changing it changes how every deployed wallet meters spending. See [THREAT_MODEL.md](../THREAT_MODEL.md) §3.8. |
| `agentId` | `uint256` | ERC-8004 token ID of the registered protocol agent. **`0` is a valid id** — ERC-8004 registries mint from 0, so no zero-check exists or should. |
| `spendingLimitModule` | `address` | Hook installed on wallets deployed from here on. Owner-settable; **not** a constructor arg — see below. Changing it never touches existing wallets. |
| `recurringPaymentModule` | `address` | Executor installed on wallets deployed from here on. Owner-settable like `spendingLimitModule` (the user's choice, for symmetry); changing it never touches existing wallets. More sensitive than the hook setter: an executor can move funds (within the guard and the cap), a hook can only refuse. |
| `agentModule` | `address` | Validator (`AgentModule`) installed on wallets deployed from here on. Owner-settable the same way; changing it never touches existing wallets. As sensitive as the executor setter: a validator approves UserOps for the wallet (AgentModule only ops signed by an agent the wallet's owner added). |
| `ENTRY_POINT` | `address` (immutable) | Canonical ERC-4337 EntryPoint; `SHFactory` bakes it into the `SessionHandler` implementation, so every wallet shares it |
| `REPUTATION_REGISTRY` | `address` (immutable) | Reputation Registry, baked into the implementation the same way |
| `IDENTITY_REGISTRY` | `address` (immutable) | ERC-8004 Identity Registry, baked into the implementation the same way |
| `factory` | `address` | The SHFactory deploying against this registry. Off-chain discoverability only — no contract reads it. |

```solidity
struct FeeBounds { uint256 initial; uint256 min; uint256 max; }   // wei, each a whole gwei

uint256 public immutable MIN_PROTOCOL_FEE;   // wei — $0.005 at the native price on deploy day
uint256 public immutable MAX_PROTOCOL_FEE;   // wei — $0.10 at the native price on deploy day
uint256 public immutable MIN_KEEPER_FEE;     // wei — per chain (HelperConfig); may be 0
uint256 public immutable MAX_KEEPER_FEE;     // wei — per chain (HelperConfig)
uint256 public constant MAX_STORABLE_FEE = type(uint48).max * 1 gwei;   // ≈ 281,000 ETH

uint256 public constant ORACLE_TIMELOCK = 2 days;

function getFee() external view returns (uint256);        // protocol fee in wei (same as protocolFee())
function protocolFee() public view returns (uint256);     // wei
function keeperFee() public view returns (uint256);       // wei
function feeConfig() external view returns (address treasury, uint256 protocolFee, uint256 keeperFee);   // one slot
function setProtocolFee(uint256 newFee) external onlyOwner;   // wei, whole gwei, within bounds
function setKeeperFee(uint256 newFee) external onlyOwner;     // wei, whole gwei, within bounds
function setTreasury(address newTreasury) external onlyOwner;
function setAgentId(uint256 newId) external onlyOwner;
function setSpendingLimitModule(address newModule) external onlyOwner;
function setRecurringPaymentModule(address newModule) external onlyOwner;
function setAgentModule(address newModule) external onlyOwner;
function setFactory(address newFactory) external onlyOwner;

// Oracle changes are two-phase and delayed (see below)
function proposePriceOracle(address newOracle) external onlyOwner;
function commitPriceOracle() external onlyOwner;
function cancelPriceOracle() external onlyOwner;
address public pendingPriceOracle;
uint48 public pendingPriceOracleEta;
```

> **Storage is packed.** `treasury` and both fees share **one slot** (slot 7 since `agentModule` was added: 160 + 48 + 48 bits), so `_applyFee`'s single `feeConfig()` call costs one storage read. That is why the fees are stored in **gwei** as `uint48`s: two `uint128`s would fill a slot by themselves and push `treasury` into a second one, making every session-key transaction read two slots. Everything outside the contract stays in wei — the setters take wei and refuse anything that is not a whole gwei (`SHRegistry_FeeNotWholeGwei`, never silently rounded), and the getters return wei. A `uint48` of gwei holds up to ≈ 281,000 ETH; the constructor refuses a ceiling past `MAX_STORABLE_FEE`. `pendingPriceOracleEta` is a `uint48` sharing a slot with `pendingPriceOracle`. `agentId` stays `uint256` because an external registry mints it.

> **`setPriceOracle` was replaced by a two-phase timelock.** The oracle governs every wallet's spending cap, so repointing it is the single highest-impact action the operator key can take. `proposePriceOracle` records the candidate and starts a 2-day delay; `commitPriceOracle` applies it once the ETA passes; `cancelPriceOracle` withdraws it. `proposePriceOracle` also **rejects an oracle that cannot price native (`address(0)`)**, and **the constructor makes the same check on the initial oracle**. The module meters the native balance delta on every wallet that counts native, so an oracle without that feed would revert every native-moving execution on all of them. The check also catches a mistyped or wrong-network address, which has no `isPriced()` to call. See [THREAT_MODEL.md](../THREAT_MODEL.md) §3.8.

> **Why `spendingLimitModule` is not a constructor argument.** `SpendingLimitModule`'s own constructor reads `priceOracle()` off the registry — that check is what catches a mis-wired deployment (passing the oracle address where the registry belongs). So the registry must exist before the module can be deployed. Taking the module as a registry constructor argument would make the two mutually undeployable. The deploy script constructs the module against the live registry and then calls `setSpendingLimitModule`; `SHFactory.deployWallet` reverts with `SHFactory_SpendingLimitModuleNotSet` in the window between.

### Protocol fee

The fee is a **flat amount of native token, in wei**. `SessionHandler._applyFee` reads it off the registry and sends it — the fee path makes no oracle call:

```
SessionHandler._applyFee(withKeeperFee)
  └─ SHRegistry.feeConfig()     // (treasury, protocolFee, keeperFee) — one call, one storage slot
       → sends protocolFee (+ keeperFee on the executor path) to treasury in ONE transfer
       → emits ProtocolFeePaid(fee, keeperFee)
```

It used to be a USD figure converted at execution time. That cost every session execution a Chainlink read (~18k gas, plus the sequencer check on an L2), and a stale ETH/USD feed halted *all* agent activity. A stored wei amount avoids both.

**Bounds are per chain.** The same wei is a different dollar amount on each chain (ETH vs BNB), so `MIN_PROTOCOL_FEE` and `MAX_PROTOCOL_FEE` are constructor arguments, not constants. `DeploySHProtocol` holds the dollar targets — **$0.005 floor, $0.10 ceiling, $0.015 starting fee** — and converts each to wei through the chain's native Chainlink feed at deploy time (`_usdToNative`), then to whole gwei (`_feeBounds`: the floor rounds up, the ceiling down, the starting value down and then up to the floor if needed). It reads the feed directly, skipping the oracle's staleness check, so a stale testnet feed cannot block a deploy. The constructor rejects a zero floor, a floor above the ceiling, a ceiling past `MAX_STORABLE_FEE`, a bound that is not whole gwei, and a starting fee outside the bounds.

What follows from a flat fee:

- **Its dollar value moves with the native price.** The operator re-sets it with `setProtocolFee` when it drifts too far.
- **The bounds drift too, and are permanent.** They are immutable, so if ETH doubles the range becomes ≈ $0.01–$0.20; if it halves, ≈ $0.0025–$0.05. The 20x gap is the room for that: the starting fee sits near the floor, so the price can triple before the floor forces the fee up, and fall ~6x before the ceiling forces it down. New bounds mean a new registry, which means redeploying the protocol.
- **The ceiling does not consult the oracle, on purpose.** The operator controls the oracle's feeds with no delay, so a ceiling checked in dollars could be lifted by first faking a low native price. A wei ceiling holds regardless.

Three more properties worth knowing:

- **Only session-key and executor executions pay.** `execute` charges the fee when `msg.sender != owner()`; owner-initiated calls pay nothing. `executeFromExecutor` always charges, and adds the keeper fee.
- **The fee is outside the spending cap.** `_applyFee` runs *before* `_execute`, and the hook's `preCheck`→`postCheck` window opens inside `_execute` — so the fee never counts against the account's USD cap. This is deliberate: the cap meters what the *user* spends, and a protocol fee is not the user's spend. It does mean the fee is native outflow the cap cannot see, bounded per execution by `MAX_PROTOCOL_FEE` (plus `MAX_KEEPER_FEE` on the executor path) rather than by the cap.
- **A stale ETH/USD feed does not block the fee.** An ERC-20-only session execution never touches the native feed. See [THREAT_MODEL.md](../THREAT_MODEL.md) §3.7.

`SHTreasury` keeps no running total, because one would cost every session-key execution about 5,000 gas. Each wallet's `ProtocolFeePaid(fee, keeperFee)` event records what it paid, in **wei** — the only on-chain record of fees, since the treasury's `receive()` logs nothing. It no longer carries the treasury address (always the registry's; dropping the indexed topic saves ~375 gas). Those amounts span different native prices, so summing them does not give a dollar total.

### Keeper fee

A recurring payment is sent by the protocol's **backend** (the keeper), which pays the transaction's gas itself — unlike a session-key UserOp, which pays from the wallet's EntryPoint deposit. The keeper fee pays that back: `executeFromExecutor` calls `_applyFee(true)`, which adds `keeperFee` to the protocol fee in the same transfer to the treasury. The operator then funds the keeper's address from the treasury. `execute` calls `_applyFee(false)` and never pays it.

- **Per-chain dollars.** Gas costs very different amounts per chain, so the keeper fee's starting value and bounds are `NetworkConfig` fields (`keeperFeeUsd`, `minKeeperFeeUsd`, `maxKeeperFeeUsd`), converted to whole gwei like the protocol fee.
- **Sized from measured gas.** One `run` measured **~103k gas** (a later payment) to **~141k** (the payee's first, which creates their token balance) on local mocks, and ~139k for a native payment, as whole transactions. Real tokens and Chainlink proxies cost more (≈ +20k), each extra watched token adds ≈ 7k, and the L2s add a sequencer read and an L1 data fee — so the HelperConfig figures assume **~200k gas**. See `RecurringPaymentTest.test_gas_*`.
- **Every executor pays it**: `RecurringPaymentModule`, and `AgentModule` for each agent trade. Right when Mitfah's backend sends the trade and pays its gas; when an agent sends its own trade, the wallet still pays it (accepted, THREAT_MODEL §3.17). `AgentModule` charges it, with the protocol fee, to the agent's own limit.
- Like the protocol fee, it is **outside the spending cap** and bounded only by `MAX_KEEPER_FEE`.

> `router` / `setUniswapRouter` were **removed**: which DEX a wallet trades on is a wallet-level choice, not protocol configuration. A wallet trusts only the spenders its deployer passes to `deployWallet` (the app passes the chain's exchange contracts, `constants.get_default_spenders`), and the owner can grant more via `addTrustedSpender`.

> The former `callValueInterpreter` parameter and `SHValueInterpreter` contract were **removed**: the new module meters spending by diffing watched-token balances, so it needs no per-call calldata decoder.

---

## `SHTreasury.sol`

`SHTreasury` is the protocol's **single admin root** and fee sink. It owns the `SHRegistry`, the `SHOracle`, and the `SHFactory`, and is the only address that can drive their owner-only functions — the operator EOA owns the treasury and reaches everything else through its pass-through setters.

```
operator EOA → SHTreasury → { SHRegistry, SHOracle, SHFactory }
```

It is deployed **first**, with no constructor dependencies, so its address is available as the `owner` argument to all three. The one back-reference it needs — the registry it administers — is wired in afterwards by `setRegistry`, which is **write-once**: a second call reverts, so `REGISTRY` carries the same guarantee an `immutable` would.

The oracle and factory pass-throughs take their target's address as an argument rather than reading a stored one. For the oracle that is what lets a **replacement oracle be seeded with feeds while it sits pending in the timelock**, before it becomes live; for the factory it means the protocol can run more than one factory without redeploying the treasury.

```solidity
constructor();  // no dependencies: deployed first, owns everything deployed after it

// One-time wiring, called by the deploy script right after SHRegistry is constructed
function setRegistry(address registry) external onlyOwner;   // reverts if already set

// Balance management
receive() external payable;                                                   // accepts fees, records nothing
function withdraw(address recipient, uint256 amount) external onlyOwner nonReentrant;
function withdrawAll(address recipient) external onlyOwner nonReentrant;

// Registry pass-through admin (all `registrySet`-guarded)
function setProtocolFee(uint256 newFee) external onlyOwner;
function setKeeperFee(uint256 newFee) external onlyOwner;
function setTreasury(address newTreasury) external onlyOwner;
function setAgentId(uint256 newId) external onlyOwner;
function proposePriceOracle(address newOracle) external onlyOwner;
function commitPriceOracle() external onlyOwner;
function cancelPriceOracle() external onlyOwner;

// Oracle pass-through admin (target passed in). Every feed change waits SHOracle.FEED_TIMELOCK (2 days).
function proposeOracleFeed(address oracle, address token, address priceFeed, uint256 heartbeat) external onlyOwner;
function proposeOracleFeedRemoval(address oracle, address token) external onlyOwner;
function commitOracleFeed(address oracle, address token) external onlyOwner;
function cancelOracleFeed(address oracle, address token) external onlyOwner;

// App checks (registry pass-through). Adding or replacing waits SHRegistry.APP_CHECK_TIMELOCK (2 days);
// removing is instant (it only takes ability away).
function proposeAppCheck(address app, address check) external onlyOwner;
function commitAppCheck(address app) external onlyOwner;
function cancelAppCheck(address app) external onlyOwner;
function removeAppCheck(address app) external onlyOwner;

// Factory pass-through admin (target passed in, so multiple factories are supported)
function pauseFactory(address factory) external onlyOwner;
function unpauseFactory(address factory) external onlyOwner;

// Registry-held wallet config (the modules live on the registry, so one call covers every factory)
function setSpendingLimitModule(address newModule) external onlyOwner;
function setRecurringPaymentModule(address newModule) external onlyOwner;
function setAgentModule(address newModule) external onlyOwner;
function setFactory(address newFactory) external onlyOwner;

// One-way migration: hands registry + oracle + factory to a successor owner or multi-sig.
// Does NOT move this contract's own ownership, nor redirect the fee stream — call setTreasury
// first if that is the intent, since afterwards this contract can no longer reach the registry.
function transferProtocol(address newOwner) external onlyOwner;

address public REGISTRY;          // write-once via setRegistry
```

---

## `SHFactory.sol`

`SHFactory` is the user-facing entry point for deploying new `SessionHandler` wallets. It deploys a single `SessionHandler` implementation in its constructor, then `deployWallet(...)` creates each user's wallet as an **EIP-1167 minimal-proxy clone** and calls `initialize()` on it — which installs the configured `RecurringPaymentModule` as an **executor** and `SpendingLimitModule` as a **hook**, seeds the wallet's spending-cap configuration, and applies the two owner-only grants (session key, trusted spenders) that used to need a follow-up transaction each. ETH sent with the call is forwarded to the new wallet as the initial gas prefund.

**One transaction is enough.** `deployWallet` takes the per-wallet spending-cap config *and* the first passkey (`sessionKeyX`, `sessionKeyY`, `sessionKeyValidUntil`) + `trustedSpenders`, so a wallet is fully usable the moment it is deployed. `(0, 0)` as the key means an owner-only wallet. The seeded key goes through the same internal grant as `addSession`, so a deploy cannot seed a key on terms an owner transaction would refuse. Previously onboarding was three owner-signed transactions (`deployWallet`, then `addSession`, then `addTrustedSpender`); those two functions remain for later re-grants but are no longer part of the deploy path. They cannot be batched through `execute` — see the two guards described under `SessionHandler` — which is why seeding them in `initialize` is the only way to get to one signature.

**Wallets are deployed with CREATE2.** The salt is `keccak256(abi.encode(msg.sender, deployCount[msg.sender]))`, so `predictWalletAddress(owner)` returns the address that owner's *next* deploy will produce, before it exists. Binding the owner into the salt is what makes that prediction safe to publish: no other caller can reach the same salt, so a predicted address cannot be squatted.

The nonce is a **per-owner** counter held by the factory, not a caller-supplied value and not a global one. A global counter would make a predicted address depend on how many wallets *everyone else* had deployed, so any other user's deploy landing first would move it — which would defeat the point: the app uses the predicted address to find the deploy even when the user's wallet replaced the transaction (sped it up, say) and the original hash never mines. Two consequences worth knowing:

- A prediction is only valid for **one factory instance** — the deployer is baked into a CREATE2 address, so redeploying `SHFactory` invalidates every previously shown address.
- Calling `deployWallet` again simply consumes the next nonce and yields another wallet — one EOA may own several. A salt can never repeat, so there is no collision case and no "already deployed" error. **Enforcing one-wallet-per-user is the caller's job**: read `deployCount(owner)` and refuse when it is non-zero, which is a free view rather than a reverted transaction the user pays for. That check means "does this USER have a wallet" only when the user is `msg.sender` — i.e. when they sign their own deploy. A caller deploying on users' behalf from one shared EOA (as `app/deploy_wallet.py` does) sees a counter covering all its users and must track per-user ownership itself.

The factory stores **no** protocol addresses of its own. EntryPoint, both ERC-8004 registries, and both modules are read off the registry at `deployWallet` time, so correcting any of them is a single registry call rather than a factory redeploy. `SHFactory` itself owns only the clone implementation and the wallet index.

```solidity
constructor(
    address owner,      // SHTreasury — the admin root
    address _registry   // SHRegistry; every other address is read from it at deploy-wallet time
);

/// Deploys a new SessionHandler owned by msg.sender; forwards msg.value as ETH prefund.
/// dailyLimitUsd (18 decimals, >= 0), windowDuration (seconds, 1 to 2^24 - 1), watchedTokens (each
/// must already be priced by the oracle) and nativeMetered (whether ETH/BNB counts) seed the
/// hook's onInstall config.
/// The passkey (sessionKeyX, sessionKeyY) is authorized until sessionKeyValidUntil ((0, 0) =
/// owner-only wallet, deadline ignored); every entry in trustedSpenders is granted the
/// unpriced-approval exemption (may be empty). Both are applied inside initialize(), so no
/// follow-up owner transaction is needed.
/// Salted with (msg.sender, deployCount[msg.sender]), which is consumed here — so calling twice
/// yields two distinct wallets rather than reverting.
/// Reverts with SHFactory_SpendingLimitModuleNotSet / SHFactory_RecurringPaymentModuleNotSet
/// if the registry has either module unset.
function deployWallet(
    int256 dailyLimitUsd,
    uint256 windowDuration,
    address[] calldata watchedTokens,
    bool nativeMetered,
    bytes32 sessionKeyX,
    bytes32 sessionKeyY,
    uint48 sessionKeyValidUntil,
    address[] calldata trustedSpenders
) external payable whenNotPaused returns (address);

/// The address `owner`'s NEXT deployWallet call will produce, before it is deployed.
function predictWalletAddress(address owner) external view returns (address);

/// How many wallets `owner` has deployed. Doubles as their next salt nonce, and as a
/// "does this user already have a wallet?" check a front end can read before asking for a signature.
mapping(address owner => uint256) public deployCount;

function pause() external onlyOwner;      // via SHTreasury.pauseFactory
function unpause() external onlyOwner;    // via SHTreasury.unpauseFactory

SHRegistry public immutable REGISTRY;
address public immutable IMPLEMENTATION;
event WalletDeployed(address indexed walletAddress, address indexed owner);
```

> `setSpendingLimitModule` moved to `SHRegistry` — every factory reads the module from there, so one call now covers them all. There is no wallet counter or id list: nothing on chain needs one, and both can be rebuilt from `WalletDeployed` logs.

---

## `SHOracle.sol`

The `SHOracle` converts native-asset and ERC-20 token amounts into real-time USD values using **Chainlink** price feeds. It accounts for stablecoin depeg events (e.g. USDC at $0.87 during the March 2023 SVB crisis) by querying actual market prices rather than assuming a 1:1 peg.

The module calls it directly (there is no interpreter layer). `getPrice` returns the **signed 18-decimal USD value** of an amount, and `isPriced` gates whether a token may be watched or approved through the hook.

**Supported tokens:** registered per-token via parallel `(address token, address priceFeed, uint256 heartbeat)` arrays passed to the constructor — native ETH/BNB uses `address(0)`. Pairs whose `priceFeed` is `address(0)` are silently skipped, so the same array shapes can be reused across networks. Each token's `decimals`, and each feed's, are read once when the feed is set and cached, so `getPrice` never makes an external `decimals()` call at valuation time. The feed's decimals are read rather than assumed: Chainlink's USD feeds use 8, but a feed that uses another number is still priced correctly, and one finer than 18 decimals is refused (`PriceOracle_InvalidFeedDecimals`).

**Staleness protection:** each registered feed has its **own** heartbeat, set once at construction — volatile assets (ETH, LINK, BTC) update roughly hourly, stablecoins every 23–24 hours. `getPrice` reverts with `PriceOracle_StalePrice` if `block.timestamp - updatedAt > heartbeat` for that feed, and with `PriceOracle_InvalidPrice` on a non-positive answer. (The error names retain the `PriceOracle_` prefix from the contract's earlier name.)

**L2 sequencer gate:** on an L2 the heartbeat check alone is not enough — Chainlink publishes updates *through* the sequencer, so an outage freezes every feed at an answer whose `updatedAt` still looks recent. `_requireSequencerUp()` runs at the top of `_stalePriceCheck` (so it covers `getPrice` and every module valuation) and reverts with `PriceOracle_SequencerDown` while the uptime feed reads down or reports an uninitialised round, and `PriceOracle_SequencerGracePeriod` until the sequencer has been back for longer than `SEQUENCER_GRACE_PERIOD` (1 hour). `SEQUENCER_UPTIME_FEED` is **immutable**, comes from `HelperConfig.sequencerUptimeFeed`, and is `address(0)` on chains with no sequencer — which skips the check entirely. The constructor probes a non-zero feed once and rejects anything that cannot answer or that answers with something other than the 0/1 status flag (`PriceOracle_InvalidSequencerFeed`), catching a price feed passed in by mistake. `isPriced` is a plain storage read and stays answerable through an outage. Blast radius and rationale: [THREAT_MODEL.md](../THREAT_MODEL.md) §3.14.

```solidity
constructor(
    address owner,
    address sequencerUptimeFeed,   // address(0) on a chain with no sequencer
    address[] memory tokens,
    address[] memory priceFeeds,
    uint256[] memory heartbeats
);

/// True if `token` has a registered feed (safe to watch/approve/price). Never gated by the sequencer.
function isPriced(address token) external view returns (bool);

/// USD value of `amount` of `token`, 18 decimals, signed (int256 for arithmetic parity with the module).
function getPrice(address token, uint256 amount) external view returns (int256);

/// Feed admin (owner = SHTreasury). Nothing binds before FEED_TIMELOCK (2 days): prices decide what the cap
/// charges and what PayeeGuard's floors allow. A removal waits too (an unpriced token has no floor).
function proposeFeed(address token, address priceFeed, uint256 heartbeat) external onlyOwner;
function proposeFeedRemoval(address token) external onlyOwner;   // never the native feed
function commitFeed(address token) external onlyOwner;
function cancelFeed(address token) external onlyOwner;
function pendingFeeds(address token) external view returns (address feed, uint48 heartbeat, uint48 eta);
```

---

## `SessionHandler.sol`

The `SessionHandler` is an **ERC-7579 smart account** (extends OpenZeppelin's `AccountERC7579Hooked`, plus `OwnableUpgradeable` and `Pausable`), deployed behind an EIP-1167 clone by `SHFactory`. It owns three responsibilities the module doesn't: **validating its own UserOps**, **managing its passkey session keys**, and **guarding session-key executions** away from its own admin surface. `SpendingLimitModule` is installed as a **hook only**; the one validator every wallet has is `AgentModule`, asked only about ops neither the owner nor a passkey signed.

**Self-validation, with one route to a validator.** `SessionHandler._authenticate` (called from `validateUserOp`) authenticates the owner and the passkeys itself instead of delegating to `AccountERC7579._validateUserOp`. It tells the three kinds of signer apart by the signature's **length**:

- **Exactly 65 bytes — the owner.** Plain ECDSA over the EIP-191 envelope of the userOpHash (`toEthSignedMessageHash`, matching the Foundry helper and the user's browser wallet). Valid unconditionally, with no validity window. A 65-byte signature by anyone else fails: a session key is never an Ethereum address now.
- **256 bytes or more — a passkey.** `qx ‖ qy ‖ abi.encode(r, s, challengeIndex, typeIndex, authenticatorData, clientDataJSON)`: the passkey's public key, then a WebAuthn assertion as **six values in a row** (not `abi.encode` of the struct value, which adds a leading offset word and is refused). `_authenticatePasskey` hashes the key to its id, reads its deadline (one storage read; 0 = not held → fail), decodes the assertion, bounds-checks both JSON indices, and verifies it with **user verification required** (Face ID, fingerprint or PIN — not a bare touch) and the raw userOpHash as the challenge. 256 bytes is the smallest blob that can hold one (`_MIN_PASSKEY_SIGNATURE`).
- **Any other length — a validator module** (`_validateWithModule`): the one named by the top 20 bytes of the op's nonce key, if it is installed as a validator and the op's `callData` calls `executeUserOp` (`_opModule`). It is asked with a **staticcall**, given the `validateUserOp` call's own arguments re-addressed to `IERC7579Validator.validateUserOp` (so the op isn't re-encoded; the route is refused from any other entry point). Its answer is the validation data; its revert is bubbled up with the reason. AgentModule's signatures are 95 bytes. No owner or passkey op ever reaches a module, whatever its nonce key says.

Every bad signature — malformed, wrong key, wrong op, truncated, garbage — returns `SIG_VALIDATION_FAILED` and **never reverts**: a revert in validation would fail the whole bundle, not just the op. Origin and rpId inside the assertion are **not** checked on chain (a contract cannot know which site the user meant); the browser enforces them and the server checks them before relaying. What binds a signature to one op on chain is the key, user verification and the challenge — the op hash, which covers the wallet, chain, EntryPoint, nonce and calldata. Raw-signature validation stays at `AccountERC7579`'s default of `false`, so exactly one function decides who may sign.

**A passkey op carries a deadline of its own.** The top 48 bits of its nonce (the top of the EntryPoint nonce *key*) are the op's deadline: `nonce = (opDeadline << 208) | (lane << 64) | sequence`. The validity window returned to the EntryPoint is the **earlier** of the key's deadline and the op's, so an expired one fails `AA22 expired or not due`, not `AA24`. A zero op deadline is refused outright — zero would read as "valid forever". Why: the app sends each passkey op on a fresh random 144-bit lane so confirmations never queue behind each other, but nothing else ever uses that lane, so without its own deadline a signed-but-abandoned op would stay sendable until the key expired (up to 90 days). The nonce is inside the signed hash, so a relayer cannot move the deadline. Owner ops ignore it and use nonce key 0. Because an expired op and an expired key both say `AA22`, the app tells them apart by asking whether the key is still active.

**The P-256 precompile is required.** Signatures are checked with `WebAuthnNative` (OpenZeppelin's WebAuthn with `P256.verifyNative`, no Solidity fallback, ~2 KB smaller), which calls the RIP-7212 / EIP-7951 precompile at `0x100`. It is live on every chain served (Ethereum, Arbitrum, Base, BSC — checked 2026-10-20). Locally that means `evm_version = "osaka"` in foundry.toml and `anvil --hardfork osaka` (the Makefile's `anvil` and fork targets pass it); without it every passkey op fails.

Not delegating is deliberate. The base asks a validator module first whenever the nonce key names an installed one; here the owner and the passkeys are judged with no module hop, and a module only for an op neither could have signed. Deciding here is also what lets an expired key fail as `AA22 expired or not due` instead of `AA24 signature error`. `isValidSignature` (ERC-1271) still consults installed validators, as OpenZeppelin's account does; AgentModule refuses every message, so the wallet signs none.

**`executeUserOp` — how a validator's op runs.** ERC-4337's `IAccountExecute`: the EntryPoint calls `executeUserOp(userOp, userOpHash)` instead of forwarding the callData whenever that callData starts with its selector. It is `onlyEntryPoint`, runs only ops `_opModule` routes to a module (the same test validation used, so the module whose hooks run is the one that approved the op; an owner or passkey op calling it reverts `SessionHandler_NotModuleOp`), and only when the rest of the callData is a call to the wallet's own `execute`:

```
EntryPoint → executeUserOp(userOp, userOpHash)
    ├─ module.preExecute(userOp, userOpHash)        (IUserOpHooks; AgentModule re-checks the agent, notes the remaining budget)
    ├─ address(this).execute(mode, calls)            SELF-CALL: _guardSessionExecution, _applyFee(false), the hook around the calls
    └─ module.postExecute(userOp, userOpHash)       (AgentModule charges the agent)
```

The self-call is **load-bearing**: the hook reads the calls it meters out of `execute` calldata, so calls run any other way would skip its approval tracking, own-unit caps and admin-target check. Through it, a module op gets exactly a passkey op's treatment, protocol fee included and no keeper fee (the wallet paid its own gas). Validator ops are ops the owner did not sign, so they are charged to `sessionGasBudget` like a passkey's.

**Admin-surface guard.** For any non-owner execution (a session-key UserOp through the EntryPoint, or a self-call), `execute` runs `_guardSessionExecution`, which reverts if any single/batch sub-call targets `address(this)` or `address(SH_MODULE)`, and rejects `delegatecall` outright. This is what stops a session key from calling the module's cap setters, self-calling `uninstallModule` to delete the cap, minting more session keys, or delegatecalling arbitrary code. Owner-initiated calls skip the guard.

**Deliberate deviation from stock `AccountERC7579Hooked`,** which makes all three `onlyEntryPointOrSelf`. They are *not* reopened the same way:

- `execute` also accepts the owner directly — still reachable through the EntryPoint (that is how session keys act at all), with `_guardSessionExecution` restraining non-owner callers.
- `installModule` / `uninstallModule` become plain `onlyOwner`, which is strictly **tighter** than stock: an owner-signed UserOp arrives as `msg.sender == EntryPoint`, not the owner, so these are reachable only by a direct owner call and by **no UserOp at all**. That closes the path where a session key submits a UserOp aimed straight at `installModule` — bypassing `execute`'s guard entirely, since `execute` is never involved — to install a malicious validator or executor and escape the cap.

Session-key management and cap configuration are plain `onlyOwner`. Note that the module also refuses `execute`-routed calls to its own setters for *every* caller, owner included — see `SpendingLimitModule` below.

**Up to five passkeys, one per device, and each expires.** `addSession(qx, qy, validUntil)` authorizes a passkey; a key the wallet already holds is **renewed** instead (its deadline overwritten, `SessionAdded` emitted, no new slot). `removeSession(keyId)` revokes one and reverts `SessionHandler_UnknownSessionKey` for a key the wallet does not hold, so a stale id never reads as a successful revoke; `removeAllSessions()` is the panic button (a no-op when empty). `getSessions()` lists every held key with its deadline — the answer to "which keys does this wallet trust?" — and `isSessionActive(keyId)` compares with `<=` to match the EntryPoint exactly (an op is still valid in the second that equals the deadline). Keys are identified by `sessionKeyId(qx, qy) = keccak256(abi.encode(qx, qy))`, derived rather than assigned, so the app, the contract and an indexer agree without asking each other.

`addSession` refuses a pair that is not a point on P-256 (`SessionHandler_InvalidSessionKey` — it would authorize nobody while using a slot), a deadline that has passed — **including zero, which the EntryPoint would read as "valid forever"** — and anything beyond `MAX_SESSION_TTL` (90 days). A **new** key when five are held first sweeps every expired key (emitting `SessionRemoved` for each — they already stopped working); only if all five are still live does it revert `SessionHandler_TooManySessionKeys(5)`. Five keys spend no more than one: the cap, the gas budget and the allowlist all belong to the wallet.

The signature carries the public key, so validation makes **one** storage read (the deadline in `sessionValidUntil`) where storing the key would take three. That is also what lets `app/bundler.py` simulate a quote for a user who has not signed yet: it plants a throwaway key's deadline with a single storage override (see the slot note below).

Within its window a key is still a bare signer with no per-key target/selector scope: it can drive any external call, bounded by the spending cap, the guard, and the per-UserOp gas ceiling — optionally narrowed to a set of contracts via the target allowlist (off by default; an address with no code always passes). The first passkey (with its deadline) and the chain's exchange contracts as trusted spenders are normally seeded at deploy time by `SHFactory.deployWallet`; `addSession` / `addTrustedSpender` add more devices and re-grant afterwards.

**Nothing is trusted unless the deployer says so.** `initialize` grants exactly the `trustedSpenders` the caller passed — an empty array leaves the list empty, and every approval of a token the oracle can't price (a BSC LP token for `removeLiquidity`, a token added by address for a swap or a deposit) then fails until the owner grants a spender. This is *not* a return to the old deploy-time auto-trust of `SHRegistry.router()`: that was protocol config choosing the venue, whereas this list comes from the deploying caller. Which venue a wallet trades on stays the owner's choice.

```solidity
/// Passed ONCE, when SHFactory's constructor creates the implementation. The four become
/// immutables of the implementation's code, which every clone runs, so no wallet stores them.
struct ProtocolAddresses {
    address entryPoint;
    address reputationRegistry;
    address identityRegistry;
    address registry;
}
constructor(ProtocolAddresses memory protocol);

/// Called once by SHFactory on each freshly cloned wallet, for the per-wallet settings.
/// Installs the recurring-payment module as an EXECUTOR (no data), then the spending module as a
/// HOOK with abi.encode(dailyLimitUsd, windowDuration, watchedTokens, nativeMetered), authorizes the
/// passkey (sessionKeyX, sessionKeyY) unless both are zero, and grants each cfg.trustedSpenders entry.
/// The executor goes FIRST: OpenZeppelin wraps every module install in the hook, so installing it
/// after the hook would run a pointless preCheck/postCheck on every deploy.
struct InitConfig {
    address owner;
    address spendingLimitModule;
    address recurringPaymentModule;
    int256 dailyLimitUsd;
    uint256 windowDuration;
    address[] watchedTokens;
    bool nativeMetered;          // whether ETH/BNB counts toward the limit
    bytes32 sessionKeyX;         // (0, 0) = owner-only wallet (not an error, unlike addSession)
    bytes32 sessionKeyY;
    uint48 sessionKeyValidUntil; // the key's deadline; same rules as addSession (ignored for (0, 0))
    address[] trustedSpenders;   // may be empty; granted AFTER the hook is installed
}

function initialize(InitConfig calldata cfg) external;

// Execution + module admin (owner escape hatch)
function execute(bytes32 mode, bytes calldata executionCalldata) public payable override whenNotPaused;   // EntryPoint, the account itself or the owner
function executeFromExecutor(bytes32 mode, bytes calldata executionCalldata) public payable override;    // installed executors only: guard + protocol fee + keeper fee, then the hooked _execute
function installModule(uint256 moduleTypeId, address module, bytes calldata initData) public override onlyOwner;
function uninstallModule(uint256 moduleTypeId, address module, bytes calldata deInitData) public override onlyOwner;

// Passkey session keys (owner-only to change). sessionValidUntil MUST stay on storage slot 9.
uint256 public constant MAX_SESSION_KEYS = 5;
uint48  public constant MAX_SESSION_TTL = 90 days;
mapping(bytes32 keyId => uint48 validUntil) public sessionValidUntil;   // 0 = not held
function sessionKeyId(bytes32 qx, bytes32 qy) public pure returns (bytes32);   // keccak256(abi.encode(qx, qy))
function addSession(bytes32 qx, bytes32 qy, uint48 validUntil) external onlyOwner;   // add OR renew
function removeSession(bytes32 keyId) external onlyOwner;    // unknown id reverts
function removeAllSessions() external onlyOwner;             // empty: no-op
function getSessions() external view returns (bytes32[] memory keyIds, uint48[] memory validUntils);
function isSessionActive(bytes32 keyId) public view returns (bool);   // held && now <= validUntil

// Spending-cap config passthroughs (owner-only; call the module as this account)
function setDailyLimit(int256 dailyLimitUsd) external onlyOwner;
function setWindowDuration(uint256 windowDuration) external onlyOwner;
/// Switches native metering, and moves wrappedNative (WETH/WBNB) on or off the watched list with
/// it in the same call; address(0) moves the flag only.
function setNativeMetering(bool metered, address wrappedNative) external onlyOwner;
function addWatchedToken(address token) external onlyOwner;
function removeWatchedToken(address token) external onlyOwner;
function setTokenCap(address token, uint256 limit) external onlyOwner;   // in the token's own units
function removeTokenCap(address token) external onlyOwner;
function addTrustedSpender(address spender) external onlyOwner;
function removeTrustedSpender(address spender) external onlyOwner;

// Cap views
function getConfig() external view returns (SpendingLimitModule.Config memory);
function isWatched(address token) external view returns (bool);
function isTrustedSpender(address spender) external view returns (bool);
function getTokenCap(address token) external view returns (bool capped, uint256 limit, uint256 remaining);
function getRemainingBudget() external view returns (int256);          // remaining USD this window
function getUsdValue(address token, uint256 amount) public view returns (int256);

// Owner-only account functions
function pause() external onlyOwner;
function unpause() external onlyOwner;
function withdraw(address token, uint256 amount, address to) external onlyOwner;   // ERC20 or ETH; bypasses the hook

// ERC-8004
function getAgentId() public view returns (uint256);
function getAgentIdentity() public view returns (bool registered, uint256 agentId, string memory agentUri);
function getAgentReputation() public view returns (uint256 agentId, uint64 feedbackCount, int128 summaryValue, uint8 summaryValueDecimals);

// Gas ceilings (THREAT_MODEL 3.12) - the cap cannot see gas, these bound it. Defaults are set per
// chain when the factory builds the implementation (HelperConfig: maxOpGasCost, sessionGasBudget).
uint256 public immutable DEFAULT_MAX_OP_GAS_COST;
uint256 public immutable DEFAULT_SESSION_GAS_BUDGET;
uint256 public constant SESSION_GAS_WINDOW = 1 days;
uint80 public maxOpGasCost;                     // per UserOp; up to ~1.2M ETH
function setMaxOpGasCost(uint256 newMax) external onlyOwner;   // rejects 0 and > type(uint80).max
uint80 public sessionGasBudget;                 // per window, across every op the owner didn't sign
uint128 public sessionGasSpent;                 // ...spent so far this window
uint48 public sessionGasWindowStart;
function setSessionGasBudget(uint256 newBudget) external onlyOwner;   // rejects 0 and > type(uint80).max
function sessionGasRemaining() external view returns (uint256);

// Optional session-key target allowlist (THREAT_MODEL 3.13), off by default. Confines calls to
// listed CONTRACTS: while it is on, an address with no code passes whether listed or not.
bool public sessionAllowlistEnabled;
function enableAllowList(address[] calldata targets) external onlyOwner;  // add + turn on, one tx
function toggleAllowList(bool enabled) external onlyOwner;      // refuses to enable while empty
function addAllowedTarget(address target) external onlyOwner;
function addAllowedTargets(address[] calldata targets) external onlyOwner;
function removeAllowedTarget(address target) external onlyOwner;
function sessionTargetAllowlist(address target) external view returns (bool);  // listed?
function allowedTargetCount() external view returns (uint256);
function getAllowedTargets() external view returns (address[] memory);  // whole list, any order

event SessionAdded(bytes32 indexed keyId, bytes32 qx, bytes32 qy, uint48 validUntil);   // also on renewal
event SessionRemoved(bytes32 indexed keyId);
event MaxOpGasCostUpdated(uint256 oldMax, uint256 newMax);
event SessionGasBudgetUpdated(uint256 oldBudget, uint256 newBudget);
event SessionAllowlistToggled(bool enabled);
event AllowedTargetAdded(address indexed target);
event AllowedTargetRemoved(address indexed target);
```

> **Shared values are immutables; per-wallet values are packed storage.** `ENTRY_POINT`, `REPUTATION_REGISTRY`, `IDENTITY_REGISTRY` and `REGISTRY` are the same for every wallet a factory deploys, so they are immutables of the implementation (set via `ProtocolAddresses`) and cost nothing to read. `SH_MODULE` stays in each wallet's storage because the operator can change the registry's module, and each wallet keeps the one it was deployed with. The rest is packed: `sessionAllowlistEnabled` and `maxOpGasCost` (`uint80`) share a slot with the inherited `_hook` and `_paused`, and `SH_MODULE` has a slot to itself. A session-key UserOp therefore reads two of these slots instead of five (allowlist lookups aside). The allowlist is an OpenZeppelin `EnumerableSet` so `getAllowedTargets` can return it whole; it takes two slots, which puts the `sessionValidUntil` mapping in slot 9 — `app/bundler.py`'s `SESSION_VALID_UNTIL_SLOT` must match, since a key's deadline lives at `keccak256(abi.encode(keyId, uint256(9)))` and the bundler overrides exactly that word (`PasskeyValidationTest.test_slot9_isSessionValidUntil` pins it). The key list (`_sessionKeys`, an `EnumerableSet`) takes slots 10–11, and the gas budget's three fields (`sessionGasSpent`, `sessionGasWindowStart`, `sessionGasBudget`) share slot 12, after them, so slot 9 doesn't move. `forge inspect SessionHandler storageLayout` shows the assignment.

> **Two gas ceilings.** `maxOpGasCost` bounds one UserOp: `_authenticate` prices the op's own gas fields against it, and `validateUserOp` bounds the prefund transfer too. `sessionGasBudget` bounds all of them: every UserOp the owner did not sign is charged its prefund top-up (`missingAccountFunds`) against a budget that refills every `SESSION_GAS_WINDOW` (24 hours), and validation reverts with `SessionHandler_SessionGasBudgetExceeded(spent, budget)` past it. Without it, a stolen key running its own bundler could pack many ops, each just under the per-op ceiling, into one `handleOps` and collect all their gas. The charge is the top-up, not the op's maximum price, so an ordinary op uses up about what it really cost. That is only safe because `_authenticate` refuses any op whose `callData` calls `validateUserOp` itself (`SessionHandler_SelfValidationCall`), the one way a key could otherwise have the account pay a prefund outside validation. The window reads `block.timestamp` during validation, which ERC-7562 bans for public bundlers; the protocol bundles every op itself (`app/bundler.py`), so it holds.

> **The nonce key decides who may sign only on the validator route.** For an owner op it is 0; for a passkey op it carries only the op's deadline and a lane; for a validator's op its top 20 bytes name the module and the 4 bytes below them are a lane of the caller's choosing (the app picks a fresh one per op, so ops never queue). A signature's length, not the key, picks the route, so no nonce key can send an owner's or a passkey's op to a module.

---

## `SpendingLimitModule.sol`

`SpendingLimitModule` is an **ERC-7579 Hook (type 4 only — not a validator)** enforcing a global, USD-denominated spending cap per rolling window across every venue and every key on the installing account. One deployed instance serves every account; all state is keyed by `account` (`msg.sender` in the hook callbacks). It never authenticates anyone — authentication is the account's job.

**Config (per account):**

```solidity
struct Config {
    bool installed;              // gates every hook and setter
    bool nativeMetered;          // whether ETH/BNB balance changes count
    uint32 windowStart;          // timestamp anchoring the current window
    uint24 windowDuration;       // seconds, at most 2^24 - 1 (about 194 days)
    int88 spentInWindow;         // 18-decimal USD net value spent this window
    int88 dailyLimitUsd;         // 18-decimal USD cap per window
    uint8 cappedTokenCount;      // cappedTokens.length, so a wallet with no caps pays no extra read
    address[] watchedTokens;     // tokens whose value changes are metered (max 32)
    address[] trustedSpenders;   // spenders approvable even for unpriced tokens (max 16)
    address[] cappedTokens;      // tokens capped in their own units (max 8)
}
```

**How metering works (net value, not gross outflow):**

- **`preCheck`** rejects any execution that targets the module's own admin surface (see below), rolls the spending window if expired, collects any allowance-granting sub-calls — `approve` and legacy `increaseAllowance` / `increaseApproval` — (rejecting unlimited approvals, unpriced-to-untrusted approvals and mainnet SAND's other grant functions up front), and snapshots the raw balance of every watched token the execution could move: all of them, unless every call is a plain send (`transfer` on a watched token reads only that token; empty calldata to an address with no code reads none). It snapshots the native balance only when `nativeMetered` is on, and passes that decision to `postCheck` in `hookData`, so a switch mid-transaction cannot split the two halves. **No oracle calls happen here.**
- **`postCheck`** does two independent checks:
  1. **Net-value spend.** For each watched token whose balance *changed*, it prices the delta via `SHOracle` and sums the signed differences across the portfolio; any net USD **decrease** is added to `spentInWindow`, reverting `BudgetExceeded` if it crosses `dailyLimitUsd` and emitting `SpendMetered` once the spend stands. A token that didn't move is never priced (so a stale feed on an untouched token can't block a transaction), and a net increase adds nothing, never banks credit for a later transaction, and emits nothing.
  2. **No standing approvals.** Any allowance approved in this transaction must be consumed to **exactly 0** by the end of it, else revert `StandingApprovalNotAllowed`. This closes deferred-pull risk outright rather than size-bounding it.

Because spending *is* the drop in watched-portfolio USD, the module needs no per-venue swap decoder: a value-neutral swap nets ~0, and a bad-rate or sandwiched swap registers its lost value automatically. Both `execute` and `executeFromExecutor` calldata are decoded for approvals; delegatecalls are not decoded (no reliable selector), but the net-value cap still applies to them.

**Limits in a token's own units.** A token can be capped in its own units instead of counting in USD: `setTokenCap(token, limit)`, up to 8 per wallet. A token with no feed can only be capped this way; one with a feed can be either, never both, and `setTokenCap` and `addWatchedToken` each revert `TokenAlreadyMetered` on a token on the other's list. `postCheck` adds the token's balance drop to its spend and reverts `TokenCapExceeded` past the limit, without reading the oracle. It nets only within that token, so selling one uses its limit however fair the rate, and buying one with a metered token charges the USD cap the full amount paid. Each cap is one storage slot (`limit`, `spent`, `windowStart`, `position`); `spent` only counts while `windowStart` matches the account's, so every cap rolls with the window with no loop. A capped token may be anyone's contract, so calls aimed at it may only be `transfer` or a recorded grant (`CallNotAllowedOnCappedToken`), and a failed `balanceOf` reverts the execution until the owner calls `removeTokenCap`, which never calls the token. See THREAT_MODEL §3.15.

**Trusted-spender exemption (unpriced approvals).** An `approve` on a token the oracle can't price normally reverts (`TokenNotPriced`) — *unless* the spender is on the account's `trustedSpenders` list. This is the escape hatch that lets a V2 **LP token** (which has no Chainlink feed) be approved to the router for `removeLiquidity` on BSC, and a token the user added by address be approved to the exchange for a swap or a Uniswap V3 deposit. The no-standing-approval and no-unlimited-approval rules still apply in full to trusted spenders — the exemption only skips the price check. The list starts empty unless the deployer seeds one: `SHFactory.deployWallet`'s `trustedSpenders` argument grants them inside `initialize` (this is how the app grants the chain's exchange contracts, from `constants.get_default_spenders`: SwapRouter02 and the position manager on the Uniswap V3 chains, PancakeSwap's router on BSC). `addTrustedSpender` remains for granting one later.

**Self-guard on the module's admin surface.** `preCheck` reverts `AdminExecution` if any sub-call of the execution targets the module itself with one of its own selectors (the nine config setters, `onInstall`, `onUninstall`). It runs *first*, before approvals are collected, so a refused transaction pays almost nothing. It is defence-in-depth that holds even on a host account with no guard of its own — and it deliberately blocks the **owner's** `execute` path too, because by the time `preCheck` runs `msg.sender` is the account whether an owner or a session key drove it, leaving no way to tell them apart. Nothing legitimate is lost: the owner reaches the nine setters through `SessionHandler`'s `onlyOwner` passthroughs (a direct call, so no hook runs), and `onInstall`/`onUninstall` through `installModule`/`uninstallModule`, whose outer calldata is not an `execute` selector and so never reaches the check.

**Where the oracle comes from.** The module stores an immutable `REGISTRY` but resolves the oracle itself from `SHRegistry.priceOracle()` on every valuation, so an oracle bug can be fixed for every already-deployed wallet at once without redeploying the module or migrating any wallet. The trade-off is a trust assumption: the registry owner (`SHTreasury` → the protocol operator) can repoint every wallet's oracle in one transaction — see [THREAT_MODEL.md](../THREAT_MODEL.md) §3.8. Resolution is deliberately per-use rather than cached in a local, so a path that never prices anything (a trusted-spender approval, a watched-token add of `address(0)`) makes no registry read at all; re-reading is cheap after the first, as the registry account and slot stay warm for the rest of the transaction.

```solidity
constructor(address registry);  // SHRegistry address — NOT the oracle; reverts OracleNotSet if it reports none

// IERC7579Module
function onInstall(bytes calldata data) external;    // decodes (int256 dailyLimitUsd, uint256 windowDuration, address[] watchedTokens, bool nativeMetered)
function onUninstall(bytes calldata) external;        // wipes all per-account state
function isModuleType(uint256 moduleTypeId) external pure returns (bool);   // true only for MODULE_TYPE_HOOK (4)

// Hook callbacks (called by the account around _execute)
function preCheck(address, uint256, bytes calldata msgData) external returns (bytes memory hookData);
function postCheck(bytes calldata hookData) external;

// Config setters (onlyInstalled — the account calls these as itself, via SessionHandler passthroughs)
function setDailyLimit(int256 dailyLimitUsd) external;   // 0 <= limit <= type(int88).max
function setWindowDuration(uint256 windowDuration) external;   // 1 <= duration <= type(uint24).max
function setNativeMetering(bool metered) external;              // flag only; the wallet moves WETH/WBNB with it
function addWatchedToken(address token) external;
function removeWatchedToken(address token) external;
function setTokenCap(address token, uint256 limit) external;   // 0 <= limit <= type(uint104).max
function removeTokenCap(address token) external;               // never calls the token
function addTrustedSpender(address spender) external;
function removeTrustedSpender(address spender) external;

// Per-account state. Field order follows storage packing: the first seven fields share ONE slot.
struct Config {
    bool installed;
    bool nativeMetered;
    uint32 windowStart;
    uint24 windowDuration;
    int88 spentInWindow;       // 18-decimal USD, running total for the current window
    int88 dailyLimitUsd;       // 18-decimal USD
    uint8 cappedTokenCount;    // cappedTokens.length
    address[] watchedTokens;
    address[] trustedSpenders;
    address[] cappedTokens;
}

// Views (keyed by account)
function getConfig(address account) external view returns (Config memory);   // read by field NAME
function isWatched(address account, address token) external view returns (bool);
function isTrustedSpender(address account, address spender) external view returns (bool);
function getRemainingBudget(address account) external view returns (int256);
function getTokenCap(address account, address token)
    external view returns (bool capped, uint256 limit, uint256 remaining);   // in the token's own units

address public immutable REGISTRY;   // oracle is read from REGISTRY.priceOracle() per valuation

event ConfigUpdated(address indexed account);
event WatchedTokenAdded(address indexed account, address indexed token);
event WatchedTokenRemoved(address indexed account, address indexed token);
event TrustedSpenderAdded(address indexed account, address indexed spender);
event TrustedSpenderRemoved(address indexed account, address indexed spender);
/// netOutflowUsd = USD that left the metered portfolio this tx; spentInWindow = running total after.
/// Emitted only when a transaction actually consumed budget (net outflow > 0).
event SpendMetered(address indexed account, int256 netOutflowUsd, int256 spentInWindow);
```

**Errors:** `AlreadyInstalled`, `NotInstalled`, `InvalidDailyLimit`, `InvalidWindowDuration`, `TokenNotPriced(token)`, `TooManyWatchedTokens`, `TooManyTrustedSpenders`, `InvalidTrustedSpender`, `TooManyCappedTokens`, `InvalidTokenCap`, `TokenAlreadyMetered(token)`, `CallNotAllowedOnCappedToken(token, selector)`, `TokenCapExceeded(token, spent, limit)`, `UnlimitedApprovalRejected`, `UnsupportedApprovalCall(target, selector)`, `StandingApprovalNotAllowed(token, spender, residual)`, `BudgetExceeded(spentUsd, dailyLimitUsd)`, `AdminExecution`, `OracleNotSet` — each prefixed `SpendingLimitModule_`.

**The limit and the running total share a slot with the install flag and the window.** `preCheck` already reads that slot, so `postCheck` reads both cheaply, a window reset writes the new start and the zeroed total in one store, and writing the total never pays the 20,000-gas cost of filling an empty slot. Both money values are `int88` (about $154.7 million at 18 decimals); the stored total can never be cut off because `setDailyLimit` keeps the limit inside `int88` and `postCheck` checks the new total against the limit before storing it. `windowStart` is `uint32`, so timestamps run until the year 2106, after which every transaction would start a new window. `windowDuration` is `uint24`, a window of up to about 194 days, which made room for the `nativeMetered` byte; `onInstall` and `setWindowDuration` refuse a longer one. The per-transaction outflow is still computed at full `int256`. Because the field order follows the packing, read `getConfig()` by field name, never by position — the Python app uses `contracts.read_spending_config`.

**Native value is metered when the wallet chooses it.** `nativeMetered` is set at deploy (`deployWallet`'s `nativeMetered`) and switched later by the owner (`SessionHandler.setNativeMetering`). When it is on, preCheck snapshots the account's native balance and postCheck prices its net change through the `address(0)` sentinel feed, alongside the watched tokens — so a native send, and the native leg of a swap (e.g. `swapExactETHForTokens`), count against the cap. When it is off, the native balance is left out **both ways**: a native send is not charged, and native coming in offsets nothing, so selling a counted token for ETH charges its full value (otherwise token → ETH → out would move a counted token free). Nothing reads the native feed then, so a stale ETH/USD feed blocks nothing. Gas is excluded either way: the ERC-4337 prefund leaves the account before preCheck and the refund settles to the EntryPoint deposit after postCheck, never touching the metered delta.

**WETH/WBNB follow the switch.** Wrapping is a free conversion, so a counted WETH beside uncounted ETH would only be bypassed by unwrapping first, and an uncounted WETH beside counted ETH would be the bypass. `SessionHandler.setNativeMetering(metered, wrappedNative)` moves the wrapped token on or off the watched list in the same call, and the app refuses to add or remove it on its own. The module itself doesn't know which token is the wrapped one; the pairing is the wallet's and the app's.

**A wallet that counts nothing has no limit.** With native off and no watched token, every transfer is outside the meter, and the app allows a limit of 0 there. Adding a watched token or switching native on while the limit is 0 freezes those tokens until a limit is set, so the app asks for a limit first.

**Deliberate v1 exclusions** (per design, not gaps): Permit2/permit-signature approvals and per-venue rate limits. Any unwatched ERC-20, and native when `nativeMetered` is off, sits outside the meter — the protection model is "count the tokens you care about".

---

## `PayeeGuard.sol` and the app checks

Mitfah's server builds every op a passkey approves, and a passkey's prompt shows nothing about it, so the contact list is enforced by the wallet. `SpendingLimitModule.preCheck` calls `PayeeGuard.check(account, contactsVoidedAt, msgData)` for every execution whose caller is the EntryPoint (a passkey's op, or an owner-signed UserOp). Not checked: the owner's direct calls, installed executors, and trading agents' ops (the wallet calling itself from `executeUserOp`; the owner's call, 2026-11-27). The guard is stateless; its address is an immutable of the module. THREAT_MODEL §3.18.

**Contact passes.** The owner signs EIP-712 `Contact(string name,address payee,uint64 issuedAt)` under domain `{name: "Mitfah", version: "2"}` (no chainId, no verifying contract: one signature, every wallet the owner has). Mitfah appends what an op needs after `execute`'s ABI arguments:

```
execute(mode, executionCalldata) || pass_1 .. pass_n || uint8 n || 0x4d465031 ("MFP1")
pass = payee (20) | issuedAt (8, big-endian) | keccak256(name) (32) | r (32) | s (32) | v (1)   // 125 bytes, n <= 8
```

A pass is valid while its signer is the wallet's current `owner()`, `issuedAt > contactsVoidedAt(account)` and `issuedAt <= block.timestamp + 1 day`. It is only verified when a payee needs it (about 6,600 gas each, measured on a transfer).

**Rules, per call** (anything else reverts):

| Call | Rule |
|---|---|
| no calldata | target must be the wallet or a contact (`PayeeGuard_NotAContact`) |
| `transfer`, `transferFrom`, ERC-721/1155 safe transfers (no value) | recipient must be the wallet or a contact |
| `approve`, `increaseAllowance`, `increaseApproval` | spender must be a trusted spender, or address(0) (`PayeeGuard_UntrustedSpender`) |
| `setApprovalForAll` | only `false` |
| `removeAgent(wallet, agent)` | on a validator the wallet installed, for this wallet |
| anything else | `SHRegistry.appCheck(target)` must exist; its report's payees, trades (out ≥ 88% of in at oracle prices) and pool prices (within 12%) are checked; unpriced tokens have no floor |

```solidity
function check(address account, uint64 voidedAt, bytes calldata msgData) external view;
function contactDigest(address payee, uint64 issuedAt, bytes32 nameHash) external view returns (bytes32);
```

**Module additions:** `constructor(address registry, address payeeGuard)` (the EntryPoint is read from the registry); `voidContactPasses(address account)` — the account's owner only, calling directly — sets `contactsVoidedAt(account)` to now and emits `ContactPassesVoided`.

**App checks** (`src/appchecks/`, `IAppCheck`): `app()` names the one contract a check reads, and `inspect(account, value, data)` returns `(address[] payees, Trade[] trades, PoolPrice[] prices)` or reverts for anything it doesn't fully understand. A check reports; the guard decides. `SHRegistry` keeps `appCheck(app)`, set by its constructor at deploy (`AppCheckEntry[]`) and afterwards only through `proposeAppCheck` → 2 days → `commitAppCheck` (or `cancelAppCheck`); `removeAppCheck` is instant. The deploy script registers, per chain: the wrapped native token's and the reputation registry's checks everywhere, SwapRouter02 and the V3 position manager on the Uniswap V3 chains, PancakeSwap's V2 router on BSC.

---

## `RecurringPaymentModule.sol`

An ERC-7579 **executor** (module type 2) that pays a fixed amount of one token, or the native coin, to one recipient on a fixed interval. The owner approves the schedule **once**; after that no signature is needed per payment. `SHFactory` installs it on **every** wallet (the user's call: about 70k more gas per deploy — three new storage slots plus two calls — and a bug in it would reach every wallet, accepted). It can move nothing until the owner creates a schedule.

```
keeper (backend) → RecurringPaymentModule.run(account, id)
                 → SessionHandler.executeFromExecutor(single call)
                     ├─ _guardSessionExecution   (same guard as a session key, allowlist included)
                     ├─ _applyFee(true)           (protocol fee + keeper fee → treasury)
                     └─ _execute → hook preCheck → token.transfer(recipient, amount) → hook postCheck (USD cap)
```

```solidity
uint48 public constant MIN_INTERVAL = 1 hours;

struct Schedule {            // three slots
    address token;           // address(0) = native
    uint48  nextDue;
    uint48  end;             // last moment a payment may fall due; 0 = no end
    address recipient;
    uint48  interval;
    uint48  generation;      // the wallet's generation at creation; older = wiped by an uninstall
    uint256 amount;          // 0 marks an empty slot
}

// Owner only, calling the module DIRECTLY (msg.sender == account.owner(), owner != account)
function createSchedule(address account, address token, address recipient, uint256 amount,
                        uint48 interval, uint48 firstDue, uint48 end) external returns (uint256 id);
function cancelSchedule(address account, uint256 id) external;

// Anyone (the backend is the keeper)
function run(address account, uint256 id) external;

function getSchedule(address account, uint256 id) external view returns (Schedule memory, bool live);
function isDue(address account, uint256 id) external view returns (bool);
mapping(address => uint256) public scheduleCount;   // ids run 1..scheduleCount, never reused
mapping(address => uint48)  public generation;      // bumped by onUninstall
```

**Who can do what — the property it rests on.** Creating and cancelling are owner-only, by the owner's EOA calling the module directly. A session key (passkey) acts *as the wallet*, so anything it routes through `execute` reaches the module with `msg.sender == wallet`, which is never the owner: a key cannot create a schedule that would keep paying after the key expires. A wallet that owns itself is refused for the same reason. This means a schedule needs MetaMask on the dashboard; the assistant and Telegram can only send the user a link. `run` is open to anyone, because the schedule fixes token, recipient, amount and timing — the caller only picks the moment, and only within the due window.

**One payment per `run`, no pile-up.** If periods are missed (paused wallet, no funds, over the cap, keeper down), the next `run` pays **one** instalment and moves `nextDue` to the first point on the schedule's grid (`firstDue + k × interval`) still in the future. Catching up would let a stalled schedule drain a wallet in one burst. When `nextDue` passes `end`, the schedule is deleted after that payment (`RecurringPaymentExecuted(…, nextDue = 0)`).

**Checks-effects-interactions.** `run` moves `nextDue` (or deletes the finished schedule) *before* calling the wallet, so a recipient or token that re-enters `run` during the payment finds nothing due.

**A failed payment changes nothing.** Over the cap, paused, not on the allowlist, not enough for the fee, or a token whose `transfer` returns `false` (`RecurringPayment_TransferFailed`; no return data counts as success, as in SafeERC20): the whole `run` reverts, `nextDue` stays, no fee is charged, and the keeper retries.

**Uninstall wipes everything in O(1).** `onUninstall` bumps the wallet's `generation`; schedules from an older generation are dead. A reinstall starts empty, and ids keep counting.

**Errors** (prefixed `RecurringPayment_`): `NotOwner(account, caller)`, `NotInstalled(account)`, `UnknownSchedule(account, id)`, `NotDue(dueAt)`, `InvalidRecipient`, `InvalidToken`, `InvalidAmount`, `InvalidInterval(interval, min)`, `InvalidEnd(end, firstDue)`, `TransferFailed`.

---

## `AgentModule.sol`

An ERC-7579 **validator** (module type 1) that gives a wallet up to **three trading agents**, each with its own secp256k1 key (held by the agent; Mitfah never sees it), its own **USD limit** per window of the wallet's limit, and an expiry at most **90 days** ahead. An agent's trade needs no tap from the user: it is an ordinary **ERC-4337 UserOp** from the wallet, sent by Mitfah's bundler exactly like a passkey's, with **gas paid by the wallet** (from its daily gas budget, shared with the passkeys). One contract per chain serves every wallet; `SHFactory` installs the registry's `agentModule` on every wallet as a validator, empty until the owner adds an agent.

```
agent op:  nonce key = AgentModule ‖ 4-byte lane      callData = executeUserOp.selector ‖ execute(mode, calls)
           signature = agent (20) ‖ epoch (4) ‖ deadline (6) ‖ r ‖ s ‖ v      (95 bytes)

EntryPoint.handleOps
 ├─ validation: wallet.validateUserOp → _authenticate (95 bytes → the validator route; see SessionHandler)
 │      ├─ maxOpGasCost, sessionGasBudget, whenNotPaused        (the wallet's, as for any non-owner op)
 │      └─ AgentModule.validateUserOp  [staticcall, read-only]
 │             ├─ agent registered, epoch matches, deadline ≠ 0, ECDSA over hashOperation(userOpHash, deadline, epoch)
 │             │        any failure → SIG_VALIDATION_FAILED
 │             ├─ callData = executeUserOp ‖ execute(mode, calls), mode single or batch     (else revert)
 │             ├─ account.getConfig(): native metered (rule 1); every call checked (rules 2 and 3)   (else revert)
 │             └─ valid until min(deadline, agent's expiry)
 └─ execution: wallet.executeUserOp
        ├─ AgentModule.preExecute: agent still registered, same epoch; remaining = getRemainingBudget() → transient
        ├─ wallet.execute(mode, calls)  [self-call]: guard, _applyFee(false) (protocol fee only), hook around the calls
        └─ AgentModule.postExecute: charged = remaining − getRemainingBudget(); agent's spend += charged + fee in USD;
                                    over its limit → revert; AgentExecuted
```

```solidity
uint256 public constant MAX_AGENTS = 3;
uint256 public constant MAX_VALIDITY = 90 days;
uint256 public constant SIGNATURE_LENGTH = 95;
bytes32 public constant OPERATION_TYPEHASH =
    keccak256("AgentOperation(bytes32 userOpHash,uint48 deadline,uint32 epoch)");
// EIP-712 domain: name "AgentModule", version "1", chainId, verifyingContract = the module

struct Agent {               // two slots
    uint128 limitUsd;        // 18-decimal USD per window of the wallet's limit
    uint128 spentUsd;        // in the window that started at windowStart
    uint48  validUntil;
    uint32  windowStart;     // the wallet's window start when spentUsd was written; another one = 0 spent
    uint32  epoch;           // signatures must name it; new on every add, never 0
    uint8   position;        // 1-based in the wallet's list; 0 = not an agent
}

// Owner only, calling the module DIRECTLY (msg.sender == account.owner(), owner != account)
function addAgent(address account, address agent, uint128 limitUsd, uint48 validUntil) external;
function updateAgent(address account, address agent, uint128 limitUsd, uint48 validUntil) external;
// Owner, or the wallet itself (a passkey through execute): removing only takes authority away
function removeAgent(address account, address agent) external;

// The wallet calls these (msg.sender == the wallet)
function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash) external view returns (uint256);
function isValidSignatureWithSender(address, bytes32, bytes calldata) external pure returns (bytes4); // always 0xffffffff
function preExecute(PackedUserOperation calldata userOp, bytes32 userOpHash) external;   // IUserOpHooks
function postExecute(PackedUserOperation calldata userOp, bytes32 userOpHash) external;  // IUserOpHooks

function hashOperation(bytes32 userOpHash, uint48 deadline, uint32 epoch) public view returns (bytes32);
function getAgents(address account) external view returns (address[] memory);
function getAgent(address account, address agent) external view
    returns (Agent memory info, uint256 spentUsd, uint256 remainingUsd, bool active);
```

**Who can do what.** As with recurring payments, adding an agent or changing its limit or expiry needs the owner's EOA calling the module directly; a passkey acts *as the wallet* and is refused. `removeAgent` also accepts the wallet itself, so a passkey can stop an agent without the owner wallet — and an op the agent already signed then fails when it runs, even later in the same bundle (`preExecute` re-checks). `onInstall` takes nothing, because anything acting as the wallet can call a module's `onInstall` directly. `addAgent` needs the module installed as a validator and native metering on. An agent can never sign a message for the wallet: `isValidSignatureWithSender` always answers "invalid" (ERC-1271 signatures such as Permit2 grants could move tokens around the limit).

**What an agent's trade may do.** Any call the wallet's guard allows a passkey, on any exchange or protocol and to any address (the user's choice: agents are not held to the PayeeGuard's contacts, app checks or floors), plus three rules of its own, all checked in **validation**, so a rule-breaking op is refused before it costs anything, with the rule's error in the EntryPoint's `AA23 reverted`:
1. **Native ETH/BNB must be metered** (`Config.nativeMetered`), checked on every op, so an owner who turns it off stops their agents until it is back on.
2. **No uncounted token leaves.** A call with a token-moving function — `transfer`, `transferFrom`, `approve`, `increaseAllowance`, `increaseApproval`, ERC-721's `safeTransferFrom` (×2) and `setApprovalForAll`, ERC-1155's `safeTransferFrom` and `safeBatchTransferFrom`, ERC-1363/677's `transferAndCall` (×2), `transferFromAndCall` (×2) and 2-argument `approveAndCall`, ERC-777's `send`, `operatorSend` and `authorizeOperator`, ERC-6909's `transfer`, `transferFrom`, `approve` and `setOperator`, and DSToken's `push` and `move` — to any target not on the wallet's watched list is refused (`AgentModule_UncountedToken`). Tokens capped in their own units aren't on the watched list, so they are refused too. The grants the hook can't track are refused by the hook for everyone.
3. **No call to the module**, so an agent can't remove, add or raise agents, or touch the hooks' bookkeeping, through its own trade.

The calls are read out of the callData exactly as the wallet's `execute` and the hook's `_decodeExecuteCalldata` read them (the same ABI offset formula), so the calls checked are the calls that run.

**The agent's limit.** The hook already measures the net USD that left the wallet. `getRemainingBudget()` is "as of now" (a run-out window counts as empty), the wallet's spend only rises within a transaction, and its limit can't change inside one, so the drop in remaining budget from `preExecute` to `postExecute` is exactly the trade's charge. It is carried between them in **transient storage keyed by the wallet**, so another caller can only write its own. The module adds the protocol fee (`feeConfig()`, priced with `getUsdValue(address(0), …)`), which the wallet's limit leaves out: without it, a leaked key could empty the wallet's ETH in fees through empty ops. **Gas is not counted**, as for a passkey; the wallet's gas ceilings bound it. The agent's spend is kept against the wallet's window start (like a token cap), so it starts over when the wallet's window does, including a window that rolls in the trade's own `preCheck` (`postExecute` reads the window after the calls). Over either limit the op reverts in **execution**, so nothing moves but that op's gas is paid; the backend simulates every op first. Anything else calling the hooks (the wallet itself, through a passkey) can only add to an agent's spend: `postExecute` without an open `preExecute` reverts, and a spend is only ever added to.

**Signatures.** EIP-712 `AgentOperation(userOpHash, deadline, epoch)` (`hashOperation`); any agent wallet's `signTypedData` produces it. The op hash covers every field of the op (wallet, chain, EntryPoint, nonce, calls, gas), and the EntryPoint's nonce makes each op usable once. The deadline (non-zero) makes an unsent op die minutes after it was signed; the op is valid until the earlier of it and the agent's expiry. The epoch is new on every add, so removing an agent voids every op it signed, even if the same address is added back. The agent and the epoch ride in the signature so the hooks can read them without recovering the signer again.

**Uninstall** removes every agent of the wallet (at most three deletes, never reverts). **Pause** stops them (`validateUserOp` is `whenNotPaused`).

**Gas.** The same real swap as a whole `handleOps` (fork tests, 2026-11-23), agent vs passkey: Ethereum 414k vs 384k, BSC 469k vs 437k, Arbitrum 457k vs 423k, Base 442k vs 408k — about 31–34k (7–8%) more for an agent: the module's signature check and config read in validation, then the budget reads, the fee's price, the config read for the window and the agent's slot around the calls. On local mocks a USDC transfer is ~236k vs ~167k. See the fork suites' `test_agent_gas_swap` / `test_agent_gas_passkeySwapForComparison` and `AgentModuleTest.test_gas_agentOpBesideAPasskeyOp`.

**Errors** (prefixed `AgentModule_`): `NotOwner(account, caller)`, `NotInstalled(account)`, `NativeNotMetered(account)`, `InvalidAgent(agent)`, `InvalidLimit`, `InvalidExpiry(validUntil, latest)`, `TooManyAgents(account)`, `AlreadyAdded(account, agent)`, `UnknownAgent(account, agent)`, `NotAnAgentOp`, `UnsupportedMode(mode)`, `TargetNotAllowed(target)`, `UncountedToken(token, selector)`, `AgentLimitExceeded(spentUsd, limitUsd)`, `NoTradeOpen(account)`.

**Events:** `AgentAdded`, `AgentUpdated`, `AgentRemoved(account, agent, by)`, `AgentsCleared`, and `AgentExecuted(account, agent, userOpHash, chargedUsd, feeUsd, spentUsd)` for every trade (the op hash matches the EntryPoint's `UserOperationEvent`), which the backend turns into a notification.

---

## ERC-8004 Infrastructure

The project integrates the **ERC-8004** standard for on-chain agent identity and reputation.

**Canonical registries (Sepolia / Mainnet):** On live networks, the UUPS-upgradeable registries deployed by the ERC-8004 working group are used, baked into `SHFactory` and `SessionHandler` at deployment via `HelperConfig`.

| Contract | Sepolia | Mainnet |
|---|---|---|
| `IdentityRegistry` | `0x8004A818BFB912233c491871b3d84c89A494BD9e` | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| `ReputationRegistry` | `0x8004B663056A597Dffe9eCcC1965A193B7388713` | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` |

> BSC currently reuses the Mainnet ERC-8004 registry addresses — confirm before relying on it in production.

`src/mocks/MockIdentityRegistry.sol` and `MockReputationRegistry.sol` are full non-upgradeable mocks for Anvil and unit tests. `DeploySHProtocol.s.sol` calls `IIdentityRegistry.register(AGENT_URI)` during deployment, minting the agent's identity NFT; the returned `agentId` is stored in `SHRegistry` and readable from any `SessionHandler` via `getAgentId()`.

Session-key calls to the Reputation Registry (`giveFeedback`) work without any special registration: `giveFeedback` moves no value, and the target is not a watched token, so the net-value meter simply charges nothing. (The old `addUnpricedSessionKey` escape hatch no longer exists — there's nothing per-target to exempt.)

---

## `HelperConfig.s.sol`

`HelperConfig` resolves chain-specific deployment parameters at runtime. Its `NetworkConfig` struct carries ~22 tokens and their Chainlink USD price feeds + heartbeats, plus the L2 sequencer uptime feed, the ERC-8004 registry addresses, and the chain's starting gas ceilings for every wallet (`maxOpGasCost` per op, `sessionGasBudget` per day; see `SessionHandler` above). (The router is **not** protocol config — which venue a wallet trades on is a wallet-level choice; the fork suite reads it straight from `Constants.s.sol`, as an owner would.)

| Network | Chain ID | EntryPoint | Exchange (from Constants) | Sequencer uptime feed |
|---|---|---|---|---|
| Ethereum Mainnet | 1 | `ENTRYPOINT_V07` (canonical) | Uniswap V3 (SwapRouter02, position manager) | none (`address(0)`) |
| Ethereum Sepolia | 11155111 | `ENTRYPOINT_V07` (canonical) | Uniswap V3 (SwapRouter02, position manager) | none (`address(0)`) |
| BSC | 56 | `ENTRYPOINT_V07` (canonical) | PancakeSwap V2 | none (`address(0)`) |
| Arbitrum One | 42161 | `ENTRYPOINT_V07` (canonical) | Uniswap V3 (SwapRouter02, position manager) | `ARB_SEQUENCER_UPTIME_FEED` |
| Base | 8453 | `ENTRYPOINT_V07` (canonical) | Uniswap V3 (SwapRouter02, position manager) | `BASE_SEQUENCER_UPTIME_FEED` |
| Anvil (local) | 31337 | Freshly deployed, cached per session | none (`address(0)`) | none (`address(0)`) |

`getConfigByChainId` **reverts with `HelperConfig__InvalidChainId`** on an unrecognised chain ID, rather than silently handing back mainnet's token and feed addresses — which would be wrong for that chain. For Anvil, `HelperConfig` deploys a fresh `EntryPoint`, `MockV3Aggregator` feeds seeded with approximate real-world prices, and the ERC-8004 mocks (all inside `vm.startBroadcast`, so the mock token addresses land on-chain and are recoverable from the broadcast file). Sepolia uses a wide 72h heartbeat across the board (its Chainlink nodes update far less often than mainnet's — an accepted testnet characteristic). The mainnet/BSC/Arbitrum/Base deployer resolves from `MAINNET_DEPLOYER_ADDRESS`, falling back to a deterministic placeholder for fork tests — export the real one before a live deployment.

**Arbitrum coverage.** 16 of the ~21 tokens carry both an official Arbitrum deployment and a Chainlink USD feed. The rest are zeroed **in pairs** — token *and* feed together — because `DeploySHProtocol` pairs the arrays positionally and `SHOracle` reads a zero token as the native-ETH sentinel, so a zero token beside a live feed would silently repoint native pricing at it. ENS, SAND and IMX have no Arbitrum feed; BNB, AVAX and wTAO have feeds but no credible token deployment on the chain.

**Base coverage.** 9 tokens carry both a credible Base deployment and a Chainlink USD feed: USDC (Circle's native one, not the bridged USDbC), DAI, USDT (the standard-bridge one, not Stargate's USD₮0), WETH, AAVE, LINK, WBTC, COMP and YFI. The rest are zeroed in pairs, as on Arbitrum: 1INCH, CRV, SAND, SUSHI, UNI, CAKE and TAO are deployed on Base but have no feed there; APE, ARB, ENS and IMX have neither; BNB and AVAX have feeds but no credible token. Two choices differ from the other chains. WBTC is priced by Base's own WBTC/USD feed, not BTC/USD, so a depegged WBTC is never valued at the BTC price (net-value metering would let a swap into it offset real spending). And Chainlink's reference data no longer lists a bare `eth-usd` on Base: ETH/USD runs on a shared SVR aggregator, and `BASE_ETH_USD_PRICE_FEED` is its long-standing public proxy, which reads that same aggregator (checked with `aggregator()`).

---

## `DeploySHProtocol.s.sol`

Orchestrates deployment of all shared infrastructure. Individual `SessionHandler` wallets are not deployed here — users call `SHFactory.deployWallet(...)`.

**Deployment sequence:**

1. Instantiate `HelperConfig`; build parallel `(tokens, priceFeeds, heartbeats)` arrays. Convert both fees' dollar targets — the protocol fee's constants (`MIN_PROTOCOL_FEE_USD`, `MAX_PROTOCOL_FEE_USD`, `INITIAL_PROTOCOL_FEE_USD`) and the chain's keeper-fee fields — to whole-gwei wei amounts through the native feed, `priceFeeds[0]` (`_feeBounds`).
2. Deploy `SHTreasury()` — the admin root, deployed **first** so its address can own everything below.
3. Deploy `SHOracle(address(treasury), config.sequencerUptimeFeed, tokens, priceFeeds, heartbeats)` — born owned by the treasury.
4. Call `IIdentityRegistry.register(AGENT_URI)` to mint the agent NFT and obtain `agentId`.
4b. Deploy this chain's app checks (`_deployAppChecks`): `WrappedNativeCheck` (WBNB on BSC, WETH elsewhere) and `ReputationCheck` everywhere; `SwapRouter02Check` and `V3PositionManagerCheck` on the Uniswap V3 chains; `V2RouterCheck` for PancakeSwap on BSC; nothing more on a bare Anvil node. Each reads its app at construction.
5. Deploy `SHRegistry(address(treasury), protocolFeeBounds, keeperFeeBounds, address(treasury), address(oracle), …, agentId, appChecks)` — owned by the treasury and paying fees to it; the starting app checks are registered with no delay (no wallet exists yet).
6. Call `treasury.setRegistry(address(registry))` — write-once, fixing the pairing.
6b. Deploy `PayeeGuard(address(registry))`, then `SpendingLimitModule(address(registry), address(payeeGuard))` — wired to the **registry**, from which it resolves the current oracle on every valuation (no interpreter). Must come after the registry: its constructor reads `priceOracle()` off it.
7. Call `treasury.setSpendingLimitModule(address(module))` — the module could not be a registry constructor argument (see the registry section), so it is registered here.
7b. Deploy `RecurringPaymentModule()` (no constructor arguments) and call `treasury.setRecurringPaymentModule(address(recurringModule))`.
7c. Deploy `AgentModule()` (no constructor arguments: it reads all it needs from each wallet) and call `treasury.setAgentModule(address(agentModule))`. Every wallet the factory deploys then installs it as a validator. The app reads its address off the registry (`agentModule()`).
8. Deploy `SHFactory(address(treasury), address(registry), GasCeilings(maxOpGasCost, sessionGasBudget))` — owner, registry, and this chain's starting gas ceilings from `HelperConfig`. Every address a wallet needs is read off the registry, so the factory stores none of them. It deploys the `SessionHandler` implementation it clones from internally, with the gas ceilings as that implementation's defaults.
9. Call `treasury.setFactory(address(factory))` — records the factory on the registry for off-chain discoverability.

No `transferOwnership` step is needed anywhere, and at no point does the deployer EOA own a live contract other than the treasury.

```solidity
function run() external returns (SHFactory factory, SHTreasury treasury, HelperConfig.NetworkConfig memory config, SHOracle oracle);
```

---

## `SendPackedUserOp.s.sol`

A reusable helper for constructing `PackedUserOperation`s, used by the test suite. It signs **owner** ops itself (secp256k1, the only key a script can hold); for **passkey** ops it builds the same op unsigned and hands back the hash, which the caller signs as a WebAuthn challenge (`test/utils/PasskeyHelper.sol` plays the phone in tests).

**Flow:**
1. Fetch the nonce from `EntryPoint.getNonce(sender, nonceKey)`. Owner ops use key 0. A passkey op's key must carry a non-zero op deadline in its top 48 bits (`PasskeyHelper.nonceKey(opDeadline, lane)`), or the account refuses it.
2. Pack the execution into `execute(mode, executionCalldata)` calldata — single-call or batch.
3. Get `userOpHash` from `EntryPoint.getUserOpHash(userOp)`.
4. Owner: wrap in the EIP-191 envelope via `toEthSignedMessageHash` (what `_authenticate` re-wraps before recovery), sign, attach `(r, s, v)`. Passkey: stop here; the raw `userOpHash` is the challenge.

```solidity
// Single-call
function generateSignedUserOp(
    address sender, HelperConfig.NetworkConfig memory config,
    address dest, uint256 value, bytes memory data,
    address signer, uint256 signerKey
) external view returns (PackedUserOperation memory, bytes32 userOpHash, bytes32 digest);

// Atomic batch (e.g. [approve router, swap] so the approval is consumed in one transaction)
function generateSignedBatchUserOp(
    address sender, HelperConfig.NetworkConfig memory config,
    Execution[] memory executions,
    address signer, uint256 signerKey
) external view returns (PackedUserOperation memory, bytes32 userOpHash, bytes32 digest);

// Unsigned twins, for a passkey to sign the returned hash
function generateUnsignedUserOp(
    address sender, HelperConfig.NetworkConfig memory config,
    address dest, uint256 value, bytes memory data,
    uint192 nonceKey
) external view returns (PackedUserOperation memory userOp, bytes32 userOpHash);
function generateUnsignedBatchUserOp(
    address sender, HelperConfig.NetworkConfig memory config,
    Execution[] memory executions,
    uint192 nonceKey
) external view returns (PackedUserOperation memory userOp, bytes32 userOpHash);
```

---

## Test Suite

Totals: **528** unit (133 protocol + 85 guard + 54 passkey + 70 admin + 42 token cap + 22 native metering + 26 recurring payments + 81 agents + 15 registry fees) + **13** invariants, all local (`forge test --no-match-path "test/fork/*"`), and **22–26** fork tests per network on mainnet, BSC, Arbitrum and Base, plus Sepolia. All passing.

**`test/unit/SHProtocolTest.t.sol` (133 tests)** — deploy/factory config (every wallet gets the recurring executor and the agent validator; the factory refuses while either is unset), module lifecycle (install/uninstall/reinstall, `isModuleType` hook-only), config setters (limit/window/watched-token cap, non-owner reverts), passkey management (add/renew, the curve and expiry checks, the cap of five and the sweep of expired keys, remove/remove-all, `getSessions`), **net-value metering** (transfer pricing, inflow-offset within a tx, no-banked-credit across txs, window roll, per-token staleness isolation), **approvals** (unlimited rejected, standing reverts, consumed-in-same-tx passes, partial reverts, approve-then-zero), **trusted spenders** (Option C: unpriced approval allowed when trusted / reverts when untrusted, unlimited still rejected, standing still reverts, remove reinstates the price gate, uninstall clears), plus the harness calldata/approval-classifier tests. Uses a `MockSpender` to consume allowances mid-batch.

**`test/unit/TokenCapTest.t.sol` (42 tests)** — limits in a token's own units, on a token with a feed and one without: the setters and their refusals, one meter per token, charging (netting within a transaction, a balance drop with no call to the token, the window roll, a cap of 0), buying and selling a capped token, the calls a capped token may receive, and a token whose `balanceOf` fails: it blocks what reads it, can't skip being charged, can always be removed, and doesn't stop a plain send.

**`test/unit/NativeMeteringTest.t.sol` (22 tests)** — the `nativeMetered` flag: the deploy choice lands in the Config slot's second byte; with it off a native send isn't charged, no price is read (so a stale native feed blocks nothing), native coming in offsets nothing (selling a counted token for ETH charges the full amount), watched tokens are still capped and unwrapping WETH is still charged; a wallet that counts nothing works at limit 0, and a counted token freezes there until a limit is set; switching moves WETH with the flag in one call, keeps what was spent, refuses a non-owner and refuses being reached through `execute`; `preCheck` hands its decision to `postCheck`, so a switch between the two can't split them; and a reinstall starts from the new choice.

**`test/unit/SessionGuardTest.t.sol` (85 tests)** — drives real `EntryPoint.handleOps` end-to-end, every session-key op signed by a soft passkey. A wallet deployed with `deployWallet`'s passkey arguments is driven by that key with no `addSession` call anywhere (`test_deployWallet_seededSessionKeyExecutesImmediately`). Session-key UserOps attempting `uninstallModule`, `setDailyLimit`, `addSession`, or a batch smuggling a restricted target all fail with the admin state asserted **unchanged**; direct EntryPoint-pranked calls prove the exact guard errors (`SessionHandler_SessionRestrictedTarget`, `SessionHandler_SessionDelegateCallForbidden`); owner-direct `execute` bypasses the account's guard but is still refused by the module's own (`SpendingLimitModule_AdminExecution`, single and batch); and unknown/removed signers fail validation with `AA24`.

Expiry is proved through the same real `handleOps` path: a key past its deadline fails `AA22 expired or not due` (not `AA24`), is accepted in the second that *equals* the deadline, works again after renewal, and a **removed** key fails `AA24` because it is no longer the wallet's key at all. An owner-signed op still executes a year after the session key lapsed — the owner carries no validity window.

**`test/unit/PasskeyValidationTest.t.sol` (54 tests)** — what `_authenticatePasskey` accepts (honest assertions, synced-passkey flags, any signature counter, Chrome's extra clientDataJSON field) and every way a signature must fail **without reverting**: unknown/removed key, tampered JSON or challenge, a signature over another op or nonce, UV or UP off, high `s`, the wrong client-data type, another key under a held key's id, a non-owner ECDSA signature, the struct-value encoding, garbage of every length, and huge JSON indices (with a test showing OpenZeppelin's own verifier panics on one — why the account bounds them first). The **op deadline**: zero refused (`AA24`), the window is the earlier of op and key, it cannot be moved after signing, an expired op fails `AA22` while the key is live, and the owner ignores it. Plus the bundler's one-slot override, a known-answer vector produced outside Foundry (Python), and gas measurements (passkey validation is under 150k gas).

> **Gotcha:** the vendored account-abstraction is EntryPoint **v0.9**, whose `nonReentrant` requires `tx.origin == msg.sender` — tests must submit `handleOps` with a two-arg `vm.prank(bundler, bundler)` (EOA bundler) or it reverts `Reentrancy()`.

**`test/fork/SHForkTestBase.sol` (32 tests, abstract)** + thin per-network instances: **`SHMainnetUniswapV3Test`** (mainnet, WETH→DAI), **`SHSepoliaUniswapV3Test`** (Sepolia, WETH→USDC), **`SHArbitrumUniswapV3Test`** and **`SHBaseUniswapV3Test`** (WETH→USDC, plus the sequencer-gate tests), all swapping through Uniswap V3's SwapRouter02 via **`SHUniswapV3ForkTestBase`**, and **`SHPancakeswapV2Test`** (BSC, USDT→WBNB→LINK multi-hop on PancakeSwap V2). Each suite forks its own chain in `setUp` (`vm.createSelectFork(vm.rpcUrl(...))`, URLs from foundry.toml's `[rpc_endpoints]`). Runs identically against the real router, live Chainlink feeds, and the canonical EntryPoint: wallet deploys with cap config, `deployWallet` seeding the chain's default spenders (`_trustedSpenders()`: SwapRouter02 and the position manager on the V3 networks, PancakeSwap's router on BSC — from Constants.s.sol, as `constants.get_default_spenders` seeds them in the app), each asserted trusted; net-value metering at live prices; budget-exceeded reverts atomically; a real **session-key swap** metered by net portfolio change; standing/unlimited approvals rejected on a real DEX; a session op targeting the module reverts; a revoked key fails validation; on a wallet that doesn't count native, a native send isn't charged and selling a counted token for native at live prices charges its full value; and a **liquidity round-trip** (`_liquidityRoundTrip()`, whose body depends on the venue). On BSC it is `addLiquidity → removeLiquidity`, proving the unpriced LP-token approval flows through the trusted router (the router creates the pair where there is no direct pool). On the V3 networks it is a position, every step a session-key UserOp through the real EntryPoint: approve both tokens, `mint`, approve both back to 0; then `decreaseLiquidity` by half and `collect`; then the rest, `collect` and `burn`. It checks the wallet holds the NFT (the position manager mints with a plain `_mint`, so a SessionHandler without `onERC721Received` can), the deposit is metered, no allowance is left, and neither withdrawal is metered — they need no approval, since the wallet owns the position. Metering assertions are computed from actual balance diffs priced through the deployed oracle, so they hold despite testnet pool-ratio divergence. **Agents** (`test_agent_*`, the `AgentModule` the factory installed, one agent added by the owner, every trade a UserOp through the real EntryPoint): a swap charges the wallet's limit the net value lost and the agent's limit that plus the fee; a native-in swap is charged net, the wallet paying the value, the protocol fee and the op's gas (counted in its gas budget) in native; a deposit into the venue (`_depositBatch()`: a V2 `addLiquidity`, or step 1 of the V3 round-trip) is charged at what went in; a listed token the wallet doesn't count can be neither sent nor approved (refused in validation, `AA23`); the agent's own limit stops a transfer the wallet's would allow (in execution, nothing moves); and the gas of the same swap from an agent and from a passkey, both whole `handleOps`.

**`test/unit/SHAdminTest.t.sol` (70 tests)** — the admin surface: the SHTreasury-rooted ownership graph (direct calls to the registry, oracle or factory revert; everything goes through a treasury passthrough), the two-day oracle timelock, and SHOracle's feed admin.

> **Gotcha:** `SHAdminTest` sits right at solc's legacy-assembler limit — adding a few tests made it fail with *"Internal compiler error: Tag too large for reserved space"*. New registry tests go in `RegistryFeeTest` instead.

**`test/unit/RecurringPaymentTest.t.sol` (26 tests)** — `RecurringPaymentModule` on a real wallet: only the owner creates or cancels (a stranger, a passkey through `execute` — exact error and through real `handleOps` — and a self-owned wallet are all refused); input checks; a due token payment pays the payee, moves the due date, sends protocol + keeper fee and is metered by the cap; native payments; anyone can run but only once per period; missed periods pay once and land on the grid; the last payment ends the schedule; over the cap, paused, allowlist, no fee money and a false-returning token all revert with the schedule still due; a re-entering recipient is paid exactly once; uninstall wipes and a reinstall starts clean. Plus the gas of one `run` (`-vv`), which sized the keeper fee.

> **Gotcha:** never write `uint256 start = block.timestamp;` before a `vm.warp` — the optimizer treats `TIMESTAMP` as constant within a call and may re-read it after the warp, shifting every expected date. Use a constant start and warp to it.

**`test/unit/AgentModuleTest.t.sol` (81 tests)** — `AgentModule` on a real wallet, every trade a real UserOp through `handleOps`. Every wallet has it as a validator. Adding: the owner only (a stranger, a passkey through `execute` — exact error and through real `handleOps` — and a self-owned wallet refused), installed, native metered, bad agents and terms, no duplicates, at most three, `onInstall` adds nothing. Updating keeps the epoch; a passkey can't raise. Removing: the owner, or a passkey through a real UserOp; swap-and-pop keeps the rest findable; uninstall removes all. Trades: both limits, the protocol fee and no keeper fee, the treasury, the event, the wallet's gas budget charged; a deposit into another protocol and a plain ETH send are charged; the owner's and a passkey's spend isn't the agent's; the gas budget is shared with the passkeys; two lanes in one bundle. Signatures: other keys (the owner's included), every field of the op (callData, gas, epoch, deadline, the agent named), every wrong length (0, 64, 65, 94, 96), another chain, another module instance, replay (`AA25`), a passed or zero deadline, the agent's expiry capping the deadline, removal voiding old ops, `hashOperation` against Forge's own EIP-712 encoder, and no message signing (ERC-1271). Unknown (any epoch named), removed, uninstalled, paused, other modes, the wallet's guard (limit module, EntryPoint); an agent-signed op calling `execute` directly is never shown to the module; inside `executeUserOp` only `execute`; an agent removed earlier in the same bundle. The wallet's route, with a validator that approves everything: an owner op never reaches a module, `executeUserOp` refuses the owner's op, a passkey's op and any caller but the EntryPoint, runs only `execute`, needs `executeUserOp` in the callData, gives the self-call the guard, the fee and the hook (a standing approval refused), refuses an uninstalled validator, and passes a module's revert on. The hooks: `postExecute` alone refused, a trade leaves nothing open, a removed agent isn't written back, the wallet calling them only adds to a spend, a stranger reaches only its own. Rule 1, rule 2 (every listed function, inside a batch, a capped token, buying an uncounted token but not selling it), rule 3 (the hooks included). The agent's limit, the wallet's limit, fees stopping empty ops, the window (rolling inside the trade, started by another trade), a lowered limit, and a fuzz test of the limits. Plus the gas of one agent op beside a passkey op (`-vv`). 33 rules (11 in the wallet's route, 22 in the module) were each broken on purpose and the suite failed every time; one more break (accepting signatures longer than 95 bytes) changes nothing, since ECDSA recovery refuses them anyway.

**`test/unit/RegistryFeeTest.t.sol` (15 tests)** — both fees and all four bounds deployed as whole gwei and in order; the treasury and both fees packed in slot 7 (read with `vm.load`); `feeConfig`; `setKeeperFee` through the treasury, bounds and owner checks; non-whole-gwei fees refused, never rounded; `setRecurringPaymentModule` and `setAgentModule` reach only wallets deployed afterwards and refuse zero and strangers; the keeper fee's zero floor; the constructor's gwei and range rules for both fees.

**`test/invariant/` (13 invariants)** — `SHHandler` fuzzes owner executes (transfers, mint+transfer batches), config changes, non-owner attacks, passkey management (add, renew, remove, remove-all against a ghost list), a token capped in its own units, and time warps, maintaining a **ghost model** that independently replicates the module's roll-then-accumulate net metering. `InvariantSH` asserts, among others: `invariant_meteringMatchesGhostModel` (module `spentInWindow` equals the ghost exactly), spend never negative, config only ever changed by the owner, the hook stays installed, the watched list stays bounded/consistent, remaining-budget consistency, and for passkeys: never more than five, the on-chain list matches the ghost list, a key is listed iff its deadline is set, and every passkey call behaved as modelled.

---

## Foundry Commands

```bash
# Build
forge build

# Run all local (non-fork) tests
forge test

# Unit + guard suites only
forge test --match-path "test/unit/*"

# Invariant tests
forge test --match-contract InvariantSH

# Fork tests (need the respective RPC in .env)
make mainnet-uniswap-test    # Uniswap V3, mainnet fork
make sepolia-uniswap-test    # Uniswap V3, Sepolia fork  (make sepolia-test is an alias)
make pancakeswap-test        # PancakeSwap V2, BSC fork
make arbitrum-uniswap-test   # Uniswap V3, Arbitrum One fork (+ the live sequencer gate)
make base-uniswap-test       # Uniswap V3, Base fork (+ the live sequencer gate)

# Deploy shared protocol infrastructure
forge script script/DeploySHProtocol.s.sol \
  --rpc-url $SEPOLIA_RPC_URL \
  --account <keystore-account> \
  --broadcast \
  --verify
```
