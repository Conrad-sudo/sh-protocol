import { useMutation, useQueryClient } from '@tanstack/react-query'
import { createSiweMessage } from 'viem/siwe'
import type { Address } from 'viem'
import { useSignMessage } from 'wagmi'
import { Button, Message, Text } from 'rsuite'
import { siweNonce, siweVerify } from '../../api/wallet'
import { errorText, isUserRejection } from '../../lib/tx'
import { AddressText } from '../AddressText'

const STATEMENT = 'Link this wallet to your Mitfah account. It will own your Mitfah wallet.'

/**
 * Proves the connected address belongs to this user by signing a Sign-In With Ethereum message.
 * Signing costs nothing and moves no funds. The API binds the address only if the message names
 * this site — which is what stops a look-alike site from reusing the signature.
 */
export function SiweVerifyCard({
  address,
  chainId,
  actionLabel = 'Verify with wallet',
}: {
  address: Address
  chainId: number
  actionLabel?: string
}) {
  const { mutateAsync: signMessageAsync } = useSignMessage()
  const queryClient = useQueryClient()

  const verify = useMutation({
    mutationFn: async () => {
      const { nonce } = await siweNonce()
      const issuedAt = new Date()
      const message = createSiweMessage({
        domain: window.location.host,
        address,
        statement: STATEMENT,
        uri: window.location.origin,
        version: '1',
        chainId,
        nonce,
        issuedAt,
        expirationTime: new Date(issuedAt.getTime() + 10 * 60_000),
      })
      const signature = await signMessageAsync({ message, account: address })
      return siweVerify({ message, signature, nonce })
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['me'] }),
  })

  return (
    <div className="mf-verify">
      <Text>
        Sign a message with <AddressText address={address} /> to prove it's yours. Signing is free and
        doesn't move any funds.
      </Text>
      <Message type="info" showIcon className="mf-settings-note">
        <strong>This address will own your Mitfah wallet.</strong> Only it can pause the wallet, change
        its limits or withdraw. If you lose access to it, Mitfah can't recover your wallet for you.
      </Message>
      {verify.isError && (
        <Message type="error" showIcon className="mf-settings-note">
          {isUserRejection(verify.error) ? 'Cancelled — nothing was signed.' : errorText(verify.error)}
        </Message>
      )}
      <Button appearance="primary" className="mf-step-action" loading={verify.isPending} onClick={() => verify.mutate()}>
        {actionLabel}
      </Button>
    </div>
  )
}
