import { Message, Panel, Text } from 'rsuite'
import { AddressText } from '../AddressText'
import { CopyButton } from '../CopyButton'
import { StatusTag } from '../StatusTag'

/**
 * The browser-wallet address that owns the user's Mitfah wallet on chain. Mitfah has no authority
 * over it — only a transfer signed by that address can change the owner — so the page says plainly
 * that losing it cannot be undone by account recovery.
 */
export function OwnerAddressCard({ ownerAddr }: { ownerAddr: string | null }) {
  return (
    <Panel bordered header="Wallet owner">
      <div className="mf-settings-row">
        <div className="mf-settings-row-label">
          <Text weight="medium">Owner address</Text>
          <Text muted size="sm">The wallet in your browser that controls your Mitfah wallet.</Text>
        </div>
        <div className="mf-settings-row-action">
          {ownerAddr ? (
            <>
              <AddressText address={ownerAddr} />
              <CopyButton value={ownerAddr} label="Address" />
            </>
          ) : (
            <StatusTag>Not linked yet</StatusTag>
          )}
        </div>
      </div>

      <Message type="info" showIcon className="mf-settings-note">
        {ownerAddr
          ? 'Only this address can pause your wallet, change its limits or withdraw. If you lose access to it, Mitfah cannot recover your wallet for you.'
          : "You'll link it when you create your wallet. Only that address will be able to pause the wallet, change its limits or withdraw, so keep it safe."}
      </Message>
    </Panel>
  )
}
