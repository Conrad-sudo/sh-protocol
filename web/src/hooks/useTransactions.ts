import { useInfiniteQuery } from '@tanstack/react-query'
import { fetchTransactions } from '../api/transactions'

export const TRANSACTIONS_KEY = ['transactions'] as const

/** How often to look again while a transaction is still pending. */
const PENDING_POLL_MS = 15_000

/**
 * The account's transactions, newest first, a page at a time: every network's, or one network's.
 *
 * Read afresh whenever the History tab opens, since a payment made in the chat or Telegram lands
 * here without this page knowing. Each read also settles anything still pending on the server, so
 * while a transaction is pending the list is read again every little while.
 */
export function useTransactions(chainId: number | null) {
  return useInfiniteQuery({
    queryKey: [...TRANSACTIONS_KEY, chainId ?? 'all'],
    queryFn: ({ pageParam }) => fetchTransactions(chainId, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: last => last.next_before ?? undefined,
    refetchOnMount: 'always',
    refetchInterval: query =>
      query.state.data?.pages.some(page => page.transactions.some(t => t.status === 'pending'))
        ? PENDING_POLL_MS
        : false,
  })
}
