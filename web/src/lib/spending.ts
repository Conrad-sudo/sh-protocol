import type { WalletState } from '../api/types'

/** The spending periods offered when choosing a limit, in seconds. A month is 30 days. */
export const WINDOW_CHOICES = [
  { label: '12 hours', value: 43_200 },
  { label: '24 hours', value: 86_400 },
  { label: '7 days', value: 604_800 },
  { label: '1 month', value: 2_592_000 },
]

/**
 * Whether anything counts toward the USD limit: the native asset, or at least one token. When
 * nothing does the limit binds nothing, whatever its figure (a wallet made with no limit holds $0),
 * so the app says "no limit" rather than showing it.
 */
export function limitApplies(spending: Pick<WalletState['spending'], 'native_metered' | 'watched_tokens'>): boolean {
  return spending.native_metered || spending.watched_tokens.length > 0
}

export interface SpendingSummary {
  limit: number
  left: number
  spent: number
  percentLeft: number
  periodSecs: number
  /** When the current period ends, in milliseconds. */
  endsAt: number
  /** The period is over: the whole limit is available until the next spend starts a new one. */
  ended: boolean
}

/**
 * What the assistant may still spend at `now`. The period is fixed from its start and only restarts
 * on the first spend after it ends, so once it has ended the whole limit is available again even
 * though the stored "spent" figure has not been reset yet.
 */
export function summarizeSpending(
  spending: Pick<WalletState['spending'], 'daily_limit_usd' | 'remaining_usd' | 'window_hours' | 'window_start'>,
  now: number,
): SpendingSummary {
  const limit = spending.daily_limit_usd
  const periodSecs = Math.round(spending.window_hours * 3_600)
  const endsAt = (spending.window_start + periodSecs) * 1_000
  const ended = now >= endsAt
  const left = ended ? limit : Math.min(Math.max(spending.remaining_usd, 0), limit)
  return {
    limit,
    left,
    spent: limit - left,
    percentLeft: limit > 0 ? Math.round((left / limit) * 100) : 0,
    periodSecs,
    endsAt,
    ended,
  }
}
