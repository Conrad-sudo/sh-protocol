import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Message, useToaster } from 'rsuite'
import { cancelSign, finishSign, startSign } from '../api/sign'
import { fetchTransaction } from '../api/transactions'
import type { Agent, PasskeyAssertionJSON, SignStart } from '../api/types'
import { stopAgent } from '../api/wallet'
import { agentName } from '../lib/agents'
import { isPasskeyError, PasskeyError, signWithPasskey } from '../lib/passkey'
import { GIVE_UP_MS, POLL_GAP_MS, sleep } from '../lib/tx'
import { agentsKey } from './useAgents'
import { freshSignStart, useSignStart } from './useChat'
import { TRANSACTIONS_KEY } from './useTransactions'

interface ApproveVariables {
  /** The confirmation the tap signed, or null when none was fresh: one is then started first. */
  start: SignStart | null
  /** The passkey prompt the tap opened, or null along with `start`. */
  assertion: Promise<PasskeyAssertionJSON> | null
}

/**
 * Stopping an agent with a passkey, for when the owner wallet isn't at hand. `begin`, from the tap on
 * an agent's "Stop with passkey", opens the dialog and has Mitfah quote the wallet's own removal of the
 * agent (POST /api/wallet/agents/stop); the dialog's Approve then signs it with a passkey and sends
 * it, as a chat card's Confirm does (POST /api/sign/start, then /finish), and follows it until it
 * lands. Two taps because the passkey prompt has to open inside a tap, and the quote takes a moment.
 */
export function useAgentStop(chainId: number) {
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [agent, setAgent] = useState<Agent | null>(null)
  const [open, setOpen] = useState(false)
  // Approved and on the wire: what's left is waiting for the network.
  const [sending, setSending] = useState(false)
  const build = useMutation({ mutationFn: (address: string) => stopAgent(chainId, address) })
  const quote = open ? (build.data?.quote ?? null) : null

  const approve = useMutation({
    networkMode: 'always',
    mutationFn: async ({ start, assertion }: ApproveVariables) => {
      if (!quote) throw new Error('Nothing to approve yet.')
      if (!start || !assertion) {
        // No fresh confirmation at the tap: one is started now, after which a browser may refuse to
        // open the prompt outside the tap. The next tap then has a fresh one.
        start = await startSign({ chain_id: quote.chain_id, quote_id: quote.quote_id })
        assertion = signWithPasskey(start).catch((error: unknown) => {
          if (isPasskeyError(error, 'cancelled')) {
            throw new PasskeyError('cancelled', 'Your browser needs one more tap. Tap Approve again.')
          }
          throw error
        })
      }
      const credential = await assertion
      setSending(true)
      const sent = await finishSign({ sign_id: start.sign_id, credential })
      // Sent, but the server can't tell whether it went out: the History tab will say.
      if (!sent.transaction_id) throw new Error(sent.outcome)
      const deadline = Date.now() + GIVE_UP_MS
      for (;;) {
        const row = await fetchTransaction(sent.transaction_id)
        if (row.status === 'confirmed') return
        if (row.status !== 'pending') {
          throw new Error(
            "It didn't go through, so the agent is still on the wallet. Try again, or remove it with your owner wallet.",
          )
        }
        if (Date.now() > deadline) {
          throw new Error('Still waiting for the network. The History tab will show when it lands.')
        }
        await sleep(POLL_GAP_MS)
      }
    },
    onSuccess: () => {
      setOpen(false)
      toaster.push(
        <Message type="success" showIcon closable>
          {agent ? agentName(agent) : 'The agent'} can no longer trade from this wallet.
        </Message>,
        { placement: 'topCenter', duration: 4000 },
      )
    },
    onSettled: (_data, error) => {
      setSending(false)
      // A closed prompt changed nothing, and its confirmation is still good for another tap.
      if (isPasskeyError(error)) return
      void queryClient.invalidateQueries({ queryKey: agentsKey(chainId) })
      void queryClient.invalidateQueries({ queryKey: ['wallet', chainId] })
      void queryClient.invalidateQueries({ queryKey: TRANSACTIONS_KEY })
      // A refused send spent its confirmation: another try needs a fresh one.
      if (error) void queryClient.invalidateQueries({ queryKey: ['sign-start', chainId] })
    },
  })

  const signStart = useSignStart(quote, approve.isPending || approve.isSuccess)

  /** From the tap on "Stop with passkey": opens the dialog and asks for the quote. */
  const begin = (next: Agent) => {
    build.reset()
    approve.reset()
    setAgent(next)
    setOpen(true)
    build.mutate(next.address)
  }

  /** From the tap on Approve: opens the passkey prompt at once when the confirmation is ready. */
  const confirm = () => {
    const start = freshSignStart(signStart)
    approve.mutate({ start, assertion: start ? signWithPasskey(start) : null })
  }

  /** Closes the dialog. A quote not yet approved is dropped, so it can't be approved later. */
  const close = () => {
    setOpen(false)
    if (quote && !approve.isPending && !approve.isSuccess) {
      void cancelSign({ chain_id: quote.chain_id, quote_id: quote.quote_id }).catch(() => undefined)
    }
  }

  return { agent, open, sending, build, approve, signStart, quote, begin, confirm, close }
}

export type AgentStopHandle = ReturnType<typeof useAgentStop>
