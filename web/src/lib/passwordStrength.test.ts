import { describe, expect, it } from 'vitest'
import { passwordStrength } from './passwordStrength'

describe('passwordStrength', () => {
  it.each([
    ['short', 0],
    ['abcdefgh', 1],
    ['abcdefgh1', 2],
    ['Abcdefgh1', 3],
    ['abcdefghijklmnop', 2],
    ['Abcdefghijklmnop!', 3],
  ] as const)('%s → %i', (password, level) => {
    expect(passwordStrength(password)).toBe(level)
  })
})
