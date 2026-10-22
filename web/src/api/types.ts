// Response shapes, mirroring app/api.py. Keep in step with the handlers named on each type.

/** _issue_session / refresh — the refresh token itself travels in an httpOnly cookie. */
export interface TokenResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  user_id: number
}

/** GET /api/me */
export interface Me {
  user_id: number
  /**
   * The address the account signs in as, which owns its wallets. Null only for an account from
   * Telegram-only days, which the web app can't sign in.
   */
  owner_addr: string | null
  telegram_linked: boolean
  /**
   * The username (no @) of this server's Telegram bot, or null when it runs none. The one bot serves
   * every network; /chain in the chat picks the one it acts on.
   */
  telegram_bot: string | null
  wallet_chains: number[]
}

/** POST /api/integrations/telegram/link — the link works once, for `expires_in` seconds. */
export interface TelegramLink {
  url: string
  nonce: string
  expires_in: number
}

/** A token this deployment can price, from GET /api/tokens. */
export interface Token {
  ticker: string
  address: string
  /**
   * The wrapped native token (WETH, WBNB): it always counts toward the limit, like the native asset
   * it wraps. The API adds it to every new wallet and won't remove it.
   */
  always_counted?: boolean
}

/**
 * An unsigned transaction built by the API (_to_json_tx). Integers arrive as hex strings so a
 * browser never rounds them; the wallet fills in fees and nonce itself.
 */
export interface PreparedTx {
  to: string
  data: string
  value?: string
  gas?: string
  chainId?: string
}

/** POST /api/deploy body. The user id is never sent — the API takes it from the token. */
export interface DeployRequest {
  chain_id: number
  deployer: string
  /** Whole dollars. */
  daily_limit_usd: number
  window_secs: number
  watched_tokens: Token[]
  /** Native-token amount as a decimal string, e.g. "0.05". */
  prefund_eth: string
  /**
   * The passkey made during onboarding, which the new wallet trusts from the start; null for a
   * wallet only its owner can use until a device is added in Controls.
   */
  key_id: string | null
  /** How long that passkey may approve payments, in seconds. */
  session_ttl_secs: number
}

export interface DeployPrepared {
  chain_id: number
  predicted_address: string
  watched_tokens: string[]
  tx: PreparedTx
}

/**
 * POST /api/deploy/confirm: 202 while the transaction is pending, 200 once it has mined. `seen` as in
 * OwnerTxConfirmResult.
 */
export type DeployConfirmResult =
  | { status: 'pending'; tx_hash: string; seen?: boolean }
  | {
      status: 'deployed'
      chain_id: number
      wallet_address: string
      /** The passkey the wallet was created trusting, or null for an owner-only wallet. */
      key_id: string | null
      /** True once the wallet is seen trusting that passkey. */
      session_key_authorized: boolean
      /** Unix seconds: when that passkey stops approving payments. */
      session_key_expires_at: number | null
    }

/** GET /api/chains */
export interface Chain {
  chain_id: number
  name: string
  native_ticker: string | null
  /** True when this server points the chain at a local fork. */
  fork: boolean
  /** That fork's local node, for the user's wallet (each fork has its own port); null on a live chain. */
  rpc_url: string | null
  /**
   * The exchange contracts every new wallet here is deployed trusting, checksummed, in this order:
   * Uniswap V3's router then its positions contract, or PancakeSwap's router on BSC. Empty where the
   * network has no exchange.
   */
  default_spenders: string[]
  /** The exchange the assistant swaps and provides liquidity on, or null where it can do neither. */
  exchange: Exchange | null
}

/** Uniswap V3 on Ethereum, Sepolia, Arbitrum and Base; PancakeSwap V2 on BSC. */
export type Exchange = 'uniswap-v3' | 'pancakeswap-v2'

