import { useRef, useState } from 'react'
import { useMutation, useMutationState, useQueryClient } from '@tanstack/react-query'
import PeoplesIcon from '@rsuite/icons/Peoples'
import PlusIcon from '@rsuite/icons/Plus'
import { Button, Message, Placeholder, Text, useToaster } from 'rsuite'
import { ApiError } from '../api/client'
import { deleteContact } from '../api/contacts'
import type { Contact } from '../api/types'
import { AddressText } from '../components/AddressText'
import { AddContactModal } from '../components/contacts/AddContactModal'
import { CopyButton } from '../components/CopyButton'
import { EmptyState } from '../components/EmptyState'
import { ConfirmModal } from '../components/owner/ConfirmModal'
import { PageHeader } from '../components/PageHeader'
import { CONTACTS_KEY, useContacts } from '../hooks/useContacts'
import { errorText } from '../lib/tx'

const TITLE = 'Contacts'
const DESCRIPTION = 'The people your assistant may pay. Ask it to “send 10 USDC to sam” using a name from this list.'
const REMOVE_KEY = ['contacts', 'remove'] as const

/**
 * The account's contacts: the only addresses the assistant can send money to. The assistant reads
 * this list but can't change it, so adding someone happens only here.
 */
export function ContactsPage() {
  const contacts = useContacts()
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const [addOpen, setAddOpen] = useState(false)
  // A fresh form each time the dialog opens.
  const [addKey, setAddKey] = useState(0)
  // Kept after closing so the dialog's text doesn't vanish while it animates out.
  const [toRemove, setToRemove] = useState<Contact | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)

  const notify = (type: 'success' | 'info' | 'error', text: string) =>
    toaster.push(
      <Message type={type} showIcon closable>
        {text}
      </Message>,
      { placement: 'topCenter', duration: type === 'error' ? 6000 : 3000 },
    )

  const remove = useMutation({
    mutationKey: REMOVE_KEY,
    mutationFn: async (name: string) => {
      try {
        await deleteContact(name)
        return true
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return false
        throw error
      }
    },
    onSuccess: (removed, name) => {
      // Focus was on the row that is going away; move it somewhere that stays.
      if (document.activeElement?.closest('[data-contact]')?.getAttribute('data-contact') === name) {
        headingRef.current?.focus()
      }
      queryClient.setQueryData<Contact[]>(CONTACTS_KEY, list => list?.filter(c => c.name !== name))
      void queryClient.invalidateQueries({ queryKey: CONTACTS_KEY })
      notify(removed ? 'success' : 'info', removed ? `${name} removed.` : `${name} was already removed.`)
    },
    onError: (error, name) => notify('error', `Couldn't remove ${name}: ${errorText(error)}`),
  })
  const removing = useMutationState({
    filters: { mutationKey: REMOVE_KEY, status: 'pending' },
    select: mutation => mutation.state.variables as string,
  })

  const openAdd = () => {
    setAddKey(k => k + 1)
    setAddOpen(true)
  }

  // A failed refresh keeps showing the list it already has.
  let body
  if (contacts.data === undefined && !contacts.isError) body = <Placeholder.Paragraph rows={4} active />
  else if (contacts.data === undefined) {
    body = (
      <Message type="error" showIcon>
        Couldn't load your contacts: {errorText(contacts.error)}{' '}
        <Button appearance="link" size="sm" onClick={() => void contacts.refetch()}>
          Try again
        </Button>
      </Message>
    )
  } else {
    const list = contacts.data
    body = (
      <section aria-labelledby="contacts-heading">
        <div className="mf-section-head">
          <h2 id="contacts-heading" ref={headingRef} tabIndex={-1}>
            Your contacts <span className="mf-muted mf-num">({list.length})</span>
          </h2>
          <Button appearance="primary" startIcon={<PlusIcon />} onClick={openAdd}>
            Add contact
          </Button>
        </div>
        {list.length === 0 ? (
          <EmptyState icon={<PeoplesIcon />} title="No contacts yet">
            Add the first person you want your assistant to be able to pay.
          </EmptyState>
        ) : (
          <ul className="mf-contact-list">
            {list.map(contact => (
              <li key={contact.name} className="mf-contact" data-contact={contact.name}>
                <span className="mf-contact-name">{contact.name}</span>
                <span className="mf-contact-address">
                  <AddressText address={contact.address} />
                  <CopyButton value={contact.address} label={`Address of ${contact.name}`} />
                </span>
                <Button
                  appearance="subtle"
                  size="sm"
                  className="mf-contact-remove"
                  aria-label={`Remove ${contact.name}`}
                  loading={removing.includes(contact.name)}
                  onClick={() => {
                    setToRemove(contact)
                    setRemoveOpen(true)
                  }}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        )}
        <AddContactModal
          key={addKey}
          open={addOpen}
          onClose={() => setAddOpen(false)}
          contacts={list}
          onSaved={(saved, replaced) =>
            notify(
              'success',
              replaced ? `${saved.name}'s address updated.` : `${saved.name} saved. Your assistant can now pay them.`,
            )
          }
        />
      </section>
    )
  }

  return (
    <>
      <PageHeader title={TITLE} description={DESCRIPTION} />
      <div className="mf-dashboard">
        <Message type="info" showIcon>
          <strong>Your assistant can only send money to people on this list.</strong>{' '}
          <Text as="span">Only you can add someone, and only here: the assistant can't change the list.</Text>
        </Message>
        {body}
      </div>
      {toRemove && (
        <ConfirmModal
          open={removeOpen}
          title={`Remove ${toRemove.name}?`}
          confirmLabel="Remove"
          tone="danger"
          onConfirm={() => remove.mutate(toRemove.name)}
          onClose={() => setRemoveOpen(false)}
        >
          <Text>
            Your assistant won't be able to send money to {toRemove.name} any more. You can add them again at any
            time.
          </Text>
        </ConfirmModal>
      )}
    </>
  )
}
