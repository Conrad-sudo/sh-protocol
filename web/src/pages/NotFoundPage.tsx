import { EmptyState } from '../components/EmptyState'
import { LinkButton } from '../components/LinkButton'

export function NotFoundPage() {
  return (
    <div className="mf-fullpage">
      <title>Page not found · Mitfah</title>
      <EmptyState
        title="Page not found"
        action={
          <LinkButton to="/" appearance="primary">
            Go home
          </LinkButton>
        }
      >
        That link doesn't lead anywhere.
      </EmptyState>
    </div>
  )
}
