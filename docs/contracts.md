# Smart Contract Architecture

The smart contract layer implements the full SessionHandler Protocol: shared infrastructure contracts deployed once per chain, and per-user `SessionHandler` smart wallets deployed on demand through `SHFactory`.

## Contract Overview

```
src/
├── SHFactory.sol              ← User-facing factory — deploys one SessionHandler per user
├── SHTreasury.sol             ← Protocol fee collector — owns and administers SHRegistry
├── SHRegistry.sol             ← Central config store (fee, treasury, oracle, agentId, router, interpreter)
├── SHOracle.sol               ← Chainlink-based USD value converter
├── SHValueInterpreter.sol     ← Decodes session-key calldata → USD debit/credit values
├── SessionHandler.sol         ← ERC-4337 smart account with session key logic
├── interfaces/
│   ├── IWETH.sol              ← WETH interface (extends IERC20Extended)
│   ├── IERC20Extended.sol     ← IERC20 + IERC20Metadata combined interface
│   ├── IIdentityRegistry.sol  ← ERC-8004 IIdentityRegistry interface
│   └── IReputationRegistry.sol← ERC-8004 IReputationRegistry interface
└── mocks/
    ├── MockIdentityRegistry.sol  ← Full ERC-8004 Identity Registry mock (local testing)
    ├── MockReputationRegistry.sol← ERC-8004 Reputation Registry mock (local testing)
    ├── ERC20Mock.sol             ← Mintable ERC20 for local testing
    ├── MockV3Aggregator.sol      ← Chainlink price feed mock for Anvil
    └── MockWeth.sol              ← WETH mock with deposit/withdraw for Anvil

script/
├── DeploySHProtocol.s.sol    ← Deployment entry point (SHOracle → SHTreasury → SHValueInterpreter → SHFactory)
├── Constants.s.sol            ← Shared mainnet/Sepolia contract addresses
├── HelperConfig.s.sol         ← Chain-specific configuration resolver
└── SendPackedUserOp.s.sol     ← UserOp construction and signing helper

test/
├── unit/
│   ├── SHProtocolTest.t.sol        ← Full SessionHandler unit test suite
│   └── SessionHandlerHarness.sol   ← Test harness exposing internal functions
├── fork/
│   ├── SHUniswapV2Test.t.sol   ← Uniswap V2 integration tests (mainnet fork)
│   └── SHSepoliaTest.t.sol     ← SessionHandler integration tests (Sepolia fork)
└── invariant/
    ├── InvariantSH.t.sol  ← Stateful invariant tests
    └── SHHandler.sol      ← Action handler for fuzzing
```

---

## `SHRegistry.sol`

The `SHRegistry` is the central configuration store for the entire protocol. All deployed `SessionHandler` wallets read their runtime parameters — protocol fee, treasury address, price oracle, Uniswap router, value interpreter, and agent identity — from this single contract. This means any protocol parameter can be updated by the operator without redeploying any user wallets.

`SHRegistry` is owned by `SHTreasury`. All admin functions on the registry are exposed through `SHTreasury`'s pass-through setters — operators never call `SHRegistry` directly.

**Stored parameters:**

| Parameter | Type | Purpose |
|---|---|---|
| `protocolFee` | `uint256` | Flat ETH fee charged on every session-key execution (capped at `MAX_PROTOCOL_FEE = 0.001 ether`) |
| `treasury` | `address` | Destination for protocol fee payments — `SHTreasury` |
| `priceOracle` | `address` | Canonical `SHOracle` address for USD accounting |
| `agentId` | `uint256` | ERC-8004 token ID of the registered protocol agent |
| `uniswapRouter` | `address` | Uniswap V2 Router; may be `address(0)` on chains without V2 |
| `callValueInterpreter` | `address` | `SHValueInterpreter` address for calldata → USD computation |

