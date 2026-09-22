/**
 * The site wallpaper (scripts/wallpaper/render.ts): the logo's ring with its circuit running out to
 * the screen's edges, held still behind the page so the content scrolls over it. `place` sets where
 * the ring sits: round the landing page's headline, round the sign-in card, or in the dashboard's
 * content area.
 */
export function Wallpaper({ place }: { place: 'hero' | 'center' | 'dashboard' }) {
  return <div className="mf-wallpaper" data-place={place} />
}
