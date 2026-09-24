import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { WalletState } from '../api/types'
import { routes } from '../routes'
import { makeSession, makeWalletState, SEPOLIA, USDC, WETH } from '../test/fixtures'
import { answerRpc, isRpc, json, ME, renderRoutes, RpcError, setViewportWidth, TOKEN, WALLET } from '../test/utils'

// Polling waits 2 s between checks; the tests don't.
vi.mock('../lib/tx', async importOriginal => ({
  ...(await importOriginal<typeof import('../lib/tx')>()),
  sleep: () => Promise.resolve(),
}))

const MAINNET = 1
const TX_HASH = `0x${'78'.repeat(32)}`
const PREPARED_TX = { to: '0x2222222222222222222222222222222222222222', data: '0x8456cb59', value: '0x0', gas: '0x7530' }
const TOKENS = [
  { ticker: 'usdc', address: USDC },
  { ticker: 'weth', address: WETH },
]
const PENDING_KEY = 'mitfah-pending-owner-tx:7'
// Sepolia's exchange router, as /api/chains names it.
const ROUTER = '0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3'

interface ServerOptions {
  walletChains?: number[]
  /** GET /api/wallet answers in order; the last one repeats. */
  wallets?: WalletState[]
  /** A prepare endpoint that refuses, by path. */
  refuse?: Record<string, string>
  /** The wallet declines to send. */
  rejectSend?: boolean
  /** Holds the wallet's answers until this settles, like a wallet prompt left open. */
  walletPrompt?: Promise<void>
  /** What /api/wallet/session/confirm reports once mined. By default it matches the last prepare. */
  sessionOutcome?: 'granted' | 'revoked' | 'unrecognized_key'
}

interface Prepared {
  path: string
  body: Record<string, unknown>
}

/**
 * A signed-in owner whose wallet answers from `wallets`. Every prepare succeeds unless refused;
 * each confirm endpoint answers "pending" once per transaction, then its final status.
 */
function stubServer({
  walletChains = [SEPOLIA],
  wallets = [makeWalletState()],
  refuse = {},
  rejectSend = false,
  walletPrompt = Promise.resolve(),
  sessionOutcome,
}: ServerOptions = {}) {
  const prepared: Prepared[] = []
  const confirms: Record<string, unknown>[] = []
  const sessionConfirms: Record<string, unknown>[] = []
  const sent: Record<string, string>[] = []
  const sentTo: string[] = []
  let walletReads = 0

  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (isRpc(url)) {
        return walletPrompt.then(() =>
          answerRpc(init, {
            eth_sendTransaction: ([tx]) => {
              if (rejectSend) throw new RpcError(4001, 'User rejected the request.')
              sent.push(tx as Record<string, string>)
              sentTo.push(url)
              return TX_HASH
            },
          }),
        )
      }
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {}
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, { ...ME, owner_addr: WALLET, wallet_chains: walletChains }))
      if (url === '/api/chains') {
        return Promise.resolve(
          json(200, {
            chains: [
              { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: false, router: ROUTER },
              { chain_id: MAINNET, name: 'mainnet', native_ticker: 'ETH', fork: false, router: null },
            ],
          }),
        )
      }
      if (url.startsWith('/api/tokens')) return Promise.resolve(json(200, { tokens: TOKENS }))
      if (/^\/api\/wallet\/\d+$/.test(url)) {
        const answer = wallets[Math.min(walletReads, wallets.length - 1)]
        walletReads += 1
        return Promise.resolve(json(200, answer))
      }
      if (url.endsWith('/prepare')) {
        prepared.push({ path: url, body })
        if (url in refuse) return Promise.resolve(json(400, { detail: refuse[url] }))
        return Promise.resolve(json(200, { tx: PREPARED_TX }))
      }
      if (url === '/api/wallet/tx/confirm') {
        confirms.push(body)
        return Promise.resolve(
          confirms.length % 2 === 1
            ? json(202, { status: 'pending', tx_hash: body.tx_hash })
            : json(200, { status: 'confirmed', tx_hash: body.tx_hash }),
        )
      }
      if (url === '/api/wallet/session/confirm') {
        sessionConfirms.push(body)
        const last = prepared.findLast(p => p.path === '/api/wallet/session/prepare')
        const outcome = sessionOutcome ?? (last?.body.action === 'remove' ? 'revoked' : 'granted')
        return Promise.resolve(
          sessionConfirms.length % 2 === 1
            ? json(202, { status: 'pending', tx_hash: body.tx_hash })
            : json(200, { status: outcome, tx_hash: body.tx_hash }),
        )
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { prepared, confirms, sessionConfirms, sent, sentTo, walletReads: () => walletReads }
}

