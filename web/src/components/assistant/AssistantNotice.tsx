import { Message } from 'rsuite'
import type { WalletState } from '../../api/types'
import { LinkButton } from '../LinkButton'

/**
 * Why the assistant can't send money right now, or why it could send too much. It can still answer
 * questions either way, so the chat stays open.
 */
export function AssistantNotice({ wallet }: { wallet: WalletState }) {
  let text: string | null = null
  let type: 'warning' | 'error' = 'warning'
  if (wallet.paused) text = 'Your wallet is paused, so the assistant can answer questions but can’t send anything.'
  else if (!wallet.session.active) text = 'The assistant is turned off, so it can answer questions but can’t send anything.'
  else if (!wallet.spending.hook_installed) {
    type = 'error'
    text =
      'The spending limit isn’t switched on for this wallet, so nothing caps what the assistant can spend. Turn the assistant off until it’s fixed.'
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
