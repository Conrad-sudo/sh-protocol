import { expect, test, type Page } from '@playwright/test'
import { FAKE_WALLET_NAME, installFakeWallet, type WalletTx } from './fakeWallet.ts'
import {
  addPasskeyAuthenticator,
  expectNoSidewaysScroll,
  expectTheme,
  isApiUrl,
  ME,
  passkey,
  PASSKEY_ID,
  sessionBlock,
  sessionKey,
  snap,
  TOKEN,
  USDC,
  walletState,
  WETH,
} from './helpers.ts'

/*
 * The owner's controls — the Controls page, the dashboard's Withdraw drawer and Pause — at real
 * screen sizes, in both themes, against a mocked API and a fake browser wallet.
 */

const SEPOLIA = 11155111
const ADDRESS = '0x2222222222222222222222222222222222222222'
const OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const TX_HASH = `0x${'9a'.repeat(32)}`
// What every new wallet on Sepolia trusts, as /api/chains lists it: Uniswap V3's router, then its
// positions contract. The mocked wallet predates them, so the Advanced panel offers to trust both.
const V3_ROUTER = '0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E'
const POSITION_MANAGER = '0x1238536071E1c677A632429e3655c799b22cDA52'
// A review registry, as the allowlist read names it.
const REGISTRY = '0x8004000000000000000000000000000000000713'
const ALLOWLIST_SUGGESTED = [
  { address: V3_ROUTER, label: 'Uniswap V3 router' },
  { address: USDC, label: 'USDC' },
  { address: REGISTRY, label: 'ERC-8004 review registry' },
]
/** GET /api/wallet/{chain_id}/allowlist for a wallet that has never used it. */
const ALLOWLIST_OFF = { enabled: false, targets: [], suggested: ALLOWLIST_SUGGESTED }
/** The key id the server gives a passkey made during a test. */
const NEW_KEY_ID = `0x${'ef'.repeat(32)}`
/** POST /api/passkeys/register/options: for this site, ES256 only, the user verified. */
const REGISTRATION_OPTIONS = {
  rp: { id: 'localhost', name: 'Mitfah' },
  user: { id: Buffer.from('user-handle-7').toString('base64url'), name: 'mitfah-7', displayName: 'Mitfah' },
  challenge: Buffer.from('registration challenge 32 bytes!').toString('base64url'),
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
  authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
  attestation: 'none',
  excludeCredentials: [],
  timeout: 60_000,
}

interface Prepared {
  path: string
  body: Record<string, unknown>
}

interface AllowlistAnswers {
  /** What GET /api/wallet/{chain_id}/allowlist answers. */
  allowlist?: object
  /** Replaces it once an owner transaction confirms. */
  allowlistAfterConfirm?: object
}

/**
 * A signed-in owner with one wallet. `wallet` is what GET /api/wallet answers; `afterConfirm`
 * replaces it once an owner transaction confirms, as the chain would.
 */
