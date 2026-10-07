import { useEffect, useState, type ReactNode } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router'
import { FullPageLoader } from '../components/FullPageLoader'
import { safeNext } from './safeNext'
import { useAuth } from './useAuth'

/** How long the opening's styles stay after the page changes; its transition lasts 520ms (app.css). */
const OPENING_MS = 1000
let closing: ReturnType<typeof setTimeout> | undefined

/**
 * Sends a signed-in user past the sign-in page to where they were headed.
 *
 * Someone who signs in here leaves through the opening safe (app.css, "Opening the safe"): the page
 * stays up until the next one is ready, and the change between them is a view transition, where the
 * browser has them. Arriving already signed in, there is nothing to open; they go straight on.
 */
export function RedirectIfSignedIn({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const next = safeNext(params.get('next'))
  // Seen signed out on this page, so a session from now on was started here.
  const [wasSignedOut, setWasSignedOut] = useState(false)
  if (status === 'signedOut' && !wasSignedOut) setWasSignedOut(true)
  const opening = wasSignedOut && status === 'signedIn'

  useEffect(() => {
    if (!opening) return
    const root = document.documentElement
    clearTimeout(closing)
    root.dataset.safe = 'open'
    void navigate(next, { replace: true, viewTransition: typeof document.startViewTransition === 'function' })
    return () => {
      closing = setTimeout(() => delete root.dataset.safe, OPENING_MS)
    }
  }, [opening, next, navigate])

  if (status === 'loading') return <FullPageLoader />
  if (status === 'signedIn' && !opening) return <Navigate to={next} replace />
  return children
}
