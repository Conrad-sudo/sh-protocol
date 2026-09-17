import { useState, type ReactNode } from 'react'
import { useAuth } from '../auth/useAuth'
import { useChains } from '../hooks/useChains'
import { useMe } from '../hooks/useMe'
import { ChainContext } from './ChainContext'
import { readStoredChain, resolveChain, writeStoredChain } from './selectedChainStore'

/** Which network the app is showing. Remembered per account, per browser. */
export function ChainProvider({ children }: { children: ReactNode }) {
  const { userId } = useAuth()
  const { data: me } = useMe()
  const { data: chains = [] } = useChains()
  // A choice made this session, tagged with the account it was made for.
  const [choice, setChoice] = useState<{ userId: number | null; chainId: number } | null>(null)

  const walletChains = me?.wallet_chains ?? []
  const preferred = choice && choice.userId === userId ? choice.chainId : readStoredChain(userId)
  const chainId = resolveChain(
    preferred,
    walletChains,
    chains.map(chain => chain.chain_id),
  )

  const setChainId = (next: number) => {
    writeStoredChain(userId, next)
    setChoice({ userId, chainId: next })
  }

  return (
    <ChainContext
      value={{
        chainId,
        setChainId,
        chains,
        walletChains,
        chain: chains.find(chain => chain.chain_id === chainId),
      }}
    >
      {children}
    </ChainContext>
  )
}
