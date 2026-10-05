import { useState, type ReactNode } from 'react'
import { Checkbox, CheckboxGroup, Form, Input, Message, Placeholder, Text } from 'rsuite'
import { getAddress, isAddress, zeroAddress } from 'viem'
import type { AllowlistEntry } from '../../api/types'
import { useAllowlist } from '../../hooks/useAllowlist'
import type { OwnerTxRequest } from '../../hooks/useOwnerAction'
import { AddressText } from '../AddressText'
import { QueryError } from '../QueryError'
import { StatusTag } from '../StatusTag'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { ControlRow } from './ControlRow'
import type { ControlPanelProps } from './types'

type ListProps = ControlPanelProps & { enabled: boolean; targets: AllowlistEntry[] }

/**
 * The contract allowlist. While it's on, the assistant can call only the contracts listed; sending to
 * a plain wallet address is never blocked. Turning it on and removing an entry tighten what the
 * assistant can reach, adding one while it's on and turning it off loosen it. The loosening ones ask
 * first, and so do the two that can stop the assistant working: turning it on, and emptying it.
 *
 * @param open  Whether the Advanced section is open: the list is only read then.
 */
export function AllowlistRow(props: ControlPanelProps & { open: boolean }) {
  const { wallet, tx, locked, ask, open } = props
  const allowlist = useAllowlist(wallet.chain_id, open)
  // The list's own read once it's in, so the switch and the entries always come from the same answer.
  const enabled = allowlist.data?.enabled ?? wallet.limits.allowlist_enabled
  const targets = allowlist.data?.targets

  let body: ReactNode
  if (allowlist.isPending) body = <Placeholder.Paragraph rows={2} active />
  else if (allowlist.isError)
    body = <QueryError what="the allowlist" error={allowlist.error} onRetry={() => void allowlist.refetch()} />
  else if (!targets)
    body = (
      <Text size="sm" muted>
        This wallet was created before Mitfah could show its list, so its entries can't be shown or changed here.
      </Text>
    )
  else {
    const suggested = allowlist.data.suggested
    body = (
      <>
        <ListedTargets {...props} enabled={enabled} targets={targets} />
        <SuggestedTargets
          // Fresh ticks whenever the offer changes: everything ticked to turn on, nothing once it's on.
          key={`${enabled}:${suggested.map(s => s.address).join()}`}
          {...props}
          enabled={enabled}
          targets={targets}
          suggested={suggested}
        />
        <AddTarget key={targets.length} {...props} enabled={enabled} targets={targets} />
      </>
    )
  }

  return (
    <ControlRow
      title="Contract allowlist"
      status={enabled ? <StatusTag tone="success">On</StatusTag> : <StatusTag tone="neutral">Off</StatusTag>}
      help="When on, the assistant can only call the contracts on this list. Sending to a plain wallet address is never blocked."
      action={
        enabled ? (
          <TxButton
            tx={tx}
            txKey="allowlist-disable"
            appearance="ghost"
            color="orange"
            disabled={locked}
            onClick={() =>
              ask({
                title: 'Turn off the contract allowlist?',
                body: (
                  <Text>
                    The assistant will be able to call any contract again, within its spending limit. The list is
                    kept, so you can turn it back on later.
                  </Text>
                ),
                confirmLabel: 'Turn off',
                request: {
                  key: 'allowlist-disable',
                  action: { kind: 'allowlist', action: 'disable', targets: [] },
                  success: 'Contract allowlist off.',
                },
              })
            }
          >
            Turn off
          </TxButton>
        ) : null
      }
    >
      <TxStatus tx={tx} txKey="allowlist-disable" />
      {body}
    </ControlRow>
  )
}

/** "USDC 0x1c7D…7238", or just the address when Mitfah has no name for it. */
function EntryName({ entry }: { entry: AllowlistEntry }) {
  return (
    <span className="mf-token">
      {entry.label && <span>{entry.label}</span>}
      <AddressText address={entry.address} />
    </span>
  )
}

