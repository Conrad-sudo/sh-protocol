import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router'
import { useConnection, useSwitchChain } from 'wagmi'
import {
  Button,
  Checkbox,
  CheckboxGroup,
  Form,
  Message,
  NumberInput,
  Panel,
  Placeholder,
  Progress,
  RadioTile,
  RadioTileGroup,
  SegmentedControl,
  Steps,
  Text,
  useToaster,
} from 'rsuite'
import { fetchTokens } from '../../api/wallet'
import type { Chain, Token } from '../../api/types'
import { useSelectedChain } from '../../chain/useSelectedChain'
import { AddressText } from '../../components/AddressText'
import { AmountInput } from '../../components/AmountInput'
import { PageHeader } from '../../components/PageHeader'
import { StatusTag } from '../../components/StatusTag'
import { AccountMismatchBanner } from '../../components/wallet/AccountMismatchBanner'
import { ConnectDialog } from '../../components/wallet/ConnectDialog'
import { NetworkBanner } from '../../components/wallet/NetworkBanner'
import { SiweVerifyCard } from '../../components/wallet/SiweVerifyCard'
import { useDeploy, type DeployPhase } from '../../hooks/useDeploy'
import { useMe } from '../../hooks/useMe'
import { useLayoutMode } from '../../layouts/useLayoutMode'
import { formatUsd, formatWindow, isValidAmount } from '../../lib/format'
import { chainName, isSupportedChainId } from '../../wallet/chains'
import { DeployProgress } from './DeployProgress'

const STEP_TITLES = [
  'Connect wallet',
  'Verify ownership',
  'Choose network',
  'Set your limit',
  'Add gas funds',
  'Review and create',
] as const

const WINDOWS = [
  { label: '12 hours', value: 43_200 },
  { label: '24 hours', value: 86_400 },
  { label: '7 days', value: 604_800 },
]

type WizardStep = 'network' | 'limits' | 'fund' | 'review'
const WIZARD_INDEX: Record<WizardStep, number> = { network: 2, limits: 3, fund: 4, review: 5 }

interface Draft {
  chainId: number | null
  limitUsd: number | null
  windowSecs: number
  /** null until the user changes it: then every listed token is watched. */
  tickers: string[] | null
  prefund: string | null
}

const BUSY: DeployPhase[] = ['preparing', 'signing', 'confirming']

