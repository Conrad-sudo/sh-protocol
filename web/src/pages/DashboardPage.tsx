import { useState } from 'react'
import { Button } from 'rsuite'
import type { WalletState } from '../api/types'
import { useSelectedChain } from '../chain/useSelectedChain'
import { BalancesCard } from '../components/dashboard/BalancesCard'
import { FundDrawer } from '../components/dashboard/FundDrawer'
import { SpendingCard } from '../components/dashboard/SpendingCard'
import { WalletHeader } from '../components/dashboard/WalletHeader'
import { LinkButton } from '../components/LinkButton'
import { PauseModal } from '../components/owner/PauseModal'
import { WithdrawDrawer } from '../components/owner/WithdrawDrawer'
import { PageHeader } from '../components/PageHeader'
import { useMe } from '../hooks/useMe'
import { useOwnerAction } from '../hooks/useOwnerAction'
import { useWalletView } from '../hooks/useWalletView'

/** The selected network's wallet: its status, what the assistant may still spend, and balances. */
export function DashboardPage() {
  const { data: me } = useMe()
  const view = useWalletView()

  return (
    <>
      <PageHeader
        title="Dashboard"
        description={me?.email ? `Signed in as ${me.email}` : undefined}
        actions={view.wallet && <WalletActions wallet={view.wallet} />}
      />
      {view.wallet ? <WalletOverview wallet={view.wallet} /> : view.fallback}
    </>
  )
}

function WalletOverview({ wallet }: { wallet: WalletState }) {
  const { chain } = useSelectedChain()
  return (
    <div className="mf-dashboard">
      <WalletHeader wallet={wallet} fork={chain?.fork ?? false} />
      <div className="mf-card-grid">
        <SpendingCard spending={wallet.spending} />
        <BalancesCard wallet={wallet} />
      </div>
    </div>
  )
}

/** Add funds, withdraw, and the emergency brake. */
function WalletActions({ wallet }: { wallet: WalletState }) {
  const { chain } = useSelectedChain()
  const tx = useOwnerAction(wallet.chain_id, wallet.owner)
  const [fundOpen, setFundOpen] = useState(false)
  const [withdrawOpen, setWithdrawOpen] = useState(false)
  const [pauseMode, setPauseMode] = useState<'pause' | 'unpause' | null>(null)

  return (
    <>
      <Button appearance="primary" onClick={() => setFundOpen(true)}>
        Add funds
      </Button>
      <Button appearance="ghost" onClick={() => setWithdrawOpen(true)}>
        Withdraw
      </Button>
      <LinkButton to="/assistant">Ask the assistant</LinkButton>
      {wallet.paused ? (
        <Button appearance="ghost" color="orange" onClick={() => setPauseMode('unpause')}>
          Unpause
        </Button>
      ) : (
        <Button appearance="ghost" color="red" onClick={() => setPauseMode('pause')}>
          Pause wallet
        </Button>
      )}
      <FundDrawer open={fundOpen} onClose={() => setFundOpen(false)} wallet={wallet} chain={chain} />
      <WithdrawDrawer
        key={wallet.chain_id}
        open={withdrawOpen}
        onClose={() => setWithdrawOpen(false)}
        wallet={wallet}
        chain={chain}
        tx={tx}
      />
      <PauseModal mode={pauseMode} onClose={() => setPauseMode(null)} wallet={wallet} chain={chain} tx={tx} />
    </>
  )
}
