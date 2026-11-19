import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Message, Text } from 'rsuite'
import { disconnectApp, fetchConnectedApps } from '../../api/oauth'
import type { ConnectedApp } from '../../api/types'
import { formatDate } from '../../lib/format'
import { errorText } from '../../lib/tx'
import { CopyButton } from '../CopyButton'
import { GlassPanel } from '../Glass'
import { QueryError } from '../QueryError'
import { ControlRow } from './ControlRow'

const CONNECTED_APPS_KEY = ['connected-apps'] as const

/** When an app connected and when it last used the connection, in one line. */
function usedText(app: ConnectedApp) {
  const connected = `Connected ${formatDate(app.created_at * 1_000)}`
  return app.last_used_at ? `${connected} · last used ${formatDate(app.last_used_at * 1_000)}` : `${connected} · not used yet`
}

/**
 * The AI apps connected to the account over MCP (Claude, Claude Code, ChatGPT). Each can read the
 * account and prepare transactions; none can send one, which only the user's passkey does.
 * Disconnecting ends an app's access on its very next call.
 *
 * The account's list, not one wallet's: it is the same on every network, and shown even before the
 * account has a wallet. No owner signature is involved, so it isn't locked like the panels around it.
 */
export function ConnectedAppsPanel() {
  const queryClient = useQueryClient()
  const apps = useQuery({ queryKey: CONNECTED_APPS_KEY, queryFn: ({ signal }) => fetchConnectedApps(signal) })
  const disconnect = useMutation({
    mutationFn: disconnectApp,
    onSettled: () => queryClient.invalidateQueries({ queryKey: CONNECTED_APPS_KEY }),
  })
  const address = `${window.location.origin}/mcp`

  return (
    <GlassPanel bordered header={<h2>Connected apps</h2>} className="mf-card">
      <Text muted size="sm">
        AI apps you allowed to use your account. They can see your wallets and prepare transactions; only your passkey
        sends them.
      </Text>
      {apps.data && apps.data.length > 0 && (
        <ul className="mf-device-list" aria-label="Connected apps">
          {apps.data.map(app => (
            <li key={app.id}>
              <ControlRow
                title={app.app.name}
                help={`${app.app.site} · ${usedText(app)}`}
                action={
                  <Button
                    appearance="ghost"
                    size="sm"
                    loading={disconnect.isPending && disconnect.variables === app.id}
                    disabled={disconnect.isPending}
                    onClick={() => disconnect.mutate(app.id)}
                  >
                    Disconnect
                  </Button>
                }
              />
            </li>
          ))}
        </ul>
      )}
      {apps.data?.length === 0 && (
        <Text size="sm" className="mf-settings-note">
          No apps connected.
        </Text>
      )}
      {apps.isError && (
        <QueryError what="your connected apps" error={apps.error} onRetry={() => void apps.refetch()} className="mf-settings-note" />
      )}
      {disconnect.isError && (
        <Message type="error" showIcon className="mf-settings-note">
          {errorText(disconnect.error)}
        </Message>
      )}
      <div className="mf-settings-note">
        <Text size="sm">To connect an app, add Mitfah to it as a custom connector with this address:</Text>
        <div className="mf-connect-url">
          <code>{address}</code>
          <CopyButton value={address} label="Address" />
        </div>
      </div>
    </GlassPanel>
  )
}
