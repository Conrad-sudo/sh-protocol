import { useState } from 'react'
import { Form, NumberInput, Panel, SegmentedControl, Text } from 'rsuite'
import { formatUsd, formatWindow } from '../../lib/format'
import { WINDOW_CHOICES } from '../../lib/spending'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import type { ControlPanelProps } from './types'

/**
 * The limit and its period. Each editor is keyed on the value on chain, so once a change is
 * confirmed and the wallet is read again, it starts from the new value.
 */
export function SpendingPanel(props: ControlPanelProps) {
  const { spending } = props.wallet
  const periodSecs = Math.round(spending.window_hours * 3_600)
  return (
    <Panel bordered header={<h2>Spending limit</h2>} className="mf-card">
      <LimitEditor key={spending.daily_limit_usd} {...props} periodSecs={periodSecs} />
      <PeriodEditor key={periodSecs} {...props} periodSecs={periodSecs} />
    </Panel>
  )
}

function LimitEditor({ wallet, tx, locked, start, ask, periodSecs }: ControlPanelProps & { periodSecs: number }) {
  const current = wallet.spending.daily_limit_usd
  const [value, setValue] = useState<number | null>(current)
  const valid = value !== null && Number.isInteger(value) && value >= 0
  const changed = valid && value !== current

  const save = () => {
    if (!valid) return
    const request = {
      key: 'daily-limit',
      action: { kind: 'daily-limit', dailyLimitUsd: value },
      success: `Spending limit set to ${formatUsd(value)}.`,
    } as const
    if (value > current) {
      ask({
        title: 'Raise the spending limit?',
        body: (
          <Text>
            The assistant will be able to spend up to {formatUsd(value)} every {formatWindow(periodSecs)}, up from{' '}
            {formatUsd(current)}.
          </Text>
        ),
        confirmLabel: 'Raise limit',
        request,
      })
    } else {
      start(request)
    }
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={save}>
      <Form.Group controlId="daily-limit">
        <Form.Label>Limit per period</Form.Label>
        <div className="mf-inline-field">
          <NumberInput
            id="daily-limit"
            prefix="$"
            min={0}
            step={10}
            value={value ?? ''}
            disabled={locked}
            onChange={next => {
              const n = Number(next)
              setValue(next === '' || next === null || Number.isNaN(n) ? null : Math.floor(n))
            }}
          />
          <TxButton tx={tx} txKey="daily-limit" type="submit" appearance="primary" disabled={locked || !changed}>
            Save
          </TxButton>
        </div>
        {!valid ? (
          <Form.Text className="mf-error-text">Enter a whole number of dollars, $0 or more.</Form.Text>
        ) : value === 0 ? (
          <Form.Text>A $0 limit stops the assistant spending without turning it off.</Form.Text>
        ) : (
          <Form.Text>
            The most the assistant can spend per period, across all tokens. Lowering it doesn't undo what's already
            been spent.
          </Form.Text>
        )}
      </Form.Group>
      <TxStatus tx={tx} txKey="daily-limit" />
    </Form>
  )
}

function PeriodEditor({ tx, locked, start, ask, periodSecs }: ControlPanelProps & { periodSecs: number }) {
  const [value, setValue] = useState(periodSecs)
  // A period set some other way is shown too, so the control never claims a value it doesn't have.
  const choices = WINDOW_CHOICES.some(c => c.value === periodSecs)
    ? WINDOW_CHOICES
    : [...WINDOW_CHOICES, { label: formatWindow(periodSecs), value: periodSecs }].sort((a, b) => a.value - b.value)

  const save = () => {
    const request = {
      key: 'window',
      action: { kind: 'window', windowSecs: value },
      success: `The limit now refills every ${formatWindow(value)}.`,
    } as const
    if (value < periodSecs) {
      ask({
        title: 'Shorten the period?',
        body: (
          <Text>
            Your limit will refill every {formatWindow(value)} instead of every {formatWindow(periodSecs)}, so the
            assistant can spend more over time.
          </Text>
        ),
        confirmLabel: 'Shorten period',
        request,
      })
    } else {
      start(request)
    }
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={save}>
      <Form.Group controlId="window">
        <Form.Label>Period</Form.Label>
        <div className="mf-inline-field">
          <SegmentedControl
            aria-label="Period"
            data={choices}
            value={value}
            disabled={locked}
            onChange={next => setValue(Number(next))}
          />
          <TxButton tx={tx} txKey="window" type="submit" appearance="primary" disabled={locked || value === periodSecs}>
            Save
          </TxButton>
        </div>
        <Form.Text>
          The limit refills every {formatWindow(value)}. Changing it doesn't restart the current period.
        </Form.Text>
      </Form.Group>
      <TxStatus tx={tx} txKey="window" />
    </Form>
  )
}
