import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Message, Text } from 'rsuite'
import { disconnectApp, fetchConnectedApps } from '../../api/oauth'
import type { ConnectedApp, CreatedToken } from '../../api/types'
import { formatDate } from '../../lib/format'
import { errorText } from '../../lib/tx'
import { CopyButton } from '../CopyButton'
import { GlassPanel } from '../Glass'
import { QueryError } from '../QueryError'
import { AccessTokenForm, accessText, CreatedTokenNote } from './AccessTokenForm'
import { ControlRow } from './ControlRow'

const CONNECTED_APPS_KEY = ['connected-apps'] as const

/** When it was made and when it was last used, in a few words. */
function usedText(app: ConnectedApp) {
  if (app.kind === 'token') {
    const until = `Expires ${formatDate((app.expires_at ?? 0) * 1_000)}`
    const used = app.last_used_at ? `last used ${formatDate(app.last_used_at * 1_000)}` : 'not used yet'
    return `${accessText(app.access)} · ${until} · ${used}`
  }
  const connected = `Connected ${formatDate(app.created_at * 1_000)}`
  return app.last_used_at ? `${connected} · last used ${formatDate(app.last_used_at * 1_000)}` : `${connected} · not used yet`
}

/**
 * The AI apps connected to the account over MCP (Claude, Claude Code, ChatGPT), and the personal
 * access tokens the user made for their own scripts and agents. Each can read the account, and an
 * app or a "read and prepare" token can prepare transactions; none can send one, which only the
 * user's passkey does. Disconnecting an app or revoking a token ends its access on its very next call.
 *
 * The account's list, not one wallet's: it is the same on every network, and shown even before the
 * account has a wallet. No owner signature is involved, so it isn't locked like the panels around
 * it; making a token takes a passkey instead (AccessTokenForm).
 */
export function ConnectedAppsPanel() {
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<CreatedToken | null>(null)
  const apps = useQuery({ queryKey: CONNECTED_APPS_KEY, queryFn: ({ signal }) => fetchConnectedApps(signal) })
  const disconnect = useMutation({
    mutationFn: disconnectApp,
    onSettled: () => queryClient.invalidateQueries({ queryKey: CONNECTED_APPS_KEY }),
  })
  const address = `${window.location.origin}/mcp`

  const onCreated = (token: CreatedToken) => {
    setCreating(false)
    setCreated(token)
    void queryClient.invalidateQueries({ queryKey: CONNECTED_APPS_KEY })
  }

  return (
    <GlassPanel bordered header={<h2>Connected apps</h2>} className="mf-card">
      <Text muted size="sm">
        AI apps you allowed, and tokens you made for your own scripts and agents. They can see your wallets and, if you
        allow it, prepare transactions; only your passkey sends them.
      </Text>
      {apps.data && apps.data.length > 0 && (
        <ul className="mf-device-list" aria-label="Connected apps and tokens">
          {apps.data.map(app => (
            <li key={app.id}>
              <ControlRow
                title={app.app.name}
                help={app.kind === 'token' ? usedText(app) : `${app.app.site} · ${usedText(app)}`}
                action={
                  <Button
                    appearance="ghost"
                    size="sm"
                    aria-label={`${app.kind === 'token' ? 'Revoke' : 'Disconnect'} ${app.app.name}`}
                    loading={disconnect.isPending && disconnect.variables === app.id}
                    disabled={disconnect.isPending}
                    onClick={() => disconnect.mutate(app.id)}
                  >
                    {app.kind === 'token' ? 'Revoke' : 'Disconnect'}
                  </Button>
                }
              />
            </li>
          ))}
        </ul>
      )}
      {apps.data?.length === 0 && (
        <Text size="sm" className="mf-settings-note">
          No apps connected and no tokens.
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
      {created ? (
        <CreatedTokenNote token={created} address={address} onDone={() => setCreated(null)} />
      ) : creating ? (
        <AccessTokenForm onCreated={onCreated} onCancel={() => setCreating(false)} />
      ) : (
        <div className="mf-settings-note">
          <Text size="sm">
            For your own script or agent, or an app that can't sign in to Mitfah, make a token. It works at the same
            address.
          </Text>
          <Button appearance="ghost" size="sm" className="mf-tx-status" onClick={() => setCreating(true)}>
            Create a token
          </Button>
        </div>
      )}
    </GlassPanel>
  )
}
