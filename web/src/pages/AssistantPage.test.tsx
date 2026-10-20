import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { ChatMessage, Contact, Exchange, QuoteOffer, Transaction, WalletState } from '../api/types'
import { routes } from '../routes'
import { makeSession, makeWalletState, SEPOLIA } from '../test/fixtures'
import { json, ME, renderRoutes, setViewportWidth, TOKEN, WALLET } from '../test/utils'

const CHAINS = [{ chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: false }]
const TOKENS = [
  { ticker: 'weth', address: '0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14', always_counted: true },
  { ticker: 'usdc', address: '0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238' },
]
const SAM: Contact = { name: 'sam', address: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8' }

/** A reply, and the quote its turn offered, if any. */
type Answer = { reply: string; quote?: QuoteOffer } | { status: number; detail?: string } | 'offline'
/** What a tap on a quote card's button gets; by default the server's own wording. */
type TapAnswer = { outcome: string; transaction_id?: number | null } | { status: number; detail?: string } | 'offline'

/** The History row a confirmed send is followed in. */
const SENT_ROW = 77

const STALE = 'That quote has expired or was already used. Nothing was sent. Ask me again for a fresh one.'

function makeQuote(overrides: Partial<QuoteOffer> = {}): QuoteOffer {
  return {
    quote_id: 'q-abc_123',
    chain_id: SEPOLIA,
    action: 'Transfer 5 USDC to sam',
    fees: '$0.35',
    expires_at: Math.floor(Date.now() / 1000) + 300,
    ...overrides,
  }
}

interface ServerOptions {
  history?: ChatMessage[]
  /** GET /api/chat/history fails this many times first. */
  historyFailures?: number
  /** POST /api/chat answers, in order; the last one repeats. Default: an echo. */
  answers?: Answer[]
  /** POST /api/chat waits for this before answering. */
  gate?: Promise<void>
  /** Whether the server keeps a message it answered with an error (it had started the turn). */
  keepsFailed?: boolean
  wallet?: WalletState
  /** Every GET /api/wallet read after the first fails. */
  walletFailsLater?: boolean
  walletChains?: number[]
  contacts?: Contact[]
  /** What DELETE /api/chat/history answers: 200 clears the chat, 409 means a turn is running. */
  clearStatus?: number
  /** The network's exchange; without one the assistant can't swap. */
  exchange?: Exchange
  /** Whether the account has linked Telegram. */
  telegramLinked?: boolean
  /** The username of the server's Telegram bot; it runs none by default. */
  telegramBot?: string
  /** The quote waiting for a tap when the page loads. */
  quote?: QuoteOffer | null
  /** What taps on a quote card's buttons get, in order; the last one repeats. */
  taps?: TapAnswer[]
  /** A tap waits for this before answering. */
  tapGate?: Promise<void>
  /** What reads of a confirmed send's History row find, in order; the last one repeats. */
  landing?: Transaction['status'][]
}

/** A signed-in account with a wallet on Sepolia and a chat that behaves like app/api.py. */
function stubServer({
  history = [],
  historyFailures = 0,
  answers = [],
  gate = Promise.resolve(),
  keepsFailed = false,
  wallet = makeWalletState(),
  walletFailsLater = false,
  walletChains = [SEPOLIA],
  contacts = [SAM],
  clearStatus = 200,
  exchange,
  telegramLinked = false,
  telegramBot,
  quote = null,
  taps = [],
  tapGate = Promise.resolve(),
  landing = ['confirmed'],
}: ServerOptions = {}) {
  let thread = [...history]
  let offered = quote
  let sending = ''
  const posted: unknown[] = []
  const tapped: { url: string; body: unknown }[] = []
  const cleared: string[] = []
  const calls = { history: 0, wallet: 0, transaction: 0 }
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') {
        return Promise.resolve(
          json(200, {
            ...ME,
            owner_addr: WALLET,
            wallet_chains: walletChains,
            telegram_linked: telegramLinked,
            telegram_bot: telegramBot ?? null,
          }),
        )
      }
      if (url === '/api/chains') {
        return Promise.resolve(
          json(200, { chains: CHAINS.map(c => ({ ...c, exchange: exchange ?? null, default_spenders: [] })) }),
        )
      }
      if (url === `/api/tokens?chain_id=${SEPOLIA}`) return Promise.resolve(json(200, { tokens: TOKENS }))
      if (url === '/api/contacts') return Promise.resolve(json(200, { contacts }))
      if (url === `/api/wallet/${SEPOLIA}`) {
        calls.wallet++
        if (walletFailsLater && calls.wallet > 1) return Promise.resolve(json(502, { detail: 'RPC unavailable' }))
        return Promise.resolve(json(200, wallet))
      }
      if (url.startsWith('/api/chat/history') && method === 'DELETE') {
        cleared.push(url)
        if (clearStatus !== 200) {
          return Promise.resolve(json(clearStatus, { detail: 'The assistant is still answering a message.' }))
        }
        thread = []
        offered = null
        return Promise.resolve(json(200, { status: 'cleared', chain_ids: [SEPOLIA] }))
      }
      if (url.startsWith('/api/chat/history?')) {
        calls.history++
        if (historyFailures-- > 0) return Promise.resolve(new Response('Internal Server Error', { status: 500 }))
        return Promise.resolve(json(200, { chain_id: SEPOLIA, messages: thread }))
      }
      if (url === `/api/chat/quote?chain_id=${SEPOLIA}`) return Promise.resolve(json(200, { quote: offered }))
      if (url.startsWith('/api/chat/quote/') && method === 'POST') {
        const body = JSON.parse(String(init?.body)) as { chain_id: number; quote_id: string }
        const confirm = url.endsWith('/confirm')
        const answer = taps.length ? taps[Math.min(tapped.length, taps.length - 1)] : null
        tapped.push({ url, body })
        return tapGate.then(() => {
          if (answer === 'offline') throw new TypeError('Failed to fetch')
          if (answer && 'status' in answer) {
            if (answer.status === 409) offered = null
            return answer.detail
              ? json(answer.status, { detail: answer.detail })
              : new Response('Bad Gateway', { status: answer.status })
          }
          const outcome =
            answer?.outcome ??
            (confirm ? `Sending — ${offered?.action}. Waiting for the network to confirm it.` : 'Cancelled. Nothing was sent.')
          thread = [
            ...thread,
            { role: 'user', text: confirm ? '✅ Confirm' : '✖️ Cancel', button: true },
            { role: 'assistant', text: outcome, button: true },
          ]
          if (!confirm) {
            offered = null
            return json(200, { outcome })
          }
          sending = offered?.action ?? ''
          offered = null
          const transactionId = answer && 'transaction_id' in answer ? answer.transaction_id : SENT_ROW
          return json(200, { outcome, transaction_id: transactionId })
        })
      }
      if (url === `/api/transactions/${SENT_ROW}`) {
        const status = landing[Math.min(calls.transaction++, landing.length - 1)]
        const row: Transaction = {
          id: SENT_ROW,
          chain_id: SEPOLIA,
          source: 'assistant',
          action: sending,
          status,
          tx_hash: status === 'pending' ? null : `0x${'ab'.repeat(32)}`,
          created_at: Math.floor(Date.now() / 1000),
          mined_at: status === 'pending' ? null : Math.floor(Date.now() / 1000),
          direction: 'out',
        }
        return Promise.resolve(json(200, row))
      }
      if (url === '/api/chat' && method === 'POST') {
        const body = JSON.parse(String(init?.body)) as { chain_id: number; message: string }
        const answer = answers.length ? answers[Math.min(posted.length, answers.length - 1)] : null
        posted.push(body)
        return gate.then(() => {
          if (answer === 'offline') {
            if (keepsFailed) thread = [...thread, { role: 'user', text: body.message }]
            throw new TypeError('Failed to fetch')
          }
          if (answer && 'status' in answer) {
            if (keepsFailed) thread = [...thread, { role: 'user', text: body.message }]
            return answer.detail
              ? json(answer.status, { detail: answer.detail })
              : new Response('Bad Gateway', { status: answer.status })
          }
          const reply = answer?.reply ?? `You said: ${body.message}`
          thread = [...thread, { role: 'user', text: body.message }, { role: 'assistant', text: reply }]
          if (answer && 'quote' in answer && answer.quote) offered = answer.quote
          return json(200, { reply })
        })
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { posted, calls, cleared, tapped }
}

function quoteCard() {
  return screen.findByRole('region', { name: 'Transaction waiting for your answer' })
}

function composer() {
  return screen.findByRole('textbox', { name: 'Message' })
}

function log() {
  return screen.getByRole('log', { name: 'Conversation with Mitfah' })
}

function deferred() {
  let open!: () => void
  const promise = new Promise<void>(resolve => {
    open = resolve
  })
  return { promise, open }
}

describe('AssistantPage', () => {
  beforeEach(() => {
    resetClientForTests()
    setViewportWidth(1280)
    ;(window as { coarsePointer?: boolean }).coarsePointer = false
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the conversation, with replies as formatted text that runs nothing', async () => {
    stubServer({
      history: [
        { role: 'user', text: 'what can I spend?' },
        {
          role: 'assistant',
          text: [
            'You have **$60** left. See [the docs](https://example.com/docs).',
            '<img src=x onerror="alert(1)"> ![tracker](https://evil.example/pixel?leak=1)',
            '[click](javascript:alert(1))',
            '| Token | Amount |\n| --- | --- |\n| USDC | 25 |',
          ].join('\n\n'),
        },
      ],
    })
    await renderRoutes(routes, '/assistant')

    expect(await screen.findByText('what can I spend?')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Assistant', level: 1 })).toBeInTheDocument()
    const conversation = log()
    expect(within(conversation).getByText('You said:')).toBeInTheDocument()
    expect(within(conversation).getByText('Mitfah said:')).toBeInTheDocument()
    expect(within(conversation).getByText('$60').tagName).toBe('STRONG')
    const docs = within(conversation).getByRole('link', { name: 'the docs' })
    expect(docs).toHaveAttribute('href', 'https://example.com/docs')
    expect(docs).toHaveAttribute('target', '_blank')
    expect(docs).toHaveAttribute('rel', 'noopener noreferrer')
    // Raw HTML shows as text; images never load; a javascript: link loses its target.
    expect(conversation.querySelector('img')).toBeNull()
    expect(within(conversation).getByText('[image: tracker]')).toBeInTheDocument()
    expect(within(conversation).getByText('click').getAttribute('href') ?? '').not.toContain('javascript')
    expect(within(conversation).getByRole('table')).toBeInTheDocument()
    // No starting suggestions once there's a conversation.
    expect(screen.queryByRole('list', { name: 'Suggestions' })).not.toBeInTheDocument()
  })

  it('shows what the assistant can spend and whom it can pay beside the chat', async () => {
    stubServer({ contacts: [SAM, { name: 'alex', address: '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC' }] })
    await renderRoutes(routes, '/assistant')

    const aside = await screen.findByRole('complementary', { name: 'What the assistant can do' })
    expect(within(aside).getByText('$60.00')).toBeInTheDocument()
    expect(within(aside).getByText('of $100.00')).toBeInTheDocument()
    expect(within(aside).getByRole('progressbar', { name: '60% of the limit left' })).toBeInTheDocument()
    expect(within(aside).getByText(/^Resets in/)).toBeInTheDocument()
    expect(await within(aside).findByText('alex')).toBeInTheDocument()
    expect(within(aside).getByText('sam')).toBeInTheDocument()
    expect(within(aside).getByRole('link', { name: 'Manage contacts' })).toHaveAttribute('href', '/contacts')
  })

  it('opens the Telegram bot once Telegram is linked', async () => {
    stubServer({ telegramLinked: true, telegramBot: 'mitfah_bot' })
    await renderRoutes(routes, '/assistant')

    const open = await screen.findByRole('link', { name: 'Open in Telegram' })
    expect(open).toHaveAttribute('href', 'https://t.me/mitfah_bot')
    expect(open).toHaveAttribute('target', '_blank')
    expect(open).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('offers no Telegram link before Telegram is linked, or where the server runs no bot', async () => {
    for (const options of [{ telegramBot: 'mitfah_bot' }, { telegramLinked: true }]) {
      cleanup()
      resetClientForTests()
      stubServer(options)
      await renderRoutes(routes, '/assistant')
      // The chat waits for the account, which says both, so the link would be showing by now.
      await composer()
      expect(screen.queryByRole('link', { name: 'Open in Telegram' })).not.toBeInTheDocument()
    }
  })

  it('sends on Enter, shows the message at once, and waits for the reply', async () => {
    const reply = deferred()
    const { posted, calls } = stubServer({ answers: [{ reply: 'You have 1.5 ETH.' }], gate: reply.promise })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const input = await composer()
    await waitFor(() => expect(calls.wallet).toBe(1))
    await user.type(input, "  what's my balance?  {Enter}")

    expect(posted).toEqual([{ chain_id: SEPOLIA, message: "what's my balance?" }])
    expect(within(log()).getByText("what's my balance?")).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Mitfah is working… This can take up to a minute.')
    expect(input).toHaveValue('')
    expect(input).toHaveFocus()
    // The next message can be typed, not sent.
    await user.type(input, 'and my limit?')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.keyboard('{Enter}')
    expect(posted).toHaveLength(1)

    reply.open()
    expect(await within(log()).findByText('You have 1.5 ETH.')).toBeInTheDocument()
    expect(screen.queryByText(/Mitfah is working/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    expect(within(log()).getAllByText("what's my balance?")).toHaveLength(1)
    // The conversation and the wallet (a payment changes what's left) are read again.
    await waitFor(() => expect(calls.history).toBe(2))
    await waitFor(() => expect(calls.wallet).toBe(2))
    expect(input).toHaveValue('and my limit?')
  })

  it('keeps the chat, and what is being typed, when a wallet refresh fails', async () => {
    const { calls } = stubServer({ walletFailsLater: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const input = await composer()
    await user.type(input, 'hello{Enter}')
    await user.type(input, 'half-typed')
    expect(await within(log()).findByText('You said: hello')).toBeInTheDocument()
    // The reply makes the page read the wallet again, and that read fails.
    await waitFor(() => expect(calls.wallet).toBe(2))
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(screen.queryByText(/Couldn't load your wallet/)).not.toBeInTheDocument()
    expect(await composer()).toHaveValue('half-typed')
    expect(screen.getByRole('complementary', { name: 'What the assistant can do' })).toBeInTheDocument()
  })

  it('starts a new line with Shift+Enter', async () => {
    const { posted } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const input = await composer()
    await user.type(input, 'line one{Shift>}{Enter}{/Shift}line two')
    expect(input).toHaveValue('line one\nline two')
    expect(posted).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Send' }))
    expect(posted).toEqual([{ chain_id: SEPOLIA, message: 'line one\nline two' }])
    expect(await within(log()).findByText('You said: line one line two', { normalizer: s => s.replace(/\s+/g, ' ') })).toBeInTheDocument()
  })

  it('makes Enter a new line on a touch screen, where the button sends', async () => {
    ;(window as { coarsePointer?: boolean }).coarsePointer = true
    const { posted } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const input = await composer()
    await user.type(input, 'one{Enter}two')
    expect(input).toHaveValue('one\ntwo')
    expect(posted).toHaveLength(0)
  })

  it('does not send a word still being composed with an input method', async () => {
    const { posted } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const input = await composer()
    await user.type(input, 'こんにちは')
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 229, bubbles: true, cancelable: true }))
    expect(posted).toHaveLength(0)
  })

  it('never sends a blank or over-long message', async () => {
    const { posted } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const input = await composer()
    await user.type(input, '   {Enter}')
    expect(posted).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    await user.clear(input)
    await user.paste('x'.repeat(4_001))
    expect(screen.getByText('4,001 / 4,000 — too long to send')).toBeInTheDocument()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.keyboard('{Enter}')
    expect(posted).toHaveLength(0)
  })

  it('offers questions to start with, and drafts a payment without sending it', async () => {
    const { posted } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const suggestions = await screen.findByRole('list', { name: 'Suggestions' })
    await user.click(await within(suggestions).findByRole('button', { name: 'Send 5 USDC to sam…' }))
    expect(posted).toHaveLength(0)
    expect(await composer()).toHaveValue('Send 5 USDC to sam')
    expect(await composer()).toHaveFocus()

    await user.click(within(suggestions).getByRole('button', { name: 'What’s in my wallet?' }))
    expect(posted).toEqual([{ chain_id: SEPOLIA, message: 'What’s in my wallet?' }])
    expect(await within(log()).findByText('You said: What’s in my wallet?')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Suggestions' })).not.toBeInTheDocument()
  })

  it('drafts a swap where the assistant can trade, without sending it', async () => {
    const { posted } = stubServer({ exchange: 'uniswap-v3' })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const suggestions = await screen.findByRole('list', { name: 'Suggestions' })
    await user.click(await within(suggestions).findByRole('button', { name: 'Swap 1 ETH for USDC…' }))
    expect(posted).toHaveLength(0)
    expect(await composer()).toHaveValue('Swap 1 ETH for USDC')
    expect(await composer()).toHaveFocus()
  })

  it('offers no swap where the network has no exchange, though it lists a stablecoin', async () => {
    stubServer()
    await renderRoutes(routes, '/assistant')

    const suggestions = await screen.findByRole('list', { name: 'Suggestions' })
    expect(await within(suggestions).findByRole('button', { name: 'Send 5 USDC to sam…' })).toBeInTheDocument()
    expect(within(suggestions).queryByRole('button', { name: /^Swap/ })).toBeNull()
  })

  it('leaves out the payment suggestion when there is no one to pay', async () => {
    stubServer({ contacts: [] })
    await renderRoutes(routes, '/assistant')

    const suggestions = await screen.findByRole('list', { name: 'Suggestions' })
    expect(within(suggestions).getAllByRole('button')).toHaveLength(3)
    expect(await screen.findByText('No one yet. Add a contact before asking it to pay someone.')).toBeInTheDocument()
  })

  it('offers to send again when the server refused the message', async () => {
    const { posted } = stubServer({
      answers: [{ status: 422, detail: 'String should have at most 4000 characters' }, { reply: 'Hello!' }],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.type(await composer(), 'hello{Enter}')
    expect(await screen.findByText('Not sent.')).toBeInTheDocument()
    expect(screen.getByText('String should have at most 4000 characters')).toBeInTheDocument()
    expect(within(log()).getByText('hello')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await within(log()).findByText('Hello!')).toBeInTheDocument()
    expect(screen.queryByText('Not sent.')).not.toBeInTheDocument()
    expect(posted).toHaveLength(2)
  })

  it('puts a refused message back in the composer to edit', async () => {
    const { posted } = stubServer({ answers: [{ status: 400, detail: 'Unsupported chain ID: 11155111' }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.type(await composer(), 'hello{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Edit message' }))
    expect(await composer()).toHaveValue('hello')
    expect(await composer()).toHaveFocus()
    expect(screen.queryByText('Not sent.')).not.toBeInTheDocument()
    expect(within(log()).queryByText('hello')).not.toBeInTheDocument()
    expect(posted).toHaveLength(1)
  })

  it('never offers to resend a message that may have reached the assistant, and gives it back only if it did not', async () => {
    const { posted } = stubServer({ answers: ['offline'] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.type(await composer(), 'send 5 usdc to sam{Enter}')
    expect(await screen.findByText('No reply arrived.')).toBeInTheDocument()
    expect(screen.getByText(/may still have handled your message/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Check for a reply' }))
    // Not on the server, so it comes back to send on purpose.
    await waitFor(async () => expect(await composer()).toHaveValue('send 5 usdc to sam'))
    expect(screen.queryByText('No reply arrived.')).not.toBeInTheDocument()
    expect(posted).toHaveLength(1)
  })

  it('shows a message the server kept after the connection dropped, without giving it back', async () => {
    const { posted, calls } = stubServer({ answers: [{ status: 502 }], keepsFailed: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.type(await composer(), 'send 5 usdc to sam{Enter}')
    expect(await screen.findByText('No reply arrived.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Check for a reply' }))

    await waitFor(() => expect(calls.history).toBe(2))
    expect(await screen.findByText(/No reply to this yet/)).toBeInTheDocument()
    expect(within(log()).getAllByText('send 5 usdc to sam')).toHaveLength(1)
    expect(await composer()).toHaveValue('')
    await user.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(calls.history).toBe(3))
    expect(posted).toHaveLength(1)
  })

  it('keeps waiting after the page is left and come back to, and still allows one message at a time', async () => {
    const reply = deferred()
    const { posted } = stubServer({ gate: reply.promise })
    const user = userEvent.setup()
    const router = await renderRoutes(routes, '/assistant')

    await user.type(await composer(), 'first{Enter}')
    expect(await screen.findByText(/Mitfah is working/)).toBeInTheDocument()

    await router.navigate('/contacts')
    expect(await screen.findByRole('heading', { name: 'Contacts', level: 1 })).toBeInTheDocument()
    await router.navigate('/assistant')

    expect(await screen.findByText(/Mitfah is working/)).toBeInTheDocument()
    expect(within(log()).getByText('first')).toBeInTheDocument()
    await user.type(await composer(), 'second{Enter}')
    expect(posted).toHaveLength(1)

    reply.open()
    expect(await within(log()).findByText('You said: first')).toBeInTheDocument()
    expect(within(log()).getAllByText('first')).toHaveLength(1)
  })

  it('says why the assistant cannot send while the wallet is paused or it is turned off', async () => {
    stubServer({ wallet: makeWalletState({ paused: true }) })
    await renderRoutes(routes, '/assistant')
    expect(
      await screen.findByText('Your wallet is paused, so the assistant can answer questions but can’t send anything.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Controls' })).toHaveAttribute('href', '/controls')
    cleanup()

    stubServer({ wallet: makeWalletState({ session: makeSession('off'), is_owner: false }) })
    await renderRoutes(routes, '/assistant')
    expect(
      await screen.findByText('The assistant is turned off, so it can answer questions but can’t send anything.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open Controls' })).not.toBeInTheDocument()
  })

  it("says when the assistant's access has run out, and warns before it does", async () => {
    stubServer({ wallet: makeWalletState({ session: makeSession('expired') }) })
    await renderRoutes(routes, '/assistant')
    expect(
      await screen.findByText(
        'The assistant’s access has run out, so it can answer questions but can’t send anything. Renew it in Controls.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Controls' })).toHaveAttribute('href', '/controls')
    cleanup()

    stubServer({ wallet: makeWalletState({ session: makeSession('expiring') }) })
    await renderRoutes(routes, '/assistant')
    const notice = (await screen.findByText(/The assistant’s access runs out in (1 d 23 h|2 d)\./)).closest('.rs-message')
    expect(notice).toHaveClass('rs-message-warning')
  })

  it('warns in red when nothing caps what the assistant can spend', async () => {
    const spending = { ...makeWalletState().spending, hook_installed: false, daily_limit_usd: 0, remaining_usd: 0 }
    stubServer({ wallet: makeWalletState({ spending }) })
    await renderRoutes(routes, '/assistant')

    const notice = (await screen.findByText(/nothing caps what the assistant can spend/)).closest('.rs-message')
    expect(notice).toHaveClass('rs-message-error')
    const aside = screen.getByRole('complementary', { name: 'What the assistant can do' })
    expect(within(aside).getByText('No limit')).toBeInTheDocument()
    expect(within(aside).queryByRole('progressbar')).not.toBeInTheDocument()
    expect(within(aside).queryByText('$0.00')).not.toBeInTheDocument()
    cleanup()

    setViewportWidth(390)
    stubServer({ wallet: makeWalletState({ spending }) })
    await renderRoutes(routes, '/assistant')
    expect(await screen.findByText('No spending limit')).toBeInTheDocument()
  })

  it('asks for a wallet first when there is none', async () => {
    stubServer({ walletChains: [] })
    await renderRoutes(routes, '/assistant')
    expect(await screen.findByRole('heading', { name: 'Create your wallet' })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Message' })).not.toBeInTheDocument()
  })

  it('offers a retry when the conversation fails to load, and can still send', async () => {
    const { posted } = stubServer({ historyFailures: 1 })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    expect(await screen.findByText(/Couldn't load the conversation/)).toBeInTheDocument()
    await user.type(await composer(), 'hi{Enter}')
    expect(posted).toHaveLength(1)
    expect(await within(log()).findByText('You said: hi')).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load the conversation/)).not.toBeInTheDocument()
  })

  it('lets the keyboard shrink the page only while the chat is open', async () => {
    const meta = document.createElement('meta')
    meta.name = 'viewport'
    meta.content = 'width=device-width, initial-scale=1.0'
    document.head.append(meta)
    try {
      stubServer()
      const router = await renderRoutes(routes, '/assistant')
      await composer()
      expect(meta.content).toBe('width=device-width, initial-scale=1.0, interactive-widget=resizes-content')

      await router.navigate('/contacts')
      expect(await screen.findByRole('heading', { name: 'Contacts', level: 1 })).toBeInTheDocument()
      expect(meta.content).toBe('width=device-width, initial-scale=1.0')
    } finally {
      meta.remove()
    }
  })

  it('fits a phone: one summary line instead of the side panel, and an icon send button', async () => {
    setViewportWidth(390)
    stubServer()
    await renderRoutes(routes, '/assistant')

    expect(await screen.findByRole('link', { name: '1 contact' })).toHaveAttribute('href', '/contacts')
    expect(screen.getByText('$60.00').closest('.mf-budget-strip')).toHaveTextContent('$60.00 of $100.00 left')
    expect(screen.queryByRole('complementary', { name: 'What the assistant can do' })).not.toBeInTheDocument()
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).not.toHaveTextContent('Send')
  })

  it('clears the chat after asking, so the assistant forgets it', async () => {
    const { cleared } = stubServer({
      history: [
        { role: 'user', text: 'what can you do?' },
        { role: 'assistant', text: 'I can pay your contacts.' },
      ],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await composer()
    expect(await within(log()).findByText('what can you do?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear chat' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/including anything said on Telegram/)
    expect(dialog).toHaveTextContent(/transaction history stay as they are/)
    await user.click(within(dialog).getByRole('button', { name: 'Clear chat' }))

    expect(await screen.findByText('Chat cleared.')).toBeInTheDocument()
    expect(cleared).toEqual([`/api/chat/history?chain_id=${SEPOLIA}`])
    await waitFor(() => expect(within(log()).queryByText('what can you do?')).toBeNull())
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(screen.getByRole('button', { name: 'Clear chat' })).toBeDisabled()
  })

  it('says so when the chat cannot be cleared while the assistant is answering', async () => {
    stubServer({ history: [{ role: 'user', text: 'hi' }, { role: 'assistant', text: 'hello' }], clearStatus: 409 })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await composer()
    await within(log()).findByText('hi')
    await user.click(screen.getByRole('button', { name: 'Clear chat' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Clear chat' }))
    expect(await screen.findByText(/still answering your last message/)).toBeInTheDocument()
    expect(within(log()).getByText('hi')).toBeInTheDocument()
  })

  it('offers nothing to clear in an empty chat', async () => {
    stubServer()
    await renderRoutes(routes, '/assistant')

    await composer()
    expect(screen.getByRole('button', { name: 'Clear chat' })).toBeDisabled()
  })

  it('marks where the chat started afresh after a transaction, and points to the history', async () => {
    stubServer({
      history: [
        { role: 'user', text: 'yes', carried_over: true },
        { role: 'assistant', text: 'Sent 5 USDC to sam.', carried_over: true },
        { role: 'user', text: "what's my balance?" },
        { role: 'assistant', text: 'You have 20 USDC.' },
      ],
    })
    await renderRoutes(routes, '/assistant')

    await composer()
    expect(await within(log()).findByText(/Earlier messages were cleared/)).toBeInTheDocument()
    expect(within(log()).getByRole('link', { name: 'History' })).toHaveAttribute('href', '/history')
    expect(within(log()).getByText('Sent 5 USDC to sam.')).toBeInTheDocument()
  })

  it('offers a quoted transaction on a card, as plain text, and sends it with one tap', async () => {
    const action = 'Transfer 5 [USDC](https://evil.example) to sam'
    const sent = deferred()
    const { tapped, calls } = stubServer({
      answers: [{ reply: 'That costs $0.35 in fees. Reply yes to send it.', quote: makeQuote({ action }) }],
      tapGate: sent.promise,
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    expect(screen.queryByRole('region', { name: 'Transaction waiting for your answer' })).not.toBeInTheDocument()
    await user.type(await composer(), 'send 5 usdc to sam{Enter}')
    const card = await quoteCard()
    expect(within(card).getByText('Confirm on Sepolia?')).toBeInTheDocument()
    // The server's words, as text: a token's symbol can't become a link.
    expect(within(card).getByText(action)).toBeInTheDocument()
    expect(within(card).queryByRole('link')).not.toBeInTheDocument()
    expect(within(card).getByText('Fees: $0.35 · good for 5 minutes')).toBeInTheDocument()

    await user.click(within(card).getByRole('button', { name: 'Confirm' }))
    expect(tapped).toEqual([{ url: '/api/chat/quote/confirm', body: { chain_id: SEPOLIA, quote_id: 'q-abc_123' } }])
    expect(within(card).getByRole('status')).toHaveTextContent('Sending…')
    expect(within(card).queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    // One turn at a time: nothing else can be sent meanwhile.
    await user.type(await composer(), 'and my balance?')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    const wallets = calls.wallet
    sent.open()
    expect(await within(log()).findByText('✅ Confirm')).toBeInTheDocument()
    const outcome = within(log()).getByText(`Sending — ${action}. Waiting for the network to confirm it.`)
    expect(outcome.tagName).toBe('P')
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Transaction waiting for your answer' })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    // A payment changes the balances and what is left to spend.
    await waitFor(() => expect(calls.wallet).toBeGreaterThan(wallets))
    expect(tapped).toHaveLength(1)
  })

  it('follows a confirmed send until it lands, then reads the wallet again', async () => {
    const { calls } = stubServer({ quote: makeQuote(), landing: ['pending', 'confirmed'] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.click(within(await quoteCard()).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByText('Waiting for the network to confirm “Transfer 5 USDC to sam”…')).toBeInTheDocument()
    const wallets = calls.wallet
    const landed = await screen.findByText(/Confirmed on Sepolia: Transfer 5 USDC to sam\./, {}, { timeout: 5_000 })
    expect(within(landed).getByRole('link', { name: 'View transaction' })).toHaveAttribute(
      'href',
      `https://sepolia.etherscan.io/tx/0x${'ab'.repeat(32)}`,
    )
    // What it changed is read again, and the row is read no more.
    await waitFor(() => expect(calls.wallet).toBeGreaterThan(wallets))
    const reads = calls.transaction
    await new Promise(resolve => setTimeout(resolve, 2_500))
    expect(calls.transaction).toBe(reads)

    // The next message clears a send that has landed.
    await user.type(await composer(), 'thanks{Enter}')
    await waitFor(() => expect(screen.queryByText(/Confirmed on Sepolia/)).not.toBeInTheDocument())
  }, 15_000)

  it('says when a confirmed send failed on chain, and points to History', async () => {
    stubServer({ quote: makeQuote(), landing: ['failed'] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.click(within(await quoteCard()).getByRole('button', { name: 'Confirm' }))
    const failed = await screen.findByText(/“Transfer 5 USDC to sam” went out but failed on chain/)
    expect(within(failed).getByRole('link', { name: 'History' })).toHaveAttribute('href', '/history')
  })

  it('follows nothing when the confirm sent nothing', async () => {
    const { calls } = stubServer({
      quote: makeQuote(),
      taps: [{ outcome: "That didn't go through: not enough USDC", transaction_id: null }],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.click(within(await quoteCard()).getByRole('button', { name: 'Confirm' }))
    expect(await within(log()).findByText("That didn't go through: not enough USDC")).toBeInTheDocument()
    expect(screen.queryByText(/Waiting for the network/)).not.toBeInTheDocument()
    expect(calls.transaction).toBe(0)
  })

  it('shows the waiting quote again after a reload, and cancels it', async () => {
    const { tapped } = stubServer({
      history: [
        { role: 'user', text: 'send 5 usdc to sam' },
        { role: 'assistant', text: 'Reply yes to send it.' },
      ],
      quote: makeQuote(),
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    const card = await quoteCard()
    await user.click(within(card).getByRole('button', { name: 'Cancel' }))
    expect(await within(log()).findByText('✖️ Cancel')).toBeInTheDocument()
    expect(within(log()).getByText('Cancelled. Nothing was sent.')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Transaction waiting for your answer' })).not.toBeInTheDocument()
    expect(tapped).toEqual([{ url: '/api/chat/quote/cancel', body: { chain_id: SEPOLIA, quote_id: 'q-abc_123' } }])
  })

  it('says why when a quote is gone, and nothing was sent', async () => {
    stubServer({ quote: makeQuote(), taps: [{ status: 409, detail: STALE }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.click(within(await quoteCard()).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByText(STALE)).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Transaction waiting for your answer' })).not.toBeInTheDocument(),
    )
    expect(within(log()).queryByText('✅ Confirm')).not.toBeInTheDocument()
  })

  it('warns to check History when a confirm may have gone out', async () => {
    const { tapped, calls } = stubServer({ quote: makeQuote(), taps: ['offline'] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/assistant')

    await user.click(within(await quoteCard()).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByText('No answer arrived.')).toBeInTheDocument()
    const warning = screen.getByText(/may still have been sent/)
    expect(within(warning).getByRole('link', { name: 'History' })).toHaveAttribute('href', '/history')
    // The chat is read again, in case the server recorded what happened.
    await waitFor(() => expect(calls.history).toBe(2))
    expect(tapped).toHaveLength(1)
  })

  it('says when a quote has expired, with nothing left to tap', async () => {
    stubServer({ quote: makeQuote({ expires_at: Math.floor(Date.now() / 1000) - 1 }) })
    await renderRoutes(routes, '/assistant')

    expect(await screen.findByText(/The quote for “Transfer 5 USDC to sam” has expired/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
  })

  it('shows a button tap from the history as plain text', async () => {
    stubServer({
      history: [
        { role: 'user', text: '✅ Confirm', button: true },
        { role: 'assistant', text: 'Sent — Transfer 1 [x](https://evil.example) to sam.', button: true },
      ],
    })
    await renderRoutes(routes, '/assistant')

    await composer()
    expect(await within(log()).findByText('Sent — Transfer 1 [x](https://evil.example) to sam.')).toBeInTheDocument()
    expect(within(log()).queryByRole('link')).not.toBeInTheDocument()
  })
})
