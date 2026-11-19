import { apiFetch } from './client'
import type { ConnectAnswer, ConnectedApp, ConnectRequest } from './types'

/**
 * AI apps connecting to the account over MCP: the Allow page's request, its two answers, and the
 * apps already connected. The browser's binding cookie (set when the app opened the request) goes
 * with every call here: only the browser that opened a request may answer it.
 */

const requestPath = (id: string) => `/api/oauth/requests/${encodeURIComponent(id)}`

/** The request an app opened: which app (from the server's known list) and until when. */
export function fetchConnectRequest(id: string, signal?: AbortSignal) {
  return apiFetch<ConnectRequest>(requestPath(id), { signal })
}

/** Allow: returns the app's address, carrying the code, to send the browser to. */
export function approveConnectRequest(id: string) {
  return apiFetch<ConnectAnswer>(`${requestPath(id)}/approve`, { method: 'POST' })
}

/** Deny: returns the app's address, carrying the refusal, to send the browser to. */
export function denyConnectRequest(id: string) {
  return apiFetch<ConnectAnswer>(`${requestPath(id)}/deny`, { method: 'POST' })
}

/** The AI apps connected to the account, newest first. */
export async function fetchConnectedApps(signal?: AbortSignal) {
  return (await apiFetch<{ connections: ConnectedApp[] }>('/api/connections', { signal })).connections
}

/** Disconnects an app: its access ends on its very next call. */
export function disconnectApp(id: number) {
  return apiFetch<void>(`/api/connections/${id}`, { method: 'DELETE' })
}
