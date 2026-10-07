import { getAddress, isAddress } from 'viem'
import type { Contact, TransactionDirection, TransactionFilters } from '../api/types'
import { normalizeName } from './contacts'
import { isValidAmount } from './format'

/** The History tab's groups, as its tabs offer them. */
export type HistoryGroup = 'all' | TransactionDirection

/**
 * What the History tab shows. It lives in the page's address (`?type=out&from=2026-10-01&…`), so a
 * refresh or a shared link keeps it. Dates are calendar days (YYYY-MM-DD) in the reader's own time
 * zone, amounts are as typed, and the address is always a 0x address, never a contact's name.
 */
export interface HistoryView {
  group: HistoryGroup
  from: string
  to: string
  min: string
  max: string
  address: string
  token: string
}

/** The filter panel's fields while they are being edited: `who` is an address or a contact's name. */
export interface FilterDraft {
  from: string
  to: string
  min: string
  max: string
  who: string
  token: string
}

export const NO_FILTERS: Omit<HistoryView, 'group'> = { from: '', to: '', min: '', max: '', address: '', token: '' }

const GROUPS: readonly HistoryGroup[] = ['all', 'in', 'out', 'none']
const DAY = /^\d{4}-\d{2}-\d{2}$/
// A ticker as the server takes one (app/api.py list_transactions).
const MAX_TICKER = 40

/** The view in the page's address. Anything malformed is left out rather than sent. */
export function readView(params: URLSearchParams): HistoryView {
  const get = (name: string) => params.get(name)?.trim() ?? ''
  const type = get('type') as HistoryGroup
  const address = get('address')
  const day = (value: string) => (DAY.test(value) && !Number.isNaN(startOfDay(value)) ? value : '')
  const amount = (value: string) => (isValidAmount(value) ? value : '')
  return {
    group: GROUPS.includes(type) ? type : 'all',
    from: day(get('from')),
    to: day(get('to')),
    min: amount(get('min')),
    max: amount(get('max')),
    address: isAddress(address, { strict: false }) ? getAddress(address) : '',
    token: get('token').slice(0, MAX_TICKER),
  }
}

/** The view as the page's address: only what is set. */
export function writeView(view: HistoryView): URLSearchParams {
  const params = new URLSearchParams()
  if (view.group !== 'all') params.set('type', view.group)
  for (const name of ['from', 'to', 'min', 'max', 'address', 'token'] as const) {
    if (view[name] !== '') params.set(name, view[name])
  }
  return params
}

/** How many filters the panel has set; the tab isn't one. */
export function countFilters(view: HistoryView): number {
  return (['from', 'to', 'min', 'max', 'address', 'token'] as const).filter(name => view[name] !== '').length
}

/** The view as GET /api/transactions takes it. */
export function serverFilters(view: HistoryView): TransactionFilters {
  const filters: TransactionFilters = {}
  if (view.group !== 'all') filters.direction = view.group
  if (view.from) filters.since = startOfDay(view.from)
  if (view.to) filters.until = startOfDay(view.to, 1)
  if (view.min) filters.min_amount = view.min
  if (view.max) filters.max_amount = view.max
  if (view.address) filters.address = view.address
  if (view.token) filters.token = view.token
  return filters
}

/** Unix seconds at the start of a calendar day in the reader's time zone, or of a day after it. */
export function startOfDay(day: string, plusDays = 0): number {
  const [year, month, date] = day.split('-').map(Number)
  return Math.floor(new Date(year, month - 1, date + plusDays).getTime() / 1_000)
}

/** Today as a calendar day, in the reader's time zone: the latest a date filter can pick. */
export function today(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/** The panel's fields for a view, the address shown as its contact's name when it has one. */
export function draftOf(view: HistoryView, contacts: Contact[]): FilterDraft {
  const contact = contacts.find(c => c.address.toLowerCase() === view.address.toLowerCase())
  return {
    from: view.from,
    to: view.to,
    min: view.min,
    max: view.max,
    who: view.address ? (contact?.name ?? view.address) : '',
    token: view.token,
  }
}

/**
 * The address the panel's "address or contact" field names: a contact's, or the 0x address typed.
 * '' for an empty field, null for one that names neither.
 */
export function resolveWho(who: string, contacts: Contact[]): string | null {
  const text = who.trim()
  if (text === '') return ''
  const contact = contacts.find(c => c.name === normalizeName(text))
  if (contact) return getAddress(contact.address)
  return isAddress(text, { strict: false }) ? getAddress(text) : null
}

export interface DraftProblems {
  dates?: string
  amounts?: string
  who?: string
}

/** What is wrong with the panel's fields, in plain words. Empty when they can be applied. */
export function draftProblems(draft: FilterDraft, contacts: Contact[]): DraftProblems {
  const problems: DraftProblems = {}
  if (draft.from && draft.to && draft.to < draft.from) problems.dates = 'The end date is before the start date.'
  const min = draft.min.trim()
  const max = draft.max.trim()
  if ((min && !isValidAmount(min)) || (max && !isValidAmount(max))) {
    problems.amounts = 'Enter an amount like 25 or 0.5.'
  } else if (min && max && Number(min) > Number(max)) {
    problems.amounts = 'The lowest amount is more than the highest.'
  }
  if (resolveWho(draft.who, contacts) === null) {
    problems.who = "Enter a full address starting with 0x, or one of your contacts' names."
  }
  return problems
}

/** The view the panel's fields make, keeping the tab. Call once draftProblems finds nothing. */
export function viewOf(group: HistoryGroup, draft: FilterDraft, contacts: Contact[]): HistoryView {
  return {
    group,
    from: draft.from,
    to: draft.to,
    min: draft.min.trim(),
    max: draft.max.trim(),
    address: resolveWho(draft.who, contacts) ?? '',
    token: draft.token,
  }
}
