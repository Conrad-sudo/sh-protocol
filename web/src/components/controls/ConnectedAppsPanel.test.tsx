import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../../api/client'
import type { ConnectedApp, CreatedToken, PasskeyPrompt } from '../../api/types'
import { json, makeWagmiConfig, Providers, TOKEN } from '../../test/utils'
import { b64url, installPasskeys, refused, signedCredential } from '../../test/webauthn'
import { ConnectedAppsPanel } from './ConnectedAppsPanel'

const CLAUDE: ConnectedApp = {
  id: 3,
  kind: 'app',
  app: { key: 'claude', name: 'Claude', site: 'claude.ai' },
  access: 'prepare',
  created_at: 1_790_000_000,
  last_used_at: 1_790_100_000,
  expires_at: null,
}
const CHATGPT: ConnectedApp = {
  id: 4,
  kind: 'app',
  app: { key: 'chatgpt', name: 'ChatGPT', site: 'chatgpt.com' },
  access: 'prepare',
  created_at: 1_790_200_000,
  last_used_at: null,
  expires_at: null,
}
const BOT: ConnectedApp = {
  id: 7,
  kind: 'token',
  app: { key: 'personal', name: 'Trading bot', site: '' },
  access: 'read',
  created_at: 1_790_300_000,
  last_used_at: null,
  expires_at: 1_792_900_000,
}

const CREDENTIAL = b64url('phone passkey')
const OPTIONS: PasskeyPrompt = {
  challenge: b64url('a challenge of thirty-two bytes!'),
  rp_id: 'localhost',
  allow_credentials: [{ type: 'public-key', id: CREDENTIAL }],
  user_verification: 'required',
  timeout_ms: 60_000,
}
const MADE: CreatedToken = {
  id: 9,
  token: 'mfp_secret-token-value',
  name: 'Script',
  access: 'prepare',
  expires_at: 1_792_900_000,
}

interface Stub {
  deleted: number[]
  /** Bodies posted to /api/tokens. */
  made: Record<string, unknown>[]
  /** How many times the options were fetched. */
  optionsFetched: () => number
}

/**
 * Signed in; /api/connections lists `apps` until one is deleted. /api/tokens/options answers
 * `options` (an answer of its own when given as [status, body]); /api/tokens makes MADE.
 */
function stubApi(apps: ConnectedApp[], options: PasskeyPrompt | [number, unknown] = OPTIONS): Stub {
  let listed = [...apps]
  const deleted: number[] = []
  const made: Record<string, unknown>[] = []
  let optionsFetched = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith('/api/auth/refresh')) return Promise.resolve(json(200, TOKEN))
      if (url.endsWith('/api/connections')) return Promise.resolve(json(200, { connections: listed }))
      if (url.endsWith('/api/tokens/options')) {
        optionsFetched += 1
        return Promise.resolve(Array.isArray(options) ? json(options[0], options[1]) : json(200, options))
      }
      if (url.endsWith('/api/tokens') && init?.method === 'POST') {
        made.push(JSON.parse(String(init.body)))
        listed = [{ ...BOT, id: MADE.id, app: { key: 'personal', name: MADE.name, site: '' }, access: MADE.access }, ...listed]
        return Promise.resolve(json(200, MADE))
      }
      const gone = /\/api\/connections\/(\d+)$/.exec(url)
      if (gone && init?.method === 'DELETE') {
        deleted.push(Number(gone[1]))
        listed = listed.filter(app => app.id !== Number(gone[1]))
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { deleted, made, optionsFetched: () => optionsFetched }
}

function renderPanel() {
  render(
    <Providers wagmiConfig={makeWagmiConfig()}>
      <ConnectedAppsPanel />
    </Providers>,
  )
}

/** Opens the form and waits until its passkey options are in. */
async function openForm(user: ReturnType<typeof userEvent.setup>, stub: Stub) {
  await user.click(await screen.findByRole('button', { name: 'Create a token' }))
  await waitFor(() => expect(stub.optionsFetched()).toBeGreaterThan(0))
}

