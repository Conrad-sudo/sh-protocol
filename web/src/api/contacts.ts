import { apiFetch } from './client'
import type { Contact } from './types'

/** The account's contacts, sorted by name. */
export async function fetchContacts() {
  const { contacts } = await apiFetch<{ contacts: Contact[] }>('/api/contacts')
  return contacts
}

/**
 * Saves a contact. The server lowercases the name and checksums the address, and a name already
 * saved gets the new address. Answers what was stored.
 */
export function saveContact(contact: Contact) {
  return apiFetch<Contact>('/api/contacts', { method: 'POST', body: contact })
}

/** Deletes a contact. A 404 means it was already gone. */
export function deleteContact(name: string) {
  return apiFetch<{ status: 'deleted'; name: string }>(`/api/contacts/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
}
