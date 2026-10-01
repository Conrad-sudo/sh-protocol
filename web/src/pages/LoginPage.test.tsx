import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { answerRpc, isRpc, json, makeWagmiConfig, ME, renderRoutes, TOKEN, WALLET } from '../test/utils'
import { routes } from '../routes'

const NONCE = 'f00dfeed1234abcd'
const SIGNATURE = `0x${'ab'.repeat(65)}`

/**
 * Signed out on load. The nonce endpoint answers NONCE, the mock wallet signs anything, and
 * `/api/auth/siwe/login` answers with `loginResponse`. Returns what was posted to it.
 */
function stubApi(loginResponse: () => Response) {
  const logins: { message: string; signature: string; nonce: string }[] = []
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (isRpc(url)) return Promise.resolve(answerRpc(init, { eth_sign: () => SIGNATURE }))
    if (url.endsWith('/api/auth/refresh')) return Promise.resolve(json(401, { detail: 'No refresh token' }))
    if (url.endsWith('/api/auth/siwe/nonce')) return Promise.resolve(json(200, { nonce: NONCE }))
    if (url.endsWith('/api/auth/siwe/login')) {
      logins.push(JSON.parse(String(init?.body)))
      return Promise.resolve(loginResponse())
    }
    if (url.endsWith('/api/me')) return Promise.resolve(json(200, ME))
    if (url.endsWith('/api/chains')) return Promise.resolve(json(200, { chains: [] }))
    if (url.endsWith('/api/contacts')) return Promise.resolve(json(200, { contacts: [] }))
    return Promise.resolve(json(404, { detail: 'Not Found' }))
  })
  vi.stubGlobal('fetch', fetch)
  return { fetch, logins }
}

/** Connects the mock wallet, then signs in with it. */
async function connectAndSignIn() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Connect wallet' }))
  await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
  await user.click(await screen.findByRole('button', { name: 'Sign in' }))
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

  it('signs in with the wallet: connect, sign a message for this site, and on to the dashboard', async () => {
    const { logins } = stubApi(() => json(200, TOKEN))
    const router = await renderRoutes(routes, '/login')

    expect(await screen.findByRole('heading', { name: 'Sign in with your wallet' })).toBeInTheDocument()
    expect(screen.getByText(/New here\? Signing in creates your account\./)).toBeInTheDocument()
    // Nothing about email, passwords or Google is left.
    expect(screen.queryByLabelText('Email')).toBeNull()
    expect(document.querySelector('.mf-google-button')).toBeNull()

    await connectAndSignIn()
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')

    expect(logins).toHaveLength(1)
    const [{ message, signature, nonce }] = logins
    expect(nonce).toBe(NONCE)
    expect(signature).toBe(SIGNATURE)
    // The API refuses a message written for any other site, so it must name this page's own host.
    expect(message).toMatch(new RegExp(`^${window.location.host} wants you to sign in with your Ethereum account:\n${WALLET}\n`))
    expect(message).toContain('Sign in to Mitfah with this wallet.')
    expect(message).toContain(`Nonce: ${NONCE}`)
  })

  it('says so when the signature is declined', async () => {
    stubApi(() => json(200, TOKEN))
    await renderRoutes(routes, '/login', { wagmiConfig: makeWagmiConfig({ signMessageError: true }) })
    await connectAndSignIn()
    expect(await screen.findByText('Cancelled — nothing was signed.')).toBeInTheDocument()
  })

  it("shows the server's reason for refusing a sign-in", async () => {
    stubApi(() => json(400, { detail: 'This message was written for evil.example, not for this site' }))
    await renderRoutes(routes, '/login')
    await connectAndSignIn()
    expect(await screen.findByText(/written for evil\.example/)).toBeInTheDocument()
  })

  it('explains rate limiting', async () => {
    stubApi(() => json(429, { error: 'Rate limit exceeded' }))
    await renderRoutes(routes, '/login')
    await connectAndSignIn()
    expect(await screen.findByText(/too many attempts/i)).toBeInTheDocument()
  })

  it('goes on to `next` after signing in', async () => {
    stubApi(() => json(200, TOKEN))
    const router = await renderRoutes(routes, `/login?next=${encodeURIComponent('/contacts')}`)
    await connectAndSignIn()
    expect(await screen.findByRole('heading', { name: 'Contacts' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/contacts')
  })

  it('ignores a `next` that points off the site', async () => {
    stubApi(() => json(200, TOKEN))
    const router = await renderRoutes(routes, `/login?next=${encodeURIComponent('//evil.example')}`)
    await connectAndSignIn()
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')
  })

  it('sends an old sign-up link to sign-in, keeping where it was going', async () => {
    stubApi(() => json(200, TOKEN))
    const router = await renderRoutes(routes, `/signup?next=${encodeURIComponent('/onboarding')}`)
    expect(await screen.findByRole('heading', { name: 'Sign in with your wallet' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/onboarding')}`)
  })
})
