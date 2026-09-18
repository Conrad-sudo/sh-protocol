import ExitIcon from '@rsuite/icons/Exit'
import { Link, useLocation } from 'react-router'
import { Button, IconButton, Sidenav, Text, Tooltip, Whisper } from 'rsuite'
import { Logo } from '../components/brand/Logo'
import { useMe } from '../hooks/useMe'
import { useSignOut } from '../hooks/useSignOut'
import { isActive, NAV_ITEMS } from './nav'

interface SidebarProps {
  expanded: boolean
  /** Shows the expand/collapse toggle (tablet). */
  collapsible: boolean
  onToggle: (expanded: boolean) => void
}

/** The navy side navigation: full width on desktop, an icon rail on tablets. */
export function Sidebar({ expanded, collapsible, onToggle }: SidebarProps) {
  const { pathname } = useLocation()
  const { data: me } = useMe()
  const signOut = useSignOut()

  return (
    <aside className="mf-sidebar" data-expanded={expanded}>
      {/* Sidenav renders the <nav> landmark itself. */}
      <Sidenav appearance="inverse" expanded={expanded} aria-label="Main">
        <Sidenav.Header className="mf-sidebar-header">
          <Link to="/dashboard" aria-label="Mitfah dashboard">
            <Logo variant={expanded ? 'lockup' : 'mark'} size={28} tone="onDark" />
          </Link>
        </Sidenav.Header>
        <Sidenav.Body>
          {/*
           * The list is written out rather than built from RSuite's Nav, which puts its links
           * straight inside the <ul> and marks the current one aria-selected — an attribute links
           * may not carry. Its classes still do the styling.
           */}
          <ul className="rs-sidenav-nav rs-nav mf-nav-list">
            {NAV_ITEMS.map(({ to, label, Icon }) => {
              const active = isActive(pathname, to)
              return (
                <li key={to}>
                  <Link
                    to={to}
                    className="rs-sidenav-item"
                    data-active={active}
                    // The collapsed rail drops the text, leaving screen readers nothing to announce.
                    aria-label={label}
                    aria-current={active ? 'page' : undefined}
                  >
                    <Icon className="rs-sidenav-item-icon rs-icon" aria-hidden />
                    <span className="rs-sidenav-item-title">{label}</span>
                  </Link>
                </li>
              )
            })}
          </ul>
        </Sidenav.Body>
        {collapsible && <Sidenav.Toggle onToggle={onToggle} />}
      </Sidenav>

      <div className="mf-sidebar-footer">
        {expanded ? (
          <>
            {me?.email && (
              <Text size="sm" className="mf-sidebar-email" title={me.email}>
                {me.email}
              </Text>
            )}
            <Button appearance="subtle" startIcon={<ExitIcon />} onClick={signOut} block>
              Sign out
            </Button>
          </>
        ) : (
          <Whisper placement="right" speaker={<Tooltip>Sign out</Tooltip>}>
            <IconButton appearance="subtle" icon={<ExitIcon />} aria-label="Sign out" onClick={signOut} />
          </Whisper>
        )}
      </div>
    </aside>
  )
}
