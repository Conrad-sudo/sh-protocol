import { getAddress, isAddress, zeroAddress } from 'viem'

/**
 * Checks for a new contact, in plain words. They mirror what app/api.py `create_contact` accepts,
 * plus a few mistakes the server lets through: a mistyped checksum, the zero address.
 */

export const MAX_NAME_LENGTH = 64

/** The name as the server stores it: trimmed and lowercased. */
export function normalizeName(name: string) {
  return name.trim().toLowerCase()
}

/** What is wrong with `name` as a contact name, or null. */
export function nameProblem(name: string): string | null {
  const normalized = normalizeName(name)
  if (normalized === '') return 'Enter a name.'
  // The server counts characters, not UTF-16 units.
  if ([...normalized].length > MAX_NAME_LENGTH) return `Use ${MAX_NAME_LENGTH} characters or fewer.`
  if ([...normalized].some(ch => ch === '/' || ch === '\\' || ch.charCodeAt(0) < 0x20)) {
    return 'A name can\'t contain "/", "\\" or tabs.'
  }
  if (normalized === 'me') return '"me" always means your own wallet, so it can\'t name a contact.'
  // A browser can't send a request to delete these (see create_contact).
  if (normalized === '.' || normalized === '..') return "A name can't be just dots."
  return null
}

/** What is wrong with `address` (already trimmed) as a contact's address, or null. */
export function addressProblem(address: string): string | null {
  if (address === '') return 'Enter an address.'
  if (!isAddress(address, { strict: false })) {
    if (/^0x[0-9a-f]*$/i.test(address)) {
      return `An address is 0x and 40 more characters. This one has ${address.length - 2}.`
    }
    if (address.includes('.')) return "Paste the 0x address. Names such as sam.eth aren't supported."
    return 'Enter a full address starting with 0x.'
  }
  // Mixed case carries a checksum; all one case carries none (EIP-55).
  const body = address.slice(2)
  const mixedCase = body !== body.toLowerCase() && body !== body.toUpperCase()
  if (mixedCase && getAddress(address) !== address) {
    return "This address has a typo: its capital letters don't match. Copy it again from where you got it."
  }
  if (getAddress(address) === zeroAddress) return 'This is the zero address. Money sent there is lost for good.'
  return null
}
