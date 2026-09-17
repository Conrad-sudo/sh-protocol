import ArrowDownLineIcon from '@rsuite/icons/ArrowDownLine'
import { useNavigate } from 'react-router'
import { Button, Dropdown } from 'rsuite'
import { useSelectedChain } from '../../chain/useSelectedChain'
import { chainName } from '../../wallet/chains'

/**
 * Which network the app is showing, among those the user has a wallet on, plus a way to create a
 * wallet on another one. Hidden until the user has a wallet.
 */
export function ChainSwitcher() {
  const { chainId, setChainId, chains, walletChains } = useSelectedChain()
  const navigate = useNavigate()

  if (chainId === null || walletChains.length === 0) return null

  const canAdd = chains.some(chain => !walletChains.includes(chain.chain_id))
  const isFork = (id: number) => chains.find(chain => chain.chain_id === id)?.fork === true

  return (
    <Dropdown
      placement="bottomStart"
      renderToggle={(props, ref) => (
        <Button
          {...props}
          ref={ref}
          size="sm"
          appearance="subtle"
          className="mf-chain-toggle"
          aria-label={`Network: ${chainName(chainId)}`}
          endIcon={<ArrowDownLineIcon />}
        >
          <span className="mf-chain-dot" aria-hidden="true" />
          <span className="mf-chain-name">{chainName(chainId)}</span>
        </Button>
      )}
    >
      {walletChains.map(id => (
        <Dropdown.Item key={id} active={id === chainId} onSelect={() => setChainId(id)}>
          {chainName(id)}
          {isFork(id) && <small className="mf-muted"> · local test network</small>}
        </Dropdown.Item>
      ))}
      {canAdd && <Dropdown.Separator />}
      {canAdd && <Dropdown.Item onSelect={() => navigate('/wallets/new')}>Add a network</Dropdown.Item>}
    </Dropdown>
  )
}
