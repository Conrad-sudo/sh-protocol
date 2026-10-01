import { Button, Text } from 'rsuite'
import { useContacts } from '../../hooks/useContacts'

interface SuggestedPromptsProps {
  /** The token the payment example uses. */
  payTicker: string
  /** The swap example's two tokens, or null where the assistant can't swap. */
  swap: { from: string; to: string } | null
  /** Send a question as it is. */
  onAsk: (text: string) => void
  /** Put a payment or swap request in the composer to edit: the amounts and person are only examples. */
  onDraft: (text: string) => void
  disabled: boolean
}

/** Starting points for an empty conversation. */
export function SuggestedPrompts({ payTicker, swap, onAsk, onDraft, disabled }: SuggestedPromptsProps) {
  const { data: contacts } = useContacts()
  const payee = contacts?.[0]?.name
  const questions = [
    'What’s in my wallet?',
    'How much can you still spend for me?',
    'Which tokens count toward my limit?',
  ]
  const drafts = [
    ...(payee ? [`Send 5 ${payTicker} to ${payee}`] : []),
    ...(swap ? [`Swap 1 ${swap.from} for ${swap.to}`] : []),
  ]

  return (
    <div className="mf-chat-start">
      <h2 className="mf-chat-start-title">What can I help with?</h2>
      <Text muted>
        Ask about your balances and limit, or ask me to pay one of your contacts or swap one token for another. I’ll
        ask you to confirm before I send anything.
      </Text>
      <ul className="mf-prompts" aria-label="Suggestions">
        {questions.map(question => (
          <li key={question}>
            <Button appearance="ghost" size="sm" disabled={disabled} onClick={() => onAsk(question)}>
              {question}
            </Button>
          </li>
        ))}
        {drafts.map(draft => (
          <li key={draft}>
            <Button appearance="ghost" size="sm" onClick={() => onDraft(draft)}>
              {draft}…
            </Button>
          </li>
        ))}
      </ul>
    </div>
  )
}
