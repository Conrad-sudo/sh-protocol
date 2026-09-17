import { useQuery } from '@tanstack/react-query'
import { fetchChains } from '../api/chains'

/** The networks this server can deploy wallets on. Public, and rarely changes. */
export function useChains() {
  return useQuery({ queryKey: ['chains'], queryFn: fetchChains, staleTime: 5 * 60_000 })
}
