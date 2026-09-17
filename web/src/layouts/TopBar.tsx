import { Link } from 'react-router'
import { Logo } from '../components/brand/Logo'
import { ThemeToggleButton } from '../components/ThemeSwitch'
import { ConnectButton } from '../components/wallet/ConnectButton'
import type { LayoutMode } from './useLayoutMode'

/**
 * The bar above the page. Pages carry their own <h1>, so this holds only global controls: the
 * wallet connection and the theme toggle. On phones, where there is no sidebar, it also carries the
 * logo.
 */
export function TopBar({ mode }: { mode: LayoutMode }) {
  return (
    <header className="mf-topbar">
      {mode === 'mobile' && (
        <Link to="/dashboard" aria-label="Mitfah dashboard">
          <Logo variant="lockup" size={24} />
        </Link>
      )}
      <div className="mf-topbar-actions">
        <ConnectButton />
        <ThemeToggleButton />
      </div>
    </header>
  )
}