/** Connects the mock wallet from the page's owner bar (the top bar has its own button too). */
async function connectFromOwnerBar(user: UserEvent) {
  const bar = (await screen.findByText(/Connect your owner wallet/)).closest('.mf-owner-bar') as HTMLElement
  await user.click(within(bar).getByRole('button', { name: 'Connect wallet' }))
  await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
}

async function connect(user: UserEvent) {
  await connectFromOwnerBar(user)
  await screen.findByText('Owner connected')
}

const row = (title: string) => screen.getByRole('heading', { name: title, level: 3 }).closest('.mf-control-row') as HTMLElement

describe('ControlsPage', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
    sessionStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('pauses the wallet in one click and waits for the network', async () => {
    const paused = makeWalletState({ paused: true })
    const server = stubServer({ wallets: [makeWalletState(), paused] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(within(row('Wallet')).getByText('Active')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))

    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(server.prepared).toEqual([{ path: '/api/wallet/pause/prepare', body: { chain_id: SEPOLIA } }])
    expect(server.sent).toEqual([
      expect.objectContaining({ from: WALLET, to: PREPARED_TX.to, data: PREPARED_TX.data, gas: PREPARED_TX.gas }),
    ])
    // Pending once, then confirmed.
    expect(server.confirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
    ])
    // The wallet was read again and now shows as paused.
    expect(await within(row('Wallet')).findByText('Paused')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unpause' })).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it('asks before unpausing, and sends nothing if cancelled', async () => {
    const server = stubServer({ wallets: [makeWalletState({ paused: true })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Unpause' }))
    let dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Unpause this wallet?')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(server.prepared).toEqual([])

    await user.click(screen.getByRole('button', { name: 'Unpause' }))
    dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Unpause wallet' }))
    expect(await screen.findByText('Wallet unpaused.')).toBeInTheDocument()
    expect(server.prepared).toEqual([{ path: '/api/wallet/unpause/prepare', body: { chain_id: SEPOLIA } }])
  })

  it('turns the assistant off in one click, and asks before turning it back on with a new key', async () => {
    const server = stubServer({
      wallets: [makeWalletState(), makeWalletState({ session: makeSession('off') })],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Turn off assistant' }))
    expect(await screen.findByText('Assistant turned off. It can no longer act for this wallet.')).toBeInTheDocument()
    expect(await within(row('Assistant')).findByText('Off')).toBeInTheDocument()

    // Turning it off makes Mitfah forget the key, and turning it on mints a new one, so the switch
    // is still offered with no key held.
    await user.click(screen.getByRole('button', { name: 'Turn on assistant' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(
      /For 30 days, until \w{3} \d{1,2}, \d{4}, it will be able to move funds from this wallet, up to \$100\.00 every 24 hours\./,
    )
    await user.click(within(dialog).getByRole('button', { name: 'Turn on assistant' }))
    expect(await screen.findByText(/^Assistant turned on until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()
    expect(server.prepared.map(p => p.body)).toEqual([
      { chain_id: SEPOLIA, action: 'remove' },
      { chain_id: SEPOLIA, action: 'add', ttl_secs: 30 * 86_400 },
    ])
    // Both went through the endpoint that files or forgets the key, never the generic one.
    expect(server.sessionConfirms).toHaveLength(4)
    expect(server.confirms).toEqual([])
  })

  it("shows when the assistant's access ends, and renews it after asking", async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const assistant = within(row('Assistant'))
    expect(assistant.getByText('On')).toBeInTheDocument()
    expect(assistant.getByText(/can spend within your limit until \w{3} \d{1,2}, \d{4}\./)).toBeInTheDocument()
    await user.click(assistant.getByRole('button', { name: 'Renew' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText("Renew the assistant's access?")).toBeInTheDocument()
    expect(dialog).toHaveTextContent(/It keeps its access for another 30 days, until \w{3} \d{1,2}, \d{4}, and can move funds/)
    await user.click(within(dialog).getByRole('button', { name: 'Renew' }))

    expect(await screen.findByText(/^Assistant renewed until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()
    expect(server.prepared).toEqual([
      { path: '/api/wallet/session/prepare', body: { chain_id: SEPOLIA, action: 'add', ttl_secs: 30 * 86_400 } },
    ])
    expect(server.sessionConfirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
    ])
  })

  it('warns while the access is about to run out', async () => {
    stubServer({ wallets: [makeWalletState({ session: makeSession('expiring') })] })
    await renderRoutes(routes, '/controls')

    const assistant = within((await screen.findByRole('heading', { name: 'Assistant', level: 3 })).closest('.mf-control-row') as HTMLElement)
    expect(assistant.getByText('Expires soon')).toBeInTheDocument()
    expect(assistant.getByText(/access runs out in 1 d 23 h|access runs out in 2 d/)).toBeInTheDocument()
    expect(assistant.getByRole('button', { name: 'Renew' })).toBeInTheDocument()
    expect(assistant.getByRole('button', { name: 'Turn off assistant' })).toBeInTheDocument()
  })

  it('offers only a renewal once the access has run out', async () => {
    stubServer({ wallets: [makeWalletState({ session: makeSession('expired') })] })
    await renderRoutes(routes, '/controls')

    const assistant = within((await screen.findByRole('heading', { name: 'Assistant', level: 3 })).closest('.mf-control-row') as HTMLElement)
    expect(assistant.getByText('Expired')).toBeInTheDocument()
    expect(assistant.getByText(/access ran out on \w{3} \d{1,2}, \d{4}, so it can't act/)).toBeInTheDocument()
    expect(assistant.getByRole('button', { name: 'Renew' })).toBeInTheDocument()
    // An expired key can do nothing, and a new grant replaces it anyway.
    expect(assistant.queryByRole('button', { name: 'Turn off assistant' })).toBeNull()
  })

  it('offers to replace a key the wallet trusts that Mitfah does not hold', async () => {
    stubServer({ wallets: [makeWalletState({ session: makeSession('foreign') })] })
    await renderRoutes(routes, '/controls')

    const assistant = within((await screen.findByRole('heading', { name: 'Assistant', level: 3 })).closest('.mf-control-row') as HTMLElement)
    expect(assistant.getByText('Off')).toBeInTheDocument()
    expect(assistant.getByText(/trusts a signing key Mitfah doesn't hold/)).toBeInTheDocument()
    expect(assistant.getByRole('button', { name: 'Turn on assistant' })).toBeInTheDocument()
  })

  it('says so when the wallet ends up trusting a key Mitfah does not hold', async () => {
    const server = stubServer({ sessionOutcome: 'unrecognized_key' })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Renew' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Renew' }))

    expect(await within(row('Assistant')).findByText(/now trusts a key Mitfah doesn't hold/)).toBeInTheDocument()
    expect(screen.queryByText(/^Assistant renewed/)).toBeNull()
    // It mined, so there's nothing to check again, and the wallet was read afresh.
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
    expect(server.walletReads()).toBeGreaterThan(1)
  })

  it('asks before raising the limit', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const limit = screen.getByLabelText('Limit per period')
    expect(limit).toHaveValue('100')
    const save = within(limit.closest('form')!).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.clear(limit)
    await user.type(limit, '250')
    await user.click(save)
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The assistant will be able to spend up to $250.00 every 24 hours, up from $100.00.')
    await user.click(within(dialog).getByRole('button', { name: 'Raise limit' }))

    expect(await screen.findByText('Spending limit set to $250.00.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      { path: '/api/wallet/daily-limit/prepare', body: { chain_id: SEPOLIA, daily_limit_usd: 250 } },
    ])
  })

  it('lowers the limit straight away, and explains a $0 limit', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const limit = screen.getByLabelText('Limit per period')
    await user.clear(limit)
    await user.type(limit, '0')
    expect(screen.getByText("A $0 limit stops the assistant spending without turning it off.")).toBeInTheDocument()
    await user.click(within(limit.closest('form')!).getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Spending limit set to $0.00.')).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(server.prepared[0].body).toEqual({ chain_id: SEPOLIA, daily_limit_usd: 0 })
  })

  it('asks before shortening the period, and a confirmed change resets the editor', async () => {
    const weekly = makeWalletState({ spending: { ...makeWalletState().spending, window_hours: 168 } })
    const daily = makeWalletState()
    const server = stubServer({ wallets: [weekly, daily] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const period = screen.getByRole('radiogroup', { name: 'Period' })
    expect(within(period).getByRole('radio', { name: '7 days' })).toBeChecked()
    await user.click(within(period).getByRole('radio', { name: '24 hours' }))
    await user.click(within(period.closest('form')!).getByRole('button', { name: 'Save' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Your limit will refill every 24 hours instead of every 7 days')
    await user.click(within(dialog).getByRole('button', { name: 'Shorten period' }))

    expect(await screen.findByText('The limit now refills every 24 hours.')).toBeInTheDocument()
    expect(server.prepared[0]).toEqual({ path: '/api/wallet/window-duration/prepare', body: { chain_id: SEPOLIA, window_secs: 86_400 } })
    const saved = screen.getByRole('radiogroup', { name: 'Period' })
    expect(within(saved).getByRole('radio', { name: '24 hours' })).toBeChecked()
    expect(within(saved.closest('form')!).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('counts a new token, and asks before it stops counting one', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const counted = screen.getByRole('list', { name: 'Counted tokens' })
    expect(within(counted).getByText('Always counts')).toBeInTheDocument()
    expect(within(counted).getByText('USDC')).toBeInTheDocument()

    // WETH isn't counted yet, so it's the one to add.
    await user.click(await screen.findByRole('combobox', { name: 'Add a token' }))
    await user.click(await screen.findByRole('option', { name: 'WETH' }))
    await user.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText('WETH now counts toward your limit.')).toBeInTheDocument()

    await user.click(within(counted).getByRole('button', { name: 'Stop counting USDC' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The assistant will be able to move USDC out of this wallet without any limit.')
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting USDC' }))
    expect(await screen.findByText('USDC no longer counts toward your limit.')).toBeInTheDocument()

    expect(server.prepared).toEqual([
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'weth', action: 'add' } },
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'usdc', action: 'remove' } },
    ])
  })

  it('says when every listed token already counts, and leaves an unlisted one alone', async () => {
    const unlisted = '0x7777777777777777777777777777777777777777'
    stubServer({
      wallets: [
        makeWalletState({
          spending: {
            ...makeWalletState().spending,
            watched_tokens: [
              { ticker: 'usdc', address: USDC },
              { ticker: 'weth', address: WETH },
              { ticker: null, address: unlisted },
            ],
          },
        }),
      ],
    })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText('Every token Mitfah lists on this network already counts.')).toBeInTheDocument()
    expect(screen.getByText("Not a token Mitfah lists, so it can't be removed here.")).toBeInTheDocument()
  })

  it('trusts a spender only after a warning, and checks the address first', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(await screen.findByText('None.')).toBeInTheDocument()
    const input = screen.getByLabelText('Trust a spender')
    const trust = screen.getByRole('button', { name: 'Trust' })

    await user.type(input, '0x123')
    expect(screen.getByText('Enter a full address starting with 0x.')).toBeInTheDocument()
    expect(trust).toBeDisabled()

    await user.clear(input)
    await user.type(input, '0xeb2a2b8f6e0b1a1b3e5ec2df1d2b2e1ad2e1f0a7')
    await user.click(trust)
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent("Only trust a contract you know, such as your exchange's router.")
    await user.click(within(dialog).getByRole('button', { name: 'Trust spender' }))

    expect(await screen.findByText('Spender trusted.')).toBeInTheDocument()
    expect(server.prepared[0]).toEqual({
      path: '/api/wallet/trusted-spenders/prepare',
      body: { chain_id: SEPOLIA, spender: '0xeb2A2b8F6e0b1a1b3e5ec2dF1d2B2E1ad2E1F0A7', action: 'add' },
    })
  })

  it('removes a trusted spender without asking', async () => {
    const spender = '0xeb2A2b8F6e0b1a1b3e5ec2dF1d2B2E1ad2E1F0A7'
    const server = stubServer({
      wallets: [makeWalletState({ limits: { ...makeWalletState().limits, trusted_spenders: [spender] } })],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    await user.click(await screen.findByRole('button', { name: `Stop trusting ${spender}` }))
    expect(await screen.findByText('Spender removed.')).toBeInTheDocument()
    expect(server.prepared[0].body).toEqual({ chain_id: SEPOLIA, spender, action: 'remove' })
  })

  it("keeps the exchange's router off the list, so it can't be removed by accident", async () => {
    const other = '0xeb2A2b8F6e0b1a1b3e5ec2dF1d2B2E1ad2E1F0A7'
    // Lower case on purpose: the match must not depend on how the address is written.
    const trusted = [ROUTER.toLowerCase(), other]
    stubServer({ wallets: [makeWalletState({ limits: { ...makeWalletState().limits, trusted_spenders: trusted } })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    const list = await screen.findByRole('list', { name: 'Trusted spenders' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(within(list).getByRole('button', { name: `Stop trusting ${other}` })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: new RegExp(`Stop trusting ${ROUTER}`, 'i') })).toBeNull()
    expect(screen.getByText(/Your exchange's router is trusted too/)).toBeInTheDocument()
    expect(screen.queryByText('None.')).toBeNull()

    // Still counted as trusted, so it can't be added a second time.
    await user.type(screen.getByLabelText('Trust a spender'), ROUTER)
    expect(screen.getByText('Already trusted.')).toBeInTheDocument()
  })

  it('asks before raising the network fee cap', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    const cap = await screen.findByLabelText('Most per operation')
    expect(cap).toHaveValue('0.01')
    await user.clear(cap)
    await user.type(cap, '0.05')
    await user.click(within(cap.closest('form')!).getByRole('button', { name: 'Save' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('up to 0.05 ETH in fees, up from 0.01 ETH')
    await user.click(within(dialog).getByRole('button', { name: 'Raise cap' }))
    expect(await screen.findByText('Network fee cap set to 0.05 ETH.')).toBeInTheDocument()
    expect(server.prepared[0]).toEqual({
      path: '/api/wallet/max-op-gas-cost/prepare',
      body: { chain_id: SEPOLIA, max_cost_eth: '0.05' },
    })
  })

  it('explains a change the wallet would refuse, before anything is signed', async () => {
    const server = stubServer({
      refuse: { '/api/wallet/pause/prepare': 'This transaction would fail: EnforcedPause()' },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))
    expect(await within(row('Wallet')).findByText('The wallet is already paused.')).toBeInTheDocument()
    expect(server.sent).toEqual([])
  })

  it('says so when the change is declined in the wallet', async () => {
    const server = stubServer({ rejectSend: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))
    expect(await within(row('Wallet')).findByText('Cancelled — nothing was sent.')).toBeInTheDocument()
    expect(server.confirms).toEqual([])
    // Nothing is left waiting, so the next change can start.
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeEnabled()
  })

  it('asks for the owner wallet before anything can change', async () => {
    stubServer()
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/Connect your owner wallet/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
    expect(screen.getByLabelText('Limit per period')).toBeDisabled()
  })

  it('points out a connected wallet that is not the owner', async () => {
    const owner = '0x9999999999999999999999999999999999999999'
    stubServer({ wallets: [makeWalletState({ owner })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connectFromOwnerBar(user)
    expect(await screen.findByText(/Switch to the linked account in your wallet\./)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Link this address instead' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
  })

  it("is read-only for an account that isn't linked to the owner", async () => {
    stubServer({ wallets: [makeWalletState({ is_owner: false })] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/so they're switched off here/)).toBeInTheDocument()
    expect(screen.queryByText(/Connect your owner wallet/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Turn off assistant' })).toBeDisabled()
  })

  it('switches the wallet to the right network before signing', async () => {
    const server = stubServer({ walletChains: [MAINNET], wallets: [makeWalletState({ chain_id: MAINNET })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(screen.getByText(/Your wallet is on Sepolia/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))

    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(server.prepared[0].body).toEqual({ chain_id: MAINNET })
    expect(server.sentTo).toHaveLength(1)
    expect(server.sentTo[0]).not.toMatch(/sepolia/)
    expect(screen.queryByText(/Your wallet is on Sepolia/)).toBeNull()
  })

  it('keeps waiting for a change sent before a reload', async () => {
    sessionStorage.setItem(
      PENDING_KEY,
      JSON.stringify({ chainId: SEPOLIA, txHash: TX_HASH, key: 'pause', success: 'Wallet paused. Nothing can go out until you unpause it.' }),
    )
    const server = stubServer()
    await renderRoutes(routes, '/controls')

    // No wallet connected: waiting needs only the API.
    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(server.confirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
    ])
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it("finishes the assistant's key through its own endpoint after a reload", async () => {
    sessionStorage.setItem(
      PENDING_KEY,
      JSON.stringify({ chainId: SEPOLIA, txHash: TX_HASH, key: 'session-on', success: 'Assistant renewed.', confirmWith: 'session' }),
    )
    const server = stubServer()
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText('Assistant renewed.')).toBeInTheDocument()
    expect(server.sessionConfirms).toHaveLength(2)
    expect(server.confirms).toEqual([])
  })

  it('finishes a change on the next page if this one is left while the wallet is open', async () => {
    let approve!: () => void
    const server = stubServer({ walletPrompt: new Promise(resolve => (approve = resolve)) })
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))
    expect(await screen.findByText('Confirm in your wallet.')).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: 'Dashboard' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/dashboard'))
    await screen.findByText(/^Your Mitfah wallet on/)

    approve()
    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
    // One page waited for it, not two.
    expect(server.confirms).toHaveLength(2)
    expect(screen.getAllByText('Wallet paused. Nothing can go out until you unpause it.')).toHaveLength(1)
  })

  it('offers to create a wallet when there is none', async () => {
    stubServer({ walletChains: [] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByRole('heading', { name: 'Create your wallet' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Controls', level: 1 })).toBeInTheDocument()
  })
})
