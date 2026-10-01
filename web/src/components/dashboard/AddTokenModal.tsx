import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getAddress } from 'viem'
import { Button, Checkbox, Form, Input, Message, Modal, Text } from 'rsuite'
import { bsc } from 'viem/chains'
import type { Chain, CustomToken, WalletState } from '../../api/types'
import { addCustomToken, lookupCustomToken } from '../../api/wallet'
import type { OwnerActionHandle } from '../../hooks/useOwnerAction'
import { useOwnerReadiness } from '../../hooks/useOwnerReadiness'
import { addressProblem } from '../../lib/contacts'
import { formatTokenAmount } from '../../lib/format'
import { errorText } from '../../lib/tx'
import { CopyButton } from '../CopyButton'
import { FullAddress } from '../FullAddress'
import { OwnerWalletBar } from '../owner/OwnerWalletBar'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'

const COUNT_KEY = 'add-token-count'

interface AddTokenModalProps {
  open: boolean
  onClose: () => void
  wallet: WalletState
  chain: Chain | undefined
  /** The network's name, for the "check it's on this network" hint. */
  networkName: string
  /** The page's owner-transaction handle, for counting a listed token toward the limit. */
  tx: OwnerActionHandle
  /** Called once a token is added and not being counted; counting announces itself when it confirms. */
  onAdded: (token: CustomToken) => void
}

/** What is wrong with `address` as a token's contract address, or null. */
function tokenAddressProblem(address: string): string | null {
  const problem = addressProblem(address)
  // The contacts wording talks about sending money there; for a token it's simply not one.
  if (problem?.startsWith('This is the zero address')) return "That's the zero address, not a token."
  return problem
}

/**
 * Adds a token to the dashboard by its contract address, MetaMask-style, in two steps: paste the
 * address, then check what the chain says it is before adding it. The server reads the token's
 * symbol and decimals — what shows it is an ERC-20 — and an address that can't answer both gets a
 * warning to check it again. It also refuses a token it can't show safely (an odd symbol, a copy of
 * a listed one).
 *
 * A token Mitfah lists has a price, so the dialog offers to count it toward the limit too. Adding
 * needs no signature; counting is an owner transaction, sent straight after. If that is cancelled
 * or fails, the token stays added and the dialog offers to try again.
 */
