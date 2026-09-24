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
  // Against the emulated screen, not window.innerWidth: a mobile browser zooms out to fit content
  // that overflows, which widens innerWidth to match and would hide the very overflow checked for.
  const screenWidth = page.viewportSize()!.width
  const pageWidth = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(pageWidth, 'page is wider than the screen').toBeLessThanOrEqual(screenWidth)
}

export async function expectTheme(page: Page, testInfo: TestInfo) {
  const dark = testInfo.project.use.colorScheme === 'dark'
  await expect(page.locator('body')).toHaveClass(dark ? /rs-theme-dark/ : /rs-theme-light/)
}

export function snap(page: Page, testInfo: TestInfo, name: string) {
  return page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true })
}

export const USDC = '0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8'
export const WETH = '0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14'

/**
 * A GET /api/wallet/{chain_id} answer: active, the assistant on, $40 of $100 spent an hour into a
 * 24-hour period, with ETH, USDC (watched) and WETH (not watched). Mirrors src/test/fixtures.ts,
 * which the e2e build cannot import.
 */
export function walletState(chainId: number, address: string, overrides: Record<string, unknown> = {}) {
  return {
    chain_id: chainId,
    chain_name: 'sepolia-fork',
    address,
    owner: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8',
    is_owner: true,
    paused: false,
    spending: {
      hook_installed: true,
      daily_limit_usd: 100,
      spent_usd: 40,
      remaining_usd: 60,
      window_hours: 24,
      window_start: Math.floor(Date.now() / 1000) - 3_600,
      watched_tokens: [{ ticker: 'usdc', address: USDC }],
    },
    session: {
      key: '0x5555555555555555555555555555555555555555',
      wallet_key: '0x5555555555555555555555555555555555555555',
      is_app_key: true,
      active: true,
      expires_at: Math.floor(Date.now() / 1000) + 20 * 86_400,
      expires_in_secs: 20 * 86_400,
      needs_renewal: false,
    },
    limits: { max_op_gas_cost_wei: '10000000000000000', allowlist_enabled: false, trusted_spenders: [] },
    balances: [
      { ticker: 'eth', address: null, native: true, decimals: 18, raw: '1500000000000000000', amount: 1.5 },
      { ticker: 'usdc', address: USDC, native: false, decimals: 6, raw: '25000000', amount: 25 },
      { ticker: 'weth', address: WETH, native: false, decimals: 18, raw: '0', amount: 0 },
    ],
    ...overrides,
  }
}