/** One row of GET /api/wallet/{chain_id} `balances`. A token that could not be read has `error`. */
export interface TokenBalance {
  ticker: string
  /** null for the native asset. */
  address: string | null
  native: boolean
  /**
   * True for a token the user added by address that Mitfah doesn't list. It has no price, so it
   * never counts toward the spending limit. Absent (false) for the native asset and listed tokens.
   */
  custom?: boolean
  /**
   * The wrapped native token (WETH, WBNB): it always counts toward the limit, so it can't be
   * removed from the dashboard. Absent for the native asset, which can't be removed either.
   */
  always_counted?: boolean
  /** A custom token's own name(), when it has a readable one. */
  name?: string | null
  /**
   * True for the LP tokens of an exchange pool the assistant deposited into, named like "eth/usdc lp".
   * The row shows only while the wallet holds some. No price, and the assistant can't send it.
   */
  lp?: boolean
  /** An LP row's share of each of the pool's tokens right now; null when the pool couldn't be read. */
  underlying?: PoolShare[] | null
  decimals: number | null
  /** Integer string in the token's smallest unit. Display from this, never from `amount`. */
  raw: string | null
  amount: number | null
  error?: string
}

/** How much of one of a pool's tokens an LP row or a liquidity position holds. */
export interface PoolShare {
  ticker: string
  decimals: number
  /** Integer string in the token's smallest unit. */
  raw: string
}

/**
 * A Uniswap V3 liquidity position the wallet holds, from GET /api/wallet/{chain_id} `positions`: a
 * deposit into one pool that earns a share of its fees while the price stays between two bounds.
 * Listed only while it still holds liquidity or unclaimed fees, and only when the user knows both
 * of its tokens: anyone can send a position to any wallet.
 *
 * Prices are in whole tokens of `quote` per `base`, e.g. USDC per ETH. The native coin is named by
 * its own ticker ("eth"), never as its wrapped token.
 */
export interface Position {
  /** The position's NFT id. */
  id: number
  /** "eth/usdc": the base token, then the quote token. */
  pair: string
  base: string
  quote: string
  /** The pool's fee tier as a percentage: 0.05 for the 0.05% pool. */
  fee_pct: number
  /** The range's lower bound; 0 for a full-range position. */
  min_price: number
  /** The range's upper bound; null for a full-range position. */
  max_price: number | null
  full_range: boolean
  current_price: number
  /** True while the price is inside the range, the only time the position earns fees. */
  in_range: boolean
  /** What the position holds now: the base token first. */
  amounts: PoolShare[]
  /** Fees it has earned and nobody has collected yet: the base token first. */
  fees: PoolShare[]
}

/**
 * One key the wallet trusts, read off the chain (getSessions). Each is a passkey on one of the
 * user's devices: it approves the assistant's payments until `valid_until`.
 */
export interface SessionKey {
  /** keccak256(qx, qy): how the wallet names the key, 0x-prefixed. */
  key_id: string
  /** The device's name from the account's passkeys; null for a key granted outside Mitfah. */
  device_name: string | null
  /** The passkey's WebAuthn credential id (base64url); null for a key Mitfah doesn't know. */
  credential_id: string | null
  /** Unix seconds: when the wallet stops accepting this key. */
  valid_until: number
  expires_in_secs: number
  /** Still within its deadline. */
  active: boolean
  /** Still works, but runs out within `renewal_warning_secs`. */
  needs_renewal: boolean
}

/** GET /api/wallet/{chain_id} `session`: the devices that may approve payments from this wallet. */
export interface SessionBlock {
  /** The most keys a wallet holds at once (5). Adding one to a full wallet first drops expired keys. */
  max_keys: number
  /** The longest a grant may last, in seconds (90 days). */
  max_ttl_secs: number
  /** How close to its deadline a key counts as needing renewal, in seconds. */
  renewal_warning_secs: number
  keys: SessionKey[]
}

/** One of the account's passkeys, from GET /api/passkeys. A wallet trusts it only once its owner adds it. */
export interface Passkey {
  key_id: string
  /** Base64url. */
  credential_id: string
  device_name: string
  /** Unix seconds. */
  created_at: number
}

