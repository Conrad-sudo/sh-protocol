import { Button, Message } from 'rsuite'
import { errorText } from '../lib/tx'

interface QueryErrorProps {
  /** What failed, in the person's terms: "your contacts", "the conversation". */
  what: string
  error?: unknown
  onRetry: () => void
  /** Spacing for a note that follows other content, e.g. `mf-settings-note`. */
  className?: string
}

/** A load that failed, said plainly, with a way to try it again. */
export function QueryError({ what, error, onRetry, className }: QueryErrorProps) {
  return (
    <Message type="error" showIcon className={className}>
      Couldn't load {what}
      {error ? `: ${errorText(error)}` : '.'}{' '}
      <Button appearance="link" size="sm" onClick={onRetry}>
        Try again
      </Button>
    </Message>
  )
}
