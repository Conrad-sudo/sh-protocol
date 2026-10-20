import { Button } from 'rsuite'
import { telegramBotUrl } from '../../api/telegram'
import { useMe } from '../../hooks/useMe'

/**
 * Opens the Telegram bot, where the same conversation carries on once /chain there picks this
 * network. Only for an account that has linked Telegram, on a server that runs a bot; linking itself
 * is in Settings.
 */
export function TelegramBotLink() {
  const { data: me } = useMe()

  if (!me?.telegram_linked || !me.telegram_bot) return null
  return (
    <Button
      as="a"
      href={telegramBotUrl(me.telegram_bot)}
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
