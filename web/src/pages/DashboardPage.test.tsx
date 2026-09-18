import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { WalletState } from '../api/types'
import { routes } from '../routes'
import { makeWalletState, SEPOLIA } from '../test/fixtures'
import { answerRpc, isRpc, json, ME, renderRoutes, setViewportWidth, TOKEN, WALLET } from '../test/utils'

// Owner transactions poll with a 2 s gap; the tests don't wait.
vi.mock('../lib/tx', async importOriginal => ({
  ...(await importOriginal<typeof import('../lib/tx')>()),
  sleep: () => Promise.resolve(),
}))

const BSC = 56
const TX_HASH = `0x${'34'.repeat(32)}`
const CHAINS = [
  { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: false },
  { chain_id: BSC, name: 'bsc', native_ticker: 'BNB', fork: false },
  { chain_id: 1, name: 'mainnet', native_ticker: 'ETH', fork: false },
]

type WalletAnswer = WalletState | { status: number; detail: string }

interface ServerOptions {
  walletChains?: number[]
  /** Answers for GET /api/wallet/{chain_id}, in order; the last one repeats. */
  wallets?: Partial<Record<number, WalletAnswer[]>>
}

/** A signed-in account whose wallets answer from `wallets`. Also answers the mock wallet's RPC. */
function stubServer({ walletChains = [SEPOLIA], wallets = {} }: ServerOptions = {}) {
  const walletCalls: number[] = []
  const sent: Record<string, string>[] = []
  const prepared: { path: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (isRpc(url)) {
        return Promise.resolve(
          answerRpc(init, {
            eth_sendTransaction: ([tx]) => {
              sent.push(tx as Record<string, string>)
              return TX_HASH
            },
            eth_blockNumber: () => '0x20',
            eth_getTransactionReceipt: () => receipt(),
            eth_getTransactionByHash: () => null,
          }),
        )
      }
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, { ...ME, owner_addr: WALLET, wallet_chains: walletChains }))
      if (url === '/api/chains') return Promise.resolve(json(200, { chains: CHAINS }))
      if (url.endsWith('/prepare')) {
        prepared.push({ path: url, body: JSON.parse(String(init?.body)) })
        return Promise.resolve(json(200, { tx: { to: '0x2222222222222222222222222222222222222222', data: '0x1234' } }))
      }
      if (url === '/api/wallet/tx/confirm') {
        return Promise.resolve(json(200, { status: 'confirmed', tx_hash: TX_HASH }))
      }
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
  return { walletCalls, sent, prepared }
}

function receipt() {
  return {
    transactionHash: TX_HASH,
    transactionIndex: '0x0',
    blockHash: `0x${'56'.repeat(32)}`,
    blockNumber: '0x1f',
    from: WALLET,
    to: '0x2222222222222222222222222222222222222222',
    cumulativeGasUsed: '0x5208',
    gasUsed: '0x5208',
    effectiveGasPrice: '0x3b9aca00',
    contractAddress: null,
    logs: [],
    logsBloom: `0x${'00'.repeat(256)}`,
    status: '0x1',
    type: '0x2',
  }
}

const walletHeader = () => screen.findByText(/^Your Mitfah wallet on/)

describe('DashboardPage', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows an active wallet: status, what is left to spend, and balances', async () => {
    stubServer()
    await renderRoutes(routes, '/dashboard')

    expect(await walletHeader()).toHaveTextContent('Your Mitfah wallet on Sepolia')
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
            session: { key: null, active: false },
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

    expect(await screen.findByText('Your Mitfah wallet on BNB Smart Chain')).toBeInTheDocument()
    expect(screen.getByText('Paused')).toBeInTheDocument()
    expect(walletCalls).toEqual([SEPOLIA, BSC])
    expect(localStorage.getItem('mitfah-chain:7')).toBe(String(BSC))

    await user.click(screen.getByRole('button', { name: 'Network: BNB Smart Chain' }))
    await user.click(screen.getByRole('menuitem', { name: 'Add a network' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/wallets/new'))
  })

  it('funds the wallet from the connected browser wallet', async () => {
    const { walletCalls, sent } = stubServer()
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
      { path: '/api/wallet/withdraw/prepare', body: { chain_id: SEPOLIA, token: 'usdc', amount: '10', to: other } },
    ])
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
