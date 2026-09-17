import KeyIcon from '@rsuite/icons/Key'
import { Link } from 'react-router'
import { Button, Placeholder } from 'rsuite'
import { useSelectedChain } from '../chain/useSelectedChain'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { useMe } from '../hooks/useMe'
import { chainName } from '../wallet/chains'

/** Placeholder until phase 4: greets the user and points at wallet setup. */
export function DashboardPage() {
  const { data: me, isPending } = useMe()
  const { chainId, walletChains } = useSelectedChain()

  let content
  if (isPending) {
    content = <Placeholder.Paragraph rows={3} active />
  } else if (walletChains.length > 0 && chainId !== null) {
    content = (
      <EmptyState icon={<KeyIcon />} title={`Your wallet is ready on ${chainName(chainId)}`}>
        Balances and spending will show here.
      </EmptyState>
    )
  } else {
    content = (
      <EmptyState
        icon={<KeyIcon />}
        title="Create your wallet"
        action={
          <Button as={Link} to="/onboarding" appearance="primary">
            Get started
          </Button>
        }
      >
        You own it, you set the daily limit, and the assistant can only spend within it.
      </EmptyState>
    )
  }

  return (
    <>
      <PageHeader title="Dashboard" description={me?.email ? `Signed in as ${me.email}` : undefined} />
      {content}
    </>
  )
}
