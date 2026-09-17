import { expect, test, type Page } from '@playwright/test'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN, walletState } from './helpers.ts'

/*
 * The Assistant page at real screen sizes, in both themes, against a mocked API. No model is
 * called: `make agent-smoke` covers the agent itself.
 */

const SEPOLIA = 11155111
const ADDRESS = '0x2222222222222222222222222222222222222222'
const SAM = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'

interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
}

// A reply with everything that could push a phone sideways.
const WIDE_REPLY = [
  'Here are your balances on **Sepolia**:',
  '| Token | Amount | Contract address |\n| --- | --- | --- |\n| ETH | 1.5 | — |\n| USDC | 25.000000 | 0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8 |',
  '```\ntransfer(0x70997970C51812dc3A010C7d01b50e0d17dc79C8, 5000000) → 0x' + 'ab'.repeat(32) + '\n```',
  `Your wallet is ${ADDRESS}${'0'.repeat(20)} and nothing is pending.`,
].join('\n\n')

function longHistory(): ChatMessage[] {
  const history: ChatMessage[] = []
  for (let i = 1; i <= 6; i++) {
    history.push({ role: 'user', text: `Question ${i}: how much can I still spend today?` })
    history.push({ role: 'assistant', text: `You can still spend **$${60 - i}** of your $100 limit. It resets in 23 hours.` })
  }
  history.push({ role: 'user', text: 'show my balances' })
  history.push({ role: 'assistant', text: WIDE_REPLY })
  return history
}

interface Options {
  history?: ChatMessage[]
  /** How POST /api/chat answers. */
  reply?: { text: string; delayMs?: number } | 'offline'
}

