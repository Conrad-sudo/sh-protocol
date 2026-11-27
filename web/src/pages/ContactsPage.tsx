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
import { GlassLayer } from '../components/Glass'
import { ConfirmModal } from '../components/owner/ConfirmModal'
import { PageHeader } from '../components/PageHeader'
import { QueryError } from '../components/QueryError'
import { StatusTag } from '../components/StatusTag'
import { CONTACTS_KEY, useContacts } from '../hooks/useContacts'
import { useSignContact } from '../hooks/useSignContact'
import { isUserRejection } from '../lib/tx'
import { errorText } from '../lib/tx'

const TITLE = 'Contacts'
const DESCRIPTION = 'The accounts your assistant may pay, by name: “send 10 USDC to sam”.'
const REMOVE_KEY = ['contacts', 'remove'] as const

/**
 * The account's contacts: the only addresses the assistant can send money to. The assistant reads
 * this list but can't change it, so adding someone happens only here. Each contact carries the owner's
 * signature, and the wallet itself refuses to pay anyone without one: a contact saved before it checked
 * (or after the owner cancelled old signatures in Controls) is paid again once signed again here.
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

  const signer = useSignContact()
  const resign = useMutation({
    mutationFn: (contact: Contact) => signer.sign(contact),
    onSuccess: saved => notify('success', `${saved.name} signed again. Your wallet will pay them.`),
    onError: (error, contact) => {
      if (isUserRejection(error)) return
      notify('error', `Couldn't sign ${contact.name}: ${errorText(error)}`)
    },
  })
  const signAgain = (contact: Contact) => {
    if (!signer.ready) {
      notify('error', 'Connect the wallet you signed in with to sign this contact.')
      return
    }
    resign.mutate(contact)
  }

  const openAdd = () => {
    setAddKey(k => k + 1)
    setAddOpen(true)
  }

  // A failed refresh keeps showing the list it already has.
  let body
  if (contacts.data === undefined && !contacts.isError) body = <Placeholder.Paragraph rows={4} active />
  else if (contacts.data === undefined) {
    body = (
      <QueryError what="your contacts" error={contacts.error} onRetry={() => void contacts.refetch()} />
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
            Add the first account you want your assistant to be able to pay.
          </EmptyState>
        ) : (
          // The glass sits beside the list, not in it: a list may only hold its items.
          <div className="mf-contact-plate mf-glass-surface">
            <GlassLayer />
            <ul className="mf-contact-list">
              {list.map(contact => (
                <li key={contact.name} className="mf-contact" data-contact={contact.name}>
                  <span className="mf-contact-name">
                    {contact.name}
                    {contact.signed === false && (
                      <>
                        {' '}
                        <StatusTag tone="warning">Needs signing</StatusTag>
                      </>
                    )}
                  </span>
                  <span className="mf-contact-address">
                    <AddressText address={contact.address} />
                    <CopyButton value={contact.address} label={`Address of ${contact.name}`} />
                  </span>
                  <Button
                    appearance={contact.signed === false ? 'primary' : 'subtle'}
                    size="sm"
                    aria-label={`Sign ${contact.name} again`}
                    loading={resign.isPending && resign.variables?.name === contact.name}
                    disabled={resign.isPending}
                    onClick={() => signAgain(contact)}
                  >
                    Sign again
                  </Button>
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
          </div>
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
          <strong>Your assistant can only pay accounts on this list.</strong>{' '}
          <Text as="span">
            Only you can change it, and your wallet itself checks it: each contact carries your signature.
          </Text>
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
          <Text>Your assistant won't be able to pay {toRemove.name} any more. You can add them again any time.</Text>
        </ConfirmModal>
      )}
    </>
  )
}
