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
 * A link to the network's own block explorer, or null for a chain that has none (a bare local node).
 *
 * On a fork too, deliberately: a fork stands in for the live network, so it links where the live
 * network would. The fork's own transactions aren't on the live chain, so the explorer won't find
 * those -- the link still behaves as it will in production.
 */
export function explorerUrl(chainId: number, kind: 'address' | 'tx', value: string): string | null {
  const base = chainById(chainId)?.blockExplorers?.default.url
  return base ? `${base}/${kind}/${value}` : null
}

/** The name of the network's block explorer ("Etherscan", "BscScan"), for link labels. */
export function explorerName(chainId: number): string {
  return chainById(chainId)?.blockExplorers?.default.name ?? 'the block explorer'
}
