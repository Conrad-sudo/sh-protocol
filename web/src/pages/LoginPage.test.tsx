import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { json, renderRoutes, TOKEN } from '../test/utils'
import { routes } from '../routes'

/** Signed out on load; `/api/auth/login` answers with `loginResponse`. */
function stubApi(loginResponse: () => Response) {
  const fetch = vi.fn((url: string) => {
    if (url.endsWith('/api/auth/refresh')) return Promise.resolve(json(401, { detail: 'No refresh token' }))
    if (url.endsWith('/api/auth/login')) return Promise.resolve(loginResponse())
    if (url.endsWith('/api/me')) return Promise.resolve(json(200, { user_id: 7, email: 'sam@example.com', wallet_chains: [] }))
    return Promise.resolve(json(404, { detail: 'Not Found' }))
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

async function fillAndSubmit(email: string, password: string) {
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Email'), email)
  await user.type(screen.getByLabelText('Password', { selector: 'input' }), password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

describe('LoginPage', () => {
  beforeEach(() => {
    resetClientForTests()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('says so plainly when the password is wrong', async () => {
    stubApi(() => json(401, { detail: 'Incorrect email or password' }))
    await renderRoutes(routes, '/login')
    await fillAndSubmit('sam@example.com', 'wrong-password')
    expect(await screen.findByText('Incorrect email or password.')).toBeInTheDocument()
  })

  it('offers no Google button when no client ID is configured', async () => {
    stubApi(() => json(200, TOKEN))
    await renderRoutes(routes, '/login')
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
    expect(screen.queryByRole('separator', { name: 'or' })).toBeNull()
    expect(document.querySelector('.mf-google-button')).toBeNull()
  })

  it('explains rate limiting', async () => {
    stubApi(() => json(429, { error: 'Rate limit exceeded' }))
    await renderRoutes(routes, '/login')
    await fillAndSubmit('sam@example.com', 'hunter2hunter2')
    expect(await screen.findByText(/too many attempts/i)).toBeInTheDocument()
  })

  it('does not call the API for an invalid email', async () => {
    const fetch = stubApi(() => json(200, TOKEN))
    await renderRoutes(routes, '/login')
    await fillAndSubmit('not-an-email', 'hunter2hunter2')
    expect(await screen.findByText('Enter a valid email address')).toBeInTheDocument()
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/api/auth/login'))).toBe(false)
  })

  it('goes on to `next` after signing in', async () => {
    stubApi(() => json(200, TOKEN))
    const router = await renderRoutes(routes, `/login?next=${encodeURIComponent('/contacts')}`)
    await fillAndSubmit('sam@example.com', 'hunter2hunter2')
    expect(await screen.findByRole('heading', { name: 'Contacts' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/contacts')
  })

  it('ignores a `next` that points off the site', async () => {
    stubApi(() => json(200, TOKEN))
    const router = await renderRoutes(routes, `/login?next=${encodeURIComponent('//evil.example')}`)
    await fillAndSubmit('sam@example.com', 'hunter2hunter2')
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')
  })
})
