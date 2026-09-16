import type { ReactNode } from 'react'
import { Text } from 'rsuite'

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  children?: ReactNode
  action?: ReactNode
}

export function EmptyState({ icon, title, children, action }: EmptyStateProps) {
  return (
    <div className="mf-empty">
      {icon && <div className="mf-empty-icon">{icon}</div>}
      <h2 className="mf-empty-title">{title}</h2>
      {children && <Text muted>{children}</Text>}
      {action && <div className="mf-empty-action">{action}</div>}
    </div>
  )
}
