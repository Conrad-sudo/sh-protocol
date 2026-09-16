import { useQuery } from '@tanstack/react-query'
import { fetchMe } from '../api/auth'
import { useAuth } from '../auth/useAuth'

/** The signed-in account: email, bound owner address, linked integrations, wallet chains. */
export function useMe() {
  const { status, userId } = useAuth()
  return useQuery({
    queryKey: ['me', userId],
    queryFn: fetchMe,
    enabled: status === 'signedIn',
  })
}
