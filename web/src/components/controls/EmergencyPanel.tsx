import { Panel, Text } from 'rsuite'
import { formatUsd, formatWindow } from '../../lib/format'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import { ControlRow } from './ControlRow'
import type { ControlPanelProps } from './types'

/**
 * The two brakes. Pausing stops everything; turning the assistant off removes only its key. Both
 * tighten, so they happen in one click; undoing either asks first.
 */
export function EmergencyPanel({ wallet, tx, locked, start, ask }: ControlPanelProps) {
  const { daily_limit_usd: limit, window_hours: hours } = wallet.spending
  const period = formatWindow(Math.round(hours * 3_600))

  return (
    <Panel bordered header={<h2>Emergency</h2>} className="mf-card">
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
                      wrong, turn the assistant off first.
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

      <ControlRow
        title="Assistant"
        status={
          wallet.session.active ? <StatusTag tone="success">On</StatusTag> : <StatusTag tone="neutral">Off</StatusTag>
        }
        help={
          wallet.session.key === null
            ? "Mitfah holds no signing key for this wallet, so the assistant can't act for it."
            : wallet.session.active
              ? 'The assistant can spend within your limit. Turning it off removes its key from the wallet; your own access stays.'
              : "The assistant can't act for this wallet until you turn it back on."
        }
        action={
          wallet.session.key === null ? null : wallet.session.active ? (
            <TxButton
              tx={tx}
              txKey="session"
              appearance="primary"
              color="red"
              disabled={locked}
              onClick={() =>
                start({
                  key: 'session',
                  action: { kind: 'session', action: 'remove' },
                  success: 'Assistant turned off. It can no longer act for this wallet.',
                })
              }
            >
              Turn off assistant
            </TxButton>
          ) : (
            <TxButton
              tx={tx}
              txKey="session"
              appearance="ghost"
              color="orange"
              disabled={locked}
              onClick={() =>
                ask({
                  title: 'Turn the assistant on?',
                  body: (
                    <Text>
                      It will be able to move funds from this wallet, up to {formatUsd(limit)} every {period}.
                    </Text>
                  ),
                  confirmLabel: 'Turn on assistant',
                  request: {
                    key: 'session',
                    action: { kind: 'session', action: 'add' },
                    success: 'Assistant turned on.',
                  },
                })
              }
            >
              Turn on assistant
            </TxButton>
          )
        }
      >
        <TxStatus tx={tx} txKey="session" />
      </ControlRow>
    </Panel>
  )
}
