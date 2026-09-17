import { Panel, ProgressCircle, Text } from 'rsuite'
import type { WalletState } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { formatTimeLeft, formatUsd, formatWindow } from '../../lib/format'

const RING = 128
const endsAtFormat = new Intl.DateTimeFormat(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })

/**
 * How much the assistant may still spend. The period is fixed from its start and only restarts on
 * the first spend after it ends, so once it has ended the whole limit is available again even
 * though the stored "spent" figure has not been reset yet.
 */
export function SpendingCard({ spending }: { spending: WalletState['spending'] }) {
  const now = useNow(30_000)
  const limit = spending.daily_limit_usd
  const periodSecs = Math.round(spending.window_hours * 3_600)
  const endsAt = (spending.window_start + periodSecs) * 1_000
  const ended = now >= endsAt
  const left = ended ? limit : Math.min(Math.max(spending.remaining_usd, 0), limit)
  const spent = limit - left
  const percentLeft = limit > 0 ? Math.round((left / limit) * 100) : 0

  let status: string
  if (limit === 0) status = "The limit is $0, so the assistant can't spend anything."
  else if (ended) status = 'Full limit available. A new period starts with the next spend.'
  else status = `Resets in ${formatTimeLeft(endsAt - now)}.`

  return (
    <Panel bordered header={<h2>Spending limit</h2>} className="mf-card">
      <div className="mf-spending">
        <ProgressCircle
          percent={percentLeft}
          // `width` sizes only the ring; the wrapper must match or the label centres on the whole row.
          width={RING}
          style={{ width: RING }}
          strokeWidth={8}
          trailWidth={8}
          strokeColor="var(--mf-green)"
          aria-label={`${percentLeft}% of the limit left`}
          renderInfo={() => (
            <span className="mf-spending-left">
              <strong className="mf-num">{formatUsd(left)}</strong>
              <small>left</small>
            </span>
          )}
        />
        <dl className="mf-facts">
          <div>
            <dt>Spent this period</dt>
            <dd className="mf-num">{formatUsd(spent)}</dd>
          </div>
          <div>
            <dt>Limit</dt>
            <dd className="mf-num">
              {formatUsd(limit)} every {formatWindow(periodSecs)}
            </dd>
          </div>
        </dl>
      </div>
      <Text size="sm" muted title={ended || limit === 0 ? undefined : endsAtFormat.format(endsAt)}>
        {status}
      </Text>
    </Panel>
  )
}
