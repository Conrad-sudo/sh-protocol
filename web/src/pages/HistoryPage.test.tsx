import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { Contact, Transaction } from '../api/types'
import { formatDateTime } from '../lib/format'
import { routes } from '../routes'
import { SEPOLIA } from '../test/fixtures'
import { json, ME, renderRoutes, setViewportWidth, TOKEN, WALLET } from '../test/utils'

const BSC = 56
const ARBITRUM = 42161
// Sepolia is served from a local fork here: its links must still go to the live explorer.
const CHAINS = [
  { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: true },
  { chain_id: BSC, name: 'bsc', native_ticker: 'BNB', fork: false },
  { chain_id: ARBITRUM, name: 'arbitrum', native_ticker: 'ETH', fork: false },
]
const SAM: Contact = { name: 'sam', address: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8' }
const STRANGER = '0x1234567890abcdef1234567890abcdef12345678'

const hash = (n: number) => `0x${n.toString(16).padStart(64, '0')}`
const T0 = 1_790_000_000

function tx(id: number, fields: Partial<Transaction> = {}): Transaction {
  return {
    id,
    chain_id: SEPOLIA,
    source: 'assistant',
    action: `Transaction ${id}`,
    status: 'confirmed',
    tx_hash: hash(id),
    created_at: T0 + id * 60,
    mined_at: T0 + id * 60 + 12,
    ...fields,
  }
}

interface ServerOptions {
  transactions?: Transaction[]
  walletChains?: number[]
  /** GET /api/transactions fails this many times first. */
  failures?: number
}

/** A signed-in account whose History answers like app/api.py: newest first, paged, filterable. */
function stubServer({ transactions = [], walletChains = [SEPOLIA, BSC, ARBITRUM], failures = 0 }: ServerOptions = {}) {
  const reads: URLSearchParams[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, { ...ME, owner_addr: WALLET, wallet_chains: walletChains }))
      if (url === '/api/chains') return Promise.resolve(json(200, { chains: CHAINS }))
      if (url === '/api/contacts') return Promise.resolve(json(200, { contacts: [SAM] }))
      if (url.startsWith('/api/transactions?')) {
        const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))
        reads.push(params)
        if (failures-- > 0) return Promise.resolve(new Response('Internal Server Error', { status: 500 }))
        const chain = params.get('chain_id')
        const before = params.get('before')
        const limit = Number(params.get('limit'))
        const rows = transactions
          .filter(t => chain === null || t.chain_id === Number(chain))
          .filter(t => before === null || t.id < Number(before))
          .sort((a, b) => b.id - a.id)
        const page = rows.slice(0, limit)
        return Promise.resolve(
          json(200, { transactions: page, next_before: rows.length > limit ? page.at(-1)!.id : null }),
        )
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { reads }
}

const list = () => screen.findByRole('list', { name: 'Transactions' })
const rows = async () => within(await list()).getAllByRole('listitem')

