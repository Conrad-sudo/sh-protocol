import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../api/client'
import { cancelSign, finishSign, startSign } from '../api/sign'
import { telegramBotUrl } from '../api/telegram'
import type { PasskeyAssertionJSON, SignCost, SignStart } from '../api/types'
import { useNow } from '../hooks/useNow'
import { explainError } from '../lib/errors'
import { isPasskeyError, signWithPasskey } from '../lib/passkey'
import { readChoice, resolveTheme, systemPrefersDark } from '../theme/themeStore'
import { browserNav, browserUrl, phone, telegramAppUrl, telegramBrowser, whenLeft, type Phone } from './browser'

/*
 * The page a Telegram quote's "Confirm with passkey" button opens: /c/<token>. Telegram may not sign
 * anything but TON inside its own apps, so the approval happens here, on mitfah.com, with the passkey
 * on this device. There is no sign-in: the link's token (single use, as long as the quote) says whose
 * payment it is, and the server sends it only with a passkey that wallet trusts.
 *
 * It starts the confirmation as the page loads and opens the passkey prompt at once: iOS Safari
 * allows one prompt per page load without a tap. Refused for want of a tap, the page shows one big
 * Confirm button, whose tap opens the prompt again.
 *
 * Telegram's own browser can't use passkeys at all, so there the page asks nothing: it reopens itself
 * in the phone's own browser (Safari on an iPhone) and closes Telegram's behind it (./browser). Once
 * the payment is sent or turned down on an iPhone, the page goes back to the chat in Telegram by
 * itself; Android's Back button does that there.
 */

/** A confirmation left waiting for the tap is started again this often, so its fees stay fresh. */
const REFRESH_MS = 50_000

/** How long "Sent" stays in view before the page goes back to Telegram. */
const RETURN_MS = 1_500

interface Note {
  tone: 'info' | 'error'
  text: string
}

type View =
  | { kind: 'loading' }
  /** In Telegram's browser: leaving for the phone's own. `stuck` once that hasn't opened. */
  | { kind: 'leaving'; on: Phone; stuck: boolean }
  /** The payment is shown; `step` is where its approval is. */
  | { kind: 'ready'; start: SignStart; step: 'prompting' | 'tap' | 'sending'; note?: Note }
  | { kind: 'sent'; start: SignStart; outcome: string }
  | { kind: 'declined'; start: SignStart; outcome: string }
  | { kind: 'expired'; start?: SignStart }
  | { kind: 'no-passkey' }
  | { kind: 'failed'; text: string; start?: SignStart; retry: boolean }

/**
 * The first start for a token, shared: React's development double mount runs the page's effect
 * twice, and a second start would stop the first one's challenge working.
 */
const firstStarts = new Map<string, Promise<SignStart>>()

function startOnce(token: string) {
  let pending = firstStarts.get(token)
  if (!pending) {
    pending = startSign({ token }).finally(() => firstStarts.delete(token))
    firstStarts.set(token, pending)
  }
  return pending
}

function startFailed(error: unknown): View {
  if (error instanceof ApiError && error.status === 404) return { kind: 'expired' }
  if (error instanceof ApiError && error.status === 409) return { kind: 'no-passkey' }
  return {
    kind: 'failed',
    text: error instanceof ApiError ? explainError(error.message) : 'Something went wrong. Please try again.',
    retry: true,
  }
}

function finishFailed(error: unknown, start: SignStart): View {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 404:
        return { kind: 'expired', start }
      case 400:
        return {
          kind: 'ready',
          start,
          step: 'tap',
          note: {
            tone: 'error',
            text: "Your passkey's approval wasn't accepted, so nothing was sent. Try again with a device you added in Mitfah.",
          },
        }
      case 409:
        return { kind: 'failed', text: 'This confirmation was replaced or has run out, so nothing was sent.', start, retry: true }
      case 422:
        return {
          kind: 'failed',
          text: `Nothing was sent: it would fail on the network. ${explainError(error.message)}`,
          start,
          retry: false,
        }
    }
    if (error.status > 0 && error.status < 500) {
      return { kind: 'failed', text: explainError(error.message), start, retry: false }
    }
  }
  // It may have reached the server and gone out, so this page never offers to send it again.
  return {
    kind: 'failed',
    text: 'No answer arrived, so it may still have been sent. Check the chat in Telegram before you try again.',
    start,
    retry: false,
  }
}

