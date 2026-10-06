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
import { createSiweMessage } from 'viem/siwe'
import { FAKE_WALLET_NAME, installFakeWallet } from '../fakeWallet.ts'
import { walkOnboarding } from '../onboardingFlow.ts'

/*
 * Shared by the real-fork specs: they sign and send real transactions on the local Sepolia fork
 * and write to wallet.db, so they need Vault, `make sepolia-fork` and the API with APP_FORK_MODE=1.
 */

export const RPC = 'http://127.0.0.1:8545'
/** The dev server's host, which a SIWE message must name (the API's SIWE_DOMAIN defaults to it). */
const SITE = 'localhost:3000'
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
    signTypedData: typedData => account.signTypedData(JSON.parse(typedData)),
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
  /** Reads the wallet through the API as this user. */
  readWallet: () => Promise<ApiWallet>
}

/**
 * Signs in to the API as `account` the way the page does — a SIWE message, signed here in Node —
 * and returns the headers for its calls. The first sign-in for an address creates its account.
 */
export async function apiSignIn(request: APIRequestContext, account: PrivateKeyAccount) {
  const { nonce } = (await (await request.get('/api/auth/siwe/nonce')).json()) as { nonce: string }
  const message = createSiweMessage({
    domain: SITE,
    address: account.address,
    uri: `http://${SITE}`,
    version: '1',
    chainId: sepolia.id,
    nonce,
  })
  const signature = await account.signMessage({ message })
  const response = await request.post('/api/auth/siwe/login', { data: { message, signature, nonce } })
  expect(response.ok(), await response.text()).toBeTruthy()
  const { access_token } = (await response.json()) as { access_token: string }
  return { Authorization: `Bearer ${access_token}` }
}

/**
 * Signs in through the page with the installed wallet, then lands on `next`. The button says
 * "Sign up" for an address the API doesn't know yet and "Sign in" for one it does.
 */
export async function signIn(page: Page, next: string) {
  await page.goto(`/login?next=${encodeURIComponent(next)}`)
  await page.getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()
  await page.getByRole('button', { name: /^Sign (in|up)$/ }).click()
  await page.waitForURL(url => url.pathname === next)
}

/**
 * Signs in as a fresh owner and walks onboarding with the defaults: Sepolia, $100 a day, every
 * token, a 1 ETH prefund. Returns once the dashboard shows the new wallet.
 */
export async function signInAndDeploy(page: Page, request: APIRequestContext, owner: RealOwner): Promise<Account> {
  await signIn(page, '/onboarding')
  await expect(page.getByRole('heading', { name: 'Create your wallet' })).toBeVisible()

  await walkOnboarding(page)
  await expect(page.getByText('Your Mitfah smart wallet on Sepolia')).toBeVisible({ timeout: 120_000 })

  // Signed in once: sign-in is rate limited, and the access token outlasts any one spec.
  let headers: Record<string, string> | undefined
  const readWallet = async () => {
    headers ??= await apiSignIn(request, owner.account)
    const response = await request.get(`/api/wallet/${sepolia.id}`, { headers })
    expect(response.ok()).toBeTruthy()
    return (await response.json()) as ApiWallet
  }
  return { readWallet }
}
