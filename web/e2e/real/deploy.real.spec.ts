import { expect, test, type APIRequestContext } from '@playwright/test'
import {
  createPublicClient,
  createTestClient,
  createWalletClient,
  http,
  parseAbi,
  parseEther,
  type Address,
  type Hex,
} from 'viem'
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts'
import { sepolia } from 'viem/chains'
import { formatTokenAmount } from '../../src/lib/format.ts'
import { installFakeWallet } from '../fakeWallet.ts'
import { walkOnboarding } from '../onboardingFlow.ts'

/*
 * The whole journey for real: sign up against the running API, prove a fresh key owns the account,
 * have that key sign and send deployWallet on the local Sepolia fork, then top the new wallet up
 * from the dashboard. Nothing is mocked, so
 * this writes a user and a wallet to wallet.db and needs Vault, `make sepolia-fork` and the API
 * with APP_FORK_MODE=1. Run with: E2E_REAL=1 npx playwright test real
 */

const RPC = 'http://127.0.0.1:8545'
const transport = http(RPC)
const publicClient = createPublicClient({ chain: sepolia, transport })
const testClient = createTestClient({ chain: sepolia, mode: 'anvil', transport })

/**
 * Refuses to go on anywhere but a local fork, like app/test_e2e_fork.py. A Sepolia fork and live
 * Sepolia report the same chain id; only a local node accepts anvil_setBalance, so a balance that
 * really changed is proof of where the transaction would go. The API must be serving Sepolia from
 * a fork too, or it would build and confirm against the live network.
 */
async function requireLocalFork(request: APIRequestContext) {
  const chainId = await publicClient.getChainId().catch(() => null)
  if (chainId !== sepolia.id) throw new Error(`No Sepolia fork at ${RPC} (chain ${chainId}). Run: make sepolia-fork`)

  const probe = privateKeyToAccount(generatePrivateKey()).address
  try {
    await testClient.setBalance({ address: probe, value: parseEther('1') })
  } catch (error) {
    throw new Error(`anvil_setBalance failed, so ${RPC} is not a local fork. Refusing to sign anything.`, {
      cause: error,
    })
  }
  if ((await publicClient.getBalance({ address: probe })) !== parseEther('1')) {
    throw new Error(`anvil_setBalance had no effect on ${RPC}. Refusing to sign anything.`)
  }

  const response = await request.get('/api/chains')
  expect(response.ok(), 'the API is not reachable through the dev server').toBeTruthy()
  const { chains } = (await response.json()) as { chains: { chain_id: number; fork: boolean }[] }
  if (!chains.find(chain => chain.chain_id === sepolia.id)?.fork) {
    throw new Error('The API is not serving Sepolia from a local fork (APP_FORK_MODE=1). Refusing.')
  }
}

test('a new user creates and funds a wallet on the local fork', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-light', 'One real deploy per run is enough.')
  test.setTimeout(180_000)
  await requireLocalFork(request)

  // A fresh owner every run: well-known anvil keys carry EIP-7702 code on a Sepolia fork, and a
  // new owner has its own deployCount, so no other test can move the predicted address.
  const owner = privateKeyToAccount(generatePrivateKey())
  await testClient.setBalance({ address: owner.address, value: parseEther('100') })
  const walletClient = createWalletClient({ account: owner, chain: sepolia, transport })
  const sentOn: number[] = []

  await installFakeWallet(page, {
    address: owner.address,
    chainId: sepolia.id,
    signMessage: message => owner.signMessage({ message: { raw: message as Hex } }),
    sendTransaction: (tx, chainId) => {
      sentOn.push(chainId)
      return walletClient.sendTransaction({
        to: tx.to as Address,
        data: tx.data as Hex,
        value: tx.value ? BigInt(tx.value) : 0n,
        gas: tx.gas ? BigInt(tx.gas) : undefined,
      })
    },
    // Reads the app sends through the wallet (the funding receipt) go to the fork, as a real
    // wallet pointed at it would send them.
    request: (method, params) => publicClient.request({ method, params } as never),
  })

  const email = `e2e-deploy-${Date.now()}@example.com`
  const password = 'Onboard-e2e-2026'
  await page.goto('/signup?next=%2Fonboarding')
  await page.getByLabel('Email').fill(email)
  await page.locator('input[name="password"]').fill(password)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('heading', { name: 'Create your wallet' })).toBeVisible()

  // The defaults: Sepolia (where the wallet is), $100 a day, every token, 1 ETH of gas funds.
  await walkOnboarding(page)
  await expect(page.getByText('Your Mitfah wallet on Sepolia')).toBeVisible({ timeout: 120_000 })
  expect(sentOn).toEqual([sepolia.id])
  await page.screenshot({ path: testInfo.outputPath('deployed.png') })

  // What the API recorded, read back as the same user.
  const login = await request.post('/api/auth/login', { data: { email, password } })
  expect(login.ok()).toBeTruthy()
  const { access_token } = (await login.json()) as { access_token: string }
  const walletResponse = await request.get(`/api/wallet/${sepolia.id}`, {
    headers: { Authorization: `Bearer ${access_token}` },
  })
  expect(walletResponse.ok()).toBeTruthy()
  const wallet = (await walletResponse.json()) as {
    address: Address
    owner: Address
    is_owner: boolean
    paused: boolean
    spending: { daily_limit_usd: number; window_hours: number }
  }
  expect(wallet).toMatchObject({
    owner: owner.address,
    is_owner: true,
    paused: false,
    spending: { daily_limit_usd: 100, window_hours: 24 },
  })

  // And what the chain says, without the API in between.
  const code = await publicClient.getCode({ address: wallet.address })
  expect(code?.length ?? 0).toBeGreaterThan(2)
  const onChainOwner = await publicClient.readContract({
    address: wallet.address,
    abi: parseAbi(['function owner() view returns (address)']),
    functionName: 'owner',
  })
  expect(onChainOwner).toBe(owner.address)
  expect(await publicClient.getBalance({ address: wallet.address })).toBeGreaterThanOrEqual(parseEther('1'))

  // Topping up from the dashboard with the connected wallet.
  const before = await publicClient.getBalance({ address: wallet.address })
  await page.getByRole('button', { name: 'Add funds' }).click()
  const drawer = page.getByRole('dialog').filter({ hasText: 'Add funds' })
  await drawer.getByLabel('Amount').fill('0.5')
  await drawer.getByRole('button', { name: 'Send' }).click()
  await expect(drawer.getByText('Received. Your balance is up to date.')).toBeVisible({ timeout: 60_000 })
  const after = await publicClient.getBalance({ address: wallet.address })
  expect(after - before).toBe(parseEther('0.5'))
  expect(sentOn).toEqual([sepolia.id, sepolia.id])

  // The dashboard shows the new balance, read back through the API.
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  const ethRow = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: 'ETH', exact: true }) })
  await expect(ethRow).toContainText(formatTokenAmount(after.toString(), 18))
  await page.screenshot({ path: testInfo.outputPath('funded.png') })
})