/** POST /api/passkeys/register: the passkey just saved, with its public key's coordinates (0x hex). */
export interface RegisteredPasskey extends Passkey {
  qx: string
  qy: string
}

/**
 * A new passkey as the browser made it (WebAuthn's RegistrationResponseJSON), binary fields in
 * base64url. The server checks it and keeps only the public key.
 */
export interface PasskeyRegistrationJSON {
  id: string
  rawId: string
  type: 'public-key'
  authenticatorAttachment?: string
  clientExtensionResults: Record<string, unknown>
  response: {
    clientDataJSON: string
    attestationObject: string
    transports?: string[]
    authenticatorData?: string
    publicKey?: string
    publicKeyAlgorithm?: number
  }
}

/** A passkey's signature over a challenge (WebAuthn's AuthenticationResponseJSON), binary fields in base64url. */
export interface PasskeyAssertionJSON {
  id: string
  rawId: string
  type: 'public-key'
  authenticatorAttachment?: string
  clientExtensionResults: Record<string, unknown>
  response: {
    clientDataJSON: string
    authenticatorData: string
    signature: string
    userHandle?: string
  }
}

/**
 * What a confirmation costs: the fees, network and Mitfah's together. `fees` is the one figure a
 * quote card shows (as QuoteOffer.fees); the totals are what it is written from.
 */
export interface SignCost {
  fees?: string
  total_usd?: number
  total_native?: number
  native_asset?: string
}

/**
 * POST /api/sign/start: the final transaction is built, and its hash is the challenge the passkey
 * signs. Starting again rebuilds it and the older `sign_id` stops working.
 */
export interface SignStart {
  sign_id: string
  /** Base64url of the 32 bytes the passkey signs. */
  challenge: string
  rp_id: string
  /** The account's passkeys this wallet trusts now: only these can approve it. */
  allow_credentials: { type: 'public-key'; id: string; transports?: string[] }[]
  user_verification: 'required'
  timeout_ms: number
  /** Unix seconds: the quote's own deadline. */
  expires_at: number
  summary: {
    /** What it does, in one line, as the quote was written. */
    action: string
    /** The network's name, e.g. "Base". */
    network: string
    chain_id: number
    cost: SignCost
  }
  /**
   * The Telegram bot's username (no @), for the confirm page's "Back to Telegram". Optional: when
   * the server leaves it out, the page says to switch back instead.
   */
  telegram_bot?: string | null
}

/** POST /api/sign/finish: the transaction is on the wire; `transaction_id` is the History row to follow. */
export interface SignFinish {
  status: 'sent'
  /** What happened, as the server worded it for the chat. */
  outcome: string
  transaction_id: number | null
}

/** GET /api/wallet/{chain_id} (get_wallet_state): everything the dashboard shows. */
export interface WalletState {
  chain_id: number
  chain_name: string
  address: string
  owner: string
  /** False when this account is not linked to the on-chain owner, so owner actions can't be offered. */
  is_owner: boolean
  paused: boolean
  spending: {
    /** False means the spending-limit hook is not installed: nothing caps the assistant. */
    hook_installed: boolean
    daily_limit_usd: number
    /** As stored on chain; stale once the period has ended (the next spend resets it). */
    spent_usd: number
    /** What the assistant may still spend now, with an ended period already counted as reset. */
    remaining_usd: number
    window_hours: number
    /** Unix seconds. The period ends `window_hours` later. */
    window_start: number
    /** ERC-20s that count toward the limit. The native asset always counts and is not listed. */
    watched_tokens: { ticker: string | null; address: string }[]
  }
  session: SessionBlock
  limits: {
    max_op_gas_cost_wei: string
    /**
     * The most the assistant's operations may pay in network fees per day, all together. Null for
     * a wallet made before the budget existed.
     */
    session_gas: {
      budget_wei: string
      remaining_wei: string
      /** Unix seconds: when what's been used comes back. Null while none of it is used. */
      refills_at: number | null
    } | null
    allowlist_enabled: boolean
    trusted_spenders: string[]
  }
  balances: TokenBalance[]
  /** The wallet's Uniswap V3 positions. Always empty on a network that doesn't use Uniswap V3. */
  positions: Position[]
}

