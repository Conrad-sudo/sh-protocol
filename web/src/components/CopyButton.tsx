import CopyIcon from '@rsuite/icons/Copy'
import { IconButton, Message, Tooltip, useToaster, Whisper } from 'rsuite'

interface CopyButtonProps {
  value: string
  /** What is being copied, for the button's label and the confirmation. */
  label: string
}

export function CopyButton({ value, label }: CopyButtonProps) {
  const toaster = useToaster()

  const copy = async () => {
    let copied = true
    try {
      await navigator.clipboard.writeText(value)
    } catch {
      copied = false // No clipboard permission (or an insecure context).
    }
    toaster.push(
      <Message type={copied ? 'success' : 'error'} showIcon closable>
        {copied ? `${label} copied` : `Couldn't copy the ${label.toLowerCase()}`}
      </Message>,
      { placement: 'topCenter', duration: 2500 },
    )
  }

  return (
    <Whisper placement="top" speaker={<Tooltip>Copy</Tooltip>}>
      <IconButton appearance="subtle" size="sm" icon={<CopyIcon />} aria-label={`Copy ${label.toLowerCase()}`} onClick={copy} />
    </Whisper>
  )
}
