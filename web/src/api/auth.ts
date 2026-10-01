import { apiFetch } from './client'
import type { Me, TokenResponse } from './types'

/** A fresh nonce for the Sign-In With Ethereum message the user is about to sign. */
export function siweNonce() {
  return apiFetch<{ nonce: string }>('/api/auth/siwe/nonce', { auth: false })
}

/**
 * Signs in with a signed SIWE message — the only way in. The first sign-in for an address creates
 * its account; the refresh token comes back as an httpOnly cookie.
 */
export function siweLogin(body: { message: string; signature: string; nonce: string }) {
  return apiFetch<TokenResponse>('/api/auth/siwe/login', { method: 'POST', body, auth: false })
}

/** Revokes the refresh cookie on the server and clears it in the browser. */
export function logout() {
  return apiFetch<{ status: string }>('/api/auth/logout', { method: 'POST', auth: false })
}

export function fetchMe() {
  return apiFetch<Me>('/api/me')
}
