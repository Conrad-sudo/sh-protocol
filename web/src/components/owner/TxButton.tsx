import { Button, type ButtonProps } from 'rsuite'
import type { OwnerActionHandle } from '../../hooks/useOwnerAction'

interface TxButtonProps extends ButtonProps {
  tx: OwnerActionHandle
  /** The control this button starts; it spins while that one is in flight. */
  txKey: string
}

/** A button that starts an owner transaction. Only one can be in flight, so the others wait. */
export function TxButton({ tx, txKey, disabled, ...rest }: TxButtonProps) {
  const mine = tx.busy && tx.state.key === txKey
  return <Button {...rest} loading={mine} disabled={disabled || (tx.busy && !mine)} />
}
