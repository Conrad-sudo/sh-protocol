import { Message, Panel, Text } from 'rsuite'
import type { WalletState } from '../../api/types'
import { chainName, explorerUrl } from '../../wallet/chains'
import { AddressText } from '../AddressText'
import { CopyButton } from '../CopyButton'
import { StatusTag } from '../StatusTag'

/**
 * Which wallet this is and whether it can act: the address, paused or active, and whether the
 * assistant may use it. Anything that weakens the user's protection is spelled out underneath.
 */
export function WalletHeader({ wallet, fork }: { wallet: WalletState; fork: boolean }) {
  const link = explorerUrl(wallet.chain_id, 'address', wallet.address, fork)

  return (
    <Panel bordered className="mf-wallet-header">
      <div className="mf-wallet-header-row">
        <div className="mf-wallet-id">
          <Text size="sm" muted>
            Your Mitfah wallet on {chainName(wallet.chain_id)}
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
          {wallet.session.active ? (
            <StatusTag tone="success">Assistant on</StatusTag>
          ) : (
            <StatusTag tone="neutral">Assistant off</StatusTag>
          )}
        </div>
      </div>

      {wallet.paused && (
        <Message type="error" showIcon className="mf-settings-note">
          This wallet is paused, so no transactions can go out, including the assistant's. You can still
          withdraw. Unpause it in Controls when you're ready.
        </Message>
      )}
      {!wallet.spending.hook_installed && (
        <Message type="error" showIcon className="mf-settings-note">
          The spending limit isn't switched on for this wallet, so nothing caps what the assistant can
          spend. Pause the wallet or turn the assistant off in Controls until it's fixed.
        </Message>
      )}
      {!wallet.is_owner && (
        <Message type="info" showIcon className="mf-settings-note">
          Changes to this wallet must be signed by its owner, <AddressText address={wallet.owner} />, and your
          account isn't linked to that address. You can still see and fund the wallet here.
        </Message>
      )}
    </Panel>
  )
}