async function mockServer(page: Page, { history = [], reply = { text: 'Done.' } }: Options = {}) {
  let thread = [...history]
  const posted: unknown[] = []

  await page.route(isApiUrl, async route => {
    const request = route.request()
    const url = new URL(request.url())
    switch (url.pathname) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, owner_addr: SAM, wallet_chains: [SEPOLIA] } })
      case '/api/chains':
        return route.fulfill({ json: { chains: [{ chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH', fork: true }] } })
      case `/api/wallet/${SEPOLIA}`:
        return route.fulfill({ json: walletState(SEPOLIA, ADDRESS) })
      case '/api/contacts':
        return route.fulfill({ json: { contacts: [{ name: 'sam', address: SAM }] } })
      case '/api/chat/history':
        return route.fulfill({ json: { chain_id: SEPOLIA, messages: thread } })
      case '/api/chat': {
        const body = request.postDataJSON() as { message: string }
        posted.push(body)
        if (reply === 'offline') return route.abort('connectionreset')
        if (reply.delayMs) await new Promise(resolve => setTimeout(resolve, reply.delayMs))
        thread = [...thread, { role: 'user', text: body.message }, { role: 'assistant', text: reply.text }]
        return route.fulfill({ json: { reply: reply.text } })
      }
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return { posted }
}

/** The composer is on screen and, on phones, clear of the tab bar. */
async function expectComposerInView(page: Page) {
  const composer = page.locator('.mf-composer')
  await expect(composer).toBeInViewport({ ratio: 1 })
  const tabbar = page.locator('.mf-tabbar')
  if (await tabbar.isVisible()) {
    const [form, tabs] = await Promise.all([composer.boundingBox(), tabbar.boundingBox()])
    expect(form!.y + form!.height, 'composer overlaps the tab bar').toBeLessThanOrEqual(tabs!.y + 1)
  }
}

test('a long conversation fits the screen, and a new reply scrolls into view', async ({ page }, testInfo) => {
  const { posted } = await mockServer(page, {
    history: longHistory(),
    reply: { text: 'You have **1.5 ETH** and 25 USDC.', delayMs: 1_500 },
  })
  await page.goto('/assistant')

  const log = page.getByRole('log', { name: 'Conversation with Mitfah' })
  await expect(log.getByRole('table')).toBeVisible()
  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  // Opens at the newest message.
  await expect(log.getByText('nothing is pending')).toBeInViewport()
  await expectComposerInView(page)
  await snap(page, testInfo, 'assistant-history')
  await page.screenshot({ path: testInfo.outputPath('assistant-history-screen.png') })

  const input = page.getByRole('textbox', { name: 'Message' })
  await input.fill('what is my balance?')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Mitfah is working' })).toBeInViewport()
  await expect(log.getByText('what is my balance?')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('assistant-working.png') })

  await expect(log.getByText('1.5 ETH')).toBeInViewport()
  await expect(page.getByText('Mitfah is working')).toHaveCount(0)
  await expectComposerInView(page)
  await expectNoSidewaysScroll(page)
  expect(posted).toEqual([{ chain_id: SEPOLIA, message: 'what is my balance?' }])
})

test('an empty conversation offers somewhere to start', async ({ page }, testInfo) => {
  const { posted } = await mockServer(page, { reply: { text: 'You have 1.5 ETH.' } })
  await page.goto('/assistant')

  const suggestions = page.getByRole('list', { name: 'Suggestions' })
  await expect(suggestions).toBeVisible()
  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  await expectComposerInView(page)
  await snap(page, testInfo, 'assistant-empty')

  await suggestions.getByRole('button', { name: 'Send 5 USDC to sam…' }).click()
  await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('Send 5 USDC to sam')
  expect(posted).toHaveLength(0)

  await suggestions.getByRole('button', { name: 'What’s in my wallet?' }).click()
  await expect(page.getByRole('log').getByText('You have 1.5 ETH.')).toBeVisible()
  expect(posted).toHaveLength(1)
})

test('on a phone the composer takes the tab bar’s place while typing', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('phone'), 'phones only')
  await mockServer(page, { history: longHistory() })
  await page.goto('/assistant')

  const tabbar = page.locator('.mf-tabbar')
  await expect(tabbar).toBeVisible()
  await expectComposerInView(page)

  await page.getByRole('textbox', { name: 'Message' }).focus()
  await expect(tabbar).toBeHidden()
  await expectComposerInView(page)
  const form = await page.locator('.mf-composer').boundingBox()
  expect(form!.y + form!.height).toBeGreaterThan(page.viewportSize()!.height - 2)
  await page.screenshot({ path: testInfo.outputPath('assistant-typing.png') })

  await page.getByRole('textbox', { name: 'Message' }).blur()
  await expect(tabbar).toBeVisible()
})

test('a dropped connection asks to check before sending again', async ({ page }, testInfo) => {
  const { posted } = await mockServer(page, { reply: 'offline' })
  await page.goto('/assistant')

  const input = page.getByRole('textbox', { name: 'Message' })
  await input.fill('send 5 usdc to sam')
  await input.press(testInfo.project.name.startsWith('desktop') ? 'Enter' : 'Tab')
  if (!testInfo.project.name.startsWith('desktop')) await page.getByRole('button', { name: 'Send', exact: true }).click()

  await expect(page.getByText('No reply arrived.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Try again' })).toHaveCount(0)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'assistant-dropped')

  await page.getByRole('button', { name: 'Check for a reply' }).click()
  await expect(input).toHaveValue('send 5 usdc to sam')
  expect(posted).toHaveLength(1)
})

test('only the chat lets the keyboard shrink the page', async ({ page }) => {
  await mockServer(page)
  const viewport = page.locator('meta[name="viewport"]')
  await page.goto('/assistant')
  await expect(page.getByRole('textbox', { name: 'Message' })).toBeVisible()
  await expect(viewport).toHaveAttribute('content', /interactive-widget=resizes-content/)

  // The sidebar, the tablet rail or the phone's tab bar, whichever this screen has.
  await page.locator('a[href="/contacts"]:visible').first().click()
  await expect(page.getByRole('heading', { name: 'Contacts', level: 1 })).toBeVisible()
  await expect(viewport).not.toHaveAttribute('content', /interactive-widget/)
})
