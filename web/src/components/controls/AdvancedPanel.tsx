import { useState } from 'react'
import ArrowDownLineIcon from '@rsuite/icons/ArrowDownLine'
import { formatEther, getAddress, isAddress, parseEther } from 'viem'
import { Form, Input, Panel, Text } from 'rsuite'
import type { Chain } from '../../api/types'
import { isValidAmount } from '../../lib/format'
import { AddressText } from '../AddressText'
import { AmountInput } from '../AmountInput'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import { ControlRow } from './ControlRow'
import type { ControlPanelProps } from './types'

const MAX_TRUSTED_SPENDERS = 16

/** Settings most people never need: trusted spenders, the per-operation fee cap, the allowlist. */
export function AdvancedPanel(props: ControlPanelProps & { chain: Chain | undefined }) {
  const { wallet, tx, locked, start, chain } = props
  const spenders = wallet.limits.trusted_spenders
  const [open, setOpen] = useState(false)

  // A heading holding a toggle button, not RSuite's collapsible Panel: that one puts the heading
  // inside its button, which hides the heading from screen readers.
  const header = (
    <h2>
      <button
        type="button"
        className="mf-disclosure"
        aria-expanded={open}
        aria-controls="advanced-controls"
        onClick={() => setOpen(o => !o)}
      >
        Advanced
        <ArrowDownLineIcon aria-hidden className="mf-disclosure-caret" />
      </button>
    </h2>
  )

  return (
    <Panel bordered header={header} bodyFill={!open} className="mf-card">
      <div id="advanced-controls" hidden={!open}>
        <ControlRow
          title="Trusted spenders"
          help="Contracts that may be approved for tokens Mitfah can't price, such as liquidity-pool tokens. Your exchange's router is usually the only one needed."
        >
          {spenders.length === 0 ? (
            <Text size="sm" muted>
              None.
            </Text>
          ) : (
            <ul className="mf-token-list" aria-label="Trusted spenders">
              {spenders.map(spender => {
                const key = `spender:${spender}`
                return (
                  <li key={spender}>
                    <div className="mf-token-row">
                      <AddressText address={spender} />
                      <TxButton
                        tx={tx}
                        txKey={key}
                        size="sm"
                        appearance="subtle"
                        disabled={locked}
                        aria-label={`Stop trusting ${spender}`}
                        onClick={() =>
                          start({
                            key,
                            action: { kind: 'trusted-spender', spender, action: 'remove' },
                            success: 'Spender removed.',
                          })
                        }
                      >
                        Remove
                      </TxButton>
                    </div>
                    <TxStatus tx={tx} txKey={key} />
                  </li>
                )
              })}
            </ul>
          )}
          <AddSpender key={spenders.length} {...props} />
        </ControlRow>

        <ControlRow
          title="Network fee cap"
          help="The most one assistant operation may cost the wallet in network fees. Fees don't count toward your spending limit, so this caps them separately. Set it too low and the assistant's operations fail."
        >
          <GasCapEditor key={wallet.limits.max_op_gas_cost_wei} {...props} ticker={chain?.native_ticker ?? 'ETH'} />
        </ControlRow>

        <ControlRow
          title="Contract allowlist"
          status={
            wallet.limits.allowlist_enabled ? <StatusTag tone="success">On</StatusTag> : <StatusTag tone="neutral">Off</StatusTag>
          }
          help="When on, the assistant may only call contracts on the wallet's list. It can't be changed here yet."
        />
      </div>
    </Panel>
  )
}

function AddSpender({ wallet, tx, locked, ask }: ControlPanelProps) {
  const [address, setAddress] = useState('')
  const spenders = wallet.limits.trusted_spenders
  const valid = isAddress(address, { strict: false })
  const already = valid && spenders.some(s => getAddress(s) === getAddress(address))
  const full = spenders.length >= MAX_TRUSTED_SPENDERS

  let problem: string | null = null
  if (address !== '' && !valid) problem = 'Enter a full address starting with 0x.'
  else if (already) problem = 'Already trusted.'
  else if (full) problem = `A wallet can trust at most ${MAX_TRUSTED_SPENDERS} spenders.`

  const add = () => {
    if (!valid || already || full) return
    const spender = getAddress(address)
    ask({
      title: 'Trust this spender?',
      body: (
        <>
          <Text>
            <AddressText address={spender} /> will be allowed approvals for tokens Mitfah can't price, and moving
            those isn't counted toward your limit.
          </Text>
          <Text className="mf-settings-note">Only trust a contract you know, such as your exchange's router.</Text>
        </>
      ),
      confirmLabel: 'Trust spender',
      request: {
        key: 'spender-add',
        action: { kind: 'trusted-spender', spender, action: 'add' },
        success: 'Spender trusted.',
      },
    })
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={add}>
      <Form.Group controlId="add-spender">
        <Form.Label>Trust a spender</Form.Label>
        <div className="mf-inline-field">
          <Input
            id="add-spender"
            className="mf-mono"
            placeholder="0x…"
            autoComplete="off"
            spellCheck={false}
            value={address}
            disabled={locked}
            onChange={value => setAddress(value.trim())}
          />
          <TxButton
            tx={tx}
            txKey="spender-add"
            type="submit"
            appearance="ghost"
            color="orange"
            disabled={locked || !valid || already || full}
          >
            Trust
          </TxButton>
        </div>
        {problem && <Form.Text className="mf-error-text">{problem}</Form.Text>}
      </Form.Group>
      <TxStatus tx={tx} txKey="spender-add" />
    </Form>
  )
}

function GasCapEditor({ wallet, tx, locked, start, ask, ticker }: ControlPanelProps & { ticker: string }) {
  const currentWei = BigInt(wallet.limits.max_op_gas_cost_wei)
  const current = formatEther(currentWei)
  const [value, setValue] = useState(current)
  const valid = isValidAmount(value) && parseEther(value) > 0n
  const changed = valid && parseEther(value) !== currentWei

  const save = () => {
    if (!changed) return
    const request = {
      key: 'max-gas',
      action: { kind: 'max-gas', maxCostEth: value },
      success: `Network fee cap set to ${value} ${ticker}.`,
    } as const
    if (parseEther(value) > currentWei) {
      ask({
        title: 'Raise the network fee cap?',
        body: (
          <Text>
            A single assistant operation will be able to cost the wallet up to {value} {ticker} in fees, up from{' '}
            {current} {ticker}. Fees don't count toward your spending limit.
          </Text>
        ),
        confirmLabel: 'Raise cap',
        request,
      })
    } else {
      start(request)
    }
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={save}>
      <Form.Group controlId="max-gas">
        <Form.Label>Most per operation</Form.Label>
        <div className="mf-inline-field">
          <AmountInput id="max-gas" value={value} unit={ticker} disabled={locked} onChange={setValue} />
          <TxButton tx={tx} txKey="max-gas" type="submit" appearance="primary" disabled={locked || !changed}>
            Save
          </TxButton>
        </div>
        {!valid && <Form.Text className="mf-error-text">Enter an amount above zero, like 0.01.</Form.Text>}
      </Form.Group>
      <TxStatus tx={tx} txKey="max-gas" />
    </Form>
  )
}
