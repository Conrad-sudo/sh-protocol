import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] })
  ],
  server: {
    // 3000 is what the API allows by default (CORS_ORIGINS) and what SIWE_DOMAIN defaults to.
    // strictPort: silently moving to 3001 would make every wallet-binding message name the
    // wrong site and be refused.
    port: 3000,
    strictPort: true,
    // Same origin as the page, so the refresh cookie (path /api/auth/refresh) is sent without CORS.
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  test: {
    // e2e/ holds Playwright specs, which Vitest must not try to run.
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
