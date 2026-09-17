import { useConnection } from 'wagmi'
import type { WalletState } from '../api/types'

/**
 * Whether owner changes can be signed from here:
 * - `not-owner`: the account isn't linked to the wallet's on-chain owner, so the API won't build them;
 * - `disconnected`: no browser wallet is connected;
 * - `wrong-account`: the browser wallet is on another address than the owner;
 * - `ready`: the owner is connected. A wrong network is fixed when the change is made.
 */
export type OwnerReadiness = 'not-owner' | 'disconnected' | 'wrong-account' | 'ready'

export function useOwnerReadiness(wallet: WalletState): OwnerReadiness {
  const { address, isConnected } = useConnection()
  if (!wallet.is_owner) return 'not-owner'
  if (!isConnected || !address) return 'disconnected'
  return address.toLowerCase() === wallet.owner.toLowerCase() ? 'ready' : 'wrong-account'
}