function EntryList({ entries }: { entries: AllowlistEntry[] }) {
  return (
    <ul className="mf-token-list">
      {entries.map(entry => (
        <li key={entry.address}>
          <EntryName entry={entry} />
        </li>
      ))}
    </ul>
  )
}

function ListedTargets({ tx, locked, start, ask, enabled, targets }: ListProps) {
  if (targets.length === 0) {
    return enabled ? (
      <Message type="warning" showIcon>
        The list is empty, so the assistant can't call any contract. Add one below, or turn the list off.
      </Message>
    ) : (
      <Text size="sm" muted>
        No contracts listed yet.
      </Text>
    )
  }
  return (
    <ul className="mf-token-list" aria-label="Allowed contracts">
      {targets.map(target => {
        const key = `allowlist-remove:${target.address}`
        const request: OwnerTxRequest = {
          key,
          action: { kind: 'allowlist', action: 'remove', targets: [target.address] },
          success: `${target.label ?? 'Contract'} removed from the allowlist.`,
        }
        // Removing tightens, so it goes straight through -- unless it would leave an enforced list
        // empty, which stops the assistant calling anything at all.
        const last = enabled && targets.length === 1
        return (
          <li key={target.address}>
            <div className="mf-token-row mf-allow-entry">
              <EntryName entry={target} />
              <TxButton
                tx={tx}
                txKey={key}
                size="sm"
                appearance="subtle"
                disabled={locked}
                aria-label={`Remove ${target.label ?? target.address} from the allowlist`}
                onClick={() =>
                  last
                    ? ask({
                        title: 'Remove the last contract?',
                        body: (
                          <Text>
                            The list stays on, so the assistant won't be able to call any contract until you add one or
                            turn the list off. Sending to plain wallet addresses still works.
                          </Text>
                        ),
                        confirmLabel: 'Remove it',
                        request,
                      })
                    : start(request)
                }
              >
                Remove
              </TxButton>
            </div>
            <TxStatus tx={tx} txKey={key} />
          </li>
        )
      })}
    </ul>
  )
}

/**
 * The contracts the assistant uses that aren't listed yet. While the list is off they come ticked,
 * with the button that turns it on: one signature lists what's ticked and switches it on. Once it's
 * on, nothing is ticked, and allowing what you tick asks first.
 */
