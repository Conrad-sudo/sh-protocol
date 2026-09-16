import { useState, useSyncExternalStore, type ReactNode } from 'react'
import { CustomProvider } from 'rsuite'
import { ThemeContext } from './ThemeContext'
import {
  readChoice,
  resolveTheme,
  subscribeSystemTheme,
  systemPrefersDark,
  writeChoice,
  type ThemeChoice,
} from './themeStore'

/**
 * Light/dark theming: follows the OS unless the user picked one, and remembers the pick.
 *
 * RSuite's CustomProvider applies the theme by putting `rs-theme-<name>` on <body>. It does that in
 * an effect, after first paint — index.html sets the same class before React loads so a dark-mode
 * user never sees a light flash.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readChoice)
  const systemDark = useSyncExternalStore(subscribeSystemTheme, systemPrefersDark, () => false)
  const resolved = resolveTheme(choice, systemDark)

  const setChoice = (next: ThemeChoice) => {
    writeChoice(next)
    setChoiceState(next)
  }

  return (
    <ThemeContext value={{ choice, setChoice, resolved }}>
      <CustomProvider theme={resolved}>{children}</CustomProvider>
    </ThemeContext>
  )
}
