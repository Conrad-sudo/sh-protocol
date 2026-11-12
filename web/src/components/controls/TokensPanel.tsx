import { useState } from 'react'
import { Form, Placeholder, SelectPicker, Text } from 'rsuite'
import type { Chain, Token } from '../../api/types'
import { useTokens } from '../../hooks/useTokens'
import { limitApplies } from '../../lib/spending'
import { AddressText } from '../AddressText'
import { GlassPanel } from '../Glass'
import { QueryError } from '../QueryError'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import type { ControlPanelProps } from './types'

/**
 * Which tokens count toward the limit. All of it is the owner's choice, the native token included:
 * it has one switch here, and its wrapped form (WETH, WBNB) moves with it in the same transaction, so
 * the two always count together. Anything that isn't counted can be moved by the assistant without
 * limit, so stopping asks first; counting doesn't. A token with a limit of its own (TokenCapsPanel)
 * can't count here too.
 *
 * A wallet made with no limit holds $0 and counts nothing. Counting something there would stop the
 * assistant spending it at all, so the limit has to be set first.
 */
export function TokensPanel(props: ControlPanelProps & { chain: Chain | undefined }) {
  const { wallet, tx, locked, ask, chain } = props
  const tokens = useTokens(wallet.chain_id)
  const followers = (tokens.data ?? []).filter(t => t.follows_native)
  const followerAddresses = new Set(followers.map(t => t.address.toLowerCase()))
  const watched = wallet.spending.watched_tokens.filter(t => !followerAddresses.has(t.address.toLowerCase()))
  const needsLimit = wallet.spending.daily_limit_usd === 0 && !limitApplies(wallet.spending)

  return (
    <GlassPanel bordered header={<h2>Tokens that count toward the limit</h2>} className="mf-card">
      {/* The token list says which token goes with the native one. Until it's here, WETH would show as
          a token of its own, with a Remove the server refuses. */}
      {tokens.isPending ? (
        <Placeholder.Paragraph rows={2} active />
      ) : tokens.isError ? (
        <QueryError what="the token list" onRetry={() => void tokens.refetch()} />
      ) : (
        <>
          <ul className="mf-token-list" aria-label="Counted tokens">
            <NativeRow
              {...props}
              native={chain?.native_ticker ?? 'Native token'}
              followers={followers}
              needsLimit={needsLimit}
            />
            {watched.map(token => {
              const key = `watched:${token.ticker ?? token.address}`
              const name = token.ticker?.toUpperCase()
              return (
                <li key={token.address}>
                  <div className="mf-token-row">
                    <span className="mf-token">{name ?? <AddressText address={token.address} />}</span>
                    {name ? (
                      <TxButton
                        tx={tx}
                        txKey={key}
                        size="sm"
                        appearance="ghost"
                        color="orange"
                        disabled={locked}
                        aria-label={`Stop counting ${name}`}
                        onClick={() =>
                          ask({
                            title: `Stop counting ${name}?`,
                            body: (
                              <Text>
                                The assistant will be able to move {name} out of this wallet without any limit.
                              </Text>
                            ),
                            confirmLabel: `Stop counting ${name}`,
                            request: {
                              key,
                              action: { kind: 'watched-token', token: token.ticker!, action: 'remove' },
                              success: `${name} no longer counts toward your limit.`,
                            },
                          })
                        }
                      >
                        Remove
                      </TxButton>
                    ) : (
                      <Text size="sm" muted>
                        Not a token Mitfah lists, so it can't be removed here.
                      </Text>
                    )}
                  </div>
                  <TxStatus tx={tx} txKey={key} />
                </li>
              )
            })}
          </ul>
          {needsLimit && (
            <Text size="sm" muted className="mf-settings-note">
              Set a spending limit above first. With a $0 limit, anything you count couldn't be spent at all.
            </Text>
          )}
          <AddToken key={watched.length} {...props} needsLimit={needsLimit} />
        </>
      )}
      <Text size="sm" muted className="mf-settings-note">
        Tokens marked "No price" on the Dashboard can't count toward this limit, because Mitfah has no price for
        them. They can have a limit of their own below. A token you count here also shows on the Dashboard.
      </Text>
    </GlassPanel>
  )
}

/**
 * The native token's row: whether it counts, and the one button that changes it. The wrapped token is
 * named beside it because the same transaction moves both.
 *
 * The two can only be apart if the owner changed one on the wallet directly. The row then says which
 * way, and its button puts the wrapped token back in step with the native one.
 */