/** Why the prompt gave no signature, for the line under the Confirm button. */
function passkeyNote(error: unknown): Note {
  if (isPasskeyError(error, 'cancelled') || isPasskeyError(error, 'no-passkey')) return { tone: 'info', text: error.message }
  return { tone: 'error', text: isPasskeyError(error) ? error.message : "Your passkey didn't answer. Try again." }
}

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })

/** The fees as one figure, as the quote card writes them. */
function costText(cost: SignCost): string | null {
  if (cost.fees) return cost.fees
  if (typeof cost.total_usd === 'number') return cost.total_usd < 0.01 ? 'under $0.01' : usd.format(cost.total_usd)
  if (typeof cost.total_native === 'number' && cost.native_asset) {
    return `${cost.total_native.toFixed(8).replace(/\.?0+$/, '')} ${cost.native_asset}`
  }
  return null
}

export function ConfirmPage({ token }: { token: string | null }) {
  const [inTelegram] = useState(telegramBrowser)
  const [view, setView] = useState<View>(() => {
    if (!token) return { kind: 'expired' }
    return inTelegram ? { kind: 'leaving', on: inTelegram, stuck: false } : { kind: 'loading' }
  })
  // Bumped by each attempt (the first load, a tap, Try again), so a late answer to an older one
  // changes nothing.
  const attempt = useRef(0)
  const now = useNow(5_000)

  /** Sends the payment with the passkey's signature, if `mine` is still the latest attempt. */
  const send = (start: SignStart, credential: PasskeyAssertionJSON, mine: number) => {
    if (attempt.current !== mine) return
    setView({ kind: 'ready', start, step: 'sending' })
    void finishSign({ sign_id: start.sign_id, credential, token: token ?? undefined }).then(
      sent => {
        if (attempt.current === mine) setView({ kind: 'sent', start, outcome: sent.outcome })
      },
      (error: unknown) => {
        if (attempt.current === mine) setView(finishFailed(error, start))
      },
    )
  }

  /**
   * The first load: the payment, then the passkey prompt with no tap. `live` turns false once the
   * page that asked is gone (React's development double mount unmounts it once).
   */
  const load = useEffectEvent((live: () => boolean) => {
    if (!token || inTelegram) return
    const mine = ++attempt.current
    void startOnce(token).then(
      start => {
        if (!live()) return
        setView({ kind: 'ready', start, step: 'prompting' })
        return signWithPasskey(start).then(
          credential => {
            if (live()) send(start, credential, mine)
          },
          (error: unknown) => {
            if (!live() || attempt.current !== mine) return
            // A refusal here is most often the browser wanting a tap first: offer the button.
            setView(
              isPasskeyError(error, 'unsupported')
                ? { kind: 'failed', text: error.message, start, retry: false }
                : { kind: 'ready', start, step: 'tap' },
            )
          },
        )
      },
      (error: unknown) => {
        if (live()) setView(startFailed(error))
      },
    )
  })
  useEffect(() => {
    let live = true
    load(() => live)
    return () => {
      live = false
    }
  }, [token])

  // In Telegram's browser: off to the phone's own, once (React's development double mount runs this
  // twice), and Telegram's browser closes once the other is in front.
  const hopped = useRef(false)
  useEffect(() => {
    if (!inTelegram) return
    if (!hopped.current) {
      hopped.current = true
      browserNav.go(browserUrl(window.location.href, inTelegram))
    }
    return whenLeft(
      () => browserNav.close(),
      () => setView(current => (current.kind === 'leaving' ? { ...current, stuck: true } : current)),
    )
  }, [inTelegram])

  // Done on a phone: this tab closes once Telegram is in front, and an iPhone goes back to it by
  // itself (iOS asks first). Chrome won't open an app from a timer, so Android goes back with Back.
  const done = view.kind === 'sent' || view.kind === 'declined'
  const bot = done ? view.start.telegram_bot : null
  useEffect(() => {
    const on = phone()
    if (!done || !on) return
    const timer =
      on === 'ios' && bot && BOT.test(bot) ? setTimeout(() => browserNav.go(telegramAppUrl(bot)), RETURN_MS) : undefined
    const stop = whenLeft(() => browserNav.close())
    return () => {
      clearTimeout(timer)
      stop()
    }
  }, [done, bot])

  // While it waits for the tap, the confirmation is built again now and then (fresh fees).
  const waiting = view.kind === 'ready' && view.step === 'tap'
  useEffect(() => {
    if (!waiting || !token) return
    const id = setInterval(() => {
      void startSign({ token }).then(
        start => setView(current => (current.kind === 'ready' && current.step === 'tap' ? { ...current, start } : current)),
        (error: unknown) => {
          if (error instanceof ApiError && error.status === 404) {
            setView(current => (current.kind === 'ready' && current.step === 'tap' ? { kind: 'expired', start: current.start } : current))
          }
        },
      )
    }, REFRESH_MS)
    return () => clearInterval(id)
  }, [waiting, token])

  /** The tap. The prompt opens inside it, with nothing awaited first: Safari's rule for passkeys. */
  const confirm = () => {
    if (view.kind !== 'ready' || view.step !== 'tap') return
    const { start } = view
    const mine = ++attempt.current
    setView({ kind: 'ready', start, step: 'prompting' })
    void signWithPasskey(start).then(
      credential => send(start, credential, mine),
      (error: unknown) => {
        if (attempt.current !== mine) return
        setView(
          isPasskeyError(error, 'unsupported')
            ? { kind: 'failed', text: error.message, start, retry: false }
            : { kind: 'ready', start, step: 'tap', note: passkeyNote(error) },
        )
      },
    )
  }

  /** Try again: a fresh confirmation, then the button (the tap opens the prompt). */
  const restart = () => {
    if (!token) return
    const mine = ++attempt.current
    setView({ kind: 'loading' })
    void startSign({ token }).then(
      start => {
        if (attempt.current === mine) setView({ kind: 'ready', start, step: 'tap' })
      },
      (error: unknown) => {
        if (attempt.current === mine) setView(startFailed(error))
      },
    )
  }

  /** "Don't send it": turns the quote down, as Telegram's Cancel does. */
  const decline = () => {
    if (!token || view.kind !== 'ready') return
    const { start } = view
    const mine = ++attempt.current
    setView({ kind: 'ready', start, step: 'sending' })
    void cancelSign({ token }).then(
      ({ outcome }) => {
        if (attempt.current === mine) setView({ kind: 'declined', start, outcome })
      },
      (error: unknown) => {
        if (attempt.current !== mine) return
        if (error instanceof ApiError && error.status === 404) return setView({ kind: 'expired', start })
        setView({ kind: 'ready', start, step: 'tap', note: { tone: 'error', text: "Couldn't cancel it. Try again." } })
      },
    )
  }

  const dark = resolveTheme(readChoice(), systemPrefersDark())
  return (
    <main className="mfc-page">
      <header className="mfc-brand">
        <img src={dark ? '/brand/mark-dark.png' : '/brand/mark-light.png'} alt="" width={26} height={29} />
        <span>Mitfah</span>
      </header>
      <section className="mfc-card">
        <Body view={view} now={now} onConfirm={confirm} onRetry={restart} onDecline={decline} />
      </section>
    </main>
  )
}

