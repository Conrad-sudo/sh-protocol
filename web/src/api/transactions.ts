import { apiFetch } from './client'
import type { TransactionPage } from './types'

/** How many transactions to load at a time. The server allows up to 100. */
export const TRANSACTIONS_PAGE_SIZE = 25

/** A page of the account's transactions, newest first: every network's, or one network's. */
export function fetchTransactions(chainId: number | null, before?: number) {
  const params = new URLSearchParams({ limit: String(TRANSACTIONS_PAGE_SIZE) })
  if (chainId !== null) params.set('chain_id', String(chainId))
  if (before !== undefined) params.set('before', String(before))
  return apiFetch<TransactionPage>(`/api/transactions?${params}`)
}

/**
 * Lists a deposit the Fund drawer just sent in the History tab. The server checks that the
 * transaction really went to this account's wallet.
 */
export async function reportDeposit(chainId: number, txHash: string) {
  await apiFetch<{ status: 'ok' }>('/api/transactions/deposit', {
    method: 'POST',
    body: { chain_id: chainId, tx_hash: txHash },
  })
}
