import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { answerRpc, isRpc, json, makeWagmiConfig, ME, renderRoutes, TOKEN, WALLET } from '../test/utils'
import { routes } from '../routes'

const NONCE = 'f00dfeed1234abcd'
const SIGNATURE = `0x${'ab'.repeat(65)}`

/**
 * Signed out on load. The nonce endpoint answers NONCE, the mock wallet signs anything, the address
 * check answers `registered`, and `/api/auth/siwe/login` answers with `loginResponse`. Returns what
 * was posted to it.
 */
function stubApi(loginResponse: () => Response, { registered = true }: { registered?: boolean } = {}) {
  const logins: { message: string; signature: string; nonce: string }[] = []
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (isRpc(url)) return Promise.resolve(answerRpc(init, { eth_sign: () => SIGNATURE }))
    if (url.endsWith('/api/auth/refresh')) return Promise.resolve(json(401, { detail: 'No refresh token' }))
    if (url.endsWith('/api/auth/siwe/nonce')) return Promise.resolve(json(200, { nonce: NONCE }))
    if (url.endsWith(`/api/auth/siwe/account?address=${WALLET}`)) return Promise.resolve(json(200, { registered }))
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

/** Connects the mock wallet. */
async function connect() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Connect wallet' }))
  await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
  return user
}

/** Connects the mock wallet, then signs in with it as a returning address. */
async function connectAndSignIn() {
  const user = await connect()
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
    expect(screen.getByText(/First time\? Signing in creates your account\./)).toBeInTheDocument()
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

  it('greets a returning address and keeps the first-time notes out of the way', async () => {
    stubApi(() => json(200, TOKEN))
    await renderRoutes(routes, '/login')
    await connect()

    expect(await screen.findByText('Welcome back.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Sign in with your wallet' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign up' })).toBeNull()
    expect(screen.queryByText(/to prove it's yours/)).toBeNull()
    expect(screen.queryByText('This address is your Mitfah account.')).toBeNull()
    // Once a wallet is connected, the line about which wallet to connect is gone.
    expect(screen.queryByText(/First time\? Signing in creates your account\./)).toBeNull()
  })

  it('asks a new address to sign up, and explains what the address is for', async () => {
    const { logins } = stubApi(() => json(200, TOKEN), { registered: false })
    await renderRoutes(routes, '/login')
    const user = await connect()

    expect(await screen.findByText(/to prove it's yours\. Signing is free and doesn't move any funds\./)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Sign up with your wallet' })).toBeInTheDocument()
    expect(screen.getByText('This address is your Mitfah account.')).toBeInTheDocument()
    expect(screen.getByText(/the only address that can pause your smart wallet, change its limits or withdraw\./)).toBeInTheDocument()
    expect(screen.queryByText(/can't recover your account/)).toBeNull()
    expect(screen.queryByText('Welcome back.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
    expect(screen.queryByText(/First time\? Signing in creates your account\./)).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Sign up' }))
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(logins).toHaveLength(1)
  })

  it('turns the safe dial behind the card before the session starts', async () => {
    stubApi(() => json(200, TOKEN), { registered: false })
    // jsdom can't animate, so the dial's turn is stood in for: it ends when `open` is called.
    let open!: () => void
    const finished = new Promise<void>(resolve => (open = resolve))
    const animate = vi.fn(() => ({ finished }))
    Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: animate })
    try {
      const router = await renderRoutes(routes, '/login')
      const user = await connect()
      await user.click(await screen.findByRole('button', { name: 'Sign up' }))

      await waitFor(() => expect(animate).toHaveBeenCalled())
      expect(animate.mock.contexts[0]).toBe(document.querySelector('.mf-wallpaper .mf-safe-rotor'))
      // Signed, but still here: the page stays as it was, busy, until the safe has opened.
      expect(router.state.location.pathname).toBe('/login')
      expect(screen.getByRole('heading', { name: 'Sign up with your wallet' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Sign up' })).toHaveAttribute('data-loading', 'true')

      open()
      expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe('/dashboard')
    } finally {
      delete (Element.prototype as Partial<Element>).animate
    }
  })

  it('leaves through the opening safe only after signing in here', async () => {
    stubApi(() => json(200, TOKEN))
    await renderRoutes(routes, '/login')
    await connectAndSignIn()
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    // The view transition's styles hold for the change of page, and then go.
    expect(document.documentElement.dataset.safe).toBe('open')
    await waitFor(() => expect(document.documentElement.dataset.safe).toBeUndefined(), { timeout: 3_000 })
    cleanup()

    // Already signed in: straight on, with nothing to open.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => Promise.resolve(url.endsWith('/api/auth/refresh') ? json(200, TOKEN) : json(200, ME))),
    )
    resetClientForTests()
    const router = await renderRoutes(routes, '/login')
    await waitFor(() => expect(router.state.location.pathname).toBe('/dashboard'))
    expect(document.documentElement.dataset.safe).toBeUndefined()
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
