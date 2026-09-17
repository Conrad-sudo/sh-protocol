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