```solidity
uint256 public constant MAX_PROTOCOL_FEE = 0.001 ether;

function setProtocolFee(uint256 newFee) external onlyOwner;
function setTreasury(address newTreasury) external onlyOwner;
function setPriceOracle(address newOracle) external onlyOwner;
function setAgentId(uint256 newId) external onlyOwner;
function setUniswapRouter(address newRouter) external onlyOwner;
function setCallValueInterpreter(address newInterpreter) external onlyOwner;
```

---

## `SHTreasury.sol`

`SHTreasury` is the protocol's operator and fee-sink contract, fulfilling two coupled responsibilities: it acts as the terminal receiver in a push-based settlement pipeline, accepting a per-execution ETH debit from every deployed `SessionHandler` instance via a low-level `call`, and it holds exclusive write-capability over the `SHRegistry` singleton. Self-referential construction — instantiating `SHRegistry` from within the `SHTreasury` constructor — establishes `address(this)` as the canonical owner and fee-destination atomically at deploy time, eliminating a separate ownership-transfer step and its associated trust window.

**Fee flow:**
```
SessionHandler.execute() → payable(REGISTRY.treasury()).call{value: fee}() → SHTreasury.receive()
```

**Admin flow:**
```
Protocol operator → SHTreasury.set*() → SHRegistry.set*()
```

```solidity
constructor(
    uint256 initialFee,      // starting protocol fee in wei
    address priceOracle,     // SHOracle address
    uint256 initialAgentId,  // ERC-8004 agent token ID
    address uniswapRouter    // address(0) on chains without Uniswap V2
);

// Fee management
function withdraw(address recipient, uint256 amount) external onlyOwner;
function withdrawAll(address recipient) external onlyOwner;

// Registry pass-through admin
function setProtocolFee(uint256 newFee) external onlyOwner;
function setPriceOracle(address newOracle) external onlyOwner;
function setTreasury(address newTreasury) external onlyOwner;
function setAgentId(uint256 newId) external onlyOwner;
function setUniswapRouter(address newRouter) external onlyOwner;
function setCallValueInterpreter(address newInterpreter) external onlyOwner;

address public immutable REGISTRY;
uint256 public totalFeesCollected;
```

---

## `SHFactory.sol`

`SHFactory` is the user-facing entry point for deploying new `SessionHandler` wallets. Calling `deployWallet()` deploys a new `SessionHandler` owned by `msg.sender` and wired to the shared protocol infrastructure. ETH sent with the call is forwarded to the new wallet as the initial gas prefund.

`SHFactory` stores the `EntryPoint`, `SHRegistry`, `IdentityRegistry`, and `ReputationRegistry` addresses as immutables, baking them into every `SessionHandler` it deploys.

```solidity
constructor(
    address _entryPoint,
    address _feeRegistry,        // SHRegistry address
    address _reputationRegistry, // ERC-8004 ReputationRegistry
    address _identityRegistry    // ERC-8004 IdentityRegistry
);

// Deploys a new SessionHandler owned by msg.sender; forwards msg.value as ETH prefund.
function deployWallet() external payable whenNotPaused returns (address);

event WalletDeployed(address indexed walletAddress, address indexed owner);
```

---

## `SHValueInterpreter.sol`

`SHValueInterpreter` decodes session-key calldata and converts the involved token amounts to USD. It is called by `SessionHandler.execute()` to compute the debit or credit value of each session-key operation before enforcing the spending limit.

Extracting this logic into a standalone contract allows the oracle and Uniswap router addresses to be updated in `SHRegistry` without redeploying any user wallets. The interpreter reads both from `SHRegistry` at call time.

**Supported operations:**

| Operation | USD treatment |
|---|---|
| Native ETH send | `value` priced via ETH/USD feed |
| ERC-20 `transfer` / `transferFrom` | token `amount` priced via token/USD feed |
| Uniswap V2 swaps | exact input or exact output amount priced at the input token |
| `addLiquidity` / `addLiquidityETH` | both deposit amounts priced and summed |
| `removeLiquidity` / `removeLiquidityETH` | returns a `creditValueInUSD` (budget credit, not debit) |
| WETH `deposit` | ETH component excluded (deposit just wraps — no net spend) |

