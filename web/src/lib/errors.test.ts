import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { explainError } from './errors'
import { errorText } from './tx'

describe('explainError', () => {
  it('puts a contract error from the simulation into plain words', () => {
    expect(explainError('This transaction would fail: SessionHandler_NotEnoughBalance()')).toBe(
      "The wallet doesn't hold that much.",
    )
    expect(explainError('This transaction would fail: SpendingLimitModule_TokenNotPriced(address)')).toBe(
      "Mitfah can't price this token, so it can't count toward your limit.",
    )
    expect(explainError('This transaction would fail: EnforcedPause()')).toBe('The wallet is already paused.')
  })

  it('finds the known error even after other brackets', () => {
    expect(explainError('Call (eth_call) failed: OwnableUnauthorizedAccount(address)')).toMatch(/^Only the wallet's owner/)
  })

  it('explains a transaction that reverted on the network', () => {
    expect(explainError(`That transaction reverted (tx: 0x${'ab'.repeat(32)})`)).toBe(
      'The transaction failed on the network, so nothing changed.',
    )
  })

  it('leaves anything else as the API said it', () => {
    expect(explainError('This transaction would fail: 0x4c0a7758')).toBe('This transaction would fail: 0x4c0a7758')
    expect(explainError('You have no wallet on chain 1.')).toBe('You have no wallet on chain 1.')
    expect(explainError('toString()')).toBe('toString()')
  })

  it('is what errorText shows for an API error', () => {
    expect(errorText(new ApiError(400, 'This transaction would fail: ExpectedPause()'))).toBe("The wallet isn't paused.")
  })
})
