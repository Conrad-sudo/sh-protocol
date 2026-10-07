import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { fetchTransactions, fetchTransactionTokens } from '../api/transactions'
import type { TransactionFilters } from '../api/types'

export const TRANSACTIONS_KEY = ['transactions'] as const

/** How often to look again while a transaction is still pending. */
const PENDING_POLL_MS = 15_000
/** How often to look again while the server searches for activity outside Mitfah. */
export const SYNC_POLL_MS = 5_000

/**
 * The account's transactions, newest first, a page at a time: every network's, or one network's,
 * and only those matching `filters`.
 *
 * Read afresh whenever the History tab opens, since a payment made in the chat or Telegram lands
 * here without this page knowing. Each read also settles anything still pending on the server, so
 * while a transaction is pending the list is read again every little while. The first read also
 * starts a search for activity outside Mitfah; while that runs, the list is read again sooner.
 */
export function useTransactions(chainId: number | null, filters: TransactionFilters = {}) {
  return useInfiniteQuery({
    queryKey: [...TRANSACTIONS_KEY, chainId ?? 'all', filters],
    queryFn: ({ pageParam }) => fetchTransactions(chainId, pageParam, filters),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: last => last.next_before ?? undefined,
    refetchOnMount: 'always',
    refetchInterval: query => {
      const pages = query.state.data?.pages
      if (pages?.[0]?.syncing) return SYNC_POLL_MS
      return pages?.some(page => page.transactions.some(t => t.status === 'pending')) ? PENDING_POLL_MS : false
    },
  })
}

/** The tickers the account's transactions have moved: what the token filter offers. */
export function useTransactionTokens(chainId: number | null) {
  return useQuery({
    queryKey: [...TRANSACTIONS_KEY, 'tokens', chainId ?? 'all'],
    queryFn: () => fetchTransactionTokens(chainId),
  })
}
