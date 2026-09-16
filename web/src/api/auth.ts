import { apiFetch } from './client'
import type { Me, TokenResponse } from './types'

export interface Credentials {
  email: string
  password: string
}

export function signup(credentials: Credentials) {
  return apiFetch<TokenResponse>('/api/auth/signup', { method: 'POST', body: credentials, auth: false })
}

export function login(credentials: Credentials) {
  return apiFetch<TokenResponse>('/api/auth/login', { method: 'POST', body: credentials, auth: false })
}

/**
 * Signs in with a Google ID token, creating the account on first use. A 409 means a password
 * account already has this email — the user must sign in with the password and link Google.
 */
export function googleSignIn(idToken: string) {
  return apiFetch<TokenResponse>('/api/auth/google', {
    method: 'POST',
    body: { id_token: idToken },
    auth: false,
  })
}

/** Attaches a Google account to the signed-in account. */
export function linkGoogle(idToken: string) {
  return apiFetch<{ status: string }>('/api/auth/google/link', {
    method: 'POST',
    body: { id_token: idToken },
  })
}

/** Revokes the refresh cookie on the server and clears it in the browser. */
export function logout() {
  return apiFetch<{ status: string }>('/api/auth/logout', { method: 'POST', auth: false })
}

export function fetchMe() {
  return apiFetch<Me>('/api/me')
}
