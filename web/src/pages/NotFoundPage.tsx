import { EmptyState } from '../components/EmptyState'
import { LinkButton } from '../components/LinkButton'

export function NotFoundPage() {
  return (
    <div className="mf-fullpage">
      <title>Page not found · Mitfah</title>
      {/* The host answers every address with the app, so a missing page still arrives as a 200. */}
      <meta name="robots" content="noindex" />
      <EmptyState
        level={1}
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
