import type { ReactNode } from 'react'
import { Button, Modal } from 'rsuite'

interface ConfirmModalProps {
  open: boolean
  title: string
  children: ReactNode
  confirmLabel: string
  /** `warning` (amber) loosens a limit; `danger` (red) is an emergency or can't be undone. */
  tone: 'warning' | 'danger'
  onConfirm: () => void
  onClose: () => void
}

/** A second look before a change that weakens the wallet's protection. */
export function ConfirmModal({ open, title, children, confirmLabel, tone, onConfirm, onClose }: ConfirmModalProps) {
  return (
    // A short question: a small dialog on every screen (RSuite caps it to the screen's width).
    <Modal open={open} onClose={onClose} size="xs" role="alertdialog">
      <Modal.Header>
        <Modal.Title>{title}</Modal.Title>
      </Modal.Header>
      <Modal.Body>{children}</Modal.Body>
      <Modal.Footer className="mf-modal-actions">
        <Button appearance="subtle" onClick={onClose}>
          Cancel
        </Button>
        <Button
          appearance="primary"
          color={tone === 'danger' ? 'red' : 'orange'}
          onClick={() => {
            onClose()
            onConfirm()
          }}
        >
          {confirmLabel}
        </Button>
      </Modal.Footer>
    </Modal>
  )
}
