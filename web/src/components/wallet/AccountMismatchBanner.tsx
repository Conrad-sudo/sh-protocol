import type { Address } from 'viem'
import { Message } from 'rsuite'
import { AddressText } from '../AddressText'
import { SiweVerifyCard } from './SiweVerifyCard'

/**
 * The connected wallet is not the one this account is linked to. Owner actions must be signed by
 * the linked address, so the usual fix is to switch accounts in the wallet. Re-linking is offered
 * only before any Mitfah wallet exists: an existing wallet's owner is fixed on chain, and changing
 * the link would leave the account unable to control it.
 */
export function AccountMismatchBanner({
  ownerAddr,
  connected,
  chainId,
  canRelink,
}: {
  ownerAddr: string
  connected: Address
  chainId: number
  canRelink: boolean
}) {
  return (
    <>
      <Message type="warning" showIcon className="mf-settings-note">
        Your account is linked to <AddressText address={ownerAddr} />, but your wallet is connected as{' '}
        <AddressText address={connected} />. Switch to the linked account in your wallet
        {canRelink ? ', or link this address instead.' : '.'}
      </Message>
      {canRelink && <SiweVerifyCard address={connected} chainId={chainId} actionLabel="Link this address instead" />}
    </>
  )
}
