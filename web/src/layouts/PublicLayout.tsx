import { Link, Outlet } from 'react-router'
import { useAuth } from '../auth/useAuth'
import { Logo } from '../components/brand/Logo'
import { LegalLinks } from '../components/LegalLinks'
import { LinkButton } from '../components/LinkButton'
import { RouteProgress } from '../components/RouteProgress'
import { SkipLink } from '../components/SkipLink'

/** Header and footer around the landing and legal pages. */
export function PublicLayout() {
  const { status } = useAuth()

  return (
    <div className="mf-public">
      <SkipLink />
      <RouteProgress />
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
      <main id="main-content" tabIndex={-1}>
        <Outlet />
      </main>
      <footer className="mf-public-footer">
        <Logo variant="mark" size={24} />
        <span>© {new Date().getFullYear()} Mitfah</span>
        <LegalLinks />
      </footer>
    </div>
  )
}
