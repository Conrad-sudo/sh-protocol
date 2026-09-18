import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, within } from '@testing-library/react'
import { resetClientForTests } from '../api/client'
import { json, ME, renderRoutes, TOKEN } from '../test/utils'
import { routes } from '../routes'

const CHAINS = [
  { chain_id: 11155111, name: 'sepolia-fork', native_ticker: 'ETH', fork: true },
  { chain_id: 42161, name: 'arbitrum', native_ticker: 'ETH', fork: false },
]

function stubApi({ signedIn = false, chains = () => json(200, { chains: CHAINS }) } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url.endsWith('/api/auth/refresh')) {
        return Promise.resolve(signedIn ? json(200, TOKEN) : json(401, { detail: 'No refresh token' }))
      }
      if (url.endsWith('/api/me')) return Promise.resolve(json(200, ME))
      if (url.endsWith('/api/chains')) return Promise.resolve(chains())
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
}

describe('HomePage', () => {
  beforeEach(() => {
    resetClientForTests()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('invites a visitor to sign up and lists the served networks', async () => {
    stubApi()
    await renderRoutes(routes, '/')

    const networks = await screen.findByRole('region', { name: 'Networks' })
    const items = within(networks).getAllByRole('listitem')
    expect(items.map(item => item.textContent)).toEqual(['SepoliaTest network', 'Arbitrum One'])
    expect(within(networks).getByRole('link', { name: 'Add one' })).toHaveAttribute('href', '/signup')

    const main = within(screen.getByRole('main'))
    expect(main.getAllByRole('link', { name: 'Get started' })).toHaveLength(2)
    expect(main.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login')
    for (const name of ['How it works', 'Built to keep your money safe', 'Also in Telegram', 'Questions']) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
    expect(screen.getByText('Does Mitfah hold my money?')).toBeInTheDocument()
  })

  it('sends a signed-in user to the app instead', async () => {
    stubApi({ signedIn: true })
    await renderRoutes(routes, '/')

    const main = within(screen.getByRole('main'))
    expect(await main.findAllByRole('link', { name: 'Open your dashboard' })).toHaveLength(2)
    expect(screen.queryByRole('link', { name: 'Get started' })).toBeNull()
    const networks = await screen.findByRole('region', { name: 'Networks' })
    expect(within(networks).getByRole('link', { name: 'Add one' })).toHaveAttribute('href', '/wallets/new')
  })

  it('leaves the networks out when the server cannot list them', async () => {
    stubApi({ chains: () => json(500, { detail: 'boom' }) })
    await renderRoutes(routes, '/')

    expect(await screen.findByRole('heading', { name: 'Questions' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Networks' })).toBeNull()
  })

  it('links to the legal pages from the footer', async () => {
    stubApi()
    await renderRoutes(routes, '/')

    const legal = await screen.findByRole('navigation', { name: 'Legal' })
    expect(within(legal).getByRole('link', { name: 'Terms' })).toHaveAttribute('href', '/terms')
    expect(within(legal).getByRole('link', { name: 'Privacy' })).toHaveAttribute('href', '/privacy')
  })
})

describe('legal pages', () => {
  beforeEach(() => resetClientForTests())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it.each([
    ['/terms', 'Terms of Service'],
    ['/privacy', 'Privacy Policy'],
  ])('%s is marked as a draft', async (path, title) => {
    stubApi()
    await renderRoutes(routes, path)

    expect(await screen.findByRole('heading', { level: 1, name: title })).toBeInTheDocument()
    expect(screen.getByText(/hasn't been reviewed by a lawyer/)).toBeInTheDocument()
  })

  it('are linked from the sign-in card', async () => {
    stubApi()
    await renderRoutes(routes, '/login')

    const legal = await screen.findByRole('navigation', { name: 'Legal' })
    expect(within(legal).getByRole('link', { name: 'Privacy' })).toHaveAttribute('href', '/privacy')
  })
})
