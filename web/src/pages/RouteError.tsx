import { isRouteErrorResponse, useRouteError } from 'react-router'
import { Button } from 'rsuite'
import { EmptyState } from '../components/EmptyState'

/** Shown when a page throws while rendering, instead of a blank screen. */
export function RouteError() {
  const error = useRouteError()
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : 'An unexpected error stopped this page from loading.'

  return (
    <div className="mf-fullpage">
      <title>Something went wrong · Mitfah</title>
      <meta name="robots" content="noindex" />
      <EmptyState
        level={1}
        title="Something went wrong"
        action={
          <Button appearance="primary" onClick={() => window.location.reload()}>
            Reload
          </Button>
        }
      >
        {detail}
      </EmptyState>
    </div>
  )
}
