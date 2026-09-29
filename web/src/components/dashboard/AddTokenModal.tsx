import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getAddress } from 'viem'
import { Button, Form, Input, Message, Modal, Text } from 'rsuite'
import { bsc } from 'viem/chains'
import type { CustomToken } from '../../api/types'
import { addCustomToken, lookupCustomToken } from '../../api/wallet'
import { addressProblem } from '../../lib/contacts'
import { formatTokenAmount } from '../../lib/format'
import { errorText } from '../../lib/tx'
import { CopyButton } from '../CopyButton'
import { FullAddress } from '../FullAddress'

interface AddTokenModalProps {
  open: boolean
  onClose: () => void
  chainId: number
  /** The network's name, for the "check it's on this network" hint. */
  networkName: string
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
 * Adds a token by its contract address, MetaMask-style, in two steps: paste the address, then check
 * what the chain says it is before adding it. The server reads the token's symbol and decimals —
 * what shows it is an ERC-20 — and an address that can't answer both gets a warning to check it
 * again. It also refuses a token it can't show safely (an odd symbol, a copy of a listed one).
 *
 * Adding changes nothing on chain and needs no signature: it only puts the token on this account's
 * list so the dashboard shows it and the assistant can name it.
 */
export function AddTokenModal({ open, onClose, chainId, networkName, onAdded }: AddTokenModalProps) {
  const queryClient = useQueryClient()
  const [address, setAddress] = useState('')
  const [preview, setPreview] = useState<CustomToken | null>(null)

  const lookup = useMutation({
    mutationFn: (value: string) => lookupCustomToken(chainId, value),
    onSuccess: setPreview,
  })
  const add = useMutation({
    mutationFn: (token: CustomToken) => addCustomToken(chainId, token.address),
    onSuccess: token => {
      void queryClient.invalidateQueries({ queryKey: ['wallet', chainId] })
      onAdded(token)
    },
  })

  const addressError = address === '' ? null : tokenAddressProblem(address)
  const ready = address !== '' && !addressError && !lookup.isPending

  const ticker = preview?.ticker.toUpperCase()

  return (
    <Modal open={open} onClose={onClose} size="sm">
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
            <Message type="warning" showIcon className="mf-settings-note">
              <strong>Your spending limit can't cover {ticker}.</strong>{' '}
              <Text as="span">
                Mitfah has no price for it, so the assistant can send or sell it without it counting toward your limit.
                Buying it counts the full amount you pay.
              </Text>
            </Message>
            <Text size="sm" muted className="mf-settings-note">
              Anyone can create a token and give it any name. Only add tokens whose contract address you got from a
              source you trust.
            </Text>
            {add.isError && (
              <Message type="error" showIcon className="mf-settings-note">
                Couldn't add it: {errorText(add.error)}
              </Message>
            )}
          </Modal.Body>
          <Modal.Footer className="mf-modal-actions">
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
              // Closing is left to this call, so a slow add can't close a dialog opened afresh.
              onClick={() => add.mutate(preview, { onSuccess: onClose })}
            >
              Add token
            </Button>
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
            <Button appearance="subtle" onClick={onClose}>
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
