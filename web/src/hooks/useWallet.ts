import { useQuery } from '@tanstack/react-query'
import { fetchWallet } from '../api/wallet'
import { useSelectedChain } from '../chain/useSelectedChain'

/**
 * The selected network's wallet. `data` is null when the account has no wallet there. Each read
 * costs the API a dozen RPC calls, so it refreshes once a minute rather than continuously.
 */
export function useWallet() {
  const { chainId, walletChains } = useSelectedChain()
  return useQuery({
    queryKey: ['wallet', chainId],
    queryFn: () => fetchWallet(chainId!),
    enabled: chainId !== null && walletChains.includes(chainId),
    staleTime: 15_000,
    refetchInterval: 60_000,
  })
}
