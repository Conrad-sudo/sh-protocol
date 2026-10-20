import { AxeBuilder } from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN, walletState } from './helpers.ts'

/*
 * The Telegram card in Settings at every screen size, in both themes, against a mocked API. The
 * "bot" is the test: it flips the account to linked, as the real bot does when Start is pressed.
 * Also the dashboard's way there.
 */

const LINK = 'https://t.me/mitfah_bot?start=e2e-nonce'
const BASE = 8453
const SEPOLIA = 11155111

/** Networks the account has a wallet on. One bot serves them all. */
const CHAINS = [
  { chain_id: BASE, name: 'base', native_ticker: 'ETH' },
  { chain_id: SEPOLIA, name: 'sepolia', native_ticker: 'ETH' },
].map(chain => ({ ...chain, fork: false, rpc_url: null, default_spenders: [], exchange: null }))

async function mockApi(page: Page, { linked = false, notConfigured = false } = {}) {
  const server = { linked, unlinks: 0 }
  await page.route(isApiUrl, route => {
    const method = route.request().method()
    const path = new URL(route.request().url()).pathname
    // The dashboard's wallet, on whichever network it asks for.
    const wallet = /^\/api\/wallet\/(\d+)$/.exec(path)
    if (wallet) return route.fulfill({ json: walletState(Number(wallet[1]), '0x2222222222222222222222222222222222222222') })
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({
          json: {
            ...ME,
            telegram_linked: server.linked,
            telegram_bot: notConfigured ? null : 'mitfah_bot',
            wallet_chains: [BASE, SEPOLIA],
          },
        })
      case '/api/chains':
        return route.fulfill({ json: { chains: CHAINS } })
      case '/api/integrations/telegram/link':
        if (method === 'DELETE') {
          server.unlinks++
          server.linked = false
          return route.fulfill({ json: { status: 'unlinked' } })
        }
        if (notConfigured) {
          return route.fulfill({
            status: 500,
            json: { detail: 'Telegram is not configured: MITFAH_USERNAME is not set' },
          })
        }
        return route.fulfill({ json: { url: LINK, nonce: 'e2e-nonce', expires_in: 600 } })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return server
}

/** axe's serious and critical findings on the page as it is, once any entrance has finished (as in a11y.spec.ts). */
async function seriousProblems(page: Page) {
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .every(animation => animation.playState !== 'running' || animation.effect?.getComputedTiming().endTime === Infinity),
  )
  const { violations } = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  return violations
    .filter(violation => violation.impact === 'serious' || violation.impact === 'critical')
    .map(violation => `${violation.id}: ${violation.nodes.map(node => node.target).join(' · ')}`)
}

test('linking Telegram: link, wait, linked', async ({ page }, testInfo) => {
  const server = await mockApi(page)
  await page.goto('/settings')

  const card = page.locator('#telegram')
  await card.getByRole('button', { name: 'Link Telegram' }).click()
  const open = card.getByRole('link', { name: 'Open Telegram' })
  await expect(open).toHaveAttribute('href', LINK)
  await expect(card.getByText('Waiting for Telegram…')).toBeVisible()

  // A touch screen (phone, tablet) opens the link itself, so only desktops show the QR code.
  const touch = testInfo.project.name.startsWith('phone') || testInfo.project.name.startsWith('tablet')
  await expect(card.locator('.mf-telegram-qr')).toBeVisible({ visible: !touch })

  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  // Of the card only: a full-page capture turns touch emulation off, which would show the QR code.
  await card.screenshot({ path: testInfo.outputPath('settings-telegram-waiting.png') })
  await expect(card.locator('.mf-telegram-qr')).toBeVisible({ visible: !touch })

  server.linked = true
  await expect(card.getByText('Linked')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('Telegram linked. You can now chat with your assistant there.')).toBeVisible()
  await expect(open).toBeHidden()
  // Once linked, the one bot is a link away, and on a desktop a QR code away too; /chain there
  // switches networks.
  const bot = card.locator('.mf-telegram-bot')
  await expect(bot.getByRole('link', { name: '@mitfah_bot' })).toHaveAttribute('href', 'https://t.me/mitfah_bot')
  await expect(bot.getByText('Send /chain there to switch networks.')).toBeVisible()
  await expect(bot.locator('.mf-telegram-qr')).toBeVisible({ visible: !touch })
  await expectNoSidewaysScroll(page)
  expect(await seriousProblems(page)).toEqual([])
  // Of the card only, as above: a full-page capture would show the code on a touch screen.
  await card.screenshot({ path: testInfo.outputPath('settings-telegram-bot.png') })
  await snap(page, testInfo, 'settings-telegram-linked')

  await card.getByRole('button', { name: 'Unlink' }).click()
  await expect(card.getByRole('button', { name: 'Link Telegram' })).toBeVisible()
  expect(server.unlinks).toBe(1)
})

test('a server without a Telegram bot says so', async ({ page }, testInfo) => {
  await mockApi(page, { notConfigured: true })
  await page.goto('/settings')

  const card = page.locator('#telegram')
  await card.getByRole('button', { name: 'Link Telegram' }).click()
  await expect(card.getByText("Telegram isn't set up on this server.")).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'settings-telegram-unavailable')
})

test("the dashboard's Link Telegram opens Settings at the Telegram card", async ({ page }, testInfo) => {
  await mockApi(page)
  await page.goto('/dashboard')

  const link = page.getByRole('link', { name: 'Link Telegram' })
  await expect(link).toBeVisible()
  await expectNoSidewaysScroll(page)
  await page.locator('.mf-page-header').screenshot({ path: testInfo.outputPath('dashboard-link-telegram.png') })

  await link.click()
  await expect(page).toHaveURL(/\/settings#telegram$/)
  const card = page.locator('#telegram')
  await expect(card.getByRole('button', { name: 'Link Telegram' })).toBeInViewport()
  // Clear of the top bar that floats over the page.
  const bar = await page.locator('.mf-topbar').boundingBox()
  const title = await card.getByText('Telegram', { exact: true }).boundingBox()
  expect(title!.y).toBeGreaterThanOrEqual(bar!.y + bar!.height)
  await snap(page, testInfo, 'settings-opened-at-telegram')
})