function SuggestedTargets({
  tx,
  locked,
  ask,
  enabled,
  targets,
  suggested,
}: ListProps & { suggested: AllowlistEntry[] }) {
  const [selected, setSelected] = useState<string[]>(() => (enabled ? [] : suggested.map(s => s.address)))
  const chosen = suggested.filter(s => selected.includes(s.address))

  if (enabled && suggested.length === 0) return null

  const turnOn = () => {
    const after = [...targets, ...chosen]
    if (after.length === 0) return
    const what = after.length === 1 ? 'this contract' : `these ${after.length} contracts`
    ask({
      title: 'Turn on the contract allowlist?',
      body: (
        <>
          <Text>The assistant will only be able to call {what}:</Text>
          <EntryList entries={after} />
          <Text className="mf-settings-note">
            Sending to a plain wallet address isn't affected. Anything else the assistant tries is refused until you
            add it here.
          </Text>
        </>
      ),
      confirmLabel: 'Turn on',
      request: {
        key: 'allowlist-enable',
        action: { kind: 'allowlist', action: 'enable', targets: chosen.map(s => s.address) },
        success: 'Contract allowlist on. The assistant can only call the contracts listed.',
      },
    })
  }

  const allow = () => {
    if (chosen.length === 0) return
    ask({
      title: chosen.length === 1 ? 'Allow this contract?' : `Allow these ${chosen.length} contracts?`,
      body: (
        <>
          <Text>The assistant will be able to call:</Text>
          <EntryList entries={chosen} />
        </>
      ),
      confirmLabel: 'Allow',
      request: {
        key: 'allowlist-add',
        action: { kind: 'allowlist', action: 'add', targets: chosen.map(s => s.address) },
        success: 'Added to the allowlist.',
      },
    })
  }

  const txKey = enabled ? 'allowlist-add' : 'allowlist-enable'
  const empty = !enabled && targets.length === 0 && chosen.length === 0
  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={enabled ? allow : turnOn}>
      <Form.Group controlId="allowlist-suggested">
        {suggested.length > 0 && (
          <>
            <Form.Label>{enabled ? 'Not on the list yet' : 'Contracts the assistant uses'}</Form.Label>
            <CheckboxGroup
              name="allowlist-suggested"
              aria-labelledby="allowlist-suggested-label"
              aria-describedby="allowlist-suggested-help-text"
              value={selected}
              onChange={value => setSelected(value.map(String))}
            >
              {suggested.map(entry => (
                <Checkbox key={entry.address} value={entry.address} disabled={locked}>
                  <EntryName entry={entry} />
                </Checkbox>
              ))}
            </CheckboxGroup>
            <Form.Text>
              {enabled
                ? 'Tick what the assistant should be able to call.'
                : "Ticked ones are listed when you turn it on. The assistant can't use anything you leave off, such as a token it would send or swap."}
            </Form.Text>
          </>
        )}
        <div className="mf-inline-field">
          <TxButton
            tx={tx}
            txKey={txKey}
            type="submit"
            appearance={enabled ? 'ghost' : 'primary'}
            disabled={locked || (enabled ? chosen.length === 0 : empty)}
          >
            {enabled ? 'Allow selected' : 'Turn on'}
          </TxButton>
        </div>
        {empty && suggested.length === 0 && (
          <Form.Text>Add a contract below first: an empty list can't be turned on.</Form.Text>
        )}
      </Form.Group>
      <TxStatus tx={tx} txKey={txKey} />
    </Form>
  )
}

function AddTarget({ wallet, tx, locked, start, ask, enabled, targets }: ListProps) {
  const [address, setAddress] = useState('')
  const target = isAddress(address, { strict: false }) ? getAddress(address) : null

  let problem: string | null = null
  if (address !== '' && !target) problem = 'Enter a full address starting with 0x.'
  else if (target === zeroAddress) problem = "The zero address can't be listed."
  else if (target === getAddress(wallet.address)) problem = "That's this wallet's own address."
  else if (target && targets.some(t => getAddress(t.address) === target)) problem = 'Already on the list.'

  const add = () => {
    if (!target || problem) return
    const request: OwnerTxRequest = {
      key: 'allowlist-add-address',
      action: { kind: 'allowlist', action: 'add', targets: [target] },
      success: 'Added to the allowlist.',
    }
    // While the list is off an entry changes nothing yet, so it needs no second look.
    if (!enabled) {
      start(request)
      return
    }
    ask({
      title: 'Allow this contract?',
      body: (
        <>
          <Text>
            The assistant will be able to call <AddressText address={target} />, within its spending limit.
          </Text>
          <Text className="mf-settings-note">Only allow a contract you know, such as a token or your exchange's router.</Text>
        </>
      ),
      confirmLabel: 'Allow contract',
      request,
    })
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={add}>
      <Form.Group controlId="add-allowed-target">
        <Form.Label>Add a contract</Form.Label>
        <div className="mf-inline-field">
          <Input
            id="add-allowed-target"
            className="mf-mono"
            placeholder="0x…"
            autoComplete="off"
            spellCheck={false}
            value={address}
            disabled={locked}
            onChange={value => setAddress(value.trim())}
          />
          <TxButton
            tx={tx}
            txKey="allowlist-add-address"
            type="submit"
            appearance="ghost"
            disabled={locked || !target || problem !== null}
          >
            Add
          </TxButton>
        </div>
        {problem && <Form.Text className="mf-error-text">{problem}</Form.Text>}
      </Form.Group>
      <TxStatus tx={tx} txKey="allowlist-add-address" />
    </Form>
  )
}
