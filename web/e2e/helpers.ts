import { expect, type Page, type TestInfo } from '@playwright/test'

export const TOKEN = { access_token: 'e2e-token', token_type: 'bearer', expires_in: 900, user_id: 7 }

export const ME = {
  user_id: 7,
  email: 'sam@example.com',
  owner_addr: null as string | null,
  has_password: true,
  google_linked: false,
  telegram_linked: false,
  wallet_chains: [] as number[],
}

// For page.route: the app's API calls only. A predicate rather than a "**/api/**" glob, because in
// dev Vite serves source files such as /src/api/client.ts, and the glob would answer those with
// mock JSON too.
export function isApiUrl(url: URL) {
  return url.pathname.startsWith('/api/')
}

export async function expectNoSidewaysScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow, 'page is wider than the screen').toBeLessThanOrEqual(0)
}

export async function expectTheme(page: Page, testInfo: TestInfo) {
  const dark = testInfo.project.use.colorScheme === 'dark'
  await expect(page.locator('body')).toHaveClass(dark ? /rs-theme-dark/ : /rs-theme-light/)
}

export function snap(page: Page, testInfo: TestInfo, name: string) {
  return page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true })
}
