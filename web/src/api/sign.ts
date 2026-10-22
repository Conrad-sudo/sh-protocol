import { apiFetch } from './client'
import type { PasskeyAssertionJSON, SignFinish, SignStart } from './types'

/**
 * Which confirmation: a quote card's, by the signed-in account; or a Telegram link's, by its token
 * alone (the confirm page has no sign-in, so the token is what proves whose it is).
 */
export type SignRequest = { chain_id: number; quote_id: string } | { token: string }

const signedIn = (request: SignRequest | { token?: string }) => !('token' in request) || request.token === undefined

/**
 * Builds the final transaction and returns the challenge a passkey signs to send it. Called before
 * the tap, and again once the answer is a minute old (fresh fees); each call stops the last one's
 * `sign_id` working.
 *
 * @throws ApiError 404 for a confirmation that has expired or was used; 409 when no passkey the
 *         account has is trusted by this wallet.
 */
export function startSign(request: SignRequest, signal?: AbortSignal) {
  return apiFetch<SignStart>('/api/sign/start', { method: 'POST', body: request, auth: signedIn(request), signal })
}

/**
 * Sends the transaction with the passkey's signature. Answers once it is on the wire; the wait for
 * it to land runs on the server, and `transaction_id` is the History row to follow.
 *
 * @throws ApiError 400 when the passkey's signature isn't accepted; 409 when the confirmation has
 *         expired, was used, or was started again since; 422 when it would fail on chain (the reason
 *         is named).
 */
export function finishSign(body: { sign_id: string; credential: PasskeyAssertionJSON; token?: string }) {
  return apiFetch<SignFinish>('/api/sign/finish', { method: 'POST', body, auth: signedIn(body) })
}

/** Turns the quote down; nothing is sent. Answers with what the chat records. */
export function cancelSign(request: SignRequest) {
  return apiFetch<{ outcome: string }>('/api/sign/cancel', { method: 'POST', body: request, auth: signedIn(request) })
}
