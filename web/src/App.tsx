import { useEffect } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router/dom'
import { queryClient } from './api/queryClient'
import { AuthProvider } from './auth/AuthProvider'
import { GoogleProvider } from './auth/GoogleProvider'
import { ChainProvider } from './chain/ChainProvider'
import { router } from './router'

// Browser wallets are set up inside the signed-in app (layouts/AppRoute), not here: nothing a
// signed-out visitor sees needs them, and that keeps wagmi and viem out of the first load.
export default function App() {
  // The page has now rendered its own title and meta tags, so the copies written into the HTML
  // (index.html, or a prerendered page's) go. Left in, they would describe the wrong page after a
  // navigation, and two canonical links make search engines ignore both.
  useEffect(() => {
    document.head.querySelectorAll('[data-static]').forEach(tag => tag.remove())
  }, [])

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
