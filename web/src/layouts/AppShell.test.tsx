import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { routes } from '../routes'
import { json, ME, renderRoutes, setViewportWidth, TOKEN } from '../test/utils'

function stubSignedIn() {
  const fetch = vi.fn((url: string) => {
    if (url.endsWith('/api/auth/refresh')) return Promise.resolve(json(200, TOKEN))
    if (url.endsWith('/api/auth/logout')) return Promise.resolve(json(200, { status: 'signed out' }))
    if (url.endsWith('/api/me')) return Promise.resolve(json(200, ME))
    return Promise.resolve(json(404, {}))
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

describe('AppShell', () => {
  beforeEach(() => resetClientForTests())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the full sidebar on desktop, with the current page marked', async () => {
    setViewportWidth(1280)
    stubSignedIn()
    renderRoutes(routes, '/contacts')

    const nav = await screen.findByRole('navigation', { name: 'Main' })
    expect(nav.closest('.mf-sidebar')).toHaveAttribute('data-expanded', 'true')
    expect(within(nav).getByRole('link', { name: /Contacts/ })).toHaveAttribute('aria-current', 'page')
    expect(within(nav).getByRole('link', { name: /Dashboard/ })).not.toHaveAttribute('aria-current')
    expect(await screen.findByText('sam@example.com')).toBeInTheDocument()
    expect(document.querySelector('.mf-tabbar')).toBeNull()
  })

  it('collapses to an icon rail on tablets', async () => {
    setViewportWidth(820)
    stubSignedIn()
    renderRoutes(routes, '/dashboard')

    const nav = await screen.findByRole('navigation', { name: 'Main' })
    expect(nav.closest('.mf-sidebar')).toHaveAttribute('data-expanded', 'false')
  })

  it('uses bottom tabs on phones', async () => {
    setViewportWidth(390)
    stubSignedIn()
    renderRoutes(routes, '/settings')

    const tabs = await screen.findByRole('navigation', { name: 'Main' })
    expect(tabs).toHaveClass('mf-tabbar')
    expect(within(tabs).getAllByRole('link')).toHaveLength(5)
    expect(within(tabs).getByRole('link', { name: 'Settings' })).toHaveAttribute('aria-current', 'page')
    expect(document.querySelector('.mf-sidebar')).toBeNull()
  })

  it('signs out to /login without a `next`', async () => {
    setViewportWidth(1280)
    const fetch = stubSignedIn()
    const router = renderRoutes(routes, '/settings')

    // Two on this page: the sidebar's and the Settings panel's. Either will do.
    const [signOut] = await screen.findAllByRole('button', { name: 'Sign out' })
    await userEvent.setup().click(signOut)

    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(router.state.location.search).toBe('')
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/api/auth/logout'))).toBe(true)
  })
})
