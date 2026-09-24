# Smart Contract Architecture

The smart contract layer implements the SessionHandler Protocol as an **ERC-7579 modular account system**: shared infrastructure contracts deployed once per chain, and per-user `SessionHandler` smart wallets deployed on demand through `SHFactory`.

The account (`SessionHandler`) validates its own UserOperations and manages a session-key allowlist. The USD spending cap lives in a single installable **hook** (`SpendingLimitModule`) that meters net portfolio value around every execution, so any ERC-7579 account could reuse it.

## Contract Overview

```
src/
├── SHFactory.sol              ← User-facing factory — deploys one SessionHandler per user, assigns sequential walletIds
├── SHTreasury.sol             ← Protocol admin root — owns SHRegistry, SHOracle, and SHFactory; fee sink
├── SHRegistry.sol             ← Central config store — every address a wallet needs (EntryPoint, both ERC-8004 registries, module, oracle, treasury, fee, agentId)
├── SHOracle.sol               ← Chainlink-based USD value converter
├── SessionHandler.sol         ← ERC-7579 smart account — self-validation, session-key allowlist, cap config, admin guard
├── SpendingLimitModule.sol   ← ERC-7579 Hook (type 4 only) — global USD spending cap (net-value metering)
├── interfaces/
│   ├── IWETH.sol                 ← WETH interface (extends IERC20Extended)
│   ├── IERC20Extended.sol        ← IERC20 + IERC20Metadata, plus legacy increaseAllowance (for selector matching only)
│   ├── AggregatorV3Interface.sol ← Vendored Chainlink price-feed interface (used by SHOracle)
│   ├── IIdentityRegistry.sol     ← ERC-8004 IIdentityRegistry interface
│   └── IReputationRegistry.sol   ← ERC-8004 IReputationRegistry interface
└── mocks/
    ├── MockIdentityRegistry.sol   ← Full ERC-8004 Identity Registry mock (local testing)
    ├── MockReputationRegistry.sol ← ERC-8004 Reputation Registry mock (local testing)
    ├── ERC20Mock.sol              ← Mintable ERC20 for local testing
    ├── MockV3Aggregator.sol       ← Chainlink AggregatorV3Interface mock, seeded per-token on Anvil
    └── MockWeth.sol               ← WETH mock with deposit/withdraw for Anvil

script/
├── DeploySHProtocol.s.sol     ← Deployment entry point (SHTreasury → SHOracle → agent → SHRegistry → setRegistry → module → setSpendingLimitModule → SHFactory → setFactory)
├── Constants.s.sol            ← Chain IDs, canonical addresses, per-network token/Chainlink-feed addresses, Anvil mock prices
├── HelperConfig.s.sol         ← Chain-specific configuration resolver (Mainnet, Sepolia, BSC, Arbitrum, Anvil)
└── SendPackedUserOp.s.sol     ← ERC-7579 UserOp construction and signing helper (single + batch)

test/
├── unit/
│   ├── SHProtocolTest.t.sol            ← Core unit suite: deploy, module lifecycle, metering, approvals, trusted spenders (61 tests)
│   ├── SessionGuardTest.t.sol          ← End-to-end guard + session-key auth proof via the real EntryPoint (37 tests)
│   └── SpendingLimitModuleHarness.sol ← Test harness exposing the module's internal calldata/approval helpers
├── fork/
│   ├── SHForkTestBase.sol              ← Shared abstract fork suite (10 tests) parameterized per network
│   ├── SHUniswapV2Test.t.sol           ← Mainnet-fork instance (WETH→DAI)
│   ├── SHSepoliaUniswapV2Test.t.sol    ← Sepolia-fork instance (WETH→USDC)
│   ├── SHPancakeswapV2Test.t.sol       ← BSC-fork instance (USDT→WBNB→LINK)
│   └── SHArbitrumUniswapV2Test.t.sol   ← Arbitrum-fork instance (WETH→USDC) + L2 sequencer-gate tests
└── invariant/
    ├── InvariantSH.t.sol  ← Stateful invariant tests (8 invariants)
    └── SHHandler.sol      ← Action handler for fuzzing, with a ghost metering model
```

> **Celo is not yet wired into the Solidity deployment path.** The Python app layer has partial scaffolding for Celo, but `HelperConfig.s.sol` / `Constants.s.sol` have no Celo branch, so `DeploySHProtocol.s.sol` cannot deploy to Celo until that's added.

---

## `SHRegistry.sol`

The `SHRegistry` is the central configuration store. Deployed `SessionHandler` wallets read runtime parameters — protocol fee, treasury address, price oracle, and agent identity — from this single contract, so any parameter can be updated by the operator without redeploying user wallets. It is owned by `SHTreasury`, and all admin flows through `SHTreasury`'s pass-through setters. The `owner` is supplied to the constructor rather than taken from `msg.sender`, so the registry is deployed already owned by the treasury with no follow-up `transferOwnership`.

**Stored parameters:**

