import { useState } from 'react'
import { Form, Input, InputGroup, Placeholder, SelectPicker, Text } from 'rsuite'
import { getAddress, isAddress, parseUnits } from 'viem'
import type { RecurringPayment, RecurringPayments, TokenBalance } from '../../api/types'
import { useContacts } from '../../hooks/useContacts'
import { useNow } from '../../hooks/useNow'
import { useRecurring } from '../../hooks/useRecurring'
import { formatDate, formatDateTime, formatTokenAmount, isValidAmount } from '../../lib/format'
import { AmountInput } from '../AmountInput'
import { GlassPanel } from '../Glass'
import { QueryError } from '../QueryError'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import type { ControlPanelProps } from './types'

const CREATE_KEY = 'recurring-create'
const OTHER = 'other'

/** The units an interval is entered in. A month is 30 days: the module counts seconds, not calendars. */
const UNITS = [
  { value: 3_600, label: 'hours' },
  { value: 86_400, label: 'days' },
  { value: 604_800, label: 'weeks' },
  { value: 2_592_000, label: 'months (30 days)' },
]

/** "every 30 days", "every week": how an interval reads in a sentence. */
function everyText(seconds: number): string {
  for (const [unit, size] of [
    ['week', 604_800],
    ['day', 86_400],
    ['hour', 3_600],
  ] as const) {
    if (seconds % size === 0 && seconds >= size) {
      const n = seconds / size
      return n === 1 ? `every ${unit}` : `every ${n} ${unit}s`
    }
  }
  return `every ${seconds} seconds`
}

/** Unix seconds for the start of a `YYYY-MM-DD` day in the reader's time zone, or null for none. */
function dayStart(value: string): number | null {
  if (!value) return null
  const ms = new Date(`${value}T00:00:00`).getTime()
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000)
}

type Payable = TokenBalance & { decimals: number }

/**
 * Payments the wallet makes by itself on a schedule: a fixed amount to one address, every so often.
 * The owner signs each schedule once, on the wallet's recurring payments module; Mitfah then pays
 * every instalment when it falls due, through the wallet's spending limit like any payment.
 */
export function RecurringPanel(props: ControlPanelProps) {
  const recurring = useRecurring(props.wallet.chain_id)

  return (
    <GlassPanel bordered header={<h2>Recurring payments</h2>} className="mf-card">
      {recurring.isPending ? (
        <Placeholder.Paragraph rows={2} active />
      ) : recurring.isError ? (
        <QueryError what="the recurring payments" onRetry={() => void recurring.refetch()} className="mf-settings-note" />
      ) : !recurring.data.available ? (
        <Text muted>
          This wallet can't make recurring payments: it was created before they existed, or their module was removed.
        </Text>
      ) : (
        <Schedules {...props} recurring={recurring.data} />
      )}
    </GlassPanel>
  )
}

function Schedules(props: ControlPanelProps & { recurring: RecurringPayments }) {
  const { tx, locked, start, recurring } = props
  const live = recurring.schedules.filter(s => s.status === 'live')
  const fee =
    recurring.fee_per_payment_wei !== null
      ? `${formatTokenAmount(recurring.fee_per_payment_wei, 18)} ${recurring.native_ticker}`
      : null

  return (
    <>
      {live.length > 0 ? (
        <ul className="mf-token-list" aria-label="Recurring payments">
          {live.map(schedule => {
            const key = `recurring-cancel:${schedule.schedule_id}`
            return (
              <li key={schedule.schedule_id}>
                <div className="mf-token-row">
                  <span className="mf-token">{schedule.summary}</span>
                  <TxButton
                    tx={tx}
                    txKey={key}
                    size="sm"
                    appearance="ghost"
                    color="orange"
                    disabled={locked}
                    aria-label={`Cancel the payment of ${schedule.summary}`}
                    onClick={() =>
                      start({
                        key,
                        action: { kind: 'recurring', action: 'cancel', scheduleId: schedule.schedule_id },
                        success: 'Recurring payment cancelled.',
                      })
                    }
                  >
                    Cancel
                  </TxButton>
                </div>
                <ScheduleNote schedule={schedule} />
                <TxStatus tx={tx} txKey={key} />
              </li>
            )
          })}
        </ul>
      ) : (
        <Text muted>No recurring payments yet.</Text>
      )}
      <NewRecurringPayment {...props} fee={fee} />
      <Text size="sm" muted className="mf-settings-note">
        Mitfah sends each payment when it falls due, once per period. Each one counts toward the spending limit when its
        token counts{fee ? `, and pays the wallet's fee of ${fee}` : ''}. One that can't go through (over the limit,
        paused, not enough funds) is tried again later, and a missed period is never paid twice.
      </Text>
    </>
  )
}

