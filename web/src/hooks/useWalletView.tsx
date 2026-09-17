import type { ReactNode } from 'react'
import KeyIcon from '@rsuite/icons/Key'
import { Button, Message, Placeholder } from 'rsuite'
import type { WalletState } from '../api/types'
import { useSelectedChain } from '../chain/useSelectedChain'
import { EmptyState } from '../components/EmptyState'
import { LinkButton } from '../components/LinkButton'
import { errorText } from '../lib/tx'
import { chainName } from '../wallet/chains'
import { useMe } from './useMe'
import { useWallet } from './useWallet'

/**
 * The selected network's wallet for a page that shows one. Until there is a wallet to show,
 * `fallback` says why — loading, no wallet yet, gone from this network, or a failed load with a
 * retry — and the page renders that instead.
 */
export function useWalletView(): { wallet: WalletState; fallback: null } | { wallet: null; fallback: ReactNode } {
  const { isPending: mePending } = useMe()
  const { chainId, walletChains } = useSelectedChain()
  const query = useWallet()
  const hasWallets = walletChains.length > 0 && chainId !== null

  if (mePending || (hasWallets && query.isPending)) {
    return { wallet: null, fallback: <Placeholder.Paragraph rows={6} active /> }
  }
  if (!hasWallets) {
    return {
      wallet: null,
      fallback: (
        <EmptyState
          icon={<KeyIcon />}
          title="Create your wallet"
          action={
            <LinkButton to="/onboarding" appearance="primary">
              Get started
            </LinkButton>
          }
        >
          You own it, you set the daily limit, and the assistant can only spend within it.
        </EmptyState>
      ),
    }
  }
  // A failed background refresh keeps showing the wallet it already has.
  if (query.isError && !query.data) {
    return {
      wallet: null,
      fallback: (
        <Message type="error" showIcon className="mf-settings-note">
          Couldn't load your wallet: {errorText(query.error)}{' '}
          <Button appearance="link" size="sm" onClick={() => void query.refetch()}>
            Try again
          </Button>
        </Message>
      ),
    }
  }
  if (!query.data) {
    return {
      wallet: null,
      fallback: (
        <EmptyState
          icon={<KeyIcon />}
          title={`No wallet on ${chainName(chainId)}`}
          action={
            <LinkButton to="/wallets/new" appearance="primary">
              Create one
            </LinkButton>
          }
        >
          This account has no Mitfah wallet on this network yet.
        </EmptyState>
      ),
    }
  }
  return { wallet: query.data, fallback: null }
}
