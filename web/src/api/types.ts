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
  /** How long the assistant's first key lasts, in seconds. */
  session_ttl_secs: number
}

export interface DeployPrepared {
  chain_id: number
  predicted_address: string
  session_key: string
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
      session_key: string
      session_key_authorized: boolean
      /** Unix seconds: when that key stops working. */
      session_key_expires_at: number
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
  /** The exchange router every wallet here is deployed trusting, or null where there is none. */
  router: string | null
}

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
  decimals: number | null
  /** Integer string in the token's smallest unit. Display from this, never from `amount`. */
  raw: string | null
  amount: number | null
  error?: string
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
  session: {
    /** The assistant's key, or null when this app holds none for the wallet. */
    key: string | null
    /** The one key the wallet trusts, read off the chain, or null when it trusts none. */
    wallet_key: string | null
    /** True when the wallet trusts the key this app holds, so the assistant can sign at all. */
    is_app_key: boolean
    /** Whether the assistant may act for the wallet: its key is the wallet's, and hasn't run out. */
    active: boolean
    /** Unix seconds: when the wallet's key stops working. Null when the wallet trusts no key. */
    expires_at: number | null
    expires_in_secs: number
    /** True while the key still works but runs out soon enough to prompt a renewal. */
    needs_renewal: boolean
  }
  limits: {
    max_op_gas_cost_wei: string
    allowlist_enabled: boolean
    trusted_spenders: string[]
  }
  balances: TokenBalance[]
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
   * The assistant's key. `add` makes the API mint a brand-new key lasting `ttlSecs`, replacing any
   * key the wallet trusts now — so it both turns the assistant on and renews it.
   */
  | { kind: 'session'; action: 'add'; ttlSecs: number }
  | { kind: 'session'; action: 'remove' }
  | { kind: 'trusted-spender'; spender: string; action: 'add' | 'remove' }
  | { kind: 'max-gas'; maxCostEth: string }

/**
 * POST /api/wallet/tx/confirm: 202 while the transaction is pending, 200 once it has mined. `seen` is
 * false while the network has never seen the hash: it may never arrive.
 */
export type OwnerTxConfirmResult =
  | { status: 'pending'; tx_hash: string; seen?: boolean }
  | { status: 'confirmed'; tx_hash: string }

/**
 * POST /api/wallet/session/confirm: like /api/wallet/tx/confirm, but once mined it files the new key
 * or forgets the revoked one. `unrecognized_key` means the wallet now trusts a key Mitfah doesn't hold.
 */
export type SessionTxConfirmResult =
  | { status: 'pending'; tx_hash: string; seen?: boolean }
  | { status: 'granted' | 'revoked' | 'unrecognized_key'; tx_hash: string }

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
}

/** One transaction made through Mitfah, from GET /api/transactions. */
export interface Transaction {
  id: number
  chain_id: number
  /** Who sent it: the assistant (from the web chat or Telegram), or the owner in the browser. */
  source: 'assistant' | 'owner'
  /** What it does, written by the server from the transaction itself. */
  action: string
  /** `dropped`: never mined, and now never will be. */
  status: 'pending' | 'confirmed' | 'failed' | 'dropped'
  /** Null only while an assistant transaction hasn't been seen on chain yet. */
  tx_hash: string | null
  /** Unix seconds: when Mitfah recorded it. */
  created_at: number
  /** Unix seconds: when its block was mined. Null until then. */
  mined_at: number | null
}

/** GET /api/transactions: a page, newest first. `next_before` asks for the page after it. */
export interface TransactionPage {
  transactions: Transaction[]
  next_before: number | null
}
