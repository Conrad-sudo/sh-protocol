import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { useConnection, useSwitchChain } from 'wagmi'
import {
  Button,
  Checkbox,
  CheckPicker,
  Form,
  Input,
  Loader,
  Message,
  NumberInput,
  Placeholder,
  Progress,
  RadioTile,
  RadioTileGroup,
  SegmentedControl,
  Steps,
  Text,
  useToaster,
} from 'rsuite'
import type { Chain, Passkey, Token } from '../../api/types'
import { useSelectedChain } from '../../chain/useSelectedChain'
import { AddressText } from '../../components/AddressText'
import { AmountInput } from '../../components/AmountInput'
import { Explained, FieldError, FieldLabel } from '../../components/Explained'
import { GlassPanel } from '../../components/Glass'
import { PageHeader } from '../../components/PageHeader'
import { PasskeyProblem } from '../../components/PasskeyProblem'
import { QueryError } from '../../components/QueryError'
import { StatusTag } from '../../components/StatusTag'
import { AccountMismatchBanner } from '../../components/wallet/AccountMismatchBanner'
import { ConnectDialog } from '../../components/wallet/ConnectDialog'
import { NetworkBanner } from '../../components/wallet/NetworkBanner'
import { useDeploy, type DeployPhase } from '../../hooks/useDeploy'
import { useMe } from '../../hooks/useMe'
import { useDevicePasskey, useDevicePasskeyMaker, usePasskeys, useRegistrationOptions } from '../../hooks/usePasskeys'
import { useTokens } from '../../hooks/useTokens'
import { useLayoutMode } from '../../layouts/useLayoutMode'
import { ASSISTANT_KEY_DAYS, ASSISTANT_KEY_TTL_SECS } from '../../lib/assistant'
import { describedBy } from '../../lib/fields'
import { formatUsd, formatWindow, isValidAmount } from '../../lib/format'
import { DEVICE_NAME_MAX, defaultDeviceName, deviceNameProblem, passkeysSupported } from '../../lib/passkey'
import { WINDOW_CHOICES } from '../../lib/spending'
import { chainName, isSupportedChainId } from '../../wallet/chains'
import { DeployProgress } from './DeployProgress'

const STEP_TITLES = [
  'Choose network',
  'Set your limit',
  'Prefund your wallet',
  'Create your passkey',
  'Review and create',
] as const

type WizardStep = 'network' | 'limits' | 'fund' | 'passkey' | 'review'
const WIZARD_INDEX: Record<WizardStep, number> = { network: 0, limits: 1, fund: 2, passkey: 3, review: 4 }

interface Draft {
  chainId: number | null
  /** Asked for, and sent, only when something counts toward it (see `counts`). */
  limitUsd: number | null
  windowSecs: number
  /** Whether the native token counts toward the limit. Its wrapped form goes with it. Off to begin with. */
  countNative: boolean
  /** The tokens the user ticked. Nothing else counts. */
  tickers: string[]
  prefund: string | null
  /**
   * The passkey the new wallet trusts from the start, so this device can approve the assistant's
   * payments at once; null when the user skipped it (the wallet is then theirs alone until a device
   * is added in Controls).
   */
  passkey: Passkey | null
}

const BUSY: DeployPhase[] = ['preparing', 'signing', 'confirming']

/**
 * Creating a Mitfah wallet: choose where it lives, how much the assistant may spend and what to put
 * in it, then sign the deploy. Signing in already proved which wallet is the user's; that wallet
 * signs the deploy, so it owns the result. If the browser wallet has since disconnected or moved to
 * another account, the page asks for it back first.
 */
/**
 * The tokens the user ticked, from this network's list. The wrapped native token is never among
 * them: it counts exactly when the native token does, and the API puts it on the list itself then.
 */
function pickedTokens(tokens: Token[], tickers: string[]) {
  return tokens.filter(t => !t.follows_native && tickers.includes(t.ticker))
}

/**
 * Whether the new wallet counts anything toward a spending limit. If it doesn't, there is no limit
 * to set: the step leaves the fields out and the wallet is made with none.
 */
function counts(draft: Pick<Draft, 'countNative' | 'tickers'>, tokens: Token[]) {
  return draft.countNative || pickedTokens(tokens, draft.tickers).length > 0
}

