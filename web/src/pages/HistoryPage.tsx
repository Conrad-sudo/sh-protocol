import { type ReactElement, useId, useState } from 'react'
import { useSearchParams } from 'react-router'
import FunnelIcon from '@rsuite/icons/Funnel'
import GearIcon from '@rsuite/icons/Gear'
import HistoryIcon from '@rsuite/icons/History'
import { Button, Placeholder, SegmentedControl, Text } from 'rsuite'
import type { Transaction, TransactionDirection } from '../api/types'
import { useSelectedChain } from '../chain/useSelectedChain'
import { AddressText } from '../components/AddressText'
import { CopyButton } from '../components/CopyButton'
import { EmptyState } from '../components/EmptyState'
import { GlassLayer } from '../components/Glass'
import { FilterChips, FilterPanel } from '../components/history/HistoryFilters'
import { ArrowInIcon, ArrowOutIcon } from '../components/icons'
import { PageHeader } from '../components/PageHeader'
import { QueryError } from '../components/QueryError'
import { StatusTag, type StatusTone } from '../components/StatusTag'
import { useContacts } from '../hooks/useContacts'
import { useTransactions, useTransactionTokens } from '../hooks/useTransactions'
import { formatDateTime, shortAddress } from '../lib/format'
import {
  countFilters,
  type HistoryGroup,
  type HistoryView,
  NO_FILTERS,
  readView,
  serverFilters,
  writeView,
} from '../lib/historyFilters'
import { chainName, explorerName, explorerUrl } from '../wallet/chains'

const TITLE = 'History'
const DESCRIPTION =
  'Every transaction on your Mitfah wallet: what your assistant sent, here or on Telegram, what you signed, and what happened outside Mitfah.'

const SOURCE: Record<Transaction['source'], string> = {
  assistant: 'By your assistant',
  owner: 'By you',
  outside: 'Outside Mitfah',
}

