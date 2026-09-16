import { NavLink } from 'react-router'
import { NAV_ITEMS } from './nav'

/** Bottom tabs for phones. NavLink sets aria-current="page" on the active tab. */
export function MobileTabBar() {
  return (
    <nav className="mf-tabbar" aria-label="Main">
      {NAV_ITEMS.map(({ to, short, Icon }) => (
        <NavLink key={to} to={to} className="mf-tab">
          <Icon />
          <span>{short}</span>
        </NavLink>
      ))}
    </nav>
  )
}
