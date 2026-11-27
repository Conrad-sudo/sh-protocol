import { AxeBuilder } from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { installFakeWallet, type WalletTx } from './fakeWallet.ts'
import { addPasskeyAuthenticator, expectNoSidewaysScroll, expectTheme, isApiUrl, ME, passkey, snap, TOKEN, walletState } from './helpers.ts'
import { closeTokenPicker, countNative, TOKEN_PICKER, walkOnboarding } from './onboardingFlow.ts'

/*
 * Creating a wallet at real screen sizes, in both themes, with a fake browser wallet. The API is
 * mocked, so nothing is signed for real and nothing reaches wallet.db or a chain.
 */

const SEPOLIA = 11155111
const WALLET = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const TX_HASH = `0x${'12'.repeat(32)}`
const PREDICTED = '0x2222222222222222222222222222222222222222'
const TOKENS = [
  { ticker: 'usdc', address: '0x3333333333333333333333333333333333333333' },
  // As the API flags it: the wrapped native token counts exactly when ETH does, so it is never picked.
  { ticker: 'weth', address: '0x4444444444444444444444444444444444444444', follows_native: true },
]
// Every token the app offers on mainnet: the longest list any network has.
const MAINNET_TICKERS = 'aave ape arb bnb comp crv dai ens imx link oneinch sand sushi uni usdc usdt wbtc weth wtao yfi'
const MAINNET_TOKENS = MAINNET_TICKERS.split(' ').map((ticker, i) => ({
  ticker,
  address: `0x${(i + 1).toString(16).padStart(40, '0')}`,
  follows_native: ticker === 'weth',
}))
const MAINNET_CHOOSABLE = MAINNET_TOKENS.filter(token => !token.follows_native)
/** The key id the server gives the passkey made during onboarding. */
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
const PREPARED_TX = {
  to: '0x6666666666666666666666666666666666666666',
  data: '0xdeadbeef',
  value: '0xde0b6b3a7640000',
  gas: '0x7a120',
}

interface Recorded {
  deploy: Record<string, unknown>[]
  /** Passkeys saved by POST /api/passkeys/register: credential id and device name. */
  registered: { credentialId: string; deviceName: string }[]
  confirms: number
  /** Messages the wallet was asked to sign: none, as signing in already proved the wallet. */
  signed: string[]
  sent: { tx: WalletTx; chainId: number }[]
}

/** Signed in as WALLET, the fake wallet's address, with no Mitfah wallet yet. */
async function mockServer(page: Page, tokens = TOKENS): Promise<Recorded> {
  const recorded: Recorded = { deploy: [], registered: [], confirms: 0, signed: [], sent: [] }
  let me = { ...ME, owner_addr: WALLET }

  await installFakeWallet(page, {
    address: WALLET,
    chainId: SEPOLIA,
    signMessage: async message => {
      recorded.signed.push(message)
      throw new Error('creating a wallet signs no message')
    },
    sendTransaction: async (tx, chainId) => {
      recorded.sent.push({ tx, chainId })
      return TX_HASH
    },
  })

  await page.route(isApiUrl, route => {
    const request = route.request()
    const body = request.postDataJSON()
    const path = new URL(request.url()).pathname
    if (path.startsWith('/api/wallet/')) {
      const chainId = Number(path.split('/').at(-1))
      return me.wallet_chains.includes(chainId)
        ? route.fulfill({ json: walletState(chainId, PREDICTED, { owner: WALLET }) })
        : route.fulfill({ status: 404, json: { detail: 'No wallet on that chain.' } })
    }
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: me })
      case '/api/chains':
        return route.fulfill({
          json: {
            chains: [
              { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: true },
              { chain_id: 1, name: 'mainnet', native_ticker: 'ETH', fork: true },
              { chain_id: 56, name: 'bsc', native_ticker: 'BNB', fork: true },
            ],
          },
        })
      case '/api/tokens':
        return route.fulfill({ json: { tokens } })
      case '/api/passkeys':
        return route.fulfill({
          json: {
            passkeys: recorded.registered.map(made =>
              passkey({ key_id: NEW_KEY_ID, credential_id: made.credentialId, device_name: made.deviceName }),
            ),
          },
        })
      case '/api/passkeys/register/options':
        return route.fulfill({ json: { options: REGISTRATION_OPTIONS } })
      case '/api/passkeys/register':
        recorded.registered.push({ credentialId: body.credential.id, deviceName: body.device_name })
        return route.fulfill({
          json: {
            ...passkey({ key_id: NEW_KEY_ID, credential_id: body.credential.id, device_name: body.device_name }),
            qx: `0x${'01'.repeat(32)}`,
            qy: `0x${'02'.repeat(32)}`,
          },
        })
      case '/api/deploy':
        recorded.deploy.push(body)
        return route.fulfill({
          json: {
            chain_id: SEPOLIA,
            predicted_address: PREDICTED,
            key_id: body.key_id,
            watched_tokens: ['usdc'],
            tx: PREPARED_TX,
          },
        })
      case '/api/deploy/confirm':
        recorded.confirms += 1
        if (recorded.confirms === 1) return route.fulfill({ status: 202, json: { status: 'pending', tx_hash: TX_HASH } })
        me = { ...me, wallet_chains: [body.chain_id] }
        {
          const keyId = (recorded.deploy.at(-1)?.key_id as string | null | undefined) ?? null
          return route.fulfill({
            json: {
              status: 'deployed',
              chain_id: body.chain_id,
              wallet_address: PREDICTED,
              key_id: keyId,
              session_key_authorized: keyId !== null,
              session_key_expires_at: keyId === null ? 0 : Math.floor(Date.now() / 1000) + 90 * 86_400,
            },
          })
        }
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return recorded
}

