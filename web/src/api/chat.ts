import { apiFetch } from './client'
import type { ChatMessage } from './types'

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
