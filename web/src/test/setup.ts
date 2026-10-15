import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/react'

// The first test in a file also loads the app's lazy pages (routes.tsx). On a cold transform -- a
// fresh checkout, CI -- that takes longer than findBy*'s 1 s default, and a test that would pass
// failed for time alone (AssistantPage). The vitest timeout is raised for the same reason.
configure({ asyncUtilTimeout: 5_000 })

/*
 * jsdom has no matchMedia. This stub answers the two kinds of query the app asks:
 * - `(prefers-color-scheme: dark)` → `window.prefersDark` (tests set it)
 * - `(min-width: Npx)` / `(max-width: Npx)` → compared with `window.innerWidth` (tests set it)
 * - `(pointer: coarse)` → `window.coarsePointer` (tests set it for a touch screen)
 */
function evaluate(query: string): boolean {
  const width = window.innerWidth
  return query.split(/\s+and\s+/).every(part => {
    if (part.includes('prefers-color-scheme: dark')) {
      return (window as { prefersDark?: boolean }).prefersDark === true
    }
    if (part.includes('pointer: coarse')) {
      return (window as { coarsePointer?: boolean }).coarsePointer === true
    }
    const min = /min-width:\s*([\d.]+)px/.exec(part)
    if (min) return width >= Number(min[1])
    const max = /max-width:\s*([\d.]+)px/.exec(part)
    if (max) return width <= Number(max[1])
    return false
  })
}

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    get matches() {
      return evaluate(query)
    },
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }),
})

// jsdom has no scrolling either; the chat scrolls the window to its newest message, and Settings to
// the card a link names.
window.scrollTo = () => {}
Element.prototype.scrollIntoView = () => {}