async function mockServer(
  page: Page,
  wallet: object,
  afterConfirm?: object,
  { allowlist = ALLOWLIST_OFF, allowlistAfterConfirm }: AllowlistAnswers = {},
) {
  let current = wallet
  let currentAllowlist = allowlist
  const prepared: Prepared[] = []
  const sent: { tx: WalletTx; chainId: number }[] = []
  const sessionConfirms: string[] = []
  const registered: { credentialId: string; deviceName: string }[] = []

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
      if (allowlistAfterConfirm) currentAllowlist = allowlistAfterConfirm
      return route.fulfill({ json: { status: 'confirmed', tx_hash: TX_HASH } })
    }
    if (/^\/api\/wallet\/\d+\/allowlist$/.test(path)) return route.fulfill({ json: currentAllowlist })
    if (path === '/api/wallet/session/confirm') {
      sessionConfirms.push(path)
      if (afterConfirm) current = afterConfirm
      return route.fulfill({ json: { status: 'confirmed', tx_hash: TX_HASH } })
    }
    if (path.startsWith('/api/wallet/')) return route.fulfill({ json: current })
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: OWNER, wallet_chains: [SEPOLIA] } })
      case '/api/passkeys':
        return route.fulfill({
          json: {
            passkeys: [
              passkey(),
              ...registered.map(made => passkey({ key_id: NEW_KEY_ID, credential_id: made.credentialId, device_name: made.deviceName })),
            ],
          },
        })
      case '/api/passkeys/register/options':
        return route.fulfill({ json: { options: REGISTRATION_OPTIONS } })
      case '/api/passkeys/register': {
        const body = request.postDataJSON() as { credential: { id: string; type: string }; device_name: string }
        registered.push({ credentialId: body.credential.id, deviceName: body.device_name })
        return route.fulfill({
          json: {
            ...passkey({ key_id: NEW_KEY_ID, credential_id: body.credential.id, device_name: body.device_name }),
            qx: `0x${'01'.repeat(32)}`,
            qy: `0x${'02'.repeat(32)}`,
          },
        })
      }
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
                default_spenders: [V3_ROUTER, POSITION_MANAGER],
              },
            ],
          },
        })
      case '/api/tokens':
        return route.fulfill({
          json: {
            tokens: [
              { ticker: 'usdc', address: USDC },
              // As the API flags it: the wrapped native token always counts, like ETH.
              { ticker: 'weth', address: WETH, always_counted: true },
            ],
          },
        })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return { prepared, sent, sessionConfirms, registered }
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

  // This wallet predates WETH always counting; counting it tightens the limit, so it goes straight through.
  await expect(page.getByText('Not counted yet')).toBeVisible()
  await page.getByRole('button', { name: 'Count WETH toward the limit' }).click()
  await expect(page.getByText('WETH now counts toward your limit.')).toBeVisible()

  await page.getByRole('button', { name: 'Advanced' }).click()
  // It predates Uniswap V3 too, so it's offered the spenders new wallets start with.
  await expect(page.getByRole('button', { name: 'Trust Uniswap V3 positions' })).toBeVisible()
  await expect(page.getByLabel('Most per operation')).toHaveValue('0.01')
  await expect(page.getByText('Contract allowlist')).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-advanced')

  expect(prepared.map(p => p.body)).toEqual([
    { chain_id: SEPOLIA, daily_limit_usd: 500 },
    { chain_id: SEPOLIA, token: 'weth', action: 'add' },
  ])
})

