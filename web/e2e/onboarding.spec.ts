import { expect, test, type Page } from '@playwright/test'
import { installFakeWallet, type WalletTx } from './fakeWallet.ts'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN, walletState } from './helpers.ts'
import { walkOnboarding } from './onboardingFlow.ts'

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
  // As the API flags it: the wrapped native token always counts, like ETH.
  { ticker: 'weth', address: '0x4444444444444444444444444444444444444444', always_counted: true },
]
// Every token the app offers on mainnet: the longest list any network has.
const MAINNET_TICKERS = 'aave ape arb bnb comp crv dai ens imx link oneinch sand sushi uni usdc usdt wbtc weth wtao yfi'
const MAINNET_TOKENS = MAINNET_TICKERS.split(' ').map((ticker, i) => ({
  ticker,
  address: `0x${(i + 1).toString(16).padStart(40, '0')}`,
}))
const PREPARED_TX = {
  to: '0x6666666666666666666666666666666666666666',
  data: '0xdeadbeef',
  value: '0xde0b6b3a7640000',
  gas: '0x7a120',
}

interface Recorded {
  deploy: unknown[]
  confirms: number
  /** Messages the wallet was asked to sign: none, as signing in already proved the wallet. */
  signed: string[]
  sent: { tx: WalletTx; chainId: number }[]
}

/** Signed in as WALLET, the fake wallet's address, with no Mitfah wallet yet. */
async function mockServer(page: Page, tokens = TOKENS): Promise<Recorded> {
  const recorded: Recorded = { deploy: [], confirms: 0, signed: [], sent: [] }
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
      case '/api/deploy':
        recorded.deploy.push(body)
        return route.fulfill({
          json: {
            chain_id: SEPOLIA,
            predicted_address: PREDICTED,
            session_key: '0x5555555555555555555555555555555555555555',
            watched_tokens: ['usdc'],
            tx: PREPARED_TX,
          },
        })
      case '/api/deploy/confirm':
        recorded.confirms += 1
        if (recorded.confirms === 1) return route.fulfill({ status: 202, json: { status: 'pending', tx_hash: TX_HASH } })
        me = { ...me, wallet_chains: [body.chain_id] }
        return route.fulfill({
          json: {
            status: 'deployed',
            chain_id: body.chain_id,
            wallet_address: PREDICTED,
            session_key: '0x5555555555555555555555555555555555555555',
            session_key_authorized: true,
          },
        })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return recorded
}

test('creating a wallet, step by step', async ({ page }, testInfo) => {
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
      await step.getByLabel('Spending limit').fill('250')
      await step.getByText('7 days').click()
      // WETH is ticked for good; USDC can be unticked.
      await expect(step.getByRole('checkbox', { name: 'WETH' })).toBeDisabled()
      await step.getByText('USDC').click()
    },
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
      watched_tokens: [TOKENS[1]],
      prefund_eth: '1',
      session_ttl_secs: 30 * 86_400,
    },
  ])
  expect(recorded.sent).toEqual([{ tx: { from: WALLET, ...PREPARED_TX }, chainId: SEPOLIA }])
  expect(recorded.confirms).toBe(2)
})

test("a network's whole token list wraps into columns, every ticker on screen", async ({ page }, testInfo) => {
  // RSuite's inline CheckboxGroup is a flex row that never wraps: mainnet's twenty tokens once ran
  // off the edge and were clipped, all but five of them on a phone.
  await mockServer(page, MAINNET_TOKENS)
  await page.goto('/onboarding')

  await walkOnboarding(page, {
    editLimits: async step => {
      const items = step.locator('.rs-checkbox')
      await expect(items).toHaveCount(MAINNET_TOKENS.length)
      const group = (await step.locator('.rs-checkbox-group').boundingBox())!
      const boxes = await items.evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON() as DOMRect))
      for (const [i, box] of boxes.entries()) {
        const ticker = MAINNET_TOKENS[i].ticker
        expect(box.left, `${ticker} starts inside the list`).toBeGreaterThanOrEqual(group.x - 0.5)
        expect(box.right, `${ticker} ends inside the list`).toBeLessThanOrEqual(group.x + group.width + 0.5)
      }
      // Columns: every row starts its tickers where the first row does.
      const firstRowTop = boxes[0].top
      const columns = boxes.filter(box => Math.abs(box.top - firstRowTop) < 1).map(box => Math.round(box.left))
      expect(columns.length).toBeGreaterThan(1)
      for (const box of boxes) expect(columns).toContain(Math.round(box.left))
      await expectNoSidewaysScroll(page)
      await snap(page, testInfo, 'onboarding-token-grid')
    },
  })
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
