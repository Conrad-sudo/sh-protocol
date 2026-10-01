import type { CSSProperties } from 'react'
import { NavLink, useLocation } from 'react-router'
import { GlassLayer } from '../components/Glass'
import { isActive, NAV_ITEMS } from './nav'

/**
 * Bottom tabs for phones, in a glass capsule floating over the page. NavLink sets
 * aria-current="page" on the active tab; a lit pill sits under it and slides to the next one.
 */
export function MobileTabBar() {
  const { pathname } = useLocation()
  const current = NAV_ITEMS.findIndex(({ to }) => isActive(pathname, to))

  return (
    <nav
      className="mf-tabbar mf-glass-surface"
      aria-label="Main"
      style={{ '--mf-tab': current, '--mf-tabs': NAV_ITEMS.length } as CSSProperties}
    >
      <GlassLayer shape="capsule" />
      {current >= 0 && <span className="mf-tab-pill" aria-hidden />}
      {NAV_ITEMS.map(({ to, short, Icon }) => (
        <NavLink key={to} to={to} className="mf-tab">
          <Icon />
          <span>{short}</span>
        </NavLink>
      ))}
    </nav>
  )
}
