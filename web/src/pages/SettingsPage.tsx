import { Button, Panel, Placeholder, Stack, Text } from 'rsuite'
import { PageHeader } from '../components/PageHeader'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { useMe } from '../hooks/useMe'
import { useSignOut } from '../hooks/useSignOut'

/** Phase 1: account email, appearance, sign out. Phase 2 adds sign-in methods and Telegram. */
export function SettingsPage() {
  const { data: me, isPending } = useMe()
  const signOut = useSignOut()

  return (
    <>
      <PageHeader title="Settings" />
      <Stack direction="column" spacing={16} alignItems="stretch">
        <Panel bordered header="Account">
          {isPending ? (
            <Placeholder.Paragraph rows={1} active />
          ) : (
            <Text>
              Signed in as <strong>{me?.email ?? 'an account without an email'}</strong>
            </Text>
          )}
        </Panel>
        <Panel bordered header="Appearance">
          <ThemeSwitch />
        </Panel>
        <Panel bordered header="Session">
          <Button color="red" appearance="ghost" onClick={signOut}>
            Sign out
          </Button>
        </Panel>
      </Stack>
    </>
  )
}
