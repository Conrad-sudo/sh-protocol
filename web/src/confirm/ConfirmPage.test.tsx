import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { SignStart } from '../api/types'
import { PASSKEY_CREDENTIAL, SEPOLIA } from '../test/fixtures'
import { json } from '../test/utils'
import { b64url, installPasskeys, refused, signedCredential } from '../test/webauthn'
import { browserNav } from './browser'
import { ConfirmPage } from './ConfirmPage'
import { tokenFromPath } from './token'

/** A Telegram card's link token, as the server mints them (token_urlsafe(24)). */
const TOKEN = 'Lk3v_Qp9-Zt2Xw8Yb4Rn6Sm1Hc5Jd7Fa'
const EXPIRED = 'This confirmation has expired. Ask again.'
const SENT = 'Sending — Transfer 5 USDC to sam. Waiting for the network to confirm it.'
const IPHONE =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/23A341 Safari/604.1'
const ANDROID =
  'Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0.0.0 Mobile Safari/537.36'

/** The page on a phone: its user agent, and what Telegram's browser puts on the window, if it is that. */
function onPhone(ua: string, telegram?: 'TelegramWebviewProxy' | 'TelegramWebview') {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(ua)
  if (telegram) vi.stubGlobal(telegram, {})
  return {
    go: vi.spyOn(browserNav, 'go').mockImplementation(() => {}),
    close: vi.spyOn(browserNav, 'close').mockImplementation(() => {}),
  }
}

