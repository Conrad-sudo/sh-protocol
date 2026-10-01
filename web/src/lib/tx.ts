import { BaseError, UserRejectedRequestError, type Address, type Hex } from 'viem'
import { ApiError } from '../api/client'
import type { PreparedTx } from '../api/types'
import { explainError } from './errors'

// How the wallet flows wait for a transaction the user's wallet sent: creating a wallet, the owner
// actions, and funding. Every wait has an end the user can see.

/** Between two asks of the API whether a transaction has mined. */
export const POLL_GAP_MS = 2_000
/** How long after sending a page stops waiting for a transaction the network has seen. */
export const GIVE_UP_MS = 10 * 60_000
/** How long a sent transaction may stay unknown to the network before the page says it may never arrive. */
export const UNSEEN_GRACE_MS = 90_000
/** How long "Check again" waits for a transaction that has already outlived GIVE_UP_MS. */
export const RECHECK_MS = 2 * 60_000
/** A prepare or confirm taking longer has hung. A confirm holds for up to 20 s on the server. */
export const REQUEST_TIMEOUT_MS = 45_000

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
  if (error instanceof ApiError) return explainError(error.message)
  // A request given up by timeoutSignal.
  if (error instanceof DOMException && error.name === 'TimeoutError') {
    return "Mitfah's server didn't answer in time. Please try again."
  }
  // viem leaves shortMessage undefined when it wraps a plain Error; its message still leads with a
  // readable line.
  if (error instanceof BaseError && error.shortMessage) return error.shortMessage
  if (error instanceof Error && error.message) return error.message.split('\n')[0]
  return 'Something went wrong. Please try again.'
}

/** Gives up on a request that hasn't answered within `ms`, where the browser can. */
export function timeoutSignal(ms: number): AbortSignal | undefined {
  return typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(ms) : undefined
}

export function sleep(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}
