// Renders scripts/og/og.html into public/og.png (1200×630). Run with `npm run og`.
import { chromium } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const template = new URL('./og.html', import.meta.url).href
const out = fileURLToPath(new URL('../../public/og.png', import.meta.url))

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } })
await page.goto(template)
await page.evaluate(() => document.fonts.ready)
await page.locator('.card').screenshot({ path: out })
await browser.close()
console.log(`Wrote ${out}`)
