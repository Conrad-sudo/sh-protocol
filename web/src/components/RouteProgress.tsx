import { useNavigation } from 'react-router'

/**
 * A bar across the top while the next page is being fetched. Pages are loaded on demand
 * (routes.tsx), so on a slow connection a click would otherwise look like nothing happened.
 */
export function RouteProgress() {
  if (useNavigation().state === 'idle') return null
  return (
    <>
      <div className="mf-route-progress" aria-hidden="true" />
      <span className="mf-visually-hidden" role="status">
        Loading page…
      </span>
    </>
  )
}