/**
 * A change only the wallet's owner can sign. Each maps to one /api/wallet/…/prepare endpoint.
 * Tokens are tickers: the API resolves them for the chain ("eth" or the chain's own ticker for the
 * native asset, withdraw only). Decimal amounts travel as strings.
 */
export type OwnerAction =
  | { kind: 'pause' }
  | { kind: 'unpause' }
  | { kind: 'withdraw'; token: string; amount: string; to: string }
  | { kind: 'watched-token'; token: string; action: 'add' | 'remove' }
  | { kind: 'daily-limit'; dailyLimitUsd: number }
  | { kind: 'window'; windowSecs: number }
  /**
   * A device's passkey. `add` lets it approve payments for `ttlSecs`, and renews one the wallet
   * already trusts; `remove` revokes one; `remove_all` revokes every key at once.
   */
  | { kind: 'session'; action: 'add'; keyId: string; ttlSecs: number }
  | { kind: 'session'; action: 'remove'; keyId: string }
  | { kind: 'session'; action: 'remove_all' }
  | { kind: 'trusted-spender'; spender: string; action: 'add' | 'remove' }
  | { kind: 'max-gas'; maxCostEth: string }
  | { kind: 'gas-budget'; budgetEth: string }
  /**
   * The contract allowlist. `enable` lists `targets` and turns it on in one transaction (`targets` may
   * be empty when the list already has entries); `remove` takes exactly one; `disable` ignores them.
   */
  | { kind: 'allowlist'; action: 'enable' | 'add' | 'remove' | 'disable'; targets: string[] }

/** One contract on, or worth adding to, the wallet's contract allowlist. */
export interface AllowlistEntry {
  address: string
  /** Mitfah's name for it ("USDC", "Uniswap V3 router", "sam (contact)"), or null when it has none. */
  label: string | null
}

/**
 * GET /api/wallet/{chain_id}/allowlist. While the list is on, the assistant can call only the
 * contracts on it; a plain wallet address (no code) is never blocked.
 */
export interface Allowlist {
  enabled: boolean
  /** null for a wallet created before its list could be read back: only its on/off state is known. */
  targets: AllowlistEntry[] | null
  /** Contracts the assistant uses that aren't on the list yet, in the order worth offering them. */
  suggested: AllowlistEntry[]
}

/**
 * POST /api/wallet/tx/confirm: 202 while the transaction is pending, 200 once it has mined. `seen` is
 * false while the network has never seen the hash: it may never arrive.
 */
export type OwnerTxConfirmResult =
  | { status: 'pending'; tx_hash: string; seen?: boolean }
  | { status: 'confirmed'; tx_hash: string }

/**
 * POST /api/wallet/session/confirm: like /api/wallet/tx/confirm for a device's passkey, but once
 * mined it answers with the wallet's keys as they are now.
 */
export type SessionTxConfirmResult =
  | { status: 'pending'; tx_hash: string; seen?: boolean }
  | { status: 'confirmed'; tx_hash?: string; session?: SessionBlock }

/** POST /api/transactions/deposit: a deposit from the Fund drawer, answered as OwnerTxConfirmResult. */
export type DepositConfirmResult =
  | { status: 'pending'; tx_hash: string; seen?: boolean }
  | { status: 'confirmed'; tx_hash: string }

/**
 * A token as the server read it off the chain: POST /api/tokens/custom/lookup (a preview, nothing
 * saved) and POST /api/tokens/custom (saved).
 */
