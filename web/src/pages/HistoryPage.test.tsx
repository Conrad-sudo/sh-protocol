import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { Contact, Transaction } from '../api/types'
import { SYNC_POLL_MS } from '../hooks/useTransactions'
import { formatDateTime } from '../lib/format'
import { startOfDay } from '../lib/historyFilters'
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
    direction: 'out',
    ...fields,
  }
}

interface ServerOptions {
  transactions?: Transaction[]
  walletChains?: number[]
  /** GET /api/transactions fails this many times first. */
  failures?: number
  /** The first reads of page one say a search for outside activity is running... */
  syncingReads?: number
  /** ...and once it stops, these have been found. */
  found?: Transaction[]
  /** What GET /api/transactions/tokens answers. */
  tokens?: string[]
}

/** Where a row sits in the server's order, as app/db.py sorts it: by time, then id. */
const sortKey = (t: Transaction): [number, number] => [t.mined_at ?? t.created_at, t.id]
const older = (a: [number, number], b: [number, number]) => a[0] < b[0] || (a[0] === b[0] && a[1] < b[1])

/**
 * A signed-in account whose History answers like app/api.py: newest first by time, paged by a
 * "<time>-<id>" cursor, filterable, and saying while a search for outside activity runs. Of the
 * History tab's filters it applies the group, and the token as a word in the action; the tests
 * check the rest by what is asked for.
 */
