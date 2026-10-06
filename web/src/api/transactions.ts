import { apiFetch } from './client'
import type { DepositConfirmResult, TransactionPage } from './types'

/** How many transactions to load at a time. The server allows up to 100. */
export const TRANSACTIONS_PAGE_SIZE = 25

/** A page of the account's transactions, newest first: every network's, or one network's. */
export function fetchTransactions(chainId: number | null, before?: string) {
  const params = new URLSearchParams({ limit: String(TRANSACTIONS_PAGE_SIZE) })
  if (chainId !== null) params.set('chain_id', String(chainId))
  if (before !== undefined) params.set('before', before)
  return apiFetch<TransactionPage>(`/api/transactions?${params}`)
}

/**
 * Waits for a deposit the Fund drawer sent, and lists it in the History tab. Answers `pending` (HTTP
 * 202) until it has mined; the server checks that it really went to this account's wallet.
 */
export function confirmDeposit(chainId: number, txHash: string, signal?: AbortSignal) {
  return apiFetch<DepositConfirmResult>('/api/transactions/deposit', {
    method: 'POST',
    body: { chain_id: chainId, tx_hash: txHash },
    signal,
  })
}
