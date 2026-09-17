import { useState } from 'react'
import { useConnection, useDisconnect } from 'wagmi'
import { Button, Dropdown, Message, useToaster } from 'rsuite'
import { shortAddress } from '../../lib/format'
import { chainName } from '../../wallet/chains'
import { ConnectDialog } from './ConnectDialog'

/** The top bar's wallet control: "Connect wallet", or the connected address with a small menu. */
export function ConnectButton() {
  const { address, chainId, isConnected } = useConnection()
  const { mutate: disconnect } = useDisconnect()
  const toaster = useToaster()
  const [open, setOpen] = useState(false)

  if (!isConnected || !address) {
    return (
      <>
        <Button appearance="ghost" size="sm" onClick={() => setOpen(true)}>
          Connect wallet
        </Button>
        <ConnectDialog open={open} onClose={() => setOpen(false)} />
      </>
    )
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(address)
      toaster.push(<Message type="success" showIcon closable>Address copied</Message>, {
        placement: 'topCenter',
        duration: 2500,
      })
    } catch {
      // Clipboard blocked; the full address is still in the menu.
    }
  }

  return (
    <Dropdown
      placement="bottomEnd"
      renderToggle={(props, ref) => (
        <Button {...props} ref={ref} size="sm" appearance="ghost" aria-label={`Wallet ${address}`}>
          <span className="mf-mono">{shortAddress(address)}</span>
        </Button>
      )}
    >
      <Dropdown.Item panel className="mf-wallet-menu-head">
        <span className="mf-mono" title={address}>{shortAddress(address, 10, 8)}</span>
        <br />
        <small>{chainName(chainId)}</small>
      </Dropdown.Item>
      <Dropdown.Separator />
      <Dropdown.Item onSelect={() => void copy()}>Copy address</Dropdown.Item>
      <Dropdown.Item onSelect={() => disconnect()}>Disconnect</Dropdown.Item>
    </Dropdown>
  )
}
