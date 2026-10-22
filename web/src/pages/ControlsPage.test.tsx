import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { Allowlist, AllowlistEntry, Passkey, RegisteredPasskey, WalletState } from '../api/types'
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
  // The API flags the wrapped native token: it always counts, like ETH.
  { ticker: 'weth', address: WETH, always_counted: true },
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
}: ServerOptions = {}) {
  const prepared: Prepared[] = []
  const confirms: Record<string, unknown>[] = []
  const sessionConfirms: Record<string, unknown>[] = []
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
      if (url.startsWith('/api/tokens')) return Promise.resolve(json(200, { tokens: TOKENS }))
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
      /iPhone keeps approving the assistant's payments from this wallet for another 30 days, until \w{3} \d{1,2}, \d{4}, up to \$100\.00 every 24 hours\./,
    )
    await user.click(within(dialog).getByRole('button', { name: 'Renew' }))

    expect(await screen.findByText(/^iPhone renewed until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()
    expect(server.prepared).toEqual([
      {
        path: '/api/wallet/session/prepare',
        body: { chain_id: SEPOLIA, action: 'add', key_id: PASSKEY_ID, ttl_secs: 30 * 86_400 },
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
      /iPhone will be able to approve the assistant's payments from this wallet for 30 days, until \w{3} \d{1,2}, \d{4}, up to \$100\.00 every 24 hours\./,
    )
    await user.click(within(dialog).getByRole('button', { name: 'Add device' }))
    expect(await screen.findByText(/^iPhone can approve payments on Sepolia until \w{3} \d{1,2}, \d{4}\.$/)).toBeInTheDocument()

    expect(server.prepared.map(p => p.body)).toEqual([
      { chain_id: SEPOLIA, action: 'remove', key_id: PASSKEY_ID },
      { chain_id: SEPOLIA, action: 'add', key_id: PASSKEY_ID, ttl_secs: 30 * 86_400 },
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
      /Your browser will first ask you to create a passkey\. This device \(“Work laptop”\) will be able to approve the assistant's payments from this wallet for 30 days/,
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
      { chain_id: SEPOLIA, action: 'add', key_id: NEW_PASSKEY.key_id, ttl_secs: 30 * 86_400 },
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
      { chain_id: SEPOLIA, action: 'add', key_id: NEW_PASSKEY.key_id, ttl_secs: 30 * 86_400 },
      { chain_id: BSC, action: 'add', key_id: NEW_PASSKEY.key_id, ttl_secs: 30 * 86_400 },
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
      { chain_id: SEPOLIA, action: 'add', key_id: PASSKEY_ID, ttl_secs: 30 * 86_400 },
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
    expect(within(counted).getByText('Always counts')).toBeInTheDocument()
    expect(within(counted).getByText('USDC')).toBeInTheDocument()

    // This wallet predates WETH always counting, so WETH has its own one-click fix.
    expect(await within(counted).findByText('Not counted yet')).toBeInTheDocument()
    await user.click(within(counted).getByRole('button', { name: 'Count WETH toward the limit' }))
    expect(await screen.findByText('WETH now counts toward your limit.')).toBeInTheDocument()

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
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'weth', action: 'add' } },
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'link', action: 'add' } },
      { path: '/api/wallet/watched-tokens/prepare', body: { chain_id: SEPOLIA, token: 'usdc', action: 'remove' } },
    ])
  })

  it('shows WETH as always counting, with no way to stop it', async () => {
    stubServer({
      wallets: [
        makeWalletState({
          spending: {
            ...makeWalletState().spending,
            watched_tokens: [
              { ticker: 'usdc', address: USDC },
              { ticker: 'weth', address: WETH },
            ],
          },
        }),
      ],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/controls')

    await connect(user)
    const counted = screen.getByRole('list', { name: 'Counted tokens' })
    const wethRow = (await within(counted).findByText('WETH')).closest('li')!
    expect(wethRow).toHaveTextContent('Always counts')
    expect(within(counted).getAllByText('Always counts')).toHaveLength(2)
    expect(within(counted).queryByRole('button', { name: 'Stop counting WETH' })).toBeNull()
    expect(within(counted).getByRole('button', { name: 'Stop counting USDC' })).toBeInTheDocument()
  })

  it('says when every listed token already counts, and leaves an unlisted one alone', async () => {
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

    expect(await screen.findByText('Every token Mitfah lists on this network already counts.')).toBeInTheDocument()
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
