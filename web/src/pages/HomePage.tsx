import { Text } from 'rsuite'
import { useAuth } from '../auth/useAuth'
import { LinkButton } from '../components/LinkButton'

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
          <LinkButton to="/dashboard" appearance="primary" size="lg">
            Open your dashboard
          </LinkButton>
        ) : (
          <>
            <LinkButton to="/signup" appearance="primary" size="lg">
              Get started
            </LinkButton>
            <LinkButton to="/login" size="lg">
              Sign in
            </LinkButton>
          </>
        )}
      </div>
    </section>
  )
}
