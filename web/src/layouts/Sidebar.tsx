import ExitIcon from '@rsuite/icons/Exit'
import { Link, useLocation } from 'react-router'
import { Button, IconButton, Nav, Sidenav, Text, Tooltip, Whisper } from 'rsuite'
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
          <Nav>
            {NAV_ITEMS.map(({ to, label, Icon }) => {
              const active = isActive(pathname, to)
              return (
                <Nav.Item
                  key={to}
                  as={Link}
                  to={to}
                  icon={<Icon />}
                  active={active}
                  // The collapsed rail drops the text, leaving screen readers nothing to announce.
                  aria-label={label}
                  aria-current={active ? 'page' : undefined}
                >
                  {label}
                </Nav.Item>
              )
            })}
          </Nav>
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
