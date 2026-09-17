import { expect, test, type Page } from '@playwright/test'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, snap, TOKEN } from './helpers.ts'

/*
 * The Telegram card in Settings at every screen size, in both themes, against a mocked API. The
 * "bot" is the test: it flips the account to linked, as the real bot does when Start is pressed.
 */

const LINK = 'https://t.me/mitfah_bot?start=e2e-nonce'

async function mockApi(page: Page, { linked = false, notConfigured = false } = {}) {
  const server = { linked, unlinks: 0 }
  await page.route(isApiUrl, route => {
    const method = route.request().method()
    switch (new URL(route.request().url()).pathname) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: { ...ME, telegram_linked: server.linked } })
      case '/api/integrations/telegram/link':
        if (method === 'DELETE') {
          server.unlinks++
          server.linked = false
          return route.fulfill({ json: { status: 'unlinked' } })
        }
        if (notConfigured) {
          return route.fulfill({ status: 500, json: { detail: 'TELEGRAM_BOT_USERNAME is not configured' } })
        }
        return route.fulfill({ json: { url: LINK, nonce: 'e2e-nonce', expires_in: 600 } })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return server
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
  await expectNoSidewaysScroll(page)
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
