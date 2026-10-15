import { useEffect } from 'react'
import { useLocation } from 'react-router'
import { Button, Placeholder, Stack } from 'rsuite'
import { GlassPanel } from '../components/Glass'
import { PageHeader } from '../components/PageHeader'
import { QueryError } from '../components/QueryError'
import { ChatHistoryCard } from '../components/settings/ChatHistoryCard'
import { OwnerAddressCard } from '../components/settings/OwnerAddressCard'
import { TelegramCard } from '../components/settings/TelegramCard'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { useMe } from '../hooks/useMe'
import { useSignOut } from '../hooks/useSignOut'

/** The address you sign in as, Telegram, chat history, appearance and session. */
export function SettingsPage() {
  const { data: me, isPending, isError, refetch } = useMe()
  const signOut = useSignOut()
  const { hash } = useLocation()
  const loaded = me !== undefined

  // Opened at a card (/settings#telegram, from the dashboard): go to it once the account's cards are
  // in. Once only: the account is read again while a Telegram link is pending, which mustn't pull
  // the page back.
  useEffect(() => {
    if (loaded && hash) document.getElementById(hash.slice(1))?.scrollIntoView({ block: 'start' })
  }, [loaded, hash])

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
