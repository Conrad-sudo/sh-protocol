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

/** Revokes the refresh cookie on the server and clears it in the browser. */
export function logout() {
  return apiFetch<{ status: string }>('/api/auth/logout', { method: 'POST', auth: false })
}

export function fetchMe() {
  return apiFetch<Me>('/api/me')
}
