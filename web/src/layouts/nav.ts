import DashboardIcon from '@rsuite/icons/Dashboard'
import GearIcon from '@rsuite/icons/Gear'
import MessageIcon from '@rsuite/icons/Message'
import PeoplesIcon from '@rsuite/icons/Peoples'
import ShieldIcon from '@rsuite/icons/Shield'

/** The signed-in app's sections. The sidebar and the mobile tab bar both render this list. */
export const NAV_ITEMS = [
  { to: '/dashboard', label: 'Dashboard', short: 'Home', Icon: DashboardIcon },
  { to: '/assistant', label: 'Assistant', short: 'Assistant', Icon: MessageIcon },
  { to: '/contacts', label: 'Contacts', short: 'Contacts', Icon: PeoplesIcon },
  { to: '/controls', label: 'Controls', short: 'Controls', Icon: ShieldIcon },
  { to: '/settings', label: 'Settings', short: 'Settings', Icon: GearIcon },
] as const

export function isActive(pathname: string, to: string) {
  return pathname === to || pathname.startsWith(`${to}/`)
}