export function OnboardingPage() {
  const { address, chainId: walletChainId, isConnected } = useConnection()
  const { data: me } = useMe()
  const { chains, walletChains, setChainId } = useSelectedChain()
  const { mutateAsync: switchChainAsync } = useSwitchChain()
  const navigate = useNavigate()
  const toaster = useToaster()
  const mode = useLayoutMode()
  const [connectOpen, setConnectOpen] = useState(false)
  const [step, setStep] = useState<WizardStep>('network')
  const [draft, setDraft] = useState<Draft>({
    chainId: null,
    limitUsd: 100,
    windowSecs: 86_400,
    countNative: false,
    tickers: [],
    prefund: null,
    passkey: null,
  })

  const { state: deployState, deploy, resume, abandon, startOver } = useDeploy(result => {
    setChainId(result.chainId)
    const network = chainName(result.chainId)
    let text = `Your wallet is ready on ${network}. You can now disconnect your browser wallet: your passkey approves the assistant's payments. Reconnect it when you want to change your limits or withdraw.`
    if (result.keyId === null) {
      text = `Your wallet is ready on ${network}. Add a device in Controls so the assistant can send payments.`
    } else if (!result.assistantReady) {
      text = "Your wallet was created, but it doesn't trust your passkey yet. Add this device in Controls."
    }
    toaster.push(
      <Message type={result.keyId !== null && result.assistantReady ? 'success' : 'warning'} showIcon closable>
        {text}
      </Message>,
      { placement: 'topCenter', duration: 15_000 },
    )
    navigate('/dashboard', { replace: true })
  })

  const freeChains = chains.filter(chain => !walletChains.includes(chain.chain_id))
  const chainId =
    draft.chainId ??
    (freeChains.find(chain => chain.chain_id === walletChainId) ?? freeChains[0])?.chain_id ??
    null
  const chain = chains.find(c => c.chain_id === chainId)
  const prefund = draft.prefund ?? (chain?.fork ? '1' : '0.05')

  const owner = me?.owner_addr ?? null
  const ownerConnected = Boolean(address && owner && owner.toLowerCase() === address.toLowerCase())
  const deploying = BUSY.includes(deployState.phase) || deployState.canResume === true
  // A deploy picked up after a reload: the form values are gone, so only its progress — and, if it
  // failed, why — can be shown until the user starts over.
  const resumed = deployState.phase !== 'idle' && step !== 'review'
  const index = deploying || resumed ? WIZARD_INDEX.review : WIZARD_INDEX[step]

  const goToLimits = async () => {
    setDraft(d => ({ ...d, chainId }))
    setStep('limits')
    if (chainId !== null && walletChainId !== chainId && isSupportedChainId(chainId)) {
      // Best effort: the review step shows a banner if the wallet is still elsewhere.
      await switchChainAsync({ chainId }).catch(() => undefined)
    }
  }

  const createWallet = async (tokens: Token[]) => {
    const limited = counts(draft, tokens)
    if (!address || chainId === null || (limited && draft.limitUsd === null)) return
    await deploy({
      chain_id: chainId,
      deployer: address,
      // No limit for a wallet that counts nothing: the API takes 0 only then.
      daily_limit_usd: limited ? (draft.limitUsd ?? 0) : 0,
      window_secs: draft.windowSecs,
      watched_tokens: pickedTokens(tokens, draft.tickers),
      count_native: draft.countNative,
      prefund_eth: prefund,
      key_id: draft.passkey?.key_id ?? null,
      session_ttl_secs: ASSISTANT_KEY_TTL_SECS,
    })
  }

  const deployChainId = deployState.chainId ?? chainId ?? 0
  const progress = (
    <DeployProgress
      state={deployState}
      chainId={deployChainId}
      onRetry={startOver}
      onResume={resume}
      onAbandon={abandon}
    />
  )

  let body
  if (resumed) {
    body = <StepBody title="Creating your wallet">{progress}</StepBody>
  } else if (deployState.phase !== 'idle') {
    body = (
      <ReviewStep
        draft={{ ...draft, chainId, prefund }}
        chain={chain}
        owner={address}
        busy={deploying}
        onBack={() => setStep('passkey')}
        onCreate={tokens => void createWallet(tokens)}
        progress={progress}
      />
    )
  } else if (!isConnected || !address) {
    body = (
      <StepBody
        title="Connect the wallet you signed in with"
        intro={
          owner && (
            <>
              <AddressText address={owner} /> will own your Mitfah wallet, so it signs its creation.
            </>
          )
        }
      >
        <Button appearance="primary" className="mf-step-action" onClick={() => setConnectOpen(true)}>
          Connect wallet
        </Button>
        <ConnectDialog open={connectOpen} onClose={() => setConnectOpen(false)} />
      </StepBody>
    )
  } else if (!ownerConnected) {
    body = (
      <StepBody title="Switch to the wallet you signed in with">
        {owner && <AccountMismatchBanner ownerAddr={owner} connected={address} />}
      </StepBody>
    )
  } else if (step === 'network') {
    body = (
      <StepBody title="Where should your wallet live?">
        {freeChains.length === 0 && chains.length > 0 ? (
          <Message type="info" showIcon className="mf-settings-note">
            You already have a wallet on every network this service supports.
          </Message>
        ) : (
          <RadioTileGroup
            aria-label="Network"
            value={chainId}
            onChange={value => setDraft(d => ({ ...d, chainId: Number(value) }))}
            className="mf-tiles"
          >
            {chains.map(c => {
              const taken = walletChains.includes(c.chain_id)
              return (
                <RadioTile
                  key={c.chain_id}
                  value={c.chain_id}
                  disabled={taken}
                  label={
                    <span className="mf-tile-label">
                      {chainName(c.chain_id)}
                      {taken && <StatusTag tone="success">Wallet exists</StatusTag>}
                      {!taken && c.fork && <StatusTag tone="neutral">Local test network</StatusTag>}
                    </span>
                  }
                >
                  {taken
                    ? 'You already have a wallet here.'
                    : `Network fees are paid in ${c.native_ticker ?? 'the native token'}.`}
                </RadioTile>
              )
            })}
          </RadioTileGroup>
        )}
        <Button
          appearance="primary"
          className="mf-step-action"
          disabled={chainId === null}
          onClick={() => void goToLimits()}
        >
          Continue
        </Button>
      </StepBody>
    )
  } else if (step === 'limits' && chainId !== null) {
    body = (
      <LimitsStep
        chainId={chainId}
        nativeTicker={chain?.native_ticker ?? 'the native token'}
        draft={draft}
        onChange={patch => setDraft(d => ({ ...d, ...patch }))}
        onBack={() => setStep('network')}
        onNext={() => setStep('fund')}
      />
    )
  } else if (step === 'fund') {
    const valid = isValidAmount(prefund)
    body = (
      <StepBody
        title="Prefund your Mitfah smart wallet"
        intro={`Its starting balance. The assistant pays for everything from it, network fees included${draft.countNative || draft.tickers.length > 0 ? ', within your limit' : ''}. Skip to start empty; add funds any time.`}
      >
        <Form fluid className="mf-auth-form">
          <Form.Group controlId="prefund">
            <Form.Label>Amount to send now</Form.Label>
            <AmountInput
              id="prefund"
              value={prefund}
              unit={chain?.native_ticker ?? undefined}
              onChange={value => setDraft(d => ({ ...d, prefund: value }))}
            />
            {!valid && <Form.Text className="mf-error-text">Enter an amount like 0.05.</Form.Text>}
          </Form.Group>
        </Form>
        <div className="mf-step-nav">
          <Button appearance="subtle" onClick={() => setStep('limits')}>
            Back
          </Button>
          <div className="mf-step-nav-end">
            <Button
              appearance="link"
              onClick={() => {
                setDraft(d => ({ ...d, prefund: '0' }))
                setStep('passkey')
              }}
            >
              Skip for now
            </Button>
            <Button appearance="primary" disabled={!valid} onClick={() => setStep('passkey')}>
              Continue
            </Button>
          </div>
        </div>
      </StepBody>
    )
  } else if (step === 'passkey' && chainId !== null) {
    body = (
      <PasskeyStep
        chainId={chainId}
        onBack={() => setStep('fund')}
        onDone={passkey => {
          setDraft(d => ({ ...d, passkey }))
          setStep('review')
        }}
      />
    )
  } else {
    body = (
      <ReviewStep
        draft={{ ...draft, chainId, prefund }}
        chain={chain}
        owner={address}
        busy={false}
        onBack={() => setStep('passkey')}
        onCreate={tokens => void createWallet(tokens)}
        progress={null}
      />
    )
  }

  return (
    <>
      <PageHeader title="Create your wallet" description="A few steps, then one signature in your wallet." />
      <GlassPanel bordered className="mf-onboarding">
        {mode === 'mobile' ? (
          <div className="mf-step-counter">
            <Text size="sm" muted>
              Step {index + 1} of {STEP_TITLES.length}
            </Text>
            <Text weight="semibold">{STEP_TITLES[index]}</Text>
            {/* The line repeats the step count above it, so it needs a name of its own. */}
            <Progress
              percent={((index + 1) / STEP_TITLES.length) * 100}
              showInfo={false}
              aria-label={`Step ${index + 1} of ${STEP_TITLES.length}`}
            />
          </div>
        ) : (
          <Steps current={index} small className="mf-steps">
            {STEP_TITLES.map(title => (
              <Steps.Item key={title} title={title} />
            ))}
          </Steps>
        )}
        <div className="mf-step-body">{body}</div>
      </GlassPanel>
    </>
  )
}

