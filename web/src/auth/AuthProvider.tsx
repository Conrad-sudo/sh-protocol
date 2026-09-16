import { useEffect, useSyncExternalStore, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { logout } from '../api/auth'
import {
  clearSession,
  getAuthSnapshot,
  refreshSession,
  restoreSession,
  setSession,
  subscribeAuth,
} from '../api/client'
import type { TokenResponse } from '../api/types'
import { AuthContext } from './AuthContext'

const CHANNEL = 'mitfah-auth'
const SIGNED_OUT = 'signed-out'

/**
 * Owns the signed-in state for the whole app.
 *
 * On load it tries to restore the session from the refresh cookie. StrictMode runs that effect
 * twice in development; both runs share refreshSession's single in-flight request, so the cookie
 * is rotated once — a second rotation of the same cookie would sign the user out everywhere.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const { status, userId } = useSyncExternalStore(subscribeAuth, getAuthSnapshot)
  const queryClient = useQueryClient()

  useEffect(() => {
    void restoreSession()
  }, [])

  // Sign-outs in other tabs end this tab's session too; a tab that comes back into view while
  // signed out checks whether another tab signed in meanwhile.
  useEffect(() => {
    const channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(CHANNEL)
    const onMessage = (event: MessageEvent) => {
      if (event.data === SIGNED_OUT) {
        clearSession()
        queryClient.clear()
      }
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible' && getAuthSnapshot().status === 'signedOut') {
        refreshSession().catch(() => {})
      }
    }
    channel?.addEventListener('message', onMessage)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      channel?.close()
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [queryClient])

  const signIn = (token: TokenResponse) => {
    queryClient.clear()
    setSession(token)
  }

  const signOut = async () => {
    try {
      await logout()
    } finally {
      // Signed out locally even if the server call failed: the user asked to leave.
      clearSession()
      queryClient.clear()
      if (typeof BroadcastChannel !== 'undefined') {
        const channel = new BroadcastChannel(CHANNEL)
        channel.postMessage(SIGNED_OUT)
        channel.close()
      }
    }
  }

  return <AuthContext value={{ status, userId, signIn, signOut }}>{children}</AuthContext>
}
