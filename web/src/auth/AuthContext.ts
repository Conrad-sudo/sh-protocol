import { createContext } from 'react'
import type { AuthStatus } from '../api/client'
import type { TokenResponse } from '../api/types'

export interface AuthState {
  status: AuthStatus
  userId: number | null
  /** Starts a session from a login/signup response. */
  signIn: (token: TokenResponse) => void
  /** Ends the session here, on the server, and in the user's other tabs. */
  signOut: () => Promise<void>
}

export const AuthContext = createContext<AuthState | null>(null)
