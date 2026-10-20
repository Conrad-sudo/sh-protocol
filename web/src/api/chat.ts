import { apiFetch } from './client'
import type { ChatMessage, QuoteOffer } from './types'

/** How many past messages to show. The server allows up to 200. */
const HISTORY_LIMIT = 100

/** The conversation on one network, oldest first. Shared with Telegram. */
export async function fetchChatHistory(chainId: number) {
  const { messages } = await apiFetch<{ chain_id: number; messages: ChatMessage[] }>(
    `/api/chat/history?chain_id=${chainId}&limit=${HISTORY_LIMIT}`,
  )
  return messages
}

/**
 * Runs one turn of the assistant. The server answers only when the turn is over, which can take a
 * minute when the assistant sends a transaction and waits for it.
 */
export async function sendChat(chainId: number, message: string) {
  const { reply } = await apiFetch<{ reply: string }>('/api/chat', {
    method: 'POST',
    body: { chain_id: chainId, message },
  })
  return reply
}

/**
 * Deletes the conversation on one network, or on every network when `chainId` is left out. The
 * server answers 409 while the assistant is still answering a message there.
 */
export async function deleteChatHistory(chainId?: number) {
  const query = chainId === undefined ? '' : `?chain_id=${chainId}`
  await apiFetch<{ status: 'cleared'; chain_ids: number[] }>(`/api/chat/history${query}`, { method: 'DELETE' })
}

/** The quote waiting for a Confirm or Cancel tap on one network, or null. */
export async function fetchQuoteOffer(chainId: number) {
  const { quote } = await apiFetch<{ quote: QuoteOffer | null }>(`/api/chat/quote?chain_id=${chainId}`)
  return quote
}

/**
 * Taps a quote card's Confirm or Cancel. Confirm answers once the transaction is on the wire, with
 * the History row to follow while it lands (`transaction_id`; null when nothing went out). The
 * server answers 409 when the quote is gone or a message is still being answered.
 *
 * @return  What happened, as the server worded it, and the row to follow.
 */
export async function answerQuote(chainId: number, quoteId: string, confirm: boolean) {
  const { outcome, transaction_id = null } = await apiFetch<{ outcome: string; transaction_id?: number | null }>(
    `/api/chat/quote/${confirm ? 'confirm' : 'cancel'}`,
    {
      method: 'POST',
      body: { chain_id: chainId, quote_id: quoteId },
    },
  )
  return { outcome, transactionId: transaction_id }
}
