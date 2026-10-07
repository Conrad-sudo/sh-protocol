import { type FormEvent, useId, useState } from 'react'
import CloseIcon from '@rsuite/icons/Close'
import { Button, Input, SelectPicker } from 'rsuite'
import type { Contact } from '../../api/types'
import { formatDate, shortAddress } from '../../lib/format'
import {
  type DraftProblems,
  draftOf,
  draftProblems,
  type FilterDraft,
  type HistoryView,
  NO_FILTERS,
  startOfDay,
  today,
  viewOf,
} from '../../lib/historyFilters'
import { AmountInput } from '../AmountInput'
import { GlassLayer } from '../Glass'

interface FilterPanelProps {
  id: string
  view: HistoryView
  contacts: Contact[]
  /** The tickers the account's transactions have moved. */
  tokens: string[]
  onApply: (view: HistoryView) => void
}

/**
 * The History tab's filters: a date range, and -- for transactions that move money -- an amount
 * range, the other side (an address or a contact) and a token. Nothing is searched until Apply.
 */
export function FilterPanel({ id, view, contacts, tokens, onApply }: FilterPanelProps) {
  const [draft, setDraft] = useState<FilterDraft>(() => draftOf(view, contacts))
  // Problems show from the first Apply on, so a half-typed field isn't called wrong.
  const [tried, setTried] = useState(false)
  const field = useId()
  const problems: DraftProblems = tried ? draftProblems(draft, contacts) : {}
  // A wallet change moves no money, so only its date can narrow it.
  const money = view.group !== 'none'
  const set = (name: keyof FilterDraft) => (value: string) => setDraft(d => ({ ...d, [name]: value }))

  const apply = (event: FormEvent) => {
    event.preventDefault()
    setTried(true)
    if (Object.keys(draftProblems(draft, contacts)).length) return
    onApply(viewOf(view.group, draft, contacts))
  }

  return (
    <form id={id} className="mf-tx-filters mf-glass-surface" aria-label="Filter transactions" onSubmit={apply} noValidate>
      <GlassLayer />
      <div className="mf-tx-filters-body">
        <fieldset className="mf-tx-filter-pair">
          <legend>Date</legend>
          <div className="mf-tx-filter-field">
            <label htmlFor={`${field}-from`}>From</label>
            <Input id={`${field}-from`} type="date" max={draft.to || today()} value={draft.from} onChange={set('from')} />
          </div>
          <div className="mf-tx-filter-field">
            <label htmlFor={`${field}-to`}>To</label>
            <Input id={`${field}-to`} type="date" min={draft.from} max={today()} value={draft.to} onChange={set('to')} />
          </div>
          {problems.dates && <p className="mf-error-text">{problems.dates}</p>}
        </fieldset>

        {money && (
          <>
            <fieldset className="mf-tx-filter-pair">
              <legend>Amount</legend>
              <div className="mf-tx-filter-field">
                <label htmlFor={`${field}-min`}>At least</label>
                <AmountInput id={`${field}-min`} value={draft.min} onChange={set('min')} />
              </div>
              <div className="mf-tx-filter-field">
                <label htmlFor={`${field}-max`}>At most</label>
                <AmountInput id={`${field}-max`} value={draft.max} onChange={set('max')} />
              </div>
              {problems.amounts && <p className="mf-error-text">{problems.amounts}</p>}
            </fieldset>

            <div className="mf-tx-filter-field">
              <label htmlFor={`${field}-who`}>Address or contact</label>
              <Input
                id={`${field}-who`}
                list={`${field}-contacts`}
                autoComplete="off"
                spellCheck={false}
                placeholder="0x… or a contact's name"
                value={draft.who}
                onChange={set('who')}
              />
              <datalist id={`${field}-contacts`}>
                {contacts.map(contact => (
                  <option key={contact.address} value={contact.name} />
                ))}
              </datalist>
              <p className="mf-tx-filter-hint">Who sent it or received it, or the token's contract.</p>
              {problems.who && <p className="mf-error-text">{problems.who}</p>}
            </div>

            <div className="mf-tx-filter-field">
              <label id={`${field}-token-label`}>Token</label>
              <SelectPicker
                // A <label for> can't name the picker's div, so point at the label.
                aria-labelledby={`${field}-token-label`}
                block
                searchable={tokens.length > 8}
                placeholder="Any token"
                data={tokens.map(ticker => ({ label: ticker, value: ticker }))}
                value={draft.token || null}
                onChange={value => set('token')(value ?? '')}
              />
            </div>
          </>
        )}

        <div className="mf-tx-filters-actions">
          <Button appearance="subtle" onClick={() => onApply({ group: view.group, ...NO_FILTERS })}>
            Clear
          </Button>
          <Button appearance="primary" type="submit">
            Apply
          </Button>
        </div>
      </div>
    </form>
  )
}

interface FilterChipsProps {
  view: HistoryView
  nameOf: (address: string) => string | undefined
  onChange: (view: HistoryView) => void
}

/** The filters in use, each with a button that takes it off. */
export function FilterChips({ view, nameOf, onChange }: FilterChipsProps) {
  const day = (value: string) => formatDate(startOfDay(value) * 1_000)
  const chips: { key: keyof typeof NO_FILTERS; label: string }[] = []
  if (view.from) chips.push({ key: 'from', label: `From ${day(view.from)}` })
  if (view.to) chips.push({ key: 'to', label: `To ${day(view.to)}` })
  if (view.min) chips.push({ key: 'min', label: `At least ${view.min}` })
  if (view.max) chips.push({ key: 'max', label: `At most ${view.max}` })
  if (view.address) chips.push({ key: 'address', label: `With ${nameOf(view.address) ?? shortAddress(view.address)}` })
  if (view.token) chips.push({ key: 'token', label: view.token })
  if (chips.length === 0) return null

  return (
    <ul className="mf-tx-chips" aria-label="Filters in use">
      {chips.map(chip => (
        <li key={chip.key} className="mf-tx-chip">
          <span>{chip.label}</span>
          <button
            type="button"
            className="mf-tx-chip-remove"
            aria-label={`Remove filter: ${chip.label}`}
            onClick={() => onChange({ ...view, [chip.key]: '' })}
          >
            <CloseIcon aria-hidden />
          </button>
        </li>
      ))}
      {chips.length > 1 && (
        <li>
          <Button appearance="link" size="sm" onClick={() => onChange({ group: view.group, ...NO_FILTERS })}>
            Clear all
          </Button>
        </li>
      )}
    </ul>
  )
}
