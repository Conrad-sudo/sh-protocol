import type { Ref } from 'react'
import { SafeDial } from './SafeDial'

/**
 * The site wallpaper: a safe's dial (SafeDial) with a circuit running from it out to the screen's
 * edges (scripts/wallpaper/render.ts), held still behind the page so the content scrolls over it.
 * `place` sets where the dial sits: round the landing page's headline, round the sign-in card, or
 * in the signed-in app's content area. `ref` is for opening the dial (openSafe).
 */
export function Wallpaper({ place, ref }: { place: 'hero' | 'center' | 'app'; ref?: Ref<HTMLDivElement> }) {
  return (
    <div className="mf-wallpaper" data-place={place} ref={ref} aria-hidden>
      <SafeDial />
    </div>
  )
}
