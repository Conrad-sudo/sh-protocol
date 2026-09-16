// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SessionHandler} from "./SessionHandler.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {SHRegistry} from "./SHRegistry.sol";

contract SHFactory is Ownable, Pausable {
    error SHFactory_FundTransferFailed();
    /// @dev Thrown by deployWallet when no SpendingLimitModule has been configured yet.
    error SHFactory_SpendingLimitModuleNotSet();

    /// @notice The SHRegistry deployed SessionHandlers read all protocol configuration from —
    ///         EntryPoint, the two ERC-8004 registries, and the SpendingLimitModule to install.
    /// @dev Immutable: a factory is bound to one registry for life. Point wallets at a different
    ///      registry by deploying a new factory and recording it with SHRegistry.setFactory.
    SHRegistry public immutable REGISTRY;

    address public immutable IMPLEMENTATION;

    /// @notice Total number of wallets deployed. Doubles as the next walletId to assign.
    uint256 public totalWallets;
    /// @notice Maps each sequential walletId to its deployed wallet address.
    mapping(uint256 => address) public wallets;

    /// @notice How many wallets an address has deployed. Doubles as that owner's next CREATE2 salt
    ///         nonce, and as a "does this user already have a wallet?" view a front end can read
    ///         BEFORE asking anyone to sign — cheaper than a reverted transaction.
    /// @dev Per-OWNER rather than global on purpose. A global counter (e.g. {totalWallets}) would
    ///      make a predicted address depend on how many wallets everyone else had deployed, so any
    ///      other user's deploy landing first would move it — and {predictWalletAddress} exists
    ///      precisely so the address can be shown, and the wallet's session key derived from it,
    ///      before the deploy is signed. Keyed by owner, only that owner's own deploys move it.
    mapping(address owner => uint256) public deployCount;

    event WalletDeployed(address indexed walletAddress, address indexed owner, uint256 indexed walletId);

    /**
     * @notice Deploys the factory and the SessionHandler implementation it clones from.
     * @dev Every other address a wallet needs (EntryPoint, reputation/identity registries, the
     *      SpendingLimitModule) is read from the registry at deploy-wallet time rather than stored
     *      here, so those can be corrected in one place without redeploying this factory.
     * @param owner     Address that will own this factory — the SHTreasury, the protocol's single
     *                  admin root. Supplied rather than taken from msg.sender so no follow-up
     *                  transferOwnership is needed; the operator reaches {pause}/{unpause} through
     *                  SHTreasury's owner-only passthroughs.
     * @param _registry The SHRegistry this factory and its wallets read configuration from.
     */
    constructor(address owner, address _registry) Ownable(owner) {
        REGISTRY = SHRegistry(_registry);
        IMPLEMENTATION = address(new SessionHandler());
    }

    /// @notice Pauses the contract, disabling execute(). Only callable by the owner.
    function pause() public onlyOwner {
        _pause();
    }

    /// @notice Unpauses the contract, re-enabling execute(). Only callable by the owner.
    function unpause() public onlyOwner {
        _unpause();
    }

    /**
     * @notice Derives the CREATE2 salt for a wallet. Binding the OWNER into the salt is what makes
     *         {predictWalletAddress} safe to publish: `nonce` is only ever that owner's own deploy
     *         count, so no other caller can reach the same salt and squat a predicted address.
     * @param owner The address that will own the wallet (deployWallet's msg.sender).
     * @param nonce That owner's {deployCount} at the time of the deploy.
     */
    function _salt(address owner, uint256 nonce) private pure returns (bytes32) {
        return keccak256(abi.encode(owner, nonce));
    }

    /**
     * @notice Returns the address `owner`'s NEXT {deployWallet} call will produce, before it is
     *         deployed. Lets a front end show the user their wallet address in the same screen that
     *         asks them to sign, and lets a backend derive the wallet's session key up front (the
     *         key's off-chain record is stored under the wallet address, so it must be known first).
     * @dev Two things move this value, and nothing else can: `owner` deploying again (their
     *      {deployCount} advances), and SHFactory being redeployed (the deployer is baked into a
     *      CREATE2 address, so a new factory invalidates every previously shown address).
     */
    function predictWalletAddress(address owner) external view returns (address) {
        return Clones.predictDeterministicAddress(IMPLEMENTATION, _salt(owner, deployCount[owner]));
    }

    /// @notice Deploys a SessionHandler (ERC-7579 account) with spendingLimitModule installed as a
    ///         hook, seeded with the caller's spending-cap config. Reverts if the module isn't set.
    /// @dev Deployed with CREATE2 at a salt derived from (msg.sender, that owner's {deployCount}),
    ///      so the address is known before the transaction is signed -- see {predictWalletAddress}.
    ///      The counter is consumed here, so calling this twice yields two distinct wallets and a
    ///      salt can never repeat; there is no collision case to guard against, and no external
    ///      party can occupy the address first because a CREATE2 address is bound to its deployer.
    /// @dev `sessionKey` and `trustedSpenders` are applied inside {SessionHandler-initialize}, so a
    ///      wallet is usable after this ONE transaction. Previously they were two follow-up
    ///      owner-signed calls to addSession/addTrustedSpender.
    /// @dev Deliberately does NOT refuse an owner who already has a wallet -- one address may own
    ///      several. A caller wanting one-wallet-per-user enforces it by reading {deployCount}
    ///      before asking the user to sign, rather than by spending gas on a revert.
    /// @dev That check only means "does this USER have a wallet" when the user is `msg.sender` --
    ///      i.e. when the end user signs their own deploy, which is the flow this is built for. A
    ///      caller that deploys on users' behalf from one shared EOA (as app/deploy_wallet.py does)
    ///      sees a counter covering ALL its users, and must track per-user ownership itself.
    /// @param dailyLimitUsd   Max USD (18 decimals) the wallet may spend per window. Must be >= 0.
    /// @param windowDuration  Spending-window length in seconds. Must be > 0.
    /// @param watchedTokens   Tokens to meter; each must already be priced by the oracle.
    /// @param sessionKey      Session key to authorize, or address(0) for an owner-only wallet.
    /// @param trustedSpenders Spenders trusted for unpriced-token approvals. May be empty.
    function deployWallet(
        int256 dailyLimitUsd,
        uint256 windowDuration,
        address[] calldata watchedTokens,
        address sessionKey,
        address[] calldata trustedSpenders
    ) external payable whenNotPaused returns (address) {
        address module = REGISTRY.spendingLimitModule();
        if (module == address(0)) revert SHFactory_SpendingLimitModuleNotSet();

        bytes32 salt = _salt(msg.sender, deployCount[msg.sender]++);

        uint256 walletId = totalWallets;
        address walletInstance = Clones.cloneDeterministic(IMPLEMENTATION, salt);

        // Bookkeeping BEFORE the external initialize() call (checks-effects-interactions). The
        // wallet's own address is fixed by CREATE2, so writing it early is safe. This matters
        // because initialize() calls the registry-configured module, which the operator can change:
        // a module that re-entered deployWallet would otherwise read a stale totalWallets and give
        // two wallets the same walletId, losing one from `wallets` and leaving the counter short.
        // No such module exists today; this removes the dependency on that staying true.
        wallets[walletId] = walletInstance;
        totalWallets = walletId + 1;

        SessionHandler(payable(walletInstance))
            .initialize(
                SessionHandler.InitConfig({
                    owner: msg.sender,
                    entryPoint: REGISTRY.ENTRY_POINT(),
                    reputationRegistry: REGISTRY.REPUTATION_REGISTRY(),
                    identityRegistry: REGISTRY.IDENTITY_REGISTRY(),
                    registry: address(REGISTRY),
                    walletId: walletId,
                    spendingLimitModule: module,
                    dailyLimitUsd: dailyLimitUsd,
                    windowDuration: windowDuration,
                    watchedTokens: watchedTokens,
                    sessionKey: sessionKey,
                    trustedSpenders: trustedSpenders
                })
            );

        (bool success,) = payable(walletInstance).call{value: msg.value}("");
        if (!success) {
            revert SHFactory_FundTransferFailed();
        }

        emit WalletDeployed(walletInstance, msg.sender, walletId);
        return walletInstance;
    }
}