export function AddTokenModal({ open, onClose, wallet, chain, networkName, tx, onAdded }: AddTokenModalProps) {
  const chainId = wallet.chain_id
  const queryClient = useQueryClient()
  const readiness = useOwnerReadiness(wallet)
  const [address, setAddress] = useState('')
  const [preview, setPreview] = useState<CustomToken | null>(null)
  const [count, setCount] = useState(true)
  // Set once a listed token is added and its counting has started, so a retry doesn't add it twice.
  const [added, setAdded] = useState(false)

  const lookup = useMutation({
    mutationFn: (value: string) => lookupCustomToken(chainId, value),
    onSuccess: setPreview,
  })
  const add = useMutation({
    mutationFn: (token: CustomToken) => addCustomToken(chainId, token.address),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['wallet', chainId] }),
  })

  const addressError = address === '' ? null : tokenAddressProblem(address)
  const ready = address !== '' && !addressError && !lookup.isPending

  const ticker = preview?.ticker.toUpperCase()
  // Counting needs the owner's signature, so it is only offered once the owner wallet is connected.
  const counting = count && readiness === 'ready'
  const countFailed =
    added && tx.state.key === COUNT_KEY && ['cancelled', 'abandoned', 'error'].includes(tx.state.phase)

  const close = () => {
    tx.release(COUNT_KEY)
    onClose()
  }

  const countIt = async (token: CustomToken) => {
    const confirmed = await tx.run({
      key: COUNT_KEY,
      action: { kind: 'watched-token', token: token.ticker, action: 'add' },
      success: `${token.ticker.toUpperCase()} added. It now counts toward your limit.`,
    })
    if (confirmed) close()
  }

  const submit = (token: CustomToken) =>
    // Closing is left to these calls, so a slow add can't close a dialog opened afresh.
    add.mutate(token, {
      onSuccess: saved => {
        if (saved.listed && counting) {
          setAdded(true)
          void countIt(saved)
        } else {
          onAdded(saved)
          onClose()
        }
      },
    })

  return (
    <Modal open={open} onClose={close} size="sm">
      <Modal.Header>
        <Modal.Title>{preview ? `Add ${ticker}?` : 'Add a token'}</Modal.Title>
      </Modal.Header>
      {preview ? (
        <>
          <Modal.Body>
            <Message type="success" showIcon className="mf-token-found">
              {chainId === bsc.id ? 'BEP-20' : 'ERC-20'} token
            </Message>
            <dl className="mf-token-preview">
              <dt>Symbol</dt>
              <dd>{preview.symbol}</dd>
              <dt>Decimals</dt>
              <dd className="mf-num">{preview.decimals}</dd>
              {preview.name && (
                <>
                  <dt>Name</dt>
                  <dd>{preview.name}</dd>
                </>
              )}
              <dt>In your wallet</dt>
              <dd className="mf-num">
                {formatTokenAmount(preview.balance_raw, preview.decimals)} {ticker}
              </dd>
              <dt>Contract</dt>
              {/* The copy button also gives keyboard users a stop inside the body, which scrolls on a phone. */}
              <dd className="mf-token-contract">
                <FullAddress address={preview.address} />
                <CopyButton value={preview.address} label="Token contract address" />
              </dd>
            </dl>
            {preview.listed ? (
              <>
                <Message type="info" showIcon className="mf-settings-note">
                  <strong>Pricing is available for {ticker}.</strong>{' '}
                  <Text as="span">Your spending limit can be applied to {ticker}.</Text>
                </Message>
                <Checkbox
                  className="mf-settings-note"
                  checked={counting}
                  disabled={readiness !== 'ready' || added}
                  onChange={(_value, checked) => setCount(checked)}
                >
                  Count {ticker} toward my spending limit
                </Checkbox>
                <Text size="sm" muted>
                  Counting it needs a signature from your owner wallet. If it doesn't count, the assistant can send or
                  sell it without limit.
                </Text>
                {readiness !== 'ready' && (
                  <OwnerWalletBar wallet={wallet} readiness={readiness} fork={chain?.fork ?? false} />
                )}
                <TxStatus tx={tx} txKey={COUNT_KEY} />
                {countFailed && (
                  <Message type="warning" showIcon className="mf-settings-note">
                    {ticker} is on your dashboard, but it doesn't count toward your limit yet.
                  </Message>
                )}
              </>
            ) : (
              <>
                <Message type="warning" showIcon className="mf-settings-note">
                  <strong>Your spending limit can't cover {ticker}.</strong>{' '}
                  <Text as="span">
                    Mitfah has no price for it, so the assistant can send or sell it without it counting toward your
                    limit. Buying it counts the full amount you pay.
                  </Text>
                </Message>
                <Text size="sm" muted className="mf-settings-note">
                  Anyone can create a token and give it any name. Only add tokens whose contract address you got from a
                  source you trust.
                </Text>
              </>
            )}
            {add.isError && (
              <Message type="error" showIcon className="mf-settings-note">
                Couldn't add it: {errorText(add.error)}
              </Message>
            )}
          </Modal.Body>
          <Modal.Footer className="mf-modal-actions">
            {added ? (
              <>
                <Button appearance="subtle" onClick={close}>
                  Close
                </Button>
                <TxButton
                  tx={tx}
                  txKey={COUNT_KEY}
                  appearance="primary"
                  disabled={readiness !== 'ready' || !countFailed}
                  onClick={() => void countIt(preview)}
                >
                  Count {ticker}
                </TxButton>
              </>
            ) : (
              <>
                <Button
                  appearance="subtle"
                  disabled={add.isPending}
                  onClick={() => {
                    setPreview(null)
                    add.reset()
                  }}
                >
                  Back
                </Button>
                <Button
                  appearance="primary"
                  loading={add.isPending}
                  // Like a TxButton: counting waits while another owner change is in flight.
                  disabled={counting && tx.busy}
                  onClick={() => submit(preview)}
                >
                  Add token
                </Button>
              </>
            )}
          </Modal.Footer>
        </>
      ) : (
        // The buttons sit inside the form so Enter in the field continues. Not `fluid`: that would
        // stack the body and footer as fields, shrunk to their content.
        <Form
          onSubmit={() => {
            if (ready) lookup.mutate(getAddress(address))
          }}
        >
          <Modal.Body>
            <Form.Group controlId="token-address">
              <Form.Label>Token contract address</Form.Label>
              <Input
                id="token-address"
                className="mf-mono"
                placeholder="0x…"
                autoComplete="off"
                spellCheck={false}
                value={address}
                onChange={value => {
                  setAddress(value.trim())
                  lookup.reset()
                }}
                aria-describedby="token-address-help-text"
                aria-invalid={!!addressError || lookup.isError}
              />
              {addressError ? (
                <Form.Text className="mf-error-text">{addressError}</Form.Text>
              ) : (
                <Form.Text>The token's address on {networkName}, not your own or a person's.</Form.Text>
              )}
            </Form.Group>
            {lookup.isError && (
              // The server's own reason: not an ERC-20 (no symbol or decimals), or one Mitfah won't show.
              <Message type="warning" showIcon className="mf-settings-note">
                {errorText(lookup.error)}
              </Message>
            )}
          </Modal.Body>
          <Modal.Footer className="mf-modal-actions">
            <Button appearance="subtle" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" appearance="primary" disabled={!ready} loading={lookup.isPending}>
              Continue
            </Button>
          </Modal.Footer>
        </Form>
      )}
    </Modal>
  )
}
