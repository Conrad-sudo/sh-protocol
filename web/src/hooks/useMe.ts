import { useQuery } from '@tanstack/react-query'
import { fetchMe } from '../api/auth'
import { useAuth } from '../auth/useAuth'

/**
 * The signed-in account: email, bound owner address, linked integrations, wallet chains.
 *
 * @param pollMs  Re-fetch this often, even from a background tab (false = don't poll). Used while
 *                waiting for something that happens outside the page, such as a Telegram link.
 */
export function useMe({ pollMs = false }: { pollMs?: number | false } = {}) {
  const { status, userId } = useAuth()
  return useQuery({
    queryKey: ['me', userId],
    queryFn: fetchMe,
    enabled: status === 'signedIn',
    refetchInterval: pollMs,
    refetchIntervalInBackground: pollMs !== false,
  })
}
