import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { routes } from '../routes'
import { json, ME, renderRoutes, setViewportWidth, TOKEN } from '../test/utils'

// Google sign-in "configured", with Google's button swapped for one that hands back a fixed token
// (or reports a closed popup). The real button needs Google's script and a person at the popup.
vi.mock('../auth/google', () => ({ GOOGLE_CLIENT_ID: 'test-client', googleEnabled: true }))
vi.mock('@react-oauth/google', () => ({
  GoogleOAuthProvider: ({ children }: { children: React.ReactNode }) => children,
  GoogleLogin: ({ onSuccess, onError, text }: {
    onSuccess: (response: { credential?: string }) => void
    onError?: () => void
    text: string
  }) => (
    <div>
      <button type="button" onClick={() => onSuccess({ credential: 'google-id-token' })}>
        Google {text}
      </button>
      <button type="button" onClick={() => onError?.()}>
        Close Google popup
      </button>
    </div>
  ),
}))

type Route = (init: RequestInit) => Response
interface ApiStub {
  signedIn: boolean
  routes: Record<string, Route>
}

/** Answers the auth routes like the API, plus any per-test `routes` keyed by path. */
function stubApi(stub: ApiStub) {
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    const path = new URL(url, 'http://localhost').pathname
    if (stub.routes[path]) return Promise.resolve(stub.routes[path](init))
    if (path === '/api/auth/refresh') {
      return Promise.resolve(stub.signedIn ? json(200, TOKEN) : json(401, { detail: 'No refresh token' }))
    }
    if (path === '/api/me') return Promise.resolve(json(200, ME))
    return Promise.resolve(json(404, { detail: 'Not Found' }))
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

const bodyOf = (fetch: ReturnType<typeof stubApi>, path: string) => {
  const call = fetch.mock.calls.find(([url]) => String(url).endsWith(path))
  return call ? JSON.parse(String(call[1]?.body)) : undefined
}

describe('Google sign-in', () => {
  beforeEach(() => {
    resetClientForTests()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('signs in with Google and lands on the dashboard', async () => {
    const fetch = stubApi({ signedIn: false, routes: { '/api/auth/google': () => json(200, TOKEN) } })
    const router = renderRoutes(routes, '/login')

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Google signin_with' }))

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')
    expect(bodyOf(fetch, '/api/auth/google')).toEqual({ id_token: 'google-id-token' })
  })

  it('points a password account at signing in with the password', async () => {
    stubApi({
      signedIn: false,
      routes: {
        '/api/auth/google': () =>
          json(409, {
            detail:
              'An account with that email already exists. Sign in with your password, then link Google from your account settings.',
          }),
      },
    })
    renderRoutes(routes, '/signup?next=%2Fcontacts')

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Google signup_with' }))

    const alert = (await screen.findByText(/link Google from your account settings/)).closest('.rs-message')!
    const link = within(alert as HTMLElement).getByRole('link', { name: 'Sign in with your password' })
    expect(link).toHaveAttribute('href', '/login?next=%2Fcontacts')
  })

  it('says so when the Google popup is closed', async () => {
    stubApi({ signedIn: false, routes: {} })
    renderRoutes(routes, '/login')

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Close Google popup' }))

    expect(await screen.findByText("Google sign-in didn't complete. Please try again.")).toBeInTheDocument()
  })
})

describe('Settings: sign-in methods and wallet owner', () => {
  beforeEach(() => {
    resetClientForTests()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('links Google, then shows it as linked', async () => {
    let linked = false
    const fetch = stubApi({
      signedIn: true,
      routes: {
        '/api/me': () => json(200, { ...ME, google_linked: linked }),
        '/api/auth/google/link': () => {
          linked = true
          return json(200, { status: 'linked' })
        },
      },
    })
    renderRoutes(routes, '/settings')

    expect(await screen.findByText('Set')).toBeInTheDocument()
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Google continue_with' }))

    expect(await screen.findByText('Linked')).toBeInTheDocument()
    expect(bodyOf(fetch, '/api/auth/google/link')).toEqual({ id_token: 'google-id-token' })
    expect(screen.queryByRole('button', { name: 'Google continue_with' })).toBeNull()
  })

  it('explains a Google account that is already linked elsewhere', async () => {
    stubApi({
      signedIn: true,
      routes: {
        '/api/auth/google/link': () => json(409, { detail: 'That Google account is already linked.' }),
      },
    })
    renderRoutes(routes, '/settings')

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Google continue_with' }))
    expect(await screen.findByText('That Google account is already linked.')).toBeInTheDocument()
  })

  it('shows a Google-only account as having no password', async () => {
    stubApi({
      signedIn: true,
      routes: { '/api/me': () => json(200, { ...ME, has_password: false, google_linked: true }) },
    })
    renderRoutes(routes, '/settings')

    expect(await screen.findByText('Not set')).toBeInTheDocument()
    expect(screen.getByText('Linked')).toBeInTheDocument()
  })

  it('shows the owner address when one is linked, and says why it matters', async () => {
    const owner = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
    stubApi({ signedIn: true, routes: { '/api/me': () => json(200, { ...ME, owner_addr: owner }) } })
    renderRoutes(routes, '/settings')

    expect(await screen.findByTitle(owner)).toHaveTextContent('0xf39F…2266')
    expect(screen.getByRole('button', { name: 'Copy address' })).toBeInTheDocument()
    expect(screen.getByText(/Mitfah cannot recover your wallet/)).toBeInTheDocument()
  })

  it('says the owner address is not linked yet', async () => {
    stubApi({ signedIn: true, routes: {} })
    renderRoutes(routes, '/settings')

    expect(await screen.findByText('Not linked yet')).toBeInTheDocument()
    expect(screen.getByText(/You'll link it when you create your wallet/)).toBeInTheDocument()
  })
})
