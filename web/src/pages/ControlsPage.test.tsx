import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { privateKeyToAccount } from 'viem/accounts'
import type {
  Agent,
  Agents,
  Allowlist,
  AllowlistEntry,
  Contact,
  Passkey,
  RecurringPayment,
  RecurringPayments,
  RegisteredPasskey,
  SignStart,
  WalletState,
} from '../api/types'
import { routes } from '../routes'
import { formatDate } from '../lib/format'
import { chainName } from '../wallet/chains'
import {
  makePasskey,
  makeSession,
  makeSessionBlock,
  makeSessionKey,
  makeWalletState,
  PASSKEY_CREDENTIAL,
  PASSKEY_ID,
  SEPOLIA,
  USDC,
  WETH,
} from '../test/fixtures'
import { answerRpc, isRpc, json, ME, renderRoutes, RpcError, setViewportWidth, TOKEN, WALLET } from '../test/utils'
import { b64url, installPasskeys, madeCredential, refused, signedCredential } from '../test/webauthn'

// Polling waits 2 s between checks; the tests don't.
vi.mock('../lib/tx', async importOriginal => ({
  ...(await importOriginal<typeof import('../lib/tx')>()),
  sleep: () => Promise.resolve(),
}))

const MAINNET = 1
const BSC = 56
const TX_HASH = `0x${'78'.repeat(32)}`
const PREPARED_TX = { to: '0x2222222222222222222222222222222222222222', data: '0x8456cb59', value: '0x0', gas: '0x7530' }
const LINK = '0x779877A7B0D9E8603169DdbD7836e478b4624789'
const TOKENS = [
  { ticker: 'link', address: LINK },
  { ticker: 'usdc', address: USDC },
  // The API flags the wrapped native token: it counts exactly when ETH does, and moves with it.
  { ticker: 'weth', address: WETH, follows_native: true },
]
const PENDING_KEY = `mitfah-pending-owner-tx:7:${SEPOLIA}`
// What every new wallet on Sepolia trusts, as /api/chains lists it: Uniswap V3's router, then its
// positions contract.
const V3_ROUTER = '0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E'
const POSITION_MANAGER = '0x1238536071E1c677A632429e3655c799b22cDA52'
// Uniswap V2's router on Sepolia: all a wallet made before the move to V3 trusts.
const V2_ROUTER = '0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3'
// PancakeSwap's router: what every new wallet on BSC trusts.
const PANCAKE_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
const REGISTRY = '0x8004000000000000000000000000000000000713'
const ROUTER_ENTRY: AllowlistEntry = { address: V3_ROUTER, label: 'Uniswap V3 router' }
const USDC_ENTRY: AllowlistEntry = { address: USDC, label: 'USDC' }
const REGISTRY_ENTRY: AllowlistEntry = { address: REGISTRY, label: 'ERC-8004 review registry' }
/** GET /api/wallet/{chain_id}/allowlist for a wallet that has never used it. */
const ALLOWLIST_OFF: Allowlist = { enabled: false, targets: [], suggested: [ROUTER_ENTRY, USDC_ENTRY, REGISTRY_ENTRY] }

/** GET /api/wallet/{chain_id}/recurring for a wallet with the module and no schedules yet. */
const NO_RECURRING: RecurringPayments = {
  available: true,
  module: '0x7ec0000000000000000000000000000000000000',
  fee_per_payment_wei: '20000000000000',
  native_ticker: 'ETH',
  min_interval_secs: 3_600,
  schedules: [],
}
const PAYEE = '0x3333333333333333333333333333333333333333'
const RENT: RecurringPayment = {
  schedule_id: 4,
  token: USDC,
  ticker: 'USDC',
  decimals: 6,
  amount_raw: '20000000',
  amount_text: '20',
  recipient: PAYEE,
  recipient_name: 'landlord',
  interval_secs: 2_592_000,
  next_due: 1_900_000_000,
  end_at: null,
  status: 'live',
  last_tx: null,
  last_run_at: null,
  last_error: null,
  retry_at: null,
  summary: '20 USDC to landlord every 30 days',
}

/** The wallet's agent module, and its agents as GET /api/wallet/{chain_id}/agents lists them. */
const AGENT_MODULE = '0x8b1cB45E2E7a101F9DafDf732acf616352069D7f'
const NO_AGENTS: Agents = {
  available: true,
  module: AGENT_MODULE,
  native_metered: true,
  max_agents: 3,
  max_days: 90,
  agents: [],
}
const withAgents = (...agents: Agent[]): Agents => ({ ...NO_AGENTS, agents })
const BOT_ADDRESS = '0x4444444444444444444444444444444444444444'
/** An agent that works for another 60 days. */
const BOT: Agent = {
  address: BOT_ADDRESS,
  name: 'Bot',
  limit_usd: 50,
  spent_usd: 12.5,
  left_usd: 37.5,
  valid_until: Math.floor(Date.now() / 1000) + 60 * 86_400,
  expires: 'in 60 days',
  active: true,
}
const NEW_AGENT_ADDRESS = '0x5555555555555555555555555555555555555555'
/** POST /api/wallet/agents/stop's quote: the wallet's own removal, waiting for a passkey. */
const STOP_QUOTE = {
  quote_id: 'q-stop_1',
  chain_id: SEPOLIA,
  action: 'Remove agent “Bot”',
  fees: '$0.12',
  expires_at: Math.floor(Date.now() / 1000) + 300,
}
/** The History row a passkey's stop is followed by. */
const STOP_TX_ID = 91

/** A passkey made on this device during a test, as the server saves it. */
const NEW_CREDENTIAL = b64url('work-laptop-passkey')
const NEW_PASSKEY: RegisteredPasskey = {
  key_id: `0x${'ef'.repeat(32)}`,
  credential_id: NEW_CREDENTIAL,
  device_name: 'Work laptop',
  created_at: 1_790_100_000,
  qx: `0x${'01'.repeat(32)}`,
  qy: `0x${'02'.repeat(32)}`,
}
/** POST /api/passkeys/register/options. */
const REGISTRATION_OPTIONS: PublicKeyCredentialCreationOptionsJSON = {
  rp: { id: 'localhost', name: 'Mitfah' },
  user: { id: b64url('user-handle-1234'), name: 'sam', displayName: 'sam' },
  challenge: b64url('registration challenge 32 bytes!'),
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
  authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
  attestation: 'none',
  excludeCredentials: [{ type: 'public-key', id: PASSKEY_CREDENTIAL }],
  timeout: 60_000,
}

/** A wallet whose contract allowlist is on, as GET /api/wallet reports it. */
function walletWithAllowlistOn() {
  const wallet = makeWalletState()
  return makeWalletState({ limits: { ...wallet.limits, allowlist_enabled: true } })
}

interface ServerOptions {
  walletChains?: number[]
  /** GET /api/wallet answers in order; the last one repeats. */
  wallets?: WalletState[]
  /** GET /api/wallet/{chain_id}/allowlist answers in order; the last one repeats. */
  allowlists?: Allowlist[]
  /** A prepare endpoint that refuses, by path. */
  refuse?: Record<string, string>
  /** The wallet declines to send. */
  rejectSend?: boolean
  /** Holds the wallet's answers until this settles, like a wallet prompt left open. */
  walletPrompt?: Promise<void>
  /** GET /api/wallet on another network than the first of `walletChains`, by chain id. */
  otherWallets?: Record<number, WalletState>
  /** The account's passkeys (GET /api/passkeys); one registered in the test joins them. */
  passkeys?: Passkey[]
  /** DELETE /api/passkeys/{key_id} refuses with this 409 detail. */
  refuseDelete?: string
  /** Holds GET /api/tokens until this settles. */
  tokensReady?: Promise<void>
  /** GET /api/wallet/{chain_id}/recurring; the last one keeps answering. */
  recurring?: RecurringPayments[]
  /** GET /api/contacts. */
  contacts?: Contact[]
  /**
   * The wallet's agents: the first until an agent change is confirmed (an owner's, or a passkey's stop
   * landing), then the next one; the last one stays.
   */
  agents?: Agents[]
  /** POST /api/wallet/agents/stop refuses with this 400 detail. */
  refuseStop?: string
  /** How the stop's History row reads, in order; the last one repeats. */
  stopStatuses?: ('pending' | 'confirmed' | 'failed')[]
}

interface Prepared {
  path: string
  body: Record<string, unknown>
}

/**
 * A signed-in owner whose wallet answers from `wallets`. Every prepare succeeds unless refused;
 * each confirm endpoint answers "pending" once per transaction, then its final status.
 */
