import { useState } from 'react'
import { useMutation, useMutationState, useQueryClient } from '@tanstack/react-query'
import PlusIcon from '@rsuite/icons/Plus'
import { Button, Message, Panel, Text, useToaster } from 'rsuite'
import { ApiError } from '../../api/client'
import type { TokenBalance, WalletState } from '../../api/types'
import { removeCustomToken } from '../../api/wallet'
import { formatTokenAmount } from '../../lib/format'
import { errorText } from '../../lib/tx'
import { chainName } from '../../wallet/chains'
import { ConfirmModal } from '../owner/ConfirmModal'
import { StatusTag } from '../StatusTag'
import { AddTokenModal } from './AddTokenModal'

const REMOVE_KEY = ['custom-token', 'remove'] as const

/**
 * What the wallet holds. The native token always counts toward the spending limit; an ERC-20 counts
 * only if it is watched, so an unwatched one is flagged — the assistant could move it without limit.
 * Tokens the user added by address can never count (Mitfah has no price for them) and are marked as
 * theirs, with a way to take them off the list. A token the server could not read shows as such
 * instead of hiding the rest.
 */
export function BalancesCard({ wallet }: { wallet: WalletState }) {
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [addOpen, setAddOpen] = useState(false)
  // A fresh form each time the dialog opens.
  const [addKey, setAddKey] = useState(0)
  // Kept after closing so the dialog's text doesn't vanish while it animates out.
  const [toRemove, setToRemove] = useState<TokenBalance | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)

  const watched = new Set(wallet.spending.watched_tokens.map(t => t.address.toLowerCase()))
  const counts = (balance: TokenBalance) =>
    balance.native || (balance.address !== null && watched.has(balance.address.toLowerCase()))
  const anyListedUnlimited = wallet.balances.some(b => !b.custom && !counts(b))
  const anyCustom = wallet.balances.some(b => b.custom)

  const notify = (type: 'success' | 'info' | 'error', text: string) =>
    toaster.push(
      <Message type={type} showIcon closable>
        {text}
      </Message>,
      { placement: 'topCenter', duration: type === 'error' ? 6000 : 3000 },
    )

  const remove = useMutation({
    mutationKey: REMOVE_KEY,
    mutationFn: async (token: TokenBalance) => {
      try {
        await removeCustomToken(wallet.chain_id, token.address!)
        return true
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return false
        throw error
      }
    },
    onSuccess: (removed, token) => {
      void queryClient.invalidateQueries({ queryKey: ['wallet', wallet.chain_id] })
      const name = token.ticker.toUpperCase()
      notify(removed ? 'success' : 'info', removed ? `${name} removed from your list.` : `${name} was already removed.`)
    },
    onError: (error, token) => notify('error', `Couldn't remove ${token.ticker.toUpperCase()}: ${errorText(error)}`),
  })
  const removing = useMutationState({
    filters: { mutationKey: REMOVE_KEY, status: 'pending' },
    select: mutation => (mutation.state.variables as TokenBalance).address,
  })

  return (
    <Panel
      bordered
      header={
        <div className="mf-section-head mf-card-head">
          <h2>Balances</h2>
          <Button
            size="sm"
            appearance="ghost"
            startIcon={<PlusIcon />}
            onClick={() => {
              setAddKey(k => k + 1)
              setAddOpen(true)
            }}
          >
            Add token
          </Button>
        </div>
      }
      className="mf-card"
    >
      <table className="mf-table">
        <caption className="mf-visually-hidden">Token balances</caption>
        <thead>
          <tr>
            <th scope="col">Token</th>
            <th scope="col" className="mf-table-end">
              Balance
            </th>
          </tr>
        </thead>
        <tbody>
          {wallet.balances.map(balance => {
            const name = balance.ticker.toUpperCase()
            return (
              <tr key={balance.address ?? 'native'}>
                <th scope="row">
                  <span className="mf-token">
                    <span title={balance.name ?? undefined}>{name}</span>
                    {balance.custom && <StatusTag>Added by you</StatusTag>}
                    {!counts(balance) && <StatusTag tone="warning">Not limited</StatusTag>}
                    {balance.custom && (
                      <Button
                        appearance="link"
                        size="xs"
                        className="mf-token-remove"
                        aria-label={`Remove ${name} from your list`}
                        loading={removing.includes(balance.address)}
                        onClick={() => {
                          setToRemove(balance)
                          setRemoveOpen(true)
                        }}
                      >
                        Remove
                      </Button>
                    )}
                  </span>
                </th>
                <td className="mf-table-end mf-num">
                  {balance.raw !== null && balance.decimals !== null ? (
                    formatTokenAmount(balance.raw, balance.decimals)
                  ) : (
                    <Text as="span" muted title={balance.error}>
                      Couldn't read
                    </Text>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {anyListedUnlimited && (
        <Text size="sm" muted>
          Tokens marked "Not limited" don't count toward your spending limit. You can add them in Controls.
        </Text>
      )}
      {anyCustom && (
        <Text size="sm" muted className={anyListedUnlimited ? 'mf-settings-note' : undefined}>
          Tokens you added have no price in Mitfah, so they never count toward your limit.
        </Text>
      )}
      <AddTokenModal
        key={addKey}
        open={addOpen}
        onClose={() => setAddOpen(false)}
        chainId={wallet.chain_id}
        networkName={chainName(wallet.chain_id)}
        onAdded={token => notify('success', `${token.ticker.toUpperCase()} added. It now shows in your balances.`)}
      />
      {toRemove && (
        <ConfirmModal
          open={removeOpen}
          title={`Remove ${toRemove.ticker.toUpperCase()}?`}
          confirmLabel="Remove"
          tone="warning"
          onConfirm={() => remove.mutate(toRemove)}
          onClose={() => setRemoveOpen(false)}
        >
          <Text>
            It disappears from your balances and the assistant stops recognising it. Any{' '}
            {toRemove.ticker.toUpperCase()} in the wallet stays there, and you can add it again at any time.
          </Text>
        </ConfirmModal>
      )}
    </Panel>
  )
}
