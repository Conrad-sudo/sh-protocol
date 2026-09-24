import { useId, type ReactNode } from 'react'

// A 270° gauge: it opens at the bottom, like a meter, and reads clockwise from lower left.
const C = 120
const START = 135
const SWEEP = 270
const TICKS = 40

function point(radius: number, degrees: number) {
  const rad = (degrees * Math.PI) / 180
  return [C + radius * Math.cos(rad), C + radius * Math.sin(rad)] as const
}

function arc(radius: number) {
  const [x1, y1] = point(radius, START)
  const [x2, y2] = point(radius, START + SWEEP)
  return `M ${x1} ${y1} A ${radius} ${radius} 0 1 1 ${x2} ${y2}`
}

const TRACK = arc(78)

// Engraved ticks every 2.5%, longer at each quarter.
const TICK_PATH = Array.from({ length: TICKS + 1 }, (_, i) => {
  const angle = START + (SWEEP * i) / TICKS
  const [x1, y1] = point(i % 10 === 0 ? 90 : 94, angle)
  const [x2, y2] = point(100, angle)
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} L ${x2.toFixed(2)} ${y2.toFixed(2)}`
}).join(' ')

interface LimitDialProps {
  /** How much of the limit is left, 0–100. */
  percent: number
  /** The accessible name, e.g. "60% of the limit left". */
  label: string
  /** Rendered diameter in px. */
  size?: number
  /** `off` greys the arc out (no limit, or the wallet is paused). */
  tone?: 'live' | 'off'
  className?: string
  /** What sits in the middle of the dial. */
  children?: ReactNode
}

/**
 * The spending limit as an instrument: a machined bezel, engraved ticks and a green arc for what's
 * left. It echoes the wallpaper's ring, and is the one bold element of every page it appears on.
 */
export function LimitDial({ percent, label, size = 220, tone = 'live', className, children }: LimitDialProps) {
  const id = useId()
  const left = Math.max(0, Math.min(100, Math.round(percent)))

  return (
    <div
      className={['mf-dial', className].filter(Boolean).join(' ')}
      data-tone={tone}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={left}
      style={{ width: size, height: size }}
    >
      <svg viewBox="0 0 240 240" aria-hidden focusable="false">
        <defs>
          <linearGradient id={`${id}-bezel`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--mf-bezel-light)" />
            <stop offset="1" stopColor="var(--mf-bezel-dark)" />
          </linearGradient>
        </defs>
        <circle className="mf-dial-face" cx={C} cy={C} r={108} />
        <circle className="mf-dial-bezel" cx={C} cy={C} r={112} stroke={`url(#${id}-bezel)`} />
        <path className="mf-dial-ticks" d={TICK_PATH} />
        <path className="mf-dial-track" d={TRACK} />
        {left > 0 && (
          <path
            className="mf-dial-value"
            d={TRACK}
            pathLength={100}
            style={{ strokeDasharray: `${left} 100` }}
          />
        )}
      </svg>
      {children && <div className="mf-dial-center">{children}</div>}
    </div>
  )
}
