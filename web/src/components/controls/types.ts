import type { ReactNode } from 'react'
import type { WalletState } from '../../api/types'
import type { OwnerActionHandle, OwnerTxRequest } from '../../hooks/useOwnerAction'

/**
 * A change that loosens the wallet's protection, held until the user confirms it. Usually one owner
 * transaction (`request`); `run` is for a change that starts with something else inside the same tap,
 * such as making a passkey, which a browser allows only from a tap.
 */
export type Confirmation = {
  title: string
  body: ReactNode
  confirmLabel: string
} & ({ request: OwnerTxRequest } | { run: () => void })

/** What every Controls panel gets from the page. */
export interface ControlPanelProps {
  wallet: WalletState
  tx: OwnerActionHandle
  /** True until the owner's browser wallet is connected: nothing can be signed. */
  locked: boolean
  /** Makes a change straight away (it tightens, or is neutral). */
  start: (request: OwnerTxRequest) => void
  /** Asks first (it loosens, or could stop the assistant working). */
  ask: (confirmation: Confirmation) => void
}
