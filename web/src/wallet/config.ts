import { createConfig, http } from 'wagmi'
import { anvil, arbitrum, bsc, celo, mainnet, sepolia } from 'wagmi/chains'
import { injected } from 'wagmi/connectors/injected'
import { walletConnect } from 'wagmi/connectors/walletConnect'
import { SUPPORTED_CHAINS } from './chains'

const walletConnectProjectId = import.meta.env.VITE_WALLETCONNECT_PROJECT_ID ?? ''

/** WalletConnect (QR code on desktop, deep links on phones) needs a Reown project ID. */
export const walletConnectEnabled = walletConnectProjectId !== ''

/**
 * The browser-wallet setup.
 *
 * Browser wallets that announce themselves (EIP-6963) are discovered automatically; `injected()`
 * is the fallback for older ones that only set window.ethereum. The HTTP transports are for the
 * few reads the app makes itself — transactions are signed and sent by the user's wallet on its own
 * RPC, and the API confirms them.
 */
export const wagmiConfig = createConfig({
  chains: SUPPORTED_CHAINS,
  connectors: [
    injected(),
    ...(walletConnectEnabled
      ? [
          walletConnect({
            projectId: walletConnectProjectId,
            showQrModal: true,
            metadata: {
              name: 'Mitfah',
              description: 'An AI assistant for your crypto wallet that can only spend what you allow.',
              url: window.location.origin,
              icons: [`${window.location.origin}/icon-192.png`],
            },
          }),
        ]
      : []),
  ],
  transports: {
    [sepolia.id]: http(),
    [mainnet.id]: http(),
    [bsc.id]: http(),
    [arbitrum.id]: http(),
    [celo.id]: http(),
    [anvil.id]: http(),
  },
  multiInjectedProviderDiscovery: true,
})

declare module 'wagmi' {
  interface Register {
    config: typeof wagmiConfig
  }
}
