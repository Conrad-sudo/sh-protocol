import { useState } from 'react'
import { Form, Placeholder, SelectPicker, Text } from 'rsuite'
import { parseUnits } from 'viem'
import type { TokenCap } from '../../api/types'
import { useTokens } from '../../hooks/useTokens'
import { formatTokenAmount } from '../../lib/format'
import { AddressText } from '../AddressText'
import { AmountInput } from '../AmountInput'
import { Explained, FieldError, FieldLabel } from '../Explained'
import { GlassPanel } from '../Glass'
import { QueryError } from '../QueryError'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import type { ControlPanelProps } from './types'

/** The wallet's own limit on how many tokens can be capped this way (MAX_CAPPED_TOKENS). */
const MAX_TOKEN_CAPS = 8

/** A figure of a cap: in whole tokens, or in base units for a token whose decimals can't be read. */
function capAmount(raw: string, cap: TokenCap): string {
  return cap.decimals === null ? `${raw} base units` : formatTokenAmount(raw, cap.decimals)
}

/**
 * Limits in a token's own units, instead of counting it toward the USD limit. A token with no price
 * can have no other kind. Setting one tightens the wallet, so it goes straight through unless it
 * raises a limit already there; raising or removing one asks first.
 */
export function TokenCapsPanel(props: ControlPanelProps) {
  const { wallet, tx, locked, ask } = props
  const caps = wallet.spending.token_caps

  return (
    <GlassPanel
      bordered
      header={
        <Explained level={2} title="Tokens with their own limit">
          Caps how many tokens can leave per period, whatever they're worth. Selling the token uses it too. A token
          counted above can't have one.
        </Explained>
      }
      className="mf-card"
    >
      {caps.length > 0 && (
        <ul className="mf-token-list" aria-label="Tokens with their own limit">
          {caps.map(cap => {
            const key = `token-cap:${cap.address}`
            const name = cap.ticker?.toUpperCase() ?? 'this token'
            return (
              <li key={cap.address}>
                <div className="mf-token-row">
                  <span className="mf-token">
                    {cap.ticker ? cap.ticker.toUpperCase() : <AddressText address={cap.address} />}
                  </span>
                  <span className="mf-token">
                    <Text size="sm" muted>
                      {capAmount(cap.remaining_raw, cap)} of {capAmount(cap.limit_raw, cap)} left
                    </Text>
                    <TxButton
                      tx={tx}
                      txKey={key}
                      size="sm"
                      appearance="ghost"
                      color="orange"
                      disabled={locked}
                      aria-label={`Remove the limit on ${name}`}
                      onClick={() =>
                        ask({
                          title: `Remove the limit on ${name}?`,
                          body: (
                            <Text>
                              The assistant will be able to move {name} out of this wallet without any limit.
                            </Text>
                          ),
                          confirmLabel: 'Remove limit',
                          request: {
                            key,
                            action: { kind: 'token-cap', token: cap.address, action: 'remove' },
                            success: `${name} no longer has a limit of its own.`,
                          },
                        })
                      }
                    >
                      Remove
                    </TxButton>
                  </span>
                </div>
                <TxStatus tx={tx} txKey={key} />
              </li>
            )
          })}
        </ul>
      )}
      <SetTokenCap key={caps.length} {...props} />
    </GlassPanel>
  )
}

