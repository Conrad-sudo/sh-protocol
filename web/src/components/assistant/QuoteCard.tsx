import { useEffect, useState, type MouseEvent } from 'react'
import { Link } from 'react-router'
import { useQueryClient } from '@tanstack/react-query'
import { Button, Message, Text } from 'rsuite'
import { ApiError } from '../../api/client'
import {
  freshSignStart,
  mayHaveRun,
  quoteOfferKey,
  useQuoteAnswer,
  useQuoteOffer,
  useSignStart,
} from '../../hooks/useChat'
import { useNow } from '../../hooks/useNow'
import { isPasskeyError, signWithPasskey } from '../../lib/passkey'
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
 * quote, never from the assistant's reply, and shown as plain text: Confirm sends exactly this, once
 * a passkey on this device approves it.
 *
 * The confirmation is started as soon as the card shows, and again while it waits (fresh fees), so a
 * tap can open the passkey prompt straight away: Safari opens it only from inside the tap.
 */
export function QuoteCard({ chainId, busy, onShow }: QuoteCardProps) {
  const queryClient = useQueryClient()
  const { data: quote } = useQuoteOffer(chainId)
  const answer = useQuoteAnswer(chainId)
  const now = useNow(5_000)
  // The passkey has signed and the transaction is going out.
  const [approved, setApproved] = useState(false)

  const secondsLeft = quote ? quote.expires_at - now / 1000 : 0
  const expired = secondsLeft <= 0 && !answer.isPending
  const live = quote && !expired ? quote : null
  const start = useSignStart(live, answer.isPending)
  const shown = live?.quote_id
  useEffect(() => {
    if (shown) onShow?.()
  }, [shown, onShow])

  // The server says the quote is gone (used, or run out): read the card again, which drops it.
  const gone = start.error instanceof ApiError && start.error.status === 404
  useEffect(() => {
    if (gone) void queryClient.invalidateQueries({ queryKey: quoteOfferKey(chainId) })
  }, [gone, chainId, queryClient])

  const failure = answer.isError && <Failure error={answer.error} onClose={answer.reset} />
  if (!quote) return failure || null
  if (expired || gone) {
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
  // No passkey the account has is trusted by this wallet: there is nothing a tap could sign with.
  const noPasskey = start.error instanceof ApiError && start.error.status === 409

  const confirm = () => {
    setApproved(false)
    const ready = freshSignStart(start)
    answer.mutate({
      quote,
      confirm: true,
      start: ready,
      // Opened here, with nothing awaited first: Safari shows the passkey prompt only for the tap's own call.
      assertion: ready ? signWithPasskey(ready) : null,
      onApproved: () => setApproved(true),
    })
  }
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
        {noPasskey && (
          <p className="mf-quote-problem">
            No device you added can approve payments from this wallet yet. <Link to="/controls">Add this device</Link>{' '}
            in Controls, then tap Confirm.
          </p>
        )}
        {start.isError && !noPasskey && (
          <p className="mf-quote-problem">
            Couldn't get this ready to approve: {errorText(start.error)}{' '}
            <Button appearance="link" size="sm" onMouseDown={keepFocus} onClick={() => void start.refetch()}>
              Try again
            </Button>
          </p>
        )}
        {confirming ? (
          <p className="mf-quote-sending" role="status">
            {approved ? 'Sending…' : 'Approve with your passkey…'}
          </p>
        ) : (
          <div className="mf-quote-actions">
            <Button
              appearance="primary"
              size="sm"
              disabled={busy || answer.isPending || start.isPending || noPasskey}
              onMouseDown={keepFocus}
              onClick={confirm}
            >
              Confirm
            </Button>
            <Button
              appearance="subtle"
              size="sm"
              disabled={busy || answer.isPending}
              loading={answer.isPending}
              onMouseDown={keepFocus}
              onClick={() => answer.mutate({ quote, confirm: false })}
            >
              Cancel
            </Button>
          </div>
        )}
      </section>
    </>
  )
}

/** What a refused Confirm means, in plain words. Nothing was sent in any of these. */
function refusal(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 400:
        return "Your passkey's approval wasn't accepted, so nothing was sent. Try again with a device you added in Controls."
      case 404:
        return 'This quote has expired, so nothing was sent. Ask Mitfah again for a fresh one.'
      case 409:
        return 'That confirmation was replaced or has run out, so nothing was sent. Tap Confirm to try again.'
      case 422:
        return `Nothing was sent: it would fail on the network. ${errorText(error)}`
    }
  }
  return errorText(error)
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
  // A closed passkey prompt is not an error: the card is still there to tap again.
  if (isPasskeyError(error, 'cancelled') || isPasskeyError(error, 'no-passkey')) {
    return (
      <Message type="info" showIcon closable onClose={onClose} className="mf-chat-failure">
        {error.message}
      </Message>
    )
  }
  return (
    <Message type="error" showIcon closable onClose={onClose} className="mf-chat-failure">
      {isPasskeyError(error) ? error.message : refusal(error)}
    </Message>
  )
}
