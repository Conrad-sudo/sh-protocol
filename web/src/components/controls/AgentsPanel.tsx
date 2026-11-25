import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Checkbox, Form, Input, Message, NumberInput, Placeholder, SegmentedControl, Text } from 'rsuite'
import { getAddress, isAddress } from 'viem'
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts'
import type { Agent, Agents, Chain, WalletState } from '../../api/types'
import { renameAgent } from '../../api/wallet'
import { useAgentStop } from '../../hooks/useAgentStop'
import { agentsKey, useAgents } from '../../hooks/useAgents'
import { agentName } from '../../lib/agents'
import { formatDate, formatUsd, formatWindow } from '../../lib/format'
import { errorText } from '../../lib/tx'
import { CopyButton } from '../CopyButton'
import { GlassPanel } from '../Glass'
import { QueryError } from '../QueryError'
import { StatusTag } from '../StatusTag'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { ControlRow } from './ControlRow'
import { StopAgentDialog } from './StopAgentDialog'
import type { ControlPanelProps } from './types'

/** How long an agent works, offered when adding or changing one. The module allows up to 90 days. */
const DAY_CHOICES = [
  { label: '7 days', value: 7 },
  { label: '30 days', value: 30 },
  { label: '90 days', value: 90 },
]
const DEFAULT_DAYS = 30
/** The longest name an agent may have (the server's agents.NAME_MAX_LENGTH). */
const AGENT_NAME_MAX = 40
const ADD_KEY = 'agent-add'
const NATIVE_KEY = 'agent-native-metering'
/** Dollars, with cents at most: how a limit is typed. */
const DOLLARS = /^\d+(\.\d{1,2})?$/

type PanelProps = ControlPanelProps & { chain: Chain | undefined }

/** Why a typed limit can't be used, or null when it can (or nothing is typed yet). */
function limitProblem(value: string): string | null {
  if (value === '') return null
  if (!DOLLARS.test(value)) return 'Enter dollars, like 50 or 12.50.'
  if (Number(value) <= 0) return 'Enter more than $0.'
  return null
}

/** Unix seconds, `days` from now: when an agent added or changed now stops working. */
const daysFromNow = (days: number) => Math.floor(Date.now() / 1000) + days * 86_400

/** "every 24 hours": the wallet's period, as the limits are explained. */
const periodText = (wallet: WalletState) => `every ${formatWindow(wallet.spending.window_hours * 3_600)}`

/**
 * The owner's own bots and AI agents that trade from the wallet without a passkey: each signs with
 * its own key, under its own dollar limit, inside the wallet's. Only the owner wallet adds an agent or
 * gives one more; a passkey can stop one when the owner wallet isn't at hand.
 */
export function AgentsPanel(props: PanelProps) {
  const agents = useAgents(props.wallet.chain_id)

  return (
    <GlassPanel bordered header={<h2>Trading agents</h2>} className="mf-card">
      <Text muted size="sm">
        Your own bots and AI agents that trade from this wallet without your passkey. Each signs its trades with its own
        key and has its own dollar limit, inside the wallet's.
      </Text>
      {agents.isPending ? (
        <Placeholder.Paragraph rows={2} active />
      ) : agents.isError ? (
        <QueryError what="the trading agents" onRetry={() => void agents.refetch()} className="mf-settings-note" />
      ) : !agents.data.available ? (
        <Text muted className="mf-settings-note">
          This wallet can't have trading agents: it was created before they existed. A wallet created now can.
        </Text>
      ) : (
        <AgentList {...props} agents={agents.data} />
      )}
    </GlassPanel>
  )
}

