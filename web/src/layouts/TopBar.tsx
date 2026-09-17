import { Link } from 'react-router'
import { Logo } from '../components/brand/Logo'
import { ThemeToggleButton } from '../components/ThemeSwitch'
import { ChainSwitcher } from '../components/wallet/ChainSwitcher'
import { ConnectButton } from '../components/wallet/ConnectButton'
import type { LayoutMode } from './useLayoutMode'

/**
 * The bar above the page. Pages carry their own <h1>, so this holds only global controls: the
 * network being shown, the wallet connection and the theme toggle. On phones, where there is no
 * sidebar, it also carries the logo.
 */
export function TopBar({ mode }: { mode: LayoutMode }) {
  return (
    <header className="mf-topbar">
      {mode === 'mobile' && (
        // The mark alone: a phone's bar also has to fit the network and the wallet.
        <Link to="/dashboard" aria-label="Mitfah dashboard">
          <Logo variant="mark" size={28} />
        </Link>
      )}
      <ChainSwitcher />
      <div className="mf-topbar-actions">
        <ConnectButton compact={mode === 'mobile'} />
        <ThemeToggleButton />
      </div>
    </header>
  )
}
