/**
 * Theme choice storage and the system-preference subscription, shared by ThemeProvider and the
 * pre-paint script in index.html (which repeats STORAGE_KEY and the resolution rule by hand, since
 * it runs before any module loads — keep the two in step).
 */

export type ThemeChoice = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

export const STORAGE_KEY = 'mitfah-theme'

const DARK_QUERY = '(prefers-color-scheme: dark)'

export function readChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === 'light' || stored === 'dark' ? stored : 'system'
  } catch {
    return 'system'
  }
}

export function writeChoice(choice: ThemeChoice) {
  try {
    if (choice === 'system') localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, choice)
  } catch {
    // Storage blocked (private mode, site data off): the choice lasts for this page only.
  }
}

export function subscribeSystemTheme(onChange: () => void) {
  const query = window.matchMedia(DARK_QUERY)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

export function systemPrefersDark() {
  return window.matchMedia(DARK_QUERY).matches
}

export function resolveTheme(choice: ThemeChoice, systemDark: boolean): ResolvedTheme {
  if (choice === 'system') return systemDark ? 'dark' : 'light'
  return choice
}
