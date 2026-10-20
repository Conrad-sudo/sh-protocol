import { useEffect, type MouseEvent } from 'react'
import { Link } from 'react-router'
import { Button, Message, Text } from 'rsuite'
import { mayHaveRun, useQuoteAnswer, useQuoteOffer } from '../../hooks/useChat'
import { useNow } from '../../hooks/useNow'
import { errorText } from '../../lib/tx'
import { chainName } from '../../wallet/chains'

interface QuoteCardProps {
  chainId: number
  /** A message is being answered: the server would refuse a tap until it is. */
  busy: boolean
  /** Called once a quote's buttons are on the page, to bring them into view. */
  onShow?: () => void
}

/**
 * The transaction the assistant quoted, with Confirm and Cancel. Written from the server's stored
 * quote, never from the assistant's reply, and shown as plain text: Confirm sends exactly this.
 */
export function QuoteCard({ chainId, busy, onShow }: QuoteCardProps) {
  const { data: quote } = useQuoteOffer(chainId)
  const answer = useQuoteAnswer(chainId)
  const now = useNow(5_000)

  const secondsLeft = quote ? quote.expires_at - now / 1000 : 0
  const expired = secondsLeft <= 0 && !answer.isPending
  const shown = quote && !expired ? quote.quote_id : undefined
  useEffect(() => {
    if (shown) onShow?.()
  }, [shown, onShow])

  const failure = answer.isError && <Failure error={answer.error} onClose={answer.reset} />
  if (!quote) return failure || null
  if (expired) {
    return (
      <>
        {failure}
        <p className="mf-quote-expired">
          <Text as="span" muted>
            The quote for “{quote.action}” has expired. Ask Mitfah again for a fresh one.
          </Text>
        </p>
      </>
    )
  }
  const minutes = Math.max(1, Math.ceil(secondsLeft / 60))
  const confirming = answer.isPending && answer.variables.confirm
  const tap = (confirm: boolean) => answer.mutate({ quote, confirm })
  // A press doesn't take the focus from the message box: on a phone that brings the tab bar back,
  // which moves the composer over the button before the tap ends (as the Send button avoids).
  const keepFocus = (event: MouseEvent) => event.preventDefault()

  return (
    <>
      {failure}
      <section className="mf-quote" aria-label="Transaction waiting for your answer">
        <p className="mf-quote-title">Confirm on {chainName(quote.chain_id)}?</p>
        <p className="mf-quote-action">{quote.action}</p>
        <p className="mf-quote-meta">
          Fees: {quote.fees} · good for {minutes} minute{minutes === 1 ? '' : 's'}
        </p>
        {confirming ? (
          <p className="mf-quote-sending" role="status">
            Sending…
          </p>
        ) : (
          <div className="mf-quote-actions">
            <Button
              appearance="primary"
              size="sm"
              disabled={busy || answer.isPending}
              onMouseDown={keepFocus}
              onClick={() => tap(true)}
            >
              Confirm
            </Button>
            <Button
              appearance="subtle"
              size="sm"
              disabled={busy || answer.isPending}
              loading={answer.isPending}
              onMouseDown={keepFocus}
              onClick={() => tap(false)}
            >
              Cancel
            </Button>
          </div>
        )}
      </section>
    </>
  )
}

function Failure({ error, onClose }: { error: unknown; onClose: () => void }) {
  if (mayHaveRun(error)) {
    // The payment may have gone out, so tapping again (or asking again) could repeat it.
    return (
      <Message type="warning" showIcon closable onClose={onClose} className="mf-chat-failure">
        <strong>No answer arrived.</strong> {errorText(error)} The transaction may still have been sent, so check{' '}
        <Link to="/history">History</Link> before you try again.
      </Message>
    )
  }
  return (
    <Message type="error" showIcon closable onClose={onClose} className="mf-chat-failure">
      {errorText(error)}
    </Message>
  )
}
