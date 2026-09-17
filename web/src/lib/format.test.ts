import { describe, expect, it } from 'vitest'
import { formatUsd, formatWindow, isValidAmount, shortAddress } from './format'

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
