/**
 * The one way the app talks to the API, plus the in-memory session it authenticates with.
 *
 * The access token lives only in this module's memory (15 minutes, never in storage). The refresh
 * token is an httpOnly cookie the browser sends to /api/auth/* on its own.
 *
 * Refreshing is the delicate part. The server rotates the refresh token on every use and treats a
 * token presented twice as stolen — it then signs the account out EVERYWHERE. So two refreshes must
 * never race: within a tab they share one in-flight promise, and across tabs they queue on a Web
 * Lock (a tab that waits sends the cookie the previous tab already rotated, which is fine).
 */
import type { TokenResponse } from './types'

const API_URL = import.meta.env.VITE_API_URL ?? ''
const REFRESH_LOCK = 'mitfah-refresh'

export class ApiError extends Error {
  /** HTTP status, or 0 when the server could not be reached. */
  readonly status: number
  /** Per-field messages from a 422, keyed by field name. */
  readonly fieldErrors: Record<string, string>

  constructor(status: number, message: string, fieldErrors: Record<string, string> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.fieldErrors = fieldErrors
  }
}

// ── Session store ─────────────────────────────────────────────────────────────

export type AuthStatus = 'loading' | 'signedIn' | 'signedOut'

export interface AuthSnapshot {
  status: AuthStatus
  userId: number | null
}

let accessToken: string | null = null
let snapshot: AuthSnapshot = { status: 'loading', userId: null }
const listeners = new Set<() => void>()

function publish(next: AuthSnapshot) {
  snapshot = next
  listeners.forEach(listener => listener())
}

export function getAuthSnapshot(): AuthSnapshot {
  return snapshot
}

export function subscribeAuth(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function setSession(token: TokenResponse) {
  accessToken = token.access_token
  publish({ status: 'signedIn', userId: token.user_id })
}

export function clearSession() {
  accessToken = null
  if (snapshot.status !== 'signedOut') publish({ status: 'signedOut', userId: null })
}

// ── Refresh ───────────────────────────────────────────────────────────────────

let refreshing: Promise<boolean> | null = null

function unreachable() {
  return new ApiError(0, "Can't reach Mitfah. Check your connection and try again.")
}

function withRefreshLock<T>(task: () => Promise<T>): Promise<T> {
  // Absent in older browsers and in jsdom; a single tab is still protected by `refreshing`.
  const locks = globalThis.navigator?.locks
  return locks ? locks.request(REFRESH_LOCK, task) : task()
}

async function requestRefresh(): Promise<boolean> {
  let response: Response
  try {
    response = await fetch(`${API_URL}/api/auth/refresh`, { method: 'POST', credentials: 'include' })
  } catch {
    throw unreachable()
  }
  if (!response.ok) {
    clearSession()
    return false
  }
  setSession((await response.json()) as TokenResponse)
  return true
}

/**
 * Trades the refresh cookie for a new access token. Concurrent callers share one request.
 *
 * @returns whether the session is now valid.
 * @throws ApiError(0) if the server could not be reached.
 */
export function refreshSession(): Promise<boolean> {
  refreshing ??= withRefreshLock(requestRefresh).finally(() => {
    refreshing = null
  })
  return refreshing
}

/** Restores a session on page load. Never throws: an unreachable server counts as signed out. */
export async function restoreSession() {
  try {
    await refreshSession()
  } catch {
    clearSession()
  }
}

// ── Requests ──────────────────────────────────────────────────────────────────

interface ValidationIssue {
  loc?: (string | number)[]
  msg?: string
}

function fallbackMessage(status: number) {
  if (status === 429) return 'Too many attempts. Wait a minute and try again.'
  if (status >= 500) return 'Something went wrong on our side. Please try again.'
  return 'That request could not be completed.'
}

async function toApiError(response: Response): Promise<ApiError> {
  let detail: unknown
  try {
    detail = ((await response.json()) as { detail?: unknown }).detail
  } catch {
    // Not JSON (a proxy error page, say): fall through to the generic message.
  }
  if (typeof detail === 'string') return new ApiError(response.status, detail)
  if (Array.isArray(detail)) {
    const fieldErrors: Record<string, string> = {}
    for (const issue of detail as ValidationIssue[]) {
      const field = issue.loc?.at(-1)
      if (typeof field === 'string' && issue.msg && !(field in fieldErrors)) {
        fieldErrors[field] = issue.msg
      }
    }
    const first = Object.values(fieldErrors)[0]
    return new ApiError(response.status, first ?? 'Some details are not valid.', fieldErrors)
  }
  return new ApiError(response.status, fallbackMessage(response.status))
}

export interface RequestOptions {
  method?: string
  body?: unknown
  /** Send the access token and recover from an expired one. Default true. */
  auth?: boolean
  signal?: AbortSignal
}

/**
 * Calls the API and returns its JSON.
 *
 * On a 401 it refreshes once and retries once. That is safe for writes too: the server rejects an
 * unauthenticated request in get_current_user, before the handler runs.
 *
 * @throws ApiError for any non-2xx answer, or status 0 if the server could not be reached.
 */
export async function apiFetch<T>(
  path: string,
  { method = 'GET', body, auth = true, signal }: RequestOptions = {},
): Promise<T> {
  const send = async (token: string | null) => {
    const headers: Record<string, string> = {}
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    if (token) headers.Authorization = `Bearer ${token}`
    try {
      return await fetch(`${API_URL}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'include',
        signal,
      })
    } catch (error) {
      if (signal?.aborted) throw error
      throw unreachable()
    }
  }

  const sentWith = auth ? accessToken : null
  let response = await send(sentWith)

  if (response.status === 401 && auth) {
    // Another request may already have refreshed while this one was in flight.
    const renewed = (accessToken !== null && accessToken !== sentWith) || (await refreshSession())
    if (!renewed) throw new ApiError(401, 'Your session has ended. Please sign in again.')
    response = await send(accessToken)
    // Still refused with a brand-new token: the account itself is gone.
    if (response.status === 401) clearSession()
  }

  if (!response.ok) throw await toApiError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** Test hook: forget all module state. */
export function resetClientForTests() {
  accessToken = null
  refreshing = null
  snapshot = { status: 'loading', userId: null }
  listeners.clear()
}
