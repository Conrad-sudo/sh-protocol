import { describe, expect, it } from 'vitest'
import { shortAddress } from './format'

describe('shortAddress', () => {
  it('keeps the start and end of an address', () => {
    expect(shortAddress('0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266')).toBe('0xf39F…2266')
  })

  it('leaves short strings alone', () => {
    expect(shortAddress('0x1234')).toBe('0x1234')
  })
})
