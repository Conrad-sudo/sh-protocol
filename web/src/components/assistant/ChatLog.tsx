import { Button, Message } from 'rsuite'
import type { ChatMessage } from '../../api/types'
import { mayHaveRun, type ChatSend } from '../../hooks/useChat'
import { useNow } from '../../hooks/useNow'
import { errorText } from '../../lib/tx'
import { ChatMarkdown } from './ChatMarkdown'

/** After this long the wait message stops promising "up to a minute". */
const LONG_WAIT_MS = 45_000

interface ChatLogProps {
  messages: ChatMessage[]
  latest: ChatSend | null
  /** Send the failed message again. Offered only when the assistant can't have seen it. */
  onRetry: (send: ChatSend) => void
  /** Put the failed message back in the composer. */
  onEdit: (send: ChatSend) => void
  /** Look for a reply to a message that may have reached the assistant. */
  onCheck: (send: ChatSend) => void
}

/** The conversation, then the message still waiting for a reply or the one that failed. */
export function ChatLog({ messages, latest, onRetry, onEdit, onCheck }: ChatLogProps) {
  const unanswered = latest && (latest.status === 'pending' || latest.status === 'error') ? latest : null
  // The server saves a message when its turn starts, so a refetch while waiting can already hold it.
  const last = messages.at(-1)
  const alreadyShown = unanswered !== null && last?.role === 'user' && last.text === unanswered.text

  return (
    <>
      <div className="mf-chat-log" role="log" aria-label="Conversation with Mitfah">
        {messages.map((message, index) => (
          <Bubble key={index} {...message} />
        ))}
        {unanswered && !alreadyShown && <Bubble role="user" text={unanswered.text} />}
      </div>
      {unanswered?.status === 'pending' && <Working since={unanswered.submittedAt} />}
      {unanswered?.status === 'error' && (
        <Failure send={unanswered} onRetry={onRetry} onEdit={onEdit} onCheck={onCheck} />
      )}
    </>
  )
}

function Bubble({ role, text }: ChatMessage) {
  return (
    <div className="mf-msg" data-role={role}>
      <span className="mf-visually-hidden">{role === 'user' ? 'You said:' : 'Mitfah said:'}</span>
      {role === 'user' ? <p className="mf-msg-text">{text}</p> : <ChatMarkdown text={text} />}
    </div>
  )
}

function Working({ since }: { since: number }) {
  const now = useNow(5_000)
  const long = now - since >= LONG_WAIT_MS
  return (
    <div className="mf-chat-working" role="status">
      <span className="mf-typing" aria-hidden>
        <i />
        <i />
        <i />
      </span>
      <span>
        {long
          ? 'Still working. Waiting for the network can take a little longer.'
          : 'Mitfah is working… This can take up to a minute.'}
      </span>
    </div>
  )
}

function Failure({ send, onRetry, onEdit, onCheck }: { send: ChatSend } & Omit<ChatLogProps, 'messages' | 'latest'>) {
  if (mayHaveRun(send.error)) {
    // Sending again could repeat a payment the assistant already made, so look first.
    return (
      <Message type="warning" showIcon className="mf-chat-failure">
        <strong>No reply arrived.</strong> {errorText(send.error)} Mitfah may still have handled your message, so
        check before you send it again.
        <div className="mf-chat-failure-actions">
          <Button appearance="primary" size="sm" onClick={() => onCheck(send)}>
            Check for a reply
          </Button>
        </div>
      </Message>
    )
  }
  return (
    <Message type="error" showIcon className="mf-chat-failure">
      <strong>Not sent.</strong> {errorText(send.error)}
      <div className="mf-chat-failure-actions">
        <Button appearance="primary" size="sm" onClick={() => onRetry(send)}>
          Try again
        </Button>
        <Button appearance="subtle" size="sm" onClick={() => onEdit(send)}>
          Edit message
        </Button>
      </div>
    </Message>
  )
}
