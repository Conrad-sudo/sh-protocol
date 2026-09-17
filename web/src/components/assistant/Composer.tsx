import type { KeyboardEvent, RefObject } from 'react'
import SendIcon from '@rsuite/icons/Send'
import { Button, Textarea, useMediaQuery } from 'rsuite'

/** The server's limit for one message. */
export const MAX_MESSAGE = 4_000
/** Show the character count from here on. */
const COUNT_FROM = 3_500

interface ComposerProps {
  value: string
  onChange: (value: string) => void
  onSend: (text: string) => void
  /** A message is waiting for its reply; typing the next one is fine, sending it isn't. */
  busy: boolean
  compact: boolean
  inputRef: RefObject<HTMLTextAreaElement | null>
}

/**
 * Where the user types. Enter sends and Shift+Enter starts a new line — except on touch screens,
 * whose keyboards have no Shift key to spare, where Enter is a new line and the button sends.
 */
export function Composer({ value, onChange, onSend, busy, compact, inputRef }: ComposerProps) {
  const [touch] = useMediaQuery(['(pointer: coarse)'])
  const text = value.trim()
  const tooLong = value.length > MAX_MESSAGE
  const canSend = text !== '' && !tooLong && !busy

  const submit = () => {
    if (!canSend) return
    onSend(text)
    // The button that was pressed is now disabled, which would drop focus: keep typing where you were.
    inputRef.current?.focus()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (touch || event.key !== 'Enter' || event.shiftKey) return
    event.preventDefault()
    // Enter also confirms a word being composed with an input method (Japanese, Chinese…); Safari
    // reports that as keyCode 229 after already clearing isComposing.
    if (event.nativeEvent.isComposing || event.keyCode === 229) return
    submit()
  }

  return (
    <form
      className="mf-composer"
      onSubmit={event => {
        event.preventDefault()
        submit()
      }}
    >
      <label htmlFor="mf-composer-input" className="mf-visually-hidden">
        Message
      </label>
      <Textarea
        id="mf-composer-input"
        ref={inputRef}
        value={value}
        onChange={next => onChange(next)}
        onKeyDown={onKeyDown}
        placeholder="Ask Mitfah…"
        autosize
        minRows={1}
        maxRows={6}
        rows={1}
        resize="none"
        enterKeyHint={touch ? 'enter' : 'send'}
        aria-describedby={value.length >= COUNT_FROM ? 'mf-composer-count' : undefined}
        aria-invalid={tooLong || undefined}
      />
      <Button
        type="submit"
        appearance="primary"
        disabled={!canSend}
        startIcon={<SendIcon />}
        aria-label={compact ? 'Send' : undefined}
        className="mf-composer-send"
        // Pressing it mustn't take focus from the message. On a phone that would close the keyboard,
        // bring the tab bar back and move the button before the tap lands.
        onPointerDown={event => event.preventDefault()}
      >
        {compact ? null : 'Send'}
      </Button>
      {value.length >= COUNT_FROM && (
        <p id="mf-composer-count" className="mf-composer-count mf-num" data-over={tooLong || undefined}>
          {value.length.toLocaleString()} / {MAX_MESSAGE.toLocaleString()}
          {tooLong && ' — too long to send'}
        </p>
      )}
    </form>
  )
}
