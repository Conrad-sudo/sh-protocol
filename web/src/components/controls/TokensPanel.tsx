import { useState } from 'react'
import { Button, Form, Message, Panel, Placeholder, SelectPicker, Text } from 'rsuite'
import type { Chain } from '../../api/types'
import { useTokens } from '../../hooks/useTokens'
import { AddressText } from '../AddressText'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import type { ControlPanelProps } from './types'

/**
 * Which tokens count toward the limit. The native token always does. An ERC-20 that isn't counted
 * can be moved by the assistant without limit, so removing one asks first; adding one doesn't.
 */
export function TokensPanel(props: ControlPanelProps & { chain: Chain | undefined }) {
  const { wallet, tx, locked, ask, chain } = props
  const watched = wallet.spending.watched_tokens

  return (
    <Panel bordered header={<h2>Tokens that count toward the limit</h2>} className="mf-card">
      <ul className="mf-token-list" aria-label="Counted tokens">
        <li>
          <div className="mf-token-row">
            <span className="mf-token">{chain?.native_ticker ?? 'Native token'}</span>
            <StatusTag tone="success">Always counts</StatusTag>
          </div>
        </li>
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
      <AddToken key={watched.length} {...props} />
    </Panel>
  )
}

function AddToken({ wallet, tx, locked, start }: ControlPanelProps) {
  const tokens = useTokens(wallet.chain_id)
  const [ticker, setTicker] = useState<string | null>(null)
  const watched = new Set(wallet.spending.watched_tokens.map(t => t.address.toLowerCase()))
  const addable = tokens.data?.filter(t => !watched.has(t.address.toLowerCase())) ?? []

  if (tokens.isPending) return <Placeholder.Paragraph rows={1} active />
  if (tokens.isError) {
    return (
      <Message type="error" showIcon className="mf-settings-note">
        Couldn't load the token list.{' '}
        <Button appearance="link" size="sm" onClick={() => void tokens.refetch()}>
          Try again
        </Button>
      </Message>
    )
  }
  if (addable.length === 0) {
    return (
      <Text size="sm" muted className="mf-settings-note">
        Every token Mitfah lists on this network already counts.
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

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={add}>
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
            disabled={locked}
          />
          <TxButton tx={tx} txKey="watched-add" type="submit" appearance="primary" disabled={locked || !ticker}>
            Add
          </TxButton>
        </div>
      </Form.Group>
      <TxStatus tx={tx} txKey="watched-add" />
    </Form>
  )
}