describe('HistoryPage', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("lists every transaction newest first, with its date, time, network and who sent it", async () => {
    stubServer({
      transactions: [
        tx(1, { source: 'owner', action: 'Create your Mitfah smart wallet' }),
        tx(2, { chain_id: BSC, source: 'owner', action: 'Withdraw 0.5 BNB to your owner address', status: 'failed' }),
        tx(3, { action: `Transfer 5 USDC to ${SAM.address}` }),
      ],
    })
    await renderRoutes(routes, '/history')

    expect(await screen.findByRole('heading', { level: 1, name: 'History' })).toBeInTheDocument()
    const [newest, middle, oldest] = await rows()
    expect(newest).toHaveTextContent('Transfer 5 USDC to sam')
    expect(newest).toHaveTextContent('By your assistant')
    expect(newest).toHaveTextContent('Sepolia')
    expect(newest).toHaveTextContent('Confirmed')
    const when = within(newest).getByText(formatDateTime((T0 + 3 * 60 + 12) * 1_000))
    expect(when.tagName).toBe('TIME')
    expect(when).toHaveAttribute('dateTime', new Date((T0 + 3 * 60 + 12) * 1_000).toISOString())

    expect(middle).toHaveTextContent('Withdraw 0.5 BNB to your owner address')
    expect(middle).toHaveTextContent('BNB Smart Chain')
    expect(middle).toHaveTextContent('By you')
    expect(middle).toHaveTextContent('Failed')
    expect(oldest).toHaveTextContent('Create your Mitfah smart wallet')
  })

  it("links each hash to its network's live explorer, a local fork's included", async () => {
    stubServer({
      transactions: [
        tx(1),
        tx(2, { chain_id: BSC, source: 'owner' }),
        tx(3, { chain_id: ARBITRUM }),
      ],
    })
    await renderRoutes(routes, '/history')
    await rows()

    // Sepolia is a local fork on this server; the link still goes where the live network's would.
    const sepolia = screen.getByRole('link', { name: `View transaction ${hash(1)} on Etherscan` })
    expect(sepolia).toHaveAttribute('href', `https://sepolia.etherscan.io/tx/${hash(1)}`)
    expect(sepolia).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: `View transaction ${hash(2)} on BscScan` })).toHaveAttribute(
      'href',
      `https://bscscan.com/tx/${hash(2)}`,
    )
    expect(screen.getByRole('link', { name: `View transaction ${hash(3)} on Arbiscan` })).toHaveAttribute(
      'href',
      `https://arbiscan.io/tx/${hash(3)}`,
    )
    // Each can be copied whole too.
    expect(screen.getAllByRole('button', { name: 'Copy transaction hash' })).toHaveLength(3)
  })

  it('names contacts and shortens other addresses in what a transaction did', async () => {
    stubServer({ transactions: [tx(1, { action: `Approve ${STRANGER} to spend 1 USDC; Transfer 1 USDC to ${SAM.address}` })] })
    await renderRoutes(routes, '/history')

    const [row] = await rows()
    expect(within(row).getByText('sam')).toHaveAttribute('title', SAM.address)
    expect(within(row).queryByText(SAM.address)).toBeNull()
    // Shortened on screen, whole for screen readers and on hover.
    expect(within(row).getByText('0x1234…5678')).toBeInTheDocument()
    expect(within(row).getByText(STRANGER)).toHaveClass('mf-visually-hidden')
  })

  it('shows each outcome, and no link for a transaction the network has not shown yet', async () => {
    stubServer({
      transactions: [
        tx(1, { status: 'dropped' }),
        tx(2, { status: 'pending', tx_hash: null, mined_at: null }),
      ],
    })
    await renderRoutes(routes, '/history')

    const [pending, dropped] = await rows()
    expect(pending).toHaveTextContent('Pending')
    expect(pending).toHaveTextContent('Waiting for the network')
    expect(within(pending).queryByRole('link')).toBeNull()
    expect(dropped).toHaveTextContent("Didn't go through")
  })

  it('shows older transactions on request', async () => {
    const { reads } = stubServer({ transactions: Array.from({ length: 30 }, (_, i) => tx(i + 1)) })
    const user = userEvent.setup()
    await renderRoutes(routes, '/history')

    expect(await rows()).toHaveLength(25)
    await user.click(screen.getByRole('button', { name: 'Show older transactions' }))
    await waitFor(async () => expect(await rows()).toHaveLength(30))
    expect(reads.at(-1)!.get('before')).toBe('6')
    expect(screen.queryByRole('button', { name: 'Show older transactions' })).toBeNull()
  })

  it('narrows the list to the network being viewed', async () => {
    const { reads } = stubServer({ transactions: [tx(1), tx(2, { chain_id: BSC })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/history')

    expect(await rows()).toHaveLength(2)
    expect(reads[0].get('chain_id')).toBeNull()
    const scope = screen.getByRole('radiogroup', { name: 'Networks to show' })
    await user.click(within(scope).getByRole('radio', { name: 'Sepolia' }))
    await waitFor(async () => expect(await rows()).toHaveLength(1))
    expect(reads.at(-1)!.get('chain_id')).toBe(String(SEPOLIA))
  })

  it('offers no network choice with a wallet on just one network', async () => {
    stubServer({ transactions: [tx(1)], walletChains: [SEPOLIA] })
    await renderRoutes(routes, '/history')

    await rows()
    expect(screen.queryByRole('radiogroup', { name: 'Networks to show' })).toBeNull()
  })

  it('says when there is nothing yet', async () => {
    stubServer()
    await renderRoutes(routes, '/history')

    expect(await screen.findByRole('heading', { name: 'No transactions yet' })).toBeInTheDocument()
  })

  it('explains a failed load and tries again', async () => {
    stubServer({ transactions: [tx(1)], failures: 1 })
    const user = userEvent.setup()
    await renderRoutes(routes, '/history')

    expect(await screen.findByText(/Couldn't load your transactions/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await rows()).toHaveLength(1)
  })
})
