import { apiFetch } from './client'
import type { TelegramLink } from './types'

/**
 * Mints a single-use t.me link that ties the Telegram chat which follows it to this account. It
 * opens the bot of `chainId`'s network; one link covers every network's bot.
 */
export function createTelegramLink(chainId?: number | null) {
  const query = chainId == null ? '' : `?chain_id=${chainId}`
  return apiFetch<TelegramLink>(`/api/integrations/telegram/link${query}`, { method: 'POST' })
}

/** Detaches the linked Telegram chat from this account. */
export function unlinkTelegram() {
  return apiFetch<{ status: string }>('/api/integrations/telegram/link', { method: 'DELETE' })
}

/** Opens a chat with a bot in Telegram. */
export function telegramBotUrl(username: string) {
  return `https://t.me/${username}`
}