test('creating a wallet, step by step', async ({ page }, testInfo) => {
  const authenticator = await addPasskeyAuthenticator(page)
  const recorded = await mockServer(page)
  await page.goto('/onboarding')
  await expect(page.getByRole('heading', { name: 'Create your wallet' })).toBeVisible()
  await expectTheme(page, testInfo)

  await walkOnboarding(page, {
    onStep: async name => {
      await expectNoSidewaysScroll(page)
      await snap(page, testInfo, `onboarding-${name}`)
    },
    editLimits: async step => {
      // Nothing counts to begin with, ETH included, so there is no limit to set until something does.
      await expect(step.getByLabel('Spending limit', { exact: true })).toHaveCount(0)
      await expect(step.getByText(/You can set one later in Controls\./)).toBeVisible()
      await countNative(step)
      await expect(step.getByText('WETH counts with it.')).toBeVisible()
      await step.getByLabel('Spending limit', { exact: true }).fill('250')
      await expect(step.getByText('1 month')).toBeVisible()
      await step.getByText('7 days').click()
      await expectNoSidewaysScroll(page)
      await snap(page, testInfo, 'onboarding-limits-counting')
      // Nothing starts ticked, and WETH isn't offered: it goes with ETH.
      await step.getByRole('combobox', { name: TOKEN_PICKER }).click()
      const usdc = page.getByRole('option', { name: 'USDC' })
      await expect(usdc).toHaveAttribute('aria-selected', 'false')
      await expect(page.getByRole('option', { name: 'WETH' })).toHaveCount(0)
      await expectNoSidewaysScroll(page)
      await snap(page, testInfo, 'onboarding-token-picker')
      await usdc.getByText('USDC').click()
      await expect(usdc).toHaveAttribute('aria-selected', 'true')
      await closeTokenPicker(page)
    },
    passkey: { deviceName: 'Work laptop' },
  })

  // The API answers "pending" once, so the progress view shows for about two seconds.
  await expect(page.getByRole('status').filter({ hasText: 'Creating your wallet on Sepolia' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'onboarding-creating')

  await expect(page).toHaveURL(/\/dashboard$/)
  await expect(page.getByText('Your Mitfah smart wallet on Sepolia')).toBeVisible()
  await expect(page.getByText(/^Your wallet is ready on Sepolia\. You can now disconnect your browser wallet/)).toBeVisible()
  await snap(page, testInfo, 'onboarding-done')

  // Signing in proved the wallet, so it was only asked to send the deploy; and what the API was sent.
  expect(recorded.signed).toEqual([])
  expect(recorded.deploy).toEqual([
    {
      chain_id: SEPOLIA,
      deployer: WALLET,
      daily_limit_usd: 250,
      window_secs: 604_800,
      // Only the pick: the API puts WETH on the list itself, because ETH counts.
      watched_tokens: [TOKENS[0]],
      count_native: true,
      prefund_eth: '1',
      key_id: NEW_KEY_ID,
      session_ttl_secs: 90 * 86_400,
    },
  ])
  // The passkey the browser really made, saved under the name given.
  expect(recorded.registered).toEqual([{ credentialId: (await authenticator.credentials())[0], deviceName: 'Work laptop' }])
  expect(recorded.sent).toEqual([{ tx: { from: WALLET, ...PREPARED_TX }, chainId: SEPOLIA }])
  expect(recorded.confirms).toBe(2)
})

test("a network's whole token list is in the dropdown, every ticker reachable", async ({ page }, testInfo) => {
  await mockServer(page, MAINNET_TOKENS)
  await page.goto('/onboarding')

  await walkOnboarding(page, {
    editLimits: async step => {
      await step.getByRole('combobox', { name: TOKEN_PICKER }).click()
      const options = page.getByRole('option')
      await expect(options).toHaveCount(MAINNET_CHOOSABLE.length)
      // WETH goes with ETH on mainnet, so it's left out. BNB is an ordinary token there, so it isn't.
      await expect(page.getByRole('option', { name: 'WETH' })).toHaveCount(0)
      await expect(page.getByRole('option', { name: 'BNB' })).toBeAttached()
      // Tapping the list must not bring up a phone's keyboard over it.
      await expect(page.getByPlaceholder('Search')).not.toBeFocused()
      await snap(page, testInfo, 'onboarding-token-list-open')
      await expect(page.getByPlaceholder('Search')).toBeInViewport({ ratio: 1 })
      await expect(page.getByRole('option', { name: 'AAVE' })).toBeInViewport({ ratio: 1 })
      // On a phone it's a sheet from the bottom; elsewhere it opens upward rather than off the screen.
      if (!testInfo.project.name.startsWith('phone')) {
        await expect(page.locator('.rs-picker-popup')).toBeInViewport({ ratio: 1 })
      }
      const { width } = page.viewportSize()!
      for (const { ticker } of MAINNET_CHOOSABLE) {
        const option = page.getByRole('option', { name: ticker.toUpperCase(), exact: true })
        await option.scrollIntoViewIfNeeded()
        await expect(option, `${ticker} is on screen`).toBeInViewport()
        const box = (await option.boundingBox())!
        expect(box.x, `${ticker} starts on screen`).toBeGreaterThanOrEqual(0)
        expect(box.x + box.width, `${ticker} ends on screen`).toBeLessThanOrEqual(width)
      }
      await expectNoSidewaysScroll(page)
      await snap(page, testInfo, 'onboarding-token-list')
      // RSuite draws a checkbox inside each role="option" of a CheckPicker (nested-interactive). It
      // can't be changed from here, and the option itself carries the state and takes the keyboard:
      // see the keyboard test below.
      const { violations } = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .disableRules(['nested-interactive'])
        .analyze()
      expect(
        violations
          .filter(violation => violation.impact === 'serious' || violation.impact === 'critical')
          .map(violation => `${violation.id} (${violation.impact}): ${violation.nodes.map(node => node.target).join(' · ')}`),
      ).toEqual([])
      await closeTokenPicker(page)
    },
  })
})

test('the token dropdown works from the keyboard', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('desktop'), 'A keyboard test; one screen size is enough here.')
  const recorded = await mockServer(page)
  await page.goto('/onboarding')

  await walkOnboarding(page, {
    editLimits: async step => {
      await step.getByRole('combobox', { name: TOKEN_PICKER }).focus()
      await page.keyboard.press('Enter')
      const usdc = page.getByRole('option', { name: 'USDC' })
      await expect(usdc).toBeVisible()

      await page.keyboard.press('ArrowDown')
      await page.keyboard.press('Enter')
      await expect(usdc).toHaveAttribute('aria-selected', 'true')

      await page.keyboard.press('Escape')
      await expect(page.getByRole('listbox')).toBeHidden()
    },
  })

  await expect.poll(() => recorded.deploy).toHaveLength(1)
  // A token ticked with ETH left out: that token alone counts, against the $100 the field starts with.
  expect(recorded.deploy[0]).toMatchObject({ watched_tokens: [TOKENS[0]], count_native: false, daily_limit_usd: 100 })
})

test('choosing a network the wallet is not on asks it to switch', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('desktop'), 'Layout is covered above; one screen size is enough here.')
  const recorded = await mockServer(page)
  await page.goto('/onboarding')

  await walkOnboarding(page, { network: /BNB Smart Chain/ })

  await expect(page.getByText('Your Mitfah smart wallet on BNB Smart Chain')).toBeVisible()
  expect(recorded.deploy).toEqual([expect.objectContaining({ chain_id: 56 })])
  // Sent on the network the user picked, which the wallet was switched to on the way.
  expect(recorded.sent).toEqual([expect.objectContaining({ chainId: 56 })])
})
