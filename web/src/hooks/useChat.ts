import { useIsMutating, useMutation, useMutationState, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { ApiError } from '../api/client'
import { fetchChatHistory, fetchQuoteOffer, sendChat } from '../api/chat'
import { cancelSign, finishSign, startSign } from '../api/sign'
import { fetchTransaction } from '../api/transactions'
import type { ChatMessage, PasskeyAssertionJSON, QuoteOffer, SignStart } from '../api/types'
import { isPasskeyError, PasskeyError, signWithPasskey } from '../lib/passkey'
import { errorText } from '../lib/tx'
import { TRANSACTIONS_KEY } from './useTransactions'

export const chatHistoryKey = (chainId: number) => ['chat-history', chainId] as const
export const quoteOfferKey = (chainId: number) => ['chat-quote', chainId] as const
export const landingKey = (chainId: number) => ['chat-landing', chainId] as const
const signStartKey = (chainId: number, quoteId: string) => ['sign-start', chainId, quoteId] as const
const sendKey = (chainId: number) => ['chat', 'send', chainId] as const
const answerKey = (chainId: number) => ['chat', 'answer', chainId] as const

/** What the server records as the user's message for a tap, as Telegram's buttons are labelled. */
export const CONFIRM_LABEL = '✅ Confirm'
export const CANCEL_LABEL = '✖️ Cancel'

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
 * so sending it again can't repeat anything; nor can a passkey prompt that gave no signature.
 */
export function mayHaveRun(error: unknown) {
  if (isPasskeyError(error) || error instanceof NothingSentError) return false
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
    // A send that has landed has said all it had to; one still on its way stays in view.
    onMutate: ({ chainId: chain }) =>
      queryClient.setQueryData<Landing | null>(landingKey(chain), old => (old?.settled ? null : old)),
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
      // The turn may have quoted a transaction to confirm with a tap.
      void queryClient.invalidateQueries({ queryKey: quoteOfferKey(chain) })
      // A payment changes the balances and what is left to spend.
      void queryClient.invalidateQueries({ queryKey: ['wallet', chain] })
    },
  })
  // A tap on a quote card is a turn too: the server runs one at a time.
  const answering = useIsMutating({ mutationKey: answerKey(chainId) }) > 0

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
    busy: latest?.status === 'pending' || answering,
    send: (text: string) => mutation.mutate({ chainId, text }),
    dismiss,
  }
}

/** A Confirm that stopped before anything was signed: its confirmation couldn't be started. */
class NothingSentError extends Error {}

/** The quote waiting for a Confirm or Cancel tap on `chainId`, or null. */
export function useQuoteOffer(chainId: number) {
  return useQuery({
    queryKey: quoteOfferKey(chainId),
    queryFn: () => fetchQuoteOffer(chainId),
  })
}

/**
 * How long a started confirmation is tapped with. Older, its fees may be stale, so the card has it
 * built again in the background (SIGN_REFRESH_MS) and a tap uses the new one.
 */
export const SIGN_FRESH_MS = 60_000
const SIGN_REFRESH_MS = 50_000

/**
 * The confirmation behind a quote card, started as soon as the card shows (POST /api/sign/start):
 * the final transaction built, and the challenge its passkey signs. Having it before the tap is what
 * lets the tap open the passkey prompt at once, which Safari requires. Built again while it waits,
 * never while a Confirm is under way: starting again stops the older one working.
 *
 * @param quote   The quote on the card, or null when there is none to confirm.
 * @param paused  A Confirm or Cancel is under way.
 */
export function useSignStart(quote: QuoteOffer | null, paused: boolean) {
  return useQuery({
    queryKey: signStartKey(quote?.chain_id ?? 0, quote?.quote_id ?? ''),
    queryFn: ({ signal }) => startSign({ chain_id: quote!.chain_id, quote_id: quote!.quote_id }, signal),
    enabled: quote !== null && !paused,
    staleTime: SIGN_REFRESH_MS,
    refetchInterval: paused ? false : SIGN_REFRESH_MS,
    gcTime: 0,
    retry: false,
  })
}

/** A started confirmation, if it is fresh enough for a tap to sign right now. */
export function freshSignStart(start: { data?: SignStart; dataUpdatedAt: number; isFetching: boolean }) {
  return start.data && !start.isFetching && Date.now() - start.dataUpdatedAt < SIGN_FRESH_MS ? start.data : null
}

type AnswerVariables =
  | { quote: QuoteOffer; confirm: false }
  | {
      quote: QuoteOffer
      confirm: true
      /** The confirmation the tap signed, or null when none was fresh: one is then started first. */
      start: SignStart | null
      /** The passkey prompt the tap opened (signWithPasskey), or null along with `start`. */
      assertion: Promise<PasskeyAssertionJSON> | null
      /** Called once the passkey has signed, while the transaction goes out. */
      onApproved?: () => void
    }

/**
 * A quote card's Confirm and Cancel. Confirm sends the quote once a passkey on this device signs
 * it: the tap opens the prompt, then POST /api/sign/finish sends it. Like a message, it lives in the
 * query client, so leaving the page while a confirm waits for the network still blocks a second
 * message, and its outcome is in the chat on return. It runs whatever the browser thinks of its
 * connection: the prompt is already open, so its answer must reach the server.
 */
