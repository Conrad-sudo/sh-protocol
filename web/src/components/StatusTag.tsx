import type { ReactNode } from 'react'

export type StatusTone = 'success' | 'neutral' | 'warning' | 'danger'

/**
 * A small status label. Colour follows the app's meaning: success = safe/active, warning = a limit
 * was loosened, danger = paused or destructive, neutral = not set up yet.
 */
export function StatusTag({ tone = 'neutral', children }: { tone?: StatusTone; children: ReactNode }) {
  return (
    <span className="mf-tag" data-tone={tone}>
      {children}
    </span>
  )
}
