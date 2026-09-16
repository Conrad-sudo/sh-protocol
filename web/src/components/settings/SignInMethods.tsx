import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader, Message, Panel, Text, useToaster } from 'rsuite'
import { linkGoogle } from '../../api/auth'
import { ApiError } from '../../api/client'
import type { Me } from '../../api/types'
import { googleEnabled } from '../../auth/google'
import { GoogleButton } from '../GoogleButton'
import { StatusTag } from '../StatusTag'

const INCOMPLETE = "Linking Google didn't complete. Please try again."

/** How this account signs in, and linking Google to it. */
export function SignInMethods({ me }: { me: Me }) {
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [popupFailed, setPopupFailed] = useState(false)

  const link = useMutation({
    mutationFn: linkGoogle,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['me'] })
      toaster.push(
        <Message type="success" showIcon closable>
          Google account linked. You can now sign in with Google.
        </Message>,
        { placement: 'topCenter', duration: 3000 },
      )
    },
  })

  let error: string | null = null
  if (popupFailed) error = INCOMPLETE
  else if (link.error) {
    const e = link.error
    error = e instanceof ApiError && e.status !== 400 ? e.message : INCOMPLETE
  }

  let googleAction
  if (me.google_linked) googleAction = <StatusTag tone="success">Linked</StatusTag>
  else if (!googleEnabled) googleAction = <Text muted size="sm">Not available yet</Text>
  else if (link.isPending) googleAction = <Loader content="Linking…" />
  else
    googleAction = (
      <GoogleButton
        text="continue_with"
        onCredential={token => {
          setPopupFailed(false)
          link.mutate(token)
        }}
        onFailure={() => {
          link.reset()
          setPopupFailed(true)
        }}
      />
    )

  return (
    <Panel bordered header="Sign-in methods">
      <div className="mf-settings-row">
        <div className="mf-settings-row-label">
          <Text weight="medium">Email and password</Text>
          <Text muted size="sm">{me.email ?? 'No email on this account'}</Text>
        </div>
        <div className="mf-settings-row-action">
          {me.has_password ? <StatusTag tone="success">Set</StatusTag> : <StatusTag>Not set</StatusTag>}
        </div>
      </div>

      <div className="mf-settings-row">
        <div className="mf-settings-row-label">
          <Text weight="medium">Google</Text>
          <Text muted size="sm">
            {me.google_linked ? 'You can sign in with your Google account.' : 'Link Google to sign in with one tap.'}
          </Text>
        </div>
        <div className="mf-settings-row-action">{googleAction}</div>
      </div>

      {error && (
        <Message type="error" showIcon className="mf-settings-note">
          {error}
        </Message>
      )}
    </Panel>
  )
}
