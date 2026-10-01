import { Button, Loader, Message } from 'rsuite'
import type { DeployState } from '../../hooks/useDeploy'
import { chainName, explorerUrl } from '../../wallet/chains'

interface DeployProgressProps {
  state: DeployState
  chainId: number
  /** Starts over. Only offered when no transaction can still create the wallet. */
  onRetry: () => void
  /** Waits again for the transaction already sent. */
  onResume: () => void
  /** Stops waiting on the API or the wallet, before anything is sent. */
  onAbandon: () => void
}

/** What is happening while the wallet is created, and what to do if it stops. */
export function DeployProgress({ state, chainId, onRetry, onResume, onAbandon }: DeployProgressProps) {
  const txLink = state.txHash ? explorerUrl(chainId, 'tx', state.txHash) : null
  const viewTx = txLink && (
    <a href={txLink} target="_blank" rel="noreferrer">
      View the transaction
    </a>
  )
  // Nothing has been sent yet, so giving up is always safe: a deploy the wallet sends later is followed.
  const waiting = (text: string) => (
    <div>
      <Loader content={text} />{' '}
      <Button appearance="link" size="sm" onClick={onAbandon}>
        Stop waiting
      </Button>
    </div>
  )
  const tryAgain = (
    <Button appearance="link" size="sm" onClick={onRetry}>
      Try again
    </Button>
  )

  switch (state.phase) {
    case 'preparing':
      return waiting('Preparing your wallet…')
    case 'signing':
      return waiting('Confirm the transaction in your wallet.')
    case 'confirming':
      return (
        // No role here: the Loader is already a status region, and nesting two makes some screen
        // readers announce the message twice.
        <div className="mf-deploy-progress">
          <Loader content={`Creating your wallet on ${chainName(chainId)}. This usually takes under a minute.`} />
          {viewTx}
        </div>
      )
    case 'cancelled':
      return (
        <Message type="info" showIcon className="mf-settings-note">
          Cancelled — nothing was sent. {tryAgain}
        </Message>
      )
    case 'abandoned':
      return (
        <Message type="info" showIcon className="mf-settings-note">
          Stopped waiting. If your wallet still shows the request, reject it there before you try again. {tryAgain}
        </Message>
      )
    case 'error':
      return (
        <Message type="error" showIcon className="mf-settings-note">
          {state.error} {viewTx}{' '}
          {state.canResume && (
            <Button appearance="link" size="sm" onClick={onResume}>
              Check again
            </Button>
          )}{' '}
          {(!state.canResume || state.lost) && tryAgain}
        </Message>
      )
    default:
      return null
  }
}
