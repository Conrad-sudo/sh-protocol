import { expect, test, type Page, type TestInfo } from '@playwright/test'

/*
 * The app shell at real screen sizes, in both themes. The API is mocked in the browser, so these
 * runs need no back end and write nothing to wallet.db.
 */

const TOKEN = { access_token: 'e2e-token', token_type: 'bearer', expires_in: 900, user_id: 7 }
const ME = {
  user_id: 7,
  email: 'sam@example.com',
  owner_addr: null,
  google_linked: false,
  telegram_linked: false,
  wallet_chains: [],
}

async function mockApi(page: Page, { signedIn }: { signedIn: boolean }) {
  let session = signedIn
  // A predicate, not the glob '**/api/**': in dev, Vite serves source files such as
  // /src/api/client.ts, and the glob would answer those with mock JSON too.
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    switch (new URL(route.request().url()).pathname) {
      case '/api/auth/refresh':
        return session
          ? route.fulfill({ json: TOKEN })
          : route.fulfill({ status: 401, json: { detail: 'No refresh token' } })
      case '/api/auth/login':
        session = true
        return route.fulfill({ json: TOKEN })
      case '/api/auth/logout':
        session = false
        return route.fulfill({ json: { status: 'signed out' } })
      case '/api/me':
        return route.fulfill({ json: ME })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
}

async function expectNoSidewaysScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow, 'page is wider than the screen').toBeLessThanOrEqual(0)
}

async function expectTheme(page: Page, testInfo: TestInfo) {
  const dark = testInfo.project.use.colorScheme === 'dark'
  await expect(page.locator('body')).toHaveClass(dark ? /rs-theme-dark/ : /rs-theme-light/)
}

function snap(page: Page, testInfo: TestInfo, name: string) {
  return page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true })
}

test('home page', async ({ page }, testInfo) => {
  await mockApi(page, { signedIn: false })
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('only spend what you allow')
  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'home')
})

test('signing in returns you to the page you asked for', async ({ page }, testInfo) => {
  await mockApi(page, { signedIn: false })
  await page.goto('/contacts')

  await expect(page).toHaveURL(/\/login\?next=%2Fcontacts$/)
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'login')

  await page.getByLabel('Email').fill('sam@example.com')
  await page.locator('input[name="password"]').fill('hunter2hunter2')
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page).toHaveURL(/\/contacts$/)
  await expect(page.getByRole('heading', { name: 'Contacts' })).toBeVisible()
})

test('sign-up page', async ({ page }, testInfo) => {
  await mockApi(page, { signedIn: false })
  await page.goto('/signup')
  await page.locator('input[name="password"]').fill('Abcdefgh1')
  await expect(page.getByText('Strong')).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'signup')
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

  // RSuite renders the collapsed rail's items as menu items rather than links.
  const nav = page.getByRole('navigation', { name: 'Main' })
  await nav.getByRole('link', { name: 'Settings' }).or(nav.getByRole('menuitem', { name: 'Settings' })).click()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'settings')
})

test('signing out lands on the sign-in page', async ({ page }) => {
  await mockApi(page, { signedIn: true })
  await page.goto('/settings')
  await page.getByRole('main').getByRole('button', { name: 'Sign out' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
})
