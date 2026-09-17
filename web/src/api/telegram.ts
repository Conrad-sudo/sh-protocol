import { apiFetch } from './client'
import type { TelegramLink } from './types'

/** Mints a single-use t.me link that ties the Telegram chat which follows it to this account. */
export function createTelegramLink() {
  return apiFetch<TelegramLink>('/api/integrations/telegram/link', { method: 'POST' })
}

/** Detaches the linked Telegram chat from this account. */
export function unlinkTelegram() {
  return apiFetch<{ status: string }>('/api/integrations/telegram/link', { method: 'DELETE' })
}
