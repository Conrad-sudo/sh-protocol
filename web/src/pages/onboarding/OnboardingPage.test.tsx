import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { parseSiweMessage } from 'viem/siwe'
import { resetClientForTests } from '../../api/client'
import { routes } from '../../routes'
import { makeWalletState } from '../../test/fixtures'
import { answerRpc, isRpc, json, makeWagmiConfig, ME, renderRoutes, setViewportWidth, TOKEN, WALLET } from '../../test/utils'

// Polling waits 2 s between checks; the tests don't.
vi.mock('../../lib/tx', async importOriginal => ({
  ...(await importOriginal<typeof import('../../lib/tx')>()),
  sleep: () => Promise.resolve(),
}))

const SEPOLIA = 11155111
const NONCE = 'k3n9v2x8q1w7'
const SIGNATURE = `0x${'ab'.repeat(65)}`
const TX_HASH = `0x${'12'.repeat(32)}`
const PREDICTED = '0x2222222222222222222222222222222222222222'
// The signed-in test account is user 7 (test/utils ME/TOKEN).
const PENDING_KEY = 'mitfah-pending-deploy:7'
const TOKENS = [
  { ticker: 'usdc', address: '0x3333333333333333333333333333333333333333' },
  { ticker: 'weth', address: '0x4444444444444444444444444444444444444444' },
]
const PREPARED = {
  chain_id: SEPOLIA,
  predicted_address: PREDICTED,
  session_key: '0x5555555555555555555555555555555555555555',
  watched_tokens: ['usdc'],
  tx: { to: '0x6666666666666666666666666666666666666666', data: '0xdeadbeef', value: '0xde0b6b3a7640000', gas: '0x7a120' },
}

interface Calls {
  siwe: { message: string; signature: string; nonce: string }[]
  deploy: unknown[]
  confirm: unknown[]
  sent: Record<string, string>[]
}

/**
 * A signed-in API with no wallet yet. SIWE binds `WALLET`; confirm answers "pending" once, then
 * "deployed". The mock wallet's signatures and transactions arrive as JSON-RPC.
 */
function stubServer({ ownerAddr = null as string | null, reverted = false, unlinked = false } = {}) {
  let me = { ...ME, owner_addr: ownerAddr, wallet_chains: [] as number[] }
  const calls: Calls = { siwe: [], deploy: [], confirm: [], sent: [] }

  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (isRpc(url)) {
        return Promise.resolve(
          answerRpc(init, {
            eth_sign: () => SIGNATURE,
            eth_sendTransaction: ([tx]) => {
              calls.sent.push(tx as Record<string, string>)
              return TX_HASH
            },
          }),
        )
      }
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      switch (url) {
        case '/api/auth/refresh':
          return Promise.resolve(json(200, TOKEN))
        case '/api/me':
          return Promise.resolve(json(200, me))
        case '/api/chains':
          return Promise.resolve(
            json(200, {
              chains: [
                { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: true },
                { chain_id: 1, name: 'mainnet', native_ticker: 'ETH', fork: true },
              ],
            }),
          )
        case '/api/auth/siwe/nonce':
          return Promise.resolve(json(200, { nonce: NONCE }))
        case '/api/auth/siwe/verify':
          calls.siwe.push(body)
          me = { ...me, owner_addr: WALLET }
          return Promise.resolve(json(200, { owner_addr: WALLET }))
        case `/api/tokens?chain_id=${SEPOLIA}`:
          return Promise.resolve(json(200, { tokens: TOKENS }))
        case '/api/deploy':
          calls.deploy.push(body)
          return Promise.resolve(json(200, PREPARED))
        case '/api/deploy/confirm':
          calls.confirm.push(body)
          if (unlinked) {
            return Promise.resolve(
              json(403, {
                detail:
                  'Link your wallet address first: GET /api/auth/siwe/nonce, sign the message, then POST /api/auth/siwe/verify.',
              }),
            )
          }
          if (calls.confirm.length === 1) return Promise.resolve(json(202, { status: 'pending', tx_hash: TX_HASH }))
          if (reverted) return Promise.resolve(json(400, { detail: `deployWallet reverted (tx: ${TX_HASH})` }))
          me = { ...me, wallet_chains: [SEPOLIA] }
          return Promise.resolve(
            json(200, {
              status: 'deployed',
              chain_id: SEPOLIA,
              wallet_address: PREDICTED,
              session_key: PREPARED.session_key,
              session_key_authorized: true,
            }),
          )
        case `/api/wallet/${SEPOLIA}`:
          return Promise.resolve(
            me.wallet_chains.includes(SEPOLIA)
              ? json(200, makeWalletState({ address: PREDICTED, owner: WALLET }))
              : json(404, { detail: 'You have no wallet on this chain.' }),
          )
        default:
          return Promise.resolve(json(404, { detail: 'Not Found' }))
      }
    }),
  )
  return calls
}

const step = (name: string) => screen.findByRole('region', { name })

async function connect(user: ReturnType<typeof userEvent.setup>) {
  const region = await step('Connect the wallet that will own your Mitfah wallet')
  await user.click(within(region).getByRole('button', { name: 'Connect wallet' }))
  await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
}

