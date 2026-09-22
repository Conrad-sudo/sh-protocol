import { Link, Outlet } from 'react-router'
import { Logo } from '../components/brand/Logo'
import { Wallpaper } from '../components/brand/Wallpaper'
import { LegalLinks } from '../components/LegalLinks'
import { RouteProgress } from '../components/RouteProgress'

/** A single centred card for sign-in and sign-up. */
export function AuthLayout() {
  return (
    <div className="mf-auth">
      <Wallpaper place="center" />
      <RouteProgress />
      {/* The card is the page's only content, so it is the main landmark. */}
      <main className="mf-auth-card" id="main-content" tabIndex={-1}>
        <Link to="/" className="mf-auth-logo" aria-label="Mitfah home">
          <Logo size={40} />
        </Link>
        <Outlet />
      </main>
      <LegalLinks />
    </div>
  )
}
