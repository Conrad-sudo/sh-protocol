import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getAddress } from 'viem'
import { Button, Form, Input, Message, Modal, Text } from 'rsuite'
import { saveContact } from '../../api/contacts'
import type { Contact } from '../../api/types'
import { CONTACTS_KEY } from '../../hooks/useContacts'
import { addressProblem, nameProblem, normalizeName } from '../../lib/contacts'
import { errorText } from '../../lib/tx'
import { FullAddress } from '../FullAddress'

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
}

/**
 * Adds someone the assistant may pay, in two steps: the details, then a look at the whole address
 * before it is saved. A name that is already saved gets the new address, so that look shows both.
 */
export function AddContactModal({ open, onClose, contacts, onSaved }: AddContactModalProps) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [address, setAddress] = useState('')
  const [review, setReview] = useState<Review | null>(null)

  const save = useMutation({
    mutationFn: ({ contact }: Review) => saveContact(contact),
    onSuccess: (saved, { previous }) => {
      queryClient.setQueryData<Contact[]>(CONTACTS_KEY, list =>
        [...(list ?? []).filter(c => c.name !== saved.name), saved].sort((a, b) => (a.name < b.name ? -1 : 1)),
      )
      void queryClient.invalidateQueries({ queryKey: CONTACTS_KEY })
      onSaved(saved, !!previous)
    },
  })

  const normalized = normalizeName(name)
  const nameError = name === '' ? null : nameProblem(name)
  const addressError = address === '' ? null : addressProblem(address)
  const checksummed = address !== '' && !addressError ? getAddress(address) : null
  const existing = nameError ? undefined : contacts.find(c => c.name === normalized)
  const unchanged = !!existing && existing.address === checksummed
  const savedAs = checksummed ? contacts.find(c => c.address === checksummed && c.name !== normalized) : undefined
  const ready = name !== '' && !nameError && checksummed !== null && !unchanged

  const change = (setter: (value: string) => void) => (value: string) => {
    setter(value)
    save.reset()
  }

  let nameHelp
  if (nameError) nameHelp = <Form.Text className="mf-error-text">{nameError}</Form.Text>
  else if (unchanged) nameHelp = <Form.Text className="mf-error-text">{normalized} already has this address.</Form.Text>
  else if (normalized !== '' && normalized !== name.trim()) nameHelp = <Form.Text>Saved as “{normalized}”.</Form.Text>
  else nameHelp = <Form.Text>The name you'll use with your assistant, as in “send 10 USDC to sam”.</Form.Text>

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
            placeholder="0x…"
            autoComplete="off"
            spellCheck={false}
            value={address}
            onChange={change(value => setAddress(value.trim()))}
            aria-describedby={addressError || savedAs ? 'contact-address-help-text' : undefined}
            aria-invalid={!!addressError}
          />
          {addressError ? (
            <Form.Text className="mf-error-text">{addressError}</Form.Text>
          ) : (
            savedAs && <Form.Text>Already saved as {savedAs.name}.</Form.Text>
          )}
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
      <Message type="warning" showIcon>
        Check every character against the address {review.contact.name} gave you. Scam addresses often match only the
        first and last few.
      </Message>
      {save.isError && (
        <Message type="error" showIcon className="mf-settings-note">
          Couldn't save: {errorText(save.error)}
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
            if (ready) setReview({ contact: { name: normalized, address: checksummed }, previous: existing })
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
