import { Button } from 'rsuite'
import { telegramBotUrl } from '../../api/telegram'
import { useSelectedChain } from '../../chain/useSelectedChain'
import { useMe } from '../../hooks/useMe'

/**
 * Opens this network's Telegram bot, where the same conversation carries on. Only for an account
 * that has linked Telegram, and only where the network has a bot; linking itself is in Settings.
 */
export function TelegramBotLink({ chainId }: { chainId: number }) {
  const { data: me } = useMe()
  const { chains } = useSelectedChain()
  const username = chains.find(chain => chain.chain_id === chainId)?.telegram_bot

  if (!me?.telegram_linked || !username) return null
  return (
    <Button
      as="a"
      href={telegramBotUrl(username)}
      target="_blank"
      rel="noopener noreferrer"
      appearance="subtle"
      size="sm"
      role="link"
    >
      Open in Telegram
    </Button>
  )
}