export interface CustomToken {
  chain_id: number
  /** Checksummed. */
  address: string
  /** The token's own symbol, lowercased: the name the assistant uses. */
  ticker: string
  /** symbol() as the contract answered it. With `decimals`, what shows it is an ERC-20. */
  symbol: string
  name: string | null
  decimals: number
  /** The wallet's balance, as an integer string in the token's smallest unit. */
  balance_raw: string
  /**
   * True for a token Mitfah lists: it has a price, so it can count toward the spending limit, and
   * adding it puts it on the dashboard under its listed ticker.
   */
  listed: boolean
}

/**
 * A saved payee, from GET /api/contacts. The assistant can only send money to these. They belong to
 * the account, not to a network.
 */
export interface Contact {
  /** Lowercase: the name the user gives the assistant. */
  name: string
  /** Checksummed. */
  address: string
}

/**
 * POST /api/contacts/prepare: the EIP-712 typed data the owner wallet signs to save a contact. The
 * name and address are as they will be stored; the nonce works once, for this contact only.
 */
export interface ContactTypedData {
  domain: { name: string; version: string }
  types: { AddContact: { name: string; type: string }[] }
  primaryType: 'AddContact'
  message: { name: string; address: string; nonce: string }
}

/** One line of the conversation, from GET /api/chat/history. Tool traffic is never included. */
export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  /**
   * True on the two messages a conversation started afresh with: the server clears the chat after
   * each transaction and keeps only the last exchange.
   */
  carried_over?: boolean
  /** True on a quote card's button tap and its outcome: the server wrote them, so they show as plain text. */
  button?: boolean
}

/**
 * A quoted transaction waiting for a Confirm or Cancel tap, from GET /api/chat/quote. Written by
 * the server from the stored quote, never by the assistant: Confirm sends exactly this.
 */
export interface QuoteOffer {
  quote_id: string
  chain_id: number
  /** What it does, in one line. */
  action: string
  /** The fees, network and Mitfah's together, as one figure: "$0.35", or in the native coin. */
  fees: string
  /** Unix seconds: when it stops being good to confirm. */
  expires_at: number
}

/** One transaction on the account's wallets, from GET /api/transactions. */
export interface Transaction {
  id: number
  chain_id: number
  /**
   * Who sent it: the assistant (from the web chat or Telegram), the owner in the browser, or
   * someone or something outside Mitfah, as a block explorer reported it.
   */
  source: 'assistant' | 'owner' | 'outside'
  /** What it does, written by the server from the transaction itself. */
  action: string
  /** `dropped`: never mined, and now never will be. */
  status: 'pending' | 'confirmed' | 'failed' | 'dropped'
  /** Null only while an assistant transaction hasn't been seen on chain yet. */
  tx_hash: string | null
  /** Unix seconds: when Mitfah recorded it (for an outside row, long after it mined). */
  created_at: number
  /** Unix seconds: when its block was mined. Null until then. */
  mined_at: number | null
  /** Its group in the History tab. Null while the server doesn't know yet. */
  direction: TransactionDirection | null
}

/** The History tab's groups: the wallet only received, something left it, or nothing moved. */
export type TransactionDirection = 'in' | 'out' | 'none'

/**
 * GET /api/transactions' filters; each one given must hold. The amount, address and token must all
 * hold for one movement of a transaction, going the `direction` way when that is 'in' or 'out'.
 */
export interface TransactionFilters {
  direction?: TransactionDirection
  /** Unix seconds, inclusive. */
  since?: number
  /** Unix seconds, exclusive. */
  until?: number
  /** Whole units of whichever token moved, as typed. */
  min_amount?: string
  max_amount?: string
  /** The other side, or the token's contract. */
  address?: string
  /** A ticker, any case, on any network. */
  token?: string
}

/** GET /api/transactions/tokens: the tickers the account's transactions have moved, A-Z. */
export interface TransactionTokens {
  tokens: string[]
}

/** GET /api/transactions: a page, newest first. `next_before` asks for the page after it. */
export interface TransactionPage {
  transactions: Transaction[]
  /** An opaque cursor ("<time>-<id>"). */
  next_before: string | null
  /** The first page only: a search for activity outside Mitfah is running, so read again soon. */
  syncing: boolean
}
