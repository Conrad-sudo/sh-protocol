import { useQuery } from '@tanstack/react-query'
import { fetchAgents } from '../api/wallet'

/** The query key of a wallet's trading agents; an owner transaction that changes them sets it. */
export const agentsKey = (chainId: number) => ['agents', chainId] as const

/** The wallet's trading agents on `chainId`, read from the chain, with what each has spent. */
export function useAgents(chainId: number) {
  return useQuery({
    queryKey: agentsKey(chainId),
    queryFn: ({ signal }) => fetchAgents(chainId, signal),
    // Read by two panels of one page: once is enough.
    staleTime: 15_000,
    // Agents trade by themselves: a trade made while the page is open shows within a minute.
    refetchInterval: 60_000,
  })
}