function NativeRow({
  wallet,
  tx,
  locked,
  ask,
  start,
  native,
  followers,
  needsLimit,
}: ControlPanelProps & { native: string; followers: Token[]; needsLimit: boolean }) {
  const key = 'native-metering'
  const metered = wallet.spending.native_metered
  const watched = new Set(wallet.spending.watched_tokens.map(t => t.address.toLowerCase()))
  const wrapped = followers.map(t => t.ticker.toUpperCase()).join(' and ')
  const both = wrapped ? `${native} and ${wrapped}` : native
  // "ETH and WETH count", "CELO counts".
  const s = wrapped ? '' : 's'
  const apart = followers.some(t => watched.has(t.address.toLowerCase()) !== metered)

  const count = (success: string) => start({ key, action: { kind: 'native-metering', metered: true }, success })
  const stop = (what: string, success: string) =>
    ask({
      title: `Stop counting ${what}?`,
      body: <Text>The assistant will be able to move {what} out of this wallet without any limit.</Text>,
      confirmLabel: `Stop counting ${what}`,
      request: { key, action: { kind: 'native-metering', metered: false }, success },
    })

  return (
    <li>
      <div className="mf-token-row">
        <span className="mf-token">{both}</span>
        <span className="mf-token">
          {apart ? (
            <StatusTag tone="warning">
              {metered ? `${wrapped} not counted yet` : `${wrapped} still counted`}
            </StatusTag>
          ) : (
            <StatusTag tone={metered ? 'success' : 'neutral'}>{metered ? 'Counted' : 'Not counted'}</StatusTag>
          )}
          {apart && metered ? (
            // Adding tightens the limit, so no second look is needed.
            <TxButton
              tx={tx}
              txKey={key}
              size="sm"
              appearance="primary"
              disabled={locked}
              aria-label={`Count ${wrapped} toward the limit`}
              onClick={() => count(`${wrapped} now counts toward your limit, like ${native}.`)}
            >
              Count it
            </TxButton>
          ) : apart ? (
            <TxButton
              tx={tx}
              txKey={key}
              size="sm"
              appearance="ghost"
              color="orange"
              disabled={locked}
              aria-label={`Stop counting ${wrapped}`}
              onClick={() => stop(wrapped, `${wrapped} no longer counts toward your limit, like ${native}.`)}
            >
              Stop counting
            </TxButton>
          ) : metered ? (
            <TxButton
              tx={tx}
              txKey={key}
              size="sm"
              appearance="ghost"
              color="orange"
              disabled={locked}
              aria-label={`Stop counting ${both}`}
              onClick={() => stop(both, `${both} no longer count${s} toward your limit.`)}
            >
              Stop counting
            </TxButton>
          ) : (
            <TxButton
              tx={tx}
              txKey={key}
              size="sm"
              appearance="primary"
              disabled={locked || needsLimit}
              aria-label={`Count ${both} toward the limit`}
              onClick={() => count(`${both} now count${s} toward your limit.`)}
            >
              Count
            </TxButton>
          )}
        </span>
      </div>
      <TxStatus tx={tx} txKey={key} />
    </li>
  )
}

function AddToken({ wallet, tx, locked, start, needsLimit }: ControlPanelProps & { needsLimit: boolean }) {
  const tokens = useTokens(wallet.chain_id)
  const [ticker, setTicker] = useState<string | null>(null)
  const watched = new Set(wallet.spending.watched_tokens.map(t => t.address.toLowerCase()))
  const capped = new Set(wallet.spending.token_caps.map(t => t.address.toLowerCase()))
  // The wrapped native token counts with the native one, on its row above. A token with a limit of its
  // own would be refused: the wallet takes one or the other.
  const addable =
    tokens.data?.filter(
      t => !t.follows_native && !watched.has(t.address.toLowerCase()) && !capped.has(t.address.toLowerCase()),
    ) ?? []

  if (tokens.isPending) return <Placeholder.Paragraph rows={1} active />
  if (tokens.isError) {
    return (
      <QueryError what="the token list" onRetry={() => void tokens.refetch()} className="mf-settings-note" />
    )
  }
  if (addable.length === 0) {
    return (
      <Text size="sm" muted className="mf-settings-note">
        {capped.size === 0
          ? 'Every other token Mitfah lists on this network already counts.'
          : 'Every other token Mitfah lists on this network already counts or has a limit of its own.'}
      </Text>
    )
  }

  const add = () => {
    if (!ticker) return
    start({
      key: 'watched-add',
      action: { kind: 'watched-token', token: ticker, action: 'add' },
      success: `${ticker.toUpperCase()} now counts toward your limit.`,
    })
  }
  const off = locked || needsLimit

  return (
    <Form fluid className="mf-control-form" disabled={off} onSubmit={add}>
      <Form.Group controlId="add-token">
        <Form.Label>Add a token</Form.Label>
        <div className="mf-inline-field">
          <SelectPicker
            id="add-token"
            // A <label for> can't name the picker's div, so point at the label RSuite rendered.
            aria-labelledby="add-token-label"
            placeholder="Choose a token"
            block
            searchable={false}
            data={addable.map(t => ({ value: t.ticker, label: t.ticker.toUpperCase() }))}
            value={ticker}
            onChange={setTicker}
            disabled={off}
          />
          <TxButton tx={tx} txKey="watched-add" type="submit" appearance="primary" disabled={off || !ticker}>
            Add
          </TxButton>
        </div>
      </Form.Group>
      <TxStatus tx={tx} txKey="watched-add" />
    </Form>
  )
}
