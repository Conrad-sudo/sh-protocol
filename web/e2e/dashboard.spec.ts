import { expect, test, type Page } from '@playwright/test'
import { FAKE_WALLET_NAME, installFakeWallet, type WalletTx } from './fakeWallet.ts'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, sessionBlock, snap, TOKEN, USDC, walletState } from './helpers.ts'

/*
 * The dashboard at real screen sizes, in both themes, against a mocked API and a fake browser
 * wallet. Nothing reaches wallet.db or a chain.
 */

const SEPOLIA = 11155111
const BSC = 56
const ADDRESS = '0x2222222222222222222222222222222222222222'
const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const TX_HASH = `0x${'34'.repeat(32)}`

/** One of a pool's two tokens, as a position's `amounts` and `fees` list them. */
const share = (ticker: string, decimals: number, raw: string) => ({ ticker, decimals, raw })

/**
 * Three Uniswap V3 positions as GET /api/wallet lists them: ETH/USDC in range, LINK/ETH out of range
 * (all ETH now, no fees), and ETH/USDC over the full range in another pool. Mirrors makePosition in
 * src/test/fixtures.ts.
 */
const POSITIONS = [
  {
    id: 101,
    pair: 'eth/usdc',
    base: 'eth',
    quote: 'usdc',
    fee_pct: 0.05,
    min_price: 2850.4321,
    max_price: 3150.1234,
    full_range: false,
    current_price: 3020.2468,
    in_range: true,
    amounts: [share('eth', 18, '1200000000000000000'), share('usdc', 6, '3400000000')],
    fees: [share('eth', 18, '4000000000000000'), share('usdc', 6, '12500000')],
  },
  {
    id: 102,
    pair: 'link/eth',
    base: 'link',
    quote: 'eth',
    fee_pct: 0.3,
    min_price: 0.0041234,
    max_price: 0.0045678,
    full_range: false,
    current_price: 0.0049876,
    in_range: false,
    amounts: [share('link', 18, '0'), share('eth', 18, '2500000000000000000')],
    fees: [share('link', 18, '0'), share('eth', 18, '0')],
  },
  {
    id: 103,
    pair: 'eth/usdc',
    base: 'eth',
    quote: 'usdc',
    fee_pct: 1,
    min_price: 0,
    max_price: null,
    full_range: true,
    current_price: 3020.2468,
    in_range: true,
    amounts: [share('eth', 18, '500000000000000000'), share('usdc', 6, '1510000000')],
    fees: [share('eth', 18, '0'), share('usdc', 6, '0')],
  },
]

/**
 * A signed-in account with a wallet on each chain in `wallets`, in that order. `extra` answers other
 * API routes, keyed by "METHOD /path".
 */