test('turning the contract allowlist on', async ({ page }, testInfo) => {
  // Three full-page photos of the tallest page, which takes about 16s alone on a phone.
  test.setTimeout(60_000)
  const wallet = walletState(SEPOLIA, ADDRESS)
  const on = { ...wallet, limits: { ...wallet.limits, allowlist_enabled: true } }
  const { prepared } = await mockServer(page, wallet, on, {
    allowlistAfterConfirm: { enabled: true, targets: ALLOWLIST_SUGGESTED, suggested: [] },
  })
  await page.goto('/controls')
  await connectOwner(page)

  await page.getByRole('button', { name: 'Advanced' }).click()
  const row = page.locator('.mf-control-row').filter({ has: page.getByRole('heading', { name: 'Contract allowlist' }) })
  // What the assistant uses comes ticked, so one signature keeps it all working.
  await expect(row.getByRole('checkbox', { name: /USDC/ })).toBeChecked()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-allowlist-off')

  await row.getByRole('button', { name: 'Turn on', exact: true }).click()
  const confirm = page.getByRole('alertdialog')
  await expect(confirm).toContainText('The assistant will only be able to call these 3 contracts')
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-allowlist-confirm')
  await confirm.getByRole('button', { name: 'Turn on', exact: true }).click()

  await expect(page.getByText('Contract allowlist on. The assistant can only call the contracts listed.')).toBeVisible()
  await expect(row.getByRole('list', { name: 'Allowed contracts' }).getByRole('listitem')).toHaveCount(3)
  await expect(row.getByRole('button', { name: 'Turn off', exact: true })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-allowlist-on')

  expect(prepared.map(p => p.body)).toEqual([
    { chain_id: SEPOLIA, action: 'enable', targets: [V3_ROUTER, USDC, REGISTRY] },
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

test('renewing a device before it runs out', async ({ page }, testInfo) => {
  const expiring = walletState(SEPOLIA, ADDRESS, { session: sessionBlock([sessionKey(2)]) })
  const { prepared, sessionConfirms } = await mockServer(page, expiring, walletState(SEPOLIA, ADDRESS))
  await page.goto('/controls')
  await connectOwner(page)

  const row = page.locator('.mf-control-row').filter({ has: page.getByRole('heading', { name: 'iPhone', exact: true }) })
  await expect(row.getByText('Expires soon')).toBeVisible()
  await expect(row.getByRole('button', { name: 'Renew iPhone' })).toBeVisible()
  await expect(row.getByRole('button', { name: 'Revoke iPhone' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-device-expiring')

  await row.getByRole('button', { name: 'Renew iPhone' }).click()
  const confirm = page.getByRole('alertdialog')
  await expect(confirm).toContainText('iPhone keeps approving the assistant\'s payments from this wallet for another 30 days')
  await confirm.getByRole('button', { name: 'Renew' }).click()
  await expect(page.getByText(/^iPhone renewed until \w{3} \d{1,2}, \d{4}\.$/)).toBeVisible()
  await expect(row.getByText('Active', { exact: true })).toBeVisible()

  expect(prepared).toEqual([
    {
      path: '/api/wallet/session/prepare',
      body: { chain_id: SEPOLIA, action: 'add', key_id: PASSKEY_ID, ttl_secs: 30 * 86_400 },
    },
  ])
  expect(sessionConfirms).toHaveLength(1)
})

test('adding this device: a real passkey is made, then the owner wallet adds it', async ({ page }, testInfo) => {
  const authenticator = await addPasskeyAuthenticator(page)
  const wallet = walletState(SEPOLIA, ADDRESS)
  // Once added, the wallet trusts the iPhone and the new passkey, whatever the browser named it.
  let made = ''
  const server = await mockServer(page, wallet)
  await page.route(
    url => url.pathname === `/api/wallet/${SEPOLIA}`,
    route =>
      route.fulfill({
        json: made
          ? walletState(SEPOLIA, ADDRESS, {
              session: sessionBlock([
                sessionKey(),
                sessionKey(30, { key_id: NEW_KEY_ID, device_name: 'Work laptop', credential_id: made }),
              ]),
            })
          : wallet,
      }),
  )
  await page.goto('/controls')
  await connectOwner(page)

  const name = page.getByLabel('Name for this device')
  await name.fill('Work laptop')
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-devices')
  await page.getByRole('button', { name: 'Add this device', exact: true }).click()
  const confirm = page.getByRole('alertdialog')
  await expect(confirm).toContainText('Your browser will first ask you to create a passkey.')
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'controls-add-device')
  await confirm.getByRole('button', { name: 'Create passkey' }).click()

  await expect(page.getByText(/^Work laptop can approve payments on Sepolia until/)).toBeVisible()
  const held = await authenticator.credentials()
  expect(held).toHaveLength(1)
  made = held[0]
  expect(server.registered).toEqual([{ credentialId: made, deviceName: 'Work laptop' }])
  expect(server.prepared.map(p => p.body)).toEqual([
    { chain_id: SEPOLIA, action: 'add', key_id: NEW_KEY_ID, ttl_secs: 30 * 86_400 },
  ])
  // Read again once confirmed: the list shows the new device as this one.
  await page.reload()
  const laptop = page.locator('.mf-control-row').filter({ has: page.getByRole('heading', { name: 'Work laptop' }) })
  await expect(laptop.getByText('This device')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add this device', exact: true })).toHaveCount(0)
})
