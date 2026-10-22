import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, Loader, Text } from 'rsuite'
import { deletePasskey, renamePasskey } from '../../api/passkeys'
import type { Passkey, SessionKey, WalletState } from '../../api/types'
import { fetchWallet } from '../../api/wallet'
import { useSelectedChain } from '../../chain/useSelectedChain'
import { useNow } from '../../hooks/useNow'
import { useOwnerAction } from '../../hooks/useOwnerAction'
import {
  PASSKEYS_KEY,
  useDevicePasskeyIds,
  useDevicePasskeyMaker,
  usePasskeys,
  useRegistrationOptions,
} from '../../hooks/usePasskeys'
import { ASSISTANT_KEY_DAYS, ASSISTANT_KEY_TTL_SECS } from '../../lib/assistant'
import { formatDate, formatTimeLeft, formatUsd, formatWindow } from '../../lib/format'
import { DEVICE_NAME_MAX, defaultDeviceName, deviceNameProblem, passkeysSupported } from '../../lib/passkey'
import { errorText } from '../../lib/tx'
import { chainName } from '../../wallet/chains'
import { GlassPanel } from '../Glass'
import { PasskeyProblem } from '../PasskeyProblem'
import { QueryError } from '../QueryError'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { StatusTag } from '../StatusTag'
import { ControlRow } from './ControlRow'
import type { ControlPanelProps } from './types'

/**
 * What adding or renewing a device lets it do, in one sentence. `who` leads it ("iPhone").
 *
 * @return  The sentence, and the date it lasts until.
 */
function grantText(wallet: WalletState, who: string, renewing: boolean) {
  const until = formatDate(Date.now() + ASSISTANT_KEY_TTL_SECS * 1_000)
  const { daily_limit_usd: limit, window_hours: hours } = wallet.spending
  const period = formatWindow(Math.round(hours * 3_600))
  const text = renewing
    ? `${who} keeps approving the assistant's payments from this wallet for another ${ASSISTANT_KEY_DAYS} days, until ${until}, up to ${formatUsd(limit)} every ${period}.`
    : `${who} will be able to approve the assistant's payments from this wallet for ${ASSISTANT_KEY_DAYS} days, until ${until}, up to ${formatUsd(limit)} every ${period}.`
  return { text, until }
}

/** The owner's request that lets `passkey` approve payments on the wallet's network, or renews it. */
function grantRequest(wallet: WalletState, keyId: string, name: string, txKey: string, renewing: boolean) {
  const { until } = grantText(wallet, name, renewing)
  return {
    key: txKey,
    action: { kind: 'session', action: 'add', keyId, ttlSecs: ASSISTANT_KEY_TTL_SECS } as const,
    success: renewing
      ? `${name} renewed until ${until}.`
      : `${name} can approve payments on ${chainName(wallet.chain_id)} until ${until}.`,
  }
}

/**
 * The devices that may approve the assistant's payments from this wallet: a passkey each, up to
 * `max_keys`. The owner signs every change, on this network only; a device is added separately on
 * each network's wallet. Adding or renewing one loosens the wallet, so it asks first; revoking is
 * one click.
 */
