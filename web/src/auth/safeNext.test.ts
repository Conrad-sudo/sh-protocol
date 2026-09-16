import { describe, expect, it } from 'vitest'
import { safeNext } from './safeNext'

describe('safeNext', () => {
  it.each(['/dashboard', '/contacts?tab=all', '/settings#telegram'])('keeps the site path %s', path => {
    expect(safeNext(path)).toBe(path)
  })

  it.each([
    null,
    '',
    'dashboard',
    '//evil.example',
    '/\\evil.example',
    'https://evil.example',
    'javascript:alert(1)',
    '/\t/evil.example',
  ])('refuses %j', value => {
    expect(safeNext(value)).toBe('/dashboard')
  })
})
