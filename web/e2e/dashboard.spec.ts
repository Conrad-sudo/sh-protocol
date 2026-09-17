import { expect, test, type Page } from '@playwright/test'
import { FAKE_WALLET_NAME, installFakeWallet, type WalletTx } from './fakeWallet.ts'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN, USDC, walletState } from './helpers.ts'

/*
 * The dashboard at real screen sizes, in both themes, against a mocked API and a fake browser
 * wallet. Nothing reaches wallet.db or a chain.
 */

const SEPOLIA = 11155111
const BSC = 56
const ADDRESS = '0x2222222222222222222222222222222222222222'
const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const TX_HASH = `0x${'34'.repeat(32)}`

const RECEIPT = {
  transactionHash: TX_HASH,
  transactionIndex: '0x0',
  blockHash: `0x${'56'.repeat(32)}`,
  blockNumber: '0x1f',
  from: OWNER,
  to: ADDRESS,
  cumulativeGasUsed: '0x5208',
  gasUsed: '0x5208',
  effectiveGasPrice: '0x3b9aca00',
  contractAddress: null,
  logs: [],
  logsBloom: `0x${'00'.repeat(256)}`,
  status: '0x1',
  type: '0x2',
}

/** A signed-in account with a wallet on each chain in `wallets`, in that order. */
async function mockServer(page: Page, wallets: Map<number, object>) {
  const walletChains = [...wallets.keys()]
  const reads: number[] = []
  const sent: { tx: WalletTx; chainId: number }[] = []

  await installFakeWallet(page, {
    address: OWNER,
    chainId: SEPOLIA,
    signMessage: async () => {
      throw new Error('Nothing on the dashboard should ask for a signature.')
    },
    sendTransaction: async (tx, chainId) => {
      sent.push({ tx, chainId })
      return TX_HASH
    },
    request: async method => {
      if (method === 'eth_blockNumber') return '0x20'
      if (method === 'eth_getTransactionReceipt') return RECEIPT
      if (method === 'eth_getTransactionByHash') return null
      throw new Error(`Unexpected wallet request ${method}`)
    },
  })

  await page.route(isApiUrl, route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/wallet/')) {
      const chainId = Number(path.split('/').at(-1))
      reads.push(chainId)
      return route.fulfill({ json: wallets.get(chainId) })
    }
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: OWNER, wallet_chains: walletChains } })
      case '/api/chains':
        return route.fulfill({
          json: {
            chains: [
              { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: false },
              { chain_id: BSC, name: 'bsc', native_ticker: 'BNB', fork: false },
              { chain_id: 1, name: 'mainnet', native_ticker: 'ETH', fork: false },
            ],
          },
        })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return { reads, sent }
}

test('an active wallet', async ({ page }, testInfo) => {
  await mockServer(page, new Map([[SEPOLIA, walletState(SEPOLIA, ADDRESS)]]))
  await page.goto('/dashboard')

  await expect(page.getByText('Your Mitfah wallet on Sepolia')).toBeVisible()
  await expectTheme(page, testInfo)
  await expect(page.getByText('Active', { exact: true })).toBeVisible()
  await expect(page.getByText('Assistant on')).toBeVisible()
  await expect(page.getByRole('progressbar', { name: '60% of the limit left' })).toBeVisible()
  await expect(page.getByRole('rowheader', { name: /^WETH/ })).toContainText('Not limited')
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'dashboard-active')
})

test('a wallet that needs attention', async ({ page }, testInfo) => {
  const healthy = walletState(SEPOLIA, ADDRESS)
  const unreadableUsdc = { ticker: 'usdc', address: USDC, native: false, decimals: null, raw: null, amount: null, error: 'no code' }
  const troubled = walletState(SEPOLIA, ADDRESS, {
    paused: true,
    is_owner: false,
    session: { key: null, active: false },
    spending: { ...healthy.spending, hook_installed: false },
    balances: [healthy.balances[0], unreadableUsdc],
  })
  await mockServer(page, new Map([[SEPOLIA, troubled]]))
  await page.goto('/dashboard')

  await expect(page.getByText('Paused', { exact: true })).toBeVisible()
  await expect(page.getByText(/This wallet is paused/)).toBeVisible()
  await expect(page.getByText(/spending limit isn't switched on/)).toBeVisible()
  await expect(page.getByText(/must be signed by its owner/)).toBeVisible()
  await expect(page.getByText("Couldn't read")).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'dashboard-warnings')
})

test('adding funds from the connected wallet', async ({ page }, testInfo) => {
  const { reads, sent } = await mockServer(page, new Map([[SEPOLIA, walletState(SEPOLIA, ADDRESS)]]))
  await page.goto('/dashboard')

  await page.getByRole('button', { name: 'Add funds' }).click()
  const drawer = page.getByRole('dialog').filter({ hasText: 'Add funds' })
  await expect(drawer.getByRole('img', { name: `QR code of ${ADDRESS}` })).toBeVisible()
  await expect(drawer.getByText(ADDRESS, { exact: true })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'fund-drawer')

  await drawer.getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()
  await drawer.getByLabel('Amount').fill('0.5')
  await drawer.getByRole('button', { name: 'Send' }).click()

  await expect(drawer.getByText('Received. Your balance is up to date.')).toBeVisible()
  expect(sent).toEqual([
    { tx: expect.objectContaining({ from: OWNER, to: ADDRESS, value: '0x6f05b59d3b20000' }), chainId: SEPOLIA },
  ])
  // The wallet was read again once the transfer confirmed.
  expect(reads.length).toBeGreaterThanOrEqual(2)
  await snap(page, testInfo, 'fund-done')
})

test('switching networks', async ({ page }, testInfo) => {
  const { reads } = await mockServer(
    page,
    new Map([
      [SEPOLIA, walletState(SEPOLIA, ADDRESS)],
      [BSC, walletState(BSC, ADDRESS, { paused: true })],
    ]),
  )
  await page.goto('/dashboard')
  await expect(page.getByText('Your Mitfah wallet on Sepolia')).toBeVisible()

  await page.getByRole('button', { name: 'Network: Sepolia' }).click()
  await expect(page.getByRole('menuitem', { name: 'Add a network' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'network-menu')

  await page.getByRole('menuitem', { name: 'BNB Smart Chain' }).click()
  await expect(page.getByText('Your Mitfah wallet on BNB Smart Chain')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Network: BNB Smart Chain' })).toBeVisible()
  await expect(page.getByText('Paused', { exact: true })).toBeVisible()
  expect(reads).toContain(BSC)
  // The longest network name still fits the phone's top bar.
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'network-switched')

  // The choice survives a reload.
  await page.reload()
  await expect(page.getByText('Your Mitfah wallet on BNB Smart Chain')).toBeVisible()
})