```solidity
constructor(address registry);  // SHRegistry address

// Returns (debitValueInUSD, creditValueInUSD) — creditValueInUSD non-zero only for removeLiquidity variants.
function computeUSDValue(
    address dest,
    uint256 value,
    bytes memory data,
    bytes4 selector
) external view returns (uint256 debitValueInUSD, uint256 creditValueInUSD);
```

---

## `SHOracle.sol`

The `SHOracle` converts ETH and ERC20 token amounts into real-time USD values using Chainlink price feeds. It is called by `SHValueInterpreter` to enforce USD-denominated spending limits rather than raw token amounts.

This design accounts for stablecoin depeg events (e.g., USDC at $0.87 during the March 2023 SVB crisis) by querying actual market prices rather than assuming a 1:1 peg.

**Supported tokens (21 registered by Forge deploy script; 26 in mainnet `HelperConfig`):** ETH (via `address(0)`), USDC, DAI, WETH, USDT, AAVE, LINK, 1INCH, APE, ARB, BNB, WBTC, COMP, CRV, ENS, MKR, SAND, SUSHI, wTAO, UNI, YFI. The mainnet `HelperConfig` additionally provides WAVAX, BAT, IMX, KNC, and RDNT. The Python `deploy.py` path registers all 26 on mainnet.

Token-to-feed mappings are stored in `mapping(address => address) private s_priceFeed`, and per-feed staleness thresholds in `mapping(address => uint256) private s_heartbeat`. Entries with `address(0)` feeds are silently skipped, allowing safe deployment on chains with partial feed availability.

**Per-feed staleness protection:** Reverts with `SHOracle_StalePrice` if a feed has not updated within its registered heartbeat. Heartbeats: 1 hour for ETH, BTC, AAVE, LINK, DAI, COMP, MKR, UNI, WETH; 23 hours for USDC; 24 hours for all others.

```solidity
constructor(
    address[] memory tokens,
    address[] memory priceFeeds,
    uint256[] memory heartbeats
);

function getUSDValue(address token, uint256 amount) external view returns (uint256);
function getPrice(address token) external view returns (uint256 price, uint8 decimals);
```

---

## `SessionHandler.sol`

The `SessionHandler` is an ERC-4337-compliant smart account (implements `IAccount`) that supports both owner-signed and session-key-signed `UserOperation`s. It inherits `Ownable`, `ReentrancyGuard`, and `Pausable` from OpenZeppelin.

Each `SessionHandler` reads all protocol parameters from `SHRegistry` at execution time rather than storing them as immutables. This means an operator updating the oracle, fee, or interpreter address in `SHRegistry` propagates to every deployed `SessionHandler` instantly.

**Key features:**

| Feature | Detail |
|---|---|
| ERC-4337 v0.7 compatible | Implements `validateUserOp` with packed gas fields |
| Session time windows | 48-bit `validFrom` / `validUntil` timestamps |
| Spending limits | USD-denominated per-session cumulative cap via `SHValueInterpreter` + `SHOracle` |
| Selector whitelisting | O(1) `mapping(address => mapping(bytes4 => bool))` lookup |
| Uniswap V2 support | Assembly-based calldata parsing for 4 token-input swap functions plus `addLiquidity`, `addLiquidityETH`, `removeLiquidity`, `removeLiquidityETH`; ETH-input swaps are budget-accounted via the `value` field |
| Native ETH sends | `address(0)` session target sentinel — allows ETH transfers to arbitrary recipients |
| Protocol fee | Charges a flat ETH fee (`REGISTRY.protocolFee()`) to `REGISTRY.treasury()` on every session-key execution |
| Owner revocation | `revokeSessionKey` cleans up mappings and resets storage |
| Owner withdrawal | `withdraw(token, amount)` allows the owner to pull ERC20 tokens or ETH from the wallet |
| Agent identity | `getAgentIdentity()` and `getAgentReputation()` proxy to the ERC-8004 registries |

**Session struct:**

