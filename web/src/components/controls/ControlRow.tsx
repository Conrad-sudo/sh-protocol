import type { ReactNode } from 'react'
import { Text } from 'rsuite'

interface ControlRowProps {
  title: string
  status?: ReactNode
  help: ReactNode
  action?: ReactNode
  /** Shown under the row across its full width: an editor, or a transaction's progress. */
  children?: ReactNode
}

/** One setting: what it is and its state on the left, its action on the right. */
export function ControlRow({ title, status, help, action, children }: ControlRowProps) {
  return (
    <div className="mf-control-row">
      <div className="mf-control-main">
        <div className="mf-control-label">
          <div className="mf-control-title">
            <h3>{title}</h3>
            {status}
          </div>
          <Text muted size="sm">
            {help}
          </Text>
        </div>
        {action && <div className="mf-control-action">{action}</div>}
      </div>
      {children && <div className="mf-control-body">{children}</div>}
    </div>
  )
}
