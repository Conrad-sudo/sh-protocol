import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { json, ME, renderRoutes, TOKEN } from '../test/utils'
import { routes } from '../routes'

const REQUEST = {
  id: 'req-1',
  app: { key: 'claude', name: 'Claude', site: 'claude.ai' },
  expires_at: Math.floor(Date.now() / 1000) + 600,
}
const APPROVED = 'https://claude.ai/api/mcp/auth_callback?code=mfc_x&state=s&iss=http%3A%2F%2Flocalhost%3A3000'
const DENIED = 'https://claude.ai/api/mcp/auth_callback?error=access_denied&state=s&iss=http%3A%2F%2Flocalhost%3A3000'

/**
 * The API as the Allow page meets it: signed in (or not) on load, the request `req-1`, and its two
 * answers. Returns which answers were posted.
 */
function stubApi({ signedIn = true, request = () => json(200, REQUEST) } = {}) {
  const answers: string[] = []
  const fetch = vi.fn((url: string) => {
    if (url.endsWith('/api/auth/refresh')) {
      return Promise.resolve(signedIn ? json(200, TOKEN) : json(401, { detail: 'No refresh token' }))
    }
    if (url.endsWith('/api/me')) return Promise.resolve(json(200, ME))
    if (url.endsWith('/api/oauth/requests/req-1')) return Promise.resolve(request())
    if (url.endsWith('/api/oauth/requests/req-1/approve')) {
      answers.push('approve')
      return Promise.resolve(json(200, { redirect_to: APPROVED }))
    }
    if (url.endsWith('/api/oauth/requests/req-1/deny')) {
      answers.push('deny')
      return Promise.resolve(json(200, { redirect_to: DENIED }))
    }
    return Promise.resolve(json(404, { detail: 'Not Found' }))
  })
  vi.stubGlobal('fetch', fetch)
  return { fetch, answers }
}

/** Replaces window.location.assign (jsdom can't leave the page) and returns the spy. */
function stubNavigation() {
  const assign = vi.fn()
  const original = window.location
  Object.defineProperty(window, 'location', { configurable: true, value: { ...original, assign } })
  return { assign, restore: () => Object.defineProperty(window, 'location', { configurable: true, value: original }) }
}

describe('ConnectPage (the Allow page for AI apps)', () => {
  let navigation: ReturnType<typeof stubNavigation>

  beforeEach(() => {
    resetClientForTests()
    navigation = stubNavigation()
  })
  afterEach(() => {
    cleanup()
    navigation.restore()
    vi.unstubAllGlobals()
  })

  it('names the app from the server, and Allow sends the browser back to it with the code', async () => {
    const { answers } = stubApi()
    await renderRoutes(routes, '/connect?request=req-1')

    expect(await screen.findByRole('heading', { name: 'Connect Claude?' })).toBeInTheDocument()
    expect(screen.getByText(/can't send anything by itself/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Allow' }))

    await waitFor(() => expect(navigation.assign).toHaveBeenCalledWith(APPROVED))
    expect(answers).toEqual(['approve'])
  })

  it('Deny sends the refusal back to the app', async () => {
    const { answers } = stubApi()
    await renderRoutes(routes, '/connect?request=req-1')

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Deny' }))

    await waitFor(() => expect(navigation.assign).toHaveBeenCalledWith(DENIED))
    expect(answers).toEqual(['deny'])
  })

  it('signs the user in first, and comes back to the same request', async () => {
    stubApi({ signedIn: false })
    const router = await renderRoutes(routes, '/connect?request=req-1')

    await waitFor(() => expect(router.state.location.pathname).toBe('/login'))
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/connect?request=req-1')}`)
  })

  it("says why when the request can't be answered here, and offers no buttons", async () => {
    stubApi({
      request: () => json(403, { detail: 'This request was opened in another browser. Start again from the app.' }),
    })
    await renderRoutes(routes, '/connect?request=req-1')

    expect(await screen.findByText(/opened in another browser/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull()
  })

  it('shows an app the server turned away, without asking anyone to sign in', async () => {
    const { fetch } = stubApi({ signedIn: false })
    const router = await renderRoutes(routes, '/connect?error=unknown_app')

    expect(await screen.findByRole('heading', { name: "This app can't connect to Mitfah" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/connect')
    expect(fetch.mock.calls.some(([url]) => String(url).includes('/api/oauth/'))).toBe(false)
  })
})
