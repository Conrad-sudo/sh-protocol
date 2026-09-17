import { useContext } from 'react'
import { ChainContext, type ChainState } from './ChainContext'

export function useSelectedChain(): ChainState {
  const state = useContext(ChainContext)
  if (!state) throw new Error('useSelectedChain must be used inside <ChainProvider>')
  return state
}
