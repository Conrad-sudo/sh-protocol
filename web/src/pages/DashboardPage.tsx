import KeyIcon from '@rsuite/icons/Key'
import { Link } from 'react-router'
import { Button, Placeholder } from 'rsuite'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { useMe } from '../hooks/useMe'

/** Phase 1 placeholder: greets the user and points at wallet setup (phases 3–4 fill this in). */
export function DashboardPage() {
  const { data: me, isPending } = useMe()

  return (
    <>
      <PageHeader title="Dashboard" description={me?.email ? `Signed in as ${me.email}` : undefined} />
      {isPending ? (
        <Placeholder.Paragraph rows={3} active />
      ) : (
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
      )}
    </>
  )
}
