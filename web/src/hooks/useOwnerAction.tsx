import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Message, useToaster } from 'rsuite'
import { useConnection, useSendTransaction, useSwitchChain } from 'wagmi'
import { ApiError } from '../api/client'
import { confirmOwnerTx, confirmSessionTx, prepareOwnerAction } from '../api/wallet'
import type { OwnerAction } from '../api/types'
import { useAuth } from '../auth/useAuth'
import {
  errorText,
  GIVE_UP_MS,
  isUserRejection,
  POLL_GAP_MS,
  RECHECK_MS,
  REQUEST_TIMEOUT_MS,
  sleep,
  timeoutSignal,
  toTxRequest,
  UNSEEN_GRACE_MS,
} from '../lib/tx'
import { chainName, isSupportedChainId } from '../wallet/chains'

export type OwnerTxPhase =
  | 'idle'
  | 'preparing'
  | 'switching'
  | 'signing'
  | 'confirming'
  | 'done'
  | 'cancelled'
  /** The user stopped waiting before the wallet answered; it may still send, and then it's picked up. */
  | 'abandoned'
  | 'error'

export interface OwnerTxState {
  phase: OwnerTxPhase
  /** The control that started it, so only that one shows progress. */
  key?: string
  /** The network the transaction is for. */
  chainId?: number
  txHash?: string
  error?: string
  /** Sent but not confirmed yet: "Check again" waits for the same transaction. */
  canResume?: boolean
}

export interface OwnerTxRequest {
  /** Names the control, e.g. `pause` or `watched:usdc`. */
  key: string
  action: OwnerAction
  /** Shown once the network confirms, e.g. "Wallet paused." */
  success: string
}

interface PendingOwnerTx {
  chainId: number
  txHash: string
  key: string
  success: string
  /** `session` for the assistant's key, which has its own confirm endpoint; absent for the rest. */
  confirmWith?: 'session'
  /** When the wallet handed the hash back (ms). Every wait is measured from it, not from a page visit. */
  sentAt: number
  /** The network still hadn't seen it after UNSEEN_GRACE_MS: kept only for "Check again". */
  lost?: boolean
}

type PollOutcome = { kind: 'confirmed' } | { kind: 'failed'; error: unknown } | { kind: 'stale' }

const BUSY: OwnerTxPhase[] = ['preparing', 'switching', 'signing', 'confirming']
/** Waiting on the API or the wallet, before any transaction exists: "Stop waiting" ends these. */
const BEFORE_SEND: OwnerTxPhase[] = ['preparing', 'switching', 'signing']
/** Sent when a stored transaction needs a page to wait for it: the page showing picks it up. */
const HANDOVER_EVENT = 'mitfah-owner-tx-handover'

/** One stored transaction per account and network, so a stuck one never blocks another network. */
const pendingKey = (userId: number | null, chainId: number) => `mitfah-pending-owner-tx:${userId ?? 'anon'}:${chainId}`

/** The transaction went through, but the wallet ended up trusting a key Mitfah doesn't hold. */
class ForeignKeyError extends Error {}

/** Asks the API once whether the transaction has mined, through the endpoint that finishes it. */
async function confirmOnce(pending: PendingOwnerTx): Promise<{ mined: boolean; seen?: boolean }> {
  const body = { chain_id: pending.chainId, tx_hash: pending.txHash }
  const signal = timeoutSignal(REQUEST_TIMEOUT_MS)
  if (pending.confirmWith !== 'session') {
    const answer = await confirmOwnerTx(body, signal)
    return answer.status === 'pending' ? { mined: false, seen: answer.seen } : { mined: true }
  }
  const answer = await confirmSessionTx(body, signal)
  if (answer.status === 'unrecognized_key') {
    throw new ForeignKeyError(
      "That went through, but your wallet now trusts a key Mitfah doesn't hold, so the assistant still can't act. Turn it on again to replace that key.",
    )
  }
  return answer.status === 'pending' ? { mined: false, seen: answer.seen } : { mined: true }
}

function readPending(storageKey: string): PendingOwnerTx | null {
  try {
    const raw = sessionStorage.getItem(storageKey)
    if (!raw) return null
    const pending = JSON.parse(raw) as PendingOwnerTx
    // Stored before sentAt existed: count from now.
    return { ...pending, sentAt: pending.sentAt ?? Date.now() }
  } catch {
    return null
  }
}

function writePending(storageKey: string, pending: PendingOwnerTx | null) {
  try {
    if (pending) sessionStorage.setItem(storageKey, JSON.stringify(pending))
    else sessionStorage.removeItem(storageKey)
  } catch {
    // Storage blocked: a reload mid-transaction will not resume; the transaction is unaffected.
  }
}

