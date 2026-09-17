import { describe, expect, it } from 'vitest'
import { addressProblem, nameProblem, normalizeName } from './contacts'

const SAM = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'

describe('normalizeName', () => {
  it('trims and lowercases, as the server stores it', () => {
    expect(normalizeName('  Sam Smith ')).toBe('sam smith')
  })
})

describe('nameProblem', () => {
  it.each(['sam', 'Sam Smith', "o'neil & co?", 'zoë', 'z'.repeat(64), '...', 'a.b'])('accepts %j', name => {
    expect(nameProblem(name)).toBeNull()
  })

  it.each([
    ['', 'Enter a name.'],
    ['   ', 'Enter a name.'],
    ['z'.repeat(65), 'Use 64 characters or fewer.'],
    ['a/b', 'A name can\'t contain "/", "\\" or tabs.'],
    ['a\\b', 'A name can\'t contain "/", "\\" or tabs.'],
    ['a\tb', 'A name can\'t contain "/", "\\" or tabs.'],
    [' ME ', '"me" always means your own wallet, so it can\'t name a contact.'],
    ['.', "A name can't be just dots."],
    [' .. ', "A name can't be just dots."],
  ])('refuses %j', (name, problem) => {
    expect(nameProblem(name)).toBe(problem)
  })

  it('counts characters, not UTF-16 units', () => {
    expect(nameProblem('😀'.repeat(64))).toBeNull()
    expect(nameProblem('😀'.repeat(65))).toBe('Use 64 characters or fewer.')
  })
})

describe('addressProblem', () => {
  it.each([SAM, SAM.toLowerCase(), `0x${SAM.slice(2).toUpperCase()}`])('accepts %s', address => {
    expect(addressProblem(address)).toBeNull()
  })

  it.each([
    ['', 'Enter an address.'],
    ['0x1234', 'An address is 0x and 40 more characters. This one has 4.'],
    [SAM.slice(0, -1), 'An address is 0x and 40 more characters. This one has 39.'],
    [`${SAM}0`, 'An address is 0x and 40 more characters. This one has 41.'],
    ['sam.eth', "Paste the 0x address. Names such as sam.eth aren't supported."],
    ['hello', 'Enter a full address starting with 0x.'],
    [`0x${'g'.repeat(40)}`, 'Enter a full address starting with 0x.'],
    // One letter's case changed: the checksum no longer matches.
    [SAM.replace('C5', 'c5'), "This address has a typo: its capital letters don't match. Copy it again from where you got it."],
    [`0x${'0'.repeat(40)}`, 'This is the zero address. Money sent there is lost for good.'],
  ])('refuses %j', (address, problem) => {
    expect(addressProblem(address)).toBe(problem)
  })
})
