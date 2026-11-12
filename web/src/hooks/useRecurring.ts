import { useQuery } from '@tanstack/react-query'
import { fetchRecurring } from '../api/wallet'

/** The query key of a wallet's recurring payments; an owner transaction that changes them sets it. */
export const recurringKey = (chainId: number) => ['recurring', chainId] as const

/** The wallet's recurring payments on `chainId`, read from the chain with how the last attempt of each went. */
export function useRecurring(chainId: number) {
  return useQuery({
    queryKey: recurringKey(chainId),
    queryFn: ({ signal }) => fetchRecurring(chainId, signal),
    // The keeper pays in the background: a payment made while the page is open shows within a minute.
    refetchInterval: 60_000,
  })
}
