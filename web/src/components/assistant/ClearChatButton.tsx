import { useState } from 'react'
import TrashIcon from '@rsuite/icons/Trash'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Message, Text, useToaster } from 'rsuite'
import { deleteChatHistory } from '../../api/chat'
import { ApiError } from '../../api/client'
import type { ChatMessage } from '../../api/types'
import { chatHistoryKey, useChatHistory, useChatSend } from '../../hooks/useChat'
import { errorText } from '../../lib/tx'
import { chainName } from '../../wallet/chains'
import { ConfirmModal } from '../owner/ConfirmModal'

/**
 * Deletes the conversation on this network, after asking. The assistant forgets it everywhere,
 * Telegram included; the transaction history stays.
 */
export function ClearChatButton({ chainId }: { chainId: number }) {
  const history = useChatHistory(chainId)
  const { busy } = useChatSend(chainId)
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
    mutationFn: () => deleteChatHistory(chainId),
    onSuccess: () => {
      queryClient.setQueryData<ChatMessage[]>(chatHistoryKey(chainId), [])
      notify('success', 'Chat cleared.')
    },
    onError: error =>
      notify(
        'error',
        error instanceof ApiError && error.status === 409
          ? 'Mitfah is still answering your last message. Try again once it has replied.'
          : `Couldn't clear the chat: ${errorText(error)}`,
      ),
  })

  return (
    <>
      <Button
        appearance="subtle"
        size="sm"
        startIcon={<TrashIcon />}
        disabled={!history.data?.length || busy}
        loading={clear.isPending}
        onClick={() => setOpen(true)}
      >
        Clear chat
      </Button>
      <ConfirmModal
        open={open}
        title="Clear this chat?"
        confirmLabel="Clear chat"
        tone="danger"
        onConfirm={() => clear.mutate()}
        onClose={() => setOpen(false)}
      >
        <Text>
          Mitfah will forget this conversation on {chainName(chainId)}, including anything said on Telegram. Your
          wallet, contacts, limits and transaction history stay as they are.
        </Text>
      </ConfirmModal>
    </>
  )
}
