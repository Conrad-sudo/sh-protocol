import { Button, Modal, Text } from 'rsuite'
import type { Chain, WalletState } from '../../api/types'
import type { OwnerActionHandle } from '../../hooks/useOwnerAction'
import { useOwnerReadiness } from '../../hooks/useOwnerReadiness'
import { OwnerWalletBar } from './OwnerWalletBar'
import { TxButton } from './TxButton'
import { TxStatus } from './TxStatus'

interface PauseModalProps {
  /** Fixed when the modal opens, so it doesn't flip once the change goes through. */
  mode: 'pause' | 'unpause' | null
  onClose: () => void
  wallet: WalletState
  chain: Chain | undefined
  tx: OwnerActionHandle
}

/** Pausing or unpausing from the dashboard, with whatever the owner wallet needs first. */
export function PauseModal({ mode, onClose, wallet, chain, tx }: PauseModalProps) {
  const readiness = useOwnerReadiness(wallet)
  const pausing = mode === 'pause'
  const key = mode ?? 'pause'

  const close = () => {
    if (!tx.busy && tx.state.key === key) tx.reset()
    onClose()
  }

  const submit = async () => {
    const confirmed = await tx.run(
      pausing
        ? { key, action: { kind: 'pause' }, success: 'Wallet paused. Nothing can go out until you unpause it.' }
        : { key, action: { kind: 'unpause' }, success: 'Wallet unpaused.' },
    )
    if (confirmed) close()
  }

  return (
    <Modal open={mode !== null} onClose={close} size="sm">
      <Modal.Header>
        <Modal.Title>{pausing ? 'Pause this wallet?' : 'Unpause this wallet?'}</Modal.Title>
      </Modal.Header>
      <Modal.Body>
        {pausing ? (
          <Text>
            Every transaction from the wallet stops, the assistant's and yours, until you unpause it. You can
            still withdraw.
          </Text>
        ) : (
          <Text>
            The assistant can spend again, within your limit. If you paused because something looked wrong,
            turn the assistant off in Controls first.
          </Text>
        )}
        <OwnerWalletBar wallet={wallet} readiness={readiness} fork={chain?.fork ?? false} />
        <TxStatus tx={tx} txKey={key} />
      </Modal.Body>
      <Modal.Footer className="mf-modal-actions">
        <Button appearance="subtle" onClick={close}>
          Cancel
        </Button>
        <TxButton
          tx={tx}
          txKey={key}
          appearance="primary"
          color={pausing ? 'red' : 'orange'}
          disabled={readiness !== 'ready'}
          onClick={() => void submit()}
        >
          {pausing ? 'Pause wallet' : 'Unpause wallet'}
        </TxButton>
      </Modal.Footer>
    </Modal>
  )
}
