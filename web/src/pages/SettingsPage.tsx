import { Button, Placeholder, Stack } from 'rsuite'
import { GlassPanel } from '../components/Glass'
import { PageHeader } from '../components/PageHeader'
import { QueryError } from '../components/QueryError'
import { ChatHistoryCard } from '../components/settings/ChatHistoryCard'
import { OwnerAddressCard } from '../components/settings/OwnerAddressCard'
import { SignInMethods } from '../components/settings/SignInMethods'
import { TelegramCard } from '../components/settings/TelegramCard'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { useMe } from '../hooks/useMe'
import { useSignOut } from '../hooks/useSignOut'

/** Account, wallet owner, Telegram, chat history, appearance and session. */
export function SettingsPage() {
  const { data: me, isPending, isError, refetch } = useMe()
  const signOut = useSignOut()

  let account
  if (isPending) {
    account = (
      <GlassPanel bordered>
        <Placeholder.Paragraph rows={3} active />
      </GlassPanel>
    )
  } else if (isError || !me) {
    account = (
      <QueryError what="your account" onRetry={() => void refetch()} />
    )
  } else {
    account = (
      <>
        <SignInMethods me={me} />
        <OwnerAddressCard ownerAddr={me.owner_addr} />
        <TelegramCard me={me} />
        <ChatHistoryCard />
      </>
    )
  }

  return (
    <>
      <PageHeader title="Settings" />
      <Stack direction="column" spacing={16} alignItems="stretch">
        {account}
        <GlassPanel bordered header="Appearance">
          <ThemeSwitch />
        </GlassPanel>
        <GlassPanel bordered header="Session">
          <Button color="red" appearance="ghost" onClick={signOut}>
            Sign out
          </Button>
        </GlassPanel>
      </Stack>
    </>
  )
}
