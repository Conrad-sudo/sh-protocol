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
