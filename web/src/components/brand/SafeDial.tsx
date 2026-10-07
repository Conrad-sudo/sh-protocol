import { useId } from 'react'
import { MARKS } from './openSafe'

/*
 * The safe dial at the middle of the wallpaper (Wallpaper.tsx): a ring that stays still, with the
 * index at the top, round a dial that turns — a scale of 100 marks, longer every five and ten, and a
 * ridged grip on its inner edge. It is drawn here, on the page, rather than into the wallpaper's image
 * (scripts/wallpaper), so that it can turn: signing in dials a combination and opens it (openSafe.ts).
 *
 * Three layers, one over another: what lies under the dial (its shadow and the seam round it), the
 * dial itself, which is the only part that turns, and what lies over it — the fixed ring, the index
 * and the light, which stays where it is while the marks run under it, as on real metal. Turning
 * the dial's own <svg>, not a group inside it, lets the browser turn it without repainting it.
 *
 * Units are the wallpaper's: one unit is one CSS pixel at full size, and the outer edge (OUTER) is
 * the bus ring that the wallpaper's traces leave from. Colours are in app.css ("Safe dial").
 */

const SIZE = 1000
const C = SIZE / 2
/** The ring that stays still. Its outer edge is BUS in scripts/wallpaper/render.ts. */
const OUTER = 468
const RING_IN = 448
/** The dial that turns: the scale from its outer edge in, then the grip. */
const DIAL_OUT = 444
const SCALE_OUT = 442
const GRIP_OUT = 392
const DIAL_IN = 366
/** The grip's grooves. */
const GROOVES = 120

/** A point `r` from the centre, `deg` degrees clockwise from the top. */
function at(r: number, deg: number) {
  const a = (deg * Math.PI) / 180
  return [C + r * Math.sin(a), C - r * Math.cos(a)] as const
}

const fixed = (n: number) => Math.round(n * 100) / 100

/**
 * A gradient stop's colour, and its opacity, from app.css's --mf-safe-* variables (a number is used
 * as it is). Inline, as a class's rule would override every stop's own opacity.
 */
function paint(colour: string, opacity?: string | number) {
  return {
    stopColor: `var(--mf-safe-${colour})`,
    stopOpacity: typeof opacity === 'string' ? `var(--mf-safe-${opacity})` : opacity,
  }
}

/** Lines in from the scale's outer edge, `length` long, at every mark `pick` keeps. */
function marks(pick: (mark: number) => boolean, length: number) {
  let d = ''
  for (let mark = 0; mark < MARKS; mark++) {
    if (!pick(mark)) continue
    const [x1, y1] = at(SCALE_OUT, (mark * 360) / MARKS)
    const [x2, y2] = at(SCALE_OUT - length, (mark * 360) / MARKS)
    d += `M${fixed(x1)} ${fixed(y1)}L${fixed(x2)} ${fixed(y2)}`
  }
  return d
}

/** The band between two circles round the centre, as one shape (filled even-odd). */
function band(inner: number, outer: number) {
  const circle = (r: number) => `M${C - r} ${C}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`
  return circle(outer) + circle(inner)
}

/** An arc round the centre, `from` → `to` degrees clockwise from the top. */
function arc(r: number, from: number, to: number) {
  const [x1, y1] = at(r, from)
  const [x2, y2] = at(r, to)
  return `M${fixed(x1)} ${fixed(y1)}A${r} ${r} 0 ${to - from > 180 ? 1 : 0} 1 ${fixed(x2)} ${fixed(y2)}`
}

const UNITS = marks(mark => mark % 5 !== 0, 8)
const FIVES = marks(mark => mark % 10 === 5, 13)
const TENS = marks(mark => mark % 10 === 0, 18)
const FACE = band(DIAL_IN, DIAL_OUT)
/** Where the light catches the grip, at its upper left. */
const SHEEN = at((DIAL_IN + GRIP_OUT) / 2, 315)
/** The index: a pointer on the fixed ring, its tip just over the dial's edge. */
const INDEX = `M${C - 11} ${C - OUTER + 2}L${C + 11} ${C - OUTER + 2}L${C} ${C - DIAL_OUT + 7}Z`