const STATUS: Record<Transaction['status'], { label: string; tone: StatusTone }> = {
  confirmed: { label: 'Confirmed', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  pending: { label: 'Pending', tone: 'warning' },
  dropped: { label: "Didn't go through", tone: 'neutral' },
}

const GROUPS: { label: string; value: HistoryGroup }[] = [
  { label: 'All', value: 'all' },
  { label: 'Incoming', value: 'in' },
  { label: 'Outgoing', value: 'out' },
  { label: 'Wallet changes', value: 'none' },
]

/** What an empty list says on each tab, when no filter is narrowing it. */
const EMPTY: Record<Exclude<HistoryGroup, 'all'>, { title: string; body: string }> = {
  in: { title: 'No incoming transactions yet', body: 'Money and tokens that reach your wallet will be listed here.' },
  out: {
    title: 'No outgoing transactions yet',
    body: 'Payments, withdrawals, swaps and anything else that leaves your wallet will be listed here.',
  },
  none: {
    title: 'No wallet changes yet',
    body: 'Changes that move no money, like a new spending limit or pausing the wallet, will be listed here.',
  },
}

const DIRECTION: Record<TransactionDirection, { label: string; icon: ReactElement }> = {
  in: { label: 'Incoming', icon: <ArrowInIcon /> },
  out: { label: 'Outgoing', icon: <ArrowOutIcon /> },
  none: { label: 'Wallet change', icon: <GearIcon aria-hidden /> },
}

const ADDRESS = /(0x[0-9a-fA-F]{40})/

/**
 * The account's transactions, newest first, each with its date and time and a link to the
 * network's block explorer. The lasting record of what happened: the chat is cleared after every
 * transaction, this list never is.
 *
 * Tabs split it into incoming, outgoing and wallet changes, and the Filter panel narrows it by date,
 * amount, address and token. The server does the searching, so a filter covers every transaction,
 * not just the pages loaded. Both live in the page's address, so a refresh keeps them.
 */
export function HistoryPage() {
  const { chainId, walletChains } = useSelectedChain()
  const [scope, setScope] = useState<'all' | 'network'>('all')
  // Only worth offering with wallets on more than one network: with one, every transaction is on it.
  const canFilter = chainId !== null && walletChains.length > 1
  const scopeChain = canFilter && scope === 'network' ? chainId : null
  const [params, setParams] = useSearchParams()
  const view = readView(params)
  const [panelOpen, setPanelOpen] = useState(false)
  const panelId = useId()
  const history = useTransactions(scopeChain, serverFilters(view))
  const tokens = useTransactionTokens(scopeChain)
  const contacts = useContacts()
  const nameOf = (address: string) =>
    contacts.data?.find(contact => contact.address.toLowerCase() === address.toLowerCase())?.name

  const filters = countFilters(view)
  const show = (next: HistoryView) => setParams(writeView(next), { replace: true })
  const changeGroup = (group: HistoryGroup) =>
    // A wallet change moves no money: an amount, address or token would match none of them.
    show(group === 'none' ? { ...view, group, min: '', max: '', address: '', token: '' } : { ...view, group })
  const clearFilters = () => show({ group: view.group, ...NO_FILTERS })

  const transactions = history.data?.pages.flatMap(page => page.transactions)
  const syncing = history.data?.pages[0]?.syncing === true
  const checking = syncing && (
    <Text size="sm" muted role="status">
      Checking for activity outside Mitfah…
    </Text>
  )

  // A failed refresh keeps showing the list it already has.
  let body
  if (transactions === undefined && !history.isError) body = <Placeholder.Paragraph rows={5} active />
  else if (transactions === undefined) {
    body = <QueryError what="your transactions" error={history.error} onRetry={() => void history.refetch()} />
  } else if (transactions.length === 0 && filters > 0) {
    body = (
      <EmptyState
        icon={<FunnelIcon />}
        title="No transactions match these filters"
        action={<Button onClick={clearFilters}>Clear filters</Button>}
      >
        Try a wider date range, or fewer filters.
      </EmptyState>
    )
  } else if (transactions.length === 0) {
    const empty = view.group === 'all' ? null : EMPTY[view.group]
    body = (
      <>
        <EmptyState icon={<HistoryIcon />} title={empty?.title ?? 'No transactions yet'}>
          {empty?.body ??
            "Payments your assistant makes, changes you sign here, and anything else that reaches your wallet will be listed with their date, time and a link to the network's explorer."}
        </EmptyState>
        {checking}
      </>
    )
  } else {
    body = (
      <section className="mf-tx-section">
        <h2 id="history-heading" className="mf-visually-hidden">
          Transactions
        </h2>
        {checking}
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
        <div className="mf-tx-toolbar">
          <SegmentedControl
            aria-label="Transactions to show"
            className="mf-tx-groups"
            data={GROUPS}
            value={view.group}
            onChange={value => changeGroup(value as HistoryGroup)}
          />
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
          <Button
            className="mf-tx-filter-button"
            startIcon={<FunnelIcon />}
            active={panelOpen}
            aria-expanded={panelOpen}
            aria-controls={panelOpen ? panelId : undefined}
            onClick={() => setPanelOpen(open => !open)}
          >
            Filter
            {filters > 0 && (
              <>
                {' '}
                <span className="mf-visually-hidden">{`(${filters} on)`}</span>
                <span className="mf-tx-filter-count" aria-hidden="true">
                  {filters}
                </span>
              </>
            )}
          </Button>
        </div>
        {panelOpen && (
          <FilterPanel
            // Opened afresh from what is applied, and again whenever the tab changes what it offers.
            key={view.group === 'none' ? 'changes' : 'money'}
            id={panelId}
            view={view}
            contacts={contacts.data ?? []}
            tokens={tokens.data?.tokens ?? []}
            onApply={next => {
              show(next)
              setPanelOpen(false)
            }}
          />
        )}
        <FilterChips view={view} nameOf={nameOf} onChange={show} />
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
        {tx.direction && <DirectionMark direction={tx.direction} />}
        <ActionText text={tx.action} nameOf={nameOf} />
      </p>
      <p className="mf-tx-meta">
        <time dateTime={new Date(at).toISOString()}>{formatDateTime(at)}</time>
        <span aria-hidden="true"> · </span>
        <span>{chainName(tx.chain_id)}</span>
        <span aria-hidden="true"> · </span>
        <span>{SOURCE[tx.source]}</span>
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

/** Which way it went: an arrow in, an arrow out, or a gear for a change that moved no money. */
function DirectionMark({ direction }: { direction: TransactionDirection }) {
  const { label, icon } = DIRECTION[direction]
  return (
    <span className="mf-tx-dir" data-direction={direction} title={label}>
      {icon}
      <span className="mf-visually-hidden">{`${label}: `}</span>
    </span>
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
