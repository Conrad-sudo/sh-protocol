import { useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { useConnection } from 'wagmi'
import { Button, Drawer, Form, Loader, Message, Text } from 'rsuite'
import type { Chain, WalletState } from '../../api/types'
import { useFundWallet, type FundState } from '../../hooks/useFundWallet'
import { useLayoutMode } from '../../layouts/useLayoutMode'
import { isValidAmount } from '../../lib/format'
import { chainName, explorerUrl } from '../../wallet/chains'
import { AmountInput } from '../AmountInput'
import { CopyButton } from '../CopyButton'
import { ConnectDialog } from '../wallet/ConnectDialog'
import { NetworkBanner } from '../wallet/NetworkBanner'

interface FundDrawerProps {
  open: boolean
  onClose: () => void
  wallet: WalletState
  /** The wallet's network as /api/chains describes it. */
  chain: Chain | undefined
}

/**
 * Two ways to add funds: send to the wallet's address from anywhere (QR code for phones and
 * exchanges), or send straight from the browser wallet that is connected here.
 */
export function FundDrawer({ open, onClose, wallet, chain }: FundDrawerProps) {
  const mode = useLayoutMode()
  const { isConnected, chainId: walletChainId } = useConnection()
  const [connectOpen, setConnectOpen] = useState(false)
  const [amount, setAmount] = useState('')
  const { state, send, resume, abandon, release } = useFundWallet(wallet.chain_id, wallet.address)

  const network = chainName(wallet.chain_id)
  const ticker = chain?.native_ticker ?? 'the native token'
  const fork = chain?.fork ?? false
  const busy = state.phase === 'signing' || state.phase === 'confirming'
  const valid = isValidAmount(amount) && Number(amount) > 0
  const onRightNetwork = walletChainId === wallet.chain_id

  const close = () => {
    // A transfer already sent keeps being followed, and shows when the drawer opens again. A wallet
    // that hasn't answered is let go, so the drawer opens clean rather than still waiting on it.
    if (state.phase !== 'confirming') setAmount('')
    release()
    onClose()
  }

  return (
    <Drawer
      open={open}
      onClose={close}
      placement={mode === 'mobile' ? 'bottom' : 'right'}
      size={mode === 'mobile' ? 'full' : 'xs'}
      className="mf-fund-drawer"
    >
      <Drawer.Header>
        <Drawer.Title>Add funds</Drawer.Title>
      </Drawer.Header>
      <Drawer.Body>
        <section className="mf-drawer-section" aria-labelledby="fund-address-title">
          <h3 id="fund-address-title">Send to your wallet's address</h3>
          <div className="mf-qr">
            <QRCodeSVG value={wallet.address} size={168} marginSize={2} title={`QR code of ${wallet.address}`} />
          </div>
          <div className="mf-fund-address">
            <span className="mf-mono">{wallet.address}</span>
            <CopyButton value={wallet.address} label="Wallet address" />
          </div>
          <Message type="warning" showIcon className="mf-settings-note">
            Only send on {network}. Anything sent on another network can be lost. The wallet pays its
            network fees in {ticker}.
          </Message>
          {fork && (
            <Text size="sm" muted>
              This server uses a local test network, so send from your connected wallet below.
            </Text>
          )}
        </section>

        <section className="mf-drawer-section" aria-labelledby="fund-send-title">
          <h3 id="fund-send-title">Or send from your connected wallet</h3>
          {!isConnected ? (
            <>
              <Text muted>Connect a browser wallet such as MetaMask to send {ticker} from it.</Text>
              <Button appearance="primary" className="mf-step-action" onClick={() => setConnectOpen(true)}>
                Connect wallet
              </Button>
              <ConnectDialog open={connectOpen} onClose={() => setConnectOpen(false)} />
            </>
          ) : (
            <>
              <NetworkBanner chainId={wallet.chain_id} fork={fork} />
              <Form fluid className="mf-auth-form">
                <Form.Group controlId="fund-amount">
                  <Form.Label>Amount</Form.Label>
                  <AmountInput
                    id="fund-amount"
                    value={amount}
                    unit={chain?.native_ticker ?? undefined}
                    disabled={busy}
                    onChange={value => {
                      setAmount(value)
                      if (!busy && state.phase !== 'idle') release()
                    }}
                  />
                </Form.Group>
              </Form>
              <Button
                appearance="primary"
                className="mf-step-action"
                loading={busy}
                disabled={!valid || !onRightNetwork}
                onClick={() => void send(amount)}
              >
                Send
              </Button>
              <FundStatus state={state} chainId={wallet.chain_id} onResume={resume} onAbandon={abandon} />
            </>
          )}
        </section>
      </Drawer.Body>
    </Drawer>
  )
}

interface FundStatusProps {
  state: FundState
  chainId: number
  /** Waits again for the transfer already sent. */
  onResume: () => void
  /** Stops waiting on the wallet, before anything is sent. */
  onAbandon: () => void
}

function FundStatus({ state, chainId, onResume, onAbandon }: FundStatusProps) {
  const link = state.txHash ? explorerUrl(chainId, 'tx', state.txHash) : null
  const viewTx = link && (
    <>
      {' '}
      <a href={link} target="_blank" rel="noreferrer">
        View the transaction
      </a>
    </>
  )

  switch (state.phase) {
    case 'signing':
      return (
        // Nothing has been sent yet, so giving up is always safe: a transfer sent later is followed.
        <div className="mf-step-action">
          <Loader content="Confirm the transfer in your wallet." />{' '}
          <Button appearance="link" size="sm" onClick={onAbandon}>
            Stop waiting
          </Button>
        </div>
      )
    case 'confirming':
      return <Loader className="mf-step-action" content="Waiting for the network to confirm the transfer…" />
    case 'done':
      return (
        <Message type="success" showIcon className="mf-settings-note">
          Received. Your balance is up to date.{viewTx}
        </Message>
      )
    case 'cancelled':
      return (
        <Message type="info" showIcon className="mf-settings-note">
          Cancelled — nothing was sent.
        </Message>
      )
    case 'abandoned':
      return (
        <Message type="info" showIcon className="mf-settings-note">
          Stopped waiting. If your wallet still shows the request, reject it there.
        </Message>
      )
    case 'error':
      return (
        <Message type="error" showIcon className="mf-settings-note">
          {state.error}
          {viewTx}
          {state.canResume && (
            <>
              {' '}
              <Button appearance="link" size="sm" onClick={onResume}>
                Check again
              </Button>
            </>
          )}
        </Message>
      )
    default:
      return null
  }
}
