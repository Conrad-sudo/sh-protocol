import { expect, type APIRequestContext, type Page } from '@playwright/test'
import {
  createPublicClient,
  createTestClient,
  createWalletClient,
  http,
  parseEther,
  type Address,
  type Hex,
  type PrivateKeyAccount,
} from 'viem'
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts'
import { sepolia } from 'viem/chains'
import { installFakeWallet } from '../fakeWallet.ts'
import { walkOnboarding } from '../onboardingFlow.ts'

/*
 * Shared by the real-fork specs: they sign and send real transactions on the local Sepolia fork
 * and write to wallet.db, so they need Vault, `make sepolia-fork` and the API with APP_FORK_MODE=1.
 */

export const RPC = 'http://127.0.0.1:8545'
const transport = http(RPC)
export const publicClient = createPublicClient({ chain: sepolia, transport })
export const testClient = createTestClient({ chain: sepolia, mode: 'anvil', transport })

/**
 * Refuses to go on anywhere but a local fork, like app/tests/test_e2e_fork.py. A Sepolia fork and live
 * Sepolia report the same chain id; only a local node accepts anvil_setBalance, so a balance that
 * really changed is proof of where the transaction would go. The API must be serving Sepolia from
 * a fork too, or it would build and confirm against the live network.
 */
export async function requireLocalFork(request: APIRequestContext) {
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

export interface RealOwner {
  account: PrivateKeyAccount
  /** The network each transaction the page asked for was sent on, in order. */
  sentOn: number[]
}

/**
 * A fresh owner with 100 ETH, installed as the page's browser wallet. Fresh every run: well-known
 * anvil keys carry EIP-7702 code on a Sepolia fork, and a new owner has its own deployCount, so no
 * other test can move the predicted address. Call before the first `page.goto`.
 */
export async function installRealOwner(page: Page): Promise<RealOwner> {
  const account = privateKeyToAccount(generatePrivateKey())
  await testClient.setBalance({ address: account.address, value: parseEther('100') })
  const walletClient = createWalletClient({ account, chain: sepolia, transport })
  const sentOn: number[] = []

  await installFakeWallet(page, {
    address: account.address,
    chainId: sepolia.id,
    signMessage: message => account.signMessage({ message: { raw: message as Hex } }),
    sendTransaction: (tx, chainId) => {
      sentOn.push(chainId)
      return walletClient.sendTransaction({
        to: tx.to as Address,
        data: tx.data as Hex,
        value: tx.value ? BigInt(tx.value) : 0n,
        gas: tx.gas ? BigInt(tx.gas) : undefined,
      })
    },
    // Reads the app sends through the wallet (a funding receipt) go to the fork, as a real wallet
    // pointed at it would send them.
    request: (method, params) => publicClient.request({ method, params } as never),
  })
  return { account, sentOn }
}

/** The API's view of the wallet, as GET /api/wallet/{chain_id} reports it. */
export interface ApiWallet {
  address: Address
  owner: Address
  is_owner: boolean
  paused: boolean
  spending: { daily_limit_usd: number; window_hours: number; watched_tokens: { ticker: string | null; address: Address }[] }
  session: { key: Address | null; wallet_key: Address | null; is_app_key: boolean; active: boolean; expires_at: number | null }
}

export interface Account {
  email: string
  password: string
  /** Reads the wallet through the API as this user. */
  readWallet: () => Promise<ApiWallet>
}

/**
 * Signs up a new account and walks onboarding with the defaults: Sepolia, $100 a day, every token,
 * 1 ETH of gas funds. Returns once the dashboard shows the new wallet.
 */
export async function signUpAndDeploy(page: Page, request: APIRequestContext, prefix: string): Promise<Account> {
  const email = `${prefix}-${Date.now()}@example.com`
  const password = 'Onboard-e2e-2026'
  await page.goto('/signup?next=%2Fonboarding')
  await page.getByLabel('Email').fill(email)
  await page.locator('input[name="password"]').fill(password)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('heading', { name: 'Create your wallet' })).toBeVisible()

  await walkOnboarding(page)
  await expect(page.getByText('Your Mitfah wallet on Sepolia')).toBeVisible({ timeout: 120_000 })

  const readWallet = async () => {
    const login = await request.post('/api/auth/login', { data: { email, password } })
    expect(login.ok()).toBeTruthy()
    const { access_token } = (await login.json()) as { access_token: string }
    const response = await request.get(`/api/wallet/${sepolia.id}`, {
      headers: { Authorization: `Bearer ${access_token}` },
    })
    expect(response.ok()).toBeTruthy()
    return (await response.json()) as ApiWallet
  }
  return { email, password, readWallet }
}
