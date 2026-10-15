import { Text } from 'rsuite'
import type { PoolShare, Position } from '../../api/types'
import { formatPrice, formatTokenAmount } from '../../lib/format'
import { GlassPanel } from '../Glass'
import { StatusTag } from '../StatusTag'

const feeTier = new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 })

/**
 * The wallet's Uniswap V3 liquidity positions. Each is a deposit into one pool between two prices:
 * it earns a share of the pool's trading fees only while the price is inside that range, and the
 * mix of the two tokens it holds shifts as the price moves. Outside the range it earns nothing,
 * which is easy to miss, so that is tagged and said in words.
 *
 * Nothing shows for a wallet without positions, which is every wallet on a network that doesn't use
 * Uniswap V3. The assistant opens, adds to, collects from and closes positions; the dashboard only
 * shows them. Old Uniswap V2 LP tokens are still balances, in BalancesCard.
 */
export function PositionsCard({ positions }: { positions: Position[] }) {
  if (positions.length === 0) return null

  return (
    <GlassPanel bordered header={<h2>Liquidity positions</h2>} className="mf-card">
      <ul className="mf-position-list" aria-label="Liquidity positions">
        {positions.map(position => (
          <PositionItem key={position.id} position={position} />
        ))}
      </ul>
      <Text size="sm" muted>
        To collect fees, add to a position or take one out, ask the assistant.
      </Text>
    </GlassPanel>
  )
}

/** "1.2 ETH + 3,400 USDC", from the raw amounts, never the floats. */
function formatShares(shares: PoolShare[]) {
  return shares.map(share => `${formatTokenAmount(share.raw, share.decimals)} ${share.ticker.toUpperCase()}`).join(' + ')
}

function PositionItem({ position }: { position: Position }) {
  const unit = `${position.quote.toUpperCase()} per ${position.base.toUpperCase()}`
  const range =
    position.full_range || position.max_price === null
      ? 'Full range'
      : `${formatPrice(position.min_price)} – ${formatPrice(position.max_price)} ${unit}`
  const earned = position.fees.some(fee => BigInt(fee.raw) > 0n)

  return (
    <li>
      <div className="mf-token-row">
        <h3 className="mf-position-title">
          {position.pair.toUpperCase()} <span className="mf-position-fee">· {feeTier.format(position.fee_pct)}%</span>
        </h3>
        {position.in_range ? (
          <StatusTag tone="success">In range</StatusTag>
        ) : (
          <StatusTag tone="warning">Out of range</StatusTag>
        )}
      </div>
      <dl className="mf-position-facts">
        <dt>Range</dt>
        <dd className="mf-num">{range}</dd>
        <dt>Current price</dt>
        <dd className="mf-num">{`${formatPrice(position.current_price)} ${unit}`}</dd>
        <dt>Holds now</dt>
        <dd className="mf-num">{formatShares(position.amounts)}</dd>
        <dt>Unclaimed fees</dt>
        <dd className="mf-num">{earned ? formatShares(position.fees) : 'None yet'}</dd>
      </dl>
      {!position.in_range && (
        <Text size="sm" muted className="mf-position-note">
          It isn't earning fees until the price comes back into this range.
        </Text>
      )}
    </li>
  )
}
