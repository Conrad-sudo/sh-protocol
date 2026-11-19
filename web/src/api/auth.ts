import { apiFetch } from './client'
import type { Me, PasskeyAssertionJSON, TokenResponse } from './types'

/** A fresh nonce for the Sign-In With Ethereum message the user is about to sign. */
export function siweNonce() {
  return apiFetch<{ nonce: string }>('/api/auth/siwe/nonce', { auth: false })
}

/** Whether an address already has an account: a returning user signs in, a new one signs up. */
export function siweAccount(address: string) {
  return apiFetch<{ registered: boolean }>(`/api/auth/siwe/account?address=${encodeURIComponent(address)}`, {
    auth: false,
  })
}

/**
 * Signs in with a signed SIWE message. The first sign-in for an address creates its account; the
 * refresh token comes back as an httpOnly cookie.
 */
export function siweLogin(body: { message: string; signature: string; nonce: string }) {
  return apiFetch<TokenResponse>('/api/auth/siwe/login', { method: 'POST', body, auth: false })
}

/**
 * The options for signing in with a passkey: a fresh challenge, and no account named — the device
 * offers the Mitfah passkeys it holds. Fetch it before the tap (Safari opens the prompt only inside it).
 */
export function passkeySignInOptions() {
  return apiFetch<{ options: PublicKeyCredentialRequestOptionsJSON }>('/api/auth/passkey/options', {
    method: 'POST',
    auth: false,
  })
}

/**
 * Signs in with a passkey's answer. Only a passkey that one of the account's wallets approves can sign
 * in; the refresh token comes back as an httpOnly cookie, as for SIWE.
 */
export function passkeyLogin(credential: PasskeyAssertionJSON) {
  return apiFetch<TokenResponse>('/api/auth/passkey/login', { method: 'POST', body: { credential }, auth: false })
}

/** Revokes the refresh cookie on the server and clears it in the browser. */
export function logout() {
  return apiFetch<{ status: string }>('/api/auth/logout', { method: 'POST', auth: false })
}

export function fetchMe() {
  return apiFetch<Me>('/api/me')
}
