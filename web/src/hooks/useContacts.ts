import { useQuery } from '@tanstack/react-query'
import { fetchContacts } from '../api/contacts'

export const CONTACTS_KEY = ['contacts'] as const

/** The people the assistant may pay. Only this browser session changes them. */
export function useContacts() {
  return useQuery({ queryKey: CONTACTS_KEY, queryFn: fetchContacts })
}
