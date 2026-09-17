import { Link } from 'react-router'
import { Panel, Progress, Text } from 'rsuite'
import type { Contact, WalletState } from '../../api/types'
import { useContacts } from '../../hooks/useContacts'
import { useNow } from '../../hooks/useNow'
import { formatTimeLeft, formatUsd } from '../../lib/format'
import { summarizeSpending } from '../../lib/spending'
import { LinkButton } from '../LinkButton'

/** Names shown before "and N more". */
const SHOWN_CONTACTS = 8

function resetText(spending: WalletState['spending'], now: number) {
  const { limit, ended, endsAt } = summarizeSpending(spending, now)
  if (limit === 0) return "The limit is $0, so it can't spend anything."
  if (ended) return 'Full limit available.'
  return `Resets in ${formatTimeLeft(endsAt - now)}.`
}

/** Beside the chat on wide screens: what the assistant may still spend, and whom it may pay. */
export function BudgetPanel({ wallet }: { wallet: WalletState }) {
  const now = useNow(30_000)
  const { limit, left, percentLeft } = summarizeSpending(wallet.spending, now)
  const contacts = useContacts()

  return (
    <aside className="mf-chat-aside" aria-label="What the assistant can do">
      <Panel bordered header={<h2>Left to spend</h2>} className="mf-card">
        {wallet.spending.hook_installed ? (
          <>
            <p className="mf-budget-left">
              <strong className="mf-num">{formatUsd(left)}</strong>{' '}
              <Text as="span" muted>
                of {formatUsd(limit)}
              </Text>
            </p>
            <Progress
              percent={percentLeft}
              showInfo={false}
              strokeColor="var(--mf-green)"
              aria-label={`${percentLeft}% of the limit left`}
            />
            <Text size="sm" muted className="mf-budget-reset">
              {resetText(wallet.spending, now)}
            </Text>
          </>
        ) : (
          <>
            <p className="mf-budget-left">
              <strong className="mf-budget-unlimited">No limit</strong>
            </p>
            <Text size="sm" muted>
              The spending limit isn’t switched on for this wallet.
            </Text>
          </>
        )}
      </Panel>
      <Panel bordered header={<h2>Who it can pay</h2>} className="mf-card">
        <ContactNames contacts={contacts.data} failed={contacts.isError} />
        <LinkButton to="/contacts" appearance="link" size="sm" className="mf-budget-manage">
          Manage contacts
        </LinkButton>
      </Panel>
    </aside>
  )
}

function ContactNames({ contacts, failed }: { contacts: Contact[] | undefined; failed: boolean }) {
  if (!contacts) {
    return (
      <Text size="sm" muted>
        {failed ? "Couldn't load your contacts." : 'Loading…'}
      </Text>
    )
  }
  if (contacts.length === 0) {
    return (
      <Text size="sm" muted>
        No one yet. Add a contact before asking it to pay someone.
      </Text>
    )
  }
  const more = contacts.length - SHOWN_CONTACTS
  return (
    <>
      <ul className="mf-budget-contacts">
        {contacts.slice(0, SHOWN_CONTACTS).map(contact => (
          <li key={contact.name}>{contact.name}</li>
        ))}
      </ul>
      {more > 0 && (
        <Text size="sm" muted>
          and {more} more
        </Text>
      )}
    </>
  )
}

/** The same facts in one line, above the chat on phones and tablets. */
export function BudgetStrip({ wallet }: { wallet: WalletState }) {
  const now = useNow(30_000)
  const { limit, left } = summarizeSpending(wallet.spending, now)
  const contacts = useContacts()
  const count = contacts.data?.length

  return (
    <p className="mf-budget-strip">
      {wallet.spending.hook_installed ? (
        <span>
          <strong className="mf-num">{formatUsd(left)}</strong> of <span className="mf-num">{formatUsd(limit)}</span>{' '}
          left
        </span>
      ) : (
        <strong className="mf-budget-unlimited">No spending limit</strong>
      )}
      <Link to="/contacts">
        {count === undefined ? 'Contacts' : `${count} ${count === 1 ? 'contact' : 'contacts'}`}
      </Link>
    </p>
  )
}
