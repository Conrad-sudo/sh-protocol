import { useMediaQuery } from 'rsuite'

export type LayoutMode = 'desktop' | 'tablet' | 'mobile'

/** desktop ≥ 1024px (full sidebar), tablet ≥ 768px (icon rail), mobile below (bottom tabs). */
export function useLayoutMode(): LayoutMode {
  const [desktop, tablet] = useMediaQuery(['(min-width: 1024px)', '(min-width: 768px)'])
  if (desktop) return 'desktop'
  return tablet ? 'tablet' : 'mobile'
}
