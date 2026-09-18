import { useContext, type ReactNode } from 'react'
import { WagmiContext, WagmiProvider } from 'wagmi'
import { wagmiConfig } from './config'

/**
 * Browser-wallet support for the signed-in app.
 *
 * It sits inside the app's routes rather than at the root, so wagmi and viem — about a third of
 * the old first load — are only fetched once someone opens the app itself. A test (or any caller)
 * that already supplies a config keeps it; nesting a second provider would shadow its connector.
 */
export function WalletProvider({ children }: { children: ReactNode }) {
  if (useContext(WagmiContext)) return children
  return <WagmiProvider config={wagmiConfig}>{children}</WagmiProvider>
}
