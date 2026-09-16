import { IconButton, SegmentedControl } from 'rsuite'
import { useTheme } from '../theme/useTheme'
import type { ThemeChoice } from '../theme/themeStore'
import { MoonIcon, SunIcon } from './icons'

const OPTIONS = [
  { label: 'System', value: 'system' },
  { label: 'Light', value: 'light' },
  { label: 'Dark', value: 'dark' },
]

/** The full System / Light / Dark choice, for Settings. */
export function ThemeSwitch() {
  const { choice, setChoice } = useTheme()
  return (
    <SegmentedControl
      data={OPTIONS}
      value={choice}
      onChange={value => setChoice(value as ThemeChoice)}
      aria-label="Theme"
    />
  )
}

/** A one-tap light/dark flip, for the top bar. */
export function ThemeToggleButton() {
  const { resolved, setChoice } = useTheme()
  const next = resolved === 'dark' ? 'light' : 'dark'
  return (
    <IconButton
      appearance="subtle"
      circle
      icon={resolved === 'dark' ? <SunIcon /> : <MoonIcon />}
      aria-label={`Switch to ${next} theme`}
      onClick={() => setChoice(next)}
    />
  )
}
