import { Button, Message } from 'rsuite'
import { isPasskeyError } from '../lib/passkey'
import { errorText } from '../lib/tx'

interface PasskeyProblemProps {
  error: Error
  /** This device already holds one of the account's passkeys: the tap that finds which. */
  onUseExisting: () => void
  className?: string
}

/**
 * Why making (or finding) this device's passkey stopped, in plain words, and the way on where there
 * is one. A closed prompt is news, not an error.
 */
export function PasskeyProblem({ error, onUseExisting, className = 'mf-tx-status' }: PasskeyProblemProps) {
  if (isPasskeyError(error, 'exists')) {
    return (
      <Message type="info" showIcon className={className}>
        This device already has one of your Mitfah passkeys, perhaps synced from another of your devices.{' '}
        <Button appearance="link" size="sm" onClick={onUseExisting}>
          Use it
        </Button>
      </Message>
    )
  }
  if (isPasskeyError(error, 'cancelled')) {
    return (
      <Message type="info" showIcon className={className}>
        {error.message}
      </Message>
    )
  }
  return (
    <Message type="error" showIcon className={className}>
      {isPasskeyError(error) ? error.message : `Couldn't save the passkey: ${errorText(error)}`}
    </Message>
  )
}
