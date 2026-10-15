import { useState } from 'react'
import ArrowDownLineIcon from '@rsuite/icons/ArrowDownLine'
import { type Address, formatEther, getAddress, isAddress, parseEther } from 'viem'
import { Form, Input, Text } from 'rsuite'
import type { Chain, Exchange, WalletState } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { formatTimeLeft, formatTokenAmount, isValidAmount } from '../../lib/format'
import { AddressText } from '../AddressText'
import { AmountInput } from '../AmountInput'
import { GlassPanel } from '../Glass'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import { AllowlistRow } from './AllowlistRow'
import { ControlRow } from './ControlRow'
import type { Confirmation, ControlPanelProps } from './types'

const MAX_TRUSTED_SPENDERS = 16

/** What each of a network's default spenders is, in the order GET /api/chains lists them. */
const DEFAULT_SPENDER_NAMES: Record<Exchange, string[]> = {
  'uniswap-v3': ['Uniswap V3 router', 'Uniswap V3 positions'],
  'pancakeswap-v2': ['PancakeSwap router'],
}

interface DefaultSpender {
  address: Address
  name: string
}

/** The exchange contracts every new wallet on `chain` is deployed trusting, with their names. */
function defaultSpenders(chain: Chain | undefined): DefaultSpender[] {
  const names = (chain?.exchange && DEFAULT_SPENDER_NAMES[chain.exchange]) || []
  return (chain?.default_spenders ?? []).map((address, index) => ({
    address: getAddress(address),
    name: names[index] ?? 'Exchange contract',
  }))
}

/** "The Uniswap V3 router and Uniswap V3 positions are trusted by default, …" */
function trustedByDefault(names: string[]) {
  const one = names.length === 1
  return `The ${names.join(' and ')} ${one ? 'is' : 'are'} trusted by default, as the assistant uses ${one ? 'it' : 'them'} to swap and to add or remove liquidity.`
}

/**
 * Settings most people never need: trusted spenders, the network fee cap and daily budget, the allowlist.
 */