function stubServer({
  transactions = [],
  walletChains = [SEPOLIA, BSC, ARBITRUM],
  failures = 0,
  syncingReads = 0,
  found = [],
  tokens = [],
}: ServerOptions = {}) {
  const reads: URLSearchParams[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, { ...ME, owner_addr: WALLET, wallet_chains: walletChains }))
      if (url === '/api/chains') return Promise.resolve(json(200, { chains: CHAINS }))
      if (url === '/api/contacts') return Promise.resolve(json(200, { contacts: [SAM] }))
      if (url.startsWith('/api/transactions/tokens')) return Promise.resolve(json(200, { tokens }))
      if (url.startsWith('/api/transactions?')) {
        const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))
        reads.push(params)
        if (failures-- > 0) return Promise.resolve(new Response('Internal Server Error', { status: 500 }))
        const chain = params.get('chain_id')
        const before = params.get('before')
        const limit = Number(params.get('limit'))
        let syncing = false
        if (before === null) {
          syncing = syncingReads-- > 0
          if (!syncing && found.length) transactions = [...transactions, ...found.splice(0)]
        }
        const cursor = before === null ? null : (before.split('-').map(Number) as [number, number])
        const direction = params.get('direction')
        const token = params.get('token')
        const rows = transactions
          .filter(t => chain === null || t.chain_id === Number(chain))
          .filter(t => direction === null || t.direction === direction)
          .filter(t => token === null || t.action.split(' ').includes(token))
          .filter(t => cursor === null || older(sortKey(t), cursor))
          .sort((a, b) => (older(sortKey(a), sortKey(b)) ? 1 : -1))
        const page = rows.slice(0, limit)
        const last = page.at(-1)
        const next = rows.length > limit && last ? sortKey(last).join('-') : null
        return Promise.resolve(json(200, { transactions: page, next_before: next, syncing }))
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

  it('marks what happened outside Mitfah, in its place in time', async () => {
    stubServer({
      transactions: [
        tx(1),
        // Recorded last (the highest id), but it happened between the other two.
        tx(9, { source: 'outside', action: `Received 100 USDC from ${SAM.address}`, created_at: T0 + 9_000, mined_at: T0 + 90 }),
        tx(2),
      ],
    })
    await renderRoutes(routes, '/history')

    const [newest, outside, oldest] = await rows()
    expect(newest).toHaveTextContent('Transaction 2')
    expect(outside).toHaveTextContent('Received 100 USDC from sam')
    expect(outside).toHaveTextContent('Outside Mitfah')
    expect(outside).not.toHaveTextContent('By you')
    expect(oldest).toHaveTextContent('Transaction 1')
  })

  it('says while it checks for activity outside Mitfah, and looks again until it is done', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const { reads } = stubServer({
        transactions: [tx(1)],
        syncingReads: 2,
        found: [tx(5, { source: 'outside', action: 'Received 0.1 ETH from your owner address' })],
      })
      await renderRoutes(routes, '/history')

      expect(await rows()).toHaveLength(1)
      expect(screen.getByRole('status')).toHaveTextContent('Checking for activity outside Mitfah…')
      await act(() => vi.advanceTimersByTimeAsync(SYNC_POLL_MS))
      await act(() => vi.advanceTimersByTimeAsync(SYNC_POLL_MS))
      await waitFor(async () => expect(await rows()).toHaveLength(2))
      expect((await rows())[0]).toHaveTextContent('Outside Mitfah')
      expect(screen.queryByText('Checking for activity outside Mitfah…')).toBeNull()

      // Done, and nothing pending: no more reads.
      const settled = reads.length
      await act(() => vi.advanceTimersByTimeAsync(SYNC_POLL_MS * 6))
      expect(reads.length).toBe(settled)
    } finally {
      vi.useRealTimers()
    }
  })

  it('checks for outside activity with nothing listed yet', async () => {
    stubServer({ syncingReads: 1 })
    await renderRoutes(routes, '/history')

    expect(await screen.findByRole('heading', { name: 'No transactions yet' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Checking for activity outside Mitfah…')
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
    expect(reads.at(-1)!.get('before')).toBe(`${T0 + 6 * 60 + 12}-6`)
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

  it('marks which way each transaction went', async () => {
    stubServer({
      transactions: [
        tx(1, { direction: 'in', action: 'Received 1 USDC' }),
        tx(2, { direction: 'out', action: 'Sent 1 USDC' }),
        tx(3, { direction: 'none', action: 'Pause the wallet' }),
        tx(4, { direction: null, action: 'Something still being read' }),
      ],
    })
    await renderRoutes(routes, '/history')

    const [unknown, change, sent, received] = await rows()
    expect(within(received).getByTitle('Incoming')).toHaveTextContent('Incoming:')
    expect(within(sent).getByTitle('Outgoing')).toBeInTheDocument()
    expect(within(change).getByTitle('Wallet change')).toBeInTheDocument()
    expect(unknown.querySelector('.mf-tx-dir')).toBeNull()
  })

  it('splits the list into incoming, outgoing and wallet changes', async () => {
    const { reads } = stubServer({
      transactions: [
        tx(1, { direction: 'in', action: 'Received 1 USDC' }),
        tx(2, { direction: 'out', action: 'Sent 1 USDC' }),
        tx(3, { direction: 'none', action: 'Pause the wallet' }),
      ],
    })
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/history')

    expect(await rows()).toHaveLength(3)
    expect(reads[0].get('direction')).toBeNull()
    const groups = screen.getByRole('radiogroup', { name: 'Transactions to show' })

    await user.click(within(groups).getByRole('radio', { name: 'Incoming' }))
    await waitFor(async () => expect(await rows()).toHaveLength(1))
    expect((await rows())[0]).toHaveTextContent('Received 1 USDC')
    expect(reads.at(-1)!.get('direction')).toBe('in')
    // Kept in the page's address, so a refresh shows the same tab.
    expect(router.state.location.search).toBe('?type=in')

    await user.click(within(groups).getByRole('radio', { name: 'Outgoing' }))
    await waitFor(async () => expect((await rows())[0]).toHaveTextContent('Sent 1 USDC'))
    await user.click(within(groups).getByRole('radio', { name: 'Wallet changes' }))
    await waitFor(async () => expect((await rows())[0]).toHaveTextContent('Pause the wallet'))
    expect(reads.at(-1)!.get('direction')).toBe('none')
  })

  it('says when a tab has nothing yet', async () => {
    stubServer({ transactions: [tx(1, { direction: 'out' })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/history')

    await rows()
    await user.click(screen.getByRole('radio', { name: 'Incoming' }))
    expect(await screen.findByRole('heading', { name: 'No incoming transactions yet' })).toBeInTheDocument()
  })

  it('filters by date, amount, address and token, and shows each filter so it can be taken off', async () => {
    const { reads } = stubServer({ transactions: [tx(1, { action: 'Sent 5 USDC' })], tokens: ['ETH', 'USDC'] })
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/history')
    await rows()

    const filter = screen.getByRole('button', { name: 'Filter' })
    expect(filter).toHaveAttribute('aria-expanded', 'false')
    await user.click(filter)
    const panel = screen.getByRole('form', { name: 'Filter transactions' })
    expect(filter).toHaveAttribute('aria-expanded', 'true')
    fireEvent.change(within(panel).getByLabelText('From'), { target: { value: '2026-10-01' } })
    fireEvent.change(within(panel).getByLabelText('To'), { target: { value: '2026-10-03' } })
    await user.type(within(panel).getByLabelText('At least'), '5')
    await user.type(within(panel).getByLabelText('At most'), '10.5')
    await user.type(within(panel).getByLabelText('Address or contact'), 'Sam')
    await user.click(within(panel).getByRole('combobox', { name: 'Token' }))
    await user.click(await screen.findByRole('option', { name: 'USDC' }))
    await user.click(within(panel).getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(reads.at(-1)!.get('token')).toBe('USDC'))
    const read = reads.at(-1)!
    // Whole days in the reader's own time zone: from the start of the first to the end of the last.
    expect(read.get('since')).toBe(String(startOfDay('2026-10-01')))
    expect(read.get('until')).toBe(String(startOfDay('2026-10-04')))
    expect(read.get('min_amount')).toBe('5')
    expect(read.get('max_amount')).toBe('10.5')
    // A contact's name is sent as their address.
    expect(read.get('address')).toBe(SAM.address)
    expect(screen.queryByRole('form', { name: 'Filter transactions' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Filter (6 on)' })).toBeInTheDocument()
    expect(router.state.location.search).toContain('token=USDC')

    const chips = screen.getByRole('list', { name: 'Filters in use' })
    expect(chips).toHaveTextContent('From Oct 1, 2026')
    expect(chips).toHaveTextContent('To Oct 3, 2026')
    expect(chips).toHaveTextContent('At least 5')
    expect(chips).toHaveTextContent('With sam')
    await user.click(within(chips).getByRole('button', { name: 'Remove filter: USDC' }))
    await waitFor(() => expect(reads.at(-1)!.get('token')).toBeNull())
    expect(reads.at(-1)!.get('min_amount')).toBe('5')

    await user.click(within(chips).getByRole('button', { name: 'Clear all' }))
    await waitFor(() => expect(reads.at(-1)!.get('since')).toBeNull())
    expect(screen.queryByRole('list', { name: 'Filters in use' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Filter' })).toBeInTheDocument()
  })

  it("explains what's wrong before filtering, and sends nothing until it's fixed", async () => {
    const { reads } = stubServer({ transactions: [tx(1)] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/history')
    await rows()

    await user.click(screen.getByRole('button', { name: 'Filter' }))
    const panel = screen.getByRole('form', { name: 'Filter transactions' })
    fireEvent.change(within(panel).getByLabelText('From'), { target: { value: '2026-10-05' } })
    fireEvent.change(within(panel).getByLabelText('To'), { target: { value: '2026-10-01' } })
    await user.type(within(panel).getByLabelText('At least'), '10')
    await user.type(within(panel).getByLabelText('At most'), '2')
    await user.type(within(panel).getByLabelText('Address or contact'), 'nobody')
    const before = reads.length
    await user.click(within(panel).getByRole('button', { name: 'Apply' }))

    expect(within(panel).getByText('The end date is before the start date.')).toBeInTheDocument()
    expect(within(panel).getByText('The lowest amount is more than the highest.')).toBeInTheDocument()
    expect(within(panel).getByText("Enter a full address starting with 0x, or one of your contacts' names.")).toBeInTheDocument()
    expect(reads.length).toBe(before)

    await user.clear(within(panel).getByLabelText('Address or contact'))
    await user.type(within(panel).getByLabelText('Address or contact'), STRANGER)
    expect(within(panel).queryByText(/one of your contacts' names/)).toBeNull()
  })

  it('says when nothing matches, and clears the filters on request', async () => {
    const { reads } = stubServer({ transactions: [tx(1, { action: 'Sent 5 USDC' })], tokens: ['ETH', 'USDC'] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/history?token=ETH')

    expect(await screen.findByRole('heading', { name: 'No transactions match these filters' })).toBeInTheDocument()
    expect(reads[0].get('token')).toBe('ETH')
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(await rows()).toHaveLength(1)
    expect(reads.at(-1)!.get('token')).toBeNull()
  })

  it('opens with the tab and filters in the address', async () => {
    const { reads } = stubServer({ transactions: [tx(1, { action: 'Sent 5 USDC' })] })
    await renderRoutes(routes, `/history?type=out&token=USDC&address=${SAM.address.toLowerCase()}&min=junk`)

    await rows()
    expect(reads[0].get('direction')).toBe('out')
    expect(reads[0].get('token')).toBe('USDC')
    expect(reads[0].get('address')).toBe(SAM.address)
    // Something malformed in the address is left out rather than sent.
    expect(reads[0].get('min_amount')).toBeNull()
    expect(screen.getByRole('radio', { name: 'Outgoing' })).toBeChecked()
    expect(screen.getByRole('list', { name: 'Filters in use' })).toHaveTextContent('With sam')
  })

  it('offers only dates on the wallet-changes tab, and drops the money filters there', async () => {
    const { reads } = stubServer({ transactions: [tx(1, { direction: 'none', action: 'Pause the wallet' })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/history?token=USDC&from=2026-10-01')

    await screen.findByRole('list', { name: 'Filters in use' })
    await user.click(screen.getByRole('radio', { name: 'Wallet changes' }))
    await waitFor(() => expect(reads.at(-1)!.get('direction')).toBe('none'))
    expect(reads.at(-1)!.get('token')).toBeNull()
    expect(reads.at(-1)!.get('since')).toBe(String(startOfDay('2026-10-01')))

    await user.click(screen.getByRole('button', { name: /^Filter/ }))
    const panel = screen.getByRole('form', { name: 'Filter transactions' })
    expect(within(panel).getByLabelText('From')).toHaveValue('2026-10-01')
    expect(within(panel).queryByLabelText('At least')).toBeNull()
    expect(within(panel).queryByRole('combobox', { name: 'Token' })).toBeNull()
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
