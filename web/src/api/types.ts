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
  email: string | null
  owner_addr: string | null
  /** False for an account created through Google. */
  has_password: boolean
  google_linked: boolean
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
}

export interface DeployPrepared {
  chain_id: number
  predicted_address: string
  session_key: string
  watched_tokens: string[]
  tx: PreparedTx
}

/** POST /api/deploy/confirm: 202 while the transaction is pending, 200 once it has mined. */
export type DeployConfirmResult =
  | { status: 'pending'; tx_hash: string }
  | {
      status: 'deployed'
      chain_id: number
      wallet_address: string
      session_key: string
      session_key_authorized: boolean
    }

/** GET /api/chains */
export interface Chain {
  chain_id: number
  name: string
  native_ticker: string | null
  /** True when this server points the chain at a local fork. */
  fork: boolean
}

/** One row of GET /api/wallet/{chain_id} `balances`. A token that could not be read has `error`. */
export interface TokenBalance {
  ticker: string
  /** null for the native asset. */
  address: string | null
  native: boolean
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
    /** Whether the assistant may act for the wallet. */
    active: boolean
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
  /** The assistant's own key; the API fills it in. */
  | { kind: 'session'; action: 'add' | 'remove' }
  | { kind: 'trusted-spender'; spender: string; action: 'add' | 'remove' }
  | { kind: 'max-gas'; maxCostEth: string }

/** POST /api/wallet/tx/confirm: 202 while the transaction is pending, 200 once it has mined. */
export type OwnerTxConfirmResult =
  | { status: 'pending'; tx_hash: string }
  | { status: 'confirmed'; tx_hash: string }

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

/** One line of the conversation, from GET /api/chat/history. Tool traffic is never included. */
export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
}
