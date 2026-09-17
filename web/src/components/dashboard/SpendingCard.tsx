import { Panel, ProgressCircle, Text } from 'rsuite'
import type { WalletState } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { formatTimeLeft, formatUsd, formatWindow } from '../../lib/format'
import { summarizeSpending } from '../../lib/spending'

const RING = 128
const endsAtFormat = new Intl.DateTimeFormat(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })

/** How much the assistant may still spend, and when the period resets (see summarizeSpending). */
export function SpendingCard({ spending }: { spending: WalletState['spending'] }) {
  const now = useNow(30_000)
  const { limit, left, spent, percentLeft, periodSecs, endsAt, ended } = summarizeSpending(spending, now)

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
