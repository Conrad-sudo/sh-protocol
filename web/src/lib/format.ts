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