async function mockServer(
  page: Page,
  wallets: Map<number, object>,
  extra: Record<string, (body: unknown) => object> = {},
) {
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
  })

  await page.route(isApiUrl, route => {
    const path = new URL(route.request().url()).pathname
    const handler = extra[`${route.request().method()} ${path}`]
    if (handler) return route.fulfill(handler(route.request().postDataJSON()))
    if (path.startsWith('/api/wallet/')) {
      const chainId = Number(path.split('/').at(-1))
      reads.push(chainId)
      return route.fulfill({ json: wallets.get(chainId) })
    }
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      // A deposit from the Fund drawer, followed through the API: mined at the first ask.
      case '/api/transactions/deposit':
        return route.fulfill({ json: { status: 'confirmed', tx_hash: TX_HASH } })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: OWNER, wallet_chains: walletChains } })
      case '/api/chains':
        return route.fulfill({
          json: {
            chains: [
              {
                chain_id: SEPOLIA,
                name: 'sepolia',
                native_ticker: 'ETH',
                fork: false,
                exchange: 'uniswap-v3',
                default_spenders: ['0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E', '0x1238536071E1c677A632429e3655c799b22cDA52'],
              },
              {
                chain_id: BSC,
                name: 'bsc',
                native_ticker: 'BNB',
                fork: false,
                exchange: 'pancakeswap-v2',
                default_spenders: ['0x10ED43C718714eb63d5aA57B78B54704E256024E'],
              },
              {
                chain_id: 1,
                name: 'mainnet',
                native_ticker: 'ETH',
                fork: false,
                exchange: 'uniswap-v3',
                default_spenders: ['0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45', '0xC36442b4a4522E871399CD717aBDD847Ab11FE88'],
              },
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

  await expect(page.getByText('Your Mitfah smart wallet on Sepolia')).toBeVisible()
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
    session: sessionBlock([]),
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

test('liquidity positions, in range and out', async ({ page }, testInfo) => {
  await mockServer(page, new Map([[SEPOLIA, walletState(SEPOLIA, ADDRESS, { positions: POSITIONS })]]))
  await page.goto('/dashboard')

  const positions = page.getByRole('list', { name: 'Liquidity positions' })
  await expect(positions.getByRole('listitem')).toHaveCount(3)
  await expect(positions.getByText('2,850.4 – 3,150.1 USDC per ETH')).toBeVisible()
  await expect(positions.getByText('Out of range', { exact: true })).toBeVisible()
  await expect(positions.getByText("It isn't earning fees until the price comes back into this range.")).toBeVisible()
  await expect(positions.getByText('Full range', { exact: true })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'dashboard-positions')
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
  await expect(page.getByText('Your Mitfah smart wallet on Sepolia')).toBeVisible()

  await page.getByRole('button', { name: 'Network: Sepolia' }).click()
  await expect(page.getByRole('menuitem', { name: 'Add a network' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'network-menu')

  await page.getByRole('menuitem', { name: 'BNB Smart Chain' }).click()
  await expect(page.getByText('Your Mitfah smart wallet on BNB Smart Chain')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Network: BNB Smart Chain' })).toBeVisible()
  await expect(page.getByText('Paused', { exact: true })).toBeVisible()
  expect(reads).toContain(BSC)
  // The longest network name still fits the phone's top bar.
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'network-switched')

  // The choice survives a reload.
  await page.reload()
  await expect(page.getByText('Your Mitfah smart wallet on BNB Smart Chain')).toBeVisible()
})

test('adding a token by its address', async ({ page }, testInfo) => {
  const PEPE = '0x6982508145454Ce325dDbE47a25d4ec3d2311933'
  const pepe = { chain_id: SEPOLIA, address: PEPE, ticker: 'pepe', symbol: 'PEPE', name: 'Pepe', decimals: 18, balance_raw: '5000000000000000000', listed: false }
  const wallets = new Map<number, object>([[SEPOLIA, walletState(SEPOLIA, ADDRESS)]])
  await mockServer(page, wallets, {
    'POST /api/tokens/custom/lookup': () => ({ json: pepe }),
    'POST /api/tokens/custom': () => {
      const before = walletState(SEPOLIA, ADDRESS)
      wallets.set(
        SEPOLIA,
        walletState(SEPOLIA, ADDRESS, {
          balances: [
            ...before.balances,
            { ticker: 'pepe', address: PEPE, native: false, custom: true, name: 'Pepe', decimals: 18, raw: pepe.balance_raw, amount: 5 },
          ],
        }),
      )
      return { status: 201, json: pepe }
    },
  })
  await page.goto('/dashboard')

  await page.getByRole('button', { name: 'Add token' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Token contract address').fill(PEPE)
  await dialog.getByRole('button', { name: 'Continue' }).click()
  await expect(dialog.getByText("Your spending limit can't cover PEPE.")).toBeVisible()
  await expect(dialog.getByText('5 PEPE')).toBeVisible()
  await expect(dialog.getByText('ERC-20 token')).toBeVisible()
  await expectNoSidewaysScroll(page)
  await dialog.screenshot({ path: testInfo.outputPath('add-token-preview.png') })

  await dialog.getByRole('button', { name: 'Add token' }).click()
  await expect(page.getByText('PEPE added. It now shows in your balances.')).toBeVisible()
  const row = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: /^PEPE/ }) })
  await expect(row).toContainText('No price')
  await expect(row).toContainText('Not limited')
  await expect(page.getByRole('button', { name: 'Remove PEPE from your dashboard' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'dashboard-custom-token')
})

test('adding a token Mitfah lists, and counting it', async ({ page }, testInfo) => {
  const USDT = '0xaA8E23Fb1079EA71e0a56F48a2aA51851D8433D0'
  const usdt = { chain_id: SEPOLIA, address: USDT, ticker: 'usdt', symbol: 'USDT', name: 'Tether USD', decimals: 6, balance_raw: '0', listed: true }
  const wallets = new Map<number, object>([[SEPOLIA, walletState(SEPOLIA, ADDRESS)]])
  const { sent } = await mockServer(page, wallets, {
    'POST /api/tokens/custom/lookup': () => ({ json: usdt }),
    'POST /api/tokens/custom': () => ({ status: 201, json: usdt }),
    'POST /api/wallet/watched-tokens/prepare': () => ({ json: { tx: { to: ADDRESS, data: '0x1234' }, token_address: USDT } }),
    'POST /api/wallet/tx/confirm': () => {
      const before = walletState(SEPOLIA, ADDRESS)
      wallets.set(
        SEPOLIA,
        walletState(SEPOLIA, ADDRESS, {
          spending: { ...before.spending, watched_tokens: [...before.spending.watched_tokens, { ticker: 'usdt', address: USDT }] },
          balances: [...before.balances, { ticker: 'usdt', address: USDT, native: false, decimals: 6, raw: '0', amount: 0 }],
        }),
      )
      return { json: { status: 'confirmed', tx_hash: TX_HASH } }
    },
  })
  await page.goto('/dashboard')

  await page.getByRole('button', { name: 'Add token' }).click()
  const dialog = page.getByRole('dialog').filter({ hasText: 'Add a token' })
  await dialog.getByLabel('Token contract address').fill(USDT)
  await dialog.getByRole('button', { name: 'Continue' }).click()
  const preview = page.getByRole('dialog').filter({ hasText: 'Add USDT?' })
  await expect(preview.getByText('Pricing is available for USDT.')).toBeVisible()
  await preview.getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()
  await expect(preview.getByRole('checkbox', { name: 'Count USDT toward my spending limit' })).toBeChecked()
  await expectNoSidewaysScroll(page)
  await preview.screenshot({ path: testInfo.outputPath('add-listed-token-preview.png') })

  await preview.getByRole('button', { name: 'Add token' }).click()
  await expect(page.getByText('USDT added. It now counts toward your limit.')).toBeVisible()
  const row = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: /^USDT/ }) })
  await expect(row).toBeVisible()
  await expect(row).not.toContainText('Not limited')
  expect(sent).toHaveLength(1)
})

test('removing a token that counts asks to stop counting it first', async ({ page }, testInfo) => {
  await mockServer(page, new Map([[SEPOLIA, walletState(SEPOLIA, ADDRESS)]]))
  await page.goto('/dashboard')

  // ETH and WETH always count, so they have no Remove.
  await expect(page.getByRole('button', { name: /^Remove .+ from your dashboard$/ })).toHaveCount(1)
  await page.getByRole('button', { name: 'Remove USDC from your dashboard' }).click()
  const dialog = page.getByRole('alertdialog', { name: 'Remove USDC?' })
  await expect(dialog.getByText('Step 1 of 2: stop counting USDC')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Stop counting USDC' })).toBeDisabled()
  await expectNoSidewaysScroll(page)
  await dialog.screenshot({ path: testInfo.outputPath('remove-counted-token.png') })
})
