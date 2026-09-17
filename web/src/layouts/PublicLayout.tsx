import { Link, Outlet } from 'react-router'
import { useAuth } from '../auth/useAuth'
import { Logo } from '../components/brand/Logo'
import { LinkButton } from '../components/LinkButton'

/** Header for public pages. The full landing page and footer arrive in phase 9. */
export function PublicLayout() {
  const { status } = useAuth()

  return (
    <div className="mf-public">
      <header className="mf-public-header">
        <Link to="/" aria-label="Mitfah home">
          <Logo size={30} />
        </Link>
        <nav className="mf-public-nav" aria-label="Account">
          {status === 'signedIn' ? (
            <LinkButton to="/dashboard" appearance="primary">
              Open app
            </LinkButton>
          ) : (
            <>
              <LinkButton to="/login" appearance="subtle">
                Sign in
              </LinkButton>
              <LinkButton to="/signup" appearance="primary">
                Get started
              </LinkButton>
            </>
          )}
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  )
}
