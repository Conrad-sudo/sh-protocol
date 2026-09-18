import { AxeBuilder } from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { installFakeWallet } from './fakeWallet.ts'
import { isApiUrl, ME, TOKEN, walletState } from './helpers.ts'

/*
 * An axe scan of every page a person actually uses, at each screen size and in both themes
 * (the projects in playwright.config.ts). Only serious problems are asserted on — axe's
 * "moderate" findings are mostly advice — and each page is scanned as it first appears.
 */

const SEPOLIA = 11155111
const ADDRESS = '0x2222222222222222222222222222222222222222'
const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'

const CHAINS = [{ chain_id: SEPOLIA, name: 'sepolia-fork', native_ticker: 'ETH', fork: true }]
const CONTACTS = [
  { name: 'alex', address: OWNER },
  { name: 'sam', address: '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC' },
]
const HISTORY = [
  { role: 'user', text: 'how much can I still spend today?' },
  { role: 'assistant', text: 'You can still spend **$60** of your $100 limit.' },
]

/** A signed-in account with a wallet, contacts and a short conversation. */
async function mockServer(page: Page) {
  await installFakeWallet(page, {
    address: OWNER,
    chainId: SEPOLIA,
    signMessage: async () => {
      throw new Error('No signature is needed to look at a page.')
    },
    sendTransaction: async () => {
      throw new Error('No transaction is sent by the scan.')
    },
  })
  await page.route(isApiUrl, route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/wallet/')) {
      return route.fulfill({ json: walletState(SEPOLIA, ADDRESS) })
    }
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: OWNER, wallet_chains: [SEPOLIA] } })
      case '/api/chains':
        return route.fulfill({ json: { chains: CHAINS } })
      case '/api/contacts':
        return route.fulfill({ json: { contacts: CONTACTS } })
      case '/api/chat/history':
        return route.fulfill({ json: { messages: HISTORY } })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
}

/** A signed-out visitor: the API says so, and no chain is asked for. */
async function mockSignedOut(page: Page) {
  await page.route(isApiUrl, route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chains') return route.fulfill({ json: { chains: CHAINS } })
    return route.fulfill({ status: 401, json: { detail: 'Not authenticated' } })
  })
}

async function scan(page: Page) {
  const { violations } = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze()
  return violations
    .filter(violation => violation.impact === 'serious' || violation.impact === 'critical')
    .map(violation => `${violation.id} (${violation.impact}): ${violation.nodes.map(node => node.target).join(' · ')}`)
}

const SIGNED_OUT = ['/', '/login', '/signup', '/terms', '/privacy']
const SIGNED_IN = ['/dashboard', '/assistant', '/contacts', '/controls', '/settings', '/onboarding']

for (const path of SIGNED_OUT) {
  test(`no serious accessibility problems on ${path}`, async ({ page }) => {
    await mockSignedOut(page)
    await page.goto(path)
    await expect(page.getByRole('main')).toBeVisible()
    expect(await scan(page)).toEqual([])
  })
}

for (const path of SIGNED_IN) {
  test(`no serious accessibility problems on ${path}`, async ({ page }) => {
    await mockServer(page)
    await page.goto(path)
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    expect(await scan(page)).toEqual([])
  })
}

test('the keyboard reaches the content without walking through the navigation', async ({ page }) => {
  await mockServer(page)
  await page.goto('/dashboard')
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()

  await page.keyboard.press('Tab')
  const skip = page.getByRole('link', { name: 'Skip to content' })
  await expect(skip).toBeFocused()
  // Hidden off-screen until it is focused, and on screen once it is.
  await expect(skip).toBeInViewport()

  await page.keyboard.press('Enter')
  await expect(page.locator('main#main-content')).toBeFocused()
})
