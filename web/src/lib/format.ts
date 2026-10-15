import { formatUnits } from 'viem'

/** `0x1234…abcd` — enough to recognise an address at a glance, never enough to act on. */
export function shortAddress(address: string, lead = 6, tail = 4): string {
  if (address.length <= lead + tail + 1) return address
  return `${address.slice(0, lead)}…${address.slice(-tail)}`
}

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })

/** `$1,234.50`. */
export function formatUsd(amount: number): string {
  return usd.format(amount)
}

/**
 * A spending window in words: "24 hours", "7 days", "90 minutes". A single day stays "24 hours",
 * which is how the limit is chosen and reads better after "every".
 */
export function formatWindow(seconds: number): string {
  const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`
  if (seconds % 86_400 === 0 && seconds > 86_400) return plural(seconds / 86_400, 'day')
  if (seconds % 3_600 === 0) return plural(seconds / 3_600, 'hour')
  return plural(Math.round(seconds / 60), 'minute')
}

/** A decimal amount as typed: digits, optionally a dot and up to 18 decimals. */
export function isValidAmount(value: string): boolean {
  return /^\d+(\.\d{1,18})?$/.test(value)
}

/**
 * A token amount for display, from the raw integer string the API sends (never its float). Keeps up
 * to `maxFraction` decimals without rounding up, groups thousands, and shows dust as "<0.000001"
 * rather than a misleading 0.
 */
export function formatTokenAmount(raw: string, decimals: number, maxFraction = 6): string {
  const value = BigInt(raw)
  const [whole, fraction = ''] = formatUnits(value, decimals).split('.')
  const shown = fraction.slice(0, maxFraction).replace(/0+$/, '')
  if (value > 0n && whole === '0' && shown === '') return `<0.${'0'.repeat(maxFraction - 1)}1`
  const grouped = BigInt(whole).toLocaleString('en-US')
  return shown ? `${grouped}.${shown}` : grouped
}

const price = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 5 })
const largePrice = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })

/**
 * A pool price for display, from the server's float: "3,150.4", "0.00041231". Five significant
 * figures keep a range's two ends apart even on a stablecoin pair (0.995 – 1.005). From 100,000 up,
 * whole numbers.
 */
export function formatPrice(value: number): string {
  return value >= 100_000 ? largePrice.format(value) : price.format(value)
}

const date = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' })

/** A calendar day, e.g. "Oct 24, 2026", from milliseconds. */
export function formatDate(ms: number): string {
  return date.format(ms)
}

const dateTime = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})

/** A moment in the reader's own time zone, e.g. "Oct 24, 2026, 2:05 PM", from milliseconds. */
export function formatDateTime(ms: number): string {
  return dateTime.format(ms)
}

/** Time until something happens, compactly: "2 d 3 h", "5 h 12 min", "40 min". */
export function formatTimeLeft(ms: number): string {
  if (ms < 60_000) return 'less than a minute'
  const totalMinutes = Math.floor(ms / 60_000)
  const days = Math.floor(totalMinutes / 1_440)
  const hours = Math.floor((totalMinutes % 1_440) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return hours > 0 ? `${days} d ${hours} h` : `${days} d`
  if (hours > 0) return minutes > 0 ? `${hours} h ${minutes} min` : `${hours} h`
  return `${minutes} min`
}
