import { useQuery } from '@tanstack/react-query'
import { fetchTokens } from '../api/wallet'

/** Tokens a wallet on `chainId` can count toward its limit. The list rarely changes. */
export function useTokens(chainId: number) {
  return useQuery({ queryKey: ['tokens', chainId], queryFn: () => fetchTokens(chainId), staleTime: 5 * 60_000 })
}
