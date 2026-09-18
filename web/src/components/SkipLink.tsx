/**
 * The first thing a keyboard or screen-reader visitor reaches: a link past the navigation. It is
 * invisible until focused. The target is the page's `<main id="main-content" tabIndex={-1}>`.
 */
export function SkipLink() {
  return (
    <a className="mf-skip-link" href="#main-content">
      Skip to content
    </a>
  )
}
