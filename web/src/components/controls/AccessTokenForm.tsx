import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, Loader, Message, SelectPicker, Text } from 'rsuite'
import { createToken, fetchTokenOptions } from '../../api/oauth'
import type { ConnectionAccess, CreatedToken, PasskeyAssertionJSON, PasskeyPrompt, TokenDays } from '../../api/types'
import { describedBy } from '../../lib/fields'
import { formatDate } from '../../lib/format'
import { isPasskeyError, passkeysSupported, signWithPasskey } from '../../lib/passkey'
import { errorText } from '../../lib/tx'
import { CopyButton } from '../CopyButton'
import { FieldError, FieldLabel } from '../Explained'

export const TOKEN_OPTIONS_KEY = ['token-options'] as const

/**
 * How long fetched options are tapped with. The server keeps a challenge for 10 minutes; they are
 * fetched again well before, while the form waits.
 */
const OPTIONS_FRESH_MS = 50_000

export const TOKEN_NAME_MAX = 40

const ACCESS_CHOICES: { value: ConnectionAccess; label: string }[] = [
  { value: 'read', label: 'Read only' },
  { value: 'prepare', label: 'Read and prepare transactions' },
]
const DAY_CHOICES: { value: TokenDays; label: string }[] = [
  { value: 7, label: '7 days' },
  { value: 30, label: '30 days' },
  { value: 90, label: '90 days' },
]

/** What a token may do, in a few words, for its row and the note it is shown in. */
export function accessText(access: ConnectionAccess) {
  return access === 'read' ? 'Read only' : 'Read and prepare'
}

/** Why a name can't be used, or null when it can. The server allows 1-40 characters. */
function nameProblem(name: string): string | null {
  const trimmed = name.trim()
  if (trimmed === '') return 'Give the token a name, such as “Access token”.'
  if (trimmed.length > TOKEN_NAME_MAX) return `Use at most ${TOKEN_NAME_MAX} characters.`
  return null
}

/** Why making the token stopped, in plain words. A closed prompt is news, not an error. */
function failureText(error: Error): string {
  if (isPasskeyError(error, 'cancelled')) return 'Cancelled. No token was made.'
  if (isPasskeyError(error, 'no-passkey')) {
    return 'No token was made. If this device has no approved passkey, make the token on a device that has one, or add this one in Devices.'
  }
  if (isPasskeyError(error)) return error.message
  return `No token was made: ${errorText(error)}`
}

interface MakeVariables {
  /** The browser's prompt, opened by the tap (signWithPasskey). */
  pending: Promise<PasskeyAssertionJSON>
  name: string
  days: TokenDays
  access: ConnectionAccess
}

interface AccessTokenFormProps {
  onCreated: (token: CreatedToken) => void
  onCancel: () => void
}

/**
 * Makes a personal access token, for the user's own scripts and agents: a name, what it may do and
 * how long it lasts, confirmed with a passkey one of the wallets approves.
 *
 * The passkey prompt opens inside the tap on Create (Safari allows it nowhere else), so its options
 * are fetched as soon as the form opens and kept fresh while it waits. Each try uses its challenge
 * up, so a new one is fetched after every answer.
 */
