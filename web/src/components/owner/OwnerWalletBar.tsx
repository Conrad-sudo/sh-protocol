import { useState } from 'react'
import { useConnection } from 'wagmi'
import { Button, Message, Text } from 'rsuite'
import type { WalletState } from '../../api/types'
import type { OwnerReadiness } from '../../hooks/useOwnerReadiness'
import { AddressText } from '../AddressText'
import { StatusTag } from '../StatusTag'
import { AccountMismatchBanner } from '../wallet/AccountMismatchBanner'
import { ConnectDialog } from '../wallet/ConnectDialog'
import { NetworkBanner } from '../wallet/NetworkBanner'

/**
 * What stands between the user and signing an owner change, and how to fix it: connect the owner's
 * browser wallet, or switch to it. Once ready it says which address will sign.
 */
export function OwnerWalletBar({
  wallet,
  readiness,
  fork,
}: {
  wallet: WalletState
  readiness: OwnerReadiness
  fork: boolean
}) {
  const { address } = useConnection()
  const [connectOpen, setConnectOpen] = useState(false)

  switch (readiness) {
    case 'not-owner':
      return (
        <Message type="info" showIcon className="mf-owner-bar">
          Changes to this wallet must be signed by its owner, <AddressText address={wallet.owner} />, and your
          account isn't linked to that address, so they're switched off here.
        </Message>
      )
    case 'disconnected':
      return (
        <div className="mf-owner-bar mf-owner-bar-row">
          <Text>
            Connect your owner wallet, <AddressText address={wallet.owner} />, to make changes.
          </Text>
          <Button appearance="primary" onClick={() => setConnectOpen(true)}>
            Connect wallet
          </Button>
          <ConnectDialog open={connectOpen} onClose={() => setConnectOpen(false)} />
        </div>
      )
    case 'wrong-account':
      return (
        <div className="mf-owner-bar">
          <AccountMismatchBanner
            ownerAddr={wallet.owner}
            connected={address!}
            chainId={wallet.chain_id}
            canRelink={false}
          />
        </div>
      )
    case 'ready':
      return (
        <div className="mf-owner-bar">
          <div className="mf-owner-bar-row">
            <Text>
              Changes are signed by <AddressText address={wallet.owner} />
            </Text>
            <StatusTag tone="success">Owner connected</StatusTag>
          </div>
          <NetworkBanner chainId={wallet.chain_id} fork={fork} />
        </div>
      )
  }
}
