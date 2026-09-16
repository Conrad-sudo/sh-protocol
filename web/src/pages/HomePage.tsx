import { Link } from 'react-router'
import { Button, Text } from 'rsuite'
import { useAuth } from '../auth/useAuth'

/** Placeholder landing page; the full one is phase 9. */
export function HomePage() {
  const { status } = useAuth()

  return (
    <section className="mf-hero">
      <title>Mitfah — an AI assistant for your crypto wallet</title>
      <h1>An AI assistant for your crypto wallet that can only spend what you allow.</h1>
      <Text muted size="lg">
        You own the wallet. You set a daily dollar limit. You can pause it any time.
      </Text>
      <div className="mf-hero-actions">
        {status === 'signedIn' ? (
          <Button as={Link} to="/dashboard" appearance="primary" size="lg">
            Open your dashboard
          </Button>
        ) : (
          <>
            <Button as={Link} to="/signup" appearance="primary" size="lg">
              Get started
            </Button>
            <Button as={Link} to="/login" size="lg">
              Sign in
            </Button>
          </>
        )}
      </div>
    </section>
  )
}
