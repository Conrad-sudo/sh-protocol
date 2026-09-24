import type { ReactNode } from 'react'
import { Panel, Text } from 'rsuite'
import { useNow } from '../../hooks/useNow'
import { ASSISTANT_KEY_DAYS, ASSISTANT_KEY_TTL_SECS, assistantStatus, type AssistantStatus } from '../../lib/assistant'
import { formatDate, formatTimeLeft, formatUsd, formatWindow } from '../../lib/format'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import { ControlRow } from './ControlRow'
import type { ControlPanelProps } from './types'

/**
 * The two brakes. Pausing stops everything; turning the assistant off removes only its key. Both
 * tighten, so they happen in one click; undoing either asks first.
 */
export function EmergencyPanel(props: ControlPanelProps) {
  const { wallet, tx, locked, start, ask } = props

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

      <AssistantRow {...props} />
    </Panel>
  )
}

const ASSISTANT_TAGS: Record<AssistantStatus, ReactNode> = {
  on: <StatusTag tone="success">On</StatusTag>,
  expiring: <StatusTag tone="warning">Expires soon</StatusTag>,
  expired: <StatusTag tone="neutral">Expired</StatusTag>,
  off: <StatusTag tone="neutral">Off</StatusTag>,
  foreign: <StatusTag tone="neutral">Off</StatusTag>,
}

/**
 * The assistant's key. Each grant lasts ASSISTANT_KEY_DAYS and is a brand-new key, so turning the
 * assistant on and renewing it are the same transaction, and both loosen, so both ask first.
 */
function AssistantRow({ wallet, tx, locked, start, ask }: ControlPanelProps) {
  const now = useNow(60_000)
  const status = assistantStatus(wallet.session)
  const expiresAt = (wallet.session.expires_at ?? 0) * 1_000
  const { daily_limit_usd: limit, window_hours: hours } = wallet.spending
  const period = formatWindow(Math.round(hours * 3_600))
  const renewing = status === 'on' || status === 'expiring' || status === 'expired'

  const help = {
    on: `The assistant can spend within your limit until ${formatDate(expiresAt)}. Turning it off removes its key from the wallet; your own access stays.`,
    expiring: `The assistant's access runs out in ${formatTimeLeft(expiresAt - now)}. Renew it to keep the assistant working.`,
    expired: `The assistant's access ran out on ${formatDate(expiresAt)}, so it can't act for this wallet. Renew it to switch it back on.`,
    off: "The assistant can't act for this wallet until you turn it on.",
    foreign: "Your wallet trusts a signing key Mitfah doesn't hold, so the assistant can't act. Turning it on replaces that key.",
  }[status]

  const grant = () => {
    const until = formatDate(Date.now() + ASSISTANT_KEY_TTL_SECS * 1_000)
    ask({
      title: renewing ? "Renew the assistant's access?" : 'Turn the assistant on?',
      body: (
        <Text>
          {renewing
            ? `It keeps its access for another ${ASSISTANT_KEY_DAYS} days, until ${until}, and can move funds from this wallet`
            : `For ${ASSISTANT_KEY_DAYS} days, until ${until}, it will be able to move funds from this wallet`}
          , up to {formatUsd(limit)} every {period}.
        </Text>
      ),
      confirmLabel: renewing ? 'Renew' : 'Turn on assistant',
      request: {
        key: 'session-on',
        action: { kind: 'session', action: 'add', ttlSecs: ASSISTANT_KEY_TTL_SECS },
        success: renewing ? `Assistant renewed until ${until}.` : `Assistant turned on until ${until}.`,
      },
    })
  }

  const grantButton = (
    <TxButton tx={tx} txKey="session-on" appearance="ghost" color="orange" disabled={locked} onClick={grant}>
      {renewing ? 'Renew' : 'Turn on assistant'}
    </TxButton>
  )

  return (
    <ControlRow
      title="Assistant"
      status={ASSISTANT_TAGS[status]}
      help={help}
      action={
        status === 'on' || status === 'expiring' ? (
          <div className="mf-control-buttons">
            {grantButton}
            <TxButton
              tx={tx}
              txKey="session-off"
              appearance="primary"
              color="red"
              disabled={locked}
              onClick={() =>
                start({
                  key: 'session-off',
                  action: { kind: 'session', action: 'remove' },
                  success: 'Assistant turned off. It can no longer act for this wallet.',
                })
              }
            >
              Turn off assistant
            </TxButton>
          </div>
        ) : (
          grantButton
        )
      }
    >
      <TxStatus tx={tx} txKey="session-on" />
      <TxStatus tx={tx} txKey="session-off" />
    </ControlRow>
  )
}
