import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { googleSignIn } from '../api/auth'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/useAuth'

const INCOMPLETE = "Google sign-in didn't complete. Please try again."

export interface GoogleSignInError {
  message: string
  /** A password account already has this email: the fix is to sign in with the password. */
  emailTaken: boolean
}

/**
 * Google sign-in for the login and sign-up pages. A success starts the session, and the route
 * guard around those pages moves the user on.
 */
export function useGoogleSignIn() {
  const { signIn } = useAuth()
  const [popupFailed, setPopupFailed] = useState(false)
  const mutation = useMutation({ mutationFn: googleSignIn, onSuccess: signIn })

  let error: GoogleSignInError | null = null
  if (popupFailed) {
    error = { message: INCOMPLETE, emailTaken: false }
  } else if (mutation.error) {
    const e = mutation.error
    const status = e instanceof ApiError ? e.status : 0
    error = {
      // 400 is a token Google issued but the API rejected (expired, wrong audience); to the user
      // that is the same as the popup failing.
      message: status === 400 || !(e instanceof ApiError) ? INCOMPLETE : e.message,
      emailTaken: status === 409,
    }
  }

  return {
    error,
    pending: mutation.isPending,
    onCredential: (idToken: string) => {
      setPopupFailed(false)
      mutation.mutate(idToken)
    },
    onFailure: () => {
      mutation.reset()
      setPopupFailed(true)
    },
    reset: () => {
      mutation.reset()
      setPopupFailed(false)
    },
  }
}
