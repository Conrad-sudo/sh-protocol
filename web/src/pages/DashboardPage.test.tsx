import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { WalletState } from '../api/types'
import { routes } from '../routes'
import { makeSession, makeWalletState, SEPOLIA, USDC } from '../test/fixtures'
import { answerRpc, isRpc, json, ME, renderRoutes, setViewportWidth, TOKEN, WALLET } from '../test/utils'

// Owner transactions poll with a 2 s gap; the tests don't wait.
vi.mock('../lib/tx', async importOriginal => ({
  ...(await importOriginal<typeof import('../lib/tx')>()),
  sleep: () => Promise.resolve(),
}))

const BSC = 56
const BASE = 8453
const TX_HASH = `0x${'34'.repeat(32)}`
const CHAINS = [
  { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: false },
  { chain_id: BSC, name: 'bsc', native_ticker: 'BNB', fork: false },
  { chain_id: 1, name: 'mainnet', native_ticker: 'ETH', fork: false },
  { chain_id: BASE, name: 'base', native_ticker: 'ETH', fork: false },
]

type WalletAnswer = WalletState | { status: number; detail: string }

interface ServerOptions {
  walletChains?: number[]
  /** Answers for GET /api/wallet/{chain_id}, in order; the last one repeats. */
  wallets?: Partial<Record<number, WalletAnswer[]>>
  /** Answers for other API routes, keyed by "METHOD /path"; each gets the parsed request body. */
  extra?: Record<string, (body: unknown) => Response>
  /** Holds the wallet's answer to a send until this settles, like a wallet prompt left open. */
  walletPrompt?: Promise<void>
  /** Answers POST /api/wallet/tx/confirm; by default every transaction has mined. */
  confirm?: () => Response | Promise<Response>
}

/** A signed-in account whose wallets answer from `wallets`. Also answers the mock wallet's RPC. */
function stubServer({
  walletChains = [SEPOLIA],
  wallets = {},
  extra = {},
  walletPrompt = Promise.resolve(),
  confirm = () => json(200, { status: 'confirmed', tx_hash: TX_HASH }),
}: ServerOptions = {}) {
  const walletCalls: number[] = []
  const sent: Record<string, string>[] = []
  const prepared: { path: string; body: unknown }[] = []
  const requests: { route: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (isRpc(url)) {
        const sending = String(init?.body).includes('"eth_sendTransaction"')
        return (sending ? walletPrompt : Promise.resolve()).then(() =>
          answerRpc(init, {
            eth_sendTransaction: ([tx]) => {
              sent.push(tx as Record<string, string>)
              return TX_HASH
            },
          }),
        )
      }
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, { ...ME, owner_addr: WALLET, wallet_chains: walletChains }))
      if (url === '/api/chains') return Promise.resolve(json(200, { chains: CHAINS }))
      const route = `${(init?.method ?? 'GET').toUpperCase()} ${url}`
      if (extra[route]) {
        const body = init?.body ? JSON.parse(String(init.body)) : undefined
        requests.push({ route, body })
        return Promise.resolve(extra[route](body))
      }
      if (url.endsWith('/prepare')) {
        prepared.push({ path: url, body: JSON.parse(String(init?.body)) })
        return Promise.resolve(json(200, { tx: { to: '0x2222222222222222222222222222222222222222', data: '0x1234' } }))
      }
      if (url === '/api/wallet/tx/confirm') return Promise.resolve(confirm())
      const match = /^\/api\/wallet\/(\d+)$/.exec(url)
      if (match) {
        const chainId = Number(match[1])
        const answers = wallets[chainId] ?? [makeWalletState({ chain_id: chainId })]
        const answer = answers[Math.min(walletCalls.filter(id => id === chainId).length, answers.length - 1)]
        walletCalls.push(chainId)
        return Promise.resolve('status' in answer ? json(answer.status, { detail: answer.detail }) : json(200, answer))
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { walletCalls, sent, prepared, requests }
}

const walletHeader = () => screen.findByText(/^Your Mitfah smart wallet on/)
const pendingKey = (chainId: number) => `mitfah-pending-owner-tx:7:${chainId}`

