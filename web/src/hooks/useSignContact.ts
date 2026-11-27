import { useQueryClient } from '@tanstack/react-query'
import type { Address } from 'viem'
import { useConnection, useSignTypedData } from 'wagmi'
import { prepareContact, saveContact } from '../api/contacts'
import type { Contact } from '../api/types'
import { CONTACTS_KEY } from './useContacts'
import { useMe } from './useMe'

/**
 * Signs a saved contact again with the owner's wallet, as Contacts' "Sign again" does: for a contact
 * saved before the wallet checked contacts itself, or after the owner cancelled old signatures. The
 * same prepare, sign and save as adding one. `ready` is false until the wallet the user signed in with
 * is the one connected.
 */
export function useSignContact() {
  const queryClient = useQueryClient()
  const { data: me } = useMe()
  const owner = me?.owner_addr ?? null
  const { address: connected } = useConnection()
  const { mutateAsync: signTypedDataAsync } = useSignTypedData()
  const ready = !!owner && !!connected && connected.toLowerCase() === owner.toLowerCase()

  const sign = async (contact: Contact) => {
    const typed = await prepareContact({ name: contact.name, address: contact.address })
    const signature = await signTypedDataAsync({ ...typed, account: owner as Address })
    const saved = await saveContact({ ...typed.message, signature })
    queryClient.setQueryData<Contact[]>(CONTACTS_KEY, list => (list ?? []).map(c => (c.name === saved.name ? saved : c)))
    void queryClient.invalidateQueries({ queryKey: CONTACTS_KEY })
    return saved
  }

  return { sign, ready, owner }
}
