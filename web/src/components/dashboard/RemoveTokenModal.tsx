import { useState } from 'react'
import { Button, Modal, Text } from 'rsuite'
import type { Chain, TokenBalance, WalletState } from '../../api/types'
import type { OwnerActionHandle } from '../../hooks/useOwnerAction'
import { useOwnerReadiness } from '../../hooks/useOwnerReadiness'
import { OwnerWalletBar } from '../owner/OwnerWalletBar'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'

interface RemoveTokenModalProps {
  open: boolean
  token: TokenBalance
  /** Whether the token counts toward the limit right now; read live, so step 2 follows step 1. */
  counts: boolean
  wallet: WalletState
  chain: Chain | undefined
  tx: OwnerActionHandle
  onRemove: (token: TokenBalance) => void
  onClose: () => void
}

/**
 * Takes a token off the dashboard. The dashboard shows everything the limit covers, so a token that
 * counts has to stop counting first: step 1 is that owner transaction, step 2 the removal. Nothing
 * leaves the wallet either way. Mounted afresh for each removal, so it remembers how it started.
 */
export function RemoveTokenModal({ open, token, counts, wallet, chain, tx, onRemove, onClose }: RemoveTokenModalProps) {
  const readiness = useOwnerReadiness(wallet)
  const [twoSteps] = useState(counts)
  const name = token.ticker.toUpperCase()
  const key = `stop-counting:${token.ticker}`

  const close = () => {
    if (!tx.busy && tx.state.key === key) tx.reset()
    onClose()
  }

  const stopCounting = () =>
    void tx.run({
      key,
      action: { kind: 'watched-token', token: token.ticker, action: 'remove' },
      success: `${name} no longer counts toward your limit.`,
    })

  const staysInWallet = token.custom
    ? `It disappears from your dashboard and the assistant stops recognising it. Any ${name} in the wallet stays there, and you can add it again at any time.`
    : `It disappears from your dashboard. Any ${name} in the wallet stays there, and you can add it back by its address at any time.`

  return (
    // A short question: a small dialog on every screen, like ConfirmModal.
    <Modal open={open} onClose={close} size="xs" role="alertdialog">
      <Modal.Header>
        <Modal.Title>{`Remove ${name}?`}</Modal.Title>
      </Modal.Header>
      <Modal.Body>
        {counts ? (
          <>
            <Text>
              {name} counts toward your spending limit, so it has to stop counting before it can come off your
              dashboard.
            </Text>
            <Text weight="semibold" className="mf-settings-note">
              Step 1 of 2: stop counting {name}
            </Text>
            <Text>The assistant will then be able to move {name} out of this wallet without any limit.</Text>
            <OwnerWalletBar wallet={wallet} readiness={readiness} fork={chain?.fork ?? false} />
            <TxStatus tx={tx} txKey={key} />
          </>
        ) : (
          <>
            {twoSteps && (
              <Text weight="semibold">
                {name} no longer counts toward your limit. Step 2 of 2: remove it from your dashboard.
              </Text>
            )}
            <Text className={twoSteps ? 'mf-settings-note' : undefined}>{staysInWallet}</Text>
          </>
        )}
      </Modal.Body>
      <Modal.Footer className="mf-modal-actions">
        <Button appearance="subtle" onClick={close}>
          {counts || !twoSteps ? 'Cancel' : 'Keep it'}
        </Button>
        {counts ? (
          <TxButton
            tx={tx}
            txKey={key}
            appearance="primary"
            color="orange"
            disabled={readiness !== 'ready'}
            onClick={stopCounting}
          >
            Stop counting {name}
          </TxButton>
        ) : (
          <Button
            appearance="primary"
            color="orange"
            onClick={() => {
              close()
              onRemove(token)
            }}
          >
            Remove
          </Button>
        )}
      </Modal.Footer>
    </Modal>
  )
}