function AgentList(props: PanelProps & { agents: Agents }) {
  const { wallet, agents } = props
  const stop = useAgentStop(wallet.chain_id)

  return (
    <>
      {agents.agents.length > 0 ? (
        <ul className="mf-device-list" aria-label="Trading agents">
          {agents.agents.map(agent => (
            <li key={agent.address}>
              <AgentRow {...props} agent={agent} onStop={() => stop.begin(agent)} />
            </li>
          ))}
        </ul>
      ) : (
        <Text muted className="mf-settings-note">
          No agents yet.
        </Text>
      )}
      {agents.agents.length < agents.max_agents ? (
        // A fresh form, and any key made in it forgotten, once the agents change: one was added.
        <AddAgent key={agents.agents.map(agent => agent.address).join()} {...props} />
      ) : (
        <Text size="sm" muted className="mf-settings-note">
          This wallet has {agents.max_agents} agents, the most it can have. Remove one to add another.
        </Text>
      )}
      <Text size="sm" muted className="mf-settings-note">
        An agent reaches Mitfah with a personal access token that can “read and prepare” (Connected apps, above). Revoking
        the token doesn't stop it, since it holds its own key: removing it does, as do its expiry and pausing the wallet.
      </Text>
      <StopAgentDialog stop={stop} chainId={wallet.chain_id} />
    </>
  )
}

/** One agent: what it has spent of its limit, until when it works, and the owner's changes to it. */
function AgentRow({ wallet, tx, locked, start, ask, agent, onStop }: PanelProps & { agent: Agent; onStop: () => void }) {
  const [editing, setEditing] = useState<'change' | 'rename' | null>(null)
  const name = agentName(agent)
  const removeKey = `agent-remove:${agent.address}`
  const changeKey = `agent-change:${agent.address}`
  const until = formatDate(agent.valid_until * 1_000)

  const help = wallet.paused
    ? `The wallet is paused, so no agent can trade until you unpause it. ${formatUsd(agent.spent_usd)} of ${formatUsd(agent.limit_usd)} spent this period.`
    : agent.active
      ? `${formatUsd(agent.spent_usd)} of ${formatUsd(agent.limit_usd)} spent this period. Works until ${until}.`
      : `Stopped working on ${until}. Change it to give it more time, or remove it.`

  return (
    <ControlRow
      title={name}
      status={agent.active ? <StatusTag tone="success">Active</StatusTag> : <StatusTag>Expired</StatusTag>}
      help={help}
      action={
        <div className="mf-control-buttons">
          <TxButton
            tx={tx}
            txKey={changeKey}
            size="sm"
            appearance="ghost"
            color="orange"
            disabled={locked}
            aria-label={`Change ${name}`}
            onClick={() => setEditing(editing === 'change' ? null : 'change')}
          >
            Change
          </TxButton>
          <Button
            size="sm"
            appearance="subtle"
            aria-label={`Rename ${name}`}
            onClick={() => setEditing(editing === 'rename' ? null : 'rename')}
          >
            Rename
          </Button>
          {locked ? (
            // No owner wallet to sign with: the passkey can still take the agent off.
            <Button
              size="sm"
              appearance="ghost"
              color="red"
              disabled={wallet.paused}
              aria-label={`Stop ${name} with your passkey`}
              onClick={onStop}
            >
              Stop with passkey
            </Button>
          ) : (
            <TxButton
              tx={tx}
              txKey={removeKey}
              size="sm"
              appearance="ghost"
              color="red"
              aria-label={`Remove ${name}`}
              onClick={() =>
                start({
                  key: removeKey,
                  action: { kind: 'agent', action: 'remove', agent: agent.address },
                  success: `${name} can no longer trade from this wallet.`,
                })
              }
            >
              Remove
            </TxButton>
          )}
        </div>
      }
    >
      <div className="mf-connect-url">
        <code>{agent.address}</code>
        <CopyButton value={agent.address} label="Agent's address" />
      </div>
      {editing === 'rename' && <RenameAgentForm wallet={wallet} agent={agent} onDone={() => setEditing(null)} />}
      {editing === 'change' && (
        <ChangeAgentForm
          wallet={wallet}
          tx={tx}
          locked={locked}
          start={start}
          ask={ask}
          agent={agent}
          txKey={changeKey}
          onDone={() => setEditing(null)}
        />
      )}
      <TxStatus tx={tx} txKey={changeKey} />
      <TxStatus tx={tx} txKey={removeKey} />
    </ControlRow>
  )
}

