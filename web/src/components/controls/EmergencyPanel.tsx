import { Text } from 'rsuite'
import { GlassPanel } from '../Glass'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import { ControlRow } from './ControlRow'
import type { ControlPanelProps } from './types'

/**
 * The brake: pausing stops everything, in one click; unpausing asks first. Stopping only the
 * assistant is revoking its devices, in the Devices card.
 */
export function EmergencyPanel({ wallet, tx, locked, start, ask }: ControlPanelProps) {
  return (
    <GlassPanel bordered header={<h2>Emergency</h2>} className="mf-card">
      <ControlRow
        title="Wallet"
        status={wallet.paused ? <StatusTag tone="danger">Paused</StatusTag> : <StatusTag tone="success">Active</StatusTag>}
        help={
          wallet.paused
            ? "Nothing can go out, including the assistant's spending. Withdrawals still work."
            : "Pausing stops every transaction from the wallet, the assistant's and yours. Withdrawals still work."
        }
        action={
          wallet.paused ? (
            <TxButton
              tx={tx}
              txKey="unpause"
              appearance="ghost"
              color="orange"
              disabled={locked}
              onClick={() =>
                ask({
                  title: 'Unpause this wallet?',
                  body: (
                    <Text>
                      The assistant can spend again, within your limit. If you paused because something looked
                      wrong, revoke its devices first.
                    </Text>
                  ),
                  confirmLabel: 'Unpause wallet',
                  request: { key: 'unpause', action: { kind: 'unpause' }, success: 'Wallet unpaused.' },
                })
              }
            >
              Unpause
            </TxButton>
          ) : (
            <TxButton
              tx={tx}
              txKey="pause"
              appearance="primary"
              color="red"
              disabled={locked}
              onClick={() =>
                start({
                  key: 'pause',
                  action: { kind: 'pause' },
                  success: 'Wallet paused. Nothing can go out until you unpause it.',
                })
              }
            >
              Pause wallet
            </TxButton>
          )
        }
      >
        <TxStatus tx={tx} txKey="pause" />
        <TxStatus tx={tx} txKey="unpause" />
      </ControlRow>
    </GlassPanel>
  )
}
