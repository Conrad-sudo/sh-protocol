import { Link, Outlet } from 'react-router'
import { Button } from 'rsuite'
import { useAuth } from '../auth/useAuth'
import { Logo } from '../components/brand/Logo'

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
            <Button as={Link} to="/dashboard" appearance="primary">
              Open app
            </Button>
          ) : (
            <>
              <Button as={Link} to="/login" appearance="subtle">
                Sign in
              </Button>
              <Button as={Link} to="/signup" appearance="primary">
                Get started
              </Button>
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