/**
 * A new limit and a new end for one agent. Giving it more (a higher limit, or longer than it has
 * now) asks first; giving it less doesn't.
 */
function ChangeAgentForm({
  wallet,
  tx,
  locked,
  start,
  ask,
  agent,
  txKey,
  onDone,
}: ControlPanelProps & { agent: Agent; txKey: string; onDone: () => void }) {
  const [limit, setLimit] = useState(String(agent.limit_usd))
  const [days, setDays] = useState(DEFAULT_DAYS)
  const problem = limitProblem(limit)
  const name = agentName(agent)
  const id = `agent-limit-${agent.address}`

  const save = () => {
    if (problem || limit === '') return
    const request = {
      key: txKey,
      action: { kind: 'agent', action: 'update', agent: agent.address, limitUsd: limit, days },
      success: `${name} may now spend up to ${formatUsd(Number(limit))} ${periodText(wallet)}, until ${formatDate(daysFromNow(days) * 1_000)}.`,
    } as const
    onDone()
    if (Number(limit) > agent.limit_usd || daysFromNow(days) > agent.valid_until) {
      ask({
        title: `Give ${name} more?`,
        body: (
          <Text>
            {name} will be able to trade up to {formatUsd(Number(limit))} {periodText(wallet)}, until{' '}
            {formatDate(daysFromNow(days) * 1_000)}, without your passkey.
          </Text>
        ),
        confirmLabel: 'Change agent',
        request,
      })
    } else {
      start(request)
    }
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={save}>
      <Form.Group controlId={id}>
        <Form.Label>New limit per period</Form.Label>
        <NumberInput
          id={id}
          prefix="$"
          min={0}
          step={10}
          value={limit}
          disabled={locked}
          onChange={next => setLimit(next === null ? '' : String(next))}
        />
        {problem && <Form.Text className="mf-error-text">{problem}</Form.Text>}
      </Form.Group>
      <Form.Group controlId={`agent-days-${agent.address}`}>
        <Form.Label>Works for</Form.Label>
        <SegmentedControl
          aria-label="Works for"
          data={DAY_CHOICES}
          value={days}
          disabled={locked}
          onChange={next => setDays(Number(next))}
        />
        <Form.Text>From now. It works until {formatDate(agent.valid_until * 1_000)} as it is.</Form.Text>
      </Form.Group>
      <div className="mf-control-buttons">
        <TxButton tx={tx} txKey={txKey} type="submit" size="sm" appearance="primary" disabled={locked || !!problem || limit === ''}>
          Save
        </TxButton>
        <Button size="sm" appearance="subtle" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </Form>
  )
}