/**
 * Creating a Mitfah wallet: connect a browser wallet, prove it's yours, then choose where the wallet
 * lives and how much the assistant may spend. The user's own wallet signs the deploy, so it owns the
 * result.
 */
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
    tickers: null,
    prefund: null,
  })

  const { state: deployState, deploy, resume, reset } = useDeploy(result => {
    setChainId(result.chainId)
    toaster.push(
      <Message type={result.assistantReady ? 'success' : 'warning'} showIcon closable>
        {result.assistantReady
          ? `Your wallet is ready on ${chainName(result.chainId)}.`
          : 'Your wallet was created, but the assistant is not switched on yet. You can turn it on in Controls.'}
      </Message>,
      { placement: 'topCenter', duration: 5000 },
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

  const verified = Boolean(address && me?.owner_addr && me.owner_addr.toLowerCase() === address.toLowerCase())
  const deploying = BUSY.includes(deployState.phase) || deployState.canResume === true
  // A deploy picked up after a reload: the form values are gone, so only its progress — and, if it
  // failed, why — can be shown until the user starts over.
  const resumed = deployState.phase !== 'idle' && step !== 'review'
  let index: number
  if (deploying || resumed) index = 5
  else if (!isConnected || !address) index = 0
  else if (!verified) index = 1
  else index = WIZARD_INDEX[step]

  const goToLimits = async () => {
    setDraft(d => ({ ...d, chainId }))
    setStep('limits')
    if (chainId !== null && walletChainId !== chainId && isSupportedChainId(chainId)) {
      // Best effort: the review step shows a banner if the wallet is still elsewhere.
      await switchChainAsync({ chainId }).catch(() => undefined)
    }
  }

  const createWallet = async (tokens: Token[]) => {
    if (!address || chainId === null || draft.limitUsd === null) return
    const { tickers } = draft
    await deploy({
      chain_id: chainId,
      deployer: address,
      daily_limit_usd: draft.limitUsd,
      window_secs: draft.windowSecs,
      watched_tokens: tickers === null ? tokens : tokens.filter(t => tickers.includes(t.ticker)),
      prefund_eth: prefund,
    })
  }

  const deployChainId = deployState.chainId ?? chainId ?? 0
  const progress = (
    <DeployProgress
      state={deployState}
      chainId={deployChainId}
      fork={chains.find(c => c.chain_id === deployChainId)?.fork ?? false}
      onRetry={reset}
      onResume={resume}
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
        onBack={() => setStep('fund')}
        onCreate={tokens => void createWallet(tokens)}
        progress={progress}
      />
    )
  } else if (index === 0) {
    body = (
      <StepBody title="Connect the wallet that will own your Mitfah wallet">
        <Text muted>
          Use a browser wallet such as MetaMask. You'll sign with it to create your Mitfah wallet, and only
          it can pause the wallet, change its limits or withdraw.
        </Text>
        <Button appearance="primary" className="mf-step-action" onClick={() => setConnectOpen(true)}>
          Connect wallet
        </Button>
        <ConnectDialog open={connectOpen} onClose={() => setConnectOpen(false)} />
      </StepBody>
    )
  } else if (index === 1 && address) {
    body = (
      <StepBody title="Prove this wallet is yours">
        {me?.owner_addr ? (
          <AccountMismatchBanner
            ownerAddr={me.owner_addr}
            connected={address}
            chainId={walletChainId ?? 1}
            canRelink={walletChains.length === 0}
          />
        ) : (
          <SiweVerifyCard address={address} chainId={walletChainId ?? 1} />
        )}
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
      <StepBody title="Add funds for network fees">
        <Text muted>
          Your Mitfah wallet pays its own network fees from this balance, and the assistant can spend it too
          (within your limit). You can add more any time.
        </Text>
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
        <StepNav onBack={() => setStep('limits')} onNext={() => setStep('review')} nextDisabled={!valid} />
      </StepBody>
    )
  } else {
    body = (
      <ReviewStep
        draft={{ ...draft, chainId, prefund }}
        chain={chain}
        owner={address}
        busy={false}
        onBack={() => setStep('fund')}
        onCreate={tokens => void createWallet(tokens)}
        progress={null}
      />
    )
  }

  return (
    <>
      <PageHeader title="Create your wallet" description="A few steps, then one signature in your wallet." />
      <Panel bordered className="mf-onboarding">
        {mode === 'mobile' ? (
          <div className="mf-step-counter">
            <Text size="sm" muted>
              Step {index + 1} of {STEP_TITLES.length}
            </Text>
            <Text weight="semibold">{STEP_TITLES[index]}</Text>
            <Progress percent={((index + 1) / STEP_TITLES.length) * 100} showInfo={false} />
          </div>
        ) : (
          <Steps current={index} small className="mf-steps">
            {STEP_TITLES.map(title => (
              <Steps.Item key={title} title={title} />
            ))}
          </Steps>
        )}
        <div className="mf-step-body">{body}</div>
      </Panel>
    </>
  )
}

function StepBody({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h2 className="mf-step-title">{title}</h2>
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

function useTokens(chainId: number) {
  return useQuery({ queryKey: ['tokens', chainId], queryFn: () => fetchTokens(chainId), staleTime: 5 * 60_000 })
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
  const selected = draft.tickers ?? tokens.data?.map(t => t.ticker) ?? []
  const limitValid = draft.limitUsd !== null && Number.isInteger(draft.limitUsd) && draft.limitUsd >= 1

  return (
    <StepBody title="How much may the assistant spend?">
      <Form fluid className="mf-auth-form">
        <Form.Group controlId="limit">
          <Form.Label>Spending limit</Form.Label>
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
          <Form.Text>
            The most the assistant can move out of your wallet per period, in US dollars, across all tokens.
          </Form.Text>
          {!limitValid && <Form.Text className="mf-error-text">Enter a whole number of dollars, at least $1.</Form.Text>}
        </Form.Group>

        <Form.Group controlId="window">
          <Form.Label>Period</Form.Label>
          <SegmentedControl
            aria-label="Period"
            data={WINDOWS}
            value={draft.windowSecs}
            onChange={value => onChange({ windowSecs: Number(value) })}
          />
          <Form.Text>The limit refills every {formatWindow(draft.windowSecs)}.</Form.Text>
        </Form.Group>

        <Form.Group controlId="tokens">
          <Form.Label>Tokens that count toward the limit</Form.Label>
          {tokens.isPending ? (
            <Placeholder.Paragraph rows={2} active />
          ) : tokens.isError ? (
            <Message type="error" showIcon>
              Couldn't load the token list.{' '}
              <Button appearance="link" size="sm" onClick={() => void tokens.refetch()}>
                Try again
              </Button>
            </Message>
          ) : (
            <CheckboxGroup
              name="tokens"
              inline
              value={selected}
              onChange={value => onChange({ tickers: value.map(String) })}
            >
              {tokens.data.map(token => (
                <Checkbox key={token.ticker} value={token.ticker}>
                  {token.ticker.toUpperCase()}
                </Checkbox>
              ))}
            </CheckboxGroup>
          )}
          <Form.Text>
            {nativeTicker} always counts. Tokens you untick can be moved without limit — keep them ticked unless
            you have a reason.
          </Form.Text>
        </Form.Group>
      </Form>
      <StepNav onBack={onBack} onNext={onNext} nextDisabled={!limitValid || tokens.isPending} />
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
  const watched = draft.tickers ?? tokens.data?.map(t => t.ticker) ?? []
  const onRightNetwork = walletChainId === draft.chainId

  return (
    <StepBody title="Check the details">
      <dl className="mf-summary">
        <dt>Network</dt>
        <dd>{chainName(draft.chainId ?? undefined)}</dd>
        <dt>Owner</dt>
        <dd>{owner ? <AddressText address={owner} /> : '—'}</dd>
        <dt>Spending limit</dt>
        <dd className="mf-num">
          {draft.limitUsd !== null ? formatUsd(draft.limitUsd) : '—'} every {formatWindow(draft.windowSecs)}
        </dd>
        <dt>Counts toward the limit</dt>
        <dd>{[chain?.native_ticker ?? 'Native token', ...watched.map(t => t.toUpperCase())].join(', ')}</dd>
        <dt>Gas funds</dt>
        <dd className="mf-num">
          {draft.prefund} {chain?.native_ticker}
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