export function DevicesPanel(props: ControlPanelProps) {
  const { wallet, tx, locked } = props
  const { session } = wallet
  const passkeys = usePasskeys()
  const deviceIds = useDevicePasskeyIds()
  // The device just added here, offered on the other networks' wallets.
  const [added, setAdded] = useState<Passkey | null>(null)

  const devicePasskey = passkeys.data?.find(passkey => deviceIds.includes(passkey.credential_id)) ?? null
  const onWallet = new Set(session.keys.map(key => key.key_id))
  const live = session.keys.filter(key => key.active).length
  // A new key needs a free place. The wallet drops expired keys to make one, never live ones.
  const full = live >= session.max_keys
  const others = (passkeys.data ?? []).filter(
    passkey => !onWallet.has(passkey.key_id) && passkey.key_id !== devicePasskey?.key_id,
  )
  const hereOnWallet = devicePasskey !== null && onWallet.has(devicePasskey.key_id)

  return (
    <GlassPanel bordered header={<h2>Devices</h2>} className="mf-card">
      <Text muted size="sm" className="mf-devices-intro">
        {session.keys.length === 0
          ? "No device can approve payments from this wallet yet, so the assistant can't send anything. "
          : `${live} of ${session.max_keys} devices can approve payments. `}
        Each device approves the assistant's payments with its own passkey: Face ID, a fingerprint or its PIN.
      </Text>
      {session.keys.length + others.length > 0 && (
        <ul className="mf-device-list" aria-label="Devices">
          {session.keys.map(key => (
            <li key={key.key_id}>
              <DeviceRow {...props} sessionKey={key} here={key.credential_id !== null && deviceIds.includes(key.credential_id)} />
            </li>
          ))}
          {others.map(passkey => (
            <li key={passkey.key_id}>
              <OtherDeviceRow {...props} passkey={passkey} full={full} />
            </li>
          ))}
        </ul>
      )}
      {passkeys.isError && (
        <QueryError what="your devices" error={passkeys.error} onRetry={() => void passkeys.refetch()} className="mf-settings-note" />
      )}
      {passkeys.isSuccess && !hereOnWallet && (
        <AddThisDevice {...props} devicePasskey={devicePasskey} passkeys={passkeys.data} full={full} onAdded={setAdded} />
      )}
      {added && <AlsoAdd passkey={added} wallet={wallet} disabled={locked || tx.busy} />}
      {session.keys.length > 0 && <RevokeAll {...props} />}
    </GlassPanel>
  )
}

/** One key the wallet trusts: its device, how long it lasts, and Renew, Rename and Revoke. */
function DeviceRow({ wallet, tx, locked, start, ask, sessionKey, here }: ControlPanelProps & { sessionKey: SessionKey; here: boolean }) {
  const now = useNow(60_000)
  const [renaming, setRenaming] = useState(false)
  const { key_id: keyId } = sessionKey
  const name = sessionKey.device_name ?? 'Unknown device'
  // A key added outside Mitfah: the server can't ask it to sign, or renew it as one of the account's.
  const known = sessionKey.credential_id !== null
  const until = sessionKey.valid_until * 1_000
  const state = !sessionKey.active ? 'expired' : sessionKey.needs_renewal ? 'expiring' : 'active'
  const renewKey = `device-renew:${keyId}`
  const revokeKey = `device-revoke:${keyId}`

  const help = !known
    ? "Added to this wallet outside Mitfah, so it can't approve the assistant's payments here. Revoke it if you don't recognise it."
    : {
        active: `Approves payments until ${formatDate(until)}.`,
        expiring: `Stops approving payments in ${formatTimeLeft(until - now)}. Renew it to keep it working.`,
        expired: `Stopped approving payments on ${formatDate(until)}. Renew it to use it again.`,
      }[state]

  const renew = () =>
    ask({
      title: `Renew ${name}?`,
      body: <Text>{grantText(wallet, name, true).text}</Text>,
      confirmLabel: 'Renew',
      request: grantRequest(wallet, keyId, name, renewKey, true),
    })

  return (
    <ControlRow
      title={name}
      status={
        <>
          {here && <StatusTag>This device</StatusTag>}
          {state === 'active' && <StatusTag tone="success">Active</StatusTag>}
          {state === 'expiring' && <StatusTag tone="warning">Expires soon</StatusTag>}
          {state === 'expired' && <StatusTag>Expired</StatusTag>}
        </>
      }
      help={help}
      action={
        <div className="mf-control-buttons">
          {known && (
            <TxButton
              tx={tx}
              txKey={renewKey}
              size="sm"
              appearance="ghost"
              color="orange"
              disabled={locked}
              aria-label={`Renew ${name}`}
              onClick={renew}
            >
              Renew
            </TxButton>
          )}
          {known && (
            <Button size="sm" appearance="subtle" aria-label={`Rename ${name}`} onClick={() => setRenaming(true)}>
              Rename
            </Button>
          )}
          <TxButton
            tx={tx}
            txKey={revokeKey}
            size="sm"
            appearance="ghost"
            color="red"
            disabled={locked}
            aria-label={`Revoke ${name}`}
            onClick={() =>
              start({
                key: revokeKey,
                action: { kind: 'session', action: 'remove', keyId },
                success: `${name} can no longer approve payments on ${chainName(wallet.chain_id)}.`,
              })
            }
          >
            Revoke
          </TxButton>
        </div>
      }
    >
      {renaming && <RenameForm keyId={keyId} current={name} onDone={() => setRenaming(false)} />}
      <TxStatus tx={tx} txKey={renewKey} />
      <TxStatus tx={tx} txKey={revokeKey} />
    </ControlRow>
  )
}

