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
  google_linked: boolean
  telegram_linked: boolean
  wallet_chains: number[]
}

/** GET /api/chains */
export interface Chain {
  chain_id: number
  name: string
  native_ticker: string | null
  /** True when this server points the chain at a local fork. */
  fork: boolean
}
