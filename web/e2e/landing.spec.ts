import { expect, test, type Page } from '@playwright/test'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, snap } from './helpers.ts'

/* The landing and legal pages, signed out, at every screen size in both themes. */

const CHAINS = [
  { chain_id: 11155111, name: 'sepolia-fork', native_ticker: 'ETH', fork: true },
  { chain_id: 1, name: 'mainnet', native_ticker: 'ETH', fork: false },
  { chain_id: 56, name: 'bsc', native_ticker: 'BNB', fork: false },
  { chain_id: 42161, name: 'arbitrum', native_ticker: 'ETH', fork: false },
  { chain_id: 42220, name: 'celo', native_ticker: 'CELO', fork: false },
]

async function mockApi(page: Page) {
  await page.route(isApiUrl, route => {
    switch (new URL(route.request().url()).pathname) {
      case '/api/auth/refresh':
        return route.fulfill({ status: 401, json: { detail: 'No refresh token' } })
      case '/api/chains':
        return route.fulfill({ json: { chains: CHAINS } })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
}

test('home page', async ({ page }, testInfo) => {
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('only spend what you allow')
  await expect(page.getByRole('region', { name: 'Networks' }).getByRole('listitem')).toHaveCount(CHAINS.length)
  await expectTheme(page, testInfo)

  // One answer open at a time.
  const first = page.getByText('Does Mitfah hold my money?')
  const second = page.getByText('What can the assistant do?')
  await first.click()
  await expect(page.getByText('Mitfah never has your owner key')).toBeVisible()
  await second.click()
  await expect(page.getByText('Check balances, send tokens')).toBeVisible()
  await expect(page.getByText('Mitfah never has your owner key')).toBeHidden()

  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'home')

  await page.getByRole('navigation', { name: 'Legal' }).getByRole('link', { name: 'Terms' }).click()
  await expect(page).toHaveURL(/\/terms$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Terms of Service' })).toBeVisible()
})

for (const [path, title] of [
  ['/terms', 'Terms of Service'],
  ['/privacy', 'Privacy Policy'],
] as const) {
  test(`${title} page`, async ({ page }, testInfo) => {
    await mockApi(page)
    await page.goto(path)
    await expect(page.getByRole('heading', { level: 1, name: title })).toBeVisible()
    await expect(page.getByText("hasn't been reviewed by a lawyer")).toBeVisible()
    await expect(page).toHaveTitle(`${title} · Mitfah`)
    await expectNoSidewaysScroll(page)
    await snap(page, testInfo, path.slice(1))
  })
}

test('the sign-in card links to the legal pages', async ({ page }) => {
  await mockApi(page)
  await page.goto('/login')
  await page.getByRole('navigation', { name: 'Legal' }).getByRole('link', { name: 'Privacy' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Privacy Policy' })).toBeVisible()
})

test('each public page describes itself to search engines and link previews', async ({ page }) => {
  await mockApi(page)
  for (const [path, title] of [
    ['/', 'Mitfah — an AI assistant for your crypto wallet'],
    ['/terms', 'Terms of Service · Mitfah'],
    ['/privacy', 'Privacy Policy · Mitfah'],
  ] as const) {
    await page.goto(path)
    await expect(page).toHaveTitle(title)
    // One of each: index.html's placeholder title goes once the page has rendered its own.
    await expect(page.locator('title')).toHaveCount(1)
    await expect(page.locator('meta[name="description"]')).toHaveCount(1)
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href', `https://mitfah.com${path}`)
    await expect(page.locator('meta[property="og:url"]')).toHaveAttribute('content', `https://mitfah.com${path}`)
  }

  await page.goto('/')
  const data = JSON.parse((await page.locator('script[type="application/ld+json"]').textContent()) ?? '')
  expect(data['@graph'].map((node: { '@type': string }) => node['@type'])).toEqual([
    'Organization',
    'WebSite',
    'WebApplication',
  ])
})

test('a missing page asks not to be indexed', async ({ page }) => {
  await mockApi(page)
  await page.goto('/no-such-page')
  await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible()
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute('content', 'noindex')
  await expect(page.locator('link[rel="canonical"]')).toHaveCount(0)
})

test('crawlers get robots.txt, llms.txt and the share image', async ({ request }) => {
  const robots = await request.get('/robots.txt')
  expect(await robots.text()).toContain('Disallow: /api/')
  const llms = await request.get('/llms.txt')
  expect(await llms.text()).toMatch(/^# Mitfah\n/)
  const og = await request.get('/og.png')
  expect(og.headers()['content-type']).toBe('image/png')
})
