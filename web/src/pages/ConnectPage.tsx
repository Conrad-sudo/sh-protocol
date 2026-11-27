import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { Button, Loader, Message, Text } from 'rsuite'
import { approveConnectRequest, denyConnectRequest, fetchConnectRequest } from '../api/oauth'
import { RequireAuth } from '../auth/RequireAuth'
import { useAuth } from '../auth/useAuth'
import { AddressText } from '../components/AddressText'
import { PageMeta } from '../components/PageMeta'
import { useMe } from '../hooks/useMe'
import { errorText } from '../lib/tx'

/** Why the server sent the browser here instead of to the Allow page (`/connect?error=…`). */
const PROBLEMS: Record<string, { title: string; body: string }> = {
  unknown_app: {
    title: "This app can't connect to Mitfah",
    body: 'Mitfah connects to Claude, Claude Code and ChatGPT. Add Mitfah from one of those apps.',
  },
  invalid_request: {
    title: 'That link is broken',
    body: 'Start connecting again from the app.',
  },
}

const NOTHING = {
  title: 'Nothing to connect',
  body: 'To connect an AI app, add Mitfah to it as a connector. The address to use is in Controls.',
}

/**
 * The Allow page: an AI app (Claude, Claude Code, ChatGPT) asks to use the account over MCP.
 *
 * The app opens /api/oauth/authorize in the browser; the server checks the app against its known list
 * and sends the browser here with `?request=<id>`, or with `?error=` when the app can't connect at
 * all. Signing in comes first (RequireAuth brings the user back here). Allow or Deny sends the browser
 * back to the app, carrying the answer. Only this browser can answer: the server tied the request to
 * it with a cookie when the app opened it, so a copied Allow link is refused anywhere else.
 *
 * That cookie can't show the user started the request: anyone can send them the app's own sign-in
 * link, which opens a fresh request here. Hence the warning above the buttons. What keeps such a
 * request from connecting the user to someone else's app account is the app's own `state` check
 * (THREAT_MODEL §4.12), and a connection can never send.
 */
export function ConnectPage() {
  const [params] = useSearchParams()
  const requestId = params.get('request')
  const error = params.get('error')
  return (
    <>
      <PageMeta
        title="Connect an app · Mitfah"
        description="Allow an AI app to use your Mitfah smart wallet. It can prepare transactions; only your passkey sends them."
        path="/connect"
      />
      {error || !requestId ? (
        <Problem problem={(error && PROBLEMS[error]) || NOTHING} />
      ) : (
        <RequireAuth>
          <Allow id={requestId} />
        </RequireAuth>
      )}
    </>
  )
}

function Problem({ problem }: { problem: { title: string; body: string } }) {
  return (
    <>
      <h1 className="mf-auth-title">{problem.title}</h1>
      <Text muted>{problem.body}</Text>
      <Text className="mf-auth-alt">
        <Link to="/dashboard">Go to your dashboard</Link>
      </Text>
    </>
  )
}

function Allow({ id }: { id: string }) {
  const { signOut } = useAuth()
  const me = useMe()
  const request = useQuery({
    queryKey: ['connect-request', id],
    queryFn: ({ signal }) => fetchConnectRequest(id, signal),
    retry: false,
    staleTime: Infinity,
  })
  const answer = useMutation({
    mutationFn: (allow: boolean) => (allow ? approveConnectRequest(id) : denyConnectRequest(id)),
    // Back to the app, with the code or the refusal. A full navigation: the app's page, not ours.
    onSuccess: ({ redirect_to }) => window.location.assign(redirect_to),
  })

  if (request.isPending) return <Loader className="mf-step-action" content="Loading the request…" />
  if (request.isError) {
    return (
      <>
        <h1 className="mf-auth-title">Can't connect</h1>
        <Message type="error" showIcon className="mf-settings-note">
          {errorText(request.error)}
        </Message>
      </>
    )
  }

  const { app } = request.data
  const leaving = answer.isSuccess
  const busy = answer.isPending || leaving
  const owner = me.data?.owner_addr

  return (
    <div className="mf-verify">
      <h1 className="mf-auth-title">Connect {app.name}?</h1>
      <Text>
        {app.name} ({app.site}) is asking to use your Mitfah account
        {owner ? (
          <>
            , <AddressText address={owner} />
          </>
        ) : null}
        . It will be able to:
      </Text>
      <ul className="mf-connect-list">
        <li>see your account, wallets and balances</li>
        <li>prepare payments, swaps and liquidity changes for you</li>
      </ul>
      <Message type="info" showIcon className="mf-settings-note">
        <strong>It can't send anything by itself.</strong> Every transaction waits for your passkey. Disconnect it in
        Controls any time.
      </Message>
      <Message type="warning" showIcon className="mf-settings-note">
        Only allow this if you just started it in {app.name}. If someone sent you this link, choose Deny.
      </Message>
      {answer.isError && (
        <Message type="error" showIcon className="mf-settings-note">
          {errorText(answer.error)}
        </Message>
      )}
      <Button
        appearance="primary"
        block
        size="lg"
        className="mf-step-action"
        loading={busy && answer.variables === true}
        disabled={busy}
        onClick={() => answer.mutate(true)}
      >
        Allow
      </Button>
      <Button
        appearance="ghost"
        block
        size="lg"
        className="mf-step-action"
        loading={busy && answer.variables === false}
        disabled={busy}
        onClick={() => answer.mutate(false)}
      >
        Deny
      </Button>
      {leaving ? (
        <Text className="mf-auth-alt" muted>
          Taking you back to {app.name}…
        </Text>
      ) : (
        <Text className="mf-auth-alt" muted>
          Not your account?{' '}
          <Button appearance="link" size="sm" disabled={busy} onClick={() => void signOut()}>
            Sign in with another one
          </Button>
          .
        </Text>
      )}
    </div>
  )
}
