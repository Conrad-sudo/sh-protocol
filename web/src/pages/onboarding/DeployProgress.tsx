import { Button, Loader, Message } from 'rsuite'
import type { DeployState } from '../../hooks/useDeploy'
import { chainName, explorerUrl } from '../../wallet/chains'

interface DeployProgressProps {
  state: DeployState
  chainId: number
  fork: boolean
  onRetry: () => void
  onResume: () => void
}

/** What is happening while the wallet is created, and what to do if it stops. */
export function DeployProgress({ state, chainId, fork, onRetry, onResume }: DeployProgressProps) {
  const txLink = state.txHash ? explorerUrl(chainId, 'tx', state.txHash, fork) : null
  const viewTx = txLink && (
    <a href={txLink} target="_blank" rel="noreferrer">
      View the transaction
    </a>
  )

  switch (state.phase) {
    case 'preparing':
      return <Loader content="Preparing your wallet…" />
    case 'signing':
      return <Loader content="Confirm the transaction in your wallet." />
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
          Cancelled — nothing was sent.{' '}
          <Button appearance="link" size="sm" onClick={onRetry}>
            Try again
          </Button>
        </Message>
      )
    case 'error':
      return (
        <Message type="error" showIcon className="mf-settings-note">
          {state.error} {viewTx}{' '}
          {state.canResume ? (
            <Button appearance="link" size="sm" onClick={onResume}>
              Check again
            </Button>
          ) : (
            <Button appearance="link" size="sm" onClick={onRetry}>
              Try again
            </Button>
          )}
        </Message>
      )
    default:
      return null
  }
}
