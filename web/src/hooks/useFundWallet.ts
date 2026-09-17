import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { parseUnits, type Address } from 'viem'
import { waitForTransactionReceipt } from 'viem/actions'
import { useConfig, useSendTransaction } from 'wagmi'
import { getConnectorClient } from 'wagmi/actions'
import { errorText, isUserRejection } from '../lib/tx'
import { chainById, isSupportedChainId } from '../wallet/chains'

export type FundPhase = 'idle' | 'signing' | 'confirming' | 'done' | 'cancelled' | 'error'

export interface FundState {
  phase: FundPhase
  txHash?: string
  error?: string
}

const RECEIPT_TIMEOUT_MS = 10 * 60_000

/**
 * Sends the native token from the connected browser wallet to the user's Mitfah wallet.
 *
 * Anyone may fund a wallet, so this does not go through the API's owner-only confirm endpoint.
 * The receipt is read through the connected wallet instead: it is the node the transfer went to,
 * and on a local fork the only one that knows about it.
 */
export function useFundWallet(chainId: number, walletAddress: string) {
  const { mutateAsync: sendTransactionAsync } = useSendTransaction()
  const config = useConfig()
  const queryClient = useQueryClient()
  const [state, setState] = useState<FundState>({ phase: 'idle' })

  const send = async (amount: string) => {
    if (!isSupportedChainId(chainId)) {
      setState({ phase: 'error', error: 'This network is not supported.' })
      return
    }
    let txHash: string | undefined
    try {
      setState({ phase: 'signing' })
      const decimals = chainById(chainId)?.nativeCurrency.decimals ?? 18
      const hash = await sendTransactionAsync({
        to: walletAddress as Address,
        value: parseUnits(amount, decimals),
        chainId,
      })
      txHash = hash
      setState({ phase: 'confirming', txHash: hash })
      const client = await getConnectorClient(config, { chainId })
      const receipt = await waitForTransactionReceipt(client, {
        hash,
        pollingInterval: 2_000,
        timeout: RECEIPT_TIMEOUT_MS,
      })
      if (receipt.status !== 'success') throw new Error('The transfer failed on the network.')
      await queryClient.invalidateQueries({ queryKey: ['wallet', chainId] })
      setState({ phase: 'done', txHash: hash })
    } catch (error) {
      if (isUserRejection(error)) setState({ phase: 'cancelled' })
      else setState({ phase: 'error', error: errorText(error), txHash })
    }
  }

  return { state, send, reset: () => setState({ phase: 'idle' }) }
}