/** Opens "Remove USDC?", connecting the owner wallet from it unless it already is. */
async function openRemoveUsdc(user: UserEvent, { connect = true } = {}) {
  await user.click(screen.getByRole('button', { name: 'Remove USDC from your dashboard' }))
  const dialog = await screen.findByRole('alertdialog', { name: 'Remove USDC?' })
  if (connect) {
    await user.click(within(dialog).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
  }
  await within(dialog).findByText('Owner connected')
  return dialog
}

describe('DashboardPage', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    sessionStorage.clear()
  })

  it('shows an active wallet: status, what is left to spend, and balances', async () => {
    stubServer()
    await renderRoutes(routes, '/dashboard')

    expect(await walletHeader()).toHaveTextContent('Your Mitfah smart wallet on Sepolia')
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText('Assistant on')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View on explorer' })).toHaveAttribute(
      'href',
      'https://sepolia.etherscan.io/address/0x2222222222222222222222222222222222222222',
    )
    expect(screen.queryByRole('alert')).toBeNull()

    // $40 of $100 spent an hour into a 24-hour period.
    expect(screen.getByRole('progressbar', { name: '60% of the limit left' })).toBeInTheDocument()
    expect(screen.getByText('$60.00')).toBeInTheDocument()
    expect(screen.getByText('Spent this period').nextElementSibling).toHaveTextContent('$40.00')
    expect(screen.getByText('Limit').nextElementSibling).toHaveTextContent('$100.00 every 24 hours')
    expect(screen.getByText(/^Resets in 2[23] h/)).toBeInTheDocument()

    const balances = within(screen.getByRole('table', { name: 'Token balances' }))
    const row = (ticker: string) => balances.getByRole('rowheader', { name: new RegExp(`^${ticker}`) }).closest('tr')!
    expect(row('ETH')).toHaveTextContent('1.5')
    expect(row('USDC')).toHaveTextContent('25')
    expect(row('USDC')).not.toHaveTextContent('Not limited')
    // WETH is not watched, so the assistant could move it without limit.
    expect(row('WETH')).toHaveTextContent('Not limited')
    expect(screen.getByRole('button', { name: 'Add funds' })).toBeInTheDocument()
  })

  it('warns about everything that weakens the protection', async () => {
    stubServer({
      wallets: {
        [SEPOLIA]: [
          makeWalletState({
            paused: true,
            is_owner: false,
            session: makeSession('off'),
            spending: { ...makeWalletState().spending, hook_installed: false },
            balances: [
              ...makeWalletState().balances.slice(0, 1),
              { ticker: 'usdc', address: '0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8', native: false, decimals: null, raw: null, amount: null, error: 'no code at address' },
            ],
          }),
        ],
      },
    })
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    expect(screen.getByText('Paused')).toBeInTheDocument()
    expect(screen.getByText('Assistant off')).toBeInTheDocument()
    expect(screen.getByText(/This wallet is paused/)).toBeInTheDocument()
    expect(screen.getByText(/spending limit isn't switched on/)).toBeInTheDocument()
    expect(screen.getByText(/must be signed by its owner/)).toBeInTheDocument()
    const unreadable = screen.getByText("Couldn't read")
    expect(unreadable).toHaveAttribute('title', 'no code at address')
    // The rest of the balances still show.
    expect(screen.getByRole('rowheader', { name: 'ETH' }).closest('tr')).toHaveTextContent('1.5')
  })

  it("warns before the assistant's access runs out, and says when it has", async () => {
    stubServer({ wallets: { [SEPOLIA]: [makeWalletState({ session: makeSession('expiring') })] } })
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    expect(screen.getByText('Assistant on')).toBeInTheDocument()
    expect(screen.getByText(/access runs out in (1 d 23 h|2 d)\. Renew it in Controls/)).toBeInTheDocument()
    cleanup()

    stubServer({ wallets: { [SEPOLIA]: [makeWalletState({ session: makeSession('expired') })] } })
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    expect(screen.getByText('Assistant expired')).toBeInTheDocument()
    expect(screen.getByText(/access ran out on \w{3} \d{1,2}, \d{4}, so it can't send anything/)).toBeInTheDocument()
  })

  it('counts an ended period as a full limit, though the stored spend is stale', async () => {
    const twoDaysAgo = Math.floor(Date.now() / 1000) - 2 * 86_400
    stubServer({
      wallets: {
        [SEPOLIA]: [makeWalletState({ spending: { ...makeWalletState().spending, window_start: twoDaysAgo } })],
      },
    })
    await renderRoutes(routes, '/dashboard')

    expect(await screen.findByText('Full limit available. A new period starts with the next spend.')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: '100% of the limit left' })).toBeInTheDocument()
    expect(screen.getByText('Spent this period').nextElementSibling).toHaveTextContent('$0.00')
  })

  it('offers to create a wallet when there is none, without asking for one', async () => {
    const { walletCalls } = stubServer({ walletChains: [] })
    await renderRoutes(routes, '/dashboard')

    expect(await screen.findByRole('heading', { name: 'Create your wallet' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Network:/ })).toBeNull()
    expect(walletCalls).toEqual([])
  })

  it("says so when the account's wallet on this network is gone", async () => {
    stubServer({ wallets: { [SEPOLIA]: [{ status: 404, detail: 'You have no wallet on chain 11155111.' }] } })
    await renderRoutes(routes, '/dashboard')

    expect(await screen.findByRole('heading', { name: 'No wallet on Sepolia' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Create one' })).toHaveAttribute('href', '/wallets/new')
  })

  it('explains a failed load and tries again', async () => {
    stubServer({
      wallets: { [SEPOLIA]: [{ status: 503, detail: 'The Sepolia node is not responding.' }, makeWalletState()] },
    })
    await renderRoutes(routes, '/dashboard')

    expect(await screen.findByText(/Couldn't load your wallet: The Sepolia node is not responding./)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Try again' }))
    expect(await walletHeader()).toBeInTheDocument()
  })

  it('switches between the networks the user has a wallet on', async () => {
    const { walletCalls } = stubServer({
      walletChains: [SEPOLIA, BSC],
      wallets: { [BSC]: [makeWalletState({ chain_id: BSC, paused: true })] },
    })
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/dashboard')

    expect(await walletHeader()).toHaveTextContent('on Sepolia')
    await user.click(screen.getByRole('button', { name: 'Network: Sepolia' }))
    // Ethereum has no wallet yet, so it is offered as a network to add rather than to show.
    expect(screen.queryByRole('menuitem', { name: /Ethereum/ })).toBeNull()
    await user.click(screen.getByRole('menuitem', { name: 'BNB Smart Chain' }))

    expect(await screen.findByText('Your Mitfah smart wallet on BNB Smart Chain')).toBeInTheDocument()
    expect(screen.getByText('Paused')).toBeInTheDocument()
    expect(walletCalls).toEqual([SEPOLIA, BSC])
    expect(localStorage.getItem('mitfah-chain:7')).toBe(String(BSC))

    await user.click(screen.getByRole('button', { name: 'Network: BNB Smart Chain' }))
    await user.click(screen.getByRole('menuitem', { name: 'Add a network' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/wallets/new'))
  })

  it('shows a wallet on Base by its name and logo, like every network', async () => {
    const { walletCalls } = stubServer({ walletChains: [SEPOLIA, BASE] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    expect(await walletHeader()).toHaveTextContent('on Sepolia')
    await user.click(screen.getByRole('button', { name: 'Network: Sepolia' }))
    const base = screen.getByRole('menuitem', { name: 'Base' })
    expect(base.querySelector('img')?.getAttribute('src')).toBe('/chainlogos/8453.svg')
    await user.click(base)

    expect(await screen.findByText('Your Mitfah smart wallet on Base')).toBeInTheDocument()
    expect(walletCalls).toEqual([SEPOLIA, BASE])
    const toggle = screen.getByRole('button', { name: 'Network: Base' })
    expect(toggle.querySelector('img')?.getAttribute('src')).toBe('/chainlogos/8453.svg')
  })

  it('funds the wallet from the connected browser wallet, and lists the deposit in the history', async () => {
    const { walletCalls, sent, requests } = stubServer({
      extra: { 'POST /api/transactions/deposit': () => json(200, { status: 'confirmed', tx_hash: TX_HASH }) },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Add funds' }))
    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getByRole('img', { name: /QR code of 0x2222/ })).toBeInTheDocument()
    expect(within(drawer).getByText('0x2222222222222222222222222222222222222222')).toBeInTheDocument()
    expect(within(drawer).getByText(/Only send on Sepolia/)).toBeInTheDocument()

    await user.click(within(drawer).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    const amount = await within(drawer).findByLabelText('Amount')
    expect(within(drawer).getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.type(amount, '0.5')
    await user.click(within(drawer).getByRole('button', { name: 'Send' }))

    expect(await within(drawer).findByText(/Received. Your balance is up to date./)).toBeInTheDocument()
    expect(sent).toEqual([
      expect.objectContaining({
        from: WALLET,
        to: '0x2222222222222222222222222222222222222222',
        value: '0x6f05b59d3b20000',
      }),
    ])
    // The balance was read again once the transfer confirmed.
    await waitFor(() => expect(walletCalls).toEqual([SEPOLIA, SEPOLIA]))
    // Followed through the API, on the node the balance is read from; that also lists it in the history.
    expect(requests).toEqual([
      { route: 'POST /api/transactions/deposit', body: { chain_id: SEPOLIA, tx_hash: TX_HASH } },
    ])
  })

  it('lets go of a wallet that never answers a transfer, so the drawer opens clean again', async () => {
    // Nobody answers the wallet's prompt. Closing the drawer used to leave the spinner for good.
    stubServer({ walletPrompt: new Promise(() => {}) })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Add funds' }))
    let drawer = await screen.findByRole('dialog')
    await user.click(within(drawer).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    await user.type(await within(drawer).findByLabelText('Amount'), '0.5')
    await user.click(within(drawer).getByRole('button', { name: 'Send' }))
    expect(await within(drawer).findByText('Confirm the transfer in your wallet.')).toBeInTheDocument()
    await user.click(within(drawer).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    await user.click(screen.getByRole('button', { name: 'Add funds' }))
    drawer = await screen.findByRole('dialog')
    expect(within(drawer).queryByText('Confirm the transfer in your wallet.')).toBeNull()
    await user.type(within(drawer).getByLabelText('Amount'), '0.5')
    const send = within(drawer).getByRole('button', { name: 'Send' })
    expect(send).toBeEnabled()

    // Without closing the drawer, "Stop waiting" frees the button where it is.
    await user.click(send)
    await user.click(await within(drawer).findByRole('button', { name: 'Stop waiting' }))
    expect(
      within(drawer).getByText('Stopped waiting. If your wallet still shows the request, reject it there.'),
    ).toBeInTheDocument()
    expect(within(drawer).getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('says so when the network never receives a transfer, and checks again on request', async () => {
    // Each check finds nothing, a minute apart, until the transfer turns up.
    const realNow = Date.now.bind(Date)
    let clock = 0
    vi.spyOn(Date, 'now').mockImplementation(() => realNow() + clock)
    let arrived = false
    stubServer({
      extra: {
        'POST /api/transactions/deposit': () => {
          if (arrived) return json(200, { status: 'confirmed', tx_hash: TX_HASH })
          clock += 60_000
          return json(202, { status: 'pending', tx_hash: TX_HASH, seen: false })
        },
      },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Add funds' }))
    const drawer = await screen.findByRole('dialog')
    await user.click(within(drawer).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    await user.type(await within(drawer).findByLabelText('Amount'), '0.5')
    await user.click(within(drawer).getByRole('button', { name: 'Send' }))
    expect(
      await within(drawer).findByText(
        "Sepolia hasn't received this transfer. If your wallet says it failed, nothing was sent and you can try again.",
      ),
    ).toBeInTheDocument()
    expect(within(drawer).getByRole('button', { name: 'Send' })).toBeEnabled()

    arrived = true
    await user.click(within(drawer).getByRole('button', { name: 'Check again' }))
    expect(await within(drawer).findByText(/Received. Your balance is up to date./)).toBeInTheDocument()
  })

  it('withdraws to the owner wallet, checking the amount against the balance', async () => {
    const { prepared } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Withdraw' }))
    const drawer = await screen.findByRole('dialog')
    await user.click(within(drawer).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    expect(await within(drawer).findByText('Owner connected')).toBeInTheDocument()

    expect(within(drawer).getByRole('combobox', { name: 'Token' })).toHaveTextContent('ETH · 1.5 available')
    expect(within(drawer).getByLabelText('Send to')).toHaveValue(WALLET)
    expect(within(drawer).getByText('Your owner wallet.')).toBeInTheDocument()
    const amount = within(drawer).getByLabelText('Amount')
    const withdraw = within(drawer).getByRole('button', { name: 'Withdraw' })

    await user.type(amount, '2')
    expect(within(drawer).getByText('The wallet holds only 1.5 ETH.')).toBeInTheDocument()
    expect(withdraw).toBeDisabled()
    await user.click(within(drawer).getByRole('button', { name: 'Max' }))
    expect(amount).toHaveValue('1.5')
    expect(withdraw).toBeEnabled()

    await user.clear(amount)
    await user.type(amount, '0.25')
    await user.click(withdraw)
    expect(await within(drawer).findByText(/Sent. The balance above is up to date./)).toBeInTheDocument()
    expect(screen.getByText('Withdrew 0.25 ETH.')).toBeInTheDocument()
    expect(prepared).toEqual([
      { path: '/api/wallet/withdraw/prepare', body: { chain_id: SEPOLIA, token: 'eth', amount: '0.25', to: WALLET } },
    ])
  })

  it('asks before withdrawing to an address that is not the owner', async () => {
    const other = '0x3333333333333333333333333333333333333333'
    const { prepared } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Withdraw' }))
    const drawer = await screen.findByRole('dialog')
    await user.click(within(drawer).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    await within(drawer).findByText('Owner connected')

    await user.click(within(drawer).getByRole('combobox', { name: 'Token' }))
    await user.click(await screen.findByRole('option', { name: 'USDC · 25 available' }))
    await user.type(within(drawer).getByLabelText('Amount'), '10')
    expect(within(drawer).getByRole('button', { name: 'Max' })).toBeInTheDocument()
    const to = within(drawer).getByLabelText('Send to')
    await user.clear(to)
    await user.type(to, 'not an address')
    expect(within(drawer).getByText('Enter a full address starting with 0x.')).toBeInTheDocument()
    await user.clear(to)
    await user.type(to, other)
    expect(within(drawer).getByText(/Not your owner wallet./)).toBeInTheDocument()

    await user.click(within(drawer).getByRole('button', { name: 'Withdraw' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent("which isn't your owner wallet")
    await user.click(within(confirm).getByRole('button', { name: 'Withdraw' }))
    expect(await screen.findByText('Withdrew 10 USDC.')).toBeInTheDocument()
    expect(prepared).toEqual([
      // An ERC-20 goes by its address, so a token the user added withdraws the same way.
      { path: '/api/wallet/withdraw/prepare', body: { chain_id: SEPOLIA, token: USDC, amount: '10', to: other } },
    ])
  })

  it('adds a token by its address after showing what it is, and marks it as never limited', async () => {
    const PEPE = '0x6982508145454Ce325dDbE47a25d4ec3d2311933'
    const FAKE_USDC = '0x9999999999999999999999999999999999999999'
    const NOT_A_TOKEN = '0x8888888888888888888888888888888888888888'
    const pepe = { chain_id: SEPOLIA, address: PEPE, ticker: 'pepe', symbol: 'PEPE', name: 'Pepe', decimals: 18, balance_raw: '5000000000000000000', listed: false }
    const checkAgain =
      "Mitfah couldn't read a symbol and decimals from this contract, so this doesn't look like an ERC-20 token on Sepolia. Check the token address again, and that it's the token's address on Sepolia."

    const withPepe = makeWalletState({
      balances: [
        ...makeWalletState().balances,
        { ticker: 'pepe', address: PEPE, native: false, custom: true, name: 'Pepe', decimals: 18, raw: pepe.balance_raw, amount: 5 },
      ],
    })
    const { requests } = stubServer({
      wallets: { [SEPOLIA]: [makeWalletState(), withPepe] },
      extra: {
        'POST /api/tokens/custom/lookup': body => {
          const { address } = body as { address: string }
          if (address === NOT_A_TOKEN) return json(400, { detail: checkAgain })
          if (address === FAKE_USDC) {
            return json(400, { detail: 'This token calls itself USDC, the same as a token Mitfah already lists on Sepolia.' })
          }
          return json(200, pepe)
        },
        'POST /api/tokens/custom': () => json(201, pepe),
      },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Add token' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add a token' })
    const field = within(dialog).getByLabelText('Token contract address')
    const next = within(dialog).getByRole('button', { name: 'Continue' })
    expect(next).toBeDisabled()

    await user.type(field, '0x1234')
    expect(within(dialog).getByText('An address is 0x and 40 more characters. This one has 4.')).toBeInTheDocument()
    // An address that doesn't answer with a symbol and decimals: a warning to check it again.
    await user.clear(field)
    await user.type(field, NOT_A_TOKEN)
    await user.click(next)
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Check the token address again')
    expect(screen.queryByRole('dialog', { name: /^Add .+\?$/ })).toBeNull()

    await user.clear(field)
    // Editing the address clears the warning.
    expect(within(dialog).queryByRole('alert')).toBeNull()
    await user.type(field, FAKE_USDC)
    await user.click(next)
    // The server's reason, shown as a warning; nothing was added.
    expect(await within(dialog).findByText(/calls itself USDC/)).toBeInTheDocument()

    await user.clear(field)
    await user.type(field, PEPE.toLowerCase())
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))
    const preview = await screen.findByRole('dialog', { name: 'Add PEPE?' })
    // What makes it an ERC-20, shown before anything is added.
    expect(within(preview).getByText('ERC-20 token')).toBeInTheDocument()
    expect(within(preview).getByText('Symbol').nextElementSibling).toHaveTextContent('PEPE')
    expect(within(preview).getByText('Decimals').nextElementSibling).toHaveTextContent('18')
    expect(preview).toHaveTextContent('Pepe')
    expect(preview).toHaveTextContent('5 PEPE')
    expect(preview).toHaveTextContent("Your spending limit can't cover PEPE.")
    expect(preview).toHaveTextContent('Buying it counts the full amount you pay.')
    // An unpriced token can't count, so there is nothing to tick.
    expect(within(preview).queryByRole('checkbox')).toBeNull()
    await user.click(within(preview).getByRole('button', { name: 'Add token' }))

    expect(await screen.findByText('PEPE added. It now shows in your balances.')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const row = (await screen.findByRole('rowheader', { name: /^PEPE/ })).closest('tr')!
    expect(row).toHaveTextContent('No price')
    expect(row).toHaveTextContent('Not limited')
    expect(row).toHaveTextContent('5')
    expect(screen.getByText('Tokens marked "No price" can never count toward your limit: Mitfah has no price for them.')).toBeInTheDocument()
    // Looked up by its checksummed address; added once.
    expect(requests).toEqual([
      { route: 'POST /api/tokens/custom/lookup', body: { chain_id: SEPOLIA, address: NOT_A_TOKEN } },
      { route: 'POST /api/tokens/custom/lookup', body: { chain_id: SEPOLIA, address: FAKE_USDC } },
      { route: 'POST /api/tokens/custom/lookup', body: { chain_id: SEPOLIA, address: PEPE } },
      { route: 'POST /api/tokens/custom', body: { chain_id: SEPOLIA, address: PEPE } },
    ])
  })

  it('shows LP tokens with what they hold, and lets the owner withdraw them', async () => {
    const POOL = '0x4444444444444444444444444444444444444444'
    const lp = {
      ticker: 'eth/usdc lp', address: POOL, native: false, custom: false, always_counted: false, lp: true,
      decimals: 18, raw: '10000000000000000000', amount: 10,
      underlying: [
        { ticker: 'eth', decimals: 18, raw: '40000000000000000000' },
        { ticker: 'usdc', decimals: 6, raw: '100000000000' },
      ],
    }
    const { prepared } = stubServer({
      wallets: { [SEPOLIA]: [makeWalletState({ balances: [...makeWalletState().balances, lp] })] },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    const row = (await screen.findByRole('rowheader', { name: 'ETH/USDC LP' })).closest('tr')!
    expect(row).toHaveTextContent('10')
    expect(row).toHaveTextContent('≈ 40 ETH + 100,000 USDC')
    // It comes and goes with the deposits, and the assistant can't send it anywhere.
    expect(row).not.toHaveTextContent('Not limited')
    expect(within(row).queryByRole('button', { name: /^Remove/ })).toBeNull()
    expect(
      screen.getByText('LP tokens are your share of an exchange pool. Under each is what it holds for you right now.'),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Withdraw' }))
    const drawer = await screen.findByRole('dialog')
    await user.click(within(drawer).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    await within(drawer).findByText('Owner connected')
    await user.click(within(drawer).getByRole('combobox', { name: 'Token' }))
    await user.click(await screen.findByRole('option', { name: 'ETH/USDC LP · 10 available' }))
    await user.type(within(drawer).getByLabelText('Amount'), '4')
    await user.click(within(drawer).getByRole('button', { name: 'Withdraw' }))
    expect(await screen.findByText('Withdrew 4 ETH/USDC LP.')).toBeInTheDocument()
    expect(prepared).toEqual([
      { path: '/api/wallet/withdraw/prepare', body: { chain_id: SEPOLIA, token: POOL, amount: '4', to: WALLET } },
    ])
  })

  it('removes a token the user added, after saying the tokens stay in the wallet', async () => {
    const PEPE = '0x6982508145454Ce325dDbE47a25d4ec3d2311933'
    const withPepe = makeWalletState({
      balances: [
        ...makeWalletState().balances,
        { ticker: 'pepe', address: PEPE, native: false, custom: true, name: 'Pepe', decimals: 18, raw: '0', amount: 0 },
      ],
    })
    const { requests } = stubServer({
      wallets: { [SEPOLIA]: [withPepe, makeWalletState()] },
      extra: { [`DELETE /api/tokens/custom/${SEPOLIA}/${PEPE}`]: () => json(200, { status: 'deleted' }) },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    // Every token can come off the dashboard except ETH and WETH, which always count.
    expect(screen.getAllByRole('button', { name: /from your dashboard$/ }).map(b => b.getAttribute('aria-label'))).toEqual([
      'Remove USDC from your dashboard',
      'Remove PEPE from your dashboard',
    ])
    await user.click(screen.getByRole('button', { name: 'Remove PEPE from your dashboard' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Remove PEPE?' })
    expect(confirm).toHaveTextContent('Any PEPE in the wallet stays there')
    expect(confirm).not.toHaveTextContent('Step 1 of 2')
    await user.click(within(confirm).getByRole('button', { name: 'Remove' }))

    expect(await screen.findByText('PEPE removed from your dashboard.')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('rowheader', { name: /^PEPE/ })).toBeNull())
    expect(requests).toEqual([{ route: `DELETE /api/tokens/custom/${SEPOLIA}/${PEPE}`, body: undefined }])
  })

  it('adds a token Mitfah lists and counts it toward the limit', async () => {
    const USDT = '0xaA8E23Fb1079EA71e0a56F48a2aA51851D8433D0'
    const usdt = { chain_id: SEPOLIA, address: USDT, ticker: 'usdt', symbol: 'USDT', name: 'Tether USD', decimals: 6, balance_raw: '0', listed: true }
    const withUsdt = makeWalletState({
      spending: { ...makeWalletState().spending, watched_tokens: [{ ticker: 'usdc', address: USDC }, { ticker: 'usdt', address: USDT }] },
      balances: [...makeWalletState().balances, { ticker: 'usdt', address: USDT, native: false, decimals: 6, raw: '0', amount: 0 }],
    })
    const { requests, prepared } = stubServer({
      wallets: { [SEPOLIA]: [makeWalletState(), withUsdt] },
      extra: {
        'POST /api/tokens/custom/lookup': () => json(200, usdt),
        'POST /api/tokens/custom': () => json(201, usdt),
      },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Add token' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add a token' })
    await user.type(within(dialog).getByLabelText('Token contract address'), USDT)
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))
    const preview = await screen.findByRole('dialog', { name: 'Add USDT?' })
    expect(preview).toHaveTextContent('Pricing is available for USDT.')
    expect(preview).not.toHaveTextContent("Your spending limit can't cover USDT.")
    // Counting needs the owner's signature, so it waits for the owner wallet.
    const countIt = within(preview).getByRole('checkbox', { name: 'Count USDT toward my spending limit' })
    expect(countIt).toBeDisabled()
    expect(countIt).not.toBeChecked()
    await user.click(within(preview).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    await waitFor(() => expect(countIt).toBeChecked())
    expect(countIt).toBeEnabled()
    await user.click(within(preview).getByRole('button', { name: 'Add token' }))

    expect(await screen.findByText('USDT added. It now counts toward your limit.')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const row = (await screen.findByRole('rowheader', { name: /^USDT/ })).closest('tr')!
    expect(row).not.toHaveTextContent('Not limited')
    expect(row).not.toHaveTextContent('No price')
    expect(requests.map(r => r.route)).toEqual(['POST /api/tokens/custom/lookup', 'POST /api/tokens/custom'])
    expect(prepared).toEqual([
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'usdt', action: 'add' } },
    ])
  })

  it('stops counting a token before removing it from the dashboard', async () => {
    const notCounted = makeWalletState({ spending: { ...makeWalletState().spending, watched_tokens: [] } })
    const removed = makeWalletState({
      spending: notCounted.spending,
      balances: makeWalletState().balances.filter(b => b.ticker !== 'usdc'),
    })
    const { requests, prepared } = stubServer({
      wallets: { [SEPOLIA]: [makeWalletState(), notCounted, removed] },
      extra: { [`DELETE /api/tokens/custom/${SEPOLIA}/${USDC}`]: () => json(200, { status: 'deleted' }) },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Remove USDC from your dashboard' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Remove USDC?' })
    expect(dialog).toHaveTextContent('Step 1 of 2: stop counting USDC')
    expect(dialog).toHaveTextContent('move USDC out of this wallet without any limit')
    expect(within(dialog).queryByRole('button', { name: 'Remove' })).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    await within(dialog).findByText('Owner connected')
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting USDC' }))

    expect(await screen.findByText('USDC no longer counts toward your limit.')).toBeInTheDocument()
    expect(await within(dialog).findByText(/Step 2 of 2: remove it from your dashboard/)).toBeInTheDocument()
    expect(requests).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Remove' }))

    expect(await screen.findByText('USDC removed from your dashboard.')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('rowheader', { name: /^USDC/ })).toBeNull())
    expect(prepared).toEqual([
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'usdc', action: 'remove' } },
    ])
    expect(requests).toEqual([{ route: `DELETE /api/tokens/custom/${SEPOLIA}/${USDC}`, body: undefined }])
  })

  it('lets go of a wallet that never answers, so the dialog opens clean again', async () => {
    // Nobody answers the wallet's prompt. Cancelling used to leave the spinner for good.
    stubServer({ walletPrompt: new Promise(() => {}) })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    let dialog = await openRemoveUsdc(user)
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting USDC' }))
    expect(await within(dialog).findByText('Confirm in your wallet.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())

    dialog = await openRemoveUsdc(user, { connect: false })
    expect(within(dialog).queryByText('Confirm in your wallet.')).toBeNull()
    const stop = within(dialog).getByRole('button', { name: 'Stop counting USDC' })
    expect(stop).toBeEnabled()

    // Without closing the dialog, "Stop waiting" frees the button where it is.
    await user.click(stop)
    await user.click(await within(dialog).findByRole('button', { name: 'Stop waiting' }))
    expect(
      within(dialog).getByText('Stopped waiting. If your wallet still shows the request, reject it there.'),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Stop counting USDC' })).toBeEnabled()
    expect(sessionStorage.getItem(pendingKey(SEPOLIA))).toBeNull()
  })

  it('still finishes a change the wallet sends after the page stopped waiting', async () => {
    let approve!: () => void
    stubServer({ walletPrompt: new Promise(resolve => (approve = resolve)) })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    const dialog = await openRemoveUsdc(user)
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting USDC' }))
    await user.click(await within(dialog).findByRole('button', { name: 'Stop waiting' }))
    expect(within(dialog).getByText(/^Stopped waiting/)).toBeInTheDocument()

    approve()
    expect(await screen.findByText('USDC no longer counts toward your limit.')).toBeInTheDocument()
    expect(sessionStorage.getItem(pendingKey(SEPOLIA))).toBeNull()
  })

  it('keeps a change waiting on one network from holding up another', async () => {
    // Sent, and the network never answers: Sepolia keeps waiting, BNB Smart Chain is free.
    stubServer({ walletChains: [SEPOLIA, BSC], confirm: () => new Promise<Response>(() => {}) })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    let dialog = await openRemoveUsdc(user)
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting USDC' }))
    expect(await within(dialog).findByText('Waiting for Sepolia to confirm…')).toBeInTheDocument()
    // A sent transaction keeps being confirmed after the dialog closes.
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(sessionStorage.getItem(pendingKey(SEPOLIA))).toContain(TX_HASH)

    await user.click(screen.getByRole('button', { name: 'Network: Sepolia' }))
    await user.click(screen.getByRole('menuitem', { name: 'BNB Smart Chain' }))
    expect(await screen.findByText('Your Mitfah smart wallet on BNB Smart Chain')).toBeInTheDocument()
    dialog = await openRemoveUsdc(user, { connect: false })
    expect(within(dialog).queryByText(/Waiting for/)).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Stop counting USDC' })).toBeEnabled()
    expect(sessionStorage.getItem(pendingKey(BSC))).toBeNull()
  })

  it('says so when the network never receives the transaction, and checks again on request', async () => {
    // Each check finds nothing, a minute apart, until the transaction turns up.
    const realNow = Date.now.bind(Date)
    let clock = 0
    vi.spyOn(Date, 'now').mockImplementation(() => realNow() + clock)
    let arrived = false
    stubServer({
      confirm: () => {
        if (arrived) return json(200, { status: 'confirmed', tx_hash: TX_HASH })
        clock += 60_000
        return json(202, { status: 'pending', tx_hash: TX_HASH, seen: false })
      },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    const dialog = await openRemoveUsdc(user)
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting USDC' }))
    expect(
      await within(dialog).findByText(
        "Sepolia hasn't received this transaction. If your wallet says it failed, nothing changed and you can try again.",
      ),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Stop counting USDC' })).toBeEnabled()

    arrived = true
    await user.click(within(dialog).getByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('USDC no longer counts toward your limit.')).toBeInTheDocument()
  })

  it('pauses from the dashboard', async () => {
    const { prepared } = stubServer({
      wallets: { [SEPOLIA]: [makeWalletState(), makeWalletState({ paused: true })] },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/dashboard')

    await walletHeader()
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))
    const modal = await screen.findByRole('dialog')
    expect(within(modal).getByText('Pause this wallet?')).toBeInTheDocument()
    expect(within(modal).getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
    await user.click(within(modal).getByRole('button', { name: 'Connect wallet' }))
    await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
    await within(modal).findByText('Owner connected')
    await user.click(within(modal).getByRole('button', { name: 'Pause wallet' }))

    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('Pause this wallet?')).toBeNull())
    expect(prepared).toEqual([{ path: '/api/wallet/pause/prepare', body: { chain_id: SEPOLIA } }])
    expect(await screen.findByRole('button', { name: 'Unpause' })).toBeInTheDocument()
    expect(screen.getByText('Paused')).toBeInTheDocument()
  })
})
