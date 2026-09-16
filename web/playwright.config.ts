import { defineConfig, devices } from '@playwright/test'

// Only Chromium is installed (`npx playwright install chromium`), so the iPhone profile keeps its
// size, touch and user agent but runs in Chromium.
const screens = {
  desktop: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
  tablet: { viewport: { width: 820, height: 1180 }, hasTouch: true },
  phone: { ...devices['iPhone 13'], browserName: 'chromium' as const },
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'retain-on-failure',
  },
  // Every screen size in both themes.
  projects: Object.entries(screens).flatMap(([name, use]) =>
    (['light', 'dark'] as const).map(colorScheme => ({
      name: `${name}-${colorScheme}`,
      use: { ...use, colorScheme },
    })),
  ),
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: true,
    timeout: 60_000,
  },
})
