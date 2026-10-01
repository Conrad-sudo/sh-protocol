import { useState } from 'react'
import HistoryIcon from '@rsuite/icons/History'
import { Button, Placeholder, SegmentedControl, Text } from 'rsuite'
import type { Transaction } from '../api/types'
import { useSelectedChain } from '../chain/useSelectedChain'
import { AddressText } from '../components/AddressText'
import { CopyButton } from '../components/CopyButton'
import { EmptyState } from '../components/EmptyState'
import { GlassLayer } from '../components/Glass'
import { PageHeader } from '../components/PageHeader'
import { QueryError } from '../components/QueryError'
import { StatusTag, type StatusTone } from '../components/StatusTag'
import { useContacts } from '../hooks/useContacts'
import { useTransactions } from '../hooks/useTransactions'
import { formatDateTime, shortAddress } from '../lib/format'
import { chainName, explorerName, explorerUrl } from '../wallet/chains'

const TITLE = 'History'
const DESCRIPTION = 'Every transaction made through Mitfah: what your assistant sent, here or on Telegram, and what you signed.'

const STATUS: Record<Transaction['status'], { label: string; tone: StatusTone }> = {
  confirmed: { label: 'Confirmed', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  pending: { label: 'Pending', tone: 'warning' },
  dropped: { label: "Didn't go through", tone: 'neutral' },
}

const ADDRESS = /(0x[0-9a-fA-F]{40})/

/**
 * The account's transactions, newest first, each with its date and time and a link to the
 * network's block explorer. The lasting record of what happened: the chat is cleared after every
 * transaction, this list never is.
 */
export function HistoryPage() {
  const { chainId, walletChains } = useSelectedChain()
  const [scope, setScope] = useState<'all' | 'network'>('all')
  // Only worth offering with wallets on more than one network: with one, every transaction is on it.
  const canFilter = chainId !== null && walletChains.length > 1
  const history = useTransactions(canFilter && scope === 'network' ? chainId : null)
  const contacts = useContacts()
  const nameOf = (address: string) =>
    contacts.data?.find(contact => contact.address.toLowerCase() === address.toLowerCase())?.name

  const transactions = history.data?.pages.flatMap(page => page.transactions)

  // A failed refresh keeps showing the list it already has.
  let body
  if (transactions === undefined && !history.isError) body = <Placeholder.Paragraph rows={5} active />
  else if (transactions === undefined) {
    body = <QueryError what="your transactions" error={history.error} onRetry={() => void history.refetch()} />
  } else if (transactions.length === 0) {
    body = (
      <EmptyState icon={<HistoryIcon />} title="No transactions yet">
        Payments your assistant makes, and changes you sign here, will be listed with their date, time and a link
        to the network's explorer.
      </EmptyState>
    )
  } else {
    body = (
      <section className="mf-tx-section">
        <h2 id="history-heading" className="mf-visually-hidden">
          Transactions
        </h2>
        {/* The glass sits beside the list, not in it: a list may only hold its items. */}
        <div className="mf-tx-plate mf-glass-surface">
          <GlassLayer />
          <ul className="mf-tx-list" aria-labelledby="history-heading">
            {transactions.map(tx => (
              <TransactionRow key={tx.id} tx={tx} nameOf={nameOf} />
            ))}
          </ul>
        </div>
        {history.hasNextPage && (
          <Button
            appearance="ghost"
            className="mf-tx-more"
            loading={history.isFetchingNextPage}
            onClick={() => void history.fetchNextPage()}
          >
            Show older transactions
          </Button>
        )}
        <Text size="sm" muted>
          Mitfah submits the assistant's transactions for your wallet, so on the explorer they come from Mitfah's
          address. Your transfer is inside the transaction.
        </Text>
      </section>
    )
  }

  return (
    <>
      <PageHeader title={TITLE} description={DESCRIPTION} />
      <div className="mf-dashboard">
        {canFilter && (
          <SegmentedControl
            aria-label="Networks to show"
            className="mf-tx-scope"
            data={[
              { label: 'All networks', value: 'all' },
              { label: chainName(chainId), value: 'network' },
            ]}
            value={scope}
            onChange={value => setScope(value as 'all' | 'network')}
          />
        )}
        {body}
      </div>
    </>
  )
}

function TransactionRow({ tx, nameOf }: { tx: Transaction; nameOf: (address: string) => string | undefined }) {
  const at = (tx.mined_at ?? tx.created_at) * 1_000
  const status = STATUS[tx.status]
  return (
    <li className="mf-tx" data-tx={tx.id}>
      <p className="mf-tx-action">
        <ActionText text={tx.action} nameOf={nameOf} />
      </p>
      <p className="mf-tx-meta">
        <time dateTime={new Date(at).toISOString()}>{formatDateTime(at)}</time>
        <span aria-hidden="true"> · </span>
        <span>{chainName(tx.chain_id)}</span>
        <span aria-hidden="true"> · </span>
        <span>{tx.source === 'assistant' ? 'By your assistant' : 'By you'}</span>
      </p>
      <div className="mf-tx-status">
        <StatusTag tone={status.tone}>{status.label}</StatusTag>
      </div>
      <div className="mf-tx-hash">
        <TxHash tx={tx} />
      </div>
    </li>
  )
}

/** The hash, linked to the network's explorer -- the live one, on a local fork too. */
function TxHash({ tx }: { tx: Transaction }) {
  if (tx.tx_hash === null) {
    return (
      <Text as="span" size="sm" muted>
        Waiting for the network
      </Text>
    )
  }
  const hash = tx.tx_hash
  const link = explorerUrl(tx.chain_id, 'tx', hash)
  const short = <span aria-hidden="true">{shortAddress(hash, 10, 8)}</span>
  return (
    <>
      {link ? (
        <a className="mf-mono" href={link} target="_blank" rel="noreferrer" title={hash}>
          {short}
          <span className="mf-visually-hidden">{`View transaction ${hash} on ${explorerName(tx.chain_id)}`}</span>
        </a>
      ) : (
        <span className="mf-mono" title={hash}>
          {short}
          <span className="mf-visually-hidden">{hash}</span>
        </span>
      )}
      <CopyButton value={hash} label="Transaction hash" />
    </>
  )
}

/** The server's description, with each address shortened -- or named, when it is a contact. */
function ActionText({ text, nameOf }: { text: string; nameOf: (address: string) => string | undefined }) {
  return (
    <>
      {text.split(ADDRESS).map((part, index) => {
        if (index % 2 === 0) return part
        const name = nameOf(part)
        return name ? (
          <strong key={index} title={part}>
            {name}
          </strong>
        ) : (
          <AddressText key={index} address={part} />
        )
      })}
    </>
  )
}
