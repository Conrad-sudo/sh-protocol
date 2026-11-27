import { Message } from 'rsuite'
import { AddressText } from '../AddressText'
import { CopyButton } from '../CopyButton'
import { Explained } from '../Explained'
import { GlassPanel } from '../Glass'
import { StatusTag } from '../StatusTag'

/**
 * The browser-wallet address the account signs in as, which also owns the user's Mitfah wallets on
 * chain. Mitfah has no authority over it — only a transfer signed by that address can change a
 * wallet's owner, and there is no other way to sign in — so the page says plainly that losing it
 * cannot be undone by account recovery.
 */
export function OwnerAddressCard({ ownerAddr }: { ownerAddr: string | null }) {
  return (
    <GlassPanel bordered header="Your wallet">
      <div className="mf-settings-row">
        <div className="mf-settings-row-label">
          <Explained level="label" title="Address">
            You sign in with it, and it owns your wallets.
          </Explained>
        </div>
        <div className="mf-settings-row-action">
          {ownerAddr ? (
            <>
              <AddressText address={ownerAddr} />
              <CopyButton value={ownerAddr} label="Address" />
            </>
          ) : (
            <StatusTag>None</StatusTag>
          )}
        </div>
      </div>

      <Message type="info" showIcon className="mf-settings-note">
        Only this address can sign you in, change your wallets or withdraw. If you lose it, Mitfah can't recover them.
      </Message>
    </GlassPanel>
  )
}
