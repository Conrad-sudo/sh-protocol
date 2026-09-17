import { useMutation, useMutationState, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../api/client'
import { fetchChatHistory, sendChat } from '../api/chat'
import type { ChatMessage } from '../api/types'

export const chatHistoryKey = (chainId: number) => ['chat-history', chainId] as const
const sendKey = (chainId: number) => ['chat', 'send', chainId] as const

interface SendVariables {
  chainId: number
  text: string
}

/** The latest message sent on a network, while it waits for a reply or after it failed. */
export interface ChatSend {
  id: number
  status: 'pending' | 'error' | 'success' | 'idle'
  text: string
  error: unknown
  submittedAt: number
}

/**
 * True when a failed send may still have reached the assistant: the connection dropped or the
 * server broke after the request left. A 4xx means the server refused it before the assistant ran,
 * so sending it again can't repeat anything.
 */
export function mayHaveRun(error: unknown) {
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500
}

/** The conversation on `chainId`, oldest first. */
export function useChatHistory(chainId: number) {
  return useQuery({
    queryKey: chatHistoryKey(chainId),
    queryFn: () => fetchChatHistory(chainId),
    staleTime: 30_000,
  })
}

/**
 * Sending on one network. Only one message at a time: two turns on the same conversation would
 * overwrite each other's saved history on the server. The send lives in the query client, not the
 * page, so leaving the page and coming back still shows it waiting and still blocks a second one.
 */
export function useChatSend(chainId: number) {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationKey: sendKey(chainId),
    mutationFn: ({ chainId: chain, text }: SendVariables) => sendChat(chain, text),
    // Runs even if the page was left while waiting, so the reply is there on return.
    onSuccess: async (reply, { chainId: chain, text }) => {
      const key = chatHistoryKey(chain)
      await queryClient.cancelQueries({ queryKey: key })
      queryClient.setQueryData<ChatMessage[]>(key, (old = []) => {
        // A refetch while waiting may already hold the message: the server saves it when the turn starts.
        const last = old.at(-1)
        const sent = last?.role === 'user' && last.text === text ? [] : [{ role: 'user' as const, text }]
        return [...old, ...sent, { role: 'assistant', text: reply }]
      })
      // The server's copy also has any messages the assistant wrote along the way.
      void queryClient.invalidateQueries({ queryKey: key })
      // A payment changes the balances and what is left to spend.
      void queryClient.invalidateQueries({ queryKey: ['wallet', chain] })
    },
  })

  const sends = useMutationState({
    filters: { mutationKey: sendKey(chainId) },
    select: (m): ChatSend => ({
      id: m.mutationId,
      status: m.state.status,
      text: (m.state.variables as SendVariables | undefined)?.text ?? '',
      error: m.state.error,
      submittedAt: m.state.submittedAt,
    }),
  })
  const latest = sends.at(-1) ?? null

  /** Forgets a failed send, so it no longer shows. */
  const dismiss = (id: number) => {
    const cache = queryClient.getMutationCache()
    const found = cache.getAll().find(m => m.mutationId === id)
    if (found) cache.remove(found)
  }

  return {
    latest,
    busy: latest?.status === 'pending',
    send: (text: string) => mutation.mutate({ chainId, text }),
    dismiss,
  }
}
