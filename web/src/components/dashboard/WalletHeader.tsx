import { Message, Text } from 'rsuite'
import type { WalletState } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { assistantDeadline, assistantStatus } from '../../lib/assistant'
import { formatDate, formatTimeLeft } from '../../lib/format'
import { chainName, explorerUrl } from '../../wallet/chains'
import { AddressText } from '../AddressText'
import { CopyButton } from '../CopyButton'
import { GlassPanel } from '../Glass'
import { StatusTag } from '../StatusTag'

/**
 * Which wallet this is and whether it can act: the address, paused or active, and whether the
 * assistant may use it. Anything that weakens the user's protection is spelled out underneath.
 */
export function WalletHeader({ wallet }: { wallet: WalletState }) {
  const link = explorerUrl(wallet.chain_id, 'address', wallet.address)
  const now = useNow(60_000)
  const assistant = assistantStatus(wallet.session)
  const expiresAt = assistantDeadline(wallet.session) ?? 0

  return (
    <GlassPanel bordered className="mf-wallet-header">
      <div className="mf-wallet-header-row">
        <div className="mf-wallet-id">
          <Text size="sm" muted>
            Your Mitfah smart wallet on {chainName(wallet.chain_id)}
          </Text>
          <div className="mf-wallet-address">
            <AddressText address={wallet.address} />
            <CopyButton value={wallet.address} label="Wallet address" />
            {link && (
              <a href={link} target="_blank" rel="noreferrer">
                View on explorer
              </a>
            )}
          </div>
        </div>
        <div className="mf-wallet-tags">
          {wallet.paused ? <StatusTag tone="danger">Paused</StatusTag> : <StatusTag tone="success">Active</StatusTag>}
          {assistant === 'on' || assistant === 'expiring' ? (
            <StatusTag tone="success">Assistant on</StatusTag>
          ) : assistant === 'expired' ? (
            <StatusTag tone="neutral">Assistant expired</StatusTag>
          ) : (
            <StatusTag tone="neutral">Assistant off</StatusTag>
          )}
        </div>
      </div>

      {wallet.paused && (
        <Message type="error" showIcon className="mf-settings-note">
          This wallet is paused: nothing can go out except your withdrawals. Unpause it in Controls.
        </Message>
      )}
      {!wallet.spending.hook_installed && (
        <Message type="error" showIcon className="mf-settings-note">
          The spending limit is off, so nothing caps what the assistant spends. Pause the wallet or revoke its
          devices in Controls.
        </Message>
      )}
      {assistant === 'expiring' && (
        <Message type="warning" showIcon className="mf-settings-note">
          The assistant's access runs out in {formatTimeLeft(expiresAt - now)}. Renew it in Controls.
        </Message>
      )}
      {assistant === 'expired' && (
        <Message type="warning" showIcon className="mf-settings-note">
          The assistant's access ran out on {formatDate(expiresAt)}. Renew it in Controls.
        </Message>
      )}
      {!wallet.is_owner && (
        <Message type="info" showIcon className="mf-settings-note">
          Only the owner, <AddressText address={wallet.owner} />, can change this wallet. You can still see and fund
          it.
        </Message>
      )}
    </GlassPanel>
  )
}
