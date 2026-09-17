import { createContext } from 'react'
import type { Chain } from '../api/types'

export interface ChainState {
  /** The network the app is showing: one of the user's wallets, else the first one served. */
  chainId: number | null
  setChainId: (chainId: number) => void
  /** Networks this server serves (/api/chains). */
  chains: Chain[]
  /** Networks the user has a wallet on. */
  walletChains: number[]
  /** The selected network's entry in `chains`, when known. */
  chain: Chain | undefined
}

export const ChainContext = createContext<ChainState | null>(null)
