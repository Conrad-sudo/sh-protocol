import { QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter } from 'react-router'
import { RouterProvider } from 'react-router/dom'
import { queryClient } from './api/queryClient'
import { AuthProvider } from './auth/AuthProvider'
import { GoogleProvider } from './auth/GoogleProvider'
import { ChainProvider } from './chain/ChainProvider'
import { routes } from './routes'

const router = createBrowserRouter(routes)

// Browser wallets are set up inside the signed-in app (layouts/AppRoute), not here: nothing a
// signed-out visitor sees needs them, and that keeps wagmi and viem out of the first load.
export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <GoogleProvider>
        <AuthProvider>
          <ChainProvider>
            <RouterProvider router={router} />
          </ChainProvider>
        </AuthProvider>
      </GoogleProvider>
    </QueryClientProvider>
  )
}
