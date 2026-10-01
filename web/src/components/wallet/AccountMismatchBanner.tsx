import { Message } from 'rsuite'
import { AddressText } from '../AddressText'

/**
 * The browser wallet is connected as another address than the one this account signs in as — the
 * address that owns its Mitfah wallets and signs their changes. The fix is to switch accounts in the
 * wallet. (Signing in with the other address would reach that address's own account instead.)
 */
export function AccountMismatchBanner({ ownerAddr, connected }: { ownerAddr: string; connected: string }) {
  return (
    <Message type="warning" showIcon className="mf-settings-note">
      You're signed in as <AddressText address={ownerAddr} />, but your wallet is connected as{' '}
      <AddressText address={connected} />. Switch to <AddressText address={ownerAddr} /> in your wallet.
    </Message>
  )
}
