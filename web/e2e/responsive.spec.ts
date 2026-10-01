import { expect, test, type Page } from '@playwright/test'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, TOKEN, walletState } from './helpers.ts'

/*
 * Every page at the widths real screens come in, from a small phone to a wide desktop. The other
 * specs use the three device profiles; this one walks the sizes in between, where a layout breaks
 * quietly — a card that won't shrink, a row that pushes the page sideways.
 *
 * It runs in the desktop projects, which set the viewport rather than emulate a device, so a width
 * can be changed mid-test; both themes still run.
 */

const WIDTHS = [360, 390, 768, 1024, 1440]
const SEPOLIA = 11155111
const ADDRESS = '0x2222222222222222222222222222222222222222'
const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const CHAINS = [{ chain_id: SEPOLIA, name: 'sepolia-fork', native_ticker: 'ETH', fork: true }]
// The longest kind of line the History tab shows: an assistant swap, whole addresses and all.
const TRANSACTIONS = [1, 2].map(id => ({
  id,
  chain_id: SEPOLIA,
  source: 'assistant',
  status: 'confirmed',
  tx_hash: `0x${String(id).repeat(64)}`,
  action:
    'Approve 0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3 to spend 100.0 of 0x834D47259805D7F29e4Dcc9a3254F33E47bd0380; ' +
    'Swap 100.0 of 0x834D47259805D7F29e4Dcc9a3254F33E47bd0380 for at least 9.4894769844675e-05 of the native asset',
  created_at: 1_790_000_000 + id,
  mined_at: 1_790_000_012 + id,
}))

const PAGES = [
  { path: '/', heading: 'Send and swap crypto by chat. Your wallet enforces the rules, your AI assistant follows them.' },
  { path: '/dashboard', heading: 'Dashboard' },
  { path: '/assistant', heading: 'Assistant' },
  { path: '/history', heading: 'History' },
  { path: '/contacts', heading: 'Contacts' },
  { path: '/controls', heading: 'Controls' },
  { path: '/settings', heading: 'Settings' },
  { path: '/onboarding', heading: 'Create your wallet' },
]

// The desktop projects set a viewport instead of emulating a device, so the width can be changed
// mid-test; the phone and tablet projects would fight with it.
// oxlint-disable-next-line no-empty-pattern -- Playwright insists on the fixtures object here.
test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('desktop'), 'this spec sets the width itself')
})

async function mockServer(page: Page) {
  await page.route(isApiUrl, route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/wallet/')) return route.fulfill({ json: walletState(SEPOLIA, ADDRESS) })
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: OWNER, wallet_chains: [SEPOLIA] } })
      case '/api/chains':
        return route.fulfill({ json: { chains: CHAINS } })
      case '/api/contacts':
        return route.fulfill({
          json: { contacts: [{ name: 'the-landlord-of-the-flat-on-maple-street-who-is-paid-every-month', address: OWNER }] },
        })
      case '/api/chat/history':
        return route.fulfill({ json: { messages: [] } })
      case '/api/transactions':
        return route.fulfill({ json: { transactions: TRANSACTIONS, next_before: null } })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
}

for (const width of WIDTHS) {
  test(`every page fits a ${width}px screen`, async ({ page }, testInfo) => {
    // Eight pages, each loaded and photographed.
    test.setTimeout(120_000)
    await mockServer(page)
    await page.setViewportSize({ width, height: 900 })

    for (const { path, heading } of PAGES) {
      await page.goto(path)
      await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible()
      await expectNoSidewaysScroll(page)
      await page.screenshot({ path: testInfo.outputPath(`${width}${path.replace(/\//g, '-')}.png`), fullPage: true })
    }
    await expectTheme(page, testInfo)
  })
}

test('the navigation changes shape with the screen, and nothing is lost', async ({ page }) => {
  await mockServer(page)
  await page.goto('/dashboard')
  const nav = page.getByRole('navigation', { name: 'Main' })

  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(page.locator('.mf-sidebar')).toHaveAttribute('data-expanded', 'true')
  await expect(nav.getByRole('link', { name: 'Settings' })).toBeVisible()

  await page.setViewportSize({ width: 768, height: 900 })
  await expect(page.locator('.mf-sidebar')).toHaveAttribute('data-expanded', 'false')
  await expect(nav.getByRole('link', { name: 'Settings' })).toBeVisible()

  await page.setViewportSize({ width: 360, height: 900 })
  await expect(page.locator('.mf-sidebar')).toHaveCount(0)
  const tabs = page.locator('.mf-tabbar')
  await expect(tabs.getByRole('link')).toHaveCount(6)
  await expect(tabs.getByRole('link', { name: 'Settings' })).toBeVisible()
  // Six tabs on a small phone: every label still fits its tab.
  const clipped = await tabs.locator('.mf-tab > span').evaluateAll(labels =>
    labels.filter(label => label.scrollWidth > label.parentElement!.clientWidth).map(label => label.textContent),
  )
  expect(clipped).toEqual([])
})
