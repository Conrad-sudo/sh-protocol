import { useConnection, useSwitchChain } from 'wagmi'
import { Button, Message, Text } from 'rsuite'
import { errorText, isUserRejection } from '../../lib/tx'
import { chainName, isSupportedChainId } from '../../wallet/chains'

/**
 * Shown when the wallet is on a different network than the action needs. On a server that points
 * the network at a local fork, the wallet must use the same local RPC or it will see a different
 * chain state from the one the API builds transactions against.
 */
export function NetworkBanner({ chainId, fork = false }: { chainId: number; fork?: boolean }) {
  const { chainId: connectedChainId, isConnected } = useConnection()
  const { mutateAsync: switchChainAsync, isPending, error, reset } = useSwitchChain()

  if (!isConnected || connectedChainId === chainId) return null

  const switchNetwork = async () => {
    reset()
    if (!isSupportedChainId(chainId)) return
    try {
      await switchChainAsync({ chainId })
    } catch {
      // Shown below from `error`.
    }
  }

  return (
    <Message type="info" showIcon className="mf-settings-note">
      <Text>
        Your wallet is on {chainName(connectedChainId)}. Switch it to <strong>{chainName(chainId)}</strong>{' '}
        to continue.
      </Text>
      {fork && (
        <Text size="sm" muted>
          This server uses a local test network: in your wallet, set the {chainName(chainId)} RPC URL to{' '}
          <code>http://127.0.0.1:8545</code>.
        </Text>
      )}
      <Button size="sm" appearance="primary" className="mf-step-action" loading={isPending} onClick={() => void switchNetwork()}>
        Switch network
      </Button>
      {error && (
        <Text size="sm" className="mf-error-text">
          {isUserRejection(error) ? 'Cancelled.' : errorText(error)}
        </Text>
      )}
    </Message>
  )
}