export function AdvancedPanel(props: ControlPanelProps & { chain: Chain | undefined }) {
  const { wallet, tx, locked, start, ask, chain } = props
  const spenders = wallet.limits.trusted_spenders
  const trusted = new Set(spenders.map(spender => getAddress(spender)))
  // The exchange contracts new wallets start out trusting stay off the list, so they have no Remove
  // button: the assistant needs them to swap and to add or remove liquidity, and one stray click
  // would break that.
  const defaults = defaultSpenders(chain)
  const defaultAddresses = new Set(defaults.map(d => d.address))
  const shown = spenders.filter(spender => !defaultAddresses.has(getAddress(spender)))
  const trustedDefaults = defaults.filter(d => trusted.has(d.address))
  // A wallet made before its network moved from Uniswap V2 to V3 trusts only the old router.
  const missingDefaults = defaults.filter(d => !trusted.has(d.address))
  const full = spenders.length >= MAX_TRUSTED_SPENDERS
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
    <GlassPanel bordered header={header} bodyFill={!open} className="mf-card">
      <div id="advanced-controls" hidden={!open}>
        <ControlRow
          title="Trusted spenders"
          help="Contracts that may be approved for tokens Mitfah can't price, such as liquidity-pool tokens."
        >
          {spenders.length === 0 ? (
            <Text size="sm" muted>
              None.
            </Text>
          ) : null}
          {shown.length > 0 ? (
            <ul className="mf-token-list" aria-label="Trusted spenders">
              {shown.map(spender => {
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
          ) : null}
          {trustedDefaults.length > 0 ? (
            <Text size="sm" muted>
              {trustedByDefault(trustedDefaults.map(spender => spender.name))}
            </Text>
          ) : null}
          {missingDefaults.length > 0 ? (
            <>
              <ul className="mf-token-list" aria-label="Spenders new wallets trust">
                {missingDefaults.map(spender => {
                  const key = `spender-add:${spender.address}`
                  return (
                    <li key={spender.address}>
                      <div className="mf-token-row">
                        <span className="mf-token">
                          {spender.name}
                          <AddressText address={spender.address} />
                        </span>
                        <span className="mf-token">
                          <StatusTag>Not trusted yet</StatusTag>
                          <TxButton
                            tx={tx}
                            txKey={key}
                            size="sm"
                            appearance="ghost"
                            color="orange"
                            disabled={locked || full}
                            aria-label={`Trust ${spender.name}`}
                            onClick={() => ask(trustConfirmation(spender.address, key, spender.name))}
                          >
                            Trust
                          </TxButton>
                        </span>
                      </div>
                      <TxStatus tx={tx} txKey={key} />
                    </li>
                  )
                })}
              </ul>
              <Text size="sm" muted>
                {missingDefaults.length === 1
                  ? 'New wallets trust it from the start. Without it,'
                  : 'New wallets trust these from the start. Without them,'}{' '}
                the assistant can only swap and provide liquidity with tokens Mitfah can price.
              </Text>
            </>
          ) : null}
          <AddSpender key={spenders.length} {...props} />
        </ControlRow>

        <ControlRow
          title="Network fee cap"
          help="The most one assistant operation may cost the wallet in network fees. Fees don't count toward your spending limit, so this caps them separately. Set it too low and the assistant's operations fail."
        >
          <GasCapEditor key={wallet.limits.max_op_gas_cost_wei} {...props} ticker={chain?.native_ticker ?? 'ETH'} />
        </ControlRow>

        <ControlRow
          title="Daily fee budget"
          help="The most all the assistant's operations together may cost the wallet in network fees in a day. Once it's used up, the assistant stops until it refills."
        >
          {wallet.limits.session_gas ? (
            <GasBudgetEditor
              key={wallet.limits.session_gas.budget_wei}
              {...props}
              budget={wallet.limits.session_gas}
              ticker={chain?.native_ticker ?? 'ETH'}
            />
          ) : (
            <Text size="sm" muted>
              This wallet was made before daily fee budgets existed.
            </Text>
          )}
        </ControlRow>

        <AllowlistRow {...props} open={open} />
      </div>
    </GlassPanel>
  )
}

/**
 * Trusting a spender loosens the limit, so it always asks first. A default spender is named, and
 * known to every new wallet; any other address comes with a warning to trust only what you know.
 */
function trustConfirmation(spender: string, key: string, name?: string): Confirmation {
  return {
    title: 'Trust this spender?',
    body: (
      <>
        <Text>
          {name ? (
            <>
              {name} (<AddressText address={spender} />)
            </>
          ) : (
            <AddressText address={spender} />
          )}{' '}
          will be allowed approvals for tokens Mitfah can't price, and moving those isn't counted toward your limit.
        </Text>
        <Text className="mf-settings-note">
          {name
            ? 'New wallets on this network trust it from the start.'
            : "Only trust a contract you know, such as your exchange's router."}
        </Text>
      </>
    ),
    confirmLabel: 'Trust spender',
    request: {
      key,
      action: { kind: 'trusted-spender', spender, action: 'add' },
      success: name ? `${name} trusted.` : 'Spender trusted.',
    },
  }
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
    ask(trustConfirmation(getAddress(address), 'spender-add'))
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

type GasBudget = NonNullable<WalletState['limits']['session_gas']>

function GasBudgetEditor({
  tx,
  locked,
  start,
  ask,
  budget,
  ticker,
}: ControlPanelProps & { budget: GasBudget; ticker: string }) {
  const now = useNow(60_000)
  const currentWei = BigInt(budget.budget_wei)
  const current = formatEther(currentWei)
  const [value, setValue] = useState(current)
  const valid = isValidAmount(value) && parseEther(value) > 0n
  const changed = valid && parseEther(value) !== currentWei

  // What's used comes back all at once, a day after the first fee of the day.
  const refillsAt = budget.refills_at !== null && budget.refills_at * 1_000 > now ? budget.refills_at * 1_000 : null
  const usage =
    refillsAt === null
      ? `All ${formatTokenAmount(budget.budget_wei, 18)} ${ticker} left.`
      : `${formatTokenAmount(budget.remaining_wei, 18)} of ${formatTokenAmount(budget.budget_wei, 18)} ${ticker} left. Refills in ${formatTimeLeft(refillsAt - now)}.`

  const save = () => {
    if (!changed) return
    const request = {
      key: 'gas-budget',
      action: { kind: 'gas-budget', budgetEth: value },
      success: `Daily fee budget set to ${value} ${ticker}.`,
    } as const
    if (parseEther(value) > currentWei) {
      ask({
        title: 'Raise the daily fee budget?',
        body: (
          <Text>
            The assistant's operations will be able to cost the wallet up to {value} {ticker} in fees a day, up from{' '}
            {current} {ticker}. Fees don't count toward your spending limit.
          </Text>
        ),
        confirmLabel: 'Raise budget',
        request,
      })
    } else {
      start(request)
    }
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={save}>
      <Text size="sm" muted className="mf-num">
        {usage}
      </Text>
      <Form.Group controlId="gas-budget">
        <Form.Label>Most per day</Form.Label>
        <div className="mf-inline-field">
          <AmountInput id="gas-budget" value={value} unit={ticker} disabled={locked} onChange={setValue} />
          <TxButton tx={tx} txKey="gas-budget" type="submit" appearance="primary" disabled={locked || !changed}>
            Save
          </TxButton>
        </div>
        {!valid && <Form.Text className="mf-error-text">Enter an amount above zero, like 0.02.</Form.Text>}
      </Form.Group>
      <TxStatus tx={tx} txKey="gas-budget" />
    </Form>
  )
}
