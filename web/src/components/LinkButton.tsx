import type { ReactNode } from 'react'
import { Link } from 'react-router'
import { Button, type ButtonProps } from 'rsuite'

type LinkButtonProps = Omit<ButtonProps, 'as' | 'href' | 'role'> & { to: string; children: ReactNode }

/**
 * A link styled as a button. RSuite gives any non-<button> element role="button", which makes
 * screen readers announce a navigation as an action; this keeps it a link.
 */
export function LinkButton({ to, ...props }: LinkButtonProps) {
  return <Button as={Link} to={to} role="link" {...props} />
}
