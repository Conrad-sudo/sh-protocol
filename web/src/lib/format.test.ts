import { describe, expect, it } from 'vitest'
import { formatTimeLeft, formatTokenAmount, formatUsd, formatWindow, isValidAmount, shortAddress } from './format'

describe('shortAddress', () => {
  it('keeps the start and end of an address', () => {
    expect(shortAddress('0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266')).toBe('0xf39F…2266')
  })

  it('leaves short strings alone', () => {
    expect(shortAddress('0x1234')).toBe('0x1234')
  })
})

describe('formatWindow', () => {
  it('names the periods the user can pick the way the picker does', () => {
    expect(formatWindow(43_200)).toBe('12 hours')
    expect(formatWindow(86_400)).toBe('24 hours')
    expect(formatWindow(604_800)).toBe('7 days')
  })

  it('handles single units and odd lengths', () => {
    expect(formatWindow(3_600)).toBe('1 hour')
    expect(formatWindow(172_800)).toBe('2 days')
    expect(formatWindow(5_400)).toBe('90 minutes')
  })
})

describe('isValidAmount', () => {
  it('accepts plain decimals up to 18 places', () => {
    for (const ok of ['0', '1', '0.05', '12.5', `1.${'0'.repeat(18)}`]) expect(isValidAmount(ok)).toBe(true)
  })

  it('rejects anything else', () => {
    for (const bad of ['', '.5', '1.', '1e3', '-1', '1,5', `1.${'0'.repeat(19)}`]) expect(isValidAmount(bad)).toBe(false)
  })
})

describe('formatUsd', () => {
  it('shows dollars and cents', () => {
    expect(formatUsd(250)).toBe('$250.00')
    expect(formatUsd(50_000)).toBe('$50,000.00')
  })
})

describe('formatTokenAmount', () => {
  it('shows whole and fractional amounts from the raw integer', () => {
    expect(formatTokenAmount('1000000000000000000', 18)).toBe('1')
    expect(formatTokenAmount('1234567890000', 6)).toBe('1,234,567.89')
    expect(formatTokenAmount('0', 18)).toBe('0')
  })

  it('cuts extra decimals instead of rounding up', () => {
    expect(formatTokenAmount('1999999999999999999', 18)).toBe('1.999999')
    expect(formatTokenAmount('123456789', 8, 2)).toBe('1.23')
  })

  it('keeps precision a float would lose', () => {
    expect(formatTokenAmount('123456789012345678901234567', 18)).toBe('123,456,789.012345')
  })

  it('never shows dust as zero', () => {
    expect(formatTokenAmount('1', 18)).toBe('<0.000001')
    expect(formatTokenAmount('5', 6, 2)).toBe('<0.01')
  })
})

describe('formatTimeLeft', () => {
  const min = 60_000
  it('uses the two largest units', () => {
    expect(formatTimeLeft(2 * 1_440 * min + 3 * 60 * min + 5 * min)).toBe('2 d 3 h')
    expect(formatTimeLeft(5 * 60 * min + 12 * min + 30_000)).toBe('5 h 12 min')
    expect(formatTimeLeft(40 * min)).toBe('40 min')
  })

  it('drops a zero second unit', () => {
    expect(formatTimeLeft(1_440 * min)).toBe('1 d')
    expect(formatTimeLeft(3 * 60 * min)).toBe('3 h')
  })

  it('says so when under a minute is left', () => {
    expect(formatTimeLeft(59_999)).toBe('less than a minute')
    expect(formatTimeLeft(-5)).toBe('less than a minute')
  })
})
