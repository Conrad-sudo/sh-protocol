import { QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter } from 'react-router'
import { RouterProvider } from 'react-router/dom'
import { WagmiProvider } from 'wagmi'
import { queryClient } from './api/queryClient'
import { AuthProvider } from './auth/AuthProvider'
import { GoogleProvider } from './auth/GoogleProvider'
import { ChainProvider } from './chain/ChainProvider'
import { routes } from './routes'
import { wagmiConfig } from './wallet/config'

const router = createBrowserRouter(routes)

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <WagmiProvider config={wagmiConfig}>
        <GoogleProvider>
          <AuthProvider>
            <ChainProvider>
              <RouterProvider router={router} />
            </ChainProvider>
          </AuthProvider>
        </GoogleProvider>
      </WagmiProvider>
    </QueryClientProvider>
  )
}
