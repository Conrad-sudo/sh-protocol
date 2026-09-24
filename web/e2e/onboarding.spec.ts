import { expect, test, type Page } from '@playwright/test'
import { hexToString } from 'viem'
import { parseSiweMessage } from 'viem/siwe'
import { installFakeWallet, type WalletTx } from './fakeWallet.ts'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN, walletState } from './helpers.ts'
import { walkOnboarding } from './onboardingFlow.ts'

/*
 * Creating a wallet at real screen sizes, in both themes, with a fake browser wallet. The API is
 * mocked, so nothing is signed for real and nothing reaches wallet.db or a chain.
 */

const SEPOLIA = 11155111
const WALLET = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const NONCE = 'k3n9v2x8q1w7'
const SIGNATURE = `0x${'ab'.repeat(65)}`
const TX_HASH = `0x${'12'.repeat(32)}`
const PREDICTED = '0x2222222222222222222222222222222222222222'
const TOKENS = [
  { ticker: 'usdc', address: '0x3333333333333333333333333333333333333333' },
  { ticker: 'weth', address: '0x4444444444444444444444444444444444444444' },
]
const PREPARED_TX = {
  to: '0x6666666666666666666666666666666666666666',
  data: '0xdeadbeef',
  value: '0xde0b6b3a7640000',
  gas: '0x7a120',
}

interface Recorded {
  siwe: { message: string; nonce: string; signature: string }[]
  deploy: unknown[]
  confirms: number
  signed: string[]
  sent: { tx: WalletTx; chainId: number }[]
}

async function mockServer(page: Page): Promise<Recorded> {
  const recorded: Recorded = { siwe: [], deploy: [], confirms: 0, signed: [], sent: [] }
  let me = { ...ME }

  await installFakeWallet(page, {
    address: WALLET,
    chainId: SEPOLIA,
    signMessage: async message => {
      recorded.signed.push(hexToString(message as `0x${string}`))
      return SIGNATURE
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
      case '/api/auth/siwe/nonce':
        return route.fulfill({ json: { nonce: NONCE } })
      case '/api/auth/siwe/verify':
        recorded.siwe.push(body)
        me = { ...me, owner_addr: WALLET }
        return route.fulfill({ json: { owner_addr: WALLET } })
      case '/api/tokens':
        return route.fulfill({ json: { tokens: TOKENS } })
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
      await step.getByText('WETH').click()
    },
  })

  // The API answers "pending" once, so the progress view shows for about two seconds.
  await expect(page.getByRole('status').filter({ hasText: 'Creating your wallet on Sepolia' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'onboarding-creating')

  await expect(page).toHaveURL(/\/dashboard$/)
  await expect(page.getByText('Your Mitfah wallet on Sepolia')).toBeVisible()
  await expect(page.getByText('Your wallet is ready on Sepolia.')).toBeVisible()
  await snap(page, testInfo, 'onboarding-done')

  // What the wallet was asked to sign, and what the API was sent.
  expect(recorded.signed).toHaveLength(1)
  expect(recorded.siwe).toEqual([{ message: recorded.signed[0], nonce: NONCE, signature: SIGNATURE }])
  expect(parseSiweMessage(recorded.signed[0])).toMatchObject({
    domain: 'localhost:3000',
    uri: 'http://localhost:3000',
    nonce: NONCE,
    address: WALLET,
    chainId: SEPOLIA,
  })
  expect(recorded.deploy).toEqual([
    {
      chain_id: SEPOLIA,
      deployer: WALLET,
      daily_limit_usd: 250,
      window_secs: 604_800,
      watched_tokens: [TOKENS[0]],
      prefund_eth: '1',
      session_ttl_secs: 30 * 86_400,
    },
  ])
  expect(recorded.sent).toEqual([{ tx: { from: WALLET, ...PREPARED_TX }, chainId: SEPOLIA }])
  expect(recorded.confirms).toBe(2)
})

test('choosing a network the wallet is not on asks it to switch', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('desktop'), 'Layout is covered above; one screen size is enough here.')
  const recorded = await mockServer(page)
  await page.goto('/onboarding')

  await walkOnboarding(page, { network: /BNB Smart Chain/ })

  await expect(page.getByText('Your Mitfah wallet on BNB Smart Chain')).toBeVisible()
  expect(recorded.deploy).toEqual([expect.objectContaining({ chain_id: 56 })])
  // Sent on the network the user picked, which the wallet was switched to on the way.
  expect(recorded.sent).toEqual([expect.objectContaining({ chainId: 56 })])
})
