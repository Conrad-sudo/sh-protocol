import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../../api/client'
import { routes } from '../../routes'
import type { Passkey, RegisteredPasskey } from '../../api/types'
import { makePasskey, makeWalletState, PASSKEY_CREDENTIAL } from '../../test/fixtures'
import { answerRpc, isRpc, json, ME, renderRoutes, setViewportWidth, TOKEN, WALLET } from '../../test/utils'
import { b64url, installPasskeys, madeCredential } from '../../test/webauthn'
import { rememberPasskey } from '../../lib/passkey'

// Polling waits 2 s between checks; the tests don't.
vi.mock('../../lib/tx', async importOriginal => ({
  ...(await importOriginal<typeof import('../../lib/tx')>()),
  sleep: () => Promise.resolve(),
}))

const SEPOLIA = 11155111
const TX_HASH = `0x${'12'.repeat(32)}`
const PREDICTED = '0x2222222222222222222222222222222222222222'
// The signed-in test account is user 7 (test/utils ME/TOKEN).
const PENDING_KEY = 'mitfah-pending-deploy:7'
const TOKENS = [
  { ticker: 'usdc', address: '0x3333333333333333333333333333333333333333' },
  // The API flags the wrapped native token: it always counts, like ETH.
  { ticker: 'weth', address: '0x4444444444444444444444444444444444444444', always_counted: true },
]
/** The passkey made during onboarding, as POST /api/passkeys/register saves it. */
const NEW_CREDENTIAL = b64url('work-laptop-passkey')
const NEW_PASSKEY: RegisteredPasskey = {
  key_id: `0x${'ef'.repeat(32)}`,
  credential_id: NEW_CREDENTIAL,
  device_name: 'Work laptop',
  created_at: 1_790_100_000,
  qx: `0x${'01'.repeat(32)}`,
  qy: `0x${'02'.repeat(32)}`,
}
const REGISTRATION_OPTIONS: PublicKeyCredentialCreationOptionsJSON = {
  rp: { id: 'localhost', name: 'Mitfah' },
  user: { id: b64url('user-handle-1234'), name: 'sam', displayName: 'sam' },
  challenge: b64url('registration challenge 32 bytes!'),
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
  authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
  attestation: 'none',
  timeout: 60_000,
}
const PREPARED = {
  chain_id: SEPOLIA,
  predicted_address: PREDICTED,
  key_id: NEW_PASSKEY.key_id,
  watched_tokens: ['usdc'],
  tx: { to: '0x6666666666666666666666666666666666666666', data: '0xdeadbeef', value: '0xde0b6b3a7640000', gas: '0x7a120' },
}

// Another address, for an account that signs in as something other than the connected wallet.
const OTHER = '0x9999999999999999999999999999999999999999'

interface Calls {
  deploy: Record<string, unknown>[]
  confirm: unknown[]
  sent: Record<string, string>[]
  registered: unknown[]
  optionsAsked: number
}

interface ServerOptions {
  ownerAddr?: string
  reverted?: boolean
  wrongDeployer?: boolean
  /** Holds the wallet's answer to a send until this settles, like a wallet prompt left open. */
  walletPrompt?: Promise<void>
  /** Answers every confirm instead of "pending" once, then "deployed". */
  confirm?: () => Response
  /** The account's passkeys (GET /api/passkeys); one made during the test joins them. */
  passkeys?: Passkey[]
}

/**
 * A signed-in API with no wallet yet, for an account that signs in as `ownerAddr` (the mock wallet
 * unless a test says otherwise). Confirm answers "pending" once, then "deployed"; `wrongDeployer`
 * has it refuse a deploy sent from another address. The mock wallet's transactions arrive as
 * JSON-RPC.
 */
