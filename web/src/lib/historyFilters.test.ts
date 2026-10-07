import { describe, expect, it } from 'vitest'
import type { Contact } from '../api/types'
import {
  draftProblems,
  readView,
  resolveWho,
  serverFilters,
  startOfDay,
  viewOf,
  writeView,
} from './historyFilters'

const SAM: Contact = { name: 'sam', address: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8' }
const DRAFT = { from: '', to: '', min: '', max: '', who: '', token: '' }

describe('historyFilters', () => {
  it('reads the view from the address, leaving out anything malformed', () => {
    const view = readView(
      new URLSearchParams(`type=in&from=2026-10-01&to=yesterday&min=1e3&max=2.5&address=${SAM.address.toLowerCase()}&token=USDC`),
    )
    expect(view).toEqual({
      group: 'in',
      from: '2026-10-01',
      to: '',
      min: '',
      max: '2.5',
      address: SAM.address,
      token: 'USDC',
    })
    expect(readView(new URLSearchParams('type=sideways&address=0x12')).group).toBe('all')
    expect(readView(new URLSearchParams('address=0x12')).address).toBe('')
  })

  it('writes only what is set, and reads it back the same', () => {
    const view = { group: 'out' as const, from: '2026-10-01', to: '', min: '5', max: '', address: '', token: 'ETH' }
    expect(writeView(view).toString()).toBe('type=out&from=2026-10-01&min=5&token=ETH')
    expect(readView(writeView(view))).toEqual(view)
    expect(writeView({ ...view, group: 'all', from: '', min: '', token: '' }).toString()).toBe('')
  })

  it('asks the server for whole days in the reader’s time zone', () => {
    const filters = serverFilters({ group: 'all', from: '2026-10-01', to: '2026-10-01', min: '', max: '', address: '', token: '' })
    expect(filters).toEqual({ since: startOfDay('2026-10-01'), until: startOfDay('2026-10-02') })
    expect(new Date(filters.since! * 1_000).getHours()).toBe(0)
    expect(filters.until! - filters.since!).toBeGreaterThanOrEqual(23 * 3_600) // a DST day is 23 or 25 hours
  })

  it('turns a contact’s name, in any case, or a typed address into a checksummed address', () => {
    expect(resolveWho(' Sam ', [SAM])).toBe(SAM.address)
    expect(resolveWho(SAM.address.toLowerCase(), [])).toBe(SAM.address)
    expect(resolveWho('', [SAM])).toBe('')
    expect(resolveWho('samuel', [SAM])).toBeNull()
    expect(viewOf('in', { ...DRAFT, who: 'sam', min: ' 5 ' }, [SAM])).toMatchObject({ address: SAM.address, min: '5' })
  })

  it('names what is wrong with the fields', () => {
    expect(draftProblems({ ...DRAFT, min: 'abc' }, [])).toEqual({ amounts: 'Enter an amount like 25 or 0.5.' })
    expect(draftProblems({ ...DRAFT, min: '10', max: '9.99' }, []).amounts).toBe('The lowest amount is more than the highest.')
    expect(draftProblems({ ...DRAFT, from: '2026-10-02', to: '2026-10-01' }, []).dates).toBeDefined()
    expect(draftProblems({ ...DRAFT, from: '2026-10-01', to: '2026-10-01', min: '1', max: '1' }, [])).toEqual({})
  })
})
