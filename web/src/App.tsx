import { Button, Panel, SegmentedControl, Stack, Text } from 'rsuite'
import { useTheme } from './theme/useTheme'
import type { ThemeChoice } from './theme/themeStore'

const THEME_OPTIONS = [
  { label: 'System', value: 'system' },
  { label: 'Light', value: 'light' },
  { label: 'Dark', value: 'dark' },
]

/**
 * Placeholder that proves the theme and brand assets. Phase 1 replaces it with the router and
 * app shell.
 */
export default function App() {
  const { choice, setChoice, resolved } = useTheme()

  return (
    <main style={{ maxWidth: 560, margin: '0 auto', padding: '64px 24px' }}>
      <Stack spacing={12} alignItems="center">
        <img
          src={resolved === 'dark' ? '/brand/mark-dark.png' : '/brand/mark-light.png'}
          alt=""
          height={48}
        />
        <h1 style={{ margin: 0, fontSize: 36 }}>mitfah</h1>
      </Stack>
      <Text muted style={{ marginTop: 8 }}>
        An AI assistant for your crypto wallet that can only spend what you allow.
      </Text>

      <Panel bordered style={{ marginTop: 32 }} header="Appearance">
        <SegmentedControl
          data={THEME_OPTIONS}
          value={choice}
          onChange={value => setChoice(value as ThemeChoice)}
        />
        <Stack spacing={8} style={{ marginTop: 16 }}>
          <Button appearance="primary">Primary</Button>
          <Button>Default</Button>
          <Button color="red" appearance="primary">Pause wallet</Button>
        </Stack>
        <Text className="mf-mono" muted style={{ marginTop: 16 }}>
          0x9f3c…41c2 · ${(38.25).toFixed(2)} left of $50.00
        </Text>
      </Panel>
    </main>
  )
}
