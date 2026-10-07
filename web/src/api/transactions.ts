import { apiFetch } from './client'
import type { DepositConfirmResult, TransactionFilters, TransactionPage, TransactionTokens } from './types'

/** How many transactions to load at a time. The server allows up to 100. */
export const TRANSACTIONS_PAGE_SIZE = 25

/**
 * A page of the account's transactions, newest first: every network's, or one network's, and only
 * those matching `filters`.
 */
export function fetchTransactions(chainId: number | null, before?: string, filters: TransactionFilters = {}) {
  const params = new URLSearchParams({ limit: String(TRANSACTIONS_PAGE_SIZE) })
  if (chainId !== null) params.set('chain_id', String(chainId))
  if (before !== undefined) params.set('before', before)
  for (const [name, value] of Object.entries(filters)) {
    if (value !== undefined) params.set(name, String(value))
  }
  return apiFetch<TransactionPage>(`/api/transactions?${params}`)
}

/** The tickers the account's transactions have moved, for the History tab's token filter. */
export function fetchTransactionTokens(chainId: number | null) {
  const query = chainId === null ? '' : `?chain_id=${chainId}`
  return apiFetch<TransactionTokens>(`/api/transactions/tokens${query}`)
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