function SetTokenCap({ wallet, tx, locked, start, ask }: ControlPanelProps) {
  const tokens = useTokens(wallet.chain_id)
  const [address, setAddress] = useState<string | null>(null)
  const [limit, setLimit] = useState('')

  if (tokens.isPending) return <Placeholder.Paragraph rows={1} active />
  if (tokens.isError) {
    return <QueryError what="the token list" onRetry={() => void tokens.refetch()} className="mf-settings-note" />
  }

  const caps = wallet.spending.token_caps
  const capOf = (candidate: string) => caps.find(c => c.address.toLowerCase() === candidate.toLowerCase())
  const watched = new Set(wallet.spending.watched_tokens.map(t => t.address.toLowerCase()))
  // The wrapped native token counts in USD or not at all, with the native one: never a limit of its own.
  const followers = new Set(tokens.data.filter(t => t.follows_native).map(t => t.address.toLowerCase()))
  const full = caps.length >= MAX_TOKEN_CAPS

  // Listed tokens and the ones the user added. A token that counts in USD has to stop counting first,
  // and once the wallet's list is full, only a limit already on it can change.
  const named = new Map<string, string>()
  for (const token of tokens.data) named.set(token.address, token.ticker)
  for (const balance of wallet.balances) {
    if (balance.custom && balance.address) named.set(balance.address, balance.ticker)
  }
  for (const cap of caps) {
    if (cap.ticker) named.set(cap.address, cap.ticker)
  }
  const choices = [...named]
    .filter(([candidate]) => !watched.has(candidate.toLowerCase()) && !followers.has(candidate.toLowerCase()))
    .filter(([candidate]) => !full || capOf(candidate) !== undefined)
    .map(([value, ticker]) => ({ value, label: ticker.toUpperCase() }))

  if (choices.length === 0) {
    return (
      <Text size="sm" muted className="mf-settings-note">
        {full ? `A wallet can limit at most ${MAX_TOKEN_CAPS} tokens this way.` : 'No token can take a limit right now.'}
      </Text>
    )
  }

  const chosen = choices.find(c => c.value === address)
  const decimals = wallet.balances.find(b => b.address === address)?.decimals ?? capOf(address ?? '')?.decimals ?? null
  const tooPrecise = decimals !== null && (limit.split('.')[1] ?? '').length > decimals
  const valid = chosen !== undefined && /^\d*\.?\d*$/.test(limit) && /\d/.test(limit) && !tooPrecise

  const save = () => {
    if (!valid || !chosen) return
    const name = chosen.label
    const existing = capOf(chosen.value)
    const request = {
      key: 'token-cap-set',
      action: { kind: 'token-cap', token: chosen.value, action: 'set', limit },
      success: `${name} is limited to ${limit} per period.`,
    } as const
    const raises =
      existing !== undefined &&
      (existing.decimals === null || parseUnits(limit, existing.decimals) > BigInt(existing.limit_raw))
    if (raises) {
      ask({
        title: `Raise the limit on ${name}?`,
        body: (
          <Text>
            The assistant will be able to move up to {limit} {name} per period, up from{' '}
            {capAmount(existing.limit_raw, existing)}.
          </Text>
        ),
        confirmLabel: 'Raise limit',
        request,
      })
    } else {
      start(request)
    }
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={save}>
      <Form.Group controlId="token-cap-token">
        <Form.Label>Token</Form.Label>
        <SelectPicker
          id="token-cap-token"
          // A <label for> can't name the picker's div, so point at the label RSuite rendered.
          aria-labelledby="token-cap-token-label"
          placeholder="Choose a token"
          block
          searchable={false}
          data={choices}
          value={address}
          onChange={setAddress}
          disabled={locked}
        />
      </Form.Group>
      <Form.Group controlId="token-cap-limit">
        <FieldLabel help="The most that can leave per period. 0 stops it moving.">Token limit per period</FieldLabel>
        <div className="mf-inline-field">
          <AmountInput
            id="token-cap-limit"
            value={limit}
            onChange={setLimit}
            unit={chosen?.label}
            disabled={locked}
          />
          <TxButton tx={tx} txKey="token-cap-set" type="submit" appearance="primary" disabled={locked || !valid}>
            Set limit
          </TxButton>
        </div>
        {tooPrecise && (
          <FieldError>
            {chosen?.label ?? 'This token'} can't be split into more than {decimals} decimal places.
          </FieldError>
        )}
      </Form.Group>
      <TxStatus tx={tx} txKey="token-cap-set" />
    </Form>
  )
}