export function useQuoteAnswer(chainId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: answerKey(chainId),
    networkMode: 'always',
    mutationFn: async (variables: AnswerVariables) => {
      const { quote } = variables
      const request = { chain_id: quote.chain_id, quote_id: quote.quote_id }
      if (!variables.confirm) {
        const { outcome } = await cancelSign(request)
        return { outcome, transactionId: null }
      }
      let { start, assertion } = variables
      if (!start || !assertion) {
        // No fresh confirmation at the tap: one is started now, after which a browser may refuse to
        // open the prompt outside the tap. The next tap then has a fresh one.
        start = await queryClient
          .fetchQuery({ queryKey: signStartKey(quote.chain_id, quote.quote_id), queryFn: () => startSign(request), staleTime: 0 })
          .catch((error: unknown) => {
            // An expired quote says so itself; anything else stopped before the passkey was asked.
            if (error instanceof ApiError && error.status === 404) throw error
            if (error instanceof ApiError && error.status === 409) {
              throw new NothingSentError(
                'No device you added can approve payments from this wallet yet. Add this device in Controls.',
              )
            }
            throw new NothingSentError(`Nothing was sent: ${errorText(error)}`)
          })
        assertion = signWithPasskey(start).catch((error: unknown) => {
          if (isPasskeyError(error, 'cancelled')) {
            throw new PasskeyError('cancelled', 'Your browser needs one more tap. Tap Confirm again.')
          }
          throw error
        })
      }
      const credential = await assertion
      variables.onApproved?.()
      const sent = await finishSign({ sign_id: start.sign_id, credential })
      return { outcome: sent.outcome, transactionId: sent.transaction_id ?? null }
    },
    onSuccess: async ({ outcome, transactionId }, { quote, confirm }) => {
      const key = chatHistoryKey(quote.chain_id)
      await queryClient.cancelQueries({ queryKey: key })
      // What the server recorded: the button pressed, then what happened.
      queryClient.setQueryData<ChatMessage[]>(key, (old = []) => [
        ...old,
        { role: 'user', text: confirm ? CONFIRM_LABEL : CANCEL_LABEL, button: true },
        { role: 'assistant', text: outcome, button: true },
      ])
      queryClient.setQueryData(quoteOfferKey(quote.chain_id), null)
      // On its way: followed until it lands (useLanding).
      if (confirm && transactionId !== null) {
        queryClient.setQueryData<Landing>(landingKey(quote.chain_id), {
          transactionId,
          action: quote.action,
          startedAt: Date.now(),
          settled: false,
        })
      }
      refresh(quote.chain_id)
    },
    // A closed passkey prompt changed nothing, and its confirmation is still good for another tap. A
    // refused tap (4xx) changed nothing either, but its quote may be gone and its confirmation is
    // spent: both are read again. A send refused because it would fail on chain (422 from
    // /api/sign/finish) is in the chat by then, and its quote closed. One that may have run may have
    // sent, so everything it could have changed is read again.
    onError: (error, { quote }) => {
      if (isPasskeyError(error)) return
      if (mayHaveRun(error) || (error instanceof ApiError && error.status === 422)) refresh(quote.chain_id)
      else invalidateOffer(quote.chain_id)
    },
  })

  function invalidateOffer(chain: number) {
    void queryClient.invalidateQueries({ queryKey: quoteOfferKey(chain) })
    void queryClient.invalidateQueries({ queryKey: ['sign-start', chain] })
  }

  function refresh(chain: number) {
    invalidateOffer(chain)
    void queryClient.invalidateQueries({ queryKey: chatHistoryKey(chain) })
    void queryClient.invalidateQueries({ queryKey: ['wallet', chain] })
    void queryClient.invalidateQueries({ queryKey: TRANSACTIONS_KEY })
  }
}

/** How often a confirmed send's History row is read while it waits to land. */
const LANDING_POLL_MS = 2_000
/** How long to keep reading it before leaving it to the History tab: past every fee bump the server tries. */
export const LANDING_GIVE_UP_MS = 10 * 60_000

/** A send a quote card's Confirm put on the wire, followed until it lands. */
export interface Landing {
  /** Its History row. */
  transactionId: number
  /** What it does, from the quote. */
  action: string
  /** When the tap was answered (ms). */
  startedAt: number
  /** It has landed, one way or another, and what it changed has been read again. */
  settled: boolean
}

/**
 * The send confirmed last on `chainId`, if any, and its History row, read every few seconds until it
 * is no longer pending (or LANDING_GIVE_UP_MS have passed). Once it lands, the wallet and the History
 * tab are read again. Kept in the query client, so leaving the page and coming back still shows it.
 */
export function useLanding(chainId: number) {
  const queryClient = useQueryClient()
  const { data: landing } = useQuery<Landing | null>({
    queryKey: landingKey(chainId),
    queryFn: () => null,
    initialData: null,
    staleTime: Infinity,
  })
  const row = useQuery({
    queryKey: ['transaction', landing?.transactionId],
    queryFn: () => fetchTransaction(landing!.transactionId),
    enabled: landing != null,
    refetchInterval: query => {
      const status = query.state.data?.status
      if (!landing || (status !== undefined && status !== 'pending')) return false
      return Date.now() - landing.startedAt < LANDING_GIVE_UP_MS ? LANDING_POLL_MS : false
    },
  })
  const landed = row.data !== undefined && row.data.status !== 'pending'

  useEffect(() => {
    if (!landed || !landing || landing.settled) return
    queryClient.setQueryData<Landing>(landingKey(chainId), { ...landing, settled: true })
    void queryClient.invalidateQueries({ queryKey: ['wallet', chainId] })
    void queryClient.invalidateQueries({ queryKey: TRANSACTIONS_KEY })
  }, [landed, landing, chainId, queryClient])

  return { landing, transaction: row.data, error: row.error }
}
