import { ApiError, apiFetch } from './client'
import type {
  CustomToken,
  DeployConfirmResult,
  DeployPrepared,
  DeployRequest,
  OwnerAction,
  OwnerTxConfirmResult,
  PreparedTx,
  SessionTxConfirmResult,
  Token,
  WalletState,
} from './types'

/** Tokens a wallet on `chainId` can count toward its limit. */
export async function fetchTokens(chainId: number) {
  const { tokens } = await apiFetch<{ tokens: Token[] }>(`/api/tokens?chain_id=${chainId}`, { auth: false })
  return tokens
}

/**
 * Reads a token off the chain so the user can check it before adding it. Saves nothing. A token that
 * can't be added answers 400 with the reason, written for the user.
 */
export function lookupCustomToken(chainId: number, address: string) {
  return apiFetch<CustomToken>('/api/tokens/custom/lookup', {
    method: 'POST',
    body: { chain_id: chainId, address },
  })
}

/** Adds a token to the account's list for `chainId`. The server checks it again on chain. */
export function addCustomToken(chainId: number, address: string) {
  return apiFetch<CustomToken>('/api/tokens/custom', { method: 'POST', body: { chain_id: chainId, address } })
}

/** Takes a token off the account's list. The tokens stay in the wallet. A 404 means it was already gone. */
export function removeCustomToken(chainId: number, address: string) {
  return apiFetch<{ status: 'deleted' }>(`/api/tokens/custom/${chainId}/${address}`, { method: 'DELETE' })
}

/** Builds the unsigned deployWallet transaction for the user's own wallet to sign. */
export function prepareDeploy(body: DeployRequest, signal?: AbortSignal) {
  return apiFetch<DeployPrepared>('/api/deploy', { method: 'POST', body, signal })
}

/** Records the wallet once the deploy has mined. Answers `pending` (HTTP 202) until then. */
export function confirmDeploy(
  body: { chain_id: number; deployer: string; tx_hash: string; predicted_address: string },
  signal?: AbortSignal,
) {
  return apiFetch<DeployConfirmResult>('/api/deploy/confirm', { method: 'POST', body, signal })
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
      return action.action === 'add'
        ? ['/api/wallet/session/prepare', { chain_id, action: 'add', ttl_secs: action.ttlSecs }]
        : ['/api/wallet/session/prepare', { chain_id, action: 'remove' }]
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
export function prepareOwnerAction(chainId: number, action: OwnerAction, signal?: AbortSignal) {
  const [path, body] = ownerActionRequest(chainId, action)
  return apiFetch<{ tx: PreparedTx }>(path, { method: 'POST', body, signal })
}

/** Waits for an owner transaction. Answers `pending` (HTTP 202) until it has mined. */
export function confirmOwnerTx(body: { chain_id: number; tx_hash: string }, signal?: AbortSignal) {
  return apiFetch<OwnerTxConfirmResult>('/api/wallet/tx/confirm', { method: 'POST', body, signal })
}

/**
 * confirmOwnerTx for the assistant's key. Turning the assistant on mints a new key that the app
 * starts using only once this sees the wallet trust it, so a grant confirmed anywhere else would
 * leave the assistant signing with the key the wallet just dropped.
 */
export function confirmSessionTx(body: { chain_id: number; tx_hash: string }, signal?: AbortSignal) {
  return apiFetch<SessionTxConfirmResult>('/api/wallet/session/confirm', { method: 'POST', body, signal })
}