function stubServer({
  ownerAddr = WALLET,
  reverted = false,
  wrongDeployer = false,
  walletPrompt = Promise.resolve(),
  confirm,
  passkeys = [],
}: ServerOptions = {}) {
  let me = { ...ME, owner_addr: ownerAddr, wallet_chains: [] as number[] }
  const calls: Calls = { deploy: [], confirm: [], sent: [], registered: [], optionsAsked: 0 }
  const accountPasskeys = [...passkeys]

  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (isRpc(url)) {
        const sending = String(init?.body).includes('"eth_sendTransaction"')
        return (sending ? walletPrompt : Promise.resolve()).then(() =>
          answerRpc(init, {
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
        case `/api/tokens?chain_id=${SEPOLIA}`:
          return Promise.resolve(json(200, { tokens: TOKENS }))
        case '/api/passkeys':
          return Promise.resolve(json(200, { passkeys: accountPasskeys }))
        case '/api/passkeys/register/options':
          calls.optionsAsked += 1
          return Promise.resolve(json(200, { options: REGISTRATION_OPTIONS }))
        case '/api/passkeys/register':
          calls.registered.push(body)
          accountPasskeys.push(NEW_PASSKEY)
          return Promise.resolve(json(200, NEW_PASSKEY))
        case '/api/deploy':
          calls.deploy.push(body)
          return Promise.resolve(json(200, { ...PREPARED, key_id: body.key_id }))
        case '/api/deploy/confirm':
          calls.confirm.push(body)
          if (confirm) return Promise.resolve(confirm())
          if (wrongDeployer) {
            return Promise.resolve(
              json(403, {
                detail: `You're signed in as ${OTHER}, not ${WALLET}. Switch to that account in your wallet.`,
              }),
            )
          }
          if (calls.confirm.length === 1) return Promise.resolve(json(202, { status: 'pending', tx_hash: TX_HASH }))
          if (reverted) return Promise.resolve(json(400, { detail: `deployWallet reverted (tx: ${TX_HASH})` }))
          me = { ...me, wallet_chains: [SEPOLIA] }
          {
            const keyId = (calls.deploy.at(-1)?.key_id as string | null | undefined) ?? null
            return Promise.resolve(
              json(200, {
                status: 'deployed',
                chain_id: SEPOLIA,
                wallet_address: PREDICTED,
                key_id: keyId,
                session_key_authorized: keyId !== null,
                session_key_expires_at: keyId === null ? 0 : Math.floor(Date.now() / 1000) + 30 * 86_400,
              }),
            )
          }
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
  const region = await step('Connect the wallet you signed in with')
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
    vi.restoreAllMocks()
  })

  it('creates the wallet with the chosen settings, once the signed-in wallet is connected', async () => {
    const browser = installPasskeys()
    browser.create.mockResolvedValue(madeCredential(NEW_CREDENTIAL))
    const calls = stubServer()
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/onboarding')

    // Signing in already proved the wallet is the user's: connecting it goes straight to the steps.
    expect(screen.queryByRole('region', { name: /Prove this wallet/ })).toBeNull()
    await connect(user)

    // Network: the wallet is already on Sepolia, so it is preselected.
    const network = await step('Where should your wallet live?')
    expect(within(network).getByRole('radio', { name: /Sepolia/ })).toBeChecked()
    await user.click(within(network).getByRole('button', { name: 'Continue' }))

    // Limits: $250 a week. USDC unticked; WETH can't be, it always counts like ETH.
    const limits = await step('How much may the assistant spend?')
    const limit = within(limits).getByLabelText('Spending limit')
    await user.clear(limit)
    await user.type(limit, '250')
    await user.click(within(limits).getByText('7 days'))
    const weth = await within(limits).findByRole('checkbox', { name: 'WETH' })
    expect(weth).toBeChecked()
    expect(weth).toBeDisabled()
    expect(within(limits).getByText(/ETH and WETH always count\./)).toBeInTheDocument()
    await user.click(within(limits).getByRole('checkbox', { name: 'USDC' }))
    expect(within(limits).getByRole('checkbox', { name: 'WETH' })).toBeChecked()
    await user.click(within(limits).getByRole('button', { name: 'Continue' }))

    // The prefund: 1 ETH by default on a local test network.
    const fund = await step('Prefund your Mitfah smart wallet')
    expect(within(fund).getByLabelText('Amount to send now')).toHaveValue('1')
    await user.click(within(fund).getByRole('button', { name: 'Continue' }))

    // This device's passkey, made with the tap: the new wallet trusts it from the start.
    const passkey = await step('Create your passkey')
    const name = within(passkey).getByLabelText('Name for this device')
    await user.clear(name)
    await user.type(name, 'Work laptop')
    // Fetched before the tap, so the tap can open the browser's prompt at once.
    await waitFor(() => expect(calls.optionsAsked).toBe(1))
    await user.click(within(passkey).getByRole('button', { name: 'Create passkey' }))
    expect(browser.create).toHaveBeenCalledTimes(1)

    const review = await step('Check the details')
    const summary = (term: string) => within(review).getByText(term).nextElementSibling
    expect(summary('Spending limit')).toHaveTextContent('$250.00 every 7 days')
    expect(summary('Counts toward the limit')).toHaveTextContent('ETH, WETH')
    expect(summary('Wallet prefund')).toHaveTextContent('1 ETH')
    expect(summary("Assistant's access")).toHaveTextContent(
      '30 days, approved with the passkey on “Work laptop”; renewable any time in Controls',
    )
    await user.click(within(review).getByRole('button', { name: 'Create wallet' }))

    expect(await screen.findByText('Your Mitfah smart wallet on Sepolia')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')
    expect(screen.getByText(/^Your wallet is ready on Sepolia\. You can now disconnect your browser wallet/)).toBeInTheDocument()
    expect(calls.registered).toEqual([
      { credential: expect.objectContaining({ id: NEW_CREDENTIAL, type: 'public-key' }), device_name: 'Work laptop' },
    ])

    expect(calls.deploy).toEqual([
      {
        chain_id: SEPOLIA,
        deployer: WALLET,
        daily_limit_usd: 250,
        window_secs: 604_800,
        watched_tokens: [TOKENS[1]],
        prefund_eth: '1',
        key_id: NEW_PASSKEY.key_id,
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

  it('asks for the wallet the account signs in with when another one is connected', async () => {
    const calls = stubServer({ ownerAddr: OTHER })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    await connect(user)
    const swap = await step('Switch to the wallet you signed in with')
    expect(swap).toHaveTextContent(/You're signed in as .+, but your wallet is connected as/)
    // Each address is spoken in full (and shown shortened).
    expect(within(swap).getAllByText(OTHER)).not.toHaveLength(0)
    expect(within(swap).getByText(WALLET)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Where should your wallet live?' })).toBeNull()
    expect(calls.deploy).toHaveLength(0)
  })

  it('picks up a deploy that was still waiting when the page was reloaded', async () => {
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    const calls = stubServer()
    const router = await renderRoutes(routes, '/onboarding')

    // No wallet connection needed: the transaction is already out.
    expect(await step('Creating your wallet')).toBeInTheDocument()
    expect(await screen.findByText('Your Mitfah smart wallet on Sepolia')).toBeInTheDocument()
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
    stubServer({ reverted: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    const creating = await step('Creating your wallet')
    expect(await within(creating).findByText(/deployWallet reverted/)).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()

    await user.click(within(creating).getByRole('button', { name: 'Try again' }))
    expect(await step('Connect the wallet you signed in with')).toBeInTheDocument()
  })

  it('ignores a deploy another account left waiting in this tab', async () => {
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED }
    sessionStorage.setItem('mitfah-pending-deploy:3', JSON.stringify(pending))
    const calls = stubServer()
    await renderRoutes(routes, '/onboarding')

    expect(await step('Connect the wallet you signed in with')).toBeInTheDocument()
    expect(calls.confirm).toHaveLength(0)
    expect(sessionStorage.getItem('mitfah-pending-deploy:3')).not.toBeNull()
  })

  it('stops waiting for a deploy sent from an address this account does not sign in as', async () => {
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    stubServer({ wrongDeployer: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    const creating = await step('Creating your wallet')
    expect(await within(creating).findByText(/You're signed in as/)).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()

    await user.click(within(creating).getByRole('button', { name: 'Try again' }))
    await connect(user)
    expect(await step('Where should your wallet live?')).toBeInTheDocument()
  })

  it('says so when the network never received the deploy, and only then offers to start over', async () => {
    const sentAt = Date.now() - 100_000
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED, sentAt }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    stubServer({ confirm: () => json(202, { status: 'pending', tx_hash: TX_HASH, seen: false }) })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    const creating = await step('Creating your wallet')
    expect(
      await within(creating).findByText(
        "Sepolia hasn't received this transaction. If your wallet says it failed, no wallet was created and you can try again.",
      ),
    ).toBeInTheDocument()
    // Kept, in case it turns up after all, until the user starts over.
    expect(within(creating).getByRole('button', { name: 'Check again' })).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).not.toBeNull()

    await user.click(within(creating).getByRole('button', { name: 'Try again' }))
    expect(await step('Connect the wallet you signed in with')).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it("never offers to start over while the network has the deploy: it would make a second wallet", async () => {
    // Each check finds it still waiting to be mined, five minutes apart.
    const realNow = Date.now.bind(Date)
    let clock = 0
    vi.spyOn(Date, 'now').mockImplementation(() => realNow() + clock)
    const pending = { chainId: SEPOLIA, deployer: WALLET, txHash: TX_HASH, predictedAddress: PREDICTED, sentAt: realNow() }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    stubServer({
      confirm: () => {
        clock += 5 * 60_000
        return json(202, { status: 'pending', tx_hash: TX_HASH, seen: true })
      },
    })
    await renderRoutes(routes, '/onboarding')

    const creating = await step('Creating your wallet')
    expect(await within(creating).findByText(/^Your wallet is still being created\./)).toBeInTheDocument()
    expect(within(creating).getByRole('button', { name: 'Check again' })).toBeInTheDocument()
    expect(within(creating).queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(sessionStorage.getItem(PENDING_KEY)).not.toBeNull()
  })

  it('lets go of a wallet that never answers, and still follows a deploy it sends later', async () => {
    let approve!: () => void
    const calls = stubServer({ walletPrompt: new Promise(resolve => (approve = resolve)) })
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/onboarding')

    await connect(user)
    await user.click(within(await step('Where should your wallet live?')).getByRole('button', { name: 'Continue' }))
    const limits = await step('How much may the assistant spend?')
    await within(limits).findByRole('checkbox', { name: 'WETH' })
    await user.click(within(limits).getByRole('button', { name: 'Continue' }))
    await user.click(within(await step('Prefund your Mitfah smart wallet')).getByRole('button', { name: 'Continue' }))
    await user.click(within(await step('Create your passkey')).getByRole('button', { name: 'Skip for now' }))
    const review = await step('Check the details')
    await user.click(within(review).getByRole('button', { name: 'Create wallet' }))
    expect(await within(review).findByText('Confirm the transaction in your wallet.')).toBeInTheDocument()

    await user.click(within(review).getByRole('button', { name: 'Stop waiting' }))
    expect(
      within(review).getByText(
        'Stopped waiting. If your wallet still shows the request, reject it there before you try again.',
      ),
    ).toBeInTheDocument()
    expect(within(review).getByRole('button', { name: 'Create wallet' })).toBeEnabled()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()

    // Approved after all: that transaction creates the wallet, so it is followed to the end.
    approve()
    expect(await screen.findByText('Your Mitfah smart wallet on Sepolia')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/dashboard')
    expect(calls.sent).toHaveLength(1)
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })
  /** Connects and accepts the defaults up to the passkey step. */
  async function toPasskeyStep(user: ReturnType<typeof userEvent.setup>) {
    await connect(user)
    await user.click(within(await step('Where should your wallet live?')).getByRole('button', { name: 'Continue' }))
    const limits = await step('How much may the assistant spend?')
    await within(limits).findByRole('checkbox', { name: 'WETH' })
    await user.click(within(limits).getByRole('button', { name: 'Continue' }))
    await user.click(within(await step('Prefund your Mitfah smart wallet')).getByRole('button', { name: 'Continue' }))
  }

  it('creates the wallet without a passkey when skipped, and says how to add a device', async () => {
    installPasskeys()
    const calls = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    await toPasskeyStep(user)
    const passkey = await step('Create your passkey')
    expect(within(passkey).getByText(/the assistant can't send anything until you add a device in Controls/)).toBeInTheDocument()
    await user.click(within(passkey).getByRole('button', { name: 'Skip for now' }))

    const review = await step('Check the details')
    expect(within(review).getByText("Assistant's access").nextElementSibling).toHaveTextContent(
      "None yet: the assistant can't send anything until you add a device in Controls",
    )
    await user.click(within(review).getByRole('button', { name: 'Create wallet' }))
    expect(
      await screen.findByText('Your wallet is ready on Sepolia. Add a device in Controls so the assistant can send payments.'),
    ).toBeInTheDocument()
    expect(calls.deploy[0]).toMatchObject({ key_id: null })
    expect(calls.registered).toEqual([])
  })

  it('uses the passkey this device already has', async () => {
    installPasskeys()
    rememberPasskey(PASSKEY_CREDENTIAL)
    const calls = stubServer({ passkeys: [makePasskey()] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    await toPasskeyStep(user)
    const passkey = await step('Your passkey')
    expect(within(passkey).getByText(/This device already has a passkey, “iPhone”\./)).toBeInTheDocument()
    await user.click(within(passkey).getByRole('button', { name: 'Continue' }))
    const review = await step('Check the details')
    expect(within(review).getByText("Assistant's access").nextElementSibling).toHaveTextContent(/approved with the passkey on “iPhone”/)
    await user.click(within(review).getByRole('button', { name: 'Create wallet' }))
    expect(await screen.findByText('Your Mitfah smart wallet on Sepolia')).toBeInTheDocument()
    expect(calls.deploy[0]).toMatchObject({ key_id: makePasskey().key_id })
    expect(calls.optionsAsked).toBe(0)
  })

  it("offers only to skip on a browser that can't make passkeys", async () => {
    stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/onboarding')

    await toPasskeyStep(user)
    const passkey = await step('Create your passkey')
    expect(within(passkey).getByText(/This browser can't make passkeys\./)).toBeInTheDocument()
    expect(within(passkey).queryByRole('button', { name: 'Create passkey' })).toBeNull()
    expect(within(passkey).getByRole('button', { name: 'Skip for now' })).toBeEnabled()
  })
})
