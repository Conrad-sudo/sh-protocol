import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../../api/client'
import { routes } from '../../routes'
import { json, ME, renderRoutes, setViewportWidth, TOKEN } from '../../test/utils'

const SEPOLIA = 11155111
const BSC = 56
const BASE = 8453

/** Three networks, each with a bot of its own, like a server with every MITFAH_<CHAIN>_USERNAME set. */
const CHAINS = [
  { chain_id: BSC, name: 'bsc', native_ticker: 'BNB', telegram_bot: 'mitfah_bsc_bot' },
  { chain_id: BASE, name: 'base', native_ticker: 'ETH', telegram_bot: 'mitfah_base_bot' },
  { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', telegram_bot: 'mitfah_sepolia_bot' },
].map(chain => ({ ...chain, fork: false, rpc_url: null, router: null }))

interface ServerOptions {
  linked?: boolean
  /** The networks the account has a wallet on. */
  walletChains?: number[]
  /** POST answers like a server with no bot usernames set. */
  notConfigured?: boolean
  /** DELETE answers 500. */
  unlinkFails?: boolean
}

/** A signed-in account with the Telegram endpoints, behaving like app/api.py. */
function stubServer({
  linked = false,
  walletChains = [BASE],
  notConfigured = false,
  unlinkFails = false,
}: ServerOptions = {}) {
  const server = { linked, links: 0, unlinks: 0, meCalls: 0, linkUrls: [] as string[] }
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') {
        server.meCalls++
        return Promise.resolve(json(200, { ...ME, telegram_linked: server.linked, wallet_chains: walletChains }))
      }
      if (url === '/api/chains') return Promise.resolve(json(200, { chains: CHAINS }))
      if (url.startsWith('/api/integrations/telegram/link') && method === 'POST') {
        server.linkUrls.push(url)
        if (notConfigured) {
          return Promise.resolve(json(500, { detail: 'Telegram is not configured: no MITFAH_<CHAIN>_USERNAME is set' }))
        }
        server.links++
        const nonce = `nonce-${server.links}`
        return Promise.resolve(
          json(200, { url: `https://t.me/mitfah_base_bot?start=${nonce}`, nonce, expires_in: 600 }),
        )
      }
      if (url === '/api/integrations/telegram/link' && method === 'DELETE') {
        server.unlinks++
        if (unlinkFails) return Promise.resolve(new Response('Internal Server Error', { status: 500 }))
        server.linked = false
        return Promise.resolve(json(200, { status: 'unlinked' }))
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return server
}

async function setup() {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
  await renderRoutes(routes, '/settings')
  return user
}

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))

describe('TelegramCard', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    resetClientForTests()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('hands out the link, waits, and shows the chat as linked once the bot has it', async () => {
    const server = stubServer()
    const user = await setup()

    await user.click(await screen.findByRole('button', { name: 'Link Telegram' }))
    const open = await screen.findByRole('link', { name: 'Open Telegram' })
    // The link opens the bot of the network the app is showing: the account's wallet, on Base.
    expect(server.linkUrls).toEqual([`/api/integrations/telegram/link?chain_id=${BASE}`])
    expect(open).toHaveAttribute('href', 'https://t.me/mitfah_base_bot?start=nonce-1')
    expect(open).toHaveAttribute('target', '_blank')
    expect(open).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getByTitle('QR code of the Telegram link')).toBeInTheDocument()
    expect(screen.getByText('10:00')).toBeInTheDocument()
    expect(screen.getByText('Waiting for Telegram…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Link Telegram' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Each network has its own bot/)).not.toBeInTheDocument()

    // Nothing happened in Telegram yet: still waiting after a few polls.
    const before = server.meCalls
    await advance(9_000)
    expect(server.meCalls).toBeGreaterThanOrEqual(before + 3)
    expect(screen.getByText('9:51')).toBeInTheDocument()

    // The user presses Start; the next poll sees it.
    server.linked = true
    await advance(3_000)
    expect(await screen.findByText('Linked')).toBeInTheDocument()
    expect(screen.getByText('Telegram linked. You can now chat with your assistant there.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open Telegram' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '@mitfah_base_bot' })).toHaveAttribute('href', 'https://t.me/mitfah_base_bot')

    // And polling stops.
    const after = server.meCalls
    await advance(10_000)
    expect(server.meCalls).toBe(after)
  })

  it('stops waiting when the link expires, and gets a fresh one on request', async () => {
    const server = stubServer()
    const user = await setup()

    await user.click(await screen.findByRole('button', { name: 'Link Telegram' }))
    await screen.findByRole('link', { name: 'Open Telegram' })
    await advance(600_000)
    expect(await screen.findByText('That link has expired.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open Telegram' })).not.toBeInTheDocument()

    const polls = server.meCalls
    await advance(10_000)
    expect(server.meCalls).toBe(polls)

    await user.click(screen.getByRole('button', { name: 'Get a new link' }))
    const open = await screen.findByRole('link', { name: 'Open Telegram' })
    expect(open).toHaveAttribute('href', 'https://t.me/mitfah_base_bot?start=nonce-2')
    expect(screen.getByText('10:00')).toBeInTheDocument()
  })

  it('lists the bots of the networks the account has a wallet on, once linked', async () => {
    stubServer({ linked: true, walletChains: [SEPOLIA, BASE] })
    await setup()

    const base = await screen.findByRole('link', { name: '@mitfah_base_bot' })
    expect(base).toHaveAttribute('href', 'https://t.me/mitfah_base_bot')
    expect(base).toHaveAttribute('target', '_blank')
    expect(base).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getByRole('link', { name: '@mitfah_sepolia_bot' })).toHaveAttribute(
      'href',
      'https://t.me/mitfah_sepolia_bot',
    )
    expect(screen.getByText(/Each network has its own bot/)).toBeInTheDocument()
    // No wallet on BNB Smart Chain, so no bot to open there.
    expect(screen.queryByRole('link', { name: '@mitfah_bsc_bot' })).not.toBeInTheDocument()
  })

  it('says so when the server has no Telegram bot', async () => {
    stubServer({ notConfigured: true })
    const user = await setup()

    await user.click(await screen.findByRole('button', { name: 'Link Telegram' }))
    expect(await screen.findByText("Telegram isn't set up on this server.")).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open Telegram' })).not.toBeInTheDocument()
  })

  it('unlinks a linked chat', async () => {
    const server = stubServer({ linked: true })
    const user = await setup()

    expect(await screen.findByText('Linked')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Unlink' }))
    expect(await screen.findByRole('button', { name: 'Link Telegram' })).toBeInTheDocument()
    expect(server.unlinks).toBe(1)
    expect(screen.queryByText('Linked')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '@mitfah_base_bot' })).not.toBeInTheDocument()
  })

  it('keeps the chat linked and says so when unlinking fails', async () => {
    stubServer({ linked: true, unlinkFails: true })
    const user = await setup()

    await user.click(await screen.findByRole('button', { name: 'Unlink' }))
    expect(
      await screen.findByText("Couldn't unlink Telegram. Something went wrong on our side. Please try again."),
    ).toBeInTheDocument()
    expect(screen.getByText('Linked')).toBeInTheDocument()
  })
})
