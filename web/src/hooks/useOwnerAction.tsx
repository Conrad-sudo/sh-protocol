import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Message, useToaster } from 'rsuite'
import { useConnection, useSendTransaction, useSwitchChain } from 'wagmi'
import { ApiError } from '../api/client'
import { confirmOwnerTx, prepareOwnerAction } from '../api/wallet'
import type { OwnerAction } from '../api/types'
import { useAuth } from '../auth/useAuth'
import { errorText, isUserRejection, sleep, toTxRequest } from '../lib/tx'
import { isSupportedChainId } from '../wallet/chains'

export type OwnerTxPhase = 'idle' | 'preparing' | 'switching' | 'signing' | 'confirming' | 'done' | 'cancelled' | 'error'

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
}

type PollOutcome = { kind: 'confirmed' } | { kind: 'failed'; error: unknown } | { kind: 'stale' }

const BUSY: OwnerTxPhase[] = ['preparing', 'switching', 'signing', 'confirming']
/** Sent when a page that was left stores a transaction, so the page showing now waits for it. */
const HANDOVER_EVENT = 'mitfah-owner-tx-handover'
const POLL_GAP_MS = 2_000
const GIVE_UP_MS = 10 * 60_000

const pendingKey = (userId: number | null) => `mitfah-pending-owner-tx:${userId ?? 'anon'}`

function readPending(storageKey: string): PendingOwnerTx | null {
  try {
    const raw = sessionStorage.getItem(storageKey)
    return raw ? (JSON.parse(raw) as PendingOwnerTx) : null
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

/**
 * Makes a change only the wallet's owner can sign. The API builds and test-runs the transaction,
 * the user's browser wallet signs and sends it, and the API confirms it once mined (answering
 * "pending" until then). One transaction at a time per page.
 *
 * The hash is kept in sessionStorage as soon as the wallet returns it, so a reload — or a phone
 * switching to the wallet app and back — keeps waiting for the same transaction.
 *
 * @param chainId  The network the wallet being changed lives on.
 * @param owner    The wallet's owner: the only address that can sign these.
 */
export function useOwnerAction(chainId: number, owner: string) {
  const { userId } = useAuth()
  const storageKey = pendingKey(userId)
  const { address, chainId: connectedChainId } = useConnection()
  const { mutateAsync: sendTransactionAsync } = useSendTransaction()
  const { mutateAsync: switchChainAsync } = useSwitchChain()
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [state, setState] = useState<OwnerTxState>(() => {
    const pending = readPending(storageKey)
    return pending
      ? { phase: 'confirming', key: pending.key, chainId: pending.chainId, txHash: pending.txHash }
      : { phase: 'idle' }
  })
  // Bumped on every mount and unmount; a polling loop that sees it move stops quietly, so
  // StrictMode's double mount (or leaving the page) never leaves two loops reporting.
  const generation = useRef(0)
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
  const poll = async (pending: PendingOwnerTx): Promise<PollOutcome> => {
    const mine = generation.current
    const stale = () => generation.current !== mine
    const deadline = Date.now() + GIVE_UP_MS
    try {
      for (;;) {
        const result = await confirmOwnerTx({ chain_id: pending.chainId, tx_hash: pending.txHash })
        if (stale()) return { kind: 'stale' }
        if (result.status === 'confirmed') {
          writePending(storageKey, null)
          await queryClient.invalidateQueries({ queryKey: ['wallet', pending.chainId] })
          return { kind: 'confirmed' }
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

  /** "Check again" after waiting gave up or the connection dropped. */
  const resume = () => {
    const pending = readPending(storageKey)
    if (!pending) return
    setState({ phase: 'confirming', key: pending.key, chainId: pending.chainId, txHash: pending.txHash })
    void poll(pending).then(outcome => settle(outcome, pending))
  }

  const onMount = useEffectEvent(() => {
    const pending = readPending(storageKey)
    if (pending) void poll(pending).then(outcome => settle(outcome, pending))
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
    let pending: PendingOwnerTx
    try {
      // Prepared first: a change that would fail is refused before the wallet is bothered at all.
      setState({ phase: 'preparing', key, chainId })
      const { tx } = await prepareOwnerAction(chainId, action)
      if (connectedChainId !== chainId) {
        setState({ phase: 'switching', key, chainId })
        await switchChainAsync({ chainId })
      }
      setState({ phase: 'signing', key, chainId })
      const txHash = await sendTransactionAsync({ ...toTxRequest(tx), chainId })
      pending = { chainId, txHash, key, success }
    } catch (error) {
      fail(error, key)
      return false
    }
    writePending(storageKey, pending)
    // The page was left while the wallet was open. Rather than poll from a page nobody sees, hand
    // the stored hash to the page showing now; one that mounts later finds it in storage.
    if (generation.current !== mine) {
      window.dispatchEvent(new Event(HANDOVER_EVENT))
      return false
    }
    setState({ phase: 'confirming', key, chainId, txHash: pending.txHash })
    const outcome = await poll(pending)
    settle(outcome, pending)
    return outcome.kind === 'confirmed'
  }

  return { state, busy, run, resume, reset: () => setState({ phase: 'idle' }) }
}

export type OwnerActionHandle = ReturnType<typeof useOwnerAction>
