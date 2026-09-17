import type { Chain } from 'viem'
import { anvil, arbitrum, bsc, celo, mainnet, sepolia } from 'viem/chains'

/** Every chain the API can serve (app/api.py CHAIN_NAME_BY_ID). /api/chains says which are live. */
export const SUPPORTED_CHAINS = [sepolia, mainnet, bsc, arbitrum, celo, anvil] as const

export type SupportedChainId = (typeof SUPPORTED_CHAINS)[number]['id']

export function chainById(chainId: number | undefined): Chain | undefined {
  return SUPPORTED_CHAINS.find(chain => chain.id === chainId)
}

export function isSupportedChainId(chainId: number): chainId is SupportedChainId {
  return SUPPORTED_CHAINS.some(chain => chain.id === chainId)
}

export function chainName(chainId: number | undefined): string {
  if (chainId === undefined) return 'Unknown network'
  return chainById(chainId)?.name ?? `Chain ${chainId}`
}

/**
 * A block-explorer link, or null when there is nothing public to link to: a local node, or a chain
 * this server points at a local fork (`fork` from /api/chains).
 */
export function explorerUrl(
  chainId: number,
  kind: 'address' | 'tx',
  value: string,
  fork = false,
): string | null {
  if (fork) return null
  const base = chainById(chainId)?.blockExplorers?.default.url
  return base ? `${base}/${kind}/${value}` : null
}
