import type { ReactNode } from 'react'
import { Explained } from './Explained'

interface PageHeaderProps {
  title: string
  description?: ReactNode
  actions?: ReactNode
}

/**
 * A page's heading row: the title with its description beside it. Also sets the document title
 * (React 19 hoists <title> into <head>).
 */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <header className="mf-page-header">
      <title>{`${title} · Mitfah`}</title>
      <Explained level={1} title={title} titleClassName="mf-page-title">
        {description}
      </Explained>
      {actions && <div className="mf-page-actions">{actions}</div>}
    </header>
  )
}
