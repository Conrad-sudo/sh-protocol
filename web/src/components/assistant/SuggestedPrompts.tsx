import { Button, Text } from 'rsuite'
import { useContacts } from '../../hooks/useContacts'

interface SuggestedPromptsProps {
  /** The token the payment example uses. */
  payTicker: string
  /** Send a question as it is. */
  onAsk: (text: string) => void
  /** Put a payment request in the composer to edit: the amount and person are only examples. */
  onDraft: (text: string) => void
  disabled: boolean
}

/** Starting points for an empty conversation. */
export function SuggestedPrompts({ payTicker, onAsk, onDraft, disabled }: SuggestedPromptsProps) {
  const { data: contacts } = useContacts()
  const payee = contacts?.[0]?.name
  const questions = [
    'What’s in my wallet?',
    'How much can you still spend for me?',
    'Which tokens count toward my limit?',
  ]

  return (
    <div className="mf-chat-start">
      <h2 className="mf-chat-start-title">What can I help with?</h2>
      <Text muted>
        Ask about your balances and limit, or ask me to pay one of your contacts. I’ll ask you to confirm before I
        send anything.
      </Text>
      <ul className="mf-prompts" aria-label="Suggestions">
        {questions.map(question => (
          <li key={question}>
            <Button appearance="ghost" size="sm" disabled={disabled} onClick={() => onAsk(question)}>
              {question}
            </Button>
          </li>
        ))}
        {payee && (
          <li>
            <Button appearance="ghost" size="sm" onClick={() => onDraft(`Send 5 ${payTicker} to ${payee}`)}>
              Send 5 {payTicker} to {payee}…
            </Button>
          </li>
        )}
      </ul>
    </div>
  )
}
