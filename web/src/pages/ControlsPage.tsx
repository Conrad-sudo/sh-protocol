import { useState } from 'react'
import { Button } from 'rsuite'
import type { WalletState } from '../api/types'
import { useSelectedChain } from '../chain/useSelectedChain'
import { AdvancedPanel } from '../components/controls/AdvancedPanel'
import { EmergencyPanel } from '../components/controls/EmergencyPanel'
import { SpendingPanel } from '../components/controls/SpendingPanel'
import { TokensPanel } from '../components/controls/TokensPanel'
import type { Confirmation } from '../components/controls/types'
import { ConfirmModal } from '../components/owner/ConfirmModal'
import { OwnerWalletBar } from '../components/owner/OwnerWalletBar'
import { WithdrawDrawer } from '../components/owner/WithdrawDrawer'
import { PageHeader } from '../components/PageHeader'
import { useOwnerAction, type OwnerTxRequest } from '../hooks/useOwnerAction'
import { useOwnerReadiness } from '../hooks/useOwnerReadiness'
import { useWalletView } from '../hooks/useWalletView'

const TITLE = 'Controls'
const DESCRIPTION = 'Changes are signed by your owner wallet and apply once the network confirms them.'

/** Everything the owner can change about the wallet: the brakes, the limit, and what it counts. */
export function ControlsPage() {
  const view = useWalletView()
  if (view.wallet) return <Controls wallet={view.wallet} />
  return (
    <>
      <PageHeader title={TITLE} description={DESCRIPTION} />
      {view.fallback}
    </>
  )
}

function Controls({ wallet }: { wallet: WalletState }) {
  const { chain } = useSelectedChain()
  const tx = useOwnerAction(wallet.chain_id, wallet.owner)
  const readiness = useOwnerReadiness(wallet)
  const [withdrawOpen, setWithdrawOpen] = useState(false)
  // Kept after closing so the modal's text doesn't vanish while it animates out.
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const panel = {
    wallet,
    tx,
    locked: readiness !== 'ready',
    start: (request: OwnerTxRequest) => void tx.run(request),
    ask: (next: Confirmation) => {
      setConfirmation(next)
      setConfirmOpen(true)
    },
  }

  return (
    <>
      <PageHeader
        title={TITLE}
        description={DESCRIPTION}
        actions={
          <Button appearance="ghost" onClick={() => setWithdrawOpen(true)}>
            Withdraw
          </Button>
        }
      />
      <div className="mf-dashboard">
        <OwnerWalletBar wallet={wallet} readiness={readiness} fork={chain?.fork ?? false} />
        <EmergencyPanel {...panel} />
        <SpendingPanel {...panel} />
        <TokensPanel {...panel} chain={chain} />
        <AdvancedPanel {...panel} chain={chain} />
      </div>
      {confirmation && (
        <ConfirmModal
          open={confirmOpen}
          title={confirmation.title}
          confirmLabel={confirmation.confirmLabel}
          tone="warning"
          onConfirm={() => panel.start(confirmation.request)}
          onClose={() => setConfirmOpen(false)}
        >
          {confirmation.body}
        </ConfirmModal>
      )}
      <WithdrawDrawer
        key={wallet.chain_id}
        open={withdrawOpen}
        onClose={() => setWithdrawOpen(false)}
        wallet={wallet}
        chain={chain}
        tx={tx}
      />
    </>
  )
}
