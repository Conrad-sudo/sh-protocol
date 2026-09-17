import type { WalletState } from '../api/types'

/** The spending periods offered when choosing a limit, in seconds. */
export const WINDOW_CHOICES = [
  { label: '12 hours', value: 43_200 },
  { label: '24 hours', value: 86_400 },
  { label: '7 days', value: 604_800 },
]

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
