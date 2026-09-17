import { Panel, Text } from 'rsuite'
import type { WalletState } from '../../api/types'
import { formatTokenAmount } from '../../lib/format'
import { StatusTag } from '../StatusTag'

/**
 * What the wallet holds. The native token always counts toward the spending limit; an ERC-20 counts
 * only if it is watched, so an unwatched one is flagged — the assistant could move it without limit.
 * A token the server could not read shows as such instead of hiding the rest.
 */
export function BalancesCard({ wallet }: { wallet: WalletState }) {
  const watched = new Set(wallet.spending.watched_tokens.map(t => t.address.toLowerCase()))
  const counts = (address: string | null, native: boolean) =>
    native || (address !== null && watched.has(address.toLowerCase()))
  const anyUnlimited = wallet.balances.some(b => !counts(b.address, b.native))

  return (
    <Panel bordered header={<h2>Balances</h2>} className="mf-card">
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
          {wallet.balances.map(balance => (
            <tr key={balance.address ?? 'native'}>
              <th scope="row">
                <span className="mf-token">
                  {balance.ticker.toUpperCase()}
                  {!counts(balance.address, balance.native) && <StatusTag tone="warning">Not limited</StatusTag>}
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
          ))}
        </tbody>
      </table>
      {anyUnlimited && (
        <Text size="sm" muted>
          Tokens marked "Not limited" don't count toward your spending limit. You can add them in Controls.
        </Text>
      )}
    </Panel>
  )
}
