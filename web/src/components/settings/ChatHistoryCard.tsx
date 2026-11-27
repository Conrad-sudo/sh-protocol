import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Message, Text, useToaster } from 'rsuite'
import { deleteChatHistory } from '../../api/chat'
import { ApiError } from '../../api/client'
import type { ChatMessage } from '../../api/types'
import { errorText } from '../../lib/tx'
import { Explained } from '../Explained'
import { GlassPanel } from '../Glass'
import { ConfirmModal } from '../owner/ConfirmModal'

/** Deletes the conversation on every network at once. The transaction history is kept. */
export function ChatHistoryCard() {
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [open, setOpen] = useState(false)

  const notify = (type: 'success' | 'error', text: string) =>
    toaster.push(
      <Message type={type} showIcon closable>
        {text}
      </Message>,
      { placement: 'topCenter', duration: type === 'error' ? 6000 : 3000 },
    )

  const clear = useMutation({
    mutationFn: () => deleteChatHistory(),
    onSuccess: () => {
      queryClient.setQueriesData<ChatMessage[]>({ queryKey: ['chat-history'] }, [])
      notify('success', 'All chat history deleted.')
    },
    onError: error =>
      notify(
        'error',
        error instanceof ApiError && error.status === 409
          ? 'Mitfah is still answering a message. Try again once it has replied.'
          : `Couldn't delete your chat history: ${errorText(error)}`,
      ),
  })

  return (
    <GlassPanel bordered header="Chat history">
      <div className="mf-settings-row">
        <div className="mf-settings-row-label">
          <Explained level="label" title="Conversations with Mitfah">
            Kept per network and cleared after each transaction. Deleting includes Telegram; your transaction history
            stays.
          </Explained>
        </div>
        <div className="mf-settings-row-action">
          <Button color="red" appearance="ghost" loading={clear.isPending} onClick={() => setOpen(true)}>
            Delete all
          </Button>
        </div>
      </div>
      <ConfirmModal
        open={open}
        title="Delete all chat history?"
        confirmLabel="Delete all"
        tone="danger"
        onConfirm={() => clear.mutate()}
        onClose={() => setOpen(false)}
      >
        <Text>
          Mitfah forgets your conversations on every network, Telegram included. This can't be undone. Your wallets,
          contacts, limits and history stay.
        </Text>
      </ConfirmModal>
    </GlassPanel>
  )
}