export function AccessTokenForm({ onCreated, onCancel }: AccessTokenFormProps) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [access, setAccess] = useState<ConnectionAccess>('read')
  const [days, setDays] = useState<TokenDays>(30)
  const [tried, setTried] = useState(false)
  const options = useQuery({
    queryKey: TOKEN_OPTIONS_KEY,
    queryFn: ({ signal }) => fetchTokenOptions(signal),
    staleTime: OPTIONS_FRESH_MS,
    refetchInterval: OPTIONS_FRESH_MS,
    gcTime: 0,
    retry: false,
  })
  const make = useMutation({
    mutationFn: async ({ pending, ...body }: MakeVariables) => createToken({ ...body, credential: await pending }),
    // The prompt is already open, so its answer must reach the server whatever the browser thinks of
    // its connection.
    networkMode: 'always',
    onSuccess: onCreated,
    onSettled: () => void queryClient.invalidateQueries({ queryKey: TOKEN_OPTIONS_KEY }),
  })
  const problem = nameProblem(name)
  const supported = passkeysSupported()

  /** The fetched options, if they are there and fresh enough to tap with now. */
  const freshOptions = () => {
    const state = queryClient.getQueryState<PasskeyPrompt>(TOKEN_OPTIONS_KEY)
    if (!state?.data || state.fetchStatus === 'fetching') return null
    return Date.now() - state.dataUpdatedAt < OPTIONS_FRESH_MS ? state.data : null
  }

  const create = () => {
    setTried(true)
    if (problem || make.isPending) return
    const prompt = freshOptions()
    if (!prompt) {
      void options.refetch()
      return
    }
    make.reset()
    make.mutate({ pending: signWithPasskey(prompt), name: name.trim(), days, access })
  }

  const blocked = !supported || options.isError
  return (
    <Form fluid className="mf-control-form" onSubmit={create}>
      <Form.Group controlId="token-name">
        <FieldLabel help="So you can tell your tokens apart.">Name</FieldLabel>
        <Input
          id="token-name"
          value={name}
          maxLength={TOKEN_NAME_MAX}
          placeholder="Access token"
          autoComplete="off"
          disabled={make.isPending}
          onChange={setName}
          aria-describedby={describedBy('token-name', tried && problem)}
          aria-invalid={tried && !!problem}
        />
        {tried && problem && <FieldError>{problem}</FieldError>}
      </Form.Group>
      <Form.Group controlId="token-access">
        <FieldLabel
          help={
            access === 'read'
              ? 'Sees your wallets, balances, prices and contacts.'
              : 'Also prepares transactions. Only your passkey sends them, except for a trading agent (below), which signs with its own key.'
          }
        >
          What it can do
        </FieldLabel>
        <SelectPicker
          id="token-access"
          aria-labelledby="token-access-label"
          block
          cleanable={false}
          searchable={false}
          data={ACCESS_CHOICES}
          value={access}
          onChange={value => value && setAccess(value)}
          disabled={make.isPending}
        />
      </Form.Group>
      <Form.Group controlId="token-days">
        <Form.Label>Lasts</Form.Label>
        <SelectPicker
          id="token-days"
          aria-labelledby="token-days-label"
          block
          cleanable={false}
          searchable={false}
          data={DAY_CHOICES}
          value={days}
          onChange={value => value && setDays(value)}
          disabled={make.isPending}
        />
      </Form.Group>
      <div className="mf-inline-field">
        <Button appearance="primary" size="sm" loading={make.isPending} disabled={blocked} onClick={create}>
          Create with passkey
        </Button>
        <Button appearance="subtle" size="sm" disabled={make.isPending} onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {!supported && (
        <Message type="info" showIcon className="mf-tx-status">
          This browser can't use passkeys, so it can't make a token. Use a phone or computer that can.
        </Message>
      )}
      {supported && options.isError && (
        <Message type="info" showIcon className="mf-tx-status">
          {errorText(options.error)}
        </Message>
      )}
      {tried && !problem && !blocked && !make.isPending && !make.isError && options.isFetching && (
        <Loader className="mf-tx-status" content="Getting ready…" />
      )}
      {make.isPending && <Loader className="mf-tx-status" content="Follow your browser to confirm with your passkey…" />}
      {make.isError && (
        <Message type={isPasskeyError(make.error, 'cancelled') ? 'info' : 'error'} showIcon className="mf-tx-status">
          {failureText(make.error)}
        </Message>
      )}
    </Form>
  )
}

/**
 * A token just made, this once, with how to use it: hidden, only its copy button holds it. Mitfah
 * keeps only its hash, so after Done it can't be copied again.
 */
export function CreatedTokenNote({ token, address, onDone }: { token: CreatedToken; address: string; onDone: () => void }) {
  return (
    <Message type="success" showIcon className="mf-settings-note">
      <Text>“{token.name}” is ready. Copy it now: Mitfah won't show it again.</Text>
      <div className="mf-connect-url">
        {/* Never on the page itself: dots, and the copy button holds the token. */}
        <code aria-label="Token, hidden">{'•'.repeat(32)}</code>
        <CopyButton value={token.token} label="Token" />
      </div>
      <Text size="sm">
        Your agent sends it to <code>{address}</code> as <code>Authorization: Bearer</code> followed by the token.{' '}
        {accessText(token.access)}, until {formatDate(token.expires_at * 1_000)}.
      </Text>
      <Button appearance="primary" size="sm" onClick={onDone}>
        Done
      </Button>
    </Message>
  )
}
