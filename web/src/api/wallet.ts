import { ApiError, apiFetch } from './client'
import type {
  DeployConfirmResult,
  DeployPrepared,
  DeployRequest,
  OwnerAction,
  OwnerTxConfirmResult,
  PreparedTx,
  Token,
  WalletState,
} from './types'

/** A single-use nonce to put in the SIWE message. */
export function siweNonce() {
  return apiFetch<{ nonce: string }>('/api/auth/siwe/nonce', { auth: false })
}

/** Binds the address that signed `message` to the signed-in account as its wallet owner. */
export function siweVerify(body: { message: string; signature: string; nonce: string }) {
  return apiFetch<{ owner_addr: string }>('/api/auth/siwe/verify', { method: 'POST', body })
}

/** Tokens a wallet on `chainId` can count toward its limit. */
export async function fetchTokens(chainId: number) {
  const { tokens } = await apiFetch<{ tokens: Token[] }>(`/api/tokens?chain_id=${chainId}`, { auth: false })
  return tokens
}

/** Builds the unsigned deployWallet transaction for the user's own wallet to sign. */
export function prepareDeploy(body: DeployRequest) {
  return apiFetch<DeployPrepared>('/api/deploy', { method: 'POST', body })
}

/** Records the wallet once the deploy has mined. Answers `pending` (HTTP 202) until then. */
export function confirmDeploy(body: {
  chain_id: number
  deployer: string
  tx_hash: string
  predicted_address: string
}) {
  return apiFetch<DeployConfirmResult>('/api/deploy/confirm', { method: 'POST', body })
}

/** The wallet's state on `chainId`, or null when the account has no wallet there (404). */
export async function fetchWallet(chainId: number): Promise<WalletState | null> {
  try {
    return await apiFetch<WalletState>(`/api/wallet/${chainId}`)
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

/** The endpoint and body that prepare `action`. */
function ownerActionRequest(chainId: number, action: OwnerAction): [string, object] {
  const chain_id = chainId
  switch (action.kind) {
    case 'pause':
    case 'unpause':
      return [`/api/wallet/${action.kind}/prepare`, { chain_id }]
    case 'withdraw':
      return ['/api/wallet/withdraw/prepare', { chain_id, token: action.token, amount: action.amount, to: action.to }]
    case 'watched-token':
      return ['/api/wallet/watched-tokens/prepare', { chain_id, token: action.token, action: action.action }]
    case 'daily-limit':
      return ['/api/wallet/daily-limit/prepare', { chain_id, daily_limit_usd: action.dailyLimitUsd }]
    case 'window':
      return ['/api/wallet/window-duration/prepare', { chain_id, window_secs: action.windowSecs }]
    case 'session':
      return ['/api/wallet/session/prepare', { chain_id, action: action.action }]
    case 'trusted-spender':
      return ['/api/wallet/trusted-spenders/prepare', { chain_id, spender: action.spender, action: action.action }]
    case 'max-gas':
      return ['/api/wallet/max-op-gas-cost/prepare', { chain_id, max_cost_eth: action.maxCostEth }]
  }
}

/**
 * Builds an owner transaction for the user's own wallet to sign. The API test-runs it first, so a
 * change that would fail is refused here (400) with the reason, before the user pays for it.
 */
export function prepareOwnerAction(chainId: number, action: OwnerAction) {
  const [path, body] = ownerActionRequest(chainId, action)
  return apiFetch<{ tx: PreparedTx }>(path, { method: 'POST', body })
}

/** Waits for an owner transaction. Answers `pending` (HTTP 202) until it has mined. */
export function confirmOwnerTx(body: { chain_id: number; tx_hash: string }) {
  return apiFetch<OwnerTxConfirmResult>('/api/wallet/tx/confirm', { method: 'POST', body })
}
