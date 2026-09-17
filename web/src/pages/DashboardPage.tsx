import { useState } from 'react'
import KeyIcon from '@rsuite/icons/Key'
import { Button, Message, Placeholder } from 'rsuite'
import { useSelectedChain } from '../chain/useSelectedChain'
import { BalancesCard } from '../components/dashboard/BalancesCard'
import { FundDrawer } from '../components/dashboard/FundDrawer'
import { SpendingCard } from '../components/dashboard/SpendingCard'
import { WalletHeader } from '../components/dashboard/WalletHeader'
import { EmptyState } from '../components/EmptyState'
import { LinkButton } from '../components/LinkButton'
import { PageHeader } from '../components/PageHeader'
import { useMe } from '../hooks/useMe'
import { useWallet } from '../hooks/useWallet'
import { errorText } from '../lib/tx'
import { chainName } from '../wallet/chains'

/** The selected network's wallet: its status, what the assistant may still spend, and balances. */
export function DashboardPage() {
  const { data: me, isPending: mePending } = useMe()
  const { chainId, chain, walletChains } = useSelectedChain()
  const wallet = useWallet()
  const [fundOpen, setFundOpen] = useState(false)
  const hasWallets = walletChains.length > 0 && chainId !== null

  let actions
  let content
  if (mePending || (hasWallets && wallet.isPending)) {
    content = <Placeholder.Paragraph rows={6} active />
  } else if (!hasWallets) {
    content = (
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
    )
  } else if (wallet.isError) {
    content = (
      <Message type="error" showIcon className="mf-settings-note">
        Couldn't load your wallet: {errorText(wallet.error)}{' '}
        <Button appearance="link" size="sm" onClick={() => void wallet.refetch()}>
          Try again
        </Button>
      </Message>
    )
  } else if (!wallet.data) {
    content = (
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
    )
  } else {
    actions = (
      <>
        <Button appearance="primary" onClick={() => setFundOpen(true)}>
          Add funds
        </Button>
        <LinkButton to="/assistant">Ask the assistant</LinkButton>
      </>
    )
    content = (
      <div className="mf-dashboard">
        <WalletHeader wallet={wallet.data} fork={chain?.fork ?? false} />
        <div className="mf-card-grid">
          <SpendingCard spending={wallet.data.spending} />
          <BalancesCard wallet={wallet.data} />
        </div>
        <FundDrawer open={fundOpen} onClose={() => setFundOpen(false)} wallet={wallet.data} chain={chain} />
      </div>
    )
  }

  return (
    <>
      <PageHeader
        title="Dashboard"
        description={me?.email ? `Signed in as ${me.email}` : undefined}
        actions={actions}
      />
      {content}
    </>
  )
}
