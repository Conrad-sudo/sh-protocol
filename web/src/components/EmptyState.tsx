import type { ReactNode } from 'react'
import { Text } from 'rsuite'

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  children?: ReactNode
  action?: ReactNode
  /** `1` when the empty state is the whole page, as on the 404 page. */
  level?: 1 | 2
}

export function EmptyState({ icon, title, children, action, level = 2 }: EmptyStateProps) {
  const Heading = level === 1 ? 'h1' : 'h2'
  return (
    <div className="mf-empty">
      {icon && <div className="mf-empty-icon">{icon}</div>}
      <Heading className="mf-empty-title">{title}</Heading>
      {children && <Text muted>{children}</Text>}
      {action && <div className="mf-empty-action">{action}</div>}
    </div>
  )
}
