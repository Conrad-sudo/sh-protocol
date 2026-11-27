import type { ReactNode } from 'react'
import { Explained } from '../Explained'

interface ControlRowProps {
  title: string
  status?: ReactNode
  help?: ReactNode
  action?: ReactNode
  /** Shown under the row across its full width: an editor, or a transaction's progress. */
  children?: ReactNode
}

/** One setting: "Title: state, explanation" on the left, its action on the right. */
export function ControlRow({ title, status, help, action, children }: ControlRowProps) {
  return (
    <div className="mf-control-row">
      <div className="mf-control-main">
        <div className="mf-control-label">
          <Explained level={3} title={title} status={status}>
            {help}
          </Explained>
        </div>
        {action && <div className="mf-control-action">{action}</div>}
      </div>
      {children && <div className="mf-control-body">{children}</div>}
    </div>
  )
}
