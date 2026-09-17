import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'
import { Button, Loader, Message, Panel, Text, useToaster } from 'rsuite'
import { ApiError } from '../../api/client'
import { createTelegramLink, unlinkTelegram } from '../../api/telegram'
import type { Me } from '../../api/types'
import { useMe } from '../../hooks/useMe'
import { useNow } from '../../hooks/useNow'
import { StatusTag } from '../StatusTag'

const POLL_MS = 3_000

interface PendingLink {
  url: string
  expiresAt: number
}

/** "9:58" */
function formatCountdown(ms: number) {
  const seconds = Math.max(0, Math.ceil(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

function linkErrorText(error: Error) {
  // The server answers 500 "TELEGRAM_BOT_USERNAME is not configured" when no bot is set up.
  if (error instanceof ApiError && error.status === 500 && /not configured/i.test(error.message)) {
    return "Telegram isn't set up on this server."
  }
  return error.message
}

/**
 * The link the user follows to finish in Telegram, and the wait for it. While mounted it polls
 * /api/me; the card switches to "Linked" as soon as the bot has tied the chat to this account.
 */
function WaitingForTelegram({ link, onRenew }: { link: PendingLink; onRenew: () => void }) {
  const now = useNow(1_000)
  const left = link.expiresAt - now
  const expired = left <= 0
  useMe({ pollMs: expired ? false : POLL_MS })

  if (expired) {
    return (
      <Message type="warning" showIcon className="mf-settings-note">
        That link has expired.{' '}
        <Button appearance="link" size="sm" onClick={onRenew}>
          Get a new link
        </Button>
      </Message>
    )
  }

  return (
    <div className="mf-telegram-pending">
      <Text>
        Open the link in Telegram and press <strong>Start</strong>. The link works once, for{' '}
        <span className="mf-num">{formatCountdown(left)}</span> more.
      </Text>
      <div className="mf-telegram-qr">
        <div className="mf-qr">
          <QRCodeSVG value={link.url} size={144} marginSize={2} title="QR code of the Telegram link" />
        </div>
        <Text muted size="sm">
          Or scan this with your phone's camera.
        </Text>
      </div>
      <div className="mf-telegram-actions">
        <Button as="a" href={link.url} target="_blank" rel="noopener noreferrer" appearance="primary" role="link">
          Open Telegram
        </Button>
        <Loader content="Waiting for Telegram…" />
      </div>
      <Text muted size="sm">
        If the bot says this chat is already linked, it belongs to another Mitfah account. Unlink it there first.
      </Text>
    </div>
  )
}

/**
 * Linking the Telegram bot to this account. The server hands out a one-time t.me link; the bot
 * learns the chat from Telegram itself when the user presses Start, so nothing here asks for a
 * chat id (one that could be typed could be someone else's).
 */
export function TelegramCard({ me }: { me: Me }) {
  const queryClient = useQueryClient()
  const toaster = useToaster()
  const [link, setLink] = useState<PendingLink | null>(null)
  const linked = me.telegram_linked
  const waiting = link !== null && !linked

  const create = useMutation({
    mutationFn: createTelegramLink,
    onSuccess: data => setLink({ url: data.url, expiresAt: Date.now() + data.expires_in * 1000 }),
  })

  const unlink = useMutation({
    mutationFn: unlinkTelegram,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['me'] }),
  })

  // The chat was linked while this page waited for it.
  useEffect(() => {
    if (linked && link) {
      toaster.push(
        <Message type="success" showIcon closable>
          Telegram linked. You can now chat with your assistant there.
        </Message>,
        { placement: 'topCenter', duration: 3000 },
      )
    }
  }, [linked, link, toaster])

  const startLink = () => {
    setLink(null)
    unlink.reset()
    create.mutate()
  }

  let action
  if (linked) {
    action = (
      <>
        <StatusTag tone="success">Linked</StatusTag>
        <Button
          size="sm"
          appearance="ghost"
          loading={unlink.isPending}
          onClick={() => unlink.mutate(undefined, { onSuccess: () => setLink(null) })}
        >
          Unlink
        </Button>
      </>
    )
  } else if (!waiting) {
    action = (
      <Button appearance="primary" loading={create.isPending} onClick={startLink}>
        Link Telegram
      </Button>
    )
  }

  return (
    <Panel bordered header="Telegram" id="telegram">
      <div className="mf-settings-row">
        <div className="mf-settings-row-label">
          <Text weight="medium">Telegram bot</Text>
          <Text muted size="sm">
            {linked
              ? 'You can chat with your assistant in Telegram, and it warns you there when your limit runs low.'
              : 'Chat with your assistant from Telegram too.'}
          </Text>
        </div>
        {action && <div className="mf-settings-row-action">{action}</div>}
      </div>

      {waiting && <WaitingForTelegram link={link} onRenew={startLink} />}

      {create.error && !waiting && (
        <Message type="error" showIcon className="mf-settings-note">
          {linkErrorText(create.error)}
        </Message>
      )}
      {unlink.error && (
        <Message type="error" showIcon className="mf-settings-note">
          Couldn't unlink Telegram. {unlink.error.message}
        </Message>
      )}
    </Panel>
  )
}
