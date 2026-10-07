/*
 * Opening the wallpaper's safe dial (SafeDial.tsx) as someone signs in.
 */

/** Marks round the dial, as on a safe's, counted clockwise from 0 at the top. They carry no numbers. */
export const MARKS = 100

/*
 * The combination: left (anticlockwise) a full turn and on to mark 72, then right to 18, where it
 * opens — about 1.5s in all. The marks count clockwise, so turning left brings higher ones to the
 * index.
 */
const STEP = 360 / MARKS
/** The dial's angle with `mark` at the index, having gone once round to the left. */
const toMark = (mark: number) => -(mark * STEP + 360)
const MOVES = [
  { to: toMark(72), ms: 880, easing: 'cubic-bezier(0.55, 0, 0.25, 1)' },
  // Onto the last number, with a little bounce as it lands.
  { to: toMark(18), ms: 520, easing: 'cubic-bezier(0.45, 0, 0.35, 1.14)' },
]
/** The hand's pause between the turns. */
const PAUSE = 100

const SPIN_MS = MOVES.reduce((sum, move) => sum + move.ms, 0) + PAUSE * (MOVES.length - 1)
const SPIN: Keyframe[] = (() => {
  const frames: Keyframe[] = []
  let t = 0
  let from = 0
  MOVES.forEach((move, i) => {
    frames.push({ offset: t / SPIN_MS, transform: `rotate(${from}deg)`, easing: move.easing })
    t += move.ms
    frames.push({ offset: t / SPIN_MS, transform: `rotate(${move.to}deg)` })
    if (i < MOVES.length - 1) t += PAUSE
    from = move.to
  })
  return frames
})()

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

/**
 * Dials the combination on the wallpaper's safe and opens it; resolves once it's open. The dial
 * stays where it stopped, at 18. Resolves at once for anyone who asks for less motion, or where
 * nothing can be animated (jsdom).
 */
export async function openSafe(wallpaper: HTMLElement | null): Promise<void> {
  const rotor = wallpaper?.querySelector('.mf-safe-rotor')
  const open = wallpaper?.querySelector('.mf-safe-open')
  if (!rotor || !open || typeof rotor.animate !== 'function') return
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return

  const spin = rotor.animate(SPIN, { duration: SPIN_MS, fill: 'forwards' })
  const glow = open.animate([{ opacity: 0 }, { opacity: 1 }], {
    duration: 260,
    delay: SPIN_MS - 140,
    easing: 'ease-out',
    fill: 'forwards',
  })
  // A hidden tab holds animations still: never let that hold up the sign-in.
  await Promise.race([Promise.allSettled([spin.finished, glow.finished]), sleep(SPIN_MS + 600)])
}
