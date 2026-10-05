import { useQuery } from '@tanstack/react-query'
import { fetchAllowlist } from '../api/wallet'

/**
 * The wallet's contract allowlist, read only while `enabled` (the Advanced section is open): it costs
 * the API a few RPC calls the dashboard doesn't need. Keyed under ['wallet', chainId], so every
 * owner transaction that refreshes the wallet refreshes this too.
 */
export function useAllowlist(chainId: number, enabled: boolean) {
  return useQuery({
    queryKey: ['wallet', chainId, 'allowlist'],
    queryFn: () => fetchAllowlist(chainId),
    enabled,
    staleTime: 15_000,
  })
}
