import type { ComponentProps, ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { createMemoryRouter, type RouteObject } from 'react-router'
import { RouterProvider } from 'react-router/dom'
import { createConfig, http, WagmiProvider } from 'wagmi'
import { anvil, arbitrum, bsc, celo, mainnet, sepolia } from 'wagmi/chains'
import { mock, type MockParameters } from 'wagmi/connectors/mock'
import { AuthProvider } from '../auth/AuthProvider'
import { ChainProvider } from '../chain/ChainProvider'
import { ThemeProvider } from '../theme/ThemeProvider'
import { SUPPORTED_CHAINS } from '../wallet/chains'

/** A JSON Response, as fetch would resolve it. */
export function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export const TOKEN = {
  access_token: 'token-1',
  token_type: 'bearer' as const,
  expires_in: 900,
  user_id: 7,
}

export const ME = {
  user_id: 7,
  email: 'sam@example.com',
  owner_addr: null,
  has_password: true,
  google_linked: false,
  telegram_linked: false,
  wallet_chains: [],
}

/** The address the mock wallet connects as. */
export const WALLET = '0x1111111111111111111111111111111111111111'

/**
 * The app's wagmi setup with a mock wallet in place of browser wallets. The mock starts on the
 * first chain (Sepolia), answers eth_chainId itself, and sends everything else — signatures
 * (as eth_sign) and transactions — to that chain's public RPC URL over fetch, so a test's fetch
 * stub answers them with `answerRpc`. Nothing is persisted between tests.
 */
export function makeWagmiConfig(features?: MockParameters['features']) {
  return createConfig({
    chains: SUPPORTED_CHAINS,
    connectors: [mock({ accounts: [WALLET], features })],
    transports: {
      [sepolia.id]: http(),
      [mainnet.id]: http(),
      [bsc.id]: http(),
      [arbitrum.id]: http(),
      [celo.id]: http(),
      [anvil.id]: http(),
    },
    storage: null,
    multiInjectedProviderDiscovery: false,
  })
}

type RpcHandlers = Record<string, (params: unknown[]) => unknown>
interface RpcRequest {
  id: number
  method: string
  params?: unknown[]
}

/** True for a request to a chain's RPC rather than to the app's API. */
export function isRpc(url: string) {
  return !url.startsWith('/api/')
}

/** Answers a JSON-RPC request from `handlers`; a method with no handler is an error response. */
export function answerRpc(init: RequestInit | undefined, handlers: RpcHandlers) {
  const body = JSON.parse(String(init?.body)) as RpcRequest | RpcRequest[]
  const answer = ({ id, method, params = [] }: RpcRequest) =>
    method in handlers
      ? { jsonrpc: '2.0', id, result: handlers[method](params) }
      : { jsonrpc: '2.0', id, error: { code: -32601, message: `No test handler for ${method}` } }
  return json(200, Array.isArray(body) ? body.map(answer) : answer(body))
}

export function Providers({
  children,
  wagmiConfig,
}: {
  children: ReactNode
  wagmiConfig: ReturnType<typeof makeWagmiConfig>
}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <ThemeProvider>
      <QueryClientProvider client={client}>
        {/* The app registers its own config type (wallet/config.ts); this one differs only in its connector. */}
        <WagmiProvider config={wagmiConfig as unknown as ComponentProps<typeof WagmiProvider>['config']}>
          <AuthProvider>
            <ChainProvider>{children}</ChainProvider>
          </AuthProvider>
        </WagmiProvider>
      </QueryClientProvider>
    </ThemeProvider>
  )
}

/** Mounts `routes` at `path` with every app-level provider, and returns the router for assertions. */
export function renderRoutes(
  routes: RouteObject[],
  path: string,
  { wagmiConfig = makeWagmiConfig() }: { wagmiConfig?: ReturnType<typeof makeWagmiConfig> } = {},
) {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(
    <Providers wagmiConfig={wagmiConfig}>
      <RouterProvider router={router} />
    </Providers>,
  )
  return router
}

export function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
}