/**
 * Renames a device. Names belong to the account's passkeys, not to one wallet, so every network's
 * list shows the new one; the wallets already read are changed in place.
 */
function RenameForm({ keyId, current, onDone }: { keyId: string; current: string; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState(current)
  const rename = useMutation({ mutationFn: (name: string) => renamePasskey(keyId, name) })
  const problem = deviceNameProblem(value)
  const id = `rename-${keyId}`

  const save = () => {
    if (problem) return
    if (value.trim() === current) return onDone()
    rename.mutate(value.trim(), {
      onSuccess: saved => {
        queryClient.setQueriesData<WalletState | null>(
          { queryKey: ['wallet'], predicate: query => query.queryKey.length === 2 },
          wallet =>
            wallet && {
              ...wallet,
              session: {
                ...wallet.session,
                keys: wallet.session.keys.map(key =>
                  key.key_id === saved.key_id ? { ...key, device_name: saved.device_name } : key,
                ),
              },
            },
        )
        void queryClient.invalidateQueries({ queryKey: PASSKEYS_KEY })
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
            maxLength={DEVICE_NAME_MAX}
            autoComplete="off"
            onChange={next => {
              setValue(next)
              rename.reset()
            }}
            aria-describedby={`${id}-help-text`}
            aria-invalid={!!problem}
          />
          <Button type="submit" size="sm" appearance="primary" loading={rename.isPending} disabled={!!problem}>
            Save
          </Button>
          <Button size="sm" appearance="subtle" disabled={rename.isPending} onClick={onDone}>
            Cancel
          </Button>
        </div>
        {problem ? (
          <Form.Text className="mf-error-text">{problem}</Form.Text>
        ) : rename.isError ? (
          <Form.Text className="mf-error-text">Couldn't rename it: {errorText(rename.error)}</Form.Text>
        ) : (
          <Form.Text>The name shows wherever this device's passkey does, on every network.</Form.Text>
        )}
      </Form.Group>
    </Form>
  )
}

/**
 * One of the account's passkeys that this wallet doesn't trust yet. It isn't called "your device":
 * anyone signed in as the account can register a passkey (THREAT_MODEL 4.11), so the row says when
 * it was registered, the Add dialog asks the user to add it only if they made it, and Remove forgets
 * one they don't recognise.
 */
function OtherDeviceRow({ wallet, tx, locked, ask, passkey, full }: ControlPanelProps & { passkey: Passkey; full: boolean }) {
  const queryClient = useQueryClient()
  const remove = useMutation({
    mutationFn: () => deletePasskey(passkey.key_id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PASSKEYS_KEY }),
  })
  const key = `device-add:${passkey.key_id}`
  const name = passkey.device_name
  const registered = formatDate(passkey.created_at * 1_000)
  return (
    <ControlRow
      title={name}
      status={<StatusTag>Not on this wallet</StatusTag>}
      help={
        full
          ? `This wallet already trusts ${wallet.session.max_keys} devices, the most it can. Revoke one to add ${name}.`
          : `A passkey registered to your account on ${registered}. Add it to let it approve payments on ${chainName(wallet.chain_id)}, or remove it if you don't recognise it.`
      }
      action={
        <div className="mf-control-buttons">
          <TxButton
            tx={tx}
            txKey={key}
            size="sm"
            appearance="ghost"
            color="orange"
            disabled={locked || full || remove.isPending}
            aria-label={`Add ${name}`}
            onClick={() =>
              ask({
                title: `Add ${name}?`,
                body: (
                  <>
                    <Text>{grantText(wallet, name, false).text}</Text>
                    <Text>
                      Only add it if you registered this passkey yourself, on {registered}. If you don't recognise it,
                      remove it instead.
                    </Text>
                  </>
                ),
                confirmLabel: 'Add device',
                request: grantRequest(wallet, passkey.key_id, name, key, false),
              })
            }
          >
            Add
          </TxButton>
          <Button
            size="sm"
            appearance="subtle"
            aria-label={`Remove ${name}`}
            loading={remove.isPending}
            disabled={tx.busy}
            onClick={() => remove.mutate()}
          >
            Remove
          </Button>
        </div>
      }
    >
      {remove.isError && <Text className="mf-error-text">Couldn't remove it: {errorText(remove.error)}</Text>}
      <TxStatus tx={tx} txKey={key} />
    </ControlRow>
  )
}

const ADD_KEY = 'device-add'

/**
 * Lets this device approve payments from this wallet: a passkey is made on it first if it has none,
 * inside the same tap that confirms (a browser opens its passkey prompt only from a tap), and the
 * owner's wallet then signs the grant on this network.
 */
function AddThisDevice({
  wallet,
  tx,
  locked,
  ask,
  devicePasskey,
  passkeys,
  full,
  onAdded,
}: ControlPanelProps & {
  devicePasskey: Passkey | null
  passkeys: Passkey[]
  full: boolean
  onAdded: (passkey: Passkey) => void
}) {
  const [name, setName] = useState(defaultDeviceName)
  // The user means to add it: from then on the options to make a passkey with are kept ready.
  const [wanted, setWanted] = useState(false)
  const needsPasskey = devicePasskey === null
  useRegistrationOptions(wanted && needsPasskey)
  const supported = passkeysSupported()
  const nameProblem = needsPasskey ? deviceNameProblem(name) : null
  const network = chainName(wallet.chain_id)

  /** The owner's transaction, once there is a passkey; offers the other networks when it lands. */
  const grant = (passkey: Passkey) =>
    void tx.run(grantRequest(wallet, passkey.key_id, passkey.device_name, ADD_KEY, false)).then(confirmed => {
      if (confirmed) onAdded(passkey)
    })
  const maker = useDevicePasskeyMaker(grant)
  const preparing = maker.making || maker.finding

  const add = () => {
    if (nameProblem) return
    setWanted(true)
    const who = devicePasskey ? devicePasskey.device_name : `This device (“${name.trim()}”)`
    ask({
      title: 'Add this device?',
      body: (
        <Text>
          {needsPasskey && 'Your browser will first ask you to create a passkey. '}
          {grantText(wallet, who, false).text}
        </Text>
      ),
      confirmLabel: needsPasskey ? 'Create passkey' : 'Add device',
      // The confirming tap: the passkey prompt has to open within it.
      run: () => (devicePasskey ? grant(devicePasskey) : maker.make(name)),
    })
  }

  const cannot = needsPasskey && !supported
  let help
  if (cannot) help = "This browser can't make passkeys, so it can't approve payments. Add a phone or computer that can."
  else if (full)
    help = `This wallet already trusts ${wallet.session.max_keys} devices, the most it can. Revoke one to add this device.`
  else if (devicePasskey)
    help = `This device's passkey, “${devicePasskey.device_name}”, can approve payments here once your owner wallet adds it on ${network}.`
  else help = `Create a passkey on this device, then your owner wallet adds it to this wallet on ${network}.`

  return (
    <ControlRow
      title="Add this device"
      status={full ? <StatusTag tone="warning">Wallet full</StatusTag> : undefined}
      help={help}
      action={
        <TxButton
          tx={tx}
          txKey={ADD_KEY}
          appearance="ghost"
          color="orange"
          disabled={locked || full || cannot || preparing || !!nameProblem}
          onClick={add}
        >
          Add this device
        </TxButton>
      }
    >
      {needsPasskey && !cannot && !full && (
        <Form className="mf-control-form" onSubmit={add}>
          <Form.Group controlId="device-name">
            <Form.Label>Name for this device</Form.Label>
            <Input
              id="device-name"
              className="mf-device-name"
              value={name}
              maxLength={DEVICE_NAME_MAX}
              autoComplete="off"
              disabled={preparing}
              onChange={setName}
              aria-describedby="device-name-help-text"
              aria-invalid={!!nameProblem}
            />
            {nameProblem ? (
              <Form.Text className="mf-error-text">{nameProblem}</Form.Text>
            ) : (
              <Form.Text>So you can tell your devices apart. You can rename it later.</Form.Text>
            )}
          </Form.Group>
        </Form>
      )}
      {maker.making && <Loader className="mf-tx-status" content="Follow your browser to create the passkey…" />}
      {maker.finding && <Loader className="mf-tx-status" content="Choose this device's passkey…" />}
      {maker.error && <PasskeyProblem error={maker.error} onUseExisting={() => maker.findExisting(passkeys)} />}
      <TxStatus tx={tx} txKey={ADD_KEY} />
    </ControlRow>
  )
}

/**
 * After a device is added here, the user's other wallets, each with its own Add: one signature on
 * each network, the wallet switched there first. One at a time, as a browser wallet handles one
 * request at a time.
 */
function AlsoAdd({ passkey, wallet, disabled }: { passkey: Passkey; wallet: WalletState; disabled: boolean }) {
  const { walletChains } = useSelectedChain()
  const [running, setRunning] = useState<number | null>(null)
  const chains = walletChains.filter(chainId => chainId !== wallet.chain_id)
  if (chains.length === 0) return null

  return (
    <section className="mf-also-add" aria-label="Add on your other wallets">
      <Text>
        Also let “{passkey.device_name}” approve payments from your other wallets? Each needs one signature in your
        wallet, on its own network.
      </Text>
      <ul className="mf-token-list">
        {chains.map(chainId => (
          <li key={chainId}>
            <AlsoAddOn
              chainId={chainId}
              passkey={passkey}
              disabled={disabled || (running !== null && running !== chainId)}
              onRun={setRunning}
            />
          </li>
        ))}
      </ul>
    </section>
  )
}

function AlsoAddOn({
  chainId,
  passkey,
  disabled,
  onRun,
}: {
  chainId: number
  passkey: Passkey
  disabled: boolean
  onRun: (chainId: number | null) => void
}) {
  const wallet = useQuery({ queryKey: ['wallet', chainId], queryFn: () => fetchWallet(chainId), staleTime: 15_000 })
  if (wallet.data) return <AlsoAddOnWallet wallet={wallet.data} passkey={passkey} disabled={disabled} onRun={onRun} />
  return (
    <div className="mf-token-row">
      <span className="mf-token">{chainName(chainId)}</span>
      {wallet.isPending ? (
        <Loader size="xs" content="Reading the wallet…" />
      ) : (
        <Button appearance="link" size="sm" onClick={() => void wallet.refetch()}>
          Couldn't read this wallet. Try again
        </Button>
      )}
    </div>
  )
}

/** Adds the device on one other network's wallet, through that wallet's own owner transaction. */
function AlsoAddOnWallet({
  wallet,
  passkey,
  disabled,
  onRun,
}: {
  wallet: WalletState
  passkey: Passkey
  disabled: boolean
  onRun: (chainId: number | null) => void
}) {
  const tx = useOwnerAction(wallet.chain_id, wallet.owner)
  const network = chainName(wallet.chain_id)
  const key = `${ADD_KEY}:${passkey.key_id}`
  const there = wallet.session.keys.some(sessionKey => sessionKey.key_id === passkey.key_id && sessionKey.active)

  const add = () => {
    onRun(wallet.chain_id)
    void tx.run(grantRequest(wallet, passkey.key_id, passkey.device_name, key, false)).finally(() => onRun(null))
  }

  return (
    <>
      <div className="mf-token-row">
        <span className="mf-token">{network}</span>
        {there ? (
          <StatusTag tone="success">Added</StatusTag>
        ) : (
          <TxButton tx={tx} txKey={key} size="sm" appearance="ghost" color="orange" disabled={disabled} onClick={add}>
            Add on {network}
          </TxButton>
        )}
      </div>
      <TxStatus tx={tx} txKey={key} />
    </>
  )
}

/** The assistant's brake: every device stops approving payments at once, in one click. */
function RevokeAll({ wallet, tx, locked, start }: ControlPanelProps) {
  const key = 'device-revoke-all'
  return (
    <ControlRow
      title="Revoke every device"
      help="Every device stops approving payments at once, so the assistant can't send anything until you add one again. Your owner wallet keeps full control."
      action={
        <TxButton
          tx={tx}
          txKey={key}
          appearance="primary"
          color="red"
          disabled={locked}
          onClick={() =>
            start({
              key,
              action: { kind: 'session', action: 'remove_all' },
              success: `Every device revoked. The assistant can't send anything on ${chainName(wallet.chain_id)} until you add one again.`,
            })
          }
        >
          Revoke all
        </TxButton>
      }
    >
      <TxStatus tx={tx} txKey={key} />
    </ControlRow>
  )
}
