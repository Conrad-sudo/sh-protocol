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

const PAGES = [
  { path: '/', heading: 'An AI assistant for your crypto wallet that can only spend what you allow.' },
  { path: '/dashboard', heading: 'Dashboard' },
  { path: '/assistant', heading: 'Assistant' },
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
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
}

for (const width of WIDTHS) {
  test(`every page fits a ${width}px screen`, async ({ page }, testInfo) => {
    // Seven pages, each loaded and photographed.
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
  await expect(page.locator('.mf-tabbar').getByRole('link', { name: 'Settings' })).toBeVisible()
})