describe('OnboardingPage', () => {
  beforeEach(() => {
    resetClientForTests()
    sessionStorage.clear()
    localStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('connects, verifies, and creates the wallet with the chosen settings', async () => {
    const calls = stubServer()
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/onboarding')

    await connect(user)

    // Verify: the signed message names this site and carries the server's nonce.
    const verify = await step('Prove this wallet is yours')
    expect(within(verify).getByText(/can't recover your wallet/)).toBeInTheDocument()
    await user.click(within(verify).getByRole('button', { name: 'Verify with wallet' }))
    await step('Where should your wallet live?')
    expect(calls.siwe).toHaveLength(1)
    const siwe = parseSiweMessage(calls.siwe[0].message)
    expect(siwe).toMatchObject({ domain: 'localhost:3000', nonce: NONCE, address: WALLET, chainId: SEPOLIA })
    expect(calls.siwe[0]).toMatchObject({ nonce: NONCE, signature: SIGNATURE })

    // Network: the wallet is already on Sepolia, so it is preselected.
    const network = await step('Where should your wallet live?')
    expect(within(network).getByRole('radio', { name: /Sepolia/ })).toBeChecked()
    await user.click(within(network).getByRole('button', { name: 'Continue' }))

    // Limits: $250 a week, counting USDC only.
    const limits = await step('How much may the assistant spend?')
    const limit = within(limits).getByLabelText('Spending limit')
    await user.clear(limit)
    await user.type(limit, '250')
    await user.click(within(limits).getByText('7 days'))
    await user.click(await within(limits).findByRole('checkbox', { name: 'WETH' }))
    await user.click(within(limits).getByRole('button', { name: 'Continue' }))

    // Gas funds: 1 ETH by default on a local test network.
    const fund = await step('Add funds for network fees')
    expect(within(fund).getByLabelText('Amount to send now')).toHaveValue('1')
    await user.click(within(fund).getByRole('button', { name: 'Continue' }))

    const review = await step('Check the details')
    const summary = (term: string) => within(review).getByText(term).nextElementSibling
    expect(summary('Spending limit')).toHaveTextContent('$250.00 every 7 days')
    expect(summary('Counts toward the limit')).toHaveTextContent('ETH, USDC')
    expect(summary('Gas funds')).toHaveTextContent('1 ETH')
    expect(summary("Assistant's access")).toHaveTextContent('30 days, renewable any time in Controls')
    await user.click(within(review).getByRole('button', { name: 'Create wallet' }))

    expect(await screen.findByText('Your Mitfah wallet on Sepolia')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')

    expect(calls.deploy).toEqual([
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
    expect(calls.sent).toHaveLength(1)
    expect(calls.sent[0]).toMatchObject({ to: PREPARED.tx.to, data: PREPARED.tx.data, value: PREPARED.tx.value })
    expect(calls.confirm).toHaveLength(2)
    expect(calls.confirm[1]).toEqual({
      chain_id: SEPOLIA,
      deployer: WALLET,
      tx_hash: TX_HASH,
      predicted_address: PREDICTED,
    })
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it('says so when the signature is declined', async () => {
    const calls = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding', { wagmiConfig: makeWagmiConfig({ signMessageError: true }) })

    await connect(user)
    const verify = await step('Prove this wallet is yours')
    await user.click(within(verify).getByRole('button', { name: 'Verify with wallet' }))

    expect(await within(verify).findByText('Cancelled — nothing was signed.')).toBeInTheDocument()
    expect(calls.siwe).toHaveLength(0)
  })

  it('picks up a deploy that was still waiting when the page was reloaded', async () => {
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    const calls = stubServer({ ownerAddr: WALLET })
    const router = await renderRoutes(routes, '/onboarding')

    // No wallet connection needed: the transaction is already out.
    expect(await step('Creating your wallet')).toBeInTheDocument()
    expect(await screen.findByText('Your Mitfah wallet on Sepolia')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')
    expect(calls.confirm).toEqual([
      { chain_id: SEPOLIA, deployer: WALLET, tx_hash: TX_HASH, predicted_address: PREDICTED },
      { chain_id: SEPOLIA, deployer: WALLET, tx_hash: TX_HASH, predicted_address: PREDICTED },
    ])
    expect(calls.deploy).toHaveLength(0)
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it('lets the user start over when the deploy reverted', async () => {
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    stubServer({ ownerAddr: WALLET, reverted: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    const creating = await step('Creating your wallet')
    expect(await within(creating).findByText(/deployWallet reverted/)).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()

    await user.click(within(creating).getByRole('button', { name: 'Try again' }))
    expect(await step('Connect the wallet that will own your Mitfah wallet')).toBeInTheDocument()
  })

  it('ignores a deploy another account left waiting in this tab', async () => {
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED }
    sessionStorage.setItem('mitfah-pending-deploy:3', JSON.stringify(pending))
    const calls = stubServer()
    await renderRoutes(routes, '/onboarding')

    expect(await step('Connect the wallet that will own your Mitfah wallet')).toBeInTheDocument()
    expect(calls.confirm).toHaveLength(0)
    expect(sessionStorage.getItem('mitfah-pending-deploy:3')).not.toBeNull()
  })

  it("stops waiting for a deploy this account isn't linked to, and lets the user link a wallet", async () => {
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    stubServer({ unlinked: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    const creating = await step('Creating your wallet')
    expect(await within(creating).findByText(/isn't linked to a wallet yet/)).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()

    await user.click(within(creating).getByRole('button', { name: 'Try again' }))
    await connect(user)
    expect(await step('Prove this wallet is yours')).toBeInTheDocument()
  })

  it('skips verification when the wallet is already linked', async () => {
    const calls = stubServer({ ownerAddr: WALLET })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    await connect(user)

    expect(await step('Where should your wallet live?')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Prove this wallet is yours' })).toBeNull()
    await waitFor(() => expect(calls.siwe).toHaveLength(0))
  })
})
