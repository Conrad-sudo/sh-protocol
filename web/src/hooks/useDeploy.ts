import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useSendTransaction } from 'wagmi'
import { ApiError } from '../api/client'
import { confirmDeploy, prepareDeploy } from '../api/wallet'
import { useAuth } from '../auth/useAuth'
import type { DeployRequest } from '../api/types'
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

export type DeployPhase =
  | 'idle'
  | 'preparing'
  | 'signing'
  | 'confirming'
  | 'done'
  | 'cancelled'
  /** The user stopped waiting before the wallet answered; it may still send, and then it's followed. */
  | 'abandoned'
  | 'error'

export interface DeployState {
  phase: DeployPhase
  txHash?: string
  /** The network `txHash` was sent on. After a reload it can differ from the form's default. */
  chainId?: number
  error?: string
  /**
   * The transaction was sent but not yet confirmed. Retrying must wait for THAT transaction —
   * starting a new deploy would create a second wallet.
   */
  canResume?: boolean
  /** The network never received the transaction, so it can't create a wallet: starting over is safe. */
  lost?: boolean
}

interface PendingDeploy {
  chainId: number
  deployer: string
  txHash: string
  predictedAddress: string
  /** When the wallet handed the hash back (ms). Every wait is measured from it, not from a page visit. */
  sentAt: number
}

export interface DeployResult {
  chainId: number
  walletAddress: string
  /** False if the wallet exists but the assistant's key could not be confirmed on it. */
  assistantReady: boolean
}

type PollOutcome =
  | { kind: 'deployed'; result: DeployResult }
  | { kind: 'failed'; error: unknown }
  /** A newer polling loop took over; this one reports nothing. */
  | { kind: 'stale' }

/** Waiting on the API or the wallet, before any transaction exists: "Stop waiting" ends these. */
const BEFORE_SEND: DeployPhase[] = ['preparing', 'signing']

/** The network still hadn't seen the transaction after UNSEEN_GRACE_MS. */
class NotReceivedError extends Error {}

// Per account: the tab outlives a sign-out, and another account's deploy is not this one's to wait
// for — the API refuses to confirm it, so it would hold this account's wizard on a deploy it can
// never finish.
const pendingKey = (userId: number | null) => `mitfah-pending-deploy:${userId ?? 'anon'}`

function readPending(storageKey: string): PendingDeploy | null {
  try {
    const raw = sessionStorage.getItem(storageKey)
    if (!raw) return null
    const pending = JSON.parse(raw) as PendingDeploy
    // Stored before sentAt existed: count from now.
    return { ...pending, sentAt: pending.sentAt ?? Date.now() }
  } catch {
    return null
  }
}

function writePending(storageKey: string, pending: PendingDeploy | null) {
  try {
    if (pending) sessionStorage.setItem(storageKey, JSON.stringify(pending))
    else sessionStorage.removeItem(storageKey)
  } catch {
    // Storage blocked: a reload mid-deploy will not resume, but the deploy itself is unaffected.
  }
}

/** How long to wait for a stored deploy from now: at least RECHECK_MS, even long after it was sent. */
const deadlineFor = (pending: PendingDeploy) => Math.max(pending.sentAt + GIVE_UP_MS, Date.now() + RECHECK_MS)

/**
 * Deploys the user's wallet: the API builds the transaction, the user's own wallet signs and sends
 * it, and the API confirms it once mined (answering "pending" until then).
 *
 * The transaction hash is kept in sessionStorage the moment the wallet returns it, so a reload — or
 * a phone switching to the wallet app and back — resumes waiting instead of losing the deploy.
 *
 * Every wait has an end the user can see. Before the wallet answers, "Stop waiting" gives up on it;
 * should the wallet send after all, that deploy is followed. After it answers, the waiting stops
 * when the network has never seen the hash for UNSEEN_GRACE_MS, and only then may the user start
 * over. Unlike an owner action, a stored deploy is never dropped unchecked: while it may still land,
 * starting over would make a second wallet.
 */
