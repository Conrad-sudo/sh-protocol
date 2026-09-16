import { createContext } from 'react'
import type { ResolvedTheme, ThemeChoice } from './themeStore'

export interface ThemeState {
  /** What the user picked; 'system' follows the OS. */
  choice: ThemeChoice
  setChoice: (choice: ThemeChoice) => void
  /** What is actually showing. */
  resolved: ResolvedTheme
}

export const ThemeContext = createContext<ThemeState | null>(null)
