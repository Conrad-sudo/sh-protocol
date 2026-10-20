import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getAddress, type Address } from 'viem'
import { useConnection, useSignTypedData } from 'wagmi'
import { Button, Form, Input, Loader, Message, Modal, Text } from 'rsuite'
import { ApiError } from '../../api/client'
import { prepareContact, resolveEnsName, saveContact } from '../../api/contacts'
import type { Contact } from '../../api/types'
import { CONTACTS_KEY } from '../../hooks/useContacts'
import { useDebouncedValue } from '../../hooks/useDebouncedValue'
import { useMe } from '../../hooks/useMe'
import { addressProblem, looksLikeEnsName, nameProblem, normalizeName } from '../../lib/contacts'
import { errorText, isUserRejection } from '../../lib/tx'
import { AddressText } from '../AddressText'
import { FullAddress } from '../FullAddress'
import { AccountMismatchBanner } from '../wallet/AccountMismatchBanner'
import { ConnectDialog } from '../wallet/ConnectDialog'

interface AddContactModalProps {
  open: boolean
  onClose: () => void
  /** The saved contacts, to spot a name or address that is already there. */
  contacts: Contact[]
  onSaved: (contact: Contact, replaced: boolean) => void
}

/** What the second step shows, fixed when it opens so saving can't change it mid-look. */
interface Review {
  contact: Contact
  /** The contact this one replaces. */
  previous?: Contact
  /** The ENS name the address was looked up from, when it was. */
  ens?: string
}

/** How long typing must pause before an ENS name is looked up. */
const LOOKUP_DELAY_MS = 400

/** Why an ENS name couldn't be used, in plain words. */
function lookupProblem(name: string, error: unknown) {
  if (error instanceof ApiError && error.status === 404) {
    return `${name} doesn't point to an address. Check the spelling, or paste their 0x address.`
  }
  if (error instanceof ApiError && error.status === 422) return `“${name}” isn't a valid ENS name.`
  return `Couldn't look up ${name}. Try again, or paste their 0x address.`
}

/**
 * Adds someone the assistant may pay, in two steps: the details, then a look at the whole address
 * before it is saved. A name that is already saved gets the new address, so that look shows both.
 *
 * The address can be typed as an ENS name (sam.eth). The server looks it up, and from then on it is
 * an address like any other: signed, saved and kept even if the name is later pointed elsewhere.
 *
 * Saving takes the signature of the wallet the user signed in with, over the exact name and address
 * (EIP-712, from POST /api/contacts/prepare): the contact list is where the assistant may send
 * money, so a signed-in session alone can't add to it, and the wallet shows the address once more.
 */
