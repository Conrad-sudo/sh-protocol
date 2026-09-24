import { Panel, Text } from 'rsuite'
import type { WalletState } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { formatTimeLeft, formatUsd, formatWindow } from '../../lib/format'
import { summarizeSpending } from '../../lib/spending'
import { LimitDial } from '../LimitDial'

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
        <LimitDial percent={percentLeft} label={`${percentLeft}% of the limit left`} size={200}>
          <strong className="mf-dial-amount mf-num">{formatUsd(left)}</strong>
          <small>left</small>
        </LimitDial>
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