export function useDeploy(onDeployed: (result: DeployResult) => void) {
  const { userId } = useAuth()
  const storageKey = pendingKey(userId)
  const { mutateAsync: sendTransactionAsync } = useSendTransaction()
  const queryClient = useQueryClient()
  const [state, setState] = useState<DeployState>(() => {
    const pending = readPending(storageKey)
    return pending ? { phase: 'confirming', txHash: pending.txHash, chainId: pending.chainId } : { phase: 'idle' }
  })
  // Bumped on every mount and unmount. A polling loop stops once the counter moves past the value
  // it started with, so StrictMode's double mount (or leaving the page) never leaves two loops.
  const generation = useRef(0)
  // Bumped when the user stops waiting; an attempt that sees it move stops reporting.
  const attempt = useRef(0)

  // Latest callback, without restarting the resume effect when it changes.
  const onDeployedRef = useRef(onDeployed)
  useEffect(() => {
    onDeployedRef.current = onDeployed
  })

  const fail = (error: unknown, pending?: PendingDeploy) => {
    if (isUserRejection(error)) setState({ phase: 'cancelled' })
    else
      setState({
        phase: 'error',
        error: errorText(error),
        txHash: pending?.txHash,
        chainId: pending?.chainId,
        canResume: readPending(storageKey) !== null,
        lost: error instanceof NotReceivedError,
      })
  }

  /**
   * Asks the API until the deploy has mined. It never sets state: the caller applies the outcome
   * with `settle`, which keeps the resume-on-mount path clear of synchronous state updates.
   */
  const poll = async (pending: PendingDeploy, deadline: number): Promise<PollOutcome> => {
    const mine = generation.current
    // A loop left behind (the page was left, or StrictMode mounted twice) stays silent and leaves
    // the answer to the loop that replaced it.
    const stale = () => generation.current !== mine
    try {
      for (;;) {
        const result = await confirmDeploy(
          {
            chain_id: pending.chainId,
            deployer: pending.deployer,
            tx_hash: pending.txHash,
            predicted_address: pending.predictedAddress,
          },
          timeoutSignal(REQUEST_TIMEOUT_MS),
        )
        if (stale()) return { kind: 'stale' }
        if (result.status === 'deployed') {
          writePending(storageKey, null)
          await queryClient.invalidateQueries({ queryKey: ['me'] })
          await queryClient.invalidateQueries({ queryKey: ['wallet'] })
          return {
            kind: 'deployed',
            result: {
              chainId: result.chain_id,
              walletAddress: result.wallet_address,
              assistantReady: result.session_key_authorized,
            },
          }
        }
        // Kept in storage all the same, for "Check again": it may yet turn up.
        if (result.seen === false && Date.now() - pending.sentAt > UNSEEN_GRACE_MS) {
          throw new NotReceivedError(
            `${chainName(pending.chainId)} hasn't received this transaction. If your wallet says it failed, no wallet was created and you can try again.`,
          )
        }
        if (Date.now() > deadline) {
          throw new Error('Your wallet is still being created. Check your wallet for the transaction, then check again.')
        }
        await sleep(POLL_GAP_MS)
        if (stale()) return { kind: 'stale' }
      }
    } catch (error) {
      if (stale()) return { kind: 'stale' }
      // A 400 is final (the transaction reverted, or isn't a deploy), and so is a 403 (this account
      // doesn't sign in as the address that sent it): stop tracking it so the user can start over.
      // Anything else — a network blip — leaves it to check again.
      if (error instanceof ApiError && (error.status === 400 || error.status === 403)) writePending(storageKey, null)
      return { kind: 'failed', error }
    }
  }

  const settle = (outcome: PollOutcome, pending: PendingDeploy) => {
    if (outcome.kind === 'deployed') {
      setState({ phase: 'done', txHash: pending.txHash, chainId: pending.chainId })
      onDeployedRef.current(outcome.result)
    } else if (outcome.kind === 'failed') {
      fail(outcome.error, pending)
    }
  }

  /** Waits for `pending`, showing it. */
  const follow = (pending: PendingDeploy, deadline: number) => {
    setState({ phase: 'confirming', txHash: pending.txHash, chainId: pending.chainId })
    void poll(pending, deadline).then(outcome => settle(outcome, pending))
  }

  /** "Check again" after polling gave up, the network dropped, or the network hadn't seen it. */
  const resume = () => {
    const pending = readPending(storageKey)
    if (pending) follow(pending, deadlineFor(pending))
  }

  // Pick up a deploy that was waiting when the page was left (the initial state already says
  // `confirming`). Runs once per mount.
  const onMount = useEffectEvent(() => {
    const pending = readPending(storageKey)
    if (pending) void poll(pending, deadlineFor(pending)).then(outcome => settle(outcome, pending))
  })
  useEffect(() => {
    generation.current += 1
    onMount()
    return () => {
      generation.current += 1
    }
  }, [])

  const deploy = async (request: DeployRequest) => {
    if (!isSupportedChainId(request.chain_id)) {
      setState({ phase: 'error', error: 'This network is not supported.' })
      return
    }
    attempt.current += 1
    const thisAttempt = attempt.current
    const abandoned = () => attempt.current !== thisAttempt
    let pending: PendingDeploy
    try {
      setState({ phase: 'preparing' })
      const prepared = await prepareDeploy(request, timeoutSignal(REQUEST_TIMEOUT_MS))
      if (abandoned()) return
      setState({ phase: 'signing' })
      const txHash = await sendTransactionAsync({ ...toTxRequest(prepared.tx), chainId: request.chain_id })
      pending = {
        chainId: request.chain_id,
        deployer: request.deployer,
        txHash,
        predictedAddress: prepared.predicted_address,
        sentAt: Date.now(),
      }
    } catch (error) {
      // Given up on already, so whatever the wallet says now is no longer news.
      if (!abandoned()) fail(error)
      return
    }
    writePending(storageKey, pending)
    // Followed even if the user stopped waiting, or has left the page: it is creating the wallet all
    // the same, and confirming it is what files the wallet as theirs.
    follow(pending, pending.sentAt + GIVE_UP_MS)
  }

  /** "Stop waiting": gives up on the API or the wallet before any transaction exists. */
  const abandon = () => {
    if (!BEFORE_SEND.includes(state.phase)) return
    attempt.current += 1
    setState({ phase: 'abandoned' })
  }

  /**
   * "Try again": forgets the last attempt, including a transaction the network never received.
   * Never one that may still land — that one only gets "Check again".
   */
  const startOver = () => {
    if (state.canResume && !state.lost) return
    writePending(storageKey, null)
    setState({ phase: 'idle' })
  }

  return { state, deploy, resume, abandon, startOver }
}
