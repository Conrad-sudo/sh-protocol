import { expect, test, type Page } from '@playwright/test'
import { FAKE_WALLET_NAME, installFakeWallet } from './fakeWallet.ts'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, OWNER, snap, TOKEN } from './helpers.ts'

const SEPOLIA = 11155111

/*
 * The app shell at real screen sizes, in both themes. The API is mocked in the browser, so these
 * runs need no back end and write nothing to wallet.db.
 */

async function mockApi(page: Page, { signedIn, me = ME }: { signedIn: boolean; me?: typeof ME }) {
  let session = signedIn
  await page.route(isApiUrl, route => {
    switch (new URL(route.request().url()).pathname) {
      case '/api/auth/refresh':
        return session
          ? route.fulfill({ json: TOKEN })
          : route.fulfill({ status: 401, json: { detail: 'No refresh token' } })
      case '/api/auth/siwe/nonce':
        return route.fulfill({ json: { nonce: 'e2enonce1234abcd' } })
      case '/api/auth/siwe/login':
        session = true
        return route.fulfill({ json: TOKEN })
      case '/api/auth/logout':
        session = false
        return route.fulfill({ json: { status: 'signed out' } })
      case '/api/me':
        return route.fulfill({ json: me })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
}

test('signing in with the wallet returns you to the page you asked for', async ({ page }, testInfo) => {
  await installFakeWallet(page, {
    address: OWNER,
    chainId: SEPOLIA,
    signMessage: async () => `0x${'ab'.repeat(65)}`,
    sendTransaction: async () => {
      throw new Error('signing in sends no transaction')
    },
  })
  await mockApi(page, { signedIn: false })
  await page.goto('/contacts')

  await expect(page).toHaveURL(/\/login\?next=%2Fcontacts$/)
  await expect(page.getByRole('heading', { name: 'Sign in with your wallet' })).toBeVisible()
  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'login')

  await page.getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()
  await expect(page.getByText(/to prove it's yours/)).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'login-connected')
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page).toHaveURL(/\/contacts$/)
  await expect(page.getByRole('heading', { name: 'Contacts' })).toBeVisible()
})

test('an old sign-up link lands on sign-in', async ({ page }) => {
  await mockApi(page, { signedIn: false })
  await page.goto('/signup?next=%2Fonboarding')
  await expect(page).toHaveURL(/\/login\?next=%2Fonboarding$/)
  await expect(page.getByRole('heading', { name: 'Sign in with your wallet' })).toBeVisible()
})

test('the app shell fits the screen', async ({ page }, testInfo) => {
  await mockApi(page, { signedIn: true })
  await page.goto('/dashboard')
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
  await expectTheme(page, testInfo)

  const width = page.viewportSize()!.width
  if (width >= 1024) {
    await expect(page.locator('.mf-sidebar')).toHaveAttribute('data-expanded', 'true')
    await expect(page.locator('.mf-tabbar')).toHaveCount(0)
  } else if (width >= 768) {
    await expect(page.locator('.mf-sidebar')).toHaveAttribute('data-expanded', 'false')
  } else {
    await expect(page.locator('.mf-tabbar')).toBeVisible()
    await expect(page.locator('.mf-sidebar')).toHaveCount(0)
  }
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'dashboard')

  const nav = page.getByRole('navigation', { name: 'Main' })
  await nav.getByRole('link', { name: 'Settings' }).click()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'settings')
})

test('settings shows the address you sign in with', async ({ page }, testInfo) => {
  const owner = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
  await mockApi(page, { signedIn: true, me: { ...ME, owner_addr: owner } })
  await page.goto('/settings')

  const panel = page.locator('.rs-panel', { hasText: 'Your wallet' })
  await expect(panel.getByTitle(owner)).toContainText('0xf39F…2266')
  await expect(panel.getByText('You sign in with it, and it owns your Mitfah wallets.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Copy address' })).toBeVisible()
  // Wallet sign-in is the only kind: nothing about email, passwords or Google.
  await expect(page.getByText('Sign-in methods')).toHaveCount(0)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'settings-owner')
})

test('signing out lands on the sign-in page', async ({ page }) => {
  await mockApi(page, { signedIn: true })
  await page.goto('/settings')
  await page.getByRole('main').getByRole('button', { name: 'Sign out' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByRole('heading', { name: 'Sign in with your wallet' })).toBeVisible()
})