| Parameter | Type | Purpose |
|---|---|---|
| `protocolFee` | `uint96` | Flat fee **in wei** charged on every session-key execution, bounded to `[MIN_PROTOCOL_FEE, MAX_PROTOCOL_FEE]`. `getFee()` returns it as stored — no oracle call. See [Protocol fee](#protocol-fee) below. |
| `MIN_PROTOCOL_FEE` / `MAX_PROTOCOL_FEE` | `uint256` (immutable) | The fee's bounds in wei, set by the constructor. The deploy script converts **$0.005 and $0.10** through the chain's native price, so each chain gets the same dollar range in its own coin. |
| `treasury` | `address` | `SHTreasury` — fee sink and admin root (owns this registry, the oracle, and the factory) |
| `priceOracle` | `address` | Canonical `SHOracle` address for USD accounting. **Also governs spending-cap enforcement** — `SpendingLimitModule` resolves it from here on every valuation, so changing it changes how every deployed wallet meters spending. See [THREAT_MODEL.md](../THREAT_MODEL.md) §3.8. |
| `agentId` | `uint256` | ERC-8004 token ID of the registered protocol agent. **`0` is a valid id** — ERC-8004 registries mint from 0, so no zero-check exists or should. |
| `spendingLimitModule` | `address` | Hook installed on wallets deployed from here on. Owner-settable; **not** a constructor arg — see below. Changing it never touches existing wallets. |
| `ENTRY_POINT` | `address` (immutable) | Canonical ERC-4337 EntryPoint; `SHFactory` bakes it into the `SessionHandler` implementation, so every wallet shares it |
| `REPUTATION_REGISTRY` | `address` (immutable) | Reputation Registry, baked into the implementation the same way |
| `IDENTITY_REGISTRY` | `address` (immutable) | ERC-8004 Identity Registry, baked into the implementation the same way |
| `factory` | `address` | The SHFactory deploying against this registry. Off-chain discoverability only — no contract reads it. |

```solidity
uint256 public immutable MIN_PROTOCOL_FEE;   // wei — $0.005 at the native price on deploy day
uint256 public immutable MAX_PROTOCOL_FEE;   // wei — $0.10 at the native price on deploy day

uint256 public constant ORACLE_TIMELOCK = 2 days;

function getFee() external view returns (uint256);   // protocolFee in wei, as stored
function setProtocolFee(uint256 newFee) external onlyOwner;
function setTreasury(address newTreasury) external onlyOwner;
function setAgentId(uint256 newId) external onlyOwner;
function setSpendingLimitModule(address newModule) external onlyOwner;
function setFactory(address newFactory) external onlyOwner;

// Oracle changes are two-phase and delayed (see below)
function proposePriceOracle(address newOracle) external onlyOwner;
function commitPriceOracle() external onlyOwner;
function cancelPriceOracle() external onlyOwner;
address public pendingPriceOracle;
uint48 public pendingPriceOracleEta;
```

> **Storage is packed.** `protocolFee` is a `uint96` sharing a slot with `treasury` (so `_extractFee`'s two registry reads cost one storage read, not two), and `pendingPriceOracleEta` is a `uint48` sharing a slot with `pendingPriceOracle`. Both are wide enough: the constructor refuses a `MAX_PROTOCOL_FEE` past `uint96`, and a `uint48` timestamp lasts millions of years. The setters still take `uint256`. `agentId` stays `uint256` because an external registry mints it.

> **`setPriceOracle` was replaced by a two-phase timelock.** The oracle governs every wallet's spending cap, so repointing it is the single highest-impact action the operator key can take. `proposePriceOracle` records the candidate and starts a 2-day delay; `commitPriceOracle` applies it once the ETA passes; `cancelPriceOracle` withdraws it. `proposePriceOracle` also **rejects an oracle that cannot price native (`address(0)`)**, and **the constructor makes the same check on the initial oracle**. The module meters the native balance delta on every metered transaction, so an oracle without that feed would revert every native-moving execution on every deployed wallet. The check also catches a mistyped or wrong-network address, which has no `isPriced()` to call. See [THREAT_MODEL.md](../THREAT_MODEL.md) §3.8.

> **Why `spendingLimitModule` is not a constructor argument.** `SpendingLimitModule`'s own constructor reads `priceOracle()` off the registry — that check is what catches a mis-wired deployment (passing the oracle address where the registry belongs). So the registry must exist before the module can be deployed. Taking the module as a registry constructor argument would make the two mutually undeployable. The deploy script constructs the module against the live registry and then calls `setSpendingLimitModule`; `SHFactory.deployWallet` reverts with `SHFactory_SpendingLimitModuleNotSet` in the window between.

### Protocol fee

The fee is a **flat amount of native token, in wei**. `SessionHandler._extractFee` reads it off the registry and sends it — the fee path makes no oracle call:

```
SessionHandler._extractFee()
  ├─ SHRegistry.treasury()
  └─ SHRegistry.getFee()        // protocolFee, as stored (same storage slot as treasury)
```

It used to be a USD figure converted at execution time. That cost every session execution a Chainlink read (~18k gas, plus the sequencer check on an L2), and a stale ETH/USD feed halted *all* agent activity. A stored wei amount avoids both.

**Bounds are per chain.** The same wei is a different dollar amount on each chain (ETH vs BNB), so `MIN_PROTOCOL_FEE` and `MAX_PROTOCOL_FEE` are constructor arguments, not constants. `DeploySHProtocol` holds the dollar targets — **$0.005 floor, $0.10 ceiling, $0.015 starting fee** — and converts each to wei through the chain's native Chainlink feed at deploy time (`_usdToNative`). It reads the feed directly, skipping the oracle's staleness check, so a stale testnet feed cannot block a deploy. The constructor rejects a zero floor, a floor above the ceiling, a ceiling past `uint96`, and a starting fee outside the bounds.

What follows from a flat fee:

- **Its dollar value moves with the native price.** The operator re-sets it with `setProtocolFee` when it drifts too far.
- **The bounds drift too, and are permanent.** They are immutable, so if ETH doubles the range becomes ≈ $0.01–$0.20; if it halves, ≈ $0.0025–$0.05. The 20x gap is the room for that: the starting fee sits near the floor, so the price can triple before the floor forces the fee up, and fall ~6x before the ceiling forces it down. New bounds mean a new registry, which means redeploying the protocol.
- **The ceiling does not consult the oracle, on purpose.** The operator controls the oracle's feeds with no delay, so a ceiling checked in dollars could be lifted by first faking a low native price. A wei ceiling holds regardless.

Three more properties worth knowing:

- **Only session-key executions pay.** `execute` charges the fee when `msg.sender != owner()`; owner-initiated calls pay nothing. `executeFromExecutor` always charges.
- **The fee is outside the spending cap.** `_extractFee` runs *before* `_execute`, and the hook's `preCheck`→`postCheck` window opens inside `_execute` — so the fee never counts against the account's USD cap. This is deliberate: the cap meters what the *user* spends, and a protocol fee is not the user's spend. It does mean the fee is native outflow the cap cannot see, bounded per execution by `MAX_PROTOCOL_FEE` rather than by the cap.
- **A stale ETH/USD feed does not block the fee.** An ERC-20-only session execution never touches the native feed. See [THREAT_MODEL.md](../THREAT_MODEL.md) §3.7.

`totalFeesCollected` on `SHTreasury` sums **wei** collected across different native prices. It is not a dollar total and cannot be converted into one with a spot price after the fact.

> `router` / `setUniswapRouter` were **removed**: which DEX a wallet trades on is a wallet-level choice, not protocol configuration. Wallets now start with an empty trusted-spender list and the owner grants a router explicitly via `addTrustedSpender`.

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
receive() external payable;                                                   // totalFeesCollected += msg.value
function withdraw(address recipient, uint256 amount) external onlyOwner nonReentrant;
function withdrawAll(address recipient) external onlyOwner nonReentrant;

// Registry pass-through admin (all `registrySet`-guarded)
function setProtocolFee(uint256 newFee) external onlyOwner;
function setTreasury(address newTreasury) external onlyOwner;
function setAgentId(uint256 newId) external onlyOwner;
function proposePriceOracle(address newOracle) external onlyOwner;
function commitPriceOracle() external onlyOwner;
function cancelPriceOracle() external onlyOwner;

// Oracle pass-through admin (target passed in, so a pending oracle can be seeded)
function setFeed(address oracle, address token, address priceFeed, uint256 heartbeat) external onlyOwner;
function removeOracleFeed(address oracle, address token) external onlyOwner;

// Factory pass-through admin (target passed in, so multiple factories are supported)
function pauseFactory(address factory) external onlyOwner;
function unpauseFactory(address factory) external onlyOwner;

// Registry-held wallet config (the module lives on the registry, so one call covers every factory)
function setSpendingLimitModule(address newModule) external onlyOwner;
function setFactory(address newFactory) external onlyOwner;

// One-way migration: hands registry + oracle + factory to a successor owner or multi-sig.
// Does NOT move this contract's own ownership, nor redirect the fee stream — call setTreasury
// first if that is the intent, since afterwards this contract can no longer reach the registry.
function transferProtocol(address newOwner) external onlyOwner;

address public REGISTRY;          // write-once via setRegistry
uint256 public totalFeesCollected;
```

---

## `SHFactory.sol`

`SHFactory` is the user-facing entry point for deploying new `SessionHandler` wallets. It deploys a single `SessionHandler` implementation in its constructor, then `deployWallet(...)` creates each user's wallet as an **EIP-1167 minimal-proxy clone** and calls `initialize()` on it — which installs the configured `SpendingLimitModule` as a **hook**, seeds the wallet's spending-cap configuration, and applies the two owner-only grants (session key, trusted spenders) that used to need a follow-up transaction each. ETH sent with the call is forwarded to the new wallet as the initial gas prefund.

**One transaction is enough.** `deployWallet` takes the per-wallet spending-cap config *and* `sessionKey` + `sessionKeyValidUntil` + `trustedSpenders`, so a wallet is fully usable the moment it is deployed. The seeded key goes through the same internal grant as `addSession`, so a deploy cannot seed a key on terms an owner transaction would refuse. Previously onboarding was three owner-signed transactions (`deployWallet`, then `addSession`, then `addTrustedSpender`); those two functions remain for later re-grants but are no longer part of the deploy path. They cannot be batched through `execute` — see the two guards described under `SessionHandler` — which is why seeding them in `initialize` is the only way to get to one signature.

**Wallets are deployed with CREATE2.** The salt is `keccak256(abi.encode(msg.sender, deployCount[msg.sender]))`, so `predictWalletAddress(owner)` returns the address that owner's *next* deploy will produce, before it exists. Binding the owner into the salt is what makes that prediction safe to publish: no other caller can reach the same salt, so a predicted address cannot be squatted.

The nonce is a **per-owner** counter held by the factory, not a caller-supplied value and not a global one. A global counter (`totalWallets`, say) would make a predicted address depend on how many wallets *everyone else* had deployed, so any other user's deploy landing first would move it — which would defeat the point, since the address is what the wallet's session key is derived against. Two consequences worth knowing:

- A prediction is only valid for **one factory instance** — the deployer is baked into a CREATE2 address, so redeploying `SHFactory` invalidates every previously shown address.
- Calling `deployWallet` again simply consumes the next nonce and yields another wallet — one EOA may own several. A salt can never repeat, so there is no collision case and no "already deployed" error. **Enforcing one-wallet-per-user is the caller's job**: read `deployCount(owner)` and refuse when it is non-zero, which is a free view rather than a reverted transaction the user pays for. That check means "does this USER have a wallet" only when the user is `msg.sender` — i.e. when they sign their own deploy. A caller deploying on users' behalf from one shared EOA (as `app/deploy_wallet.py` does) sees a counter covering all its users and must track per-user ownership itself.

Knowing the address up front is also what lets an off-chain caller derive the wallet's **session key** before deploying it — the key's Vault entry is stored under the wallet address, so without CREATE2 it could not be minted until after the deploy that needs it as an argument.

The factory stores **no** protocol addresses of its own. EntryPoint, both ERC-8004 registries, and the `SpendingLimitModule` are read off the registry at `deployWallet` time, so correcting any of them is a single registry call rather than a factory redeploy. `SHFactory` itself owns only the clone implementation and the wallet index.

```solidity
constructor(
    address owner,      // SHTreasury — the admin root
    address _registry   // SHRegistry; every other address is read from it at deploy-wallet time
);

/// Deploys a new SessionHandler owned by msg.sender; forwards msg.value as ETH prefund.
/// dailyLimitUsd (18 decimals, >= 0), windowDuration (seconds, > 0), and watchedTokens (each
/// must already be priced by the oracle) seed the hook's onInstall config.
/// sessionKey is authorized on the new wallet (address(0) = owner-only wallet); every entry in
/// trustedSpenders is granted the unpriced-approval exemption (may be empty). Both are applied
/// inside initialize(), so no follow-up owner transaction is needed.
/// Salted with (msg.sender, deployCount[msg.sender]), which is consumed here — so calling twice
/// yields two distinct wallets rather than reverting.
/// Reverts with SHFactory_SpendingLimitModuleNotSet if the registry has no module set yet.
function deployWallet(
    int256 dailyLimitUsd,
    uint256 windowDuration,
    address[] calldata watchedTokens,
    address sessionKey,
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
uint64 public totalWallets;                // next walletId to assign (wallet IDs start at 0); packed beside _owner/_paused
mapping(uint256 => address) public wallets;

event WalletDeployed(address indexed walletAddress, address indexed owner, uint256 indexed walletId);
```

> `setSpendingLimitModule` moved to `SHRegistry` — every factory reads the module from there, so one call now covers them all. Note also that `totalWallets` starts at **0**, so the first wallet has id `0`; `wallets[0]` is a real wallet, not an "unset" sentinel.

---

## `SHOracle.sol`

The `SHOracle` converts native-asset and ERC-20 token amounts into real-time USD values using **Chainlink** price feeds. It accounts for stablecoin depeg events (e.g. USDC at $0.87 during the March 2023 SVB crisis) by querying actual market prices rather than assuming a 1:1 peg.

The module calls it directly (there is no interpreter layer). `getPrice` returns the **signed 18-decimal USD value** of an amount, and `isPriced` gates whether a token may be watched or approved through the hook.

**Supported tokens:** registered per-token via parallel `(address token, address priceFeed, uint256 heartbeat)` arrays passed to the constructor — native ETH/BNB uses `address(0)`. Pairs whose `priceFeed` is `address(0)` are silently skipped, so the same array shapes can be reused across networks. Each token's `decimals` is read once at construction and cached, so `getPrice` never makes an external `decimals()` call at valuation time.

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
```

---

## `SessionHandler.sol`

The `SessionHandler` is an **ERC-7579 smart account** (extends OpenZeppelin's `AccountERC7579Hooked`, plus `OwnableUpgradeable` and `Pausable`), deployed behind an EIP-1167 clone by `SHFactory`. It owns three responsibilities the module doesn't: **validating its own UserOps**, **managing the session-key allowlist**, and **guarding session-key executions** away from its own admin surface. `SpendingLimitModule` is installed as a **hook only** (no validator).

**Self-validation — no separate validator module.** `SessionHandler._validateUserOp` authenticates every UserOp itself instead of delegating to `AccountERC7579._validateUserOp`. It recovers the signer once from the EIP-191 envelope of the userOpHash (`toEthSignedMessageHash`, matching the bot and the Foundry helper) and branches: the **owner** validates unconditionally, **`currentSession`** validates with its `validUntil` packed into the returned validation data, and anything else returns `SIG_VALIDATION_FAILED`. Raw-signature validation stays at `AccountERC7579`'s default of `false`, so exactly one function decides who may sign.

Not delegating is deliberate. The base routes to a validator module named by the nonce key and otherwise falls back to `_rawSignatureValidation`; no validator module is ever installed here, so that branch is dead, and taking it would cost a second `ecrecover` just to learn which key signed. Recovering once is what lets an expired key fail as `AA22 expired or not due` instead of `AA24 signature error`. The accepted consequence: a validator module installed by the owner is ignored for UserOp validation (though `isValidSignature` would still consult it).

**Admin-surface guard.** For any non-owner execution (a session-key UserOp through the EntryPoint, or a self-call), `execute` runs `_guardSessionExecution`, which reverts if any single/batch sub-call targets `address(this)` or `address(SH_MODULE)`, and rejects `delegatecall` outright. This is what stops a session key from calling the module's cap setters, self-calling `uninstallModule` to delete the cap, minting more session keys, or delegatecalling arbitrary code. Owner-initiated calls skip the guard.

**Deliberate deviation from stock `AccountERC7579Hooked`,** which makes all three `onlyEntryPointOrSelf`. They are *not* reopened the same way:

- `execute` becomes `onlyEntryPointOrSelfOrOwner` — still reachable through the EntryPoint (that is how session keys act at all), with `_guardSessionExecution` restraining non-owner callers.
- `installModule` / `uninstallModule` become plain `onlyOwner`, which is strictly **tighter** than stock: an owner-signed UserOp arrives as `msg.sender == EntryPoint`, not the owner, so these are reachable only by a direct owner call and by **no UserOp at all**. That closes the path where a session key submits a UserOp aimed straight at `installModule` — bypassing `execute`'s guard entirely, since `execute` is never involved — to install a malicious validator or executor and escape the cap.

Session-key management and cap configuration are plain `onlyOwner`. Note that the module also refuses `execute`-routed calls to its own setters for *every* caller, owner included — see `SpendingLimitModule` below.

**One session key at a time, and it expires.** `addSession(key, validUntil)` / `removeSession()`. The wallet holds a single key in `currentSession`, packed with its deadline in `currentSessionValidUntil`; granting a different key **evicts** the previous one in the same transaction, and granting the same key again just extends it (no spurious `SessionRemoved`). `addSession` refuses `address(0)`, the owner's own address (the owner can already sign, so it would authorize nothing while evicting the live key), a deadline that has passed — **including zero, which the EntryPoint would read as "valid forever"** — and anything beyond `MAX_SESSION_TTL` (90 days).

One key rather than an allowlist is what keeps the wallet and whoever holds the key in step: a mapping cannot be enumerated, so nothing on chain could answer "which keys does this wallet trust?". `isSessionActive(key)` answers both halves at once, comparing with `<=` to match the EntryPoint exactly (an op is still valid in the second that equals the deadline).

Within its window a key is still a bare signer with no per-key target/selector scope: it can drive any external call, bounded by the spending cap, the guard, and the per-UserOp gas ceiling — optionally narrowed to a set of target addresses via `sessionTargetAllowlist` (off by default). A key (with its deadline) and a trusted router are normally seeded at deploy time by `SHFactory.deployWallet`; `addSession` / `addTrustedSpender` remain for re-granting afterwards.

**Nothing is trusted unless the deployer says so.** `initialize` grants exactly the `trustedSpenders` the caller passed — an empty array leaves the list empty, and `removeLiquidity`'s LP-token approval then fails until the owner grants a router. This is *not* a return to the old deploy-time auto-trust of `SHRegistry.router()`: that was protocol config choosing the venue, whereas this list comes from the deploying caller. Which venue a wallet trades on stays the owner's choice.

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
/// Installs the module as a HOOK with abi.encode(dailyLimitUsd, windowDuration, watchedTokens),
/// authorizes cfg.sessionKey if non-zero, and grants each cfg.trustedSpenders entry.
struct InitConfig {
    address owner;
    uint64 walletId;
    address spendingLimitModule;
    int256 dailyLimitUsd;
    uint256 windowDuration;
    address[] watchedTokens;
    address sessionKey;        // address(0) = owner-only wallet (not an error, unlike addSession)
    uint48 sessionKeyValidUntil; // the key's deadline; same rules as addSession (ignored for address(0))
    address[] trustedSpenders; // may be empty; granted AFTER the hook is installed
}

function initialize(InitConfig calldata cfg) external;

// Execution + module admin (owner escape hatch)
function execute(bytes32 mode, bytes calldata executionCalldata) public payable override whenNotPaused onlyEntryPointOrSelfOrOwner;
function installModule(uint256 moduleTypeId, address module, bytes calldata initData) public override onlyOwner;
function uninstallModule(uint256 moduleTypeId, address module, bytes calldata deInitData) public override onlyOwner;

// The wallet's ONE session key, packed into a single slot (owner-only to change)
address public currentSession;
uint48  public currentSessionValidUntil;
uint48  public constant MAX_SESSION_TTL = 90 days;
function addSession(address sessionKey, uint48 validUntil) external onlyOwner;
function removeSession() external onlyOwner;
function isSessionActive(address key) public view returns (bool);

// Spending-cap config passthroughs (owner-only; call the module as this account)
function setDailyLimit(int256 dailyLimitUsd) external onlyOwner;
function setWindowDuration(uint256 windowDuration) external onlyOwner;
function addWatchedToken(address token) external onlyOwner;
function removeWatchedToken(address token) external onlyOwner;
function addTrustedSpender(address spender) external onlyOwner;
function removeTrustedSpender(address spender) external onlyOwner;

// Cap views
function getConfig() external view returns (SpendingLimitModule.Config memory);
function isWatched(address token) external view returns (bool);
function isTrustedSpender(address spender) external view returns (bool);
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

// Per-UserOp gas ceiling (THREAT_MODEL 3.12) - the cap cannot see gas, these bound it
uint256 public constant DEFAULT_MAX_OP_GAS_COST = 0.1 ether;
uint80 public maxOpGasCost;                     // up to ~1.2M ETH
function setMaxOpGasCost(uint256 newMax) external onlyOwner;   // rejects 0 and > type(uint80).max

// Optional session-key target allowlist (THREAT_MODEL 3.13), off by default
mapping(address => bool) public sessionTargetAllowlist;
bool public sessionAllowlistEnabled;
uint32 public allowedTargetCount;
function toggleAllowList(bool enabled) external onlyOwner;      // refuses to enable while empty
function addAllowedTarget(address target) external onlyOwner;
function addAllowedTargets(address[] calldata targets) external onlyOwner;
function removeAllowedTarget(address target) external onlyOwner;

event SessionAdded(address indexed sessionKey, uint48 validUntil);
event SessionRemoved(address indexed sessionKey);
event MaxOpGasCostUpdated(uint256 oldMax, uint256 newMax);
event SessionAllowlistToggled(bool enabled);
event AllowedTargetAdded(address indexed target);
event AllowedTargetRemoved(address indexed target);
```

> **Shared values are immutables; per-wallet values are packed storage.** `ENTRY_POINT`, `REPUTATION_REGISTRY`, `IDENTITY_REGISTRY` and `REGISTRY` are the same for every wallet a factory deploys, so they are immutables of the implementation (set via `ProtocolAddresses`) and cost nothing to read. `SH_MODULE` stays in each wallet's storage because the operator can change the registry's module, and each wallet keeps the one it was deployed with. The rest is packed: `sessionAllowlistEnabled` and `maxOpGasCost` (`uint80`) share a slot with the inherited `_hook` and `_paused`, and `WALLET_ID` (`uint64`) and `allowedTargetCount` (`uint32`) share one with `SH_MODULE`. A session-key UserOp therefore reads two of these slots instead of six (mapping lookups aside). `forge inspect SessionHandler storageLayout` shows the assignment.

> The account currently has **no validator module**, and `_validateUserOp` never consults one: the nonce key the app sends is irrelevant to who may sign. Until a validator (an owner/ECDSA validator or Smart Sessions) is added — which would mean changing `_validateUserOp` — this self-validation path is the only way UserOps are authenticated.

---

## `SpendingLimitModule.sol`

`SpendingLimitModule` is an **ERC-7579 Hook (type 4 only — not a validator)** enforcing a global, USD-denominated spending cap per rolling window across every venue and every key on the installing account. One deployed instance serves every account; all state is keyed by `account` (`msg.sender` in the hook callbacks). It never authenticates anyone — authentication is the account's job.

**Config (per account):**

```solidity
struct Config {
    bool installed;              // gates every hook and setter
    uint48 windowStart;          // timestamp anchoring the current window
    uint48 windowDuration;       // seconds
    int256 dailyLimitUsd;        // 18-decimal USD cap per window
    int256 spentInWindow;        // 18-decimal USD net value spent this window
    address[] watchedTokens;     // tokens whose value changes are metered (max 32)
    address[] trustedSpenders;   // spenders approvable even for unpriced tokens (max 16)
}
```

**How metering works (net value, not gross outflow):**

- **`preCheck`** rejects any execution that targets the module's own admin surface (see below), rolls the spending window if expired, collects any allowance-granting sub-calls — `approve` and legacy `increaseAllowance` — (rejecting unlimited approvals and unpriced-to-untrusted approvals up front), and snapshots the raw balance of every watched token. **No oracle calls happen here.**
- **`postCheck`** does two independent checks:
  1. **Net-value spend.** For each watched token whose balance *changed*, it prices the delta via `SHOracle` and sums the signed differences across the portfolio; any net USD **decrease** is added to `spentInWindow`, reverting `BudgetExceeded` if it crosses `dailyLimitUsd` and emitting `SpendMetered` once the spend stands. A token that didn't move is never priced (so a stale feed on an untouched token can't block a transaction), and a net increase adds nothing, never banks credit for a later transaction, and emits nothing.
  2. **No standing approvals.** Any allowance approved in this transaction must be consumed to **exactly 0** by the end of it, else revert `StandingApprovalNotAllowed`. This closes deferred-pull risk outright rather than size-bounding it.

Because spending *is* the drop in watched-portfolio USD, the module needs no per-venue swap decoder: a value-neutral swap nets ~0, and a bad-rate or sandwiched swap registers its lost value automatically. Both `execute` and `executeFromExecutor` calldata are decoded for approvals; delegatecalls are not decoded (no reliable selector), but the net-value cap still applies to them.

**Trusted-spender exemption (unpriced approvals).** An `approve` on a token the oracle can't price normally reverts (`TokenNotPriced`) — *unless* the spender is on the account's `trustedSpenders` list. This is the escape hatch that lets a Uniswap V2 **LP token** (which has no Chainlink feed) be approved to the router for `removeLiquidity`. The no-standing-approval and no-unlimited-approval rules still apply in full to trusted spenders — the exemption only skips the price check. The list starts empty unless the deployer seeds one: `SHFactory.deployWallet`'s `trustedSpenders` argument grants them inside `initialize` (this is how the bot grants the router now, from `constants.get_router`). `addTrustedSpender` remains for granting one later.

**Self-guard on the module's admin surface.** `preCheck` reverts `AdminExecution` if any sub-call of the execution targets the module itself with one of its own selectors (the six config setters, `onInstall`, `onUninstall`). It runs *first*, before approvals are collected, so a refused transaction pays almost nothing. It is defence-in-depth that holds even on a host account with no guard of its own — and it deliberately blocks the **owner's** `execute` path too, because by the time `preCheck` runs `msg.sender` is the account whether an owner or a session key drove it, leaving no way to tell them apart. Nothing legitimate is lost: the owner reaches the six setters through `SessionHandler`'s `onlyOwner` passthroughs (a direct call, so no hook runs), and `onInstall`/`onUninstall` through `installModule`/`uninstallModule`, whose outer calldata is not an `execute` selector and so never reaches the check.

**Where the oracle comes from.** The module stores an immutable `REGISTRY` but resolves the oracle itself from `SHRegistry.priceOracle()` on every valuation, so an oracle bug can be fixed for every already-deployed wallet at once without redeploying the module or migrating any wallet. The trade-off is a trust assumption: the registry owner (`SHTreasury` → the protocol operator) can repoint every wallet's oracle in one transaction — see [THREAT_MODEL.md](../THREAT_MODEL.md) §3.8. Resolution is deliberately per-use rather than cached in a local, so a path that never prices anything (a trusted-spender approval, a watched-token add of `address(0)`) makes no registry read at all; re-reading is cheap after the first, as the registry account and slot stay warm for the rest of the transaction.

```solidity
constructor(address registry);  // SHRegistry address — NOT the oracle; reverts OracleNotSet if it reports none

// IERC7579Module
function onInstall(bytes calldata data) external;    // decodes (int256 dailyLimitUsd, uint256 windowDuration, address[] watchedTokens)
function onUninstall(bytes calldata) external;        // wipes all per-account state
function isModuleType(uint256 moduleTypeId) external pure returns (bool);   // true only for MODULE_TYPE_HOOK (4)

// Hook callbacks (called by the account around _execute)
function preCheck(address, uint256, bytes calldata msgData) external returns (bytes memory hookData);
function postCheck(bytes calldata hookData) external;

// Config setters (onlyInstalled — the account calls these as itself, via SessionHandler passthroughs)
function setDailyLimit(int256 dailyLimitUsd) external;   // 0 <= limit <= type(int128).max
function setWindowDuration(uint256 windowDuration) external;
function addWatchedToken(address token) external;
function removeWatchedToken(address token) external;
function addTrustedSpender(address spender) external;
function removeTrustedSpender(address spender) external;

// Per-account state. Field order follows storage packing: the first four fields share ONE slot.
struct Config {
    bool installed;
    uint48 windowStart;
    uint48 windowDuration;
    int128 spentInWindow;      // 18-decimal USD, running total for the current window
    int128 dailyLimitUsd;      // 18-decimal USD
    address[] watchedTokens;
    address[] trustedSpenders;
}

// Views (keyed by account)
function getConfig(address account) external view returns (Config memory);   // read by field NAME
function isWatched(address account, address token) external view returns (bool);
function isTrustedSpender(address account, address spender) external view returns (bool);
function getRemainingBudget(address account) external view returns (int256);

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

**Errors:** `AlreadyInstalled`, `NotInstalled`, `InvalidDailyLimit`, `InvalidWindowDuration`, `TokenNotPriced(token)`, `TooManyWatchedTokens`, `TooManyTrustedSpenders`, `InvalidTrustedSpender`, `UnlimitedApprovalRejected`, `StandingApprovalNotAllowed(token, spender, residual)`, `BudgetExceeded(spentUsd, dailyLimitUsd)`, `AdminExecution`, `OracleNotSet` — each prefixed `SpendingLimitModule_`.

**The running total shares a slot with the install flag and the window.** `preCheck` already reads that slot, so `postCheck` reads the total cheaply, a window reset writes the new start and the zeroed total in one store, and writing the total never pays the 20,000-gas cost of filling an empty slot. Both money values are `int128` (~$1.7e20 at 18 decimals); the stored total can never be cut off because `setDailyLimit` keeps the limit inside `int128` and `postCheck` checks the new total against the limit *before* storing it. The per-transaction outflow is still computed at full `int256`. Measured on a session-key token transfer: about −1,900 gas on a normal spend, −4,900 on a spend that starts a new window, and −19,000 on a wallet's first spend. Because the field order follows the packing, read `getConfig()` by field name, never by position — the Python app uses `contracts.read_spending_config`.

**Native value IS metered.** preCheck snapshots the account's native balance and postCheck prices its net change through the `address(0)` sentinel feed, alongside the watched tokens — so a native send, and the native leg of a swap (e.g. `swapExactETHForTokens`), count against the cap. Gas is excluded: the ERC-4337 prefund leaves the account before preCheck and the refund settles to the EntryPoint deposit after postCheck, never touching the metered delta.

**Deliberate v1 exclusions** (per design, not gaps): Permit2/permit-signature approvals and per-venue rate limits. Any unwatched ERC-20 sits outside the meter — the protection model is "watch the tokens you care about" (native and watched tokens are always metered).

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

`HelperConfig` resolves chain-specific deployment parameters at runtime. Its `NetworkConfig` struct carries ~22 tokens and their Chainlink USD price feeds + heartbeats, plus the L2 sequencer uptime feed and the ERC-8004 registry addresses. (The router is **not** protocol config — which venue a wallet trades on is a wallet-level choice; the fork suite reads it straight from `Constants.s.sol`, as an owner would.)

| Network | Chain ID | EntryPoint | Router (from Constants) | Sequencer uptime feed |
|---|---|---|---|---|
| Ethereum Mainnet | 1 | `ENTRYPOINT_V07` (canonical) | Uniswap V2 | none (`address(0)`) |
| Ethereum Sepolia | 11155111 | `ENTRYPOINT_V07` (canonical) | Uniswap V2 | none (`address(0)`) |
| BSC | 56 | `ENTRYPOINT_V07` (canonical) | PancakeSwap V2 | none (`address(0)`) |
| Arbitrum One | 42161 | `ENTRYPOINT_V07` (canonical) | Uniswap V2 | `ARB_SEQUENCER_UPTIME_FEED` |
| Anvil (local) | 31337 | Freshly deployed, cached per session | none (`address(0)`) | none (`address(0)`) |

`getConfigByChainId` **reverts with `HelperConfig__InvalidChainId`** on an unrecognised chain ID, rather than silently handing back mainnet's token and feed addresses — which would be wrong for that chain. For Anvil, `HelperConfig` deploys a fresh `EntryPoint`, `MockV3Aggregator` feeds seeded with approximate real-world prices, and the ERC-8004 mocks (all inside `vm.startBroadcast`, so the mock token addresses land on-chain and are recoverable from the broadcast file). Sepolia uses a wide 72h heartbeat across the board (its Chainlink nodes update far less often than mainnet's — an accepted testnet characteristic). The mainnet/BSC/Arbitrum deployer resolves from `MAINNET_DEPLOYER_ADDRESS`, falling back to a deterministic placeholder for fork tests — export the real one before a live deployment.

**Arbitrum coverage.** 16 of the ~22 tokens carry both an official Arbitrum deployment and a Chainlink USD feed. The rest are zeroed **in pairs** — token *and* feed together — because `DeploySHProtocol` pairs the arrays positionally and `SHOracle` reads a zero token as the native-ETH sentinel, so a zero token beside a live feed would silently repoint native pricing at it. ENS, SAND, IMX and KNC have no Arbitrum feed; BNB, AVAX and wTAO have feeds but no credible token deployment on the chain.

---

## `DeploySHProtocol.s.sol`

Orchestrates deployment of all shared infrastructure. Individual `SessionHandler` wallets are not deployed here — users call `SHFactory.deployWallet(...)`.

**Deployment sequence:**

1. Instantiate `HelperConfig`; build parallel `(tokens, priceFeeds, heartbeats)` arrays. Convert the fee's dollar targets (`MIN_PROTOCOL_FEE_USD`, `MAX_PROTOCOL_FEE_USD`, `INITIAL_PROTOCOL_FEE_USD`) to wei through the native feed, `priceFeeds[0]`.
2. Deploy `SHTreasury()` — the admin root, deployed **first** so its address can own everything below.
3. Deploy `SHOracle(address(treasury), config.sequencerUptimeFeed, tokens, priceFeeds, heartbeats)` — born owned by the treasury.
4. Call `IIdentityRegistry.register(AGENT_URI)` to mint the agent NFT and obtain `agentId`.
5. Deploy `SHRegistry(address(treasury), initialFee, minFee, maxFee, address(treasury), address(oracle), …, agentId)` — owned by the treasury and paying fees to it.
6. Call `treasury.setRegistry(address(registry))` — write-once, fixing the pairing.
6b. Deploy `SpendingLimitModule(address(registry))` — wired to the **registry**, from which it resolves the current oracle on every valuation (no interpreter). Must come after the registry: its constructor reads `priceOracle()` off it.
7. Call `treasury.setSpendingLimitModule(address(module))` — the module could not be a registry constructor argument (see the registry section), so it is registered here.
8. Deploy `SHFactory(address(treasury), address(registry))` — just owner and registry. Every other address a wallet needs is read off the registry at `deployWallet` time, so the factory stores none of them. It deploys the `SessionHandler` implementation it clones from internally.
9. Call `treasury.setFactory(address(factory))` — records the factory on the registry for off-chain discoverability.

No `transferOwnership` step is needed anywhere, and at no point does the deployer EOA own a live contract other than the treasury.

```solidity
function run() external returns (SHFactory factory, SHTreasury treasury, HelperConfig.NetworkConfig memory config, SHOracle oracle);
```

---

## `SendPackedUserOp.s.sol`

A reusable helper for constructing signed `PackedUserOperation`s, used by the test suite. Because the account installs no validator module, UserOp validation always falls through to the account's own `_rawSignatureValidation`, so this helper signs with **whatever key it's given** — the same flow covers owner-signed and session-key-signed ops.

**Signing flow:**
1. Fetch nonce from `EntryPoint.getNonce(sender, 0)`. (The nonce key's top bytes select a validator module; `address(0)` is never installed, so the account always falls back to self-validation. Any key value behaves identically — key 0 is the canonical choice.)
2. Pack the execution into `execute(mode, executionCalldata)` calldata — single-call or batch.
3. Get `userOpHash` from `EntryPoint.getUserOpHash(userOp)`.
4. Wrap in the EIP-191 envelope via `toEthSignedMessageHash` (matching `_rawSignatureValidation`).
5. Sign the digest and attach `(r, s, v)`.

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
```

---

## Test Suite

Totals: **60** unit + **13** guard + **8** invariant (local), and **33** fork (11 × 3 networks). All passing.

**`test/unit/SHProtocolTest.t.sol` (61 tests)** — deploy/factory config, module lifecycle (install/uninstall/reinstall, `isModuleType` hook-only), config setters (limit/window/watched-token cap, non-owner reverts), session-key allowlist, **net-value metering** (transfer pricing, inflow-offset within a tx, no-banked-credit across txs, window roll, per-token staleness isolation), **approvals** (unlimited rejected, standing reverts, consumed-in-same-tx passes, partial reverts, approve-then-zero), **trusted spenders** (Option C: unpriced approval allowed when trusted / reverts when untrusted, unlimited still rejected, standing still reverts, remove reinstates the price gate, uninstall clears), plus the harness calldata/approval-classifier tests. Uses a `MockSpender` to consume allowances mid-batch.

**`test/unit/SessionGuardTest.t.sol` (52 tests)** — drives real `EntryPoint.handleOps` end-to-end. A wallet deployed with `deployWallet`'s `sessionKey` argument is driven by that key with no `addSession` call anywhere (`test_deployWallet_seededSessionKeyExecutesImmediately`). Session-key UserOps attempting `uninstallModule`, `setDailyLimit`, `addSession`, or a batch smuggling a restricted target all fail with the admin state asserted **unchanged**; direct EntryPoint-pranked calls prove the exact guard errors (`SessionHandler_SessionRestrictedTarget`, `SessionHandler_SessionDelegateCallForbidden`); owner-direct `execute` bypasses the account's guard but is still refused by the module's own (`SpendingLimitModule_AdminExecution`, single and batch); and unknown/removed signers fail validation with `AA24`.

Expiry is proved through the same real `handleOps` path: a key past its deadline fails `AA22 expired or not due` (not `AA24`), is accepted in the second that *equals* the deadline, works again after renewal, and an **evicted** key fails `AA24` because it is no longer the wallet's key at all. An owner-signed op still executes a year after the session key lapsed — the owner carries no validity window.

> **Gotcha:** the vendored account-abstraction is EntryPoint **v0.9**, whose `nonReentrant` requires `tx.origin == msg.sender` — tests must submit `handleOps` with a two-arg `vm.prank(bundler, bundler)` (EOA bundler) or it reverts `Reentrancy()`.

**`test/fork/SHForkTestBase.sol` (10 tests, abstract)** + thin per-network instances **`SHUniswapV2Test`** (mainnet, WETH→DAI), **`SHSepoliaUniswapV2Test`** (Sepolia, WETH→USDC), and **`SHPancakeswapV2Test`** (BSC, USDT→WBNB→LINK multi-hop). Runs identically against the real router, live Chainlink feeds, and the canonical EntryPoint: wallet deploys with cap config and the owner trusts the chain's router (from Constants.s.sol); net-value metering at live prices; budget-exceeded reverts atomically; a real **session-key swap** metered by net portfolio change; standing/unlimited approvals rejected on a real DEX; a session op targeting the module reverts; a revoked key fails validation; and a full **`addLiquidity → removeLiquidity` round-trip** proving the unpriced LP-token approval flows through the trusted router (BSC exercises the router's create-pair path). Metering assertions are computed from actual balance diffs priced through the deployed oracle, so they hold despite testnet pool-ratio divergence.

**`test/invariant/` (8 invariants)** — `SHHandler` fuzzes owner executes (transfers, mint+transfer batches), config changes, non-owner attacks, session-key management, and time warps, maintaining a **ghost model** that independently replicates the module's roll-then-accumulate net metering. `InvariantSH` asserts, among others: `invariant_meteringMatchesGhostModel` (module `spentInWindow` equals the ghost exactly), spend never negative, config only ever changed by the owner, the hook stays installed, the watched list stays bounded/consistent, remaining-budget consistency, and the session allowlist matches ghost bookkeeping.

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
make mainnet-uniswap-test    # Uniswap V2, mainnet fork
make sepolia-uniswap-test    # Uniswap V2, Sepolia fork  (make sepolia-test is an alias)
make pancakeswap-test        # PancakeSwap V2, BSC fork
make arbitrum-uniswap-test   # Uniswap V2, Arbitrum One fork (+ the live sequencer gate)

# Deploy shared protocol infrastructure
forge script script/DeploySHProtocol.s.sol \
  --rpc-url $SEPOLIA_RPC_URL \
  --account <keystore-account> \
  --broadcast \
  --verify
```