function Body({
  view,
  now,
  onConfirm,
  onRetry,
  onDecline,
}: {
  view: View
  now: number
  onConfirm: () => void
  onRetry: () => void
  onDecline: () => void
}) {
  switch (view.kind) {
    case 'loading':
      return (
        <p className="mfc-status" role="status">
          Loading the payment…
        </p>
      )
    case 'leaving': {
      const ios = view.on === 'ios'
      if (!view.stuck) {
        return (
          <p className="mfc-status" role="status">
            {ios ? 'Opening Safari…' : 'Opening your browser…'}
          </p>
        )
      }
      return (
        <>
          <h1>{ios ? 'Open this in Safari' : 'Open this in your browser'}</h1>
          <p>
            {ios
              ? "Telegram's browser can't use passkeys. Open the menu at the top, choose Open in Safari, and approve there with Face ID."
              : "Telegram's browser can't use passkeys. Open the ⋮ menu, choose Open in Browser, and approve there with your fingerprint or screen lock."}
          </p>
          <a className="mfc-button" href={browserUrl(window.location.href, view.on)}>
            {ios ? 'Open in Safari' : 'Open in browser'}
          </a>
        </>
      )
    }
    case 'expired':
      return (
        <>
          <h1>This confirmation has expired.</h1>
          <p>Ask again in Telegram.</p>
          <BackToTelegram bot={view.start?.telegram_bot} />
        </>
      )
    case 'no-passkey':
      return (
        <>
          <h1>No passkey here can approve this</h1>
          <p>
            No passkey on this device is approved for this wallet. Open this link on a device you added, or add this one
            in Mitfah under Controls.
          </p>
          <a className="mfc-button mfc-secondary" href="/controls">
            Open Mitfah
          </a>
        </>
      )
    case 'sent':
      return (
        <>
          <h1>
            Sent <span aria-hidden="true">✓</span>
          </h1>
          <p className="mfc-outcome">{view.outcome}</p>
          <BackToTelegram bot={view.start.telegram_bot} />
        </>
      )
    case 'declined':
      return (
        <>
          <h1>Not sent</h1>
          <p className="mfc-outcome">{view.outcome}</p>
          <BackToTelegram bot={view.start.telegram_bot} />
        </>
      )
    case 'failed':
      return (
        <>
          {view.start ? <Summary start={view.start} now={now} /> : <h1>Confirm a payment</h1>}
          <p className="mfc-note" data-tone="error" role="alert">
            {view.text}
          </p>
          {view.retry ? (
            <button type="button" className="mfc-button" onClick={onRetry}>
              Try again
            </button>
          ) : (
            <BackToTelegram bot={view.start?.telegram_bot} />
          )}
        </>
      )
    case 'ready': {
      const secondsLeft = view.start.expires_at - now / 1000
      if (secondsLeft <= 0 && view.step === 'tap') {
        return (
          <>
            <h1>This confirmation has expired.</h1>
            <p>Ask again in Telegram.</p>
            <BackToTelegram bot={view.start.telegram_bot} />
          </>
        )
      }
      let action: ReactNode
      if (view.step === 'tap') {
        action = (
          <button type="button" className="mfc-button mfc-confirm" onClick={onConfirm}>
            Confirm
          </button>
        )
      } else {
        action = (
          <p className="mfc-status" role="status">
            {view.step === 'prompting' ? 'Approve with your passkey…' : 'Sending…'}
          </p>
        )
      }
      return (
        <>
          <Summary start={view.start} now={now} />
          {action}
          {view.note && (
            <p className="mfc-note" data-tone={view.note.tone} role={view.note.tone === 'error' ? 'alert' : 'status'}>
              {view.note.text}
            </p>
          )}
          {view.step === 'tap' && (
            <button type="button" className="mfc-link" onClick={onDecline}>
              Don't send it
            </button>
          )}
        </>
      )
    }
  }
}

/** What the payment does, where, what it costs, and for how long it can be approved. */
function Summary({ start, now }: { start: SignStart; now: number }) {
  const { summary } = start
  const fees = costText(summary.cost)
  const minutes = Math.max(1, Math.ceil((start.expires_at - now / 1000) / 60))
  return (
    <>
      <h1>Confirm on {summary.network}?</h1>
      <p className="mfc-action">{summary.action}</p>
      <p className="mfc-meta">
        {fees && `Fees: ${fees} · `}good for {minutes} minute{minutes === 1 ? '' : 's'}
      </p>
    </>
  )
}

/** Telegram usernames: 5-32 letters, digits and underscores. */
const BOT = /^[A-Za-z0-9_]{5,32}$/

function BackToTelegram({ bot }: { bot?: string | null }) {
  if (!bot || !BOT.test(bot)) return <p className="mfc-meta">You can close this page and go back to Telegram.</p>
  return (
    <a className="mfc-button mfc-secondary" href={telegramBotUrl(bot)}>
      Back to Telegram
    </a>
  )
}
