// Writes the public pages into dist/ as finished HTML, so search engines, AI crawlers and link
// previews read the page itself instead of an empty <div id="root">. Runs at the end of
// `npm run build` (on its own: `npm run prerender`, after `vite build`).
//
// Each page is drawn by the built app in a real browser, signed out and in the light theme, and
// its #root and head tags are copied into a file of its own: / → index.html, /terms → terms.html.
// The app then replaces the copy when it loads (main.tsx). index.html is also what the host serves
// for every other address, so the copy carries the address it belongs to and index.html drops it
// anywhere else before it is painted.
//
// Head tags the page rendered itself (title, description, canonical, share tags) are marked
// data-static, like index.html's own title, so the app removes them once React has drawn its own.
import { chromium } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { preview } from 'vite'

const PAGES = [
  { path: '/', file: 'index.html' },
  { path: '/terms', file: 'terms.html' },
  { path: '/privacy', file: 'privacy.html' },
]

const dist = (name: string) => fileURLToPath(new URL(`../../dist/${name}`, import.meta.url))
const shell = await readFile(dist('index.html'), 'utf8')
const EMPTY_ROOT = '<div id="root"></div>'
const STATIC_TITLE = /<title data-static>[^<]*<\/title>/
if (!shell.includes(EMPTY_ROOT) || !STATIC_TITLE.test(shell)) {
  throw new Error(
    'dist/index.html has no empty #root or no <title data-static>: already prerendered? Run `vite build` first.',
  )
}

// Port 0: any free port, so a running dev or preview server is left alone.
const server = await preview({
  root: fileURLToPath(new URL('../..', import.meta.url)),
  preview: { port: 0, strictPort: false, open: false },
  logLevel: 'warn',
})
const origin = server.resolvedUrls!.local[0].replace(/\/$/, '')
const browser = await chromium.launch()

const rendered: { file: string; html: string }[] = []
try {
  for (const { path, file } of PAGES) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: 'light' })
    await page.route('**/*', route => {
      const url = new URL(route.request().url())
      // Nothing leaves the machine, and the page is drawn for a signed-out visitor. Data the page
      // only shows once the API answers (the networks list) is left for the live page to add.
      if (url.origin !== origin) return route.abort()
      if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 401, json: { detail: 'Prerender' } })
      return route.continue()
    })
    await page.goto(origin + path)
    await page.locator('#root h1').waitFor()
    await page.waitForLoadState('networkidle')
    await page.evaluate(() => document.fonts.ready)

    const { head, root } = await page.evaluate(sourceHtml => {
      const before = new Set(
        [...new DOMParser().parseFromString(sourceHtml, 'text/html').head.children].map(el => el.outerHTML),
      )
      const added = [...document.head.children].filter(el => !before.has(el.outerHTML))
      const meta = added.filter(el => el.matches('title, meta, link[rel="canonical"]'))
      meta.forEach(el => el.setAttribute('data-static', ''))
      // The page's own code and styles, so the browser fetches them alongside the HTML. Vite adds
      // these with this server's absolute address; the file needs the path alone.
      const assets = added.filter(el => el.matches('link[rel="modulepreload"], link[rel="stylesheet"]'))
      assets.forEach(el => el.setAttribute('href', new URL(el.getAttribute('href')!, location.href).pathname))
      return {
        head: [...meta, ...assets].map(el => el.outerHTML),
        root: document.getElementById('root')!.innerHTML,
      }
    }, shell)

    const titles = head.filter(tag => tag.startsWith('<title')).length
    const canonicals = head.filter(tag => tag.includes('rel="canonical"')).length
    if (titles !== 1 || canonicals !== 1) {
      throw new Error(`${path} rendered ${titles} titles and ${canonicals} canonical links; expected one of each.`)
    }

    const html = shell
      .replace(STATIC_TITLE, () => head.join('\n    '))
      .replace(EMPTY_ROOT, () => `<div id="root" data-prerendered="${path}">${root}</div>`)
    if (html.includes(origin)) throw new Error(`${path} still names the prerender server (${origin}).`)
    rendered.push({ file, html })
    await page.close()
  }
} finally {
  await browser.close()
  await server.close()
}

// Written only once every page has rendered: until then index.html is the shell the others load.
for (const { file, html } of rendered) {
  await writeFile(dist(file), html)
  console.log(`Prerendered dist/${file} (${Math.round(html.length / 1024)} kB)`)
}
