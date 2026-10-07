import { useRef } from 'react'
import { Link, Outlet } from 'react-router'
import { Logo } from '../components/brand/Logo'
import { openSafe } from '../components/brand/openSafe'
import { Wallpaper } from '../components/brand/Wallpaper'
import { GlassLayer } from '../components/Glass'
import { LegalLinks } from '../components/LegalLinks'
import { RouteProgress } from '../components/RouteProgress'

/** What the sign-in page gets from this layout (`useOutletContext`). */
export interface AuthOutlet {
  /** Turns the safe dial behind the card through its combination; resolves once it has opened. */
  openSafe: () => Promise<void>
}

/**
 * Sign-in: a glass disc in the middle of the wallpaper's safe dial, filling its face (on a phone,
 * the form lies on the wallpaper, above the dial).
 */
export function AuthLayout() {
  const wallpaper = useRef<HTMLDivElement>(null)
  const outlet: AuthOutlet = { openSafe: () => openSafe(wallpaper.current) }

  return (
    <div className="mf-auth">
      <Wallpaper place="center" ref={wallpaper} />
      <RouteProgress />
      {/* The card is the page's only content, so it is the main landmark. */}
      <main className="mf-auth-card mf-glass-surface" id="main-content" tabIndex={-1}>
        <GlassLayer shape="disc" />
        <div className="mf-auth-body">
          <Link to="/" className="mf-auth-logo" aria-label="Mitfah home">
            <Logo size={40} />
          </Link>
          <Outlet context={outlet} />
          {/* Inside the disc, which is centred on the dial: nothing else may share the column. */}
          <LegalLinks />
        </div>
      </main>
    </div>
  )
}