/** Whether a page opening now should wait for this stored transaction by itself. */
function resumable(pending: PendingOwnerTx | null): pending is PendingOwnerTx {
  return pending !== null && !pending.lost && Date.now() < pending.sentAt + GIVE_UP_MS
}

/**
 * Makes a change only the wallet's owner can sign. The API builds and test-runs the transaction,
 * the user's browser wallet signs and sends it, and the API confirms it once mined (answering
 * "pending" until then). One transaction at a time per page, which shows one network.
 *
 * The hash is kept in sessionStorage, per network, as soon as the wallet returns it, so a reload —
 * or a phone switching to the wallet app and back — keeps waiting for the same transaction.
 *
 * Every wait has an end the user can see. Before the wallet answers, "Stop waiting" (or closing the
 * dialog) gives up on it; should the wallet send after all, the hash is still picked up. After it
 * answers, the waiting stops when the network has never seen the hash for UNSEEN_GRACE_MS (a wallet
 * that failed to broadcast, or sent through another RPC), and at GIVE_UP_MS from sending.
 *
 * @param chainId  The network the wallet being changed lives on.
 * @param owner    The wallet's owner: the only address that can sign these.
 */
export function useOwnerAction(chainId: number, owner: string) {
  const { userId } = useAuth()
  const storageKey = pendingKey(userId, chainId)
  const { address, chainId: connectedChainId } = useConnection()
  const { mutateAsync: sendTransactionAsync } = useSendTransaction()
  const { mutateAsync: switchChainAsync } = useSwitchChain()
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [state, setState] = useState<OwnerTxState>(() => {
    const pending = readPending(storageKey)
    return resumable(pending)
      ? { phase: 'confirming', key: pending.key, chainId: pending.chainId, txHash: pending.txHash }
      : { phase: 'idle' }
  })
  // Bumped on every mount and unmount; a polling loop that sees it move stops quietly, so
  // StrictMode's double mount (or leaving the page) never leaves two loops reporting.
  const generation = useRef(0)
  // Bumped when the user stops waiting; an attempt that sees it move drops what it was waiting for.
  const attempt = useRef(0)
  const busy = BUSY.includes(state.phase)

  const fail = (error: unknown, key: string, pending?: PendingOwnerTx) => {
    if (isUserRejection(error)) setState({ phase: 'cancelled', key, chainId })
    else
      setState({
        phase: 'error',
        key,
        chainId: pending?.chainId ?? chainId,
        error: errorText(error),
        txHash: pending?.txHash,
        canResume: pending !== undefined && readPending(storageKey) !== null,
      })
  }

  /** Asks the API until the transaction has mined. Never sets state; `settle` applies the outcome. */
  const poll = async (pending: PendingOwnerTx, deadline: number): Promise<PollOutcome> => {
    const mine = generation.current
    const stale = () => generation.current !== mine
    try {
      for (;;) {
        const { mined, seen } = await confirmOnce(pending)
        if (stale()) return { kind: 'stale' }
        if (mined) {
          writePending(storageKey, null)
          await queryClient.invalidateQueries({ queryKey: ['wallet', pending.chainId] })
          return { kind: 'confirmed' }
        }
        if (seen === false && Date.now() - pending.sentAt > UNSEEN_GRACE_MS) {
          writePending(storageKey, { ...pending, lost: true })
          // A wallet that sped it up sent it again under a hash the page never learns, and that one
          // may have gone through: read the wallet again so the page shows what is true now.
          void queryClient.invalidateQueries({ queryKey: ['wallet', pending.chainId] })
          throw new Error(
            `${chainName(pending.chainId)} hasn't received this transaction. If your wallet says it failed, nothing changed and you can try again.`,
          )
        }
        if (Date.now() > deadline) {
          throw new Error('Still waiting for the network. Check your wallet for the transaction, then check again.')
        }
        await sleep(POLL_GAP_MS)
        if (stale()) return { kind: 'stale' }
      }
    } catch (error) {
      if (stale()) return { kind: 'stale' }
      // A 400 is final (it reverted, or wasn't sent to this wallet); anything else may be a blip.
      if (error instanceof ApiError && error.status === 400) writePending(storageKey, null)
      if (error instanceof ForeignKeyError) {
        // Final too: it mined. Re-read the wallet so the page shows which key it trusts now.
        writePending(storageKey, null)
        await queryClient.invalidateQueries({ queryKey: ['wallet', pending.chainId] })
        if (stale()) return { kind: 'stale' }
      }
      return { kind: 'failed', error }
    }
  }

  const settle = (outcome: PollOutcome, pending: PendingOwnerTx) => {
    if (outcome.kind === 'confirmed') {
      setState({ phase: 'done', key: pending.key, chainId: pending.chainId, txHash: pending.txHash })
      toaster.push(
        <Message type="success" showIcon closable>
          {pending.success}
        </Message>,
        { placement: 'topCenter', duration: 4000 },
      )
    } else if (outcome.kind === 'failed') {
      fail(outcome.error, pending.key, pending)
    }
  }

  /** "Check again" after waiting gave up or the connection dropped; also picks up a handed-over hash. */
  const resume = () => {
    const pending = readPending(storageKey)
    if (!pending) return
    setState({ phase: 'confirming', key: pending.key, chainId: pending.chainId, txHash: pending.txHash })
    const deadline = Math.max(pending.sentAt + GIVE_UP_MS, Date.now() + RECHECK_MS)
    void poll(pending, deadline).then(outcome => settle(outcome, pending))
  }

  const onMount = useEffectEvent(() => {
    const pending = readPending(storageKey)
    if (resumable(pending)) void poll(pending, pending.sentAt + GIVE_UP_MS).then(outcome => settle(outcome, pending))
    // Lost or long gone: nothing on a fresh page could offer "Check again" for it.
    else if (pending) writePending(storageKey, null)
  })
  useEffect(() => {
    generation.current += 1
    onMount()
    return () => {
      generation.current += 1
    }
  }, [])

  const onHandover = useEffectEvent(() => {
    if (!busy) resume()
  })
  useEffect(() => {
    const listener = () => onHandover()
    window.addEventListener(HANDOVER_EVENT, listener)
    return () => window.removeEventListener(HANDOVER_EVENT, listener)
  }, [])

  /** Makes the change. Resolves true once the network has confirmed it. */
  const run = async ({ key, action, success }: OwnerTxRequest): Promise<boolean> => {
    if (busy) return false
    if (!isSupportedChainId(chainId)) {
      setState({ phase: 'error', key, chainId, error: 'This network is not supported.' })
      return false
    }
    if (!address || address.toLowerCase() !== owner.toLowerCase()) {
      setState({ phase: 'error', key, chainId, error: "Connect your wallet's owner address first." })
      return false
    }
    const mine = generation.current
    attempt.current += 1
    const thisAttempt = attempt.current
    const abandoned = () => attempt.current !== thisAttempt
    let pending: PendingOwnerTx
    try {
      // Prepared first: a change that would fail is refused before the wallet is bothered at all.
      setState({ phase: 'preparing', key, chainId })
      const { tx } = await prepareOwnerAction(chainId, action, timeoutSignal(REQUEST_TIMEOUT_MS))
      if (abandoned()) return false
      if (connectedChainId !== chainId) {
        setState({ phase: 'switching', key, chainId })
        await switchChainAsync({ chainId })
        if (abandoned()) return false
      }
      setState({ phase: 'signing', key, chainId })
      const txHash = await sendTransactionAsync({ ...toTxRequest(tx), chainId })
      pending = { chainId, txHash, key, success, sentAt: Date.now() }
      if (action.kind === 'session') pending.confirmWith = 'session'
    } catch (error) {
      // Given up on already, so whatever the wallet says now is no longer news.
      if (!abandoned()) fail(error, key)
      return false
    }
    writePending(storageKey, pending)
    // The page was left, or the user stopped waiting, and the wallet sent it anyway. Rather than
    // poll from a page nobody sees, hand the stored hash to the page showing now (this one, after a
    // "Stop waiting"); one that mounts later finds it in storage.
    if (generation.current !== mine || abandoned()) {
      window.dispatchEvent(new Event(HANDOVER_EVENT))
      return false
    }
    setState({ phase: 'confirming', key, chainId, txHash: pending.txHash })
    const outcome = await poll(pending, pending.sentAt + GIVE_UP_MS)
    settle(outcome, pending)
    return outcome.kind === 'confirmed'
  }

  /** "Stop waiting": gives up on the API or the wallet before any transaction exists. */
  const abandon = () => {
    if (!BEFORE_SEND.includes(state.phase)) return
    attempt.current += 1
    setState({ phase: 'abandoned', key: state.key, chainId })
  }

  /**
   * For a dialog closing, so it opens clean next time: forgets how its own control's last attempt
   * ended, and stops waiting on the wallet. A transaction already sent keeps being confirmed.
   */
  const release = (key: string) => {
    if (state.key !== key || state.phase === 'confirming') return
    if (BEFORE_SEND.includes(state.phase)) attempt.current += 1
    setState({ phase: 'idle' })
  }

  return { state, busy, run, resume, abandon, release }
}

export type OwnerActionHandle = ReturnType<typeof useOwnerAction>
