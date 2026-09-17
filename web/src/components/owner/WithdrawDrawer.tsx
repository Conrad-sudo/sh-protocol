import { useState } from 'react'
import { formatUnits, getAddress, isAddress, parseUnits } from 'viem'
import { Button, Drawer, Form, Input, Message, SelectPicker, Text } from 'rsuite'
import type { Chain, TokenBalance, WalletState } from '../../api/types'
import type { OwnerActionHandle } from '../../hooks/useOwnerAction'
import { useOwnerReadiness } from '../../hooks/useOwnerReadiness'
import { useLayoutMode } from '../../layouts/useLayoutMode'
import { formatTokenAmount, isValidAmount } from '../../lib/format'
import { AddressText } from '../AddressText'
import { AmountInput } from '../AmountInput'
import { ConfirmModal } from './ConfirmModal'
import { OwnerWalletBar } from './OwnerWalletBar'
import { TxButton } from './TxButton'
import { TxStatus } from './TxStatus'

const KEY = 'withdraw'

interface WithdrawDrawerProps {
  open: boolean
  onClose: () => void
  wallet: WalletState
  chain: Chain | undefined
  tx: OwnerActionHandle
}

type Readable = TokenBalance & { raw: string; decimals: number }

/** Why `amount` can't be withdrawn from `balance`, or null when it can. Empty input isn't an error. */
function amountProblem(amount: string, balance: Readable | undefined): string | null {
  if (amount === '' || !balance) return null
  if (!isValidAmount(amount)) return 'Enter an amount like 0.5.'
  const ticker = balance.ticker.toUpperCase()
  if ((amount.split('.')[1] ?? '').length > balance.decimals) {
    return `${ticker} can't be split into more than ${balance.decimals} decimal places.`
  }
  const units = parseUnits(amount, balance.decimals)
  if (units === 0n) return 'Enter more than zero.'
  if (units > BigInt(balance.raw)) {
    return `The wallet holds only ${formatTokenAmount(balance.raw, balance.decimals)} ${ticker}.`
  }
  return null
}

/**
 * Moves funds out of the wallet to an address the owner chooses — by default the owner's own. It
 * skips the spending limit and works even while the wallet is paused: it is the owner's way out.
 */
export function WithdrawDrawer({ open, onClose, wallet, chain, tx }: WithdrawDrawerProps) {
  const mode = useLayoutMode()
  const readiness = useOwnerReadiness(wallet)
  const readable = wallet.balances.filter((b): b is Readable => b.raw !== null && b.decimals !== null)
  const [ticker, setTicker] = useState(readable[0]?.ticker ?? '')
  const [amount, setAmount] = useState('')
  const [to, setTo] = useState(wallet.owner)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const balance = readable.find(b => b.ticker === ticker)
  const amountError = amountProblem(amount, balance)
  const toValid = isAddress(to, { strict: false })
  const toOwner = toValid && getAddress(to) === getAddress(wallet.owner)
  const inFlight = tx.busy && tx.state.key === KEY
  const canSubmit = readiness === 'ready' && !!balance && amount !== '' && !amountError && toValid

  const clearResult = () => {
    if (!tx.busy && tx.state.key === KEY) tx.reset()
  }

  const close = () => {
    // A withdrawal in flight keeps going; reopening shows how it's doing.
    if (!inFlight) {
      clearResult()
      setAmount('')
    }
    onClose()
  }

  const withdraw = () => {
    if (!balance) return
    void tx.run({
      key: KEY,
      action: { kind: 'withdraw', token: balance.ticker, amount, to: getAddress(to) },
      success: `Withdrew ${amount} ${balance.ticker.toUpperCase()}.`,
    })
  }

  return (
    <Drawer
      open={open}
      onClose={close}
      placement={mode === 'mobile' ? 'bottom' : 'right'}
      size={mode === 'mobile' ? 'full' : 'xs'}
      className="mf-fund-drawer"
    >
      <Drawer.Header>
        <Drawer.Title>Withdraw</Drawer.Title>
      </Drawer.Header>
      <Drawer.Body>
        <Text muted>
          Move funds out of your Mitfah wallet. Withdrawals don't count toward the spending limit
          {wallet.paused ? ', and they work while the wallet is paused.' : '.'}
        </Text>
        <OwnerWalletBar wallet={wallet} readiness={readiness} fork={chain?.fork ?? false} />

        <Form fluid className="mf-auth-form" disabled={readiness !== 'ready' || inFlight}>
          <Form.Group controlId="withdraw-token">
            <Form.Label>Token</Form.Label>
            <SelectPicker
              id="withdraw-token"
              // A <label for> can't name the picker's div, so point at the label RSuite rendered.
              aria-labelledby="withdraw-token-label"
              block
              cleanable={false}
              searchable={false}
              value={ticker}
              data={readable.map(b => ({
                value: b.ticker,
                label: `${b.ticker.toUpperCase()} · ${formatTokenAmount(b.raw, b.decimals)} available`,
              }))}
              onChange={value => {
                setTicker(value ?? '')
                setAmount('')
                clearResult()
              }}
              disabled={readiness !== 'ready' || inFlight}
            />
          </Form.Group>
          <Form.Group controlId="withdraw-amount">
            <Form.Label>Amount</Form.Label>
            <AmountInput
              id="withdraw-amount"
              value={amount}
              unit={balance?.ticker.toUpperCase()}
              max={balance ? formatUnits(BigInt(balance.raw), balance.decimals) : undefined}
              disabled={readiness !== 'ready' || inFlight}
              onChange={value => {
                setAmount(value)
                clearResult()
              }}
            />
            {amountError && <Form.Text className="mf-error-text">{amountError}</Form.Text>}
          </Form.Group>
          <Form.Group controlId="withdraw-to">
            <Form.Label>Send to</Form.Label>
            <Input
              id="withdraw-to"
              className="mf-mono"
              autoComplete="off"
              spellCheck={false}
              value={to}
              disabled={readiness !== 'ready' || inFlight}
              onChange={value => {
                setTo(value.trim())
                clearResult()
              }}
            />
            {to !== '' && !toValid ? (
              <Form.Text className="mf-error-text">Enter a full address starting with 0x.</Form.Text>
            ) : toOwner ? (
              <Form.Text>Your owner wallet.</Form.Text>
            ) : (
              <Form.Text>
                Not your owner wallet.{' '}
                <Button appearance="link" size="sm" onClick={() => setTo(wallet.owner)}>
                  Send to my owner wallet instead
                </Button>
              </Form.Text>
            )}
          </Form.Group>
        </Form>

        <TxButton
          tx={tx}
          txKey={KEY}
          appearance="primary"
          className="mf-step-action"
          disabled={!canSubmit}
          onClick={() => (toOwner ? withdraw() : setConfirmOpen(true))}
        >
          Withdraw
        </TxButton>
        <TxStatus tx={tx} txKey={KEY} doneText="Sent. The balance above is up to date." />

        <ConfirmModal
          open={confirmOpen}
          title="Send to another address?"
          confirmLabel="Withdraw"
          tone="warning"
          onConfirm={withdraw}
          onClose={() => setConfirmOpen(false)}
        >
          <Text>
            You're sending {amount} {balance?.ticker.toUpperCase()} to{' '}
            {toValid && <AddressText address={getAddress(to)} />}, which isn't your owner wallet.
          </Text>
          <Message type="warning" showIcon className="mf-settings-note">
            Check the address carefully. Transfers can't be undone.
          </Message>
        </ConfirmModal>
      </Drawer.Body>
    </Drawer>
  )
}