/** A step: its title with what it's for beside it, then its fields. */
function StepBody({ title, intro, children }: { title: string; intro?: ReactNode; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <Explained level={2} title={title} titleClassName="mf-step-title">
        {intro}
      </Explained>
      {children}
    </section>
  )
}

function StepNav({ onBack, onNext, nextDisabled }: { onBack: () => void; onNext: () => void; nextDisabled?: boolean }) {
  return (
    <div className="mf-step-nav">
      <Button appearance="subtle" onClick={onBack}>
        Back
      </Button>
      <Button appearance="primary" disabled={nextDisabled} onClick={onNext}>
        Continue
      </Button>
    </div>
  )
}

function LimitsStep({
  chainId,
  nativeTicker,
  draft,
  onChange,
  onBack,
  onNext,
}: {
  chainId: number
  nativeTicker: string
  draft: Draft
  onChange: (patch: Partial<Draft>) => void
  onBack: () => void
  onNext: () => void
}) {
  const tokens = useTokens(chainId)
  const wrapped = tokens.data?.filter(t => t.follows_native).map(t => t.ticker.toUpperCase()) ?? []
  // The wrapped native token isn't offered: it counts with the native one, so there's nothing to choose.
  const choosable = tokens.data?.filter(t => !t.follows_native) ?? []
  // A token ticked on another network, before going back to change it, isn't on this list.
  const selected = draft.tickers.filter(ticker => choosable.some(t => t.ticker === ticker))
  // Nothing counted means no limit to set: the fields below appear with the first thing that counts.
  const limited = draft.countNative || selected.length > 0
  const limitValid = draft.limitUsd !== null && Number.isInteger(draft.limitUsd) && draft.limitUsd >= 1

  return (
    <StepBody title="How much may the assistant spend?">
      <Form fluid className="mf-auth-form">
        <Form.Group controlId="count-native">
          <Checkbox
            id="count-native"
            checked={draft.countNative}
            onChange={(_value, checked) => onChange({ countNative: checked })}
          >
            Count {nativeTicker} toward a spending limit
          </Checkbox>
          {wrapped.length > 0 && <Form.Text>{wrapped.join(' and ')} counts with it.</Form.Text>}
        </Form.Group>

        <Form.Group controlId="tokens">
          <FieldLabel help={choosable.length > 0 && 'Unticked tokens can be moved without limit.'}>
            Tokens that count toward the limit
          </FieldLabel>
          {tokens.isPending ? (
            <Placeholder.Paragraph rows={2} active />
          ) : tokens.isError ? (
            <QueryError what="the token list" onRetry={() => void tokens.refetch()} />
          ) : (
            <CheckPicker
              id="tokens"
              // A <label for> can't name the picker's div, so point at the label RSuite rendered.
              aria-labelledby="tokens-label"
              placeholder={choosable.length > 0 ? 'Choose tokens' : 'No other tokens on this network'}
              disabled={choosable.length === 0}
              block
              // A long list opens upward rather than off the screen, and is short enough to fit either way.
              placement="autoVerticalStart"
              listboxMaxHeight={240}
              // One click on "clear" would take every token out of the limit.
              cleanable={false}
              searchable={choosable.length > 8}
              data={choosable.map(token => ({ value: token.ticker, label: token.ticker.toUpperCase() }))}
              value={selected}
              onChange={value => onChange({ tickers: value })}
            />
          )}
        </Form.Group>

        {limited ? (
          <>
            <Form.Group controlId="limit">
              <FieldLabel help="The most the assistant can move out per period, in US dollars.">Spending limit</FieldLabel>
              <NumberInput
                id="limit"
                prefix="$"
                min={1}
                step={10}
                value={draft.limitUsd ?? ''}
                onChange={value => {
                  const n = Number(value)
                  onChange({ limitUsd: value === '' || value === null || Number.isNaN(n) ? null : Math.floor(n) })
                }}
              />
              {!limitValid && <FieldError>Enter a whole number of dollars, at least $1.</FieldError>}
            </Form.Group>

            <Form.Group controlId="window">
              <FieldLabel help="How often the limit refills.">Period</FieldLabel>
              <SegmentedControl
                aria-label="Period"
                data={WINDOW_CHOICES}
                value={draft.windowSecs}
                onChange={value => onChange({ windowSecs: Number(value) })}
              />
            </Form.Group>
          </>
        ) : (
          <Text muted>Nothing counts yet, so there's no limit. You can set one later in Controls.</Text>
        )}
      </Form>
      <StepNav onBack={onBack} onNext={onNext} nextDisabled={(limited && !limitValid) || tokens.isPending} />
    </StepBody>
  )
}

