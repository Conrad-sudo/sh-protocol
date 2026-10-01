import { apiFetch } from './client'
import type { Contact, ContactTypedData } from './types'

/** The account's contacts, sorted by name. */
export async function fetchContacts() {
  const { contacts } = await apiFetch<{ contacts: Contact[] }>('/api/contacts')
  return contacts
}

/**
 * Checks a contact and returns the typed data the owner wallet signs to save it. A name or address
 * the server would refuse fails here, before the wallet is asked anything.
 */
export function prepareContact(contact: Contact) {
  return apiFetch<ContactTypedData>('/api/contacts/prepare', { method: 'POST', body: contact })
}

/**
 * Saves a contact, given the owner wallet's signature over prepareContact's typed data. The server
 * lowercases the name and checksums the address, and a name already saved gets the new address.
 * Answers what was stored.
 */
export function saveContact(signed: ContactTypedData['message'] & { signature: string }) {
  return apiFetch<Contact>('/api/contacts', { method: 'POST', body: signed })
}

/** Deletes a contact. A 404 means it was already gone. */
export function deleteContact(name: string) {
  return apiFetch<{ status: 'deleted'; name: string }>(`/api/contacts/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
}
