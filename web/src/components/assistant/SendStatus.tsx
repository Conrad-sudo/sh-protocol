import { useEffect } from 'react'
import { Link } from 'react-router'
import { Text } from 'rsuite'
import { LANDING_GIVE_UP_MS, useLanding } from '../../hooks/useChat'
import { useNow } from '../../hooks/useNow'
import { chainName, explorerUrl } from '../../wallet/chains'

interface SendStatusProps {
  chainId: number
  /** Called whenever the line appears or says something new, to bring it into view. */
  onChange?: () => void
}

/**
 * How the send confirmed last on this network is getting on: the Confirm tap is answered as soon as
 * the transaction is on the wire, and this follows its History row until it lands.
 */
export function SendStatus({ chainId, onChange }: SendStatusProps) {
  const { landing, transaction, error } = useLanding(chainId)
  const now = useNow(5_000)
  const gaveUp = landing !== null && now - landing.startedAt >= LANDING_GIVE_UP_MS
  // What the line says, as one value: a new one is something to bring into view.
  const said = landing && `${landing.transactionId}:${transaction?.status ?? 'pending'}:${Boolean(error)}:${gaveUp}`
  useEffect(() => {
    if (said) onChange?.()
  }, [said, onChange])
  if (!landing) return null

  const action = `“${landing.action}”`
  const history = <Link to="/history">History</Link>
  let body
  if (transaction?.status === 'confirmed') {
    const url = transaction.tx_hash ? explorerUrl(chainId, 'tx', transaction.tx_hash) : null
    body = (
      <>
        Confirmed on {chainName(chainId)}: {landing.action}.{' '}
        {url && (
          <a href={url} target="_blank" rel="noreferrer">
            View transaction
          </a>
        )}
      </>
    )
  } else if (transaction?.status === 'failed') {
    body = (
      <>
        {action} went out but failed on chain. See {history} for why.
      </>
    )
  } else if (transaction?.status === 'dropped') {
    body = <>{action} never ran, so nothing was sent. Ask again for a fresh quote.</>
  } else if (error) {
    body = (
      <>
        Couldn’t check on {action}. Look in {history} before you try again.
      </>
    )
  } else if (gaveUp) {
    body = (
      <>
        {action} still isn’t confirmed. Look in {history} later, before you try again.
      </>
    )
  } else {
    body = <Text as="span" muted>Waiting for the network to confirm {action}…</Text>
  }

  return (
    <p className="mf-send-status" role="status">
      {body}
    </p>
  )
}
