import { expect, test, type Page } from '@playwright/test'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN } from './helpers.ts'

/*
 * The History tab — every transaction made through Mitfah — at real screen sizes, in both themes,
 * against a mocked API that behaves like app/api.py. Sepolia is served from a local fork here, and
 * its hashes must still link to the live explorer.
 */

const SEPOLIA = 11155111
const BSC = 56
const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const PAYEE = '0xf498C31cDC3a5421fB9ce47C4F3cc3DB0a664766'
const TOKEN_ADDRESS = '0x834D47259805D7F29e4Dcc9a3254F33E47bd0380'
const CHAINS = [
  { chain_id: SEPOLIA, name: 'sepolia-fork', native_ticker: 'ETH', fork: true },
  { chain_id: BSC, name: 'bsc', native_ticker: 'BNB', fork: false },
]
const T0 = Math.floor(Date.now() / 1000) - 86_400

const hash = (n: number) => `0x${n.toString(16).padStart(2, '0').repeat(32)}`

// What the fork e2e run actually recorded, long lines included: the assistant's descriptions carry
// whole addresses and unrounded amounts.
const TRANSACTIONS = [
  {
    id: 6, chain_id: SEPOLIA, source: 'assistant', status: 'confirmed', tx_hash: hash(6),
    action: `Approve 0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3 to spend 100.0 of ${TOKEN_ADDRESS}; Swap 100.0 of ${TOKEN_ADDRESS} for at least 9.4894769844675e-05 of the native asset`,
  },
  { id: 5, chain_id: SEPOLIA, source: 'assistant', status: 'confirmed', tx_hash: hash(5), action: `Transfer 10.0 USDC to ${PAYEE}` },
  { id: 4, chain_id: BSC, source: 'owner', status: 'failed', tx_hash: hash(4), action: 'Withdraw 0.5 BNB to your owner address' },
  { id: 3, chain_id: SEPOLIA, source: 'assistant', status: 'pending', tx_hash: null, action: `Transfer 1.0 USDC to ${PAYEE}` },
  { id: 2, chain_id: SEPOLIA, source: 'owner', status: 'confirmed', tx_hash: hash(2), action: 'Set the spending limit to $1,234' },
  { id: 1, chain_id: SEPOLIA, source: 'owner', status: 'confirmed', tx_hash: hash(1), action: 'Create your Mitfah wallet' },
].map(t => ({ ...t, created_at: T0 + t.id * 600, mined_at: t.status === 'pending' ? null : T0 + t.id * 600 + 12 }))

async function mockServer(page: Page) {
  await page.route(isApiUrl, route => {
    const url = new URL(route.request().url())
    switch (url.pathname) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: OWNER, wallet_chains: [SEPOLIA, BSC] } })
      case '/api/chains':
        return route.fulfill({ json: { chains: CHAINS } })
      case '/api/contacts':
        return route.fulfill({ json: { contacts: [{ name: 'payee', address: PAYEE }] } })
      case '/api/transactions': {
        const chain = url.searchParams.get('chain_id')
        const rows = TRANSACTIONS.filter(t => chain === null || t.chain_id === Number(chain))
        return route.fulfill({ json: { transactions: rows, next_before: null } })
      }
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
}

test('the history: every transaction, when it happened, and a link to the explorer', async ({ page }, testInfo) => {
  await mockServer(page)
  await page.goto('/history')

  await expect(page.getByRole('heading', { name: 'History', level: 1 })).toBeVisible()
  const list = page.getByRole('list', { name: 'Transactions' })
  await expect(list.getByRole('listitem')).toHaveCount(6)
  await expect(list.getByRole('listitem').nth(1)).toContainText('Transfer 10.0 USDC to payee')

  // The fork's hashes link to the live explorer, as they will in production.
  const link = page.getByRole('link', { name: `View transaction ${hash(5)} on Etherscan` })
  await expect(link).toHaveAttribute('href', `https://sepolia.etherscan.io/tx/${hash(5)}`)
  await expect(page.getByRole('link', { name: `View transaction ${hash(4)} on BscScan` })).toHaveAttribute(
    'href',
    `https://bscscan.com/tx/${hash(4)}`,
  )
  await expect(list.getByRole('listitem').nth(3)).toContainText('Waiting for the network')

  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'history')

  // The network the app is showing (Sepolia, the first wallet) narrows the list to its own.
  await page.getByRole('radiogroup', { name: 'Networks to show' }).getByText('Sepolia').click()
  await expect(list.getByRole('listitem')).toHaveCount(5)
  await expectNoSidewaysScroll(page)
})

test('the History tab is in the navigation on every screen', async ({ page }) => {
  await mockServer(page)
  await page.goto('/settings')
  const nav = page.getByRole('navigation', { name: 'Main' })

  await nav.getByRole('link', { name: 'History' }).click()
  await expect(page.getByRole('heading', { name: 'History', level: 1 })).toBeVisible()
  await expect(nav.getByRole('link', { name: 'History' })).toHaveAttribute('aria-current', 'page')
})
