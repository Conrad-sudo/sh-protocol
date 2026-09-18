import { useEffect, useRef, useState } from 'react'
import { Button, Placeholder, Text } from 'rsuite'
import type { WalletState } from '../api/types'
import { useSelectedChain } from '../chain/useSelectedChain'
import { AssistantNotice } from '../components/assistant/AssistantNotice'
import { BudgetPanel, BudgetStrip } from '../components/assistant/BudgetPanel'
import { ChatLog } from '../components/assistant/ChatLog'
import { Composer } from '../components/assistant/Composer'
import { SuggestedPrompts } from '../components/assistant/SuggestedPrompts'
import { PageHeader } from '../components/PageHeader'
import { QueryError } from '../components/QueryError'
import { useChatHistory, useChatSend, type ChatSend } from '../hooks/useChat'
import { useKeyboardResizesPage } from '../hooks/useKeyboardResizesPage'
import { useWalletView } from '../hooks/useWalletView'
import { useLayoutMode, type LayoutMode } from '../layouts/useLayoutMode'

/** How close to the end counts as "reading the latest", in pixels. */
const NEAR_END = 160

/** Chat with the assistant about the selected network's wallet. */
export function AssistantPage() {
  const view = useWalletView()
  const mode = useLayoutMode()
  useKeyboardResizesPage()

  return (
    <>
      <PageHeader
        title="Assistant"
        description={
          mode === 'mobile' ? undefined : 'Ask about your wallet, or ask it to pay a contact. It can only spend within your limit.'
        }
      />
      {/* Keyed so a network switch starts with that network's conversation and an empty composer. */}
      {view.wallet ? <Chat key={view.wallet.chain_id} wallet={view.wallet} mode={mode} /> : view.fallback}
    </>
  )
}

function scrollToEnd(smooth: boolean) {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth && !reduce ? 'smooth' : 'instant' })
}

function Chat({ wallet, mode }: { wallet: WalletState; mode: LayoutMode }) {
  const chainId = wallet.chain_id
  const { chain } = useSelectedChain()
  const history = useChatHistory(chainId)
  const chat = useChatSend(chainId)
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  // Whether the reader was at the end before the latest change, and whether to go there regardless.
  const atEnd = useRef(true)
  const forceScroll = useRef(false)
  const scrolledOnce = useRef(false)

  const messages = history.data ?? []
  const { latest } = chat
  const unanswered = latest?.status === 'pending' || latest?.status === 'error'
  // The last word is the user's: a reply may still be on its way, or the turn failed on the server.
  const noReply = !unanswered && !history.isFetching && messages.at(-1)?.role === 'user'

  useEffect(() => {
    const onScroll = () => {
      const { scrollHeight } = document.documentElement
      atEnd.current = scrollHeight - window.scrollY - window.innerHeight < NEAR_END
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  // New content: follow it if the reader was following, or if they just sent something.
  useEffect(() => {
    if (history.isPending) return
    if (!scrolledOnce.current || forceScroll.current || atEnd.current) scrollToEnd(scrolledOnce.current)
    scrolledOnce.current = true
    forceScroll.current = false
  }, [history.isPending, messages.length, latest?.id, latest?.status])

  const send = (text: string) => {
    forceScroll.current = true
    chat.send(text)
    setDraft('')
  }
  const retry = (failed: ChatSend) => {
    chat.dismiss(failed.id)
    send(failed.text)
  }
  const edit = (failed: ChatSend) => {
    chat.dismiss(failed.id)
    setDraft(failed.text)
    inputRef.current?.focus()
  }
  const check = (failed: ChatSend) => {
    chat.dismiss(failed.id)
    void history.refetch().then(({ data }) => {
      // Not on the server: it never reached the assistant, so offer it again to send deliberately.
      if (data?.filter(m => m.role === 'user').at(-1)?.text !== failed.text) {
        setDraft(current => current || failed.text)
      }
    })
  }

  const payTicker = (wallet.spending.watched_tokens.find(t => t.ticker)?.ticker ?? chain?.native_ticker ?? 'eth').toUpperCase()

  return (
    <div className="mf-chat-layout" data-wide={mode === 'desktop' || undefined}>
      <section className="mf-chat" aria-label="Chat">
        {mode !== 'desktop' && <BudgetStrip wallet={wallet} />}
        <AssistantNotice wallet={wallet} />
        {history.isPending && <Placeholder.Paragraph rows={4} active />}
        {history.isError && !history.data && (
          <QueryError what="the conversation" error={history.error} onRetry={() => void history.refetch()} />
        )}
        {history.data && messages.length === 0 && !unanswered && (
          <SuggestedPrompts
            payTicker={payTicker}
            disabled={chat.busy}
            onAsk={send}
            onDraft={text => {
              setDraft(text)
              inputRef.current?.focus()
            }}
          />
        )}
        <ChatLog messages={messages} latest={latest} onRetry={retry} onEdit={edit} onCheck={check} />
        {noReply && (
          <p className="mf-chat-noreply">
            <Text as="span" muted>
              No reply to this yet. If Mitfah was still working on it, check again in a minute.
            </Text>{' '}
            <Button appearance="link" size="sm" onClick={() => void history.refetch()}>
              Check again
            </Button>
          </p>
        )}
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={send}
          busy={chat.busy}
          compact={mode === 'mobile'}
          inputRef={inputRef}
        />
      </section>
      {mode === 'desktop' && <BudgetPanel wallet={wallet} />}
    </div>
  )
}