/** When the next payment is due, and why the last attempt didn't go through, if it didn't. */
function ScheduleNote({ schedule }: { schedule: RecurringPayment }) {
  const ends = schedule.end_at !== null ? ` · ends ${formatDate(schedule.end_at * 1000)}` : ''
  return (
    <>
      <Text size="sm" muted>
        Next payment {formatDateTime(schedule.next_due * 1000)}
        {ends}
      </Text>
      {schedule.last_error && (
        <Text size="sm" className="mf-error-text">
          The last attempt couldn't go through: {schedule.last_error}.
          {schedule.retry_at !== null && ` Trying again ${formatDateTime(schedule.retry_at * 1000)}.`}
        </Text>
      )}
    </>
  )
}

function NewRecurringPayment({
  wallet,
  tx,
  locked,
  ask,
  recurring,
  fee,
}: ControlPanelProps & { recurring: RecurringPayments; fee: string | null }) {
  const contacts = useContacts()
  // For "is this date in the past": a minute's drift changes nothing that matters here.
  const now = useNow(60_000)
  const payable = wallet.balances.filter((b): b is Payable => b.decimals !== null && !b.lp)
  const [token, setToken] = useState<string | null>(payable[0] ? (payable[0].address ?? payable[0].ticker) : null)
  const [amount, setAmount] = useState('')
  const [payee, setPayee] = useState<string | null>(null)
  const [address, setAddress] = useState('')
  const [count, setCount] = useState('30')
  const [unit, setUnit] = useState(86_400)
  const [startDay, setStartDay] = useState('')
  const [endDay, setEndDay] = useState('')

  const people = contacts.data ?? []
  const chosen = payable.find(b => (b.address ?? b.ticker) === token)
  const ticker = chosen?.ticker.toUpperCase()
  const to = payee === OTHER || people.length === 0 ? address : (people.find(c => c.name === payee)?.address ?? '')
  const intervalSecs = Number.parseInt(count, 10) * unit
  const firstDue = dayStart(startDay)
  const endAt = dayStart(endDay)

  const amountError =
    amount === '' || !chosen
      ? null
      : !isValidAmount(amount)
        ? 'Enter an amount like 0.5.'
        : (amount.split('.')[1] ?? '').length > chosen.decimals
          ? `${ticker} can't be split into more than ${chosen.decimals} decimal places.`
          : parseUnits(amount, chosen.decimals) === 0n
            ? 'Enter more than zero.'
            : null
  const toError =
    to === ''
      ? null
      : !isAddress(to, { strict: false })
        ? 'Enter a full address starting with 0x.'
        : getAddress(to) === getAddress(wallet.address)
          ? "The wallet can't pay itself."
          : null
  const intervalError =
    count === ''
      ? null
      : !/^\d+$/.test(count) || intervalSecs < recurring.min_interval_secs
        ? 'Pay at most once an hour.'
        : intervalSecs > 366 * 86_400
          ? 'Pay at least once a year.'
          : null
  const endError = endAt !== null && endAt < (firstDue ?? Math.floor(now / 1000)) ? 'The end is before the first payment.' : null
  const valid =
    chosen !== undefined &&
    amount !== '' &&
    to !== '' &&
    count !== '' &&
    !amountError &&
    !toError &&
    !intervalError &&
    !endError

  const save = () => {
    if (!valid || !chosen) return
    const recipient = getAddress(to)
    const who = people.find(c => getAddress(c.address) === recipient)?.name ?? recipient
    const starts = firstDue !== null && firstDue * 1000 > now ? `from ${formatDate(firstDue * 1000)}` : 'starting now'
    ask({
      title: 'Set up this recurring payment?',
      body: (
        <Text>
          The wallet will send {amount} {ticker} to {who} {everyText(intervalSecs)}, {starts},{' '}
          {endAt !== null ? `until ${formatDate(endAt * 1000)}` : 'until you cancel it'}, without asking again. Each payment
          counts toward the spending limit when {ticker} counts{fee ? ` and pays a fee of ${fee}` : ''}.
        </Text>
      ),
      confirmLabel: 'Set up',
      request: {
        key: CREATE_KEY,
        action: {
          kind: 'recurring',
          action: 'create',
          token: chosen.address ?? chosen.ticker,
          amount,
          to: recipient,
          intervalSecs,
          ...(firstDue !== null ? { firstDue } : {}),
          ...(endAt !== null ? { endAt } : {}),
        },
        success: 'Recurring payment set up.',
      },
    })
  }

  if (payable.length === 0) {
    return (
      <Text size="sm" muted className="mf-settings-note">
        Add funds to the wallet to set up a recurring payment.
      </Text>
    )
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={save}>
      <Form.Group controlId="recurring-token">
        <Form.Label>Pay with</Form.Label>
        <SelectPicker
          id="recurring-token"
          aria-labelledby="recurring-token-label"
          block
          cleanable={false}
          searchable={false}
          data={payable.map(b => ({ value: b.address ?? b.ticker, label: b.ticker.toUpperCase() }))}
          value={token}
          onChange={setToken}
          disabled={locked}
        />
      </Form.Group>
      <Form.Group controlId="recurring-amount">
        <Form.Label>Amount each time</Form.Label>
        <AmountInput id="recurring-amount" value={amount} onChange={setAmount} unit={ticker} disabled={locked} />
        {amountError && <Form.Text className="mf-error-text">{amountError}</Form.Text>}
      </Form.Group>
      {people.length > 0 && (
        <Form.Group controlId="recurring-payee">
          <Form.Label>Pay to</Form.Label>
          <SelectPicker
            id="recurring-payee"
            aria-labelledby="recurring-payee-label"
            block
            searchable={people.length > 8}
            placeholder="Choose a contact"
            data={[
              ...people.map(c => ({ value: c.name, label: c.name })),
              { value: OTHER, label: 'Another address' },
            ]}
            value={payee}
            onChange={setPayee}
            disabled={locked}
          />
        </Form.Group>
      )}
      {(people.length === 0 || payee === OTHER) && (
        <Form.Group controlId="recurring-address">
          <Form.Label>{people.length === 0 ? 'Pay to' : 'Address'}</Form.Label>
          <Input
            id="recurring-address"
            className="mf-mono"
            autoComplete="off"
            spellCheck={false}
            placeholder="0x…"
            value={address}
            onChange={value => setAddress(value.trim())}
            disabled={locked}
          />
          {toError && <Form.Text className="mf-error-text">{toError}</Form.Text>}
        </Form.Group>
      )}
      {payee !== OTHER && people.length > 0 && toError && <Form.Text className="mf-error-text">{toError}</Form.Text>}
      <Form.Group controlId="recurring-every">
        <Form.Label>Every</Form.Label>
        <div className="mf-inline-field">
          <InputGroup>
            <Input id="recurring-every" inputMode="numeric" value={count} onChange={setCount} disabled={locked} />
          </InputGroup>
          <SelectPicker
            aria-label="Interval unit"
            cleanable={false}
            searchable={false}
            data={UNITS}
            value={unit}
            onChange={value => setUnit(value ?? 86_400)}
            disabled={locked}
          />
        </div>
        {intervalError && <Form.Text className="mf-error-text">{intervalError}</Form.Text>}
      </Form.Group>
      <Form.Group controlId="recurring-start">
        <Form.Label>First payment (optional)</Form.Label>
        <Input id="recurring-start" type="date" value={startDay} onChange={setStartDay} disabled={locked} />
        <Form.Text>Leave empty to pay the first one now.</Form.Text>
      </Form.Group>
      <Form.Group controlId="recurring-end">
        <Form.Label>Last payment (optional)</Form.Label>
        <Input id="recurring-end" type="date" value={endDay} onChange={setEndDay} disabled={locked} />
        {endError ? (
          <Form.Text className="mf-error-text">{endError}</Form.Text>
        ) : (
          <Form.Text>Leave empty to keep paying until you cancel it.</Form.Text>
        )}
      </Form.Group>
      <TxButton tx={tx} txKey={CREATE_KEY} type="submit" appearance="primary" disabled={locked || !valid}>
        Set up payment
      </TxButton>
      <TxStatus tx={tx} txKey={CREATE_KEY} />
    </Form>
  )
}
