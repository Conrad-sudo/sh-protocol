import { fileURLToPath } from 'node:url'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'
import { loadEnv, type Plugin } from 'vite'
import { defineConfig } from 'vitest/config'

/**
 * Preloads the Latin cut of Archivo, the headline font, so a prerendered page's first paint is
 * already in it. Found late, the font swaps in after that paint and the headline reflows.
 */
function preloadHeadlineFont(): Plugin {
  return {
    name: 'mitfah:preload-headline-font',
    apply: 'build',
    transformIndexHtml: {
      order: 'post',
      handler(_html, ctx) {
        const font = Object.keys(ctx.bundle ?? {}).find(file => /archivo-latin-wdth-normal-.*\.woff2$/.test(file))
        if (!font) {
          this.warn('No Archivo Latin font in the bundle, so none is preloaded. Update preloadHeadlineFont().')
          return []
        }
        return [
          {
            tag: 'link',
            attrs: { rel: 'preload', href: `/${font}`, as: 'font', type: 'font/woff2', crossorigin: '' },
            injectTo: 'head',
          },
        ]
      },
    },
  }
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Google's client ID is one setting shared with the API (GOOGLE_CLIENT_ID in the repo's .env).
  // web/.env.local can override it with VITE_GOOGLE_CLIENT_ID. The root .env also holds the API's
  // secrets, so only this single, public value is copied into the bundle.
  const webEnv = loadEnv(mode, process.cwd(), 'VITE_')
  const rootEnv = loadEnv(mode, fileURLToPath(new URL('..', import.meta.url)), '')
  const googleClientId = webEnv.VITE_GOOGLE_CLIENT_ID || rootEnv.GOOGLE_CLIENT_ID || ''

  return {
    plugins: [
      react(),
      babel({ presets: [reactCompilerPreset()] }),
      preloadHeadlineFont(),
    ],
    define: {
      'import.meta.env.VITE_GOOGLE_CLIENT_ID': JSON.stringify(googleClientId),
    },
    server: {
      // 3000 is what the API allows by default (CORS_ORIGINS) and what SIWE_DOMAIN defaults to.
      // strictPort: silently moving to 3001 would make every wallet-binding message name the
      // wrong site and be refused.
      port: 3000,
      strictPort: true,
      // Same origin as the page, so the refresh cookie (path /api/auth) is sent without CORS.
      proxy: {
        '/api': 'http://localhost:8000',
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
  }
})
