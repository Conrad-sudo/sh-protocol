import { Link, Outlet } from 'react-router'
import { Logo } from '../components/brand/Logo'
import { Wallpaper } from '../components/brand/Wallpaper'
import { GlassLayer } from '../components/Glass'
import { LegalLinks } from '../components/LegalLinks'
import { RouteProgress } from '../components/RouteProgress'

/** A single centred glass card for sign-in and sign-up. */
export function AuthLayout() {
  return (
    <div className="mf-auth">
      <Wallpaper place="center" />
      <RouteProgress />
      {/* The card is the page's only content, so it is the main landmark. */}
      <main className="mf-auth-card mf-glass-surface" id="main-content" tabIndex={-1}>
        <GlassLayer />
        <div className="mf-auth-body">
          <Link to="/" className="mf-auth-logo" aria-label="Mitfah home">
            <Logo size={40} />
          </Link>
          <Outlet />
        </div>
      </main>
      <LegalLinks />
    </div>
  )
}
