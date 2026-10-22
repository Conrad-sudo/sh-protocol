import { Message } from 'rsuite'
import type { WalletState } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { assistantDeadline, assistantStatus } from '../../lib/assistant'
import { formatTimeLeft } from '../../lib/format'
import { LinkButton } from '../LinkButton'

/**
 * Why the assistant can't send money right now, or why it could send too much. It can still answer
 * questions either way, so the chat stays open.
 */
export function AssistantNotice({ wallet }: { wallet: WalletState }) {
  const now = useNow(60_000)
  const assistant = assistantStatus(wallet.session)
  let text: string | null = null
  let type: 'warning' | 'error' = 'warning'
  if (wallet.paused) text = 'Your wallet is paused, so the assistant can answer questions but can’t send anything.'
  else if (assistant === 'expired')
    text = 'The assistant’s access has run out, so it can answer questions but can’t send anything. Renew it in Controls.'
  else if (assistant === 'off' || assistant === 'foreign')
    text =
      'No device can approve payments from this wallet yet, so the assistant can answer questions but can’t send anything. Add this device in Controls.'
  else if (!wallet.spending.hook_installed) {
    type = 'error'
    text =
      'The spending limit isn’t switched on for this wallet, so nothing caps what the assistant can spend. Revoke its devices until it’s fixed.'
  } else if (assistant === 'expiring') {
    const left = formatTimeLeft((assistantDeadline(wallet.session) ?? 0) - now)
    text = `The assistant’s access runs out in ${left}. Renew it in Controls to keep it sending.`
  }
  if (!text) return null

  return (
    <Message type={type} showIcon className="mf-chat-notice">
      {text}{' '}
      {wallet.is_owner && (
        <LinkButton to="/controls" appearance="link" size="sm">
          Open Controls
        </LinkButton>
      )}
    </Message>
  )
}