export function AddContactModal({ open, onClose, contacts, onSaved }: AddContactModalProps) {
  const queryClient = useQueryClient()
  const { data: me } = useMe()
  const owner = me?.owner_addr ?? null
  const { address: connected } = useConnection()
  const { mutateAsync: signTypedDataAsync } = useSignTypedData()
  const [name, setName] = useState('')
  const [address, setAddress] = useState('')
  const [review, setReview] = useState<Review | null>(null)
  const [signing, setSigning] = useState(false)
  const signerReady = !!owner && !!connected && connected.toLowerCase() === owner.toLowerCase()

  const save = useMutation({
    mutationFn: async ({ contact }: Review) => {
      // Checked by the server first, so a contact it would refuse never reaches the wallet.
      const typed = await prepareContact(contact)
      setSigning(true)
      try {
        const signature = await signTypedDataAsync({ ...typed, account: owner as Address })
        return await saveContact({ ...typed.message, signature })
      } finally {
        setSigning(false)
      }
    },
    onSuccess: (saved, { previous }) => {
      queryClient.setQueryData<Contact[]>(CONTACTS_KEY, list =>
        [...(list ?? []).filter(c => c.name !== saved.name), saved].sort((a, b) => (a.name < b.name ? -1 : 1)),
      )
      void queryClient.invalidateQueries({ queryKey: CONTACTS_KEY })
      onSaved(saved, !!previous)
    },
  })

  const ensInput = looksLikeEnsName(address)
  const lookupName = useDebouncedValue(ensInput ? address : '', LOOKUP_DELAY_MS)
  const lookup = useQuery({
    queryKey: ['ens', lookupName],
    queryFn: ({ signal }) => resolveEnsName(lookupName, signal),
    enabled: lookupName !== '',
    retry: false,
    staleTime: 60_000,
  })
  // Only an answer about what is in the field now, never about what was there a moment ago.
  const looked = ensInput && lookupName === address
  const ens = looked && lookup.isSuccess ? lookup.data : null

  // Left empty, the name is the ENS name, so "send 10 USDC to sam.eth" works as typed.
  const contactName = name === '' && ens ? ens.name : name
  const normalized = normalizeName(contactName)
  const nameError = contactName === '' ? null : nameProblem(contactName)
  const addressError = address === '' || ensInput ? null : addressProblem(address)
  const typedAddress = address !== '' && !ensInput && !addressError ? getAddress(address) : null
  const checksummed = ens ? getAddress(ens.address) : typedAddress
  const existing = nameError ? undefined : contacts.find(c => c.name === normalized)
  const unchanged = !!existing && existing.address === checksummed
  const savedAs = checksummed ? contacts.find(c => c.address === checksummed && c.name !== normalized) : undefined
  const ready = contactName !== '' && !nameError && checksummed !== null && !unchanged

  const change = (setter: (value: string) => void) => (value: string) => {
    setter(value)
    save.reset()
  }

  let nameHelp
  if (nameError) nameHelp = <Form.Text className="mf-error-text">{nameError}</Form.Text>
  else if (unchanged) nameHelp = <Form.Text className="mf-error-text">{normalized} already has this address.</Form.Text>
  else if (name === '' && ens) nameHelp = <Form.Text>Saved as “{normalized}” unless you type another name.</Form.Text>
  else if (normalized !== '' && normalized !== name.trim()) nameHelp = <Form.Text>Saved as “{normalized}”.</Form.Text>
  else nameHelp = <Form.Text>The name you'll use with your assistant, as in “send 10 USDC to sam”.</Form.Text>

  let addressHelp
  if (addressError) addressHelp = <Form.Text className="mf-error-text">{addressError}</Form.Text>
  else if (ens) {
    addressHelp = (
      <Form.Text>
        {ens.name} points to <AddressText address={ens.address} />.{savedAs && ` Already saved as ${savedAs.name}.`}
      </Form.Text>
    )
  } else if (looked && lookup.isError) {
    addressHelp = <Form.Text className="mf-error-text">{lookupProblem(address, lookup.error)}</Form.Text>
  } else if (ensInput) addressHelp = <Form.Text>Looking up {address}…</Form.Text>
  else if (savedAs) addressHelp = <Form.Text>Already saved as {savedAs.name}.</Form.Text>
  else addressHelp = <Form.Text>Their 0x address, or an ENS name such as sam.eth.</Form.Text>

  const fields = (
    <>
      <Form.Stack fluid>
        <Form.Group controlId="contact-name">
          <Form.Label>Name</Form.Label>
          <Input
            id="contact-name"
            autoComplete="off"
            spellCheck={false}
            value={name}
            onChange={change(setName)}
            aria-describedby="contact-name-help-text"
            aria-invalid={!!nameError || unchanged}
          />
          {nameHelp}
        </Form.Group>
        <Form.Group controlId="contact-address">
          <Form.Label>Address</Form.Label>
          <Input
            id="contact-address"
            className="mf-mono"
            placeholder="0x… or sam.eth"
            autoComplete="off"
            spellCheck={false}
            value={address}
            onChange={change(value => setAddress(value.trim()))}
            aria-describedby="contact-address-help-text"
            aria-invalid={!!addressError || (looked && lookup.isError)}
          />
          {addressHelp}
        </Form.Group>
      </Form.Stack>
      {existing && !unchanged && (
        <Message type="warning" showIcon className="mf-settings-note">
          You already have a contact called {normalized}. Continuing replaces their address.
        </Message>
      )}
    </>
  )

  const check = review && (
    <>
      {review.previous ? (
        <>
          <Text>Your assistant will pay {review.contact.name} at the new address from now on.</Text>
          <dl className="mf-address-compare">
            <dt>Old</dt>
            <dd>
              <FullAddress address={review.previous.address} />
            </dd>
            <dt>New</dt>
            <dd>
              <FullAddress address={review.contact.address} />
            </dd>
          </dl>
        </>
      ) : (
        <>
          <Text>Your assistant will be able to send money to {review.contact.name} at:</Text>
          <div className="mf-address-box">
            <FullAddress address={review.contact.address} />
          </div>
        </>
      )}
      {review.ens ? (
        <Message type="warning" showIcon>
          This is where {review.ens} points today. Make sure it's the right name: scammers register look-alike
          names. If {review.ens} is pointed somewhere else later, this contact keeps this address.
        </Message>
      ) : (
        <Message type="warning" showIcon>
          Please check {review.contact.name}'s address carefully and make sure all characters match.
          Scam addresses often match only the first and last few characters.
        </Message>
      )}
      {owner && <SignerBar owner={owner} />}
      {signerReady &&
        (signing ? (
          <Loader className="mf-tx-status" content="Confirm in your wallet." />
        ) : (
          <Text size="sm" muted className="mf-settings-note">
            Your wallet will ask you to sign this contact. Signing is free and moves no funds.
          </Text>
        ))}
      {save.isError && (
        <Message type="error" showIcon className="mf-settings-note">
          {isUserRejection(save.error) ? 'Cancelled — nothing was saved.' : `Couldn't save: ${errorText(save.error)}`}
        </Message>
      )}
    </>
  )

  let title = 'Add a contact'
  if (review) title = review.previous ? `Replace ${review.contact.name}'s address?` : 'Check the address'

  return (
    <Modal open={open} onClose={onClose} size="sm">
      <Modal.Header>
        <Modal.Title>{title}</Modal.Title>
      </Modal.Header>
      {review ? (
        <>
          <Modal.Body>{check}</Modal.Body>
          <Modal.Footer className="mf-modal-actions">
            <Button
              appearance="subtle"
              disabled={save.isPending}
              onClick={() => {
                setReview(null)
                save.reset()
              }}
            >
              Back
            </Button>
            <Button
              appearance="primary"
              color="orange"
              loading={save.isPending}
              disabled={!signerReady}
              onClick={() =>
                // Closing is left to this call: it doesn't run once this dialog has been closed and
                // opened afresh, so a slow save can't close the new one.
                save.mutate(review, { onSuccess: onClose })
              }
            >
              {review.previous ? 'Replace address' : 'Save contact'}
            </Button>
          </Modal.Footer>
        </>
      ) : (
        // The buttons sit inside the form so Enter in either field continues. Not `fluid`: that
        // would stack the body and footer as fields, shrunk to their content.
        <Form
          onSubmit={() => {
            if (ready) {
              setReview({ contact: { name: normalized, address: checksummed }, previous: existing, ens: ens?.name })
            }
          }}
        >
          <Modal.Body>{fields}</Modal.Body>
          <Modal.Footer className="mf-modal-actions">
            <Button appearance="subtle" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" appearance="primary" disabled={!ready}>
              Continue
            </Button>
          </Modal.Footer>
        </Form>
      )}
    </Modal>
  )
}

/**
 * Connecting the wallet that signs the contact — the one the user signed in with — or switching back
 * to it. Nothing once it is connected.
 */
function SignerBar({ owner }: { owner: string }) {
  const { address, isConnected } = useConnection()
  const [connectOpen, setConnectOpen] = useState(false)

  if (!isConnected || !address) {
    return (
      <div className="mf-owner-bar mf-owner-bar-row">
        <Text>
          Connect your wallet, <AddressText address={owner} />, to approve this contact.
        </Text>
        <Button appearance="primary" onClick={() => setConnectOpen(true)}>
          Connect wallet
        </Button>
        <ConnectDialog open={connectOpen} onClose={() => setConnectOpen(false)} />
      </div>
    )
  }
  if (address.toLowerCase() !== owner.toLowerCase()) {
    return (
      <div className="mf-owner-bar">
        <AccountMismatchBanner ownerAddr={owner} connected={address} />
      </div>
    )
  }
  return null
}
