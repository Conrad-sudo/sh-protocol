import { ApiError, apiFetch } from './client'
import type { DeployConfirmResult, DeployPrepared, DeployRequest, Token, WalletState } from './types'

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
