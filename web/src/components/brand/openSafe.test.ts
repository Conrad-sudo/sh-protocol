import { afterEach, describe, expect, it, vi } from 'vitest'
import { openSafe } from './openSafe'

interface Call {
  target: Element
  frames: Keyframe[]
  options: KeyframeAnimationOptions
}

/**
 * A wallpaper holding the dial's two moving parts. With `finished`, both can be animated (jsdom
 * has no Element.animate) and every animation finishes when that promise does.
 */
function wallpaper(finished?: Promise<void>) {
  const root = document.createElement('div')
  root.innerHTML = '<svg class="mf-safe-rotor"></svg><svg><g class="mf-safe-open"></g></svg>'
  const calls: Call[] = []
  if (finished) {
    for (const part of root.querySelectorAll('.mf-safe-rotor, .mf-safe-open')) {
      Object.assign(part, {
        animate(frames: Keyframe[], options: KeyframeAnimationOptions) {
          calls.push({ target: part, frames, options })
          return { finished }
        },
      })
    }
  }
  return { root, calls }
}

/** The mark at the index for each of the dial's angles, as `rotate(…deg)`. */
function marksAtIndex(frames: Keyframe[]) {
  return frames.map(frame => {
    const deg = Number(/rotate\((-?[\d.]+)deg\)/.exec(String(frame.transform))![1])
    return Math.round((((-deg / 3.6) % 100) + 100) % 100)
  })
}

describe('openSafe', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('dials left a full turn to 72, then right to 18, in about 1.5s, and lights the index', async () => {
    const { root, calls } = wallpaper(Promise.resolve())
    await openSafe(root)

    const [spin, glow] = calls
    expect(spin.target).toBe(root.querySelector('.mf-safe-rotor'))
    // 72 twice: arriving, then the pause before the next turn.
    expect(marksAtIndex(spin.frames)).toEqual([0, 72, 72, 18])
    const angles = spin.frames.map(frame => Number(/(-?[\d.]+)deg/.exec(String(frame.transform))![1]))
    const turns = angles.slice(1).map((angle, i) => Math.sign(angle - angles[i])).filter(Boolean)
    // Left (anticlockwise) is negative: left, then right.
    expect(turns).toEqual([-1, 1])
    // The first turn goes once round the dial before it stops.
    expect(angles[1]).toBeLessThan(-360)
    expect(spin.options.duration).toBe(1_500)
    // And it stays at 18, rather than springing back to 0.
    expect(spin.options.fill).toBe('forwards')

    expect(glow.target).toBe(root.querySelector('.mf-safe-open'))
    expect(glow.frames).toEqual([{ opacity: 0 }, { opacity: 1 }])
    // It stays lit, and lights as the dial lands.
    expect(glow.options.fill).toBe('forwards')
    expect(Number(glow.options.delay)).toBeGreaterThan(Number(spin.options.duration) / 2)
  })

  it('resolves once the dial has opened', async () => {
    let open!: () => void
    const { root } = wallpaper(new Promise<void>(resolve => (open = resolve)))
    let opened = false
    const opening = openSafe(root).then(() => (opened = true))

    await Promise.resolve()
    expect(opened).toBe(false)
    open()
    await opening
    expect(opened).toBe(true)
  })

  it('never holds up a sign-in, even where the animation is held still (a hidden tab)', async () => {
    vi.useFakeTimers()
    const { root } = wallpaper(new Promise<void>(() => {}))
    let opened = false
    const opening = openSafe(root).then(() => (opened = true))

    await vi.advanceTimersByTimeAsync(1_000)
    expect(opened).toBe(false)
    await vi.advanceTimersByTimeAsync(5_000)
    await opening
    expect(opened).toBe(true)
  })

  it('stays still for anyone who asks for less motion', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('prefers-reduced-motion: reduce') }))
    const { root, calls } = wallpaper(new Promise<void>(() => {}))

    await openSafe(root)
    expect(calls).toEqual([])
  })

  it('resolves at once where nothing can be animated', async () => {
    await openSafe(wallpaper().root)
    await openSafe(null)
  })
})
