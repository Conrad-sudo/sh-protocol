import { useState } from 'react'
import { useConnect, useConnectors, type Connector } from 'wagmi'
import { Button, Message, Modal, Text } from 'rsuite'
import { useLayoutMode } from '../../layouts/useLayoutMode'
import { errorText, isUserRejection } from '../../lib/tx'

/**
 * Wallets that announce themselves (EIP-6963) appear by name. The generic "Injected" entry is kept
 * only for an older wallet that sets window.ethereum without announcing, and is hidden when nothing
 * is injected at all — clicking it would just fail.
 */
function visibleConnectors(connectors: readonly Connector[]) {
  const announced = connectors.some(c => c.type === 'injected' && c.id !== 'injected')
  const hasInjected = typeof window !== 'undefined' && 'ethereum' in window
  return connectors.filter(c => c.id !== 'injected' || (!announced && hasInjected))
}

export function ConnectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const connectors = visibleConnectors(useConnectors())
  const { mutateAsync: connectAsync, isPending, variables } = useConnect()
  const [error, setError] = useState<string | null>(null)
  const mode = useLayoutMode()

  const connect = async (connector: Connector) => {
    setError(null)
    try {
      await connectAsync({ connector })
      onClose()
    } catch (e) {
      setError(isUserRejection(e) ? 'Cancelled — nothing was connected.' : errorText(e))
    }
  }

  return (
    <Modal open={open} onClose={onClose} size={mode === 'mobile' ? 'full' : 'xs'}>
      <Modal.Header>
        <Modal.Title>Connect a wallet</Modal.Title>
      </Modal.Header>
      <Modal.Body>
        <Text muted>Choose the wallet that will own your Mitfah wallet.</Text>
        {connectors.length === 0 ? (
          <Message type="info" showIcon className="mf-settings-note">
            No browser wallet found. Install one such as{' '}
            <a href="https://metamask.io/download/" target="_blank" rel="noreferrer">
              MetaMask
            </a>
            , then reload this page.
          </Message>
        ) : (
          <div className="mf-connector-list">
            {connectors.map(connector => (
              <Button
                key={connector.uid}
                block
                size="lg"
                className="mf-connector"
                loading={isPending && variables?.connector === connector}
                disabled={isPending}
                onClick={() => void connect(connector)}
              >
                {connector.icon && <img src={connector.icon} alt="" width={24} height={24} />}
                <span>{connector.name}</span>
              </Button>
            ))}
          </div>
        )}
        {error && (
          <Message type="error" showIcon className="mf-settings-note">
            {error}
          </Message>
        )}
      </Modal.Body>
    </Modal>
  )
}
