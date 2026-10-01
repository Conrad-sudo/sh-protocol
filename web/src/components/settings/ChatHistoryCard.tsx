import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Message, Text, useToaster } from 'rsuite'
import { deleteChatHistory } from '../../api/chat'
import { ApiError } from '../../api/client'
import type { ChatMessage } from '../../api/types'
import { errorText } from '../../lib/tx'
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
          <Text weight="medium">Conversations with Mitfah</Text>
          <Text muted size="sm">
            Kept per network so Mitfah can follow along, and cleared after each transaction. Deleting includes what
            you said on Telegram. Your transaction history is kept.
          </Text>
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
          Mitfah will forget your conversations on every network, including anything said on Telegram. This can't be
          undone. Your wallets, contacts, limits and transaction history stay as they are.
        </Text>
      </ConfirmModal>
    </GlassPanel>
  )
}