```solidity
struct Session {
    bool active;            // session control switch (auto-activates when validFrom passes)
    address target;         // whitelisted target contract; address(0) = native ETH send
    uint48 validFrom;       // activation timestamp
    uint48 validUntil;      // expiry timestamp
    uint256 spendingLimit;  // max cumulative USD spend (18 decimals)
    uint256 spentAmount;    // running total of USD spent
    bytes4[] selectors;     // whitelisted function selectors
}
```

**EIP-1153 transient storage bridge:**

`validateUserOp` and `execute` run as two separate calls within the same `handleOps` transaction. Two EIP-1153 transient slots bridge the two steps:

```solidity
address transient tPendingSessionKey;
bytes4  transient tPendingSelector;
```

USD computation is deferred entirely to `execute()` via `SHValueInterpreter.computeUSDValue()`, because oracle reads (external storage) are forbidden during validation. The transient slots are zeroed automatically at transaction end.

**Signature validation flow (`validateUserOp`):**

1. Recover the signer from the EIP-191 wrapped `userOpHash` using ECDSA.
2. If signer is the **owner** — return `SIG_VALIDATION_SUCCESS` immediately.
3. If signer is in `sessionExists` — call `_validateSession`:
   - Native ETH send (`data.length == 0 && value > 0`): assert session target is `address(0)`, write transient storage, return packed time bounds.
   - `dest == REPUTATION_REGISTRY`: allow any selector — reputation sessions are unrestricted by function.
   - Otherwise: assert `dest` matches session target, extract selector, assert `_isSessionUsable` and `isSelectorAllowed`, write transient storage, return packed time bounds.
4. Otherwise — return `SIG_VALIDATION_FAILED`.

**USD computation and fee collection (`execute`):**

When called by the EntryPoint with `tPendingSessionKey != address(0)`, calls `SHValueInterpreter.computeUSDValue()` to get `(debitValueInUSD, creditValueInUSD)`, enforces the spending limit, then dispatches the inner call. After success, collects the flat protocol fee to the treasury.

**`removeLiquidity` budget accounting:**

For `removeLiquidity` variants, `computeUSDValue` returns a non-zero `creditValueInUSD` and zero `debitValueInUSD`. `execute` credits back up to the current `spentAmount` rather than charging — LP removal recovers value, it does not spend it.

**Key functions:**

```solidity
function addSessionKey(address sessionKey, address target, bytes4[] calldata selectors, uint48 validFrom, uint48 validUntil, uint256 spendingLimit) external onlyOwner;
function revokeSessionKey(address sessionKey) public onlyOwner;
function execute(address dest, uint256 value, bytes calldata data) external onlyEntryPointOrOwner whenNotPaused;
function withdraw(address token, uint256 amount) external onlyOwner;

function getSession(address sessionKey) public view returns (Session memory);
function isSessionActive(address sessionKey) public view returns (bool);
function getRemainingBudget(address sessionKey) public view returns (uint256);
function isSpendingWithinBudget(address sessionKey, address token, uint256 amount) public view returns (bool);
function getPrice(address token) public view returns (uint256 price, uint8 decimals);
function getAgentId() public view returns (uint256);
function getAgentIdentity() public view returns (bool registered, uint256 agentId, string memory agentUri);
function getAgentReputation() public view returns (uint256 agentId, uint64 feedbackCount, int128 summaryValue, uint8 summaryValueDecimals);
function getUniswapRouter() public view returns (address);
```

---

## ERC-8004 Infrastructure

The project integrates the **ERC-8004** standard for on-chain agent identity and reputation.

**Canonical registries (Sepolia / Mainnet):** On live networks, the UUPS-upgradeable registries deployed by the ERC-8004 working group are used. Their addresses are baked into `SHFactory` and `SessionHandler` at deployment time via `HelperConfig`.

