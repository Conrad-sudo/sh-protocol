import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { parseUnits, type Address } from 'viem'
import { useSendTransaction } from 'wagmi'
import { ApiError } from '../api/client'
import { confirmDeposit } from '../api/transactions'
import {
  errorText,
  GIVE_UP_MS,
  isUserRejection,
  POLL_GAP_MS,
  RECHECK_MS,
  REQUEST_TIMEOUT_MS,
  sleep,
  timeoutSignal,
  UNSEEN_GRACE_MS,
} from '../lib/tx'
import { TRANSACTIONS_KEY } from './useTransactions'
import { chainById, chainName, isSupportedChainId } from '../wallet/chains'

export type FundPhase =
  | 'idle'
  | 'signing'
  | 'confirming'
  | 'done'
  | 'cancelled'
  /** The user stopped waiting before the wallet answered; it may still send, and then it's followed. */
  | 'abandoned'
  | 'error'

export interface FundState {
  phase: FundPhase
  txHash?: string
  error?: string
  /** Sent but not confirmed yet: "Check again" waits for the same transfer. */
  canResume?: boolean
}

interface SentTransfer {
  hash: string
  /** When the wallet handed the hash back (ms). Every wait is measured from it. */
  sentAt: number
}

/**
 * Sends the native token from the connected browser wallet to the user's Mitfah wallet.
 *
 * Anyone may fund a wallet, so this does not go through the owner confirms. The API's deposit
 * endpoint follows it instead, on the node the dashboard reads the balance from, so "Received"
 * means the balance shown has it. The same endpoint lists it in the History tab.
 *
 * Every wait has an end the user can see, as for the owner actions. "Stop waiting" (or closing the
 * drawer) lets go of a wallet that hasn't answered; a transfer it sends anyway is still followed.
 * Once sent, the waiting stops when the network has never seen the hash for UNSEEN_GRACE_MS, and at
 * GIVE_UP_MS from sending.
 */
export function useFundWallet(chainId: number, walletAddress: string) {
  const { mutateAsync: sendTransactionAsync } = useSendTransaction()
  const queryClient = useQueryClient()
  const [state, setState] = useState<FundState>({ phase: 'idle' })
  // Bumped on every mount and unmount; a polling loop that sees it move stops quietly.
  const generation = useRef(0)
  // Bumped when the user stops waiting; an attempt that sees it move stops reporting.
  const attempt = useRef(0)
  // What "Check again" waits for: a transfer sent and not yet known to have mined or failed.
  const unsettled = useRef<SentTransfer | null>(null)

  useEffect(() => {
    generation.current += 1
    return () => {
      generation.current += 1
    }
  }, [])

  /** Asks the API until the transfer has mined. Throws when it failed or the network never got it. */
  const poll = async (transfer: SentTransfer, deadline: number, stale: () => boolean) => {
    for (;;) {
      const answer = await confirmDeposit(chainId, transfer.hash, timeoutSignal(REQUEST_TIMEOUT_MS))
      if (stale() || answer.status === 'confirmed') return
      if (answer.seen === false && Date.now() - transfer.sentAt > UNSEEN_GRACE_MS) {
        throw new Error(
          `${chainName(chainId)} hasn't received this transfer. If your wallet says it failed, nothing was sent and you can try again.`,
        )
      }
      if (Date.now() > deadline) {
        throw new Error('Still waiting for the network. Check your wallet for the transfer, then check again.')
      }
      await sleep(POLL_GAP_MS)
      if (stale()) return
    }
  }

  /** Waits for `transfer`, showing it. */
  const follow = async (transfer: SentTransfer, deadline: number) => {
    const mine = generation.current
    const stale = () => generation.current !== mine
    unsettled.current = transfer
    setState({ phase: 'confirming', txHash: transfer.hash })
    try {
      await poll(transfer, deadline, stale)
      if (stale()) return
      unsettled.current = null
      await queryClient.invalidateQueries({ queryKey: ['wallet', chainId] })
      setState({ phase: 'done', txHash: transfer.hash })
    } catch (error) {
      if (stale()) return
      // A 400 is final (it failed, or didn't go to this wallet); anything else may still arrive.
      if (error instanceof ApiError && error.status === 400) unsettled.current = null
      // A wallet that sped the transfer up sent it again under a hash the page never learns, and
      // that one may have arrived: read the balance again so it shows what is true now.
      void queryClient.invalidateQueries({ queryKey: ['wallet', chainId] })
      setState({ phase: 'error', error: errorText(error), txHash: transfer.hash, canResume: unsettled.current !== null })
    } finally {
      // Listed, or settled, in the History tab.
      void queryClient.invalidateQueries({ queryKey: TRANSACTIONS_KEY })
    }
  }

  const send = async (amount: string) => {
    if (!isSupportedChainId(chainId)) {
      setState({ phase: 'error', error: 'This network is not supported.' })
      return
    }
    attempt.current += 1
    const thisAttempt = attempt.current
    const abandoned = () => attempt.current !== thisAttempt
    let transfer: SentTransfer
    try {
      setState({ phase: 'signing' })
      const decimals = chainById(chainId)?.nativeCurrency.decimals ?? 18
      const hash = await sendTransactionAsync({
        to: walletAddress as Address,
        value: parseUnits(amount, decimals),
        chainId,
      })
      transfer = { hash, sentAt: Date.now() }
    } catch (error) {
      // Given up on already, so whatever the wallet says now is no longer news.
      if (abandoned()) return
      if (isUserRejection(error)) setState({ phase: 'cancelled' })
      else setState({ phase: 'error', error: errorText(error) })
      return
    }
    // Followed even if the user stopped waiting: the transfer is on its way all the same.
    await follow(transfer, transfer.sentAt + GIVE_UP_MS)
  }

  /** "Check again" after the waiting stopped short of an answer. */
  const resume = () => {
    const transfer = unsettled.current
    if (transfer) void follow(transfer, Math.max(transfer.sentAt + GIVE_UP_MS, Date.now() + RECHECK_MS))
  }

  /** "Stop waiting": lets go of a wallet that hasn't answered. */
  const abandon = () => {
    if (state.phase !== 'signing') return
    attempt.current += 1
    setState({ phase: 'abandoned' })
  }

  /**
   * Back to a clean form, for the drawer closing or the amount changing: stops waiting on the
   * wallet, and forgets how the last transfer ended. One already sent keeps being followed.
   */
  const release = () => {
    if (state.phase === 'confirming') return
    if (state.phase === 'signing') attempt.current += 1
    setState({ phase: 'idle' })
  }

  return { state, send, resume, abandon, release }
}
