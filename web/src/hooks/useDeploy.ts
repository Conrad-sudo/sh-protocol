import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useSendTransaction } from 'wagmi'
import { ApiError } from '../api/client'
import { confirmDeploy, prepareDeploy } from '../api/wallet'
import type { DeployRequest } from '../api/types'
import { errorText, isUserRejection, sleep, toTxRequest } from '../lib/tx'
import { isSupportedChainId } from '../wallet/chains'

export type DeployPhase = 'idle' | 'preparing' | 'signing' | 'confirming' | 'done' | 'cancelled' | 'error'

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
}

interface PendingDeploy {
  chainId: number
  deployer: string
  txHash: string
  predictedAddress: string
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

const PENDING_KEY = 'mitfah-pending-deploy'
const POLL_GAP_MS = 2_000
const GIVE_UP_MS = 10 * 60_000

function readPending(): PendingDeploy | null {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY)
    return raw ? (JSON.parse(raw) as PendingDeploy) : null
  } catch {
    return null
  }
}

function writePending(pending: PendingDeploy | null) {
  try {
    if (pending) sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending))
    else sessionStorage.removeItem(PENDING_KEY)
  } catch {
    // Storage blocked: a reload mid-deploy will not resume, but the deploy itself is unaffected.
  }
}

/**
 * Deploys the user's wallet: the API builds the transaction, the user's own wallet signs and sends
 * it, and the API confirms it once mined (answering "pending" until then).
 *
 * The transaction hash is kept in sessionStorage the moment the wallet returns it, so a reload — or
 * a phone switching to the wallet app and back — resumes waiting instead of losing the deploy.
 */
export function useDeploy(onDeployed: (result: DeployResult) => void) {
  const { mutateAsync: sendTransactionAsync } = useSendTransaction()
  const queryClient = useQueryClient()
  const [state, setState] = useState<DeployState>(() => {
    const pending = readPending()
    return pending ? { phase: 'confirming', txHash: pending.txHash, chainId: pending.chainId } : { phase: 'idle' }
  })
  // Bumped on every mount and unmount. A polling loop stops once the counter moves past the value
  // it started with, so StrictMode's double mount (or leaving the page) never leaves two loops.
  const generation = useRef(0)

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
        canResume: readPending() !== null,
      })
  }

  /**
   * Asks the API until the deploy has mined. It never sets state: the caller applies the outcome
   * with `settle`, which keeps the resume-on-mount path clear of synchronous state updates.
   */
  const poll = async (pending: PendingDeploy): Promise<PollOutcome> => {
    const mine = generation.current
    // A loop left behind (the page was left, or StrictMode mounted twice) stays silent and leaves
    // the answer to the loop that replaced it.
    const stale = () => generation.current !== mine
    const deadline = Date.now() + GIVE_UP_MS
    try {
      for (;;) {
        const result = await confirmDeploy({
          chain_id: pending.chainId,
          deployer: pending.deployer,
          tx_hash: pending.txHash,
          predicted_address: pending.predictedAddress,
        })
        if (stale()) return { kind: 'stale' }
        if (result.status === 'deployed') {
          writePending(null)
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
        if (Date.now() > deadline) {
          throw new Error('Your wallet is still being created. Check your wallet for the transaction, then reload this page.')
        }
        await sleep(POLL_GAP_MS)
        if (stale()) return { kind: 'stale' }
      }
    } catch (error) {
      if (stale()) return { kind: 'stale' }
      // A 400 is final (the transaction reverted, or isn't a deploy): stop tracking it so the
      // user can start over. Anything else — a network blip — leaves it to check again.
      if (error instanceof ApiError && error.status === 400) writePending(null)
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

  /** "Check again" after polling gave up or the network dropped. */
  const resume = () => {
    const pending = readPending()
    if (!pending) return
    setState({ phase: 'confirming', txHash: pending.txHash, chainId: pending.chainId })
    void poll(pending).then(outcome => settle(outcome, pending))
  }

  // Pick up a deploy that was waiting when the page was left (the initial state already says
  // `confirming`). Runs once per mount.
  const onMount = useEffectEvent(() => {
    const pending = readPending()
    if (pending) void poll(pending).then(outcome => settle(outcome, pending))
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
    let pending: PendingDeploy
    try {
      setState({ phase: 'preparing' })
      const prepared = await prepareDeploy(request)
      setState({ phase: 'signing' })
      const txHash = await sendTransactionAsync({ ...toTxRequest(prepared.tx), chainId: request.chain_id })
      pending = {
        chainId: request.chain_id,
        deployer: request.deployer,
        txHash,
        predictedAddress: prepared.predicted_address,
      }
    } catch (error) {
      fail(error)
      return
    }
    writePending(pending)
    setState({ phase: 'confirming', txHash: pending.txHash, chainId: pending.chainId })
    settle(await poll(pending), pending)
  }

  return { state, deploy, resume, reset: () => setState({ phase: 'idle' }) }
}
