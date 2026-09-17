import { describe, expect, it } from 'vitest'
import { BaseError, TransactionExecutionError, UserRejectedRequestError } from 'viem'
import { ApiError } from '../api/client'
import { errorText, isUserRejection, toTxRequest } from './tx'

describe('toTxRequest', () => {
  it('turns the API hex strings into what the wallet sends', () => {
    expect(
      toTxRequest({ to: '0xabc', data: '0x1234', value: '0xde0b6b3a7640000', gas: '0x5208', chainId: '0xaa36a7' }),
    ).toEqual({ to: '0xabc', data: '0x1234', value: 10n ** 18n, gas: 21_000n })
  })

  it('sends no value as zero and leaves a missing gas to the wallet', () => {
    expect(toTxRequest({ to: '0xabc', data: '0x' })).toEqual({ to: '0xabc', data: '0x', value: 0n, gas: undefined })
  })
})

describe('isUserRejection', () => {
  it('finds a rejection however deep it is wrapped', () => {
    const rejected = new UserRejectedRequestError(new Error('User denied'))
    const wrapped = new TransactionExecutionError(rejected, { account: undefined } as never)
    expect(isUserRejection(rejected)).toBe(true)
    expect(isUserRejection(wrapped)).toBe(true)
    expect(isUserRejection(new Error('outer', { cause: new Error('middle', { cause: rejected }) }))).toBe(true)
  })

  it('knows the plain EIP-1193 and ethers codes', () => {
    expect(isUserRejection({ code: 4001, message: 'User rejected the request.' })).toBe(true)
    expect(isUserRejection({ code: 'ACTION_REJECTED' })).toBe(true)
  })

  it('is false for everything else', () => {
    expect(isUserRejection(new Error('insufficient funds'))).toBe(false)
    expect(isUserRejection({ code: -32000 })).toBe(false)
    expect(isUserRejection(null)).toBe(false)
    expect(isUserRejection('4001')).toBe(false)
  })
})

describe('errorText', () => {
  it('uses the API message as is', () => {
    expect(errorText(new ApiError(400, 'Not enough ETH to cover the prefund.'))).toBe(
      'Not enough ETH to cover the prefund.',
    )
  })

  it("uses viem's short message, not the multi-line details", () => {
    const error = new BaseError('Insufficient funds for gas.', { details: 'balance 0, need 21000' })
    expect(error.message).toContain('\n')
    expect(errorText(error)).toBe('Insufficient funds for gas.')
  })

  it('still says something when viem wrapped an error that had no short message', () => {
    // Typed as a viem error, but viem passes a wallet's raw error straight through at runtime.
    const raw = new Error('boom') as never
    const text = errorText(new TransactionExecutionError(raw, { account: undefined } as never))
    expect(text).toBeTruthy()
    expect(text).not.toContain('\n')
  })

  it('keeps only the first line of other errors, and has a fallback', () => {
    expect(errorText(new Error('first line\nstack-ish detail'))).toBe('first line')
    expect(errorText(undefined)).toBe('Something went wrong. Please try again.')
  })
})