function stubServer({
  walletChains = [SEPOLIA],
  wallets = [makeWalletState()],
  allowlists = [ALLOWLIST_OFF],
  refuse = {},
  rejectSend = false,
  walletPrompt = Promise.resolve(),
  otherWallets = {},
  passkeys = [makePasskey()],
  refuseDelete,
  tokensReady = Promise.resolve(),
  recurring = [NO_RECURRING],
  contacts = [],
  agents = [NO_AGENTS],
  refuseStop,
  stopStatuses = ['pending', 'confirmed'],
}: ServerOptions = {}) {
  const prepared: Prepared[] = []
  const agentConfirms: Record<string, unknown>[] = []
  const agentRenames: Record<string, unknown>[] = []
  const stops: Record<string, unknown>[] = []
  const signStarts: Record<string, unknown>[] = []
  const signFinishes: Record<string, unknown>[] = []
  const signCancels: Record<string, unknown>[] = []
  // Everything the page sent, to check what it never sends.
  const bodies: string[] = []
  let agentChanges = 0
  let stopReads = 0
  const currentAgents = () => agents[Math.min(agentChanges, agents.length - 1)]
  const confirms: Record<string, unknown>[] = []
  const sessionConfirms: Record<string, unknown>[] = []
  const recurringConfirms: Record<string, unknown>[] = []
  let recurringReads = 0
  const sent: Record<string, string>[] = []
  const sentTo: string[] = []
  const registered: Record<string, unknown>[] = []
  const renamed: { url: string; body: Record<string, unknown> }[] = []
  const deleted: string[] = []
  let optionsAsked = 0
  let walletReads = 0
  let allowlistReads = 0
  const accountPasskeys = [...passkeys]

  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (isRpc(url)) {
        if (init?.body) bodies.push(String(init.body))
        return walletPrompt.then(() =>
          answerRpc(init, {
            eth_sendTransaction: ([tx]) => {
              if (rejectSend) throw new RpcError(4001, 'User rejected the request.')
              sent.push(tx as Record<string, string>)
              sentTo.push(url)
              return TX_HASH
            },
          }),
        )
      }
      if (init?.body) bodies.push(String(init.body))
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {}
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, { ...ME, owner_addr: WALLET, wallet_chains: walletChains }))
      if (url === '/api/chains') {
        return Promise.resolve(
          json(200, {
            chains: [
              {
                chain_id: SEPOLIA,
                name: 'sepolia',
                native_ticker: 'ETH',
                fork: false,
                exchange: 'uniswap-v3',
                default_spenders: [V3_ROUTER, POSITION_MANAGER],
              },
              { chain_id: MAINNET, name: 'mainnet', native_ticker: 'ETH', fork: false, exchange: null, default_spenders: [] },
              {
                chain_id: BSC,
                name: 'bsc',
                native_ticker: 'BNB',
                fork: false,
                exchange: 'pancakeswap-v2',
                default_spenders: [PANCAKE_ROUTER],
              },
            ],
          }),
        )
      }
      if (url.startsWith('/api/tokens')) return tokensReady.then(() => json(200, { tokens: TOKENS }))
      const walletRead = /^\/api\/wallet\/(\d+)$/.exec(url)
      if (walletRead) {
        const other = otherWallets[Number(walletRead[1])]
        if (other) return Promise.resolve(json(200, other))
        const answer = wallets[Math.min(walletReads, wallets.length - 1)]
        walletReads += 1
        return Promise.resolve(json(200, answer))
      }
      if (url === '/api/passkeys') return Promise.resolve(json(200, { passkeys: accountPasskeys }))
      if (url === '/api/passkeys/register/options') {
        optionsAsked += 1
        return Promise.resolve(json(200, { options: REGISTRATION_OPTIONS }))
      }
      if (url === '/api/passkeys/register') {
        registered.push(body)
        accountPasskeys.push(NEW_PASSKEY)
        return Promise.resolve(json(200, NEW_PASSKEY))
      }
      if (url.startsWith('/api/passkeys/') && init?.method === 'PATCH') {
        renamed.push({ url, body })
        const keyId = decodeURIComponent(url.slice('/api/passkeys/'.length))
        const passkey = accountPasskeys.find(p => p.key_id === keyId)!
        passkey.device_name = String(body.device_name)
        return Promise.resolve(json(200, passkey))
      }
      if (url.startsWith('/api/passkeys/') && init?.method === 'DELETE') {
        if (refuseDelete) return Promise.resolve(json(409, { detail: refuseDelete }))
        const keyId = decodeURIComponent(url.slice('/api/passkeys/'.length))
        deleted.push(keyId)
        accountPasskeys.splice(accountPasskeys.findIndex(p => p.key_id === keyId), 1)
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      if (/^\/api\/wallet\/\d+\/recurring$/.test(url)) {
        const answer = recurring[Math.min(recurringReads, recurring.length - 1)]
        recurringReads += 1
        return Promise.resolve(json(200, answer))
      }
      if (url === '/api/contacts') return Promise.resolve(json(200, { contacts }))
      if (/^\/api\/wallet\/\d+\/agents$/.test(url)) return Promise.resolve(json(200, currentAgents()))
      if (url === '/api/wallet/agents/confirm') {
        agentConfirms.push(body)
        if (agentConfirms.length % 2 === 1) return Promise.resolve(json(202, { status: 'pending', tx_hash: body.tx_hash }))
        agentChanges += 1
        return Promise.resolve(json(200, { status: 'confirmed', tx_hash: body.tx_hash, agents: currentAgents() }))
      }
      if (url === '/api/wallet/agents/rename') {
        agentRenames.push(body)
        return Promise.resolve(json(200, { agent: body.agent, name: body.name }))
      }
      if (url === '/api/wallet/agents/stop') {
        stops.push(body)
        if (refuseStop) return Promise.resolve(json(400, { detail: refuseStop }))
        return Promise.resolve(json(200, { quote: STOP_QUOTE, replaced: 0 }))
      }
      if (url === '/api/sign/start') {
        signStarts.push(body)
        const start: SignStart = {
          sign_id: `sign-${signStarts.length}`,
          challenge: b64url('the user operation hash, 32 b!!!'),
          rp_id: 'localhost',
          allow_credentials: [{ type: 'public-key', id: PASSKEY_CREDENTIAL }],
          user_verification: 'required',
          timeout_ms: 60_000,
          expires_at: STOP_QUOTE.expires_at,
          summary: { action: STOP_QUOTE.action, network: 'Sepolia', chain_id: SEPOLIA, origin: 'controls', cost: {} },
        }
        return Promise.resolve(json(200, start))
      }
      if (url === '/api/sign/finish') {
        signFinishes.push(body)
        return Promise.resolve(json(200, { status: 'sent', outcome: 'Sending — Remove agent “Bot”', transaction_id: STOP_TX_ID }))
      }
      if (url === '/api/sign/cancel') {
        signCancels.push(body)
        return Promise.resolve(json(200, { outcome: 'Cancelled. Nothing was sent.' }))
      }
      if (url === `/api/transactions/${STOP_TX_ID}`) {
        const status = stopStatuses[Math.min(stopReads, stopStatuses.length - 1)]
        stopReads += 1
        if (status === 'confirmed' && stopReads === stopStatuses.indexOf('confirmed') + 1) agentChanges += 1
        return Promise.resolve(
          json(200, {
            id: STOP_TX_ID,
            chain_id: SEPOLIA,
            source: 'controls',
            action: STOP_QUOTE.action,
            status,
            tx_hash: status === 'pending' ? null : TX_HASH,
            created_at: 1_790_000_000,
            mined_at: status === 'pending' ? null : 1_790_000_012,
            direction: 'none',
          }),
        )
      }
      if (url === '/api/wallet/recurring/confirm') {
        recurringConfirms.push(body)
        return Promise.resolve(
          recurringConfirms.length % 2 === 1
            ? json(202, { status: 'pending', tx_hash: body.tx_hash })
            : json(200, { status: 'confirmed', tx_hash: body.tx_hash, recurring: recurring[recurring.length - 1] }),
        )
      }
      if (/^\/api\/wallet\/\d+\/allowlist$/.test(url)) {
        const answer = allowlists[Math.min(allowlistReads, allowlists.length - 1)]
        allowlistReads += 1
        return Promise.resolve(json(200, answer))
      }
      if (url.endsWith('/prepare')) {
        prepared.push({ path: url, body })
        if (url in refuse) return Promise.resolve(json(400, { detail: refuse[url] }))
        return Promise.resolve(json(200, { tx: PREPARED_TX }))
      }
      if (url === '/api/wallet/tx/confirm') {
        confirms.push(body)
        return Promise.resolve(
          confirms.length % 2 === 1
            ? json(202, { status: 'pending', tx_hash: body.tx_hash })
            : json(200, { status: 'confirmed', tx_hash: body.tx_hash }),
        )
      }
      if (url === '/api/wallet/session/confirm') {
        sessionConfirms.push(body)
        return Promise.resolve(
          sessionConfirms.length % 2 === 1
            ? json(202, { status: 'pending', tx_hash: body.tx_hash })
            : json(200, { status: 'confirmed', tx_hash: body.tx_hash }),
        )
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return {
    prepared,
    confirms,
    sessionConfirms,
    recurringConfirms,
    agentConfirms,
    agentRenames,
    stops,
    signStarts,
    signFinishes,
    signCancels,
    bodies,
    stopReads: () => stopReads,
    sent,
    sentTo,
    registered,
    renamed,
    deleted,
    optionsAsked: () => optionsAsked,
    walletReads: () => walletReads,
  }
}

/** Connects the mock wallet from the page's owner bar (the top bar has its own button too). */
async function connectFromOwnerBar(user: UserEvent) {
  const bar = (await screen.findByText(/Connect your owner wallet/)).closest('.mf-owner-bar') as HTMLElement
  await user.click(within(bar).getByRole('button', { name: 'Connect wallet' }))
  await user.click(await screen.findByRole('button', { name: 'Mock Connector' }))
}

async function connect(user: UserEvent) {
  await connectFromOwnerBar(user)
  await screen.findByText('Owner connected')
}

const row = (title: string) => screen.getByRole('heading', { name: title, level: 3 }).closest('.mf-control-row') as HTMLElement

describe('ControlsPage', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
    sessionStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('pauses the wallet in one click and waits for the network', async () => {
    const paused = makeWalletState({ paused: true })
    const server = stubServer({ wallets: [makeWalletState(), paused] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(within(row('Wallet')).getByText('Active')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))

    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(server.prepared).toEqual([{ path: '/api/wallet/pause/prepare', body: { chain_id: SEPOLIA } }])
    expect(server.sent).toEqual([
      expect.objectContaining({ from: WALLET, to: PREPARED_TX.to, data: PREPARED_TX.data, gas: PREPARED_TX.gas }),
    ])
    // Pending once, then confirmed.
    expect(server.confirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
    ])
    // The wallet was read again and now shows as paused.
    expect(await within(row('Wallet')).findByText('Paused')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unpause' })).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it('asks before unpausing, and sends nothing if cancelled', async () => {
    const server = stubServer({ wallets: [makeWalletState({ paused: true })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Unpause' }))
    let dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Unpause this wallet?')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(server.prepared).toEqual([])

    await user.click(screen.getByRole('button', { name: 'Unpause' }))
    dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Unpause wallet' }))
    expect(await screen.findByText('Wallet unpaused.')).toBeInTheDocument()
    expect(server.prepared).toEqual([{ path: '/api/wallet/unpause/prepare', body: { chain_id: SEPOLIA } }])
  })

  it('lists the devices that approve payments, and renews one after asking', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(screen.getByText(/1 of 5 devices can approve payments\./)).toBeInTheDocument()
    const iphone = within(row('iPhone'))
    expect(iphone.getByText('Active')).toBeInTheDocument()
    expect(iphone.getByText(/^Approves payments until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()
    await user.click(iphone.getByRole('button', { name: 'Renew iPhone' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Renew iPhone?')).toBeInTheDocument()
    expect(dialog).toHaveTextContent(
      /iPhone keeps approving the assistant's payments from this wallet for another 90 days, until \w{3} \d{1,2}, \d{4}, up to \$100\.00 every 24 hours\./,
    )
    await user.click(within(dialog).getByRole('button', { name: 'Renew' }))

    expect(await screen.findByText(/^iPhone renewed until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/session/prepare',
        body: { chain_id: SEPOLIA, action: 'add', key_id: PASSKEY_ID, ttl_secs: 90 * 86_400 },
      },
    ])
    // Through the endpoint that reads the wallet's keys back, never the generic one.
    expect(server.sessionConfirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
    ])
    expect(server.confirms).toEqual([])
  })

  it('revokes a device in one click, and asks before adding it back', async () => {
    const server = stubServer({ wallets: [makeWalletState(), makeWalletState({ session: makeSession('off') })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Revoke iPhone' }))
    expect(await screen.findByText('iPhone can no longer approve payments on Sepolia.')).toBeInTheDocument()
    expect(await screen.findByText(/No device can approve payments from this wallet yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke all' })).toBeNull()

    // The passkey is still the account's, so the wallet offers it back.
    expect(within(row('iPhone')).getByText('Not on this wallet')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add iPhone' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Add iPhone?')).toBeInTheDocument()
    expect(dialog).toHaveTextContent(
      /iPhone will be able to approve the assistant's payments from this wallet for 90 days, until \w{3} \d{1,2}, \d{4}, up to \$100\.00 every 24 hours\./,
    )
    await user.click(within(dialog).getByRole('button', { name: 'Add device' }))
    expect(await screen.findByText(/^iPhone can approve payments on Sepolia until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()

    expect(server.prepared.map(p => p.body)).toEqual([
      { chain_id: SEPOLIA, action: 'remove', key_id: PASSKEY_ID },
      { chain_id: SEPOLIA, action: 'add', key_id: PASSKEY_ID, ttl_secs: 90 * 86_400 },
    ])
    expect(server.sessionConfirms).toHaveLength(4)
    expect(server.confirms).toEqual([])
  })

  it("says when a passkey not on the wallet was registered, asks the user to add it only if it's theirs, and removes one they don't know", async () => {
    const stranger = makePasskey({ key_id: `0x${'cd'.repeat(32)}`, credential_id: 'c3RyYW5nZXI', device_name: 'iPhone 2' })
    const server = stubServer({ passkeys: [makePasskey(), stranger] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const registered = formatDate(stranger.created_at * 1_000)
    const other = await waitFor(() => row('iPhone 2'))
    expect(within(other).getByText('Not on this wallet')).toBeInTheDocument()
    expect(other).toHaveTextContent(`A passkey registered to your account on ${registered}.`)
    expect(other).not.toHaveTextContent('One of your devices')

    await user.click(within(other).getByRole('button', { name: 'Add iPhone 2' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(`Only add it if you registered this passkey yourself, on ${registered}.`)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(server.prepared).toEqual([])

    await user.click(within(other).getByRole('button', { name: 'Remove iPhone 2' }))
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'iPhone 2', level: 3 })).toBeNull())
    expect(server.deleted).toEqual([stranger.key_id])
    // Forgetting it is the account's own list: the owner wallet signs nothing.
    expect(server.prepared).toEqual([])
    expect(server.sent).toEqual([])
  })

  it("says why a passkey couldn't be removed", async () => {
    const stranger = makePasskey({ key_id: `0x${'cd'.repeat(32)}`, credential_id: 'c3RyYW5nZXI', device_name: 'iPhone 2' })
    stubServer({ passkeys: [makePasskey(), stranger], refuseDelete: 'This passkey is still approved on your wallet.' })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(await screen.findByRole('button', { name: 'Remove iPhone 2' }))
    expect(
      await within(row('iPhone 2')).findByText(/^Couldn't remove it: This passkey is still approved on your wallet\.$/),
    ).toBeInTheDocument()
  })

  it('revokes every device at once, in one click', async () => {
    const server = stubServer({ wallets: [makeWalletState(), makeWalletState({ session: makeSession('off') })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Revoke all' }))
    expect(
      await screen.findByText("Every device revoked. The assistant can't send anything on Sepolia until you add one again."),
    ).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(server.prepared).toEqual([{ path: '/api/wallet/session/prepare', body: { chain_id: SEPOLIA, action: 'remove_all' } }])
  })

  it('warns while a device is about to run out', async () => {
    stubServer({ wallets: [makeWalletState({ session: makeSession('expiring') })] })
    await renderRoutes(routes, '/controls')

    const iphone = within((await screen.findByRole('heading', { name: 'iPhone', level: 3 })).closest('.mf-control-row') as HTMLElement)
    expect(iphone.getByText('Expires soon')).toBeInTheDocument()
    expect(iphone.getByText(/Stops approving payments in (1 d 23 h|2 d)\. Renew it to keep it working\./)).toBeInTheDocument()
    expect(iphone.getByRole('button', { name: 'Renew iPhone' })).toBeInTheDocument()
    expect(iphone.getByRole('button', { name: 'Revoke iPhone' })).toBeInTheDocument()
  })

  it('offers a renewal once a device has run out', async () => {
    stubServer({ wallets: [makeWalletState({ session: makeSession('expired') })] })
    await renderRoutes(routes, '/controls')

    const iphone = within((await screen.findByRole('heading', { name: 'iPhone', level: 3 })).closest('.mf-control-row') as HTMLElement)
    expect(iphone.getByText('Expired')).toBeInTheDocument()
    expect(iphone.getByText(/^Stopped approving payments on \w{3} \d{1,2}, \d{4}\. Renew it to use it again\.$/)).toBeInTheDocument()
    expect(iphone.getByRole('button', { name: 'Renew iPhone' })).toBeInTheDocument()
    expect(screen.getByText(/0 of 5 devices can approve payments\./)).toBeInTheDocument()
  })

  it('offers only to revoke a key added outside Mitfah', async () => {
    stubServer({ wallets: [makeWalletState({ session: makeSession('foreign') })] })
    await renderRoutes(routes, '/controls')

    const unknown = within(
      (await screen.findByRole('heading', { name: 'Unknown device', level: 3 })).closest('.mf-control-row') as HTMLElement,
    )
    expect(unknown.getByText(/Added to this wallet outside Mitfah/)).toBeInTheDocument()
    expect(unknown.getByRole('button', { name: 'Revoke Unknown device' })).toBeInTheDocument()
    expect(unknown.queryByRole('button', { name: /Renew|Rename/ })).toBeNull()
  })

  it('adds this device: its passkey is made inside the confirming tap, then the owner wallet adds it', async () => {
    const browser = installPasskeys()
    browser.create.mockResolvedValue(madeCredential(NEW_CREDENTIAL))
    const withLaptop = makeSessionBlock([
      makeSessionKey(),
      makeSessionKey({ key_id: NEW_PASSKEY.key_id, device_name: 'Work laptop', credential_id: NEW_CREDENTIAL }),
    ])
    const server = stubServer({ wallets: [makeWalletState(), makeWalletState({ session: withLaptop })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const name = await screen.findByLabelText('Name for this device')
    await user.clear(name)
    await user.type(name, 'Work laptop')
    await user.click(screen.getByRole('button', { name: 'Add this device' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(
      /Your browser will first ask you to create a passkey\. This device \(“Work laptop”\) will be able to approve the assistant's payments from this wallet for 90 days/,
    )
    // Fetched while the dialog asks, so the confirming tap can open the browser's prompt at once.
    await waitFor(() => expect(server.optionsAsked()).toBe(1))
    expect(browser.create).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Create passkey' }))
    expect(browser.create).toHaveBeenCalledTimes(1)

    expect(await screen.findByText(/^Work laptop can approve payments on Sepolia until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()
    expect(server.registered).toEqual([
      { credential: expect.objectContaining({ id: NEW_CREDENTIAL, type: 'public-key' }), device_name: 'Work laptop' },
    ])
    expect(server.prepared.map(p => p.body)).toEqual([
      { chain_id: SEPOLIA, action: 'add', key_id: NEW_PASSKEY.key_id, ttl_secs: 90 * 86_400 },
    ])
    // This browser made it, so the list says which device is this one.
    expect(await within(row('Work laptop')).findByText('This device')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add this device' })).toBeNull()
  })

  it('offers to add a new device on the other wallets too, one signature each', async () => {
    const browser = installPasskeys()
    browser.create.mockResolvedValue(madeCredential(NEW_CREDENTIAL))
    const server = stubServer({
      walletChains: [SEPOLIA, BSC],
      otherWallets: { [BSC]: makeWalletState({ chain_id: BSC, session: makeSession('off') }) },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const name = screen.getByLabelText('Name for this device')
    await user.clear(name)
    await user.type(name, 'Work laptop')
    await user.click(screen.getByRole('button', { name: 'Add this device' }))
    const dialog = await screen.findByRole('alertdialog')
    await waitFor(() => expect(server.optionsAsked()).toBe(1))
    await user.click(within(dialog).getByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByText(/^Work laptop can approve payments on Sepolia until/)).toBeInTheDocument()

    const others = within(screen.getByRole('region', { name: 'Add on your other wallets' }))
    expect(others.getByText(/Also let “Work laptop” approve payments from your other wallets\?/)).toBeInTheDocument()
    await user.click(await others.findByRole('button', { name: `Add on ${chainName(BSC)}` }))
    expect(
      await screen.findByText(new RegExp(`^Work laptop can approve payments on ${chainName(BSC)} until`)),
    ).toBeInTheDocument()
    expect(server.prepared.map(p => p.body)).toEqual([
      { chain_id: SEPOLIA, action: 'add', key_id: NEW_PASSKEY.key_id, ttl_secs: 90 * 86_400 },
      { chain_id: BSC, action: 'add', key_id: NEW_PASSKEY.key_id, ttl_secs: 90 * 86_400 },
    ])
    // The second went out on BSC, after the wallet switched there.
    expect(server.sentTo).toHaveLength(2)
    expect(server.sentTo[1]).not.toBe(server.sentTo[0])
  })

  it('uses the passkey this device already has, when the browser won’t make another', async () => {
    const browser = installPasskeys()
    browser.create.mockRejectedValue(refused('InvalidStateError'))
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const server = stubServer({ wallets: [makeWalletState({ session: makeSession('off') })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Add this device' }))
    const dialog = await screen.findByRole('alertdialog')
    await waitFor(() => expect(server.optionsAsked()).toBe(1))
    await user.click(within(dialog).getByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByText(/This device already has one of your Mitfah passkeys/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Use it' }))
    expect(browser.get).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/^iPhone can approve payments on Sepolia until/)).toBeInTheDocument()
    expect(server.registered).toEqual([])
    expect(server.prepared.map(p => p.body)).toEqual([
      { chain_id: SEPOLIA, action: 'add', key_id: PASSKEY_ID, ttl_secs: 90 * 86_400 },
    ])
  })

  it('offers no new device once the wallet trusts five', async () => {
    installPasskeys()
    const keys = [1, 2, 3, 4, 5].map(n =>
      makeSessionKey({ key_id: `0x${String(n).repeat(64)}`, device_name: `Phone ${n}`, credential_id: b64url(`phone-${n}`) }),
    )
    stubServer({
      wallets: [makeWalletState({ session: makeSessionBlock(keys) })],
      passkeys: [
        ...keys.map(key => makePasskey({ key_id: key.key_id, credential_id: key.credential_id!, device_name: key.device_name! })),
        makePasskey({ key_id: NEW_PASSKEY.key_id, credential_id: NEW_CREDENTIAL, device_name: 'Work laptop' }),
      ],
    })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/5 of 5 devices can approve payments\./)).toBeInTheDocument()
    const laptop = within(await waitFor(() => row('Work laptop')))
    expect(laptop.getByText(/already trusts 5 devices, the most it can\. Revoke one to add Work laptop\./)).toBeInTheDocument()
    expect(laptop.getByRole('button', { name: 'Add Work laptop' })).toBeDisabled()
    expect(within(row('Add this device')).getByText('Wallet full')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add this device' })).toBeDisabled()
  })

  it("says so when this browser can't make passkeys", async () => {
    stubServer({ wallets: [makeWalletState({ session: makeSession('off') })], passkeys: [] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/This browser can't make passkeys, so it can't approve payments\./)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add this device' })).toBeDisabled()
    expect(screen.queryByLabelText('Name for this device')).toBeNull()
  })

  it('renames a device everywhere its passkey shows, without the owner wallet', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    // Names belong to the account, not the chain: no wallet connected.
    await user.click(await screen.findByRole('button', { name: 'Rename iPhone' }))
    const input = screen.getByLabelText('New name')
    await user.clear(input)
    await user.type(input, "Sam's iPhone")
    await user.click(within(row('iPhone')).getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('heading', { name: "Sam's iPhone", level: 3 })).toBeInTheDocument()
    expect(server.renamed).toEqual([{ url: `/api/passkeys/${PASSKEY_ID}`, body: { device_name: "Sam's iPhone" } }])
    expect(server.prepared).toEqual([])
  })

  it('asks before raising the limit', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const limit = screen.getByLabelText('Limit per period')
    expect(limit).toHaveValue('100')
    const save = within(limit.closest('form')!).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.clear(limit)
    await user.type(limit, '250')
    await user.click(save)
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The assistant will be able to spend up to $250.00 every 24 hours, up from $100.00.')
    await user.click(within(dialog).getByRole('button', { name: 'Raise limit' }))

    expect(await screen.findByText('Spending limit set to $250.00.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      { path: '/api/wallet/daily-limit/prepare', body: { chain_id: SEPOLIA, daily_limit_usd: 250 } },
    ])
  })

  it('lowers the limit straight away, and explains a $0 limit', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const limit = screen.getByLabelText('Limit per period')
    await user.clear(limit)
    await user.type(limit, '0')
    expect(screen.getByText("A $0 limit stops the assistant spending without turning it off.")).toBeInTheDocument()
    await user.click(within(limit.closest('form')!).getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Spending limit set to $0.00.')).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(server.prepared[0].body).toEqual({ chain_id: SEPOLIA, daily_limit_usd: 0 })
  })

  it('asks before shortening the period, and a confirmed change resets the editor', async () => {
    const weekly = makeWalletState({ spending: { ...makeWalletState().spending, window_hours: 168 } })
    const daily = makeWalletState()
    const server = stubServer({ wallets: [weekly, daily] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const period = screen.getByRole('radiogroup', { name: 'Period' })
    expect(within(period).getByRole('radio', { name: '7 days' })).toBeChecked()
    await user.click(within(period).getByRole('radio', { name: '24 hours' }))
    await user.click(within(period.closest('form')!).getByRole('button', { name: 'Save' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Your limit will refill every 24 hours instead of every 7 days')
    await user.click(within(dialog).getByRole('button', { name: 'Shorten period' }))

    expect(await screen.findByText('The limit now refills every 24 hours.')).toBeInTheDocument()
    expect(server.prepared[0]).toEqual({ path: '/api/wallet/window-duration/prepare', body: { chain_id: SEPOLIA, window_secs: 86_400 } })
    const saved = screen.getByRole('radiogroup', { name: 'Period' })
    expect(within(saved).getByRole('radio', { name: '24 hours' })).toBeChecked()
    expect(within(saved.closest('form')!).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('counts a new token, and asks before it stops counting one', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const counted = screen.getByRole('list', { name: 'Counted tokens' })
    // ETH and its wrapped form share one row: they count together.
    const nativeRow = (await within(counted).findByText('ETH and WETH')).closest('li')!
    expect(nativeRow).toHaveTextContent('Counted')
    expect(within(counted).getByText('USDC')).toBeInTheDocument()

    // Any other listed token is added from the picker, which never offers WETH.
    await user.click(await screen.findByRole('combobox', { name: 'Add a token' }))
    expect(screen.queryByRole('option', { name: 'WETH' })).toBeNull()
    await user.click(await screen.findByRole('option', { name: 'LINK' }))
    await user.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText('LINK now counts toward your limit.')).toBeInTheDocument()

    await user.click(within(counted).getByRole('button', { name: 'Stop counting USDC' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The assistant will be able to move USDC out of this wallet without any limit.')
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting USDC' }))
    expect(await screen.findByText('USDC no longer counts toward your limit.')).toBeInTheDocument()

    expect(server.prepared).toEqual([
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'link', action: 'add' } },
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'usdc', action: 'remove' } },
    ])
  })

  it("sets a limit in a token's own units, never offering one that counts in USD", async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(await screen.findByRole('combobox', { name: 'Token' }))
    // USDC counts in USD and WETH always does, so neither can take a limit of its own.
    expect(screen.queryByRole('option', { name: 'USDC' })).toBeNull()
    expect(screen.queryByRole('option', { name: 'WETH' })).toBeNull()
    await user.click(await screen.findByRole('option', { name: 'LINK' }))
    await user.type(screen.getByLabelText('Token limit per period'), '150')
    await user.click(screen.getByRole('button', { name: 'Set limit' }))

    expect(await screen.findByText('LINK is limited to 150 per period.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/token-caps/prepare',
        body: { chain_id: SEPOLIA, token: LINK, action: 'set', limit: '150' },
      },
    ])
  })

  it('shows what is left of a limit, and asks before raising or removing it but not lowering it', async () => {
    const server = stubServer({
      wallets: [
        makeWalletState({
          spending: {
            ...makeWalletState().spending,
            token_caps: [
              {
                ticker: 'link',
                address: LINK,
                decimals: 18,
                limit_raw: '150000000000000000000',
                remaining_raw: '50000000000000000000',
              },
            ],
          },
        }),
      ],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const list = await screen.findByRole('list', { name: 'Tokens with their own limit' })
    expect(within(list).getByText('50 of 150 left')).toBeInTheDocument()

    // A token with its own limit can't count in USD too, and here that was the last one left to add.
    expect(screen.queryByRole('combobox', { name: 'Add a token' })).toBeNull()

    await user.click(await screen.findByRole('combobox', { name: 'Token' }))
    await user.click(await screen.findByRole('option', { name: 'LINK' }))
    const amount = screen.getByLabelText('Token limit per period')

    await user.type(amount, '100')
    await user.click(screen.getByRole('button', { name: 'Set limit' }))
    expect(await screen.findByText('LINK is limited to 100 per period.')).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).toBeNull()

    await user.clear(amount)
    await user.type(amount, '200')
    await user.click(screen.getByRole('button', { name: 'Set limit' }))
    const raising = await screen.findByRole('alertdialog')
    expect(raising).toHaveTextContent('up to 200 LINK per period, up from 150')
    await user.click(within(raising).getByRole('button', { name: 'Raise limit' }))
    expect(await screen.findByText('LINK is limited to 200 per period.')).toBeInTheDocument()

    await user.click(within(list).getByRole('button', { name: 'Remove the limit on LINK' }))
    const removing = await screen.findByRole('alertdialog')
    expect(removing).toHaveTextContent('The assistant will be able to move LINK out of this wallet without any limit.')
    await user.click(within(removing).getByRole('button', { name: 'Remove limit' }))
    expect(await screen.findByText('LINK no longer has a limit of its own.')).toBeInTheDocument()

    expect(server.prepared.map(p => p.body)).toEqual([
      { chain_id: SEPOLIA, token: LINK, action: 'set', limit: '100' },
      { chain_id: SEPOLIA, token: LINK, action: 'set', limit: '200' },
      { chain_id: SEPOLIA, token: LINK, action: 'remove' },
    ])
  })

  it('refuses a limit a token cannot hold, and offers only existing limits once the list is full', async () => {
    const cap = (address: string, ticker: string) => ({
      ticker,
      address,
      decimals: 6,
      limit_raw: '1000000',
      remaining_raw: '1000000',
    })
    const eight = Array.from({ length: 8 }, (_, i) => cap(`0x${String(i + 1).padStart(40, '0')}`, `t${i}`))
    stubServer({
      wallets: [makeWalletState({ spending: { ...makeWalletState().spending, token_caps: eight } })],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(await screen.findByRole('combobox', { name: 'Token' }))
    expect(screen.queryByRole('option', { name: 'LINK' })).toBeNull()
    await user.click(await screen.findByRole('option', { name: 'T0' }))
    await user.type(screen.getByLabelText('Token limit per period'), '1.1234567')
    expect(await screen.findByText("T0 can't be split into more than 6 decimal places.")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Set limit' })).toBeDisabled()
  })

  it("doesn't show WETH as a token of its own while the token list loads", async () => {
    let release!: () => void
    stubServer({ tokensReady: new Promise<void>(resolve => (release = resolve)) })
    await renderRoutes(routes, '/controls')

    // The wallet is here, but which token goes with ETH isn't known yet, so no rows are drawn.
    expect(await screen.findByRole('heading', { name: 'Tokens that count toward the limit' })).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Counted tokens' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop counting WETH' })).toBeNull()

    release()
    const counted = await screen.findByRole('list', { name: 'Counted tokens' })
    expect(within(counted).getByText('ETH and WETH')).toBeInTheDocument()
    expect(within(counted).queryByRole('button', { name: 'Stop counting WETH' })).toBeNull()
  })

  it('switches ETH and WETH together: asks before it stops counting them, not before it counts them', async () => {
    const on = makeWalletState()
    const off = makeWalletState({
      spending: { ...on.spending, native_metered: false, watched_tokens: [{ ticker: 'usdc', address: USDC }] },
    })
    // The wallet is read again after each change: on, then off, then on.
    const server = stubServer({ wallets: [on, off, on] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const counted = screen.getByRole('list', { name: 'Counted tokens' })
    const row = () => within(counted).getByText('ETH and WETH').closest('li')!
    await within(counted).findByText('ETH and WETH')
    expect(row()).toHaveTextContent('Counted')
    // WETH has no button of its own: the one switch moves both.
    expect(within(counted).queryByRole('button', { name: 'Stop counting WETH' })).toBeNull()
    expect(within(counted).getByRole('button', { name: 'Stop counting USDC' })).toBeInTheDocument()

    await user.click(within(row()).getByRole('button', { name: 'Stop counting ETH and WETH' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The assistant will be able to move ETH and WETH out of this wallet without any limit.')
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting ETH and WETH' }))
    expect(await screen.findByText('ETH and WETH no longer count toward your limit.')).toBeInTheDocument()

    // Read again, the wallet counts neither. Counting them tightens the limit, so nothing is asked.
    const count = await within(counted).findByRole('button', { name: 'Count ETH and WETH toward the limit' })
    expect(row()).toHaveTextContent('Not counted')
    // One click sends it: with no confirm button pressed, the success below proves nothing was asked.
    await user.click(count)
    expect(await screen.findByText('ETH and WETH now count toward your limit.')).toBeInTheDocument()

    expect(server.prepared).toEqual([
      { path: '/api/wallet/native-metering/prepare', body: { chain_id: SEPOLIA, metered: false } },
      { path: '/api/wallet/native-metering/prepare', body: { chain_id: SEPOLIA, metered: true } },
    ])
  })

  it('puts WETH back in step with ETH when the two were split on the wallet directly', async () => {
    const base = makeWalletState().spending
    // ETH counts but WETH was taken off the list; then the other way round.
    const ethOnly = makeWalletState({ spending: { ...base, watched_tokens: [{ ticker: 'usdc', address: USDC }] } })
    const wethOnly = makeWalletState({ spending: { ...base, native_metered: false } })
    const server = stubServer({ wallets: [ethOnly, wethOnly] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const counted = screen.getByRole('list', { name: 'Counted tokens' })
    expect(await within(counted).findByText('WETH not counted yet')).toBeInTheDocument()
    await user.click(within(counted).getByRole('button', { name: 'Count WETH toward the limit' }))
    expect(await screen.findByText('WETH now counts toward your limit, like ETH.')).toBeInTheDocument()

    expect(await within(counted).findByText('WETH still counted')).toBeInTheDocument()
    await user.click(within(counted).getByRole('button', { name: 'Stop counting WETH' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The assistant will be able to move WETH out of this wallet without any limit.')
    await user.click(within(dialog).getByRole('button', { name: 'Stop counting WETH' }))
    expect(await screen.findByText('WETH no longer counts toward your limit, like ETH.')).toBeInTheDocument()

    // Either way it is the one switch, set to where ETH already is.
    expect(server.prepared).toEqual([
      { path: '/api/wallet/native-metering/prepare', body: { chain_id: SEPOLIA, metered: true } },
      { path: '/api/wallet/native-metering/prepare', body: { chain_id: SEPOLIA, metered: false } },
    ])
  })

  it('asks for a spending limit before anything is counted on a wallet made without one', async () => {
    const base = makeWalletState().spending
    stubServer({
      wallets: [
        makeWalletState({
          spending: { ...base, daily_limit_usd: 0, spent_usd: 0, remaining_usd: 0, native_metered: false, watched_tokens: [] },
        }),
      ],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const counted = screen.getByRole('list', { name: 'Counted tokens' })
    // Counting something against a $0 limit would stop the assistant spending it at all.
    expect(await within(counted).findByRole('button', { name: 'Count ETH and WETH toward the limit' })).toBeDisabled()
    expect(screen.getByText(/^Set a spending limit above first\./)).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Add a token' })).toHaveAttribute('aria-disabled', 'true')
    // The limit itself says why it does nothing yet, instead of "a $0 limit stops the assistant".
    expect(screen.getByText(/^Nothing counts toward the limit yet, so it has no effect\./)).toBeInTheDocument()
    expect(screen.queryByText(/A \$0 limit stops the assistant/)).toBeNull()
  })

  it('says when every other listed token already counts, and leaves an unlisted one alone', async () => {
    const unlisted = '0x7777777777777777777777777777777777777777'
    stubServer({
      wallets: [
        makeWalletState({
          spending: {
            ...makeWalletState().spending,
            watched_tokens: [
              { ticker: 'link', address: LINK },
              { ticker: 'usdc', address: USDC },
              { ticker: 'weth', address: WETH },
              { ticker: null, address: unlisted },
            ],
          },
        }),
      ],
    })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText('Every other token Mitfah lists on this network already counts.')).toBeInTheDocument()
    expect(screen.getByText("Not a token Mitfah lists, so it can't be removed here.")).toBeInTheDocument()
  })

  it('trusts a spender only after a warning, and checks the address first', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(await screen.findByText('None.')).toBeInTheDocument()
    const input = screen.getByLabelText('Trust a spender')
    const trust = screen.getByRole('button', { name: 'Trust' })

    await user.type(input, '0x123')
    expect(screen.getByText('Enter a full address starting with 0x.')).toBeInTheDocument()
    expect(trust).toBeDisabled()

    await user.clear(input)
    await user.type(input, '0xeb2a2b8f6e0b1a1b3e5ec2df1d2b2e1ad2e1f0a7')
    await user.click(trust)
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent("Only trust a contract you know, such as your exchange's router.")
    await user.click(within(dialog).getByRole('button', { name: 'Trust spender' }))

    expect(await screen.findByText('Spender trusted.')).toBeInTheDocument()
    expect(server.prepared[0]).toEqual({
      path: '/api/wallet/trusted-spenders/prepare',
      body: { chain_id: SEPOLIA, spender: '0xeb2A2b8F6e0b1a1b3e5ec2dF1d2B2E1ad2E1F0A7', action: 'add' },
    })
  })

  it('removes a trusted spender without asking', async () => {
    const spender = '0xeb2A2b8F6e0b1a1b3e5ec2dF1d2B2E1ad2E1F0A7'
    const server = stubServer({
      wallets: [makeWalletState({ limits: { ...makeWalletState().limits, trusted_spenders: [spender] } })],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    await user.click(await screen.findByRole('button', { name: `Stop trusting ${spender}` }))
    expect(await screen.findByText('Spender removed.')).toBeInTheDocument()
    expect(server.prepared[0].body).toEqual({ chain_id: SEPOLIA, spender, action: 'remove' })
  })

  it("keeps the network's default spenders off the list, so they can't be removed by accident", async () => {
    const other = '0xeb2A2b8F6e0b1a1b3e5ec2dF1d2B2E1ad2E1F0A7'
    // Lower case on purpose: the match must not depend on how the address is written.
    const trusted = [V3_ROUTER.toLowerCase(), POSITION_MANAGER.toLowerCase(), other]
    stubServer({ wallets: [makeWalletState({ limits: { ...makeWalletState().limits, trusted_spenders: trusted } })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    const list = await screen.findByRole('list', { name: 'Trusted spenders' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(within(list).getByRole('button', { name: `Stop trusting ${other}` })).toBeInTheDocument()
    for (const spender of [V3_ROUTER, POSITION_MANAGER]) {
      expect(screen.queryByRole('button', { name: new RegExp(`Stop trusting ${spender}`, 'i') })).toBeNull()
    }
    expect(
      screen.getByText(
        'The Uniswap V3 router and Uniswap V3 positions are trusted by default, as the assistant uses them to swap and to add or remove liquidity.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText('None.')).toBeNull()
    expect(screen.queryByText('Not trusted yet')).toBeNull()

    // Still counted as trusted, so it can't be added a second time.
    await user.type(screen.getByLabelText('Trust a spender'), V3_ROUTER)
    expect(screen.getByText('Already trusted.')).toBeInTheDocument()
  })

  it('offers to trust what new wallets start with, where an older wallet lacks it', async () => {
    // Made before Sepolia moved to Uniswap V3: it trusts only V2's router, now an ordinary spender.
    const server = stubServer({
      wallets: [makeWalletState({ limits: { ...makeWalletState().limits, trusted_spenders: [V2_ROUTER] } })],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    const trusted = await screen.findByRole('list', { name: 'Trusted spenders' })
    expect(within(trusted).getByRole('button', { name: `Stop trusting ${V2_ROUTER}` })).toBeInTheDocument()
    const missing = within(screen.getByRole('list', { name: 'Spenders new wallets trust' })).getAllByRole('listitem')
    expect(missing).toHaveLength(2)
    expect(missing[0]).toHaveTextContent('Uniswap V3 router')
    expect(missing[0]).toHaveTextContent('Not trusted yet')
    expect(missing[1]).toHaveTextContent('Uniswap V3 positions')
    expect(
      screen.getByText(
        'New wallets trust these from the start. Without them, the assistant can only swap and provide liquidity with tokens Mitfah can price.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(/trusted by default/)).toBeNull()

    // The same question as trusting any spender, naming this one.
    await user.click(within(missing[1]).getByRole('button', { name: 'Trust Uniswap V3 positions' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Trust this spender?')
    expect(dialog).toHaveTextContent(`Uniswap V3 positions (0x1238…DA52${POSITION_MANAGER})`)
    expect(dialog).toHaveTextContent("will be allowed approvals for tokens Mitfah can't price")
    expect(dialog).toHaveTextContent('New wallets on this network trust it from the start.')
    await user.click(within(dialog).getByRole('button', { name: 'Trust spender' }))

    expect(await screen.findByText('Uniswap V3 positions trusted.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/trusted-spenders/prepare',
        body: { chain_id: SEPOLIA, spender: POSITION_MANAGER, action: 'add' },
      },
    ])
  })

  it("names BSC's default spender, PancakeSwap's router", async () => {
    stubServer({
      walletChains: [BSC],
      wallets: [
        makeWalletState({
          chain_id: BSC,
          limits: { ...makeWalletState().limits, trusted_spenders: [PANCAKE_ROUTER.toLowerCase()] },
        }),
      ],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await user.click(await screen.findByRole('button', { name: 'Advanced' }))
    expect(
      await screen.findByText(
        'The PancakeSwap router is trusted by default, as the assistant uses it to swap and to add or remove liquidity.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Trusted spenders' })).toBeNull()
    expect(screen.queryByText('Not trusted yet')).toBeNull()
  })

  it('asks before raising the network fee cap', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    const cap = await screen.findByLabelText('Most per operation')
    expect(cap).toHaveValue('0.01')
    await user.clear(cap)
    await user.type(cap, '0.05')
    await user.click(within(cap.closest('form')!).getByRole('button', { name: 'Save' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('up to 0.05 ETH in fees, up from 0.01 ETH')
    await user.click(within(dialog).getByRole('button', { name: 'Raise cap' }))
    expect(await screen.findByText('Network fee cap set to 0.05 ETH.')).toBeInTheDocument()
    expect(server.prepared[0]).toEqual({
      path: '/api/wallet/max-op-gas-cost/prepare',
      body: { chain_id: SEPOLIA, max_cost_eth: '0.05' },
    })
  })

  it("shows what's left of the daily fee budget, and asks before raising it", async () => {
    const limits = makeWalletState().limits
    const refillsAt = Math.floor(Date.now() / 1000) + 5 * 3_600
    const server = stubServer({
      wallets: [
        makeWalletState({
          limits: {
            ...limits,
            session_gas: { budget_wei: '50000000000000000', remaining_wei: '20000000000000000', refills_at: refillsAt },
          },
        }),
      ],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    const budget = await screen.findByLabelText('Most per day')
    expect(budget).toHaveValue('0.05')
    expect(
      within(row('Daily fee budget')).getByText(/^0\.02 of 0\.05 ETH left\. Refills in (4 h 59 min|5 h)\.$/),
    ).toBeInTheDocument()

    await user.clear(budget)
    await user.type(budget, '0.1')
    await user.click(within(budget.closest('form')!).getByRole('button', { name: 'Save' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('up to 0.1 ETH in fees a day, up from 0.05 ETH')
    await user.click(within(dialog).getByRole('button', { name: 'Raise budget' }))
    expect(await screen.findByText('Daily fee budget set to 0.1 ETH.')).toBeInTheDocument()
    expect(server.prepared[0]).toEqual({
      path: '/api/wallet/session-gas-budget/prepare',
      body: { chain_id: SEPOLIA, budget_eth: '0.1' },
    })
  })

  it('lowers the daily fee budget without asking', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(within(row('Daily fee budget')).getByText('All 0.05 ETH left.')).toBeInTheDocument()
    const budget = await screen.findByLabelText('Most per day')
    await user.clear(budget)
    await user.type(budget, '0.01')
    await user.click(within(budget.closest('form')!).getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Daily fee budget set to 0.01 ETH.')).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(server.prepared[0]).toEqual({
      path: '/api/wallet/session-gas-budget/prepare',
      body: { chain_id: SEPOLIA, budget_eth: '0.01' },
    })
  })

  it('says so when the wallet was made before daily fee budgets', async () => {
    const limits = makeWalletState().limits
    stubServer({ wallets: [makeWalletState({ limits: { ...limits, session_gas: null } })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(
      await within(row('Daily fee budget')).findByText('This wallet was made before daily fee budgets existed.'),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('Most per day')).toBeNull()
  })

  it('turns the contract allowlist on with what the assistant uses, in one signature', async () => {
    const server = stubServer({
      wallets: [makeWalletState(), walletWithAllowlistOn()],
      allowlists: [ALLOWLIST_OFF, { enabled: true, targets: [ROUTER_ENTRY, REGISTRY_ENTRY], suggested: [USDC_ENTRY] }],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    // Everything the assistant uses comes ticked; leaving one off keeps it off the list.
    const usdc = await within(row('Contract allowlist')).findByRole('checkbox', { name: /USDC/ })
    expect(within(row('Contract allowlist')).getByText('Off')).toBeInTheDocument()
    expect(usdc).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /Uniswap V3 router/ })).toBeChecked()
    await user.click(usdc)
    await user.click(within(row('Contract allowlist')).getByRole('button', { name: 'Turn on' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The assistant will only be able to call these 2 contracts')
    expect(dialog).toHaveTextContent('Uniswap V3 router')
    expect(dialog).not.toHaveTextContent('USDC')
    await user.click(within(dialog).getByRole('button', { name: 'Turn on' }))

    expect(
      await screen.findByText('Contract allowlist on. The assistant can only call the contracts listed.'),
    ).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/allowlist/prepare',
        body: { chain_id: SEPOLIA, action: 'enable', targets: [V3_ROUTER, REGISTRY] },
      },
    ])
    // Read again: on, with its two entries, and USDC still on offer.
    expect(await within(row('Contract allowlist')).findByText('On')).toBeInTheDocument()
    const listed = await screen.findByRole('list', { name: 'Allowed contracts' })
    expect(within(listed).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByRole('checkbox', { name: /USDC/ })).not.toBeChecked()
  })

  it('while the allowlist is on, removes straight away but asks before allowing more or turning it off', async () => {
    const server = stubServer({
      wallets: [walletWithAllowlistOn()],
      allowlists: [{ enabled: true, targets: [ROUTER_ENTRY, USDC_ENTRY], suggested: [REGISTRY_ENTRY] }],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    // Removing tightens, so nothing is asked.
    await user.click(await screen.findByRole('button', { name: 'Remove USDC from the allowlist' }))
    expect(await screen.findByText('USDC removed from the allowlist.')).toBeInTheDocument()
    expect(server.prepared.at(-1)).toEqual({
      path: '/api/wallet/allowlist/prepare',
      body: { chain_id: SEPOLIA, action: 'remove', targets: [USDC] },
    })

    // Allowing more loosens it: nothing comes ticked, and it asks first.
    const registry = within(row('Contract allowlist')).getByRole('checkbox', { name: /review registry/ })
    expect(registry).not.toBeChecked()
    await user.click(registry)
    await user.click(screen.getByRole('button', { name: 'Allow selected' }))
    let dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('ERC-8004 review registry')
    await user.click(within(dialog).getByRole('button', { name: 'Allow' }))
    expect(await screen.findByText('Added to the allowlist.')).toBeInTheDocument()
    expect(server.prepared.at(-1)?.body).toEqual({ chain_id: SEPOLIA, action: 'add', targets: [REGISTRY] })

    await user.click(within(row('Contract allowlist')).getByRole('button', { name: 'Turn off' }))
    dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('able to call any contract again')
    await user.click(within(dialog).getByRole('button', { name: 'Turn off' }))
    expect(await screen.findByText('Contract allowlist off.')).toBeInTheDocument()
    expect(server.prepared.at(-1)?.body).toEqual({ chain_id: SEPOLIA, action: 'disable', targets: [] })
  })

  it('checks a typed address, and warns before emptying an allowlist that is on', async () => {
    const server = stubServer({
      wallets: [walletWithAllowlistOn()],
      allowlists: [{ enabled: true, targets: [ROUTER_ENTRY], suggested: [] }],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    const input = await screen.findByLabelText('Add a contract')
    const add = within(row('Contract allowlist')).getByRole('button', { name: 'Add' })
    await user.type(input, '0x123')
    expect(screen.getByText('Enter a full address starting with 0x.')).toBeInTheDocument()
    expect(add).toBeDisabled()
    await user.clear(input)
    // Lower case on purpose: the match must not depend on how the address is written.
    await user.type(input, V3_ROUTER.toLowerCase())
    expect(screen.getByText('Already on the list.')).toBeInTheDocument()
    await user.clear(input)
    await user.type(input, makeWalletState().address)
    expect(screen.getByText("That's this wallet's own address.")).toBeInTheDocument()
    expect(add).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Remove Uniswap V3 router from the allowlist' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent("won't be able to call any contract until you add one or turn the list off")
    await user.click(within(dialog).getByRole('button', { name: 'Remove it' }))
    expect(await screen.findByText('Uniswap V3 router removed from the allowlist.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      { path: '/api/wallet/allowlist/prepare', body: { chain_id: SEPOLIA, action: 'remove', targets: [V3_ROUTER] } },
    ])
  })

  it("shows an older wallet's allowlist state, but not a list it can't read", async () => {
    stubServer({ wallets: [walletWithAllowlistOn()], allowlists: [{ enabled: true, targets: null, suggested: [] }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(await screen.findByText(/created before Mitfah could show its list/)).toBeInTheDocument()
    expect(within(row('Contract allowlist')).getByText('On')).toBeInTheDocument()
    expect(screen.queryByLabelText('Add a contract')).toBeNull()
  })

  it('explains a change the wallet would refuse, before anything is signed', async () => {
    const server = stubServer({
      refuse: { '/api/wallet/pause/prepare': 'This transaction would fail: EnforcedPause()' },
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))
    expect(await within(row('Wallet')).findByText('The wallet is already paused.')).toBeInTheDocument()
    expect(server.sent).toEqual([])
  })

  it('says so when the change is declined in the wallet', async () => {
    const server = stubServer({ rejectSend: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))
    expect(await within(row('Wallet')).findByText('Cancelled — nothing was sent.')).toBeInTheDocument()
    expect(server.confirms).toEqual([])
    // Nothing is left waiting, so the next change can start.
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeEnabled()
  })

  it('asks for the owner wallet before anything can change', async () => {
    stubServer()
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/Connect your owner wallet/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
    expect(screen.getByLabelText('Limit per period')).toBeDisabled()
  })

  it('points out a connected wallet that is not the owner', async () => {
    const owner = '0x9999999999999999999999999999999999999999'
    stubServer({ wallets: [makeWalletState({ owner })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connectFromOwnerBar(user)
    // The addresses sit in elements of their own, so only the words around them are matched.
    expect(await screen.findByText(/You're signed in as .*but your wallet is connected as/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Link this address instead' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
  })

  it("is read-only for an account that isn't linked to the owner", async () => {
    stubServer({ wallets: [makeWalletState({ is_owner: false })] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/so they're switched off here/)).toBeInTheDocument()
    expect(screen.queryByText(/Connect your owner wallet/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Revoke iPhone' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Revoke all' })).toBeDisabled()
  })

  it('switches the wallet to the right network before signing', async () => {
    const server = stubServer({ walletChains: [MAINNET], wallets: [makeWalletState({ chain_id: MAINNET })] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(screen.getByText(/Your wallet is on Sepolia/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))

    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(server.prepared[0].body).toEqual({ chain_id: MAINNET })
    expect(server.sentTo).toHaveLength(1)
    expect(server.sentTo[0]).not.toMatch(/sepolia/)
    expect(screen.queryByText(/Your wallet is on Sepolia/)).toBeNull()
  })

  it('keeps waiting for a change sent before a reload', async () => {
    sessionStorage.setItem(
      PENDING_KEY,
      JSON.stringify({ chainId: SEPOLIA, txHash: TX_HASH, key: 'pause', success: 'Wallet paused. Nothing can go out until you unpause it.' }),
    )
    const server = stubServer()
    await renderRoutes(routes, '/controls')

    // No wallet connected: waiting needs only the API.
    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(server.confirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
    ])
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it("finishes a device's change through its own endpoint after a reload", async () => {
    sessionStorage.setItem(
      PENDING_KEY,
      JSON.stringify({
        chainId: SEPOLIA,
        txHash: TX_HASH,
        key: `device-renew:${PASSKEY_ID}`,
        success: 'iPhone renewed.',
        confirmWith: 'session',
      }),
    )
    const server = stubServer()
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText('iPhone renewed.')).toBeInTheDocument()
    expect(server.sessionConfirms).toHaveLength(2)
    expect(server.confirms).toEqual([])
  })

  it("doesn't wait on this network for a change sent on another", async () => {
    sessionStorage.setItem(
      `mitfah-pending-owner-tx:7:${MAINNET}`,
      JSON.stringify({ chainId: MAINNET, txHash: TX_HASH, key: 'pause', success: 'Wallet paused.', sentAt: Date.now() }),
    )
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(screen.queryByText(/Waiting for/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeEnabled()
    expect(server.confirms).toEqual([])
  })

  it('stops waiting for a change sent more than ten minutes ago', async () => {
    sessionStorage.setItem(
      PENDING_KEY,
      JSON.stringify({ chainId: SEPOLIA, txHash: TX_HASH, key: 'pause', success: 'Wallet paused.', sentAt: Date.now() - 11 * 60_000 }),
    )
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(screen.getByRole('button', { name: 'Pause wallet' })).toBeEnabled()
    expect(server.confirms).toEqual([])
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it('finishes a change on the next page if this one is left while the wallet is open', async () => {
    let approve!: () => void
    const server = stubServer({ walletPrompt: new Promise(resolve => (approve = resolve)) })
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(screen.getByRole('button', { name: 'Pause wallet' }))
    expect(await screen.findByText('Confirm in your wallet.')).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: 'Dashboard' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/dashboard'))
    await screen.findByText(/^Your Mitfah smart wallet on/)

    approve()
    expect(await screen.findByText('Wallet paused. Nothing can go out until you unpause it.')).toBeInTheDocument()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
    // One page waited for it, not two.
    expect(server.confirms).toHaveLength(2)
    expect(screen.getAllByText('Wallet paused. Nothing can go out until you unpause it.')).toHaveLength(1)
  })

  it('offers to create a wallet when there is none', async () => {
    stubServer({ walletChains: [] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByRole('heading', { name: 'Create your wallet' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Controls', level: 1 })).toBeInTheDocument()
  })
})


describe('ControlsPage — recurring payments', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
    sessionStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('lists live schedules with their next payment and why the last attempt failed', async () => {
    const failing = { ...RENT, last_error: 'BudgetExceeded', retry_at: 1_900_000_300 }
    stubServer({ recurring: [{ ...NO_RECURRING, schedules: [failing, { ...RENT, schedule_id: 3, status: 'cancelled' }] }] })
    await renderRoutes(routes, '/controls')

    const list = await screen.findByRole('list', { name: 'Recurring payments' })
    // Only live ones: a cancelled schedule isn't listed.
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(within(list).getByText('20 USDC to landlord every 30 days')).toBeInTheDocument()
    expect(within(list).getByText(/^Next payment/)).toBeInTheDocument()
    expect(within(list).getByText(/The last attempt couldn't go through: BudgetExceeded\. Trying again/)).toBeInTheDocument()
  })

  it('sets one up to a contact, asking first, and confirms it on its own endpoint', async () => {
    const after = { ...NO_RECURRING, schedules: [RENT] }
    const server = stubServer({ recurring: [NO_RECURRING, after], contacts: [{ name: 'landlord', address: PAYEE }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(await screen.findByText('No recurring payments yet.')).toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'Pay with' }))
    await user.click(await screen.findByRole('option', { name: 'USDC' }))
    await user.type(screen.getByLabelText('Amount each time'), '20')
    await user.click(screen.getByRole('combobox', { name: 'Pay to' }))
    await user.click(await screen.findByRole('option', { name: 'landlord' }))
    await user.click(screen.getByRole('button', { name: 'Set up payment' }))

    // Money leaves without asking again, so it asks once, here.
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('send 20 USDC to landlord every 30 days, starting now, until you cancel it')
    expect(dialog).toHaveTextContent('0.00002 ETH')
    await user.click(within(dialog).getByRole('button', { name: 'Set up' }))

    expect(await screen.findByText('Recurring payment set up.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/recurring/prepare',
        body: {
          chain_id: SEPOLIA,
          token: USDC,
          amount: '20',
          to: PAYEE,
          interval_secs: 2_592_000,
          first_due: null,
          end_at: null,
        },
      },
    ])
    expect(server.recurringConfirms).toHaveLength(2)
    expect(server.confirms).toEqual([])
    // The confirm's answer is the list now: no wait for another read.
    expect(await screen.findByText('20 USDC to landlord every 30 days')).toBeInTheDocument()
  })

  it('refuses an interval under an hour and a payment to the wallet itself, before anything is signed', async () => {
    const server = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.type(await screen.findByLabelText('Amount each time'), '1')
    await user.type(screen.getByLabelText('Pay to'), makeWalletState().address)
    expect(screen.getByText("The wallet can't pay itself.")).toBeInTheDocument()
    await user.clear(screen.getByLabelText('Every'))
    await user.type(screen.getByLabelText('Every'), '0')
    expect(screen.getByText('Pay at most once an hour.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Set up payment' })).toBeDisabled()
    expect(server.prepared).toEqual([])
  })

  it('cancels one in a tap', async () => {
    const server = stubServer({ recurring: [{ ...NO_RECURRING, schedules: [RENT] }, NO_RECURRING] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(await screen.findByRole('button', { name: 'Cancel the payment of 20 USDC to landlord every 30 days' }))

    expect(await screen.findByText('Recurring payment cancelled.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      { path: '/api/wallet/recurring/cancel/prepare', body: { chain_id: SEPOLIA, schedule_id: 4 } },
    ])
    expect(await screen.findByText('No recurring payments yet.')).toBeInTheDocument()
  })

  it('says so for a wallet without the module', async () => {
    stubServer({ recurring: [{ ...NO_RECURRING, available: false, module: null }] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/This wallet can't make recurring payments/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Set up payment' })).toBeNull()
  })
})

describe('ControlsPage — trading agents', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
    sessionStorage.clear()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  /** An agent's row, once the list has loaded. */
  const agentRow = async (title: string) =>
    (await screen.findByRole('heading', { name: title, level: 3 })).closest('.mf-control-row') as HTMLElement

  it('lists each agent with what it has spent, until when it works, and its address to copy', async () => {
    const quiet: Agent = { ...BOT, address: NEW_AGENT_ADDRESS, name: null, active: false, valid_until: 1_790_000_000 }
    stubServer({ agents: [withAgents(BOT, quiet)] })
    await renderRoutes(routes, '/controls')

    const list = await screen.findByRole('list', { name: 'Trading agents' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    const bot = within(await agentRow('“Bot”'))
    expect(bot.getByText('Active')).toBeInTheDocument()
    expect(bot.getByText(/^\$12\.50 of \$50\.00 spent this period\. Works until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()
    expect(bot.getByText(BOT_ADDRESS)).toBeInTheDocument()
    expect(bot.getByRole('button', { name: "Copy agent's address" })).toBeInTheDocument()
    // One the owner never named goes by its short address.
    const unnamed = within(await agentRow('0x5555…5555'))
    expect(unnamed.getByText('Expired')).toBeInTheDocument()
    expect(unnamed.getByText(/^Stopped working on \w{3} \d{1,2}, \d{4}\./)).toBeInTheDocument()
  })

  it('adds an agent after asking, and keeps its name with the confirm', async () => {
    const added: Agent = { ...BOT, address: NEW_AGENT_ADDRESS, name: 'Scalper', limit_usd: 25, spent_usd: 0 }
    const server = stubServer({ agents: [NO_AGENTS, withAgents(added)] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(await screen.findByText('No agents yet.')).toBeInTheDocument()
    await user.type(screen.getByLabelText("Agent's name"), 'Scalper')
    await user.type(screen.getByLabelText("Agent's address"), NEW_AGENT_ADDRESS.toLowerCase())
    await user.type(screen.getByLabelText("Agent's limit per period"), '25')
    await user.click(screen.getByRole('button', { name: 'Add agent' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Add “Scalper” as a trading agent?')).toBeInTheDocument()
    expect(dialog).toHaveTextContent(/without your passkey, on any exchange or protocol, up to \$25\.00 every 24 hours until/)
    expect(dialog).toHaveTextContent(/already has in other protocols, such as pool positions or deposits, are outside the limit/)
    expect(dialog).toHaveTextContent("Revoking its access token won't stop it.")
    await user.click(within(dialog).getByRole('button', { name: 'Add agent' }))

    expect(await screen.findByText('“Scalper” can trade now, up to $25.00 every 24 hours.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/agents/prepare',
        body: { chain_id: SEPOLIA, action: 'add', agent: NEW_AGENT_ADDRESS, limit_usd: '25', days: 30 },
      },
    ])
    // The name goes with the confirm, which records it: never to the chain.
    expect(server.agentConfirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH, name: 'Scalper' },
      { chain_id: SEPOLIA, tx_hash: TX_HASH, name: 'Scalper' },
    ])
    expect(await agentRow('“Scalper”')).toBeInTheDocument()
    // A fresh form for the next one.
    expect(screen.getByLabelText("Agent's name")).toHaveValue('')
  })

  it("makes the agent's key in the browser, shows it once, and never sends it anywhere", async () => {
    const server = stubServer({ agents: [NO_AGENTS, withAgents(BOT)] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.type(await screen.findByLabelText("Agent's name"), 'Bot')
    await user.click(screen.getByRole('button', { name: 'No key yet? Make one here' }))
    const note = (await screen.findByText(/^Your agent's private key\./)).closest('.rs-message') as HTMLElement
    const key = within(note).getByText(/^0x[0-9a-f]{64}$/).textContent as `0x${string}`
    const address = privateKeyToAccount(key).address
    expect(screen.getByLabelText("Agent's address")).toHaveValue(address)
    expect(within(note).getByRole('button', { name: 'Copy private key' })).toBeInTheDocument()
    await user.type(screen.getByLabelText("Agent's limit per period"), '10')

    // Not added until the key is somewhere: an agent nobody holds the key of is no use.
    const add = screen.getByRole('button', { name: 'Add agent' })
    expect(add).toBeDisabled()
    await user.click(within(note).getByRole('checkbox', { name: "I've put this key in my agent" }))
    expect(add).toBeEnabled()
    await user.click(add)
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Add agent' }))

    expect(await screen.findByText('“Bot” can trade now, up to $10.00 every 24 hours.')).toBeInTheDocument()
    expect(server.prepared[0].body.agent).toBe(address)
    // Nothing the page sent, to Mitfah or to the browser wallet, carried the key.
    expect(server.bodies.length).toBeGreaterThan(0)
    expect(server.bodies.some(sent => sent.toLowerCase().includes(key.slice(2)))).toBe(false)
    // And it is gone from the page once the agent is added.
    expect(screen.queryByText(key)).toBeNull()
  })

  it('forgets a key made here once another address is typed', async () => {
    stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.click(await screen.findByRole('button', { name: 'No key yet? Make one here' }))
    expect(await screen.findByText(/^Your agent's private key\./)).toBeInTheDocument()
    const address = screen.getByLabelText("Agent's address")
    await user.clear(address)
    await user.type(address, NEW_AGENT_ADDRESS)
    expect(screen.queryByText(/^Your agent's private key\./)).toBeNull()
  })

  it("refuses the wallet's own address or its owner's as an agent", async () => {
    stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await user.type(await screen.findByLabelText("Agent's address"), WALLET)
    expect(screen.getByText("An agent needs a key of its own, not the wallet's or its owner's.")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add agent' })).toBeDisabled()
  })

  it('counts ETH first: no agent can be added, or trade, while the limit leaves it out', async () => {
    const wallet = makeWalletState()
    const notCounted = makeWalletState({ spending: { ...wallet.spending, native_metered: false } })
    const server = stubServer({ wallets: [notCounted, wallet], agents: [{ ...NO_AGENTS, native_metered: false }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    expect(await screen.findByText(/^Agents can trade only while the limit counts ETH/)).toBeInTheDocument()
    expect(screen.queryByLabelText("Agent's name")).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Count ETH' }))

    expect(await screen.findByText('ETH now counts toward your limit.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      { path: '/api/wallet/native-metering/prepare', body: { chain_id: SEPOLIA, metered: true } },
    ])
    expect(await screen.findByLabelText("Agent's name")).toBeInTheDocument()
  })

  it('offers no form once the wallet has as many agents as it can', async () => {
    const two = { ...BOT, address: NEW_AGENT_ADDRESS, name: 'Two' }
    const three = { ...BOT, address: '0x6666666666666666666666666666666666666666', name: 'Three' }
    stubServer({ agents: [withAgents(BOT, two, three)] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText('This wallet has 3 agents, the most it can have. Remove one to add another.')).toBeInTheDocument()
    expect(screen.queryByLabelText("Agent's name")).toBeNull()
  })

  it("says so for a wallet made before agents existed", async () => {
    stubServer({ agents: [{ ...NO_AGENTS, available: false, module: null, native_metered: null }] })
    await renderRoutes(routes, '/controls')

    expect(await screen.findByText(/^This wallet can't have trading agents: it was created before they existed\./)).toBeInTheDocument()
    expect(screen.queryByLabelText("Agent's name")).toBeNull()
  })

  it('removes an agent with the owner wallet in one click', async () => {
    const server = stubServer({ agents: [withAgents(BOT), NO_AGENTS] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const bot = within(await agentRow('“Bot”'))
    // With the owner wallet here, it is the owner's Remove, not the passkey's stop.
    expect(bot.queryByRole('button', { name: 'Stop “Bot” with your passkey' })).toBeNull()
    await user.click(bot.getByRole('button', { name: 'Remove “Bot”' }))

    expect(await screen.findByText('“Bot” can no longer trade from this wallet.')).toBeInTheDocument()
    expect(server.prepared).toEqual([
      { path: '/api/wallet/agents/prepare', body: { chain_id: SEPOLIA, action: 'remove', agent: BOT_ADDRESS } },
    ])
    expect(server.agentConfirms).toEqual([
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
      { chain_id: SEPOLIA, tx_hash: TX_HASH },
    ])
    await waitFor(() => expect(screen.queryByRole('heading', { name: '“Bot”', level: 3 })).toBeNull())
  })

  it('asks before giving an agent more, and not before giving it less', async () => {
    const server = stubServer({ agents: [withAgents(BOT)] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    // Less: a lower limit, and 7 days, sooner than it runs out now.
    let bot = within(await agentRow('“Bot”'))
    await user.click(bot.getByRole('button', { name: 'Change “Bot”' }))
    let limit = bot.getByLabelText('New limit per period')
    await user.clear(limit)
    await user.type(limit, '20')
    await user.click(bot.getByRole('radio', { name: '7 days' }))
    await user.click(bot.getByRole('button', { name: 'Save' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(await screen.findByText(/^“Bot” may now spend up to \$20\.00 every 24 hours, until/)).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/agents/prepare',
        body: { chain_id: SEPOLIA, action: 'update', agent: BOT_ADDRESS, limit_usd: '20', days: 7 },
      },
    ])

    // More: a higher limit asks first.
    bot = within(await agentRow('“Bot”'))
    await user.click(bot.getByRole('button', { name: 'Change “Bot”' }))
    limit = bot.getByLabelText('New limit per period')
    await user.clear(limit)
    await user.type(limit, '80')
    await user.click(bot.getByRole('radio', { name: '7 days' }))
    await user.click(bot.getByRole('button', { name: 'Save' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Give “Bot” more?')).toBeInTheDocument()
    expect(dialog).toHaveTextContent(/up to \$80\.00 every 24 hours, until \w{3} \d{1,2}, \d{4}, without your passkey/)
    await user.click(within(dialog).getByRole('button', { name: 'Change agent' }))
    await waitFor(() => expect(server.prepared).toHaveLength(2))
    expect(server.prepared[1].body).toEqual({ chain_id: SEPOLIA, action: 'update', agent: BOT_ADDRESS, limit_usd: '80', days: 7 })
  })

  it("renames an agent in Mitfah only, with nothing to sign", async () => {
    const server = stubServer({ agents: [withAgents(BOT)] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    const bot = within(await agentRow('“Bot”'))
    await user.click(bot.getByRole('button', { name: 'Rename “Bot”' }))
    const name = bot.getByLabelText('New name')
    await user.clear(name)
    await user.type(name, 'Grid bot')
    await user.click(bot.getByRole('button', { name: 'Save' }))

    expect(await agentRow('“Grid bot”')).toBeInTheDocument()
    expect(server.agentRenames).toEqual([{ chain_id: SEPOLIA, agent: BOT_ADDRESS, name: 'Grid bot' }])
    expect(server.prepared).toEqual([])
  })

  it('stops an agent with a passkey when the owner wallet is away', async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const server = stubServer({ agents: [withAgents(BOT), NO_AGENTS] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    // No owner wallet connected: the owner's Remove can't be signed, so the passkey's stop is offered.
    const bot = within(await agentRow('“Bot”'))
    expect(bot.queryByRole('button', { name: 'Remove “Bot”' })).toBeNull()
    await user.click(bot.getByRole('button', { name: 'Stop “Bot” with your passkey' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Stop “Bot” with your passkey?')).toBeInTheDocument()
    expect(await within(dialog).findByText(/^It costs \$0\.12 in fees, paid from the wallet/)).toBeInTheDocument()
    // The confirmation is started before the tap, so the tap opens the passkey prompt at once.
    const approve = within(dialog).getByRole('button', { name: 'Approve with passkey' })
    await waitFor(() => expect(approve).toBeEnabled())
    await user.click(approve)

    expect(await screen.findByText('“Bot” can no longer trade from this wallet.')).toBeInTheDocument()
    expect(server.stops).toEqual([{ chain_id: SEPOLIA, agent: BOT_ADDRESS }])
    expect(server.signStarts).toEqual([{ chain_id: SEPOLIA, quote_id: STOP_QUOTE.quote_id }])
    expect(browser.get).toHaveBeenCalledTimes(1)
    expect(server.signFinishes).toEqual([
      { sign_id: 'sign-1', credential: expect.objectContaining({ id: PASSKEY_CREDENTIAL }) },
    ])
    // Followed until it landed: pending, then confirmed.
    expect(server.stopReads()).toBe(2)
    expect(server.signCancels).toEqual([])
    await waitFor(() => expect(screen.queryByRole('heading', { name: '“Bot”', level: 3 })).toBeNull())
    expect(server.prepared).toEqual([])
  })

  it('drops the confirmation when the stop dialog is closed, and sends nothing', async () => {
    installPasskeys()
    const server = stubServer({ agents: [withAgents(BOT)] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await user.click(within(await agentRow('“Bot”')).getByRole('button', { name: 'Stop “Bot” with your passkey' }))
    const dialog = await screen.findByRole('alertdialog')
    await within(dialog).findByText(/^It costs \$0\.12/)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(server.signCancels).toEqual([{ chain_id: SEPOLIA, quote_id: STOP_QUOTE.quote_id }]))
    expect(server.signFinishes).toEqual([])
  })

  it("says why a passkey can't stop it, and offers nothing to approve", async () => {
    installPasskeys()
    const reason =
      "Your wallet's contract allowlist is on and doesn't list the agent module, so a passkey can't reach it. Remove the agent with your owner wallet instead."
    stubServer({ agents: [withAgents(BOT)], refuseStop: reason })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await user.click(within(await agentRow('“Bot”')).getByRole('button', { name: 'Stop “Bot” with your passkey' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByText(reason)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Approve with passkey' })).toBeDisabled()
  })

  it('warns that agents stop trading when ETH stops counting', async () => {
    stubServer({ agents: [withAgents(BOT)] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    await agentRow('“Bot”')
    await user.click(screen.getByRole('button', { name: 'Stop counting ETH and WETH' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Your trading agents will stop trading until it counts again.')
  })
})