| Contract | Sepolia | Mainnet |
|---|---|---|
| `IdentityRegistry` | `0x8004A818BFB912233c491871b3d84c89A494BD9e` | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| `ReputationRegistry` | `0x8004B663056A597Dffe9eCcC1965A193B7388713` | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` |

**`src/mocks/MockIdentityRegistry.sol`** — full non-upgradeable mock of the ERC-8004 Identity Registry for Anvil and unit tests. Implements all three `register()` overloads, `setAgentWallet` (EIP-712 + ERC-1271), `setMetadata`, `setAgentURI`, `getAgentWallet`, and `isAuthorizedOrOwner`. Clears `agentWallet` metadata on NFT transfer.

**`src/mocks/MockReputationRegistry.sol`** — mock of the ERC-8004 Reputation Registry for Anvil and unit tests.

**`src/interfaces/IIdentityRegistry.sol`** — canonical ERC-8004 identity interface: `register()` (3 overloads), `setAgentURI`, `setMetadata`, `getMetadata`, `setAgentWallet`, `getAgentWallet`, `unsetAgentWallet`, `tokenURI`, `ownerOf`, `balanceOf`, `isAuthorizedOrOwner`.

**`src/interfaces/IReputationRegistry.sol`** — canonical ERC-8004 reputation interface: `giveFeedback`, `getSummary`, `readAllFeedback`.

**Agent registration** — `DeploySHProtocol.s.sol` calls `IIdentityRegistry.register(AGENT_URI)` during deployment, minting the agent's ERC-721 identity NFT. The returned `agentId` is stored in `SHRegistry` via the `SHTreasury` constructor and is readable from any `SessionHandler` via `getAgentId()`.

---

## `HelperConfig.s.sol`

`HelperConfig` resolves chain-specific deployment parameters at runtime, keeping deployment and test scripts chain-agnostic.

| Network | Chain ID | EntryPoint |
|---|---|---|
| Ethereum Mainnet | 1 | `ENTRYPOINT_V07` (canonical) |
| Ethereum Sepolia | 11155111 | `ENTRYPOINT_V07` (canonical) |
| Anvil (local) | 31337 | Freshly deployed, cached per session |

For Anvil, `HelperConfig` deploys a fresh `EntryPoint`, 25 token mocks, 25 `MockV3Aggregator` price feeds, `MockIdentityRegistry`, and `MockReputationRegistry`, then caches the result. Mock prices approximate real-world values (ETH at $1000, USDC at $0.998). All mock feeds use a 1-hour heartbeat.

For Sepolia, live testnet token and Chainlink feed addresses are returned. USDT, AAVE, and UNI have Sepolia token addresses but no Chainlink feeds on Sepolia — they are registered as tokens but skipped during oracle construction. Uniswap V2 is not deployed on Sepolia, so `uniswapRouter` is `address(0)`.

`getMainnetConfig()` is primarily used for mainnet-fork testing and sets `account` to Anvil's default account 0. **Before deploying to live mainnet, replace this with a real funded EOA.**

---

## `DeploySHProtocol.s.sol`

Orchestrates deployment of all shared protocol infrastructure. Individual `SessionHandler` wallets are not deployed here — users call `SHFactory.deployWallet()` themselves.

**Deployment sequence:**

1. Instantiate `HelperConfig` to resolve chain-specific addresses.
2. Build parallel token/feed/heartbeat arrays and deploy `SHOracle`.
3. Call `IIdentityRegistry.register(AGENT_URI)` to mint the agent's identity NFT and obtain `agentId`.
4. Deploy `SHTreasury(initialFee, oracle, agentId, uniswapRouter)` — the treasury's constructor deploys its own `SHRegistry`.
5. Deploy `SHValueInterpreter(treasury.REGISTRY())` and wire it in via `treasury.setCallValueInterpreter(interpreter)`.
6. Deploy `SHFactory(entryPoint, treasury.REGISTRY(), reputationRegistry, identityRegistry)`.

```solidity
function run() external returns (SHFactory factory, SHTreasury treasury, HelperConfig.NetworkConfig memory config, SHOracle oracle);
```

---

## `SendPackedUserOp.s.sol`

A reusable script helper for constructing signed `PackedUserOperation`s, used by both the test suite and deployment scripts.

**Two signing modes:**
- **Owner mode** — pass `sessionSigner = address(0)` and `sessionSignerKey = 0`.
- **Session key mode** — pass a valid `sessionSigner` address and its `sessionSignerKey`.

**Signing flow:**
1. Fetch nonce from `EntryPoint.getNonce(sender, 0)`.
2. Build an unsigned `PackedUserOperation` with hardcoded gas parameters.
3. Get `userOpHash` from `EntryPoint.getUserOpHash(userOp)`.
4. Wrap in EIP-191 envelope via `toEthSignedMessageHash`.
5. Sign the digest with `vm.sign(privateKey, digest)`.
6. Attach `(r, s, v)` signature to the UserOp.

---

## Test Suite

**`test/unit/SHProtocolTest.t.sol`** — comprehensive coverage of the full `SessionHandler` lifecycle.

| Category | Coverage |
|---|---|
| Access Control | Owner/non-owner permissions for `execute`, `pause`, `addSessionKey`, `revokeSessionKey` |
| Session Validation | Time bounds, target matching, selector whitelisting, spending limits, stale prices |
| Signature Recovery | ECDSA recovery for owner and session key UserOps |
| ERC-4337 Flow | End-to-end `EntryPoint.handleOps()` for both owner and session key |
| Session Lifecycle | Activation, expiry, auto-revocation, budget exhaustion |
| View Functions | `isSessionActive`, `getRemainingBudget`, `getSession`, `isSpendingWithinBudget` |
| Events | `SessionAdded` and `SessionRevoked` emissions |

Tests that use `vm.warp` call `_refreshMockFeeds()` afterward to reset the `updatedAt` timestamp on all `MockV3Aggregator` instances, preventing false `SHOracle_StalePrice` reverts.

**`test/fork/SHUniswapV2Test.t.sol`** — integration tests for all six Uniswap V2 swap functions against a live mainnet fork. Uses `vm.mockCall` on `latestRoundData` to keep feeds fresh after `vm.warp`.

| Test | Swap Function |
|---|---|
| `testSwapExactTokensForTokensWithSession` | `swapExactTokensForTokens` |
| `testSwapTokensForExactTokensWithSession` | `swapTokensForExactTokens` |
| `testSwapEthForExactTokensWithSession` | `swapETHForExactTokens` |
| `testSwapExactTokensForETHWithSession` | `swapExactTokensForETH` |
| `testSwapTokensForExactETHWithSession` | `swapTokensForExactETH` |
| `testSwapExactETHForTokensWithSession` | `swapExactETHForTokens` |

**`test/fork/SHSepoliaTest.t.sol`** — integration tests against a live Sepolia fork.

| Test | Description |
|---|---|
| `testSendingEthWithSession` | Sends 1 ETH via an ETH-session key; verifies budget deduction and recipient balance |
| `testTransferERC20WithSession` | Transfers 20 LINK via a LINK-session key; verifies recipient balance |

**`test/unit/SessionHandlerHarness.sol`** — inherits `SessionHandler` and re-exports internal functions as external for unit testing.

**`test/invariant/`** — stateful invariant tests. `SHHandler` defines valid actions; `InvariantSH` asserts invariants (e.g. `spentAmount` never exceeds `spendingLimit`) hold across arbitrary action sequences.

---

## Foundry Commands

```bash
# Build
forge build

# Run all tests
forge test

# Verbose output
forge test -vvvv

# Specific test
forge test --match-test testFunctionName -vvvv

# Unit tests only
forge test --match-path test/unit/SHProtocolTest.t.sol

# Invariant tests
forge test --match-path test/invariant/InvariantSH.t.sol

# Fork test — Uniswap V2 (mainnet fork)
forge test --match-path test/fork/SHUniswapV2Test.t.sol --fork-url $MAINNET_RPC_URL

# Fork test — Sepolia
forge test --match-path test/fork/SHSepoliaTest.t.sol --fork-url $SEPOLIA_RPC_URL

# Deploy shared protocol infrastructure
forge script script/DeploySHProtocol.s.sol \
  --rpc-url $SEPOLIA_RPC_URL \
  --account <keystore-account> \
  --broadcast \
  --verify
```
