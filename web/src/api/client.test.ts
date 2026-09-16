import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { json, TOKEN } from '../test/utils'
import {
  ApiError,
  apiFetch,
  getAuthSnapshot,
  refreshSession,
  resetClientForTests,
  restoreSession,
  setSession,
} from './client'

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>

function mockFetch(handler: Handler) {
  const fn = vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => Promise.resolve(handler(String(input), init)))
  vi.stubGlobal('fetch', fn)
  return fn
}

const refreshCalls = (fn: ReturnType<typeof mockFetch>) =>
  fn.mock.calls.filter(([url]) => String(url).endsWith('/api/auth/refresh')).length

const authHeader = (init: RequestInit) => (init.headers as Record<string, string>).Authorization

/** Awaits a request that must fail, and returns its ApiError. */
async function failure(request: Promise<unknown>): Promise<ApiError> {
  try {
    await request
  } catch (error) {
    if (error instanceof ApiError) return error
    throw error
  }
  throw new Error('expected the request to fail')
}

describe('api client', () => {
  beforeEach(() => resetClientForTests())
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('refreshes ONCE for concurrent 401s and retries both with the new token', async () => {
    setSession({ ...TOKEN, access_token: 'expired' })
    let refreshes = 0
    const fetch = mockFetch(async (url, init) => {
      if (url.endsWith('/api/auth/refresh')) {
        refreshes += 1
        await new Promise(resolve => setTimeout(resolve, 10))
        return json(200, { ...TOKEN, access_token: `fresh-${refreshes}` })
      }
      return authHeader(init) === 'Bearer fresh-1' ? json(200, { ok: url }) : json(401, { detail: 'Token has expired' })
    })

    const [a, b] = await Promise.all([apiFetch('/api/me'), apiFetch('/api/contacts')])

    expect(a).toEqual({ ok: '/api/me' })
    expect(b).toEqual({ ok: '/api/contacts' })
    expect(refreshCalls(fetch)).toBe(1)
  })

  it('retries without refreshing when the token was renewed while the request was in flight', async () => {
    setSession({ ...TOKEN, access_token: 'old' })
    const fetch = mockFetch((url, init) => {
      if (url.endsWith('/api/auth/refresh')) return json(200, { ...TOKEN, access_token: 'unexpected' })
      if (authHeader(init) === 'Bearer old') {
        setSession({ ...TOKEN, access_token: 'new' }) // another request refreshed meanwhile
        return json(401, {})
      }
      return json(200, { sentWith: authHeader(init) })
    })

    await expect(apiFetch('/api/me')).resolves.toEqual({ sentWith: 'Bearer new' })
    expect(refreshCalls(fetch)).toBe(0)
  })

  it('signs out and throws 401 when the refresh is refused', async () => {
    setSession(TOKEN)
    mockFetch(url => (url.endsWith('/api/auth/refresh') ? json(401, { detail: 'Invalid refresh token' }) : json(401, {})))

    await expect(apiFetch('/api/me')).rejects.toMatchObject({ status: 401 })
    expect(getAuthSnapshot()).toEqual({ status: 'signedOut', userId: null })
  })

  it('shares one refresh between two page-load restores (StrictMode double effect)', async () => {
    const fetch = mockFetch(() => json(200, TOKEN))
    await Promise.all([restoreSession(), restoreSession()])
    expect(refreshCalls(fetch)).toBe(1)
    expect(getAuthSnapshot()).toEqual({ status: 'signedIn', userId: 7 })
  })

  it('treats an unreachable server as signed out on restore', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))))
    await restoreSession()
    expect(getAuthSnapshot().status).toBe('signedOut')
  })

  it('refreshes under a Web Lock when the browser has one', async () => {
    const request = vi.fn((_name: string, task: () => Promise<unknown>) => task())
    vi.stubGlobal('navigator', { ...navigator, locks: { request } })
    mockFetch(() => json(200, TOKEN))

    await refreshSession()
    expect(request).toHaveBeenCalledWith('mitfah-refresh', expect.any(Function))
  })

  it('never sends a token or refreshes for auth:false requests', async () => {
    setSession(TOKEN)
    const fetch = mockFetch(() => json(401, { detail: 'Incorrect email or password' }))

    const error = await failure(apiFetch('/api/auth/login', { method: 'POST', body: {}, auth: false }))
    expect(error).toBeInstanceOf(ApiError)
    expect(error.message).toBe('Incorrect email or password')
    expect(refreshCalls(fetch)).toBe(0)
    expect(authHeader(fetch.mock.calls[0][1] as RequestInit)).toBeUndefined()
  })

  it('turns a 422 into per-field messages', async () => {
    mockFetch(() =>
      json(422, {
        detail: [
          { loc: ['body', 'email'], msg: 'value is not a valid email address' },
          { loc: ['body', 'password'], msg: 'String should have at least 8 characters' },
        ],
      }),
    )
    const error = await failure(apiFetch('/api/auth/signup', { method: 'POST', auth: false }))
    expect(error.status).toBe(422)
    expect(error.fieldErrors).toEqual({
      email: 'value is not a valid email address',
      password: 'String should have at least 8 characters',
    })
    expect(error.message).toBe('value is not a valid email address')
  })

  it('gives rate limiting a readable message (slowapi sends no `detail`)', async () => {
    mockFetch(() => json(429, { error: 'Rate limit exceeded: 10 per 1 minute' }))
    const error = await failure(apiFetch('/api/auth/login', { method: 'POST', auth: false }))
    expect(error.status).toBe(429)
    expect(error.message).toMatch(/too many attempts/i)
  })

  it('reports an unreachable server as status 0', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))))
    const error = await failure(apiFetch('/api/chains', { auth: false }))
    expect(error).toMatchObject({ status: 0 })
  })
})
