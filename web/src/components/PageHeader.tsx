import type { ReactNode } from 'react'
import { Text } from 'rsuite'

interface PageHeaderProps {
  title: string
  description?: ReactNode
  actions?: ReactNode
}

/** A page's heading row. Also sets the document title (React 19 hoists <title> into <head>). */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <header className="mf-page-header">
      <title>{`${title} · Mitfah`}</title>
      <div>
        <h1 className="mf-page-title">{title}</h1>
        {description && <Text muted>{description}</Text>}
      </div>
      {actions && <div className="mf-page-actions">{actions}</div>}
    </header>
  )
}