function ReviewStep({
  draft,
  chain,
  owner,
  busy,
  onBack,
  onCreate,
  progress,
}: {
  draft: Draft & { prefund: string }
  chain: Chain | undefined
  owner: string | undefined
  busy: boolean
  onBack: () => void
  onCreate: (tokens: Token[]) => void
  progress: ReactNode
}) {
  const { chainId: walletChainId } = useConnection()
  const tokens = useTokens(draft.chainId ?? 0)
  const limited = counts(draft, tokens.data ?? [])
  // What counts, as it will be on the wallet: the native token with its wrapped form, then the picks.
  const counted = [
    ...(draft.countNative
      ? [
          chain?.native_ticker ?? 'Native token',
          ...(tokens.data ?? []).filter(t => t.follows_native).map(t => t.ticker.toUpperCase()),
        ]
      : []),
    ...pickedTokens(tokens.data ?? [], draft.tickers).map(t => t.ticker.toUpperCase()),
  ]
  const onRightNetwork = walletChainId === draft.chainId

  return (
    <StepBody title="Check the details">
      <dl className="mf-summary">
        <dt>Network</dt>
        <dd>{chainName(draft.chainId ?? undefined)}</dd>
        <dt>Owner</dt>
        <dd>{owner ? <AddressText address={owner} /> : '—'}</dd>
        <dt>Spending limit</dt>
        {limited ? (
          <dd className="mf-num">
            {draft.limitUsd !== null ? formatUsd(draft.limitUsd) : '—'} every {formatWindow(draft.windowSecs)}
          </dd>
        ) : (
          <dd>None</dd>
        )}
        <dt>Counts toward the limit</dt>
        <dd>{counted.length > 0 ? counted.join(', ') : 'Nothing'}</dd>
        <dt>Wallet prefund</dt>
        {Number(draft.prefund) > 0 ? (
          <dd className="mf-num">
            {draft.prefund} {chain?.native_ticker}
          </dd>
        ) : (
          <dd>None: add funds from the dashboard any time</dd>
        )}
        <dt>Assistant's access</dt>
        <dd>
          {draft.passkey
            ? `${ASSISTANT_KEY_DAYS} days, approved with the passkey on “${draft.passkey.device_name}”; renewable any time in Controls`
            : "None yet: the assistant can't send anything until you add a device in Controls"}
        </dd>
      </dl>
      {draft.chainId !== null && <NetworkBanner chainId={draft.chainId} fork={chain?.fork} />}
      {progress}
      {!busy && (
        <div className="mf-step-nav">
          <Button appearance="subtle" onClick={onBack}>
            Back
          </Button>
          <Button
            appearance="primary"
            disabled={!onRightNetwork || tokens.isPending || !tokens.data}
            onClick={() => tokens.data && onCreate(tokens.data)}
          >
            Create wallet
          </Button>
        </div>
      )}
    </StepBody>
  )
}

