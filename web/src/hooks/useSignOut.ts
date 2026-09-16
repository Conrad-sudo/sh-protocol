import { useNavigate } from 'react-router'
import { useAuth } from '../auth/useAuth'

/**
 * Signs out and lands on /login. Navigating afterwards replaces the `?next=` the route guard adds
 * when the session disappears — a deliberate sign-out should not bounce back to the same page.
 */
export function useSignOut() {
  const { signOut } = useAuth()
  const navigate = useNavigate()
  return async () => {
    await signOut().catch(() => {})
    navigate('/login', { replace: true })
  }
}
