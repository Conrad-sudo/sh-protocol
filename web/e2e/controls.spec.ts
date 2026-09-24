import { expect, test, type Page } from '@playwright/test'
import { FAKE_WALLET_NAME, installFakeWallet, type WalletTx } from './fakeWallet.ts'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN, USDC, walletState, WETH } from './helpers.ts'

/*
 * The owner's controls — the Controls page, the dashboard's Withdraw drawer and Pause — at real
 * screen sizes, in both themes, against a mocked API and a fake browser wallet.
 */

const SEPOLIA = 11155111
const ADDRESS = '0x2222222222222222222222222222222222222222'
const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const TX_HASH = `0x${'9a'.repeat(32)}`

interface Prepared {
  path: string
  body: Record<string, unknown>
}

/**
 * A signed-in owner with one wallet. `wallet` is what GET /api/wallet answers; `afterConfirm`
 * replaces it once an owner transaction confirms, as the chain would.
 */
async function mockServer(page: Page, wallet: object, afterConfirm?: object) {
  let current = wallet
  const prepared: Prepared[] = []
  const sent: { tx: WalletTx; chainId: number }[] = []
  const sessionConfirms: string[] = []

  await installFakeWallet(page, {
    address: OWNER,
    chainId: SEPOLIA,
    signMessage: async () => {
      throw new Error('Owner controls should never ask for a signature.')
    },
    sendTransaction: async (tx, chainId) => {
      sent.push({ tx, chainId })
      return TX_HASH
    },
  })

  await page.route(isApiUrl, route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/prepare')) {
      prepared.push({ path, body: request.postDataJSON() as Record<string, unknown> })
      return route.fulfill({ json: { tx: { to: ADDRESS, data: '0x8456cb59', gas: '0x7530' } } })
    }
    if (path === '/api/wallet/tx/confirm') {
      if (afterConfirm) current = afterConfirm
      return route.fulfill({ json: { status: 'confirmed', tx_hash: TX_HASH } })
    }
    if (path === '/api/wallet/session/confirm') {
      sessionConfirms.push(path)
      if (afterConfirm) current = afterConfirm
      return route.fulfill({ json: { status: 'granted', tx_hash: TX_HASH } })
    }
    if (path.startsWith('/api/wallet/')) return route.fulfill({ json: current })
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: OWNER, wallet_chains: [SEPOLIA] } })
      case '/api/chains':
        return route.fulfill({
          json: { chains: [{ chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: false }] },
        })
      case '/api/tokens':
        return route.fulfill({
          json: {
            tokens: [
              { ticker: 'usdc', address: USDC },
              { ticker: 'weth', address: WETH },
            ],
          },
        })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return { prepared, sent, sessionConfirms }
}

/** Connects the fake wallet from the owner bar in `scope` (the page, a drawer or a modal). */
async function connectOwner(page: Page, scope = page.locator('body')) {
  await scope.locator('.mf-owner-bar').getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()
  await expect(scope.getByText('Owner connected')).toBeVisible()
}

test('the controls page, and pausing from it', async ({ page }, testInfo) => {
  const wallet = walletState(SEPOLIA, ADDRESS)
  const { prepared, sent } = await mockServer(page, wallet, { ...wallet, paused: true })
  await page.goto('/controls')

  await expect(page.getByRole('heading', { name: 'Controls', level: 1 })).toBeVisible()
  await expectTheme(page, testInfo)
  await expect(page.getByText(/Connect your owner wallet/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-disconnected')

  await connectOwner(page)
  await expect(page.getByLabel('Limit per period')).toHaveValue('100')
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-ready')

  await page.getByRole('button', { name: 'Pause wallet' }).click()
  await expect(page.getByText('Wallet paused. Nothing can go out until you unpause it.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Unpause' })).toBeVisible()
  expect(prepared).toEqual([{ path: '/api/wallet/pause/prepare', body: { chain_id: SEPOLIA } }])
  expect(sent).toEqual([{ tx: expect.objectContaining({ from: OWNER, to: ADDRESS, data: '0x8456cb59' }), chainId: SEPOLIA }])
  await snap(page, testInfo, 'controls-paused')
})

test('changes that loosen a limit ask first', async ({ page }, testInfo) => {
  const { prepared } = await mockServer(page, walletState(SEPOLIA, ADDRESS))
  await page.goto('/controls')
  await connectOwner(page)

  await page.getByLabel('Limit per period').fill('500')
  await page.getByLabel('Limit per period').locator('xpath=ancestor::form').getByRole('button', { name: 'Save' }).click()
  const confirm = page.getByRole('alertdialog')
  await expect(confirm).toContainText('up to $500.00 every 24 hours, up from $100.00')
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-confirm')
  await confirm.getByRole('button', { name: 'Raise limit' }).click()
  await expect(page.getByText('Spending limit set to $500.00.')).toBeVisible()

  // Adding a token tightens the limit, so it goes straight through.
  await page.getByRole('combobox', { name: 'Add a token' }).click()
  await page.getByRole('option', { name: 'WETH' }).click()
  await page.getByRole('button', { name: 'Add', exact: true }).click()
  await expect(page.getByText('WETH now counts toward your limit.')).toBeVisible()

  await page.getByRole('button', { name: 'Advanced' }).click()
  await expect(page.getByLabel('Most per operation')).toHaveValue('0.01')
  await expect(page.getByText('Contract allowlist')).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-advanced')

  expect(prepared.map(p => p.body)).toEqual([
    { chain_id: SEPOLIA, daily_limit_usd: 500 },
    { chain_id: SEPOLIA, token: 'weth', action: 'add' },
  ])
})

test("an account that isn't the owner", async ({ page }, testInfo) => {
  await mockServer(page, walletState(SEPOLIA, ADDRESS, { is_owner: false, paused: true }))
  await page.goto('/controls')

  await expect(page.getByText(/so they're switched off here/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Unpause' })).toBeDisabled()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-not-owner')
})

