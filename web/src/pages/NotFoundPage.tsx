import { Link } from 'react-router'
import { Button } from 'rsuite'
import { EmptyState } from '../components/EmptyState'

export function NotFoundPage() {
  return (
    <div className="mf-fullpage">
      <title>Page not found · Mitfah</title>
      <EmptyState
        title="Page not found"
        action={
          <Button as={Link} to="/" appearance="primary">
            Go home
          </Button>
        }
      >
        That link doesn't lead anywhere.
      </EmptyState>
    </div>
  )
}