/** The app sent to the background: another app is in front. */
function putAway() {
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

/** POST /api/sign/start for the link's quote; each start gets a new sign_id, as the server's do. */
function signStart(n: number): SignStart {
  return {
    sign_id: `sign-${n}`,
    challenge: b64url('the user operation hash, 32 b!!!'),
    rp_id: 'localhost',
    allow_credentials: [{ type: 'public-key', id: PASSKEY_CREDENTIAL }],
    user_verification: 'required',
    timeout_ms: 60_000,
    expires_at: Math.floor(Date.now() / 1000) + 300,
    summary: { action: 'Transfer 5 USDC to sam', network: 'Sepolia', chain_id: SEPOLIA, cost: { fees: '$0.35' } },
    telegram_bot: 'MitfahBot',
  }
}

type Answer = { status: number; detail: string } | 'offline'

interface ServerOptions {
  /** What POST /api/sign/start refuses with, instead of the confirmation. */
  start?: Answer
  /** What POST /api/sign/finish refuses with, instead of sending. */
  finish?: Answer
}

/** The API as the page meets it: no sign-in, only the link's token. */
function stubServer({ start, finish }: ServerOptions = {}) {
  const requests: { url: string; body: Record<string, unknown>; headers: Record<string, string> }[] = []
  const refuse = (answer: Answer) =>
    answer === 'offline' ? Promise.reject(new TypeError('Failed to fetch')) : Promise.resolve(json(answer.status, { detail: answer.detail }))
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {}
      requests.push({ url, body, headers: (init?.headers ?? {}) as Record<string, string> })
      const starts = requests.filter(r => r.url === '/api/sign/start').length
      switch (url) {
        case '/api/sign/start':
          return start ? refuse(start) : Promise.resolve(json(200, signStart(starts)))
        case '/api/sign/finish':
          return finish ? refuse(finish) : Promise.resolve(json(200, { status: 'sent', outcome: SENT, transaction_id: 77 }))
        case '/api/sign/cancel':
          return Promise.resolve(json(200, { outcome: 'Cancelled. Nothing was sent.' }))
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { requests, sent: (path: string) => requests.filter(r => r.url === path) }
}

describe('tokenFromPath', () => {
  it('takes only a token-shaped last part of /c/<token>', () => {
    expect(tokenFromPath(`/c/${TOKEN}`)).toBe(TOKEN)
    expect(tokenFromPath(`/c/${TOKEN}/`)).toBe(TOKEN)
    expect(tokenFromPath('/c/short')).toBeNull()
    expect(tokenFromPath('/c/has%20space-and-more-characters')).toBeNull()
    expect(tokenFromPath(`/c/${TOKEN}/extra`)).toBeNull()
    expect(tokenFromPath(`/x/${TOKEN}`)).toBeNull()
  })
})

describe('ConfirmPage', () => {
  beforeEach(() => {
    resetClientForTests()
    localStorage.clear()
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.useRealTimers()
    window.history.replaceState(null, '', '/')
  })

  it('shows the payment and opens the passkey prompt at once, then sends it', async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const server = stubServer()
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('heading', { name: /Sent/ })).toBeInTheDocument()
    expect(screen.getByText(SENT)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to Telegram' })).toHaveAttribute('href', 'https://t.me/MitfahBot')
    // No tap was needed: the prompt opened as the page loaded.
    expect(browser.get).toHaveBeenCalledTimes(1)
    expect(new TextDecoder().decode(browser.get.mock.calls[0][0]!.publicKey!.challenge as ArrayBuffer)).toBe(
      'the user operation hash, 32 b!!!',
    )

    // The token is what says whose payment it is: no sign-in, no session cookie refresh.
    expect(server.requests.map(r => r.url)).toEqual(['/api/sign/start', '/api/sign/finish'])
    expect(server.requests.every(r => !('Authorization' in r.headers))).toBe(true)
    expect(server.sent('/api/sign/start')[0].body).toEqual({ token: TOKEN })
    expect(server.sent('/api/sign/finish')[0].body).toEqual({
      sign_id: 'sign-1',
      credential: expect.objectContaining({ id: PASSKEY_CREDENTIAL, type: 'public-key' }),
      token: TOKEN,
    })
  })

  it('offers one big Confirm when the browser wants a tap first, and opens the prompt from it', async () => {
    const browser = installPasskeys()
    // iOS refuses a prompt that no tap asked for.
    browser.get.mockRejectedValueOnce(refused()).mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const server = stubServer()
    const user = userEvent.setup()
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('heading', { name: 'Confirm on Sepolia?' })).toBeInTheDocument()
    expect(screen.getByText('Transfer 5 USDC to sam')).toBeInTheDocument()
    expect(screen.getByText('Fees: $0.35 · good for 5 minutes')).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: 'Confirm' }))
    expect(browser.get).toHaveBeenCalledTimes(2)
    expect(await screen.findByRole('heading', { name: /Sent/ })).toBeInTheDocument()
    expect(server.sent('/api/sign/finish')).toHaveLength(1)
  })

  it('keeps the Confirm button when the prompt is closed, and says nothing was sent', async () => {
    const browser = installPasskeys()
    browser.get.mockRejectedValue(refused())
    const server = stubServer()
    const user = userEvent.setup()
    render(<ConfirmPage token={TOKEN} />)

    await user.click(await screen.findByRole('button', { name: 'Confirm' }))
    // This browser has never used one of the account's passkeys, so it may hold none.
    expect(await screen.findByText(/^Nothing was sent\. If this device has no approved passkey/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(server.sent('/api/sign/finish')).toEqual([])
  })

  it('says when the link has expired or was used', async () => {
    installPasskeys()
    stubServer({ start: { status: 404, detail: EXPIRED } })
    render(<ConfirmPage token={TOKEN} />)
    expect(await screen.findByRole('heading', { name: 'This confirmation has expired.' })).toBeInTheDocument()
    expect(screen.getByText('Ask again in Telegram.')).toBeInTheDocument()
    cleanup()

    // A link with no token worth sending is answered the same way, without asking the server.
    const server = stubServer()
    render(<ConfirmPage token={null} />)
    expect(screen.getByRole('heading', { name: 'This confirmation has expired.' })).toBeInTheDocument()
    expect(server.requests).toEqual([])
  })

  it('says when no passkey this account has is approved on the wallet', async () => {
    const browser = installPasskeys()
    stubServer({ start: { status: 409, detail: 'No passkey on this device is approved for this wallet.' } })
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('heading', { name: 'No passkey here can approve this' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Mitfah' })).toHaveAttribute('href', '/controls')
    expect(browser.get).not.toHaveBeenCalled()
  })

  it("lets the user try again when the passkey's approval isn't accepted", async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    stubServer({ finish: { status: 400, detail: 'That passkey is not approved for this wallet.' } })
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Your passkey's approval wasn't accepted, so nothing was sent\./)
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument()
  })

  it('says why a payment that would fail was not sent, with nothing to retry', async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    stubServer({ finish: { status: 422, detail: 'not enough USDC' } })
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Nothing was sent: it would fail on the network\./)
    expect(screen.queryByRole('button', { name: /Confirm|Try again/ })).toBeNull()
    expect(screen.getByRole('link', { name: 'Back to Telegram' })).toBeInTheDocument()
  })

  it('never offers to send again when no answer arrived: it may have gone out', async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    stubServer({ finish: 'offline' })
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'No answer arrived, so it may still have been sent. Check the chat in Telegram before you try again.',
    )
    expect(screen.queryByRole('button', { name: /Confirm|Try again/ })).toBeNull()
  })

  it('starts again with a fresh confirmation when the last one was replaced', async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const server = stubServer({ finish: { status: 409, detail: EXPIRED } })
    const user = userEvent.setup()
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('This confirmation was replaced or has run out, so nothing was sent.')
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(server.sent('/api/sign/start')).toHaveLength(2)
  })

  it('turns the payment down, as Telegram’s Cancel does', async () => {
    const browser = installPasskeys()
    browser.get.mockRejectedValue(refused())
    const server = stubServer()
    const user = userEvent.setup()
    render(<ConfirmPage token={TOKEN} />)

    await user.click(await screen.findByRole('button', { name: "Don't send it" }))
    expect(await screen.findByRole('heading', { name: 'Not sent' })).toBeInTheDocument()
    expect(screen.getByText('Cancelled. Nothing was sent.')).toBeInTheDocument()
    expect(server.sent('/api/sign/cancel')[0].body).toEqual({ token: TOKEN })
    expect(server.sent('/api/sign/finish')).toEqual([])
  })

  it("leaves Telegram's browser on an iPhone for Safari, asking nothing, and closes it behind itself", async () => {
    const browser = installPasskeys()
    const nav = onPhone(IPHONE, 'TelegramWebviewProxy')
    const server = stubServer()
    render(<ConfirmPage token={TOKEN} />)

    expect(screen.getByRole('status')).toHaveTextContent('Opening Safari…')
    expect(nav.go).toHaveBeenCalledTimes(1)
    expect(nav.go).toHaveBeenCalledWith(`x-safari-${window.location.href}`)
    // Safari makes its own start: one made here would only be thrown away.
    expect(server.requests).toEqual([])
    expect(browser.get).not.toHaveBeenCalled()

    putAway()
    expect(nav.close).toHaveBeenCalledTimes(1)
  })

  it("says how to open Safari when Telegram's browser didn't hand the page over", async () => {
    vi.useFakeTimers()
    const nav = onPhone(IPHONE, 'TelegramWebviewProxy')
    render(<ConfirmPage token={TOKEN} />)

    act(() => {
      vi.advanceTimersByTime(3_000)
    })
    expect(screen.getByRole('heading', { name: 'Open this in Safari' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open in Safari' })).toHaveAttribute('href', `x-safari-${window.location.href}`)
    expect(nav.close).not.toHaveBeenCalled()
  })

  it('goes back to Telegram by itself once it is sent on a phone, and closes the tab', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const nav = onPhone(IPHONE)
    stubServer()
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('heading', { name: /Sent/ })).toBeInTheDocument()
    expect(nav.go).not.toHaveBeenCalled()
    await act(() => vi.advanceTimersByTimeAsync(1_500))
    expect(nav.go).toHaveBeenCalledWith('tg://resolve?domain=MitfahBot')

    putAway()
    expect(nav.close).toHaveBeenCalledTimes(1)
  })

  it('stays on the page once it is sent on a computer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const go = vi.spyOn(browserNav, 'go').mockImplementation(() => {})
    stubServer()
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('heading', { name: /Sent/ })).toBeInTheDocument()
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    expect(go).not.toHaveBeenCalled()
  })

  it("leaves Telegram's browser on Android for the default browser, and says how when it can't", async () => {
    vi.useFakeTimers()
    window.history.replaceState(null, '', `/c/${TOKEN}`)
    const nav = onPhone(ANDROID, 'TelegramWebviewProxy')
    const server = stubServer()
    render(<ConfirmPage token={TOKEN} />)

    expect(screen.getByRole('status')).toHaveTextContent('Opening your browser…')
    // An intent naming no app: Android opens the address in the default browser.
    const intent = `intent://localhost:3000/c/${TOKEN}#Intent;scheme=http;end`
    expect(nav.go).toHaveBeenCalledExactlyOnceWith(intent)
    expect(server.requests).toEqual([])

    act(() => {
      vi.advanceTimersByTime(3_000)
    })
    expect(screen.getByRole('heading', { name: 'Open this in your browser' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open in browser' })).toHaveAttribute('href', intent)
  })

  it('stays put once it is sent on Android, where Back goes back to Telegram, and closes the tab', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(PASSKEY_CREDENTIAL))
    const nav = onPhone(ANDROID)
    stubServer()
    render(<ConfirmPage token={TOKEN} />)

    expect(await screen.findByRole('heading', { name: /Sent/ })).toBeInTheDocument()
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    // Chrome won't open an app from a timer: the link is there for a tap.
    expect(nav.go).not.toHaveBeenCalled()
    expect(screen.getByRole('link', { name: 'Back to Telegram' })).toHaveAttribute('href', 'https://t.me/MitfahBot')

    putAway()
    expect(nav.close).toHaveBeenCalledTimes(1)
  })

  it("says so on a browser that can't use passkeys", async () => {
    stubServer()
    render(<ConfirmPage token={TOKEN} />)
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/can't use passkeys/))
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull()
  })
})
