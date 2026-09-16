import type { ReactNode } from 'react'
import { GoogleOAuthProvider } from '@react-oauth/google'
import { GOOGLE_CLIENT_ID, googleEnabled } from './google'

/** Loads Google's sign-in script, but only when Google sign-in is configured. */
export function GoogleProvider({ children }: { children: ReactNode }) {
  if (!googleEnabled) return children
  // `locale` fixes the button's language; otherwise Google picks it from the visitor's location,
  // and the rest of the site is English.
  return (
    <GoogleOAuthProvider clientId={GOOGLE_CLIENT_ID} locale="en">
      {children}
    </GoogleOAuthProvider>
  )
}