/** Renames an agent, in Mitfah's database only: nothing to sign. */
function RenameAgentForm({ wallet, agent, onDone }: { wallet: WalletState; agent: Agent; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState(agent.name ?? '')
  const rename = useMutation({ mutationFn: (name: string) => renameAgent(wallet.chain_id, agent.address, name) })
  const id = `agent-rename-${agent.address}`
  const empty = value.trim() === ''

  const save = () => {
    if (empty) return
    if (value.trim() === agent.name) return onDone()
    rename.mutate(value.trim(), {
      onSuccess: saved => {
        queryClient.setQueryData<Agents>(agentsKey(wallet.chain_id), old =>
          old && { ...old, agents: old.agents.map(a => (a.address === saved.agent ? { ...a, name: saved.name } : a)) },
        )
        onDone()
      },
    })
  }

  return (
    <Form className="mf-control-form" onSubmit={save}>
      <Form.Group controlId={id}>
        <Form.Label>New name</Form.Label>
        <div className="mf-inline-field">
          <Input
            id={id}
            value={value}
            maxLength={AGENT_NAME_MAX}
            autoComplete="off"
            onChange={next => {
              setValue(next)
              rename.reset()
            }}
            aria-describedby={`${id}-help-text`}
          />
          <Button type="submit" size="sm" appearance="primary" loading={rename.isPending} disabled={empty}>
            Save
          </Button>
          <Button size="sm" appearance="subtle" disabled={rename.isPending} onClick={onDone}>
            Cancel
          </Button>
        </div>
        {rename.isError ? (
          <Form.Text className="mf-error-text">Couldn't rename it: {errorText(rename.error)}</Form.Text>
        ) : (
          <Form.Text>Only Mitfah keeps the name: nothing changes on the wallet.</Form.Text>
        )}
      </Form.Group>
    </Form>
  )
}

/** A key made in this browser for a new agent: shown once, never sent to Mitfah. */
interface MadeKey {
  privateKey: `0x${string}`
  address: string
}

/**
 * Adds an agent: its name, the address of the key it signs with (pasted, or made here and shown once),
 * its limit, and how long it works. Adding gives it power, so it asks first. An agent can't be added
 * while the limit leaves ETH/BNB out, so that comes first.
 */
function AddAgent({ wallet, tx, locked, start, ask, chain }: PanelProps) {
  const native = chain?.native_ticker ?? 'the native token'
  const [name, setName] = useState('')
  const [address, setAddress] = useState('')
  const [made, setMade] = useState<MadeKey | null>(null)
  const [saved, setSaved] = useState(false)
  const [limit, setLimit] = useState('')
  const [days, setDays] = useState(DEFAULT_DAYS)

  if (!wallet.spending.native_metered) {
    return (
      <Message type="info" showIcon className="mf-settings-note">
        <Text>
          Agents can trade only while the limit counts {native}, so none of it can leave unseen. Count {native} first,
          then add an agent here.
        </Text>
        <TxButton
          tx={tx}
          txKey={NATIVE_KEY}
          size="sm"
          appearance="primary"
          disabled={locked}
          onClick={() =>
            start({
              key: NATIVE_KEY,
              action: { kind: 'native-metering', metered: true },
              success: `${native} now counts toward your limit.`,
            })
          }
        >
          Count {native}
        </TxButton>
        <TxStatus tx={tx} txKey={NATIVE_KEY} />
      </Message>
    )
  }

  const nameProblem = name.length > 0 && name.trim() === '' ? 'Give it a name.' : null
  const addressProblem =
    address === ''
      ? null
      : !isAddress(address, { strict: false })
        ? 'Enter a full address starting with 0x.'
        : getAddress(address) === getAddress(wallet.address) || getAddress(address) === getAddress(wallet.owner)
          ? "An agent needs a key of its own, not the wallet's or its owner's."
          : null
  const problem = limitProblem(limit)
  const valid =
    name.trim() !== '' && address !== '' && limit !== '' && !addressProblem && !problem && (!made || saved)

  const makeKey = () => {
    const privateKey = generatePrivateKey()
    const made = { privateKey, address: privateKeyToAccount(privateKey).address }
    setMade(made)
    setSaved(false)
    setAddress(made.address)
  }

  const add = () => {
    if (!valid) return
    const agent = getAddress(address)
    const who = `“${name.trim()}”`
    const ends = formatDate(daysFromNow(days) * 1_000)
    ask({
      title: `Add ${who} as a trading agent?`,
      body: (
        <>
          <Text>
            {who} will be able to trade from this wallet without your passkey, on any exchange or protocol, up to{' '}
            {formatUsd(Number(limit))} {periodText(wallet)} until {ends}. Its trades count toward the wallet's limit too.
          </Text>
          <Text className="mf-settings-note">
            Funds the wallet already has in other protocols, such as pool positions or deposits, are outside the limit:
            an agent could take them out anywhere.
          </Text>
          <Text className="mf-settings-note">
            Revoking its access token won't stop it. Removing it here will, as will its expiry or pausing the wallet.
          </Text>
        </>
      ),
      confirmLabel: 'Add agent',
      request: {
        key: ADD_KEY,
        action: { kind: 'agent', action: 'add', agent, limitUsd: limit, days, name: name.trim() },
        success: `${who} can trade now, up to ${formatUsd(Number(limit))} ${periodText(wallet)}.`,
      },
    })
  }

  return (
    <Form fluid className="mf-control-form" disabled={locked} onSubmit={add}>
      <h3>Add an agent</h3>
      <Form.Group controlId="agent-name">
        <Form.Label>Agent's name</Form.Label>
        <Input
          id="agent-name"
          value={name}
          maxLength={AGENT_NAME_MAX}
          autoComplete="off"
          onChange={setName}
          disabled={locked}
          aria-describedby="agent-name-help-text"
        />
        {nameProblem ? (
          <Form.Text className="mf-error-text">{nameProblem}</Form.Text>
        ) : (
          <Form.Text>So you can tell your agents apart. History and Telegram use it.</Form.Text>
        )}
      </Form.Group>
      <Form.Group controlId="agent-address">
        <Form.Label>Agent's address</Form.Label>
        <Input
          id="agent-address"
          className="mf-mono"
          autoComplete="off"
          spellCheck={false}
          placeholder="0x…"
          value={address}
          onChange={value => {
            setAddress(value.trim())
            // A key made here belongs to its own address only.
            if (made && value.trim() !== made.address) setMade(null)
          }}
          disabled={locked}
          aria-describedby="agent-address-help-text"
        />
        {addressProblem ? (
          <Form.Text className="mf-error-text">{addressProblem}</Form.Text>
        ) : (
          <Form.Text>
            The address of the key your agent signs its trades with. It needs no funds: the wallet pays its gas.{' '}
            {!made && (
              <Button appearance="link" size="sm" disabled={locked} onClick={makeKey}>
                No key yet? Make one here
              </Button>
            )}
          </Form.Text>
        )}
      </Form.Group>
      {made && <MadeKeyNote made={made} saved={saved} onSaved={setSaved} />}
      <Form.Group controlId="agent-limit">
        <Form.Label>Agent's limit per period</Form.Label>
        <NumberInput
          id="agent-limit"
          prefix="$"
          min={0}
          step={10}
          value={limit}
          disabled={locked}
          onChange={next => setLimit(next === null ? '' : String(next))}
        />
        {problem ? (
          <Form.Text className="mf-error-text">{problem}</Form.Text>
        ) : (
          <Form.Text>
            The most it can spend {periodText(wallet)}, fees included, inside the wallet's own limit of{' '}
            {formatUsd(wallet.spending.daily_limit_usd)}.
          </Form.Text>
        )}
      </Form.Group>
      <Form.Group controlId="agent-days">
        <Form.Label>Works for</Form.Label>
        <SegmentedControl
          aria-label="Works for"
          data={DAY_CHOICES}
          value={days}
          disabled={locked}
          onChange={next => setDays(Number(next))}
        />
        <Form.Text>You can give it more time later, or remove it at any time.</Form.Text>
      </Form.Group>
      <TxButton tx={tx} txKey={ADD_KEY} type="submit" appearance="primary" disabled={locked || !valid}>
        Add agent
      </TxButton>
      <TxStatus tx={tx} txKey={ADD_KEY} />
    </Form>
  )
}

/**
 * The key just made for a new agent, shown this once with its copy button. It lives only in this
 * page: Mitfah never receives it, and it is gone once the agent is added or the page is left.
 */
function MadeKeyNote({ made, saved, onSaved }: { made: MadeKey; saved: boolean; onSaved: (saved: boolean) => void }) {
  return (
    <Message type="warning" showIcon className="mf-settings-note">
      <Text>
        Your agent's private key. Copy it into your agent now: Mitfah doesn't keep it and can't show it again. Anyone who
        has it can trade from this wallet, within the agent's limit, until you remove the agent.
      </Text>
      <div className="mf-connect-url">
        <code>{made.privateKey}</code>
        <CopyButton value={made.privateKey} label="Private key" />
      </div>
      <Checkbox checked={saved} onChange={(_value, checked) => onSaved(checked)}>
        I've put this key in my agent
      </Checkbox>
    </Message>
  )
}
