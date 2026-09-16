import { Link, Outlet } from 'react-router'
import { Logo } from '../components/brand/Logo'

/** A single centred card for sign-in and sign-up. */
export function AuthLayout() {
  return (
    <div className="mf-auth">
      <div className="mf-auth-card">
        <Link to="/" className="mf-auth-logo" aria-label="Mitfah home">
          <Logo size={40} />
        </Link>
        <Outlet />
      </div>
    </div>
  )
}
