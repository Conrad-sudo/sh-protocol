import { useEffect } from 'react'

const SETTING = 'interactive-widget=resizes-content'

/**
 * While mounted, an on-screen keyboard shrinks the page instead of covering it (Chrome on Android;
 * browsers that don't know the setting ignore it), so a composer stuck to the bottom of the screen
 * stays above the keyboard. Only the chat wants this: on other pages the keyboard should keep
 * covering the tab bar rather than push it up over the form.
 */
export function useKeyboardResizesPage() {
  useEffect(() => {
    const meta = document.querySelector<HTMLMetaElement>('meta[name="viewport"]')
    if (!meta || meta.content.includes('interactive-widget')) return
    const original = meta.content
    meta.content = `${original}, ${SETTING}`
    return () => {
      meta.content = original
    }
  }, [])
}
