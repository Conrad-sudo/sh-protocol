import { apiFetch } from './client'
import type { Chain } from './types'

/** The networks this server can deploy wallets on. Public. */
export async function fetchChains() {
  const { chains } = await apiFetch<{ chains: Chain[] }>('/api/chains', { auth: false })
  return chains
}