/**
 * The passkey that approves the assistant's payments from the new wallet: this device's, made here
 * (the tap opens the browser's prompt), or the one it already has. It can be skipped: the wallet is
 * then the owner's alone until a device is added in Controls, which is also the way on for a
 * browser that can't make passkeys.
 */
function PasskeyStep({
  chainId,
  onBack,
  onDone,
}: {
  chainId: number
  onBack: () => void
  /** The passkey the wallet should trust, or null to create it without one. */
  onDone: (passkey: Passkey | null) => void
}) {
  const passkeys = usePasskeys()
  const devicePasskey = useDevicePasskey()
  const [name, setName] = useState(defaultDeviceName)
  const supported = passkeysSupported()
  const needsPasskey = devicePasskey === null
  // Fetched before the tap, so the tap itself can open the prompt.
  useRegistrationOptions(passkeys.isSuccess && needsPasskey && supported)
  const maker = useDevicePasskeyMaker(onDone)
  const problem = deviceNameProblem(name)
  const busy = maker.making || maker.finding

  const create = () => {
    if (problem || busy) return
    maker.make(name)
  }

  if (passkeys.isPending) {
    return (
      <StepBody title="Create your passkey">
        <Placeholder.Paragraph rows={3} active />
      </StepBody>
    )
  }
  if (devicePasskey) {
    return (
      <StepBody
        title="Your passkey"
        intro={`This device's passkey, “${devicePasskey.device_name}”, will approve each payment the assistant prepares on ${chainName(chainId)}.`}
      >
        <StepNav onBack={onBack} onNext={() => onDone(devicePasskey)} />
      </StepBody>
    )
  }
  return (
    <StepBody
      title="Create your passkey"
      intro="It approves each payment the assistant prepares, with Face ID, a fingerprint or this device's PIN. Skip, and the assistant can't send until you add a device in Controls."
    >
      {passkeys.isError && (
        <QueryError
          what="your passkeys"
          error={passkeys.error}
          onRetry={() => void passkeys.refetch()}
          className="mf-settings-note"
        />
      )}
      {supported ? (
        <Form fluid className="mf-auth-form" onSubmit={create}>
          <Form.Group controlId="device-name">
            <FieldLabel help="So you can tell your devices apart.">Name for this device</FieldLabel>
            <Input
              id="device-name"
              value={name}
              maxLength={DEVICE_NAME_MAX}
              autoComplete="off"
              disabled={busy}
              onChange={setName}
              aria-describedby={describedBy('device-name', problem)}
              aria-invalid={!!problem}
            />
            {problem && <FieldError>{problem}</FieldError>}
          </Form.Group>
        </Form>
      ) : (
        <Message type="warning" showIcon className="mf-settings-note">
          This browser can't make passkeys. Create the wallet without one, then add a phone or computer that can in
          Controls.
        </Message>
      )}
      {maker.making && <Loader className="mf-tx-status" content="Follow your browser to create the passkey…" />}
      {maker.finding && <Loader className="mf-tx-status" content="Choose this device's passkey…" />}
      {maker.error && (
        <PasskeyProblem
          error={maker.error}
          onUseExisting={() => maker.findExisting(passkeys.data ?? [])}
          className="mf-settings-note"
        />
      )}
      <div className="mf-step-nav">
        <Button appearance="subtle" disabled={busy} onClick={onBack}>
          Back
        </Button>
        <div className="mf-step-nav-end">
          <Button appearance="link" disabled={busy} onClick={() => onDone(null)}>
            Skip for now
          </Button>
          {supported && (
            <Button appearance="primary" loading={busy} disabled={!!problem || passkeys.isError} onClick={create}>
              Create passkey
            </Button>
          )}
        </div>
      </div>
    </StepBody>
  )
}