test('withdrawing from the dashboard', async ({ page }, testInfo) => {
  const { prepared } = await mockServer(page, walletState(SEPOLIA, ADDRESS))
  await page.goto('/dashboard')

  await page.getByRole('button', { name: 'Withdraw' }).click()
  const drawer = page.getByRole('dialog').filter({ hasText: 'Move funds out' })
  await connectOwner(page, drawer)
  await expect(drawer.getByRole('combobox', { name: 'Token' })).toContainText('ETH · 1.5 available')
  await drawer.getByRole('button', { name: 'Max' }).click()
  await expect(drawer.getByLabel('Amount')).toHaveValue('1.5')
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'withdraw-drawer')

  await drawer.getByRole('button', { name: 'Withdraw' }).click()
  await expect(drawer.getByText('Sent. The balance above is up to date.')).toBeVisible()
  expect(prepared).toEqual([
    { path: '/api/wallet/withdraw/prepare', body: { chain_id: SEPOLIA, token: 'eth', amount: '1.5', to: OWNER } },
  ])
  await snap(page, testInfo, 'withdraw-done')
})

test('pausing from the dashboard', async ({ page }, testInfo) => {
  const wallet = walletState(SEPOLIA, ADDRESS)
  const { prepared } = await mockServer(page, wallet, { ...wallet, paused: true })
  await page.goto('/dashboard')

  await page.getByRole('button', { name: 'Pause wallet' }).click()
  const modal = page.getByRole('dialog').filter({ hasText: 'Pause this wallet?' })
  await connectOwner(page, modal)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'pause-modal')

  await modal.getByRole('button', { name: 'Pause wallet' }).click()
  await expect(modal).toBeHidden()
  await expect(page.getByText('Paused', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Unpause' })).toBeVisible()
  expect(prepared).toEqual([{ path: '/api/wallet/pause/prepare', body: { chain_id: SEPOLIA } }])
})

test("renewing the assistant's access before it runs out", async ({ page }, testInfo) => {
  const key = '0x5555555555555555555555555555555555555555'
  const inTwoDays = Math.floor(Date.now() / 1000) + 2 * 86_400
  const expiring = walletState(SEPOLIA, ADDRESS, {
    session: {
      key,
      wallet_key: key,
      is_app_key: true,
      active: true,
      expires_at: inTwoDays,
      expires_in_secs: 2 * 86_400,
      needs_renewal: true,
    },
  })
  const { prepared, sessionConfirms } = await mockServer(page, expiring, walletState(SEPOLIA, ADDRESS))
  await page.goto('/controls')
  await connectOwner(page)

  const row = page.locator('.mf-control-row').filter({ has: page.getByRole('heading', { name: 'Assistant' }) })
  await expect(row.getByText('Expires soon')).toBeVisible()
  await expect(row.getByRole('button', { name: 'Renew' })).toBeVisible()
  await expect(row.getByRole('button', { name: 'Turn off assistant' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-assistant-expiring')

  await row.getByRole('button', { name: 'Renew' }).click()
  const confirm = page.getByRole('alertdialog')
  await expect(confirm).toContainText('It keeps its access for another 30 days')
  await confirm.getByRole('button', { name: 'Renew' }).click()
  await expect(page.getByText(/^Assistant renewed until \w{3} \d{1,2}, \d{4}\.$/)).toBeVisible()
  await expect(row.getByText('On', { exact: true })).toBeVisible()

  expect(prepared).toEqual([
    { path: '/api/wallet/session/prepare', body: { chain_id: SEPOLIA, action: 'add', ttl_secs: 30 * 86_400 } },
  ])
  expect(sessionConfirms).toHaveLength(1)
})
