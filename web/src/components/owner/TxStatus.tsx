import { Button, Loader, Message } from 'rsuite'
import { useSelectedChain } from '../../chain/useSelectedChain'
import type { OwnerActionHandle } from '../../hooks/useOwnerAction'
import { chainName, explorerUrl } from '../../wallet/chains'

interface TxStatusProps {
  tx: OwnerActionHandle
  /** Only the control that started the transaction shows its progress. */
  txKey: string
  /** What to say once it's confirmed. Left out, success is only announced by the toast. */
  doneText?: string
}

/** How an owner transaction is going, and what to do if it stopped. */
export function TxStatus({ tx, txKey, doneText }: TxStatusProps) {
  const { chains } = useSelectedChain()
  const { state } = tx
  if (state.key !== txKey) return null

  const fork = chains.find(c => c.chain_id === state.chainId)?.fork ?? false
  const link = state.txHash && state.chainId ? explorerUrl(state.chainId, 'tx', state.txHash, fork) : null
  const viewTx = link && (
    <>
      {' '}
      <a href={link} target="_blank" rel="noreferrer">
        View the transaction
      </a>
    </>
  )

  // The Loader is already a status region; wrapping it in another makes screen readers repeat it.
  switch (state.phase) {
    case 'preparing':
      return <Loader className="mf-tx-status" content="Checking the change…" />
    case 'switching':
      return <Loader className="mf-tx-status" content={`Switch your wallet to ${chainName(state.chainId)}.`} />
    case 'signing':
      return <Loader className="mf-tx-status" content="Confirm in your wallet." />
    case 'confirming':
      return (
        <div className="mf-tx-status">
          <Loader content={`Waiting for ${chainName(state.chainId)} to confirm…`} />
          {viewTx}
        </div>
      )
    case 'done':
      return doneText ? (
        <Message type="success" showIcon className="mf-tx-status">
          {doneText}
          {viewTx}
        </Message>
      ) : null
    case 'cancelled':
      return (
        <Message type="info" showIcon className="mf-tx-status">
          Cancelled — nothing was sent.
        </Message>
      )
    case 'error':
      return (
        <Message type="error" showIcon className="mf-tx-status">
          {state.error}
          {viewTx}
          {state.canResume && (
            <>
              {' '}
              <Button appearance="link" size="sm" onClick={tx.resume}>
                Check again
              </Button>
            </>
          )}
        </Message>
      )
    default:
      return null
  }
}
