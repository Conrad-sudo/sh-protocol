import type { ReactNode } from 'react'
import { Navigate, useSearchParams } from 'react-router'
import { FullPageLoader } from '../components/FullPageLoader'
import { safeNext } from './safeNext'
import { useAuth } from './useAuth'

/** Sends a signed-in user past the login and sign-up pages to where they were headed. */
export function RedirectIfSignedIn({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const [params] = useSearchParams()

  if (status === 'loading') return <FullPageLoader />
  if (status === 'signedIn') return <Navigate to={safeNext(params.get('next'))} replace />
  return children
}
