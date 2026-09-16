import { useEffect, useRef, useState } from 'react'
import { GoogleLogin } from '@react-oauth/google'
import { googleEnabled } from '../auth/google'
import { useTheme } from '../theme/useTheme'

interface GoogleButtonProps {
  text: 'signin_with' | 'signup_with' | 'continue_with'
  /** Called with Google's ID token, for the API to verify. */
  onCredential: (idToken: string) => void
  /** The popup was closed or blocked, or Google returned no token. */
  onFailure: () => void
}

// Google renders its button in an iframe with a fixed pixel width, inside these bounds.
const MIN_WIDTH = 200
const MAX_WIDTH = 400

/** Google's own sign-in button, sized to its container and matched to our theme. */
export function GoogleButton({ text, onCredential, onFailure }: GoogleButtonProps) {
  const { resolved } = useTheme()
  const container = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const element = container.current
    if (!element) return
    const measure = () => {
      // Unmeasurable (not laid out yet, or a test DOM) falls back to the widest button.
      const available = element.getBoundingClientRect().width || MAX_WIDTH
      // Snapped to 10px steps: each change re-creates Google's iframe, so skip sub-step jitter.
      setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.floor(available / 10) * 10)))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  if (!googleEnabled) return null

  return (
    <div ref={container} className="mf-google-button">
      {width > 0 && (
        <GoogleLogin
          // Re-render Google's iframe when the size or theme changes; it does not restyle in place.
          key={`${resolved}-${width}`}
          text={text}
          theme={resolved === 'dark' ? 'filled_black' : 'outline'}
          size="large"
          shape="rectangular"
          width={width}
          onSuccess={response => (response.credential ? onCredential(response.credential) : onFailure())}
          onError={onFailure}
        />
      )}
    </div>
  )
}
