// Draws the site wallpaper into public/brand/wallpaper-light.svg and wallpaper-dark.svg. Run with
// `npm run wallpaper`.
//
// It is the logo's handle, blown up and turned inside out: the ring, with the crescent that breaks
// it at the upper right and a gap at the lower left where the key's shaft crosses it, and the
// circuit that fills it in the logo running outward instead, off the ring and on to the edges.
// The middle is left empty, because that is where the page's text sits.
//
// The page holds it still behind the content (a fixed layer, see .mf-wallpaper in app.css), so the
// canvas is large enough to reach every edge of a big screen from wherever the ring is placed.
//
// Everything that looks 3D is drawn, not filtered: the metal is gradients, the bevels are offset
// highlight and shadow strokes, and the night glow is wide, faint copies of the traces under a
// bright core. A blur or lighting filter over a screen-sized fixed layer would be slow to paint,
// and a drawn edge stays sharp at any pixel density. A seeded random number generator lays out
// the traces, so every run writes the same files.
import { writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

/** Canvas side. The ring sits in the middle, so CSS can place it by its centre. */
const SIZE = 2880
const C = SIZE / 2
/** The ring: its radius and the width of the metal band. */
const RING = 430
const BAND = 11
/** Where the first traces leave from: a thin second ring just outside the band. */
const BUS = 468
/** Trace grid pitch. Parallel traces sit one pitch apart, as on a circuit board. */
const PITCH = 14
/** Traces stop this far from the canvas edge. */
const MARGIN = 70
/** The traces are at full strength out to here, then thin out gently towards the edges. */
const FULL = BUS + 180
const FADE_END = 1500
/** Arc left clear at the lower left, where the key's shaft leaves the ring in the logo (y points down). */
const KEY_GAP = { from: 118, to: 152 }
const SEED = 7

// mulberry32: small, fast and seedable, which Math.random is not.
function random(seed: number) {
  let a = seed
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const rand = random(SEED)
const between = (lo: number, hi: number) => lo + rand() * (hi - lo)
const int = (lo: number, hi: number) => Math.floor(between(lo, hi + 1))

const round = (n: number) => Math.round(n * 10) / 10
const deg = (d: number) => (d * Math.PI) / 180

// The eight directions a trace may run in, 45° apart, starting east and turning clockwise.
const DIRS = [
  [1, 0],
  [1, 1],
  [0, 1],
  [-1, 1],
  [-1, 0],
  [-1, -1],
  [0, -1],
  [1, -1],
] as const

type Cell = [number, number]
const key = ([i, j]: Cell) => `${i},${j}`
const at = ([i, j]: Cell): [number, number] => [C + i * PITCH, C + j * PITCH]
const radius = (cell: Cell) => Math.hypot(cell[0] * PITCH, cell[1] * PITCH)
const LIMIT = Math.floor((C - MARGIN) / PITCH)
const inside = ([i, j]: Cell) => Math.abs(i) <= LIMIT && Math.abs(j) <= LIMIT
/** The octilinear direction nearest to an angle in degrees. */
const nearest = (angle: number) => ((Math.round(angle / 45) % 8) + 8) % 8

type Pad = 'dot' | 'big' | 'hollow' | 'none'
interface Trace {
  /** Where the line begins: on the bus ring for the first traces, else the first cell. */
  start: [number, number]
  cells: Cell[]
  /** `via` where the trace surfaces from a hole in the board rather than from the ring. */
  via: boolean
  /** `none` where it ends by joining another trace. */
  pad: Pad
}

const taken = new Set<string>()
// A diagonal step crosses the other diagonal of the same square; keep both from being used.
const diagonals = new Set<string>()

/** A bundle's shared route, so its traces stay parallel: step → turn (±1 is 45°). */
function route(): Map<number, number> {
  const turns = new Map<number, number>()
  const turn = rand() < 0.5 ? 1 : -1
  let at = int(2, 9)
  turns.set(at, turn)
  if (rand() < 0.55) {
    at += int(3, 10)
    turns.set(at, -turn)
  }
  // Longer runs jog once more further out, as board traces route round each other.
  if (rand() < 0.7) {
    at += int(10, 26)
    const jog = rand() < 0.5 ? 1 : -1
    turns.set(at, jog)
    if (rand() < 0.6) turns.set(at + int(3, 9), -jog)
  }
  return turns
}

/**
 * Grows one trace from `first` along `dir`, turning where the bundle's route says, until it has
 * `length` cells, reaches the edge, or meets another trace — which it then joins.
 */
function grow(first: Cell, dir: number, turns: Map<number, number>, length: number, outward: boolean) {
  const cells: Cell[] = [first]
  taken.add(key(first))
  let joined: Cell | undefined
  let cell = first
  let d = dir
  for (let step = 1; step < length; step++) {
    const turn = turns.get(step)
    if (turn) d = (d + turn + 8) % 8
    const [sx, sy] = DIRS[d]
    const next: Cell = [cell[0] + sx, cell[1] + sy]
    const diagonal = sx !== 0 && sy !== 0
    const cross = diagonal ? `${Math.min(cell[0], next[0])},${Math.min(cell[1], next[1])}` : ''
    // Running into another trace: join it, as a branch does on a board, rather than stopping a
    // step short with a pad beside it.
    if (taken.has(key(next))) {
      if (!diagonal || !diagonals.has(cross)) joined = next
      break
    }
    if (diagonal && diagonals.has(cross)) break
    if (!inside(next)) break
    // Nothing heads back in towards the ring.
    if (radius(next) < BUS + 40 && radius(next) <= radius(cell)) break
    if (outward && radius(next) < radius(cell) - 0.5) break
    taken.add(key(next))
    if (diagonal) diagonals.add(cross)
    cells.push(next)
    cell = next
  }
  return { cells, joined }
}

function endPad(): Pad {
  const r = rand()
  return r < 0.13 ? 'big' : r < 0.26 ? 'hollow' : 'dot'
}

/** Whether every cell within `reach` steps of `cell` is free. */
function clear(cell: Cell, reach: number) {
  for (let i = -reach; i <= reach; i++) {
    for (let j = -reach; j <= reach; j++) if (taken.has(key([cell[0] + i, cell[1] + j]))) return false
  }
  return true
}

/** Bundles of traces leaving the bus ring, all the way round except at the key's gap. */
function fromRing(traces: Trace[]) {
  let angle = between(0, 10)
  while (angle < 360) {
    const size = int(3, 7)
    const dir = nearest(angle)
    const [dx, dy] = DIRS[dir]
    // Across the bundle: a right angle to its direction, one grid step (diagonal bundles: one diagonal step).
    const across: Cell = [-dy, dx]
    const width = size * PITCH * Math.hypot(...across)
    const span = (width / BUS) * (180 / Math.PI)
    const inGap = angle + span > KEY_GAP.from && angle < KEY_GAP.to

    if (!inGap) {
      const mid = angle + span / 2
      const base: Cell = [
        Math.round((BUS * Math.cos(deg(mid))) / PITCH),
        Math.round((BUS * Math.sin(deg(mid))) / PITCH),
      ]
      const turns = route()
      const reach = int(24, 100)

      for (let m = 0; m < size; m++) {
        const offset = m - Math.floor(size / 2)
        let cell: Cell = [base[0] + offset * across[0], base[1] + offset * across[1]]
        // Step out until clear of the bus ring.
        while (radius(cell) < BUS + 2) cell = [cell[0] + dx, cell[1] + dy]
        if (taken.has(key(cell))) continue

        // Back along the trace's first direction to where it meets the bus ring.
        const [px, py] = at(cell)
        const ux = dx / Math.hypot(dx, dy)
        const uy = dy / Math.hypot(dx, dy)
        const b = (px - C) * ux + (py - C) * uy
        const c = (px - C) ** 2 + (py - C) ** 2 - BUS ** 2
        const t = b - Math.sqrt(b * b - c)

        const { cells, joined } = grow(cell, dir, turns, reach + int(-6, 6), true)
        // A trace that only just left the ring reads as a stub; drop it.
        if (cells.length < 5) continue
        traces.push({
          start: [px - t * ux, py - t * uy],
          cells: joined ? [...cells, joined] : cells,
          via: false,
          pad: joined ? 'none' : endPad(),
        })
      }
    }
    angle += span + between(2, 7)
  }
}

/**
 * Further out the ring's bundles spread apart, so the gaps are filled with bundles that surface
 * through vias, mostly heading away from the ring like the rest and now and then across it.
 */
function fromVias(traces: Trace[]) {
  for (let attempt = 0; attempt < 9000; attempt++) {
    const seed: Cell = [int(-LIMIT, LIMIT), int(-LIMIT, LIMIT)]
    const r = radius(seed)
    if (r < BUS + 150 || !clear(seed, 3)) continue

    const outward = rand() < 0.78
    const angle = (Math.atan2(seed[1], seed[0]) * 180) / Math.PI
    const dir = (nearest(angle) + (outward ? 0 : rand() < 0.5 ? 2 : -2) + 8) % 8
    const [dx, dy] = DIRS[dir]
    const across: Cell = [-dy, dx]
    const size = int(2, 5)
    const turns = route()
    const reach = int(8, 44)

    for (let m = 0; m < size; m++) {
      const cell: Cell = [seed[0] + m * across[0], seed[1] + m * across[1]]
      if (!inside(cell) || !clear(cell, 1)) continue
      const { cells, joined } = grow(cell, dir, turns, reach + int(-4, 4), outward)
      if (cells.length < 4) continue
      traces.push({
        start: at(cells[0]),
        cells: joined ? [...cells, joined] : cells,
        via: true,
        pad: joined ? 'none' : endPad(),
      })
    }
  }
}

/** A trace as path data, keeping only its corners. */
function pathOf({ start, cells }: Trace) {
  const points = cells.map(at)
  const corners = points.filter((p, i) => {
    if (i === 0 || i === points.length - 1) return true
    const [a, b] = [points[i - 1], points[i + 1]]
    return (p[0] - a[0]) * (b[1] - p[1]) !== (p[1] - a[1]) * (b[0] - p[0])
  })
  const [sx, sy] = start
  const first = corners[0][0] === sx && corners[0][1] === sy ? corners.slice(1) : corners
  return `M${round(sx)} ${round(sy)}L${first.map(([x, y]) => `${x} ${y}`).join(' ')}`
}

/** The crescent that breaks the ring at the upper right in the logo: thick in the middle, fine at the ends. */
function crescent(from: number, to: number, r: number, thickness: number) {
  const p = (a: number, rr: number) => `${round(C + rr * Math.cos(deg(a)))} ${round(C + rr * Math.sin(deg(a)))}`
  const half = r * Math.sin(deg((to - from) / 2))
  const sagitta = r - Math.sqrt(r * r - half * half)
  const inner = sagitta - thickness
  const innerR = (inner * inner + half * half) / (2 * inner)
  return `M${p(from, r)} A${r} ${r} 0 0 1 ${p(to, r)} A${round(innerR)} ${round(innerR)} 0 0 0 ${p(from, r)}Z`
}

/** An arc of a circle round the centre, `from` → `to` degrees clockwise. */
function arc(r: number, from: number, to: number) {
  const p = (a: number) => `${round(C + r * Math.cos(deg(a)))} ${round(C + r * Math.sin(deg(a)))}`
  const large = to - from > 180 ? 1 : 0
  return `M${p(from)} A${r} ${r} 0 ${large} 1 ${p(to)}`
}

interface Theme {
  /** Stops of the ring's metal, light and dark bands in turn, as [offset, colour]. */
  metal: [number, string][]
  ringOpacity: number
  /** Soft light round the ring. */
  halo: [string, number]
  /** The shadow the ring casts on the page, below it (light only). */
  shadow?: [string, number]
  /** Thin line along the band's inner edge. */
  innerRim: [string, number]
  bus: [string, number]
  traces: string
  pads: (traces: Trace[]) => string
  defs: string
}

const LIGHT: Theme = {
  metal: [
    [0, '#5d86b4'],
    [0.16, '#0b2f59'],
    [0.3, '#9dbfe3'],
    [0.42, '#0a2a4c'],
    [0.56, '#4f78a5'],
    [0.7, '#061e3a'],
    [0.84, '#b3cdea'],
    [1, '#0b2f59'],
  ],
  ringOpacity: 0.9,
  halo: ['#0A8A5A', 0.04],
  shadow: ['#062A4E', 0.2],
  innerRim: ['#ffffff', 0.5],
  bus: ['#062A4E', 0.22],
  defs: `<linearGradient id="steel" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="460" y2="320" spreadMethod="reflect">
<stop offset="0" stop-color="#8395ab"/><stop offset="0.5" stop-color="#c8d2de"/><stop offset="1" stop-color="#6c7e95"/>
</linearGradient>
<radialGradient id="enamel" cx="0.36" cy="0.32" r="0.75">
<stop offset="0" stop-color="#9ff0cc"/><stop offset="0.45" stop-color="#0a8a5a"/><stop offset="1" stop-color="#044a31"/>
</radialGradient>`,
  // Raised steel: a shadow cast down and right, the metal, and a highlight along the upper-left edge.
  traces: `<use href="#traces" transform="translate(1.4 2.2)" stroke="#062A4E" stroke-opacity="0.13" stroke-width="2.6"/>
<use href="#traces" stroke="url(#steel)" stroke-width="2.1"/>
<use href="#traces" transform="translate(-0.55 -0.65)" stroke="#ffffff" stroke-opacity="0.85" stroke-width="0.7"/>`,
  pads: traces =>
    traces
      .flatMap(t => {
        const out: string[] = []
        if (t.via) out.push(via(at(t.cells[0]), 'url(#steel)', '#ffffff'))
        const [x, y] = at(t.cells[t.cells.length - 1])
        if (t.pad === 'big')
          out.push(
            `<circle cx="${x + 1.2}" cy="${y + 2}" r="7" fill="#062A4E" fill-opacity="0.14"/>` +
              `<circle cx="${x}" cy="${y}" r="6.2" fill="url(#enamel)" stroke="url(#steel)" stroke-width="1.8"/>` +
              `<circle cx="${x - 1.8}" cy="${y - 2}" r="1.3" fill="#ffffff" fill-opacity="0.9"/>`,
          )
        if (t.pad === 'dot')
          out.push(
            `<circle cx="${x + 0.8}" cy="${y + 1.3}" r="3.4" fill="#062A4E" fill-opacity="0.14"/>` +
              `<circle cx="${x}" cy="${y}" r="3.3" fill="url(#enamel)"/>`,
          )
        if (t.pad === 'hollow') out.push(via([x, y], 'url(#steel)', '#ffffff'))
        return out
      })
      .join(''),
}

const EMERALD = '#10B981'
const EMERALD_BRIGHT = '#34D399'

const DARK: Theme = {
  metal: [
    [0, '#dce8f5'],
    [0.14, '#56789f'],
    [0.28, '#0e2136'],
    [0.4, '#a9c6e6'],
    [0.55, '#213a58'],
    [0.7, '#eef4fb'],
    [0.85, '#35557a'],
    [1, '#c9dcf0'],
  ],
  ringOpacity: 0.8,
  halo: [EMERALD, 0.15],
  innerRim: [EMERALD_BRIGHT, 0.45],
  bus: ['#7FB0E6', 0.2],
  defs: `<radialGradient id="glow">
<stop offset="0" stop-color="${EMERALD_BRIGHT}" stop-opacity="0.55"/><stop offset="0.4" stop-color="${EMERALD}" stop-opacity="0.18"/><stop offset="1" stop-color="${EMERALD}" stop-opacity="0"/>
</radialGradient>`,
  // Neon: two wide, faint copies make the glow, then the emerald line and a pale hot core.
  traces: `<use href="#traces" stroke="${EMERALD}" stroke-opacity="0.07" stroke-width="12"/>
<use href="#traces" stroke="${EMERALD}" stroke-opacity="0.16" stroke-width="5"/>
<use href="#traces" stroke="${EMERALD_BRIGHT}" stroke-opacity="0.72" stroke-width="1.8"/>
<use href="#traces" stroke="#D1FAE5" stroke-opacity="0.35" stroke-width="0.6"/>`,
  pads: traces =>
    traces
      .flatMap(t => {
        const out: string[] = []
        if (t.via) out.push(via(at(t.cells[0]), EMERALD_BRIGHT, '#D1FAE5'))
        const [x, y] = at(t.cells[t.cells.length - 1])
        if (t.pad === 'big')
          out.push(
            `<circle cx="${x}" cy="${y}" r="18" fill="url(#glow)"/>` +
              `<circle cx="${x}" cy="${y}" r="5.4" fill="${EMERALD_BRIGHT}"/>` +
              `<circle cx="${x}" cy="${y}" r="2.2" fill="#ECFDF5"/>`,
          )
        if (t.pad === 'dot')
          out.push(
            `<circle cx="${x}" cy="${y}" r="9" fill="url(#glow)"/>` +
              `<circle cx="${x}" cy="${y}" r="2.9" fill="${EMERALD_BRIGHT}"/>`,
          )
        if (t.pad === 'hollow') out.push(via([x, y], EMERALD_BRIGHT, '#D1FAE5'))
        return out
      })
      .join(''),
}

/** A via: a plated ring with a highlight on its upper-left side. */
function via([x, y]: [number, number], metal: string, light: string) {
  return (
    `<circle cx="${x}" cy="${y}" r="3.9" fill="none" stroke="${metal}" stroke-width="1.7"/>` +
    `<path d="M${x - 3.9} ${y} A3.9 3.9 0 0 1 ${x} ${y - 3.9}" fill="none" stroke="${light}" stroke-opacity="0.7" stroke-width="0.7"/>`
  )
}

function svg(traces: Trace[], theme: Theme) {
  // The ring, open at the lower left where the key's shaft crosses it in the logo.
  const ring = arc(RING, KEY_GAP.to - 6, KEY_GAP.from + 6 + 360)
  const stops = (list: [number, string][]) =>
    list.map(([o, c]) => `<stop offset="${o}" stop-color="${c}"/>`).join('')
  const lo = C - RING - 40
  const hi = C + RING + 40

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}" width="${SIZE}" height="${SIZE}">
<defs>
<path id="traces" d="${traces.map(pathOf).join('')}"/>
<radialGradient id="halo" cx="${C}" cy="${C}" r="${RING * 2.2}" gradientUnits="userSpaceOnUse">
<stop offset="0.3" stop-color="${theme.halo[0]}" stop-opacity="0"/>
<stop offset="${round(RING / (RING * 2.2))}" stop-color="${theme.halo[0]}" stop-opacity="${theme.halo[1]}"/>
<stop offset="1" stop-color="${theme.halo[0]}" stop-opacity="0"/>
</radialGradient>
<radialGradient id="fadeGrad" cx="${C}" cy="${C}" r="${FADE_END}" gradientUnits="userSpaceOnUse">
<stop offset="${round(FULL / FADE_END)}" stop-color="#fff"/>
<stop offset="0.8" stop-color="#fff" stop-opacity="0.6"/>
<stop offset="1" stop-color="#fff" stop-opacity="0"/>
</radialGradient>
<mask id="fade" maskUnits="userSpaceOnUse" x="0" y="0" width="${SIZE}" height="${SIZE}">
<rect width="${SIZE}" height="${SIZE}" fill="url(#fadeGrad)"/>
</mask>
<linearGradient id="metal" gradientUnits="userSpaceOnUse" x1="${lo}" y1="${lo}" x2="${hi}" y2="${hi}">${stops(theme.metal)}</linearGradient>
<linearGradient id="rimLight" gradientUnits="userSpaceOnUse" x1="${lo}" y1="${lo}" x2="${hi}" y2="${hi}">
<stop offset="0" stop-color="#fff" stop-opacity="0.95"/><stop offset="0.45" stop-color="#fff" stop-opacity="0"/>
</linearGradient>
<linearGradient id="rimShade" gradientUnits="userSpaceOnUse" x1="${lo}" y1="${lo}" x2="${hi}" y2="${hi}">
<stop offset="0.55" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity="0.45"/>
</linearGradient>
<linearGradient id="innerRim" gradientUnits="userSpaceOnUse" x1="${lo}" y1="${lo}" x2="${hi}" y2="${hi}">
<stop offset="0.4" stop-color="${theme.innerRim[0]}" stop-opacity="0"/><stop offset="1" stop-color="${theme.innerRim[0]}" stop-opacity="${theme.innerRim[1]}"/>
</linearGradient>
${
  theme.shadow
    ? `<radialGradient id="shadow" cx="${C}" cy="${C + 16}" r="${RING + 40}" gradientUnits="userSpaceOnUse">
<stop offset="${round((RING - 22) / (RING + 40))}" stop-color="${theme.shadow[0]}" stop-opacity="0"/>
<stop offset="${round(RING / (RING + 40))}" stop-color="${theme.shadow[0]}" stop-opacity="${theme.shadow[1]}"/>
<stop offset="1" stop-color="${theme.shadow[0]}" stop-opacity="0"/>
</radialGradient>`
    : ''
}
${theme.defs}
</defs>
<rect width="${SIZE}" height="${SIZE}" fill="url(#halo)"/>
${theme.shadow ? `<circle cx="${C}" cy="${C + 16}" r="${RING + 40}" fill="url(#shadow)"/>` : ''}
<g mask="url(#fade)" fill="none" stroke-linejoin="round" stroke-linecap="round">
${theme.traces}
${theme.pads(traces)}
</g>
<circle cx="${C}" cy="${C}" r="${BUS}" fill="none" stroke="${theme.bus[0]}" stroke-opacity="${theme.bus[1]}" stroke-width="1.4"/>
<g fill="none" stroke-linecap="round" opacity="${theme.ringOpacity}">
<path d="${ring}" stroke="url(#metal)" stroke-width="${BAND}"/>
<path d="${ring}" stroke="url(#rimShade)" stroke-width="${BAND}"/>
<path d="${arc(RING + BAND / 2 - 0.8, KEY_GAP.to - 6, KEY_GAP.from + 6 + 360)}" stroke="url(#rimLight)" stroke-width="1.2"/>
<path d="${arc(RING - BAND / 2 + 0.8, KEY_GAP.to - 6, KEY_GAP.from + 6 + 360)}" stroke="url(#innerRim)" stroke-width="1.2"/>
<path d="${arc(RING - 1, 200, 246)}" stroke="#fff" stroke-opacity="0.8" stroke-width="2.4"/>
<path d="${arc(RING + 2, 30, 52)}" stroke="#fff" stroke-opacity="0.35" stroke-width="1.6"/>
</g>
<path d="${crescent(-72, -14, RING + 24, 8)}" fill="url(#metal)" opacity="${theme.ringOpacity}"/>
<path d="${arc(RING + 24, -66, -30)}" fill="none" stroke="#fff" stroke-opacity="0.55" stroke-width="1" stroke-linecap="round"/>
</svg>
`
}

const traces: Trace[] = []
fromRing(traces)
fromVias(traces)
const out = (name: string) => fileURLToPath(new URL(`../../public/brand/${name}`, import.meta.url))
await writeFile(out('wallpaper-light.svg'), svg(traces, LIGHT))
await writeFile(out('wallpaper-dark.svg'), svg(traces, DARK))
console.log(`Wrote wallpaper-light.svg and wallpaper-dark.svg (${traces.length} traces).`)