describe('ConnectedAppsPanel', () => {
  beforeEach(() => resetClientForTests())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('lists the connected apps, and says where to connect one', async () => {
    stubApi([CHATGPT, CLAUDE])
    renderPanel()

    expect(await screen.findByRole('heading', { name: 'Claude' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'ChatGPT' })).toBeInTheDocument()
    expect(screen.getByText(/chatgpt\.com · Connected .* · not used yet/)).toBeInTheDocument()
    expect(screen.getByText(`${window.location.origin}/mcp`)).toBeInTheDocument()
  })

  it('disconnects an app, and it leaves the list', async () => {
    const { deleted } = stubApi([CHATGPT, CLAUDE])
    renderPanel()
    await screen.findByRole('heading', { name: 'Claude' })

    await userEvent.setup().click(screen.getByRole('button', { name: 'Disconnect Claude' }))

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Claude' })).toBeNull())
    expect(deleted).toEqual([3])
    expect(screen.getByRole('heading', { name: 'ChatGPT' })).toBeInTheDocument()
  })

  it('says so when no app is connected', async () => {
    stubApi([])
    renderPanel()

    expect(await screen.findByText('No apps connected and no tokens.')).toBeInTheDocument()
  })

  it('lists a token with what it can do and when it expires, and revokes it', async () => {
    const { deleted } = stubApi([BOT, CLAUDE])
    renderPanel()

    expect(await screen.findByRole('heading', { name: 'Trading bot' })).toBeInTheDocument()
    expect(screen.getByText(/^Read only · Expires .* · not used yet$/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Revoke Trading bot' }))

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Trading bot' })).toBeNull())
    expect(deleted).toEqual([7])
  })

  it('makes a token with a passkey and shows it once', async () => {
    const authenticator = installPasskeys()
    authenticator.get.mockResolvedValue(signedCredential(CREDENTIAL))
    const stub = stubApi([CLAUDE])
    const user = userEvent.setup()
    renderPanel()
    await openForm(user, stub)

    await user.type(screen.getByLabelText('Name'), '  Script ')
    await user.click(screen.getByRole('combobox', { name: 'What it can do' }))
    await user.click(await screen.findByRole('option', { name: 'Read and prepare transactions' }))
    await user.click(screen.getByRole('button', { name: 'Create with passkey' }))

    expect(await screen.findByText(/Copy it now: Mitfah won't show it again/)).toBeInTheDocument()
    // Hidden: only the copy button has it.
    expect(screen.getByLabelText('Token, hidden')).toBeInTheDocument()
    expect(screen.queryByText(MADE.token)).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Copy token' }))
    expect(await navigator.clipboard.readText()).toBe(MADE.token)
    expect(stub.made).toHaveLength(1)
    expect(stub.made[0]).toMatchObject({ name: 'Script', days: 30, access: 'prepare' })
    expect((stub.made[0].credential as { id: string }).id).toBe(CREDENTIAL)
    const asked = authenticator.get.mock.calls[0][0]?.publicKey
    expect(asked?.userVerification).toBe('required')
    expect(asked?.allowCredentials?.length).toBe(1)
    expect(await screen.findByRole('heading', { name: 'Script' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByLabelText('Token, hidden')).toBeNull()
    expect(screen.getByRole('button', { name: 'Create a token' })).toBeInTheDocument()
  })

  it('asks for a name before opening the passkey prompt', async () => {
    const authenticator = installPasskeys()
    const stub = stubApi([])
    const user = userEvent.setup()
    renderPanel()
    await openForm(user, stub)

    await user.click(screen.getByRole('button', { name: 'Create with passkey' }))

    expect(await screen.findByText(/Give the token a name/)).toBeInTheDocument()
    expect(authenticator.get).not.toHaveBeenCalled()
    expect(stub.made).toEqual([])
  })

  it('says nothing was made when the passkey prompt is closed', async () => {
    const authenticator = installPasskeys()
    authenticator.get.mockRejectedValue(refused())
    const stub = stubApi([])
    const user = userEvent.setup()
    renderPanel()
    await openForm(user, stub)

    await user.type(screen.getByLabelText('Name'), 'Bot')
    await user.click(screen.getByRole('button', { name: 'Create with passkey' }))

    expect(await screen.findByText(/No token was made/)).toBeInTheDocument()
    expect(stub.made).toEqual([])
    // Its challenge may have been seen: fresh options are fetched for the next try.
    await waitFor(() => expect(stub.optionsFetched()).toBeGreaterThan(1))
  })

  it("says why a token can't be made, before the prompt", async () => {
    installPasskeys()
    const stub = stubApi([], [409, { detail: 'You have 10 tokens already. Revoke one before making another.' }])
    const user = userEvent.setup()
    renderPanel()
    await openForm(user, stub)

    expect(await screen.findByText(/You have 10 tokens already/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create with passkey' })).toBeDisabled()
  })
})
