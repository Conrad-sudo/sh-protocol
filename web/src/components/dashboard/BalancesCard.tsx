import { useState } from 'react'
import { useMutation, useMutationState, useQueryClient } from '@tanstack/react-query'
import PlusIcon from '@rsuite/icons/Plus'
import { Button, Message, Text, useToaster } from 'rsuite'
import { ApiError } from '../../api/client'
import type { Chain, TokenBalance, WalletState } from '../../api/types'
import { removeCustomToken } from '../../api/wallet'
import type { OwnerActionHandle } from '../../hooks/useOwnerAction'
import { formatTokenAmount } from '../../lib/format'
import { errorText } from '../../lib/tx'
import { chainName } from '../../wallet/chains'
import { Explained } from '../Explained'
import { GlassPanel } from '../Glass'
import { StatusTag } from '../StatusTag'
import { AddTokenModal } from './AddTokenModal'
import { RemoveTokenModal } from './RemoveTokenModal'

const REMOVE_KEY = ['custom-token', 'remove'] as const

interface BalancesCardProps {
  wallet: WalletState
  chain: Chain | undefined
  /** The page's owner-transaction handle: counting a token, or stopping, needs the owner's signature. */
  tx: OwnerActionHandle
}

/**
 * What the wallet holds, for the tokens on the dashboard: the native token, the tokens chosen when
 * the wallet was made, and the ones added since. The native token counts toward the spending limit
 * if the owner has it counted; an ERC-20 counts only if it is watched or has a limit of its own. One
 * with neither is flagged, the native token included — the assistant could move it without limit.
 * Tokens Mitfah has no price for can only have a limit of their own and are marked so.
 *
 * Any token can be taken off the dashboard except the native token. One that counts has to stop
 * counting first, and the remove dialog walks through that. The wrapped native token (WETH, WBNB)
 * counts exactly while the native one does and can't be stopped on its own, so it has no Remove link
 * until the native token stops counting in Controls. One with a limit of its own has none either,
 * until that limit comes off there. A token the server could not read shows as such instead of
 * hiding the rest.
 *
 * LP tokens from the assistant's liquidity deposits show while the wallet holds some, with what they
 * hold in the pool. They come and go on their own, so they have no Remove link, and they aren't
 * flagged "Not limited": the assistant can only take them out of the pool, never send them.
 */
export function BalancesCard({ wallet, chain, tx }: BalancesCardProps) {
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [addOpen, setAddOpen] = useState(false)
  // A fresh dialog each time one opens.
  const [addKey, setAddKey] = useState(0)
  const [removeKey, setRemoveKey] = useState(0)
  // Kept after closing so the dialog's text doesn't vanish while it animates out.
  const [toRemove, setToRemove] = useState<TokenBalance | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)

  const watched = new Set(wallet.spending.watched_tokens.map(t => t.address.toLowerCase()))
  const counts = (balance: TokenBalance) =>
    balance.native
      ? wallet.spending.native_metered
      : balance.address !== null && watched.has(balance.address.toLowerCase())
  const capOf = (balance: TokenBalance) =>
    wallet.spending.token_caps.find(c => c.address.toLowerCase() === balance.address?.toLowerCase())
  const removable = (balance: TokenBalance) =>
    !balance.native && !(balance.follows_native && counts(balance)) && !balance.lp && !capOf(balance)
  const anyListedUnlimited = wallet.balances.some(b => !b.custom && !b.lp && !counts(b) && !capOf(b))
  const anyCustom = wallet.balances.some(b => b.custom)
  const anyLp = wallet.balances.some(b => b.lp)

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
      notify(
        removed ? 'success' : 'info',
        removed ? `${name} removed from your dashboard.` : `${name} was already removed.`,
      )
    },
    onError: (error, token) => notify('error', `Couldn't remove ${token.ticker.toUpperCase()}: ${errorText(error)}`),
  })
  const removing = useMutationState({
    filters: { mutationKey: REMOVE_KEY, status: 'pending' },
    select: mutation => (mutation.state.variables as TokenBalance).address,
  })

  return (
    <GlassPanel
      bordered
      header={
        <div className="mf-section-head mf-card-head">
          <Explained level={2} title="Balances">
            {[
              anyListedUnlimited && '“Not limited” tokens have no limit: set one in Controls.',
              anyCustom && "“No price” tokens can't count in dollars: give one its own limit in Controls.",
              anyLp && 'LP tokens are your share of a pool; under each is what it holds for you.',
            ]
              .filter(Boolean)
              .join(' ')}
          </Explained>
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
            const cap = capOf(balance)
            return (
              <tr key={balance.address ?? 'native'}>
                <th scope="row">
                  <span className="mf-token">
                    <span title={balance.name ?? undefined}>{name}</span>
                    {balance.custom && <StatusTag>No price</StatusTag>}
                    {cap && (
                      <StatusTag tone="success">
                        {cap.decimals === null ? 'Own limit' : `${formatTokenAmount(cap.remaining_raw, cap.decimals)} left`}
                      </StatusTag>
                    )}
                    {!counts(balance) && !cap && !balance.lp && <StatusTag tone="warning">Not limited</StatusTag>}
                    {removable(balance) && (
                      <Button
                        appearance="link"
                        size="xs"
                        className="mf-token-remove"
                        aria-label={`Remove ${name} from your dashboard`}
                        loading={removing.includes(balance.address)}
                        onClick={() => {
                          setToRemove(balance)
                          setRemoveKey(k => k + 1)
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
                  {balance.underlying && (
                    <Text size="sm" muted>
                      ≈{' '}
                      {balance.underlying
                        .map(share => `${formatTokenAmount(share.raw, share.decimals)} ${share.ticker.toUpperCase()}`)
                        .join(' + ')}
                    </Text>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <AddTokenModal
        // Prefixed: the two dialogs are siblings, and both counters reach the same numbers.
        key={`add-${addKey}`}
        open={addOpen}
        onClose={() => setAddOpen(false)}
        wallet={wallet}
        chain={chain}
        networkName={chainName(wallet.chain_id)}
        tx={tx}
        onAdded={token => notify('success', `${token.ticker.toUpperCase()} added. It now shows in your balances.`)}
      />
      {toRemove && (
        <RemoveTokenModal
          key={`remove-${removeKey}`}
          open={removeOpen}
          token={toRemove}
          counts={counts(toRemove)}
          wallet={wallet}
          chain={chain}
          tx={tx}
          onRemove={token => remove.mutate(token)}
          onClose={() => setRemoveOpen(false)}
        />
      )}
    </GlassPanel>
  )
}
