import { Button, Message, Panel, Placeholder, Stack } from 'rsuite'
import { PageHeader } from '../components/PageHeader'
import { OwnerAddressCard } from '../components/settings/OwnerAddressCard'
import { SignInMethods } from '../components/settings/SignInMethods'
import { TelegramCard } from '../components/settings/TelegramCard'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { useMe } from '../hooks/useMe'
import { useSignOut } from '../hooks/useSignOut'

/** Account, wallet owner, Telegram, appearance and session. */
export function SettingsPage() {
  const { data: me, isPending, isError, refetch } = useMe()
  const signOut = useSignOut()

  let account
  if (isPending) {
    account = (
      <Panel bordered>
        <Placeholder.Paragraph rows={3} active />
      </Panel>
    )
  } else if (isError || !me) {
    account = (
      <Message type="error" showIcon>
        Couldn't load your account.{' '}
        <Button appearance="link" size="sm" onClick={() => void refetch()}>
          Try again
        </Button>
      </Message>
    )
  } else {
    account = (
      <>
        <SignInMethods me={me} />
        <OwnerAddressCard ownerAddr={me.owner_addr} />
        <TelegramCard me={me} />
      </>
    )
  }

  return (
    <>
      <PageHeader title="Settings" />
      <Stack direction="column" spacing={16} alignItems="stretch">
        {account}
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
