import { generateKeyPairSync } from 'node:crypto'
import { expect, type Page, type TestInfo } from '@playwright/test'

export const TOKEN = { access_token: 'e2e-token', token_type: 'bearer', expires_in: 900, user_id: 7 }

/** The address the mocked account signs in as: the owner of every mocked wallet (walletState). */
export const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'

export const ME = {
  user_id: 7,
  owner_addr: OWNER as string | null,
  telegram_linked: false,
  telegram_bot: null as string | null,
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

/** The test account's passkey on its iPhone: how the wallet names it, and its WebAuthn credential id. */
export const PASSKEY_ID = `0x${'ab'.repeat(32)}`
export const PASSKEY_CREDENTIAL = 'aVBob25lLXBhc3NrZXk'

/**
 * One key of GET /api/wallet's `session`: the iPhone's passkey with `days` left (negative: run out),
 * its derived fields worked out as the API does.
 */
export function sessionKey(days = 20, overrides: Record<string, unknown> = {}) {
  const now = Math.floor(Date.now() / 1000)
  const validUntil = now + Math.round(days * 86_400)
  return {
    key_id: PASSKEY_ID,
    device_name: 'iPhone',
    credential_id: PASSKEY_CREDENTIAL,
    valid_until: validUntil,
    expires_in_secs: Math.max(validUntil - now, 0),
    active: now <= validUntil,
    needs_renewal: validUntil - now < 3 * 86_400,
    ...overrides,
  }
}

/** The `session` block around `keys`, with the contract's limits. */
export function sessionBlock(keys: ReturnType<typeof sessionKey>[]) {
  return { max_keys: 5, max_ttl_secs: 90 * 86_400, renewal_warning_secs: 3 * 86_400, keys }
}

/**
 * Gives the page a passkey authenticator: Chromium's virtual one, over CDP, built into "this device"
 * and checking the user without asking, as Face ID would. So the app's own WebAuthn calls run for
 * real, only no one has to touch anything. With `credentialId` (base64url) it already holds that
 * passkey for this site, as a device the account added earlier would; the key itself is new, which
 * the mocked server never checks.
 */
export async function addPasskeyAuthenticator(page: Page, { credentialId }: { credentialId?: string } = {}) {
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  })
  if (credentialId) {
    const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' })
    await cdp.send('WebAuthn.addCredential', {
      authenticatorId,
      credential: {
        // CDP takes binary fields as plain base64.
        credentialId: Buffer.from(credentialId, 'base64url').toString('base64'),
        isResidentCredential: true,
        rpId: 'localhost',
        privateKey: privateKey.export({ format: 'der', type: 'pkcs8' }).toString('base64'),
        userHandle: Buffer.from('user-handle-7').toString('base64'),
        signCount: 0,
      },
    })
  }
  return {
    /** The passkeys it holds, by base64url credential id. */
    credentials: async () => {
      const { credentials } = await cdp.send('WebAuthn.getCredentials', { authenticatorId })
      return credentials.map(credential => Buffer.from(credential.credentialId, 'base64').toString('base64url'))
    },
  }
}

/** One of the account's passkeys, as GET /api/passkeys lists it. */
export function passkey(overrides: Record<string, unknown> = {}) {
  return { key_id: PASSKEY_ID, credential_id: PASSKEY_CREDENTIAL, device_name: 'iPhone', created_at: 1_790_000_000, ...overrides }
}

/**
 * A GET /api/wallet/{chain_id} answer: active, the assistant on, $40 of $100 spent an hour into a
 * 24-hour period, with ETH, USDC (watched) and WETH (not watched), and no liquidity positions.
 * Mirrors src/test/fixtures.ts, which the e2e build cannot import.
 */
export function walletState(chainId: number, address: string, overrides: Record<string, unknown> = {}) {
  return {
    chain_id: chainId,
    chain_name: 'sepolia-fork',
    address,
    owner: OWNER,
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
    session: sessionBlock([sessionKey()]),
    limits: {
      max_op_gas_cost_wei: '10000000000000000',
      session_gas: { budget_wei: '50000000000000000', remaining_wei: '50000000000000000', refills_at: null },
      allowlist_enabled: false,
      trusted_spenders: [],
    },
    balances: [
      { ticker: 'eth', address: null, native: true, decimals: 18, raw: '1500000000000000000', amount: 1.5 },
      { ticker: 'usdc', address: USDC, native: false, decimals: 6, raw: '25000000', amount: 25 },
      { ticker: 'weth', address: WETH, native: false, always_counted: true, decimals: 18, raw: '0', amount: 0 },
    ],
    positions: [],
    ...overrides,
  }
}
