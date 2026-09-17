import { BaseError, UserRejectedRequestError, type Address, type Hex } from 'viem'
import { ApiError } from '../api/client'
import type { PreparedTx } from '../api/types'

/**
 * Turns an API-built transaction into what the wallet should send. `to`, `data` and `value` are
 * what the user is agreeing to; `gas` is the API's own estimate against current chain state, which
 * saves a round trip. Fees and nonce are left to the wallet, which knows the user's pending
 * transactions better than the server does.
 */
export function toTxRequest(tx: PreparedTx) {
  return {
    to: tx.to as Address,
    data: tx.data as Hex,
    value: tx.value ? BigInt(tx.value) : 0n,
    gas: tx.gas ? BigInt(tx.gas) : undefined,
  }
}

/** True when the user declined in their wallet, however deep the library wrapped that. */
export function isUserRejection(error: unknown): boolean {
  let current = error
  for (let depth = 0; current && typeof current === 'object' && depth < 10; depth++) {
    if (current instanceof UserRejectedRequestError) return true
    const code = (current as { code?: unknown }).code
    if (code === 4001 || code === 'ACTION_REJECTED') return true
    current = (current as { cause?: unknown }).cause
  }
  return false
}

/** A one-line message for any error the wallet flows can raise. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message
  // viem leaves shortMessage undefined when it wraps a plain Error; its message still leads with a
  // readable line.
  if (error instanceof BaseError && error.shortMessage) return error.shortMessage
  if (error instanceof Error && error.message) return error.message.split('\n')[0]
  return 'Something went wrong. Please try again.'
}

export function sleep(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}
