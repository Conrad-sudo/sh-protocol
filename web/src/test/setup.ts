import '@testing-library/jest-dom/vitest'

// jsdom has no matchMedia. Tests that need a dark system preference set `prefersDark` first.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: query.includes('dark') && (window as { prefersDark?: boolean }).prefersDark === true,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }),
})