/** The marks. Drawn twice: a light copy a hair lower makes them look engraved. */
function Scale({ className, dy = 0 }: { className: string; dy?: number }) {
  return (
    <g className={className} transform={dy ? `translate(0 ${dy})` : undefined}>
      <path d={UNITS} strokeWidth={1.3} />
      <path d={FIVES} strokeWidth={1.8} />
      <path d={TENS} strokeWidth={2.4} />
    </g>
  )
}

export function SafeDial() {
  const id = useId()
  const url = (name: string) => `url(#${id}-${name})`
  const lo = C - OUTER - 40
  const hi = C + OUTER + 40
  // Lit from the upper left, along the diagonal to the lower right.
  const diagonal = { gradientUnits: 'userSpaceOnUse', x1: lo, y1: lo, x2: hi, y2: hi } as const

  return (
    <div className="mf-safe">
      <svg className="mf-safe-base" viewBox={`0 0 ${SIZE} ${SIZE}`} focusable="false">
        <defs>
          {/* The shadow the dial casts on the page, a little below it. */}
          <radialGradient id={`${id}-shadow`} gradientUnits="userSpaceOnUse" cx={C} cy={C + 14} r={OUTER + 40}>
            <stop offset={fixed((OUTER - 24) / (OUTER + 40))} style={paint('shadow', 0)} />
            <stop offset={fixed(OUTER / (OUTER + 40))} style={paint('shadow', 'shadow-alpha')} />
            <stop offset={1} style={paint('shadow', 0)} />
          </radialGradient>
          {/* And on the face inside it, below its upper edge. */}
          <radialGradient id={`${id}-hollow`} gradientUnits="userSpaceOnUse" cx={C} cy={C + 12} r={DIAL_IN}>
            <stop offset={0.86} style={paint('shadow', 0)} />
            <stop offset={1} style={paint('shadow', 'shadow-alpha')} />
          </radialGradient>
        </defs>
        <circle cx={C} cy={C + 14} r={OUTER + 40} fill={url('shadow')} />
        <circle cx={C} cy={C + 12} r={DIAL_IN} fill={url('hollow')} />
        <circle className="mf-safe-seam" cx={C} cy={C} r={(DIAL_OUT + RING_IN) / 2} strokeWidth={6} />
      </svg>

      <svg className="mf-safe-rotor" viewBox={`0 0 ${SIZE} ${SIZE}`} focusable="false">
        <path className="mf-safe-face" d={FACE} fillRule="evenodd" />
        <circle className="mf-safe-grip" cx={C} cy={C} r={(DIAL_IN + GRIP_OUT) / 2} strokeWidth={GRIP_OUT - DIAL_IN} />
        {/* Grooves, and a lit crest along the middle of each ridge between them. */}
        <circle
          className="mf-safe-grooves"
          cx={C}
          cy={C}
          r={(DIAL_IN + GRIP_OUT) / 2}
          strokeWidth={GRIP_OUT - DIAL_IN - 4}
          pathLength={GROOVES}
          strokeDasharray="0.36 0.64"
        />
        <circle
          className="mf-safe-crests"
          cx={C}
          cy={C}
          r={(DIAL_IN + GRIP_OUT) / 2}
          strokeWidth={GRIP_OUT - DIAL_IN - 6}
          pathLength={GROOVES}
          strokeDasharray="0.1 0.9"
          strokeDashoffset={-0.63}
        />
        <Scale className="mf-safe-engraving" dy={1.2} />
        <Scale className="mf-safe-scale" />
      </svg>

      <svg className="mf-safe-cover" viewBox={`0 0 ${SIZE} ${SIZE}`} focusable="false">
        <defs>
          <linearGradient id={`${id}-light`} {...diagonal}>
            <stop offset={0.12} stopColor="#fff" style={{ stopOpacity: 'var(--mf-safe-lit)' }} />
            <stop offset={0.5} stopColor="#fff" stopOpacity={0} />
            <stop offset={0.5} stopColor="#000" stopOpacity={0} />
            <stop offset={0.92} stopColor="#000" style={{ stopOpacity: 'var(--mf-safe-shade)' }} />
          </linearGradient>
          {/* The ring's polished metal: light and dark bands in turn, as on the logo's ring. */}
          <linearGradient id={`${id}-metal`} {...diagonal}>
            {[0, 0.16, 0.3, 0.42, 0.56, 0.7, 0.84, 1].map((offset, i) => (
              <stop key={offset} offset={offset} style={paint(`metal-${i}`)} />
            ))}
          </linearGradient>
          <linearGradient id={`${id}-edge`} {...diagonal}>
            <stop offset={0} stopColor="#fff" stopOpacity={0.95} />
            <stop offset={0.45} stopColor="#fff" stopOpacity={0} />
          </linearGradient>
          <linearGradient id={`${id}-under`} {...diagonal}>
            <stop offset={0.4} style={paint('under', 0)} />
            <stop offset={1} style={paint('under', 'under-alpha')} />
          </linearGradient>
          <linearGradient id={`${id}-dim`} {...diagonal}>
            <stop offset={0.55} stopColor="#000" stopOpacity={0} />
            <stop offset={1} stopColor="#000" stopOpacity={0.45} />
          </linearGradient>
          <radialGradient id={`${id}-sheen`} gradientUnits="userSpaceOnUse" cx={SHEEN[0]} cy={SHEEN[1]} r={150}>
            <stop offset={0} stopColor="#fff" style={{ stopOpacity: 'var(--mf-safe-sheen)' }} />
            <stop offset={1} stopColor="#fff" stopOpacity={0} />
          </radialGradient>
          <radialGradient id={`${id}-glow`}>
            <stop offset={0} style={paint('glow', 0.6)} />
            <stop offset={1} style={paint('glow', 0)} />
          </radialGradient>
        </defs>

        {/* Light over the dial, brighter to the upper left and shaded to the lower right, and a sheen on the grip. */}
        <path d={FACE} fillRule="evenodd" fill={url('light')} />
        <path d={band(DIAL_IN, GRIP_OUT)} fillRule="evenodd" fill={url('sheen')} />
        <circle cx={C} cy={C} r={DIAL_OUT - 0.6} fill="none" stroke={url('edge')} strokeWidth={1.2} />
        <circle cx={C} cy={C} r={GRIP_OUT} fill="none" className="mf-safe-step" strokeWidth={1} />
        <circle cx={C} cy={C} r={DIAL_IN + 0.6} fill="none" stroke={url('under')} strokeWidth={1.2} />

        {/* The fixed ring. */}
        <g fill="none" strokeLinecap="round">
          <circle cx={C} cy={C} r={(OUTER + RING_IN) / 2} stroke={url('metal')} strokeWidth={OUTER - RING_IN} />
          <circle cx={C} cy={C} r={(OUTER + RING_IN) / 2} stroke={url('dim')} strokeWidth={OUTER - RING_IN} />
          <circle cx={C} cy={C} r={OUTER - 0.8} stroke={url('edge')} strokeWidth={1.2} />
          <circle cx={C} cy={C} r={RING_IN + 0.8} stroke={url('under')} strokeWidth={1.2} />
          <path d={arc(OUTER - 5, 290, 336)} stroke="#fff" strokeOpacity={0.8} strokeWidth={2.4} />
          <path d={arc(OUTER - 3, 120, 142)} stroke="#fff" strokeOpacity={0.35} strokeWidth={1.6} />
        </g>

        <path className="mf-safe-index" d={INDEX} />
        {/* Shown as it opens (openSafe.ts): the index lights up, and the ring with it. */}
        <g className="mf-safe-open">
          <circle cx={C} cy={C - DIAL_OUT + 12} r={64} fill={url('glow')} />
          <circle className="mf-safe-open-ring" cx={C} cy={C} r={(OUTER + RING_IN) / 2} fill="none" strokeWidth={2} />
          <path className="mf-safe-open-index" d={INDEX} />
        </g>
      </svg>
    </div>
  )
}
