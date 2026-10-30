import { fileURLToPath } from 'node:url'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'
import { searchForWorkspaceRoot, type Connect, type Plugin } from 'vite'
import { defineConfig } from 'vitest/config'

/** The Telegram links' confirm page, an entry of its own (confirm.html, src/confirm/). */
const CONFIRM_PAGE = '/confirm.html'
/** A confirm link: /c/<token>. */
const CONFIRM_LINK = /^\/c\/[^/]+\/?$/

/**
 * Preloads the Latin cuts of the two faces on the first screen, Bodoni Moda (the headline) and
 * Archivo (the text), so a prerendered page's first paint is already in them. Found late, a font
 * swaps in after that paint and the headline reflows.
 */
const FIRST_PAINT_FONTS = [/bodoni-moda-latin-opsz-normal-.*\.woff2$/, /archivo-latin-wght-normal-.*\.woff2$/]

function preloadHeadlineFont(): Plugin {
  return {
    name: 'mitfah:preload-headline-font',
    apply: 'build',
    transformIndexHtml: {
      order: 'post',
      handler(_html, ctx) {
        // The confirm page draws in the system's faces; preloading these would only slow it down.
        if (ctx.filename.endsWith(CONFIRM_PAGE)) return []
        const files = Object.keys(ctx.bundle ?? {})
        return FIRST_PAINT_FONTS.flatMap(pattern => {
          const font = files.find(file => pattern.test(file))
          if (!font) {
            this.warn(`No font matching ${pattern} in the bundle, so it isn't preloaded. Update FIRST_PAINT_FONTS.`)
            return []
          }
          return [
            {
              tag: 'link',
              attrs: { rel: 'preload', href: `/${font}`, as: 'font', type: 'font/woff2', crossorigin: '' },
              injectTo: 'head' as const,
            },
          ]
        })
      },
    },
  }
}

/**
 * Serves /c/<token> from confirm.html in the dev server and `vite preview`, as deploy/Caddyfile does
 * on the live site. Without it both would answer with index.html, the app's own shell.
 */
function confirmPage(): Plugin {
  const rewrite: Connect.NextHandleFunction = (req, _res, next) => {
    const path = req.url?.split('?')[0] ?? ''
    if (CONFIRM_LINK.test(path)) req.url = CONFIRM_PAGE
    next()
  }
  return {
    name: 'mitfah:confirm-page',
    configureServer: server => void server.middlewares.use(rewrite),
    configurePreviewServer: server => void server.middlewares.use(rewrite),
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] }),
    preloadHeadlineFont(),
    confirmPage(),
  ],
  build: {
    rolldownOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        confirm: fileURLToPath(new URL('./confirm.html', import.meta.url)),
      },
    },
  },
  server: {
    // 3000 is what the API allows by default (CORS_ORIGINS) and what SIWE_DOMAIN defaults to.
    // strictPort: silently moving to 3001 would make every sign-in message name the wrong site
    // and be refused.
    port: 3000,
    strictPort: true,
    // A phone test reaches this server through Tailscale Funnel (https://<mac>.<tailnet>.ts.net),
    // since Telegram won't link to localhost. Vite refuses any other Host header.
    allowedHosts: ['.ts.net'],
    // Same origin as the page, so the refresh cookie (path /api/auth) is sent without CORS.
    proxy: {
      '/api': 'http://localhost:8000',
    },
    fs: {
      // Vite's default, plus ../deploy: src/securityHeaders.test.ts checks the security headers the
      // Caddyfile there sends against this app. That folder holds nothing secret.
      allow: [searchForWorkspaceRoot(process.cwd()), '../deploy'],
    },
  },
  test: {
    // e2e/ holds Playwright specs, which Vitest must not try to run.
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    // The first test in a file also loads the app's lazy pages (routes.tsx), which on a cold
    // transform costs more than the 5 s default.
    testTimeout: 20_000,
  },
})
